#!/usr/bin/env python3
"""Can this machine mmap an NI card's PCI BAR from userspace?

Run as root:

    sudo python3 dev/check_bar_mmap.py

This isolates the one operation the nixseries backend depends on, using
nothing but the Python standard library - no pscan, no shim, no DDK. If this
fails, the problem is the kernel's policy on BAR mapping rather than anything
in pscan, and the errno says which policy.

It reads the first few registers and prints them. A BAR that maps but reads
back as all 0xFF means the mapping succeeded while the device is not actually
responding, which is a different fault with a different fix.

NOTHING IS WRITTEN. It only reads.
"""

import mmap
import os
import sys

PCI_DEVICES = "/sys/bus/pci/devices"
NI_VENDOR = 0x1093


def attribute(slot, name):
    with open(os.path.join(PCI_DEVICES, slot, name)) as f:
        return f.read().strip()


def find_ni_cards():
    found = []
    for slot in sorted(os.listdir(PCI_DEVICES)):
        try:
            if int(attribute(slot, "vendor"), 16) != NI_VENDOR:
                continue
            found.append(slot)
        except (OSError, ValueError):
            continue
    return found


def bar0_geometry(slot):
    """(start, size) of BAR0, from the device's 'resource' attribute."""
    with open(os.path.join(PCI_DEVICES, slot, "resource")) as f:
        start, end, flags = (int(v, 16) for v in f.readline().split())
    return start, (end - start + 1 if end > start else 0), flags


def main():
    if os.geteuid() != 0:
        print("Run this as root - mapping a PCI BAR is privileged.")
        return 1

    cards = find_ni_cards()
    if not cards:
        print(f"No National Instruments device (vendor 0x{NI_VENDOR:04x}) "
              f"found in {PCI_DEVICES}.")
        return 1

    slot = sys.argv[1] if len(sys.argv) > 1 else cards[0]
    print(f"card           {slot}")
    print(f"device id      0x{int(attribute(slot, 'device'), 16):04x} "
          f"(generic across X Series)")
    print(f"subsystem id   0x{int(attribute(slot, 'subsystem_device'), 16):04x} "
          f"(identifies the model)")

    try:
        print(f"enable         {attribute(slot, 'enable')}")
    except OSError:
        print("enable         (unreadable)")

    # Is a driver bound? A driver that has claimed the region is the most
    # likely reason the kernel refuses to map it.
    driver = os.path.join(PCI_DEVICES, slot, "driver")
    if os.path.islink(driver):
        print(f"driver bound   {os.path.basename(os.readlink(driver))} "
              f"<-- this will block userspace mapping")
    else:
        print("driver bound   none (good)")

    start, size, flags = bar0_geometry(slot)
    print(f"BAR0           0x{start:x}, {size} bytes, flags 0x{flags:x}")

    if size == 0:
        print("\nBAR0 has no size - the BIOS has not assigned it.")
        return 1

    # Enable memory decoding, as the backend does. Harmless if already on.
    try:
        with open(os.path.join(PCI_DEVICES, slot, "enable"), "w") as f:
            f.write("1\n")
        print("enable write   ok")
    except OSError as e:
        print(f"enable write   FAILED: {e}")

    path = os.path.join(PCI_DEVICES, slot, "resource0")
    print(f"\nmapping        {path}")

    try:
        fd = os.open(path, os.O_RDWR | os.O_SYNC)
    except OSError as e:
        print(f"  open O_SYNC failed: [{e.errno}] {e.strerror}")
        try:
            fd = os.open(path, os.O_RDWR)
            print("  open without O_SYNC: ok")
        except OSError as e2:
            print(f"  open failed: [{e2.errno}] {e2.strerror}")
            return 1
    else:
        print("  open: ok")

    try:
        region = mmap.mmap(fd, size, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE)
    except OSError as e:
        print(f"  mmap FAILED: [{e.errno}] {e.strerror}")
        print("")
        if e.errno == 22:  # EINVAL
            print("  EINVAL means the kernel is refusing userspace access to")
            print("  this BAR. The two usual causes:")
            print("    1. A driver has claimed the region (see 'driver bound'")
            print("       above, and 'lspci -ks " + slot + "').")
            print("    2. CONFIG_IO_STRICT_DEVMEM=y, which forbids it. Check:")
            print("         grep IO_STRICT_DEVMEM /boot/config-$(uname -r)")
            print("       and if set, add 'iomem=relaxed' to the kernel")
            print("       command line and reboot.")
        elif e.errno in (1, 13):  # EPERM, EACCES
            print("  Permission denied even as root, which points at a")
            print("  hardening policy - lockdown mode, or Secure Boot with")
            print("  kernel lockdown enabled. Check:")
            print("         cat /sys/kernel/security/lockdown")
        os.close(fd)
        return 1

    print("  mmap: ok")

    words = [
        int.from_bytes(region[offset:offset + 4], "little")
        for offset in range(0, 32, 4)
    ]
    print("\n  first 8 BAR0 words:")
    for i, w in enumerate(words):
        print(f"    +0x{i * 4:02x}  0x{w:08x}")

    region.close()
    os.close(fd)

    if all(w == 0xFFFFFFFF for w in words):
        print("\n  All ones: the mapping worked but the device is not")
        print("  responding. Memory decoding may be off despite the enable")
        print("  write, or the card is not powered/seated properly.")
        return 1
    if all(w == 0 for w in words):
        print("\n  All zeroes: suspicious. Possible but worth a second look.")
        return 1

    print("\n  Plausible register contents - BAR mapping works on this")
    print("  machine, so the nixseries backend's bus layer is sound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
