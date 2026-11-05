#!/usr/bin/env python3
"""Can the card's BAR be mapped through vfio-pci on this machine?

    sudo python3 dev/check_vfio.py [0000:82:00.0]

WHY THIS EXISTS

Kernel lockdown - which Secure Boot turns on automatically - blocks mapping a
PCI BAR through /sys/bus/pci/devices/<slot>/resource0. It returns EPERM and
root does not override it. Lockdown cannot be lowered at runtime, and turning
Secure Boot off means a BIOS visit, which is not something an installer can
do and not something to ask a new user to do.

vfio-pci should be the way through: it is an in-tree, signed driver, and the
IOMMU contains whatever the device tries to DMA, which is the reason lockdown
can permit it when it forbids a raw BAR poke. The evidence is strong but
indirect - a 2021 kernel patch locked down VFIO's *no-IOMMU* mode
specifically, on the grounds that without an IOMMU it is "as unsafe as
accessing the PCI BARs via the device's sysfs", which implies the normal
IOMMU-backed path is not locked down.

Strong implication is not proof, and a VFIO backend is a day's work. So this
script tests the one thing that decides it, and nothing else.

WHAT IT DOES

Binds the card to vfio-pci, opens the VFIO container and group, maps BAR0 and
reads the first few registers. It only reads from the device.

It does CHANGE DRIVER BINDING, which is a real change to the system: the card
is left bound to vfio-pci afterwards. Nothing else uses this card, and
--restore puts it back. Binding does not alter the card's outputs.
"""

import fcntl
import mmap
import os
import struct
import sys

PCI_DEVICES = "/sys/bus/pci/devices"

# VFIO ioctls. All are _IO(';', nr), i.e. no direction or size encoded:
#   _IOC(dir=0, type=';', nr, size=0) == (0x3B << 8) | nr
VFIO_TYPE = 0x3B


def _io(nr):
    return (VFIO_TYPE << 8) | nr


VFIO_GET_API_VERSION = _io(100)
VFIO_CHECK_EXTENSION = _io(101)
VFIO_SET_IOMMU = _io(102)
VFIO_GROUP_GET_STATUS = _io(103)
VFIO_GROUP_SET_CONTAINER = _io(104)
VFIO_GROUP_GET_DEVICE_FD = _io(106)
VFIO_DEVICE_GET_REGION_INFO = _io(108)

VFIO_API_VERSION = 0
VFIO_TYPE1_IOMMU = 1
VFIO_GROUP_FLAGS_VIABLE = 1
VFIO_PCI_BAR0_REGION_INDEX = 0


def iommu_group(slot):
    """The card's IOMMU group number, or None if the IOMMU is off."""
    link = os.path.join(PCI_DEVICES, slot, "iommu_group")
    if not os.path.islink(link):
        return None
    return os.path.basename(os.readlink(link))


def current_driver(slot):
    link = os.path.join(PCI_DEVICES, slot, "driver")
    if not os.path.islink(link):
        return None
    return os.path.basename(os.readlink(link))


def bind_to_vfio(slot):
    """Point the card at vfio-pci. Returns the driver it had before."""
    previous = current_driver(slot)

    if previous == "vfio-pci":
        print("  already bound to vfio-pci")
        return previous

    if previous:
        print(f"  unbinding from {previous}")
        with open(os.path.join(PCI_DEVICES, slot, "driver", "unbind"), "w") as f:
            f.write(slot)

    # driver_override pins which driver may claim it, so drivers_probe cannot
    # hand the card to anything else.
    with open(os.path.join(PCI_DEVICES, slot, "driver_override"), "w") as f:
        f.write("vfio-pci")
    with open("/sys/bus/pci/drivers_probe", "w") as f:
        f.write(slot)

    bound = current_driver(slot)
    print(f"  now bound to: {bound}")
    return previous


def restore(slot):
    """Undo the binding: clear the override and re-probe."""
    try:
        if current_driver(slot) == "vfio-pci":
            with open(os.path.join(PCI_DEVICES, slot, "driver", "unbind"), "w") as f:
                f.write(slot)
        with open(os.path.join(PCI_DEVICES, slot, "driver_override"), "w") as f:
            f.write("\n")
        with open("/sys/bus/pci/drivers_probe", "w") as f:
            f.write(slot)
        print(f"restored: driver is now {current_driver(slot) or 'none'}")
    except OSError as e:
        print(f"restore failed: {e}")
    return 0


def probe(slot):
    group = iommu_group(slot)
    print(f"card           {slot}")
    print(f"IOMMU group    {group}")

    if group is None:
        print("\nNo IOMMU group: the IOMMU is not enabled, and vfio-pci")
        print("needs it. Add 'intel_iommu=on' to the kernel command line:")
        print("    /etc/default/grub -> GRUB_CMDLINE_LINUX_DEFAULT")
        print("    sudo update-grub && sudo reboot")
        print("That is scriptable, unlike a BIOS change - though VT-d must")
        print("also be enabled in firmware, which usually it already is.")
        return 1

    try:
        with open("/sys/kernel/security/lockdown") as f:
            print(f"lockdown       {f.read().strip()}")
    except OSError:
        print("lockdown       (not reported)")

    print("\nbinding to vfio-pci")
    try:
        bind_to_vfio(slot)
    except OSError as e:
        print(f"  bind FAILED: [{e.errno}] {e.strerror}")
        print("  Is the vfio-pci module available?  sudo modprobe vfio-pci")
        return 1

    group_path = f"/dev/vfio/{group}"
    if not os.path.exists(group_path):
        print(f"\n{group_path} does not exist even after binding.")
        print("Load the module:  sudo modprobe vfio-pci")
        return 1

    print(f"\nopening        /dev/vfio/vfio and {group_path}")
    container = os.open("/dev/vfio/vfio", os.O_RDWR)
    group_fd = os.open(group_path, os.O_RDWR)

    try:
        version = fcntl.ioctl(container, VFIO_GET_API_VERSION)
        print(f"  API version  {version}")
        if version != VFIO_API_VERSION:
            print(f"  unexpected (wanted {VFIO_API_VERSION})")
            return 1

        if not fcntl.ioctl(container, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU):
            print("  type1 IOMMU not supported")
            return 1
        print("  type1 IOMMU  supported")

        # struct vfio_group_status { __u32 argsz; __u32 flags; }
        status = bytearray(struct.pack("II", 8, 0))
        fcntl.ioctl(group_fd, VFIO_GROUP_GET_STATUS, status)
        _argsz, flags = struct.unpack("II", bytes(status))
        if not (flags & VFIO_GROUP_FLAGS_VIABLE):
            print("  group is NOT viable - some device in this IOMMU group")
            print("  is still bound to another driver. Everything in a group")
            print("  must be bound to vfio-pci together.")
            return 1
        print("  group        viable")

        fcntl.ioctl(group_fd, VFIO_GROUP_SET_CONTAINER,
                    struct.pack("i", container))
        fcntl.ioctl(container, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU)
        print("  container    set, IOMMU enabled")

        device_fd = fcntl.ioctl(group_fd, VFIO_GROUP_GET_DEVICE_FD,
                                slot.encode() + b"\0")
        print(f"  device fd    {device_fd}")

        # struct vfio_region_info {
        #    __u32 argsz; __u32 flags; __u32 index; __u32 cap_offset;
        #    __u64 size;  __u64 offset; }
        info = bytearray(struct.pack("IIIIQQ", 32, 0,
                                     VFIO_PCI_BAR0_REGION_INDEX, 0, 0, 0))
        fcntl.ioctl(device_fd, VFIO_DEVICE_GET_REGION_INFO, info)
        _argsz, rflags, _index, _cap, size, offset = struct.unpack(
            "IIIIQQ", bytes(info))
        print(f"  BAR0         {size} bytes at device offset 0x{offset:x}, "
              f"flags 0x{rflags:x}")

        if size == 0:
            print("  BAR0 has no size")
            return 1

        region = mmap.mmap(device_fd, size, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=offset)
        print("\n  mmap THROUGH VFIO: ok")

        words = [int.from_bytes(region[o:o + 4], "little")
                 for o in range(0, 32, 4)]
        print("  first 8 BAR0 words:")
        for i, w in enumerate(words):
            print(f"    +0x{i * 4:02x}  0x{w:08x}")
        region.close()

        if all(w == 0xFFFFFFFF for w in words):
            print("\n  All ones - mapped, but the device is not responding.")
            return 1
        if all(w == 0 for w in words):
            print("\n  All zeroes - suspicious, worth a second look.")
            return 1

        print("\n  VFIO WORKS under this lockdown mode. The nixseries")
        print("  backend can reach the card this way, with no BIOS change")
        print("  and - with a udev rule - without root.")
        return 0

    except OSError as e:
        print(f"\n  FAILED: [{e.errno}] {e.strerror}")
        if e.errno == 1:
            print("  EPERM again: lockdown is blocking VFIO too, so this")
            print("  route is closed and Secure Boot is unavoidable.")
        return 1
    finally:
        os.close(group_fd)
        os.close(container)


def main():
    if os.geteuid() != 0:
        print("Run as root - binding a driver and opening VFIO need it.")
        print("(The real backend will not, once a udev rule is in place.)")
        return 1

    args = [a for a in sys.argv[1:] if a != "--restore"]
    slot = args[0] if args else "0000:82:00.0"

    if not os.path.isdir(os.path.join(PCI_DEVICES, slot)):
        print(f"No such PCI device: {slot}")
        return 1

    if "--restore" in sys.argv:
        return restore(slot)
    return probe(slot)


if __name__ == "__main__":
    sys.exit(main())
