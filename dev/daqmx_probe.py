#!/usr/bin/env python3
"""Ask NI-DAQmx directly about the card, with none of pscan in the way.

    sudo python3 dev/daqmx_probe.py [Dev1]
    sudo python3 dev/daqmx_probe.py Dev1 --reset

WHY THIS EXISTS

Analog output works under DAQmx and analog input fails with

    -50152  A hardware failure has occurred.

pscan's own task setup is conventional - add_ai_voltage_chan,
cfg_samp_clk_timing, TASK_COMMIT - and the error arrives before a sample is
read, so it is not obviously ours. But "not obviously ours" has been wrong
five times on this card, so this asks NI's library the same questions with
pscan's driver removed from the picture entirely.

Each step is reported separately, because WHICH one fails says something
different:

    self-test fails          NI's own verdict on the hardware. Carries
                             weight in a support case in a way our code
                             never will.
    single read fails        The simplest possible acquisition - one sample,
                             software timed, no clock, no buffer. If this
                             fails the analog input path is broken outright.
    timed read fails alone   The converter works and the timing engine does
                             not, which is a much narrower fault.

--reset calls DAQmx's own reset_device() first. Worth having after weeks of
poking this card's registers directly through the MHDDK shim: it is NI's
supported way to put the board back to a known state, and unlike a warm
reboot it is not a no-op for a device that never lost power.
"""

import sys


def step(name):
    print(f"\n--- {name}")


def iommu_report():
    """Is the IOMMU blocking the card's DMA?

    This is the question when software-timed acquisition works and
    hardware-timed does not. A single on-demand read is programmed I/O, and
    so is an on-demand AO write - both work. A buffered acquisition uses DMA,
    and that is the one that fails. The thing that blocks DMA is the IOMMU.

    pscan's own installer is what turned it on: vfio-pci cannot work without
    an IOMMU, so the VFIO instructions say to add intel_iommu=on. On the
    DAQmx path that is not merely unnecessary, it is actively in the way -
    NI's kernel driver does not use the kernel DMA API the way the IOMMU
    expects, so translation faults the transfer.

    A blocked transfer logs a DMAR fault, so this looks rather than guesses.
    """
    import re
    import subprocess

    print("\n--- IOMMU, since buffered acquisition needs DMA")

    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read().strip()
    except OSError:
        cmdline = ""

    on = ("intel_iommu=on" in cmdline) or ("amd_iommu=on" in cmdline)
    passthrough = "iommu=pt" in cmdline
    print(f"  kernel cmdline : {cmdline or '(unreadable)'}")
    print(f"  IOMMU forced on: {'yes' if on else 'no'}")
    print(f"  passthrough    : {'yes' if passthrough else 'NO'}")

    faults = []
    try:
        out = subprocess.run(["dmesg", "-T"], capture_output=True,
                             text=True, timeout=30)
        for line in out.stdout.splitlines():
            if re.search(r"DMAR:.*(fault|PTE|Translation)", line, re.I):
                faults.append(line)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  dmesg unavailable ({e}) - run as root to read it")

    if faults:
        print(f"\n  {len(faults)} DMAR fault line(s), most recent last:")
        for line in faults[-6:]:
            print(f"    {line}")
        print("\n  CONFIRMED: the IOMMU is refusing the card's DMA. That is")
        print("  why a buffered read fails while a single sample works.")
    else:
        print("\n  No DMAR faults in dmesg.")
        if on and not passthrough:
            print("  Not conclusive - a transfer refused before it starts may")
            print("  log nothing. The IOMMU is forced on without passthrough,")
            print("  which is still the leading suspect.")

    if on and not passthrough:
        print("\n  THE FIX, if this is it:")
        print("    sudo nano /etc/default/grub")
        print("      GRUB_CMDLINE_LINUX_DEFAULT=\"... intel_iommu=on iommu=pt\"")
        print("    sudo update-grub && sudo reboot")
        print("")
        print("  iommu=pt gives host drivers an identity mapping, so NI's DMA")
        print("  works, while leaving the IOMMU available if the VFIO shim is")
        print("  ever wanted again. Removing intel_iommu=on entirely would")
        print("  also work and would close that door.")
    elif on and passthrough:
        print("\n  Already in passthrough, so the IOMMU should not be")
        print("  blocking anything. Look elsewhere.")
    else:
        print("\n  IOMMU is not forced on, so it is probably not the cause.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    device_name = args[0] if args else "Dev1"
    do_reset = "--reset" in sys.argv[1:]

    try:
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, TerminalConfiguration
    except Exception as e:
        print(f"Cannot import nidaqmx: {e}")
        print("Install it with 'bash install.sh', which also installs the")
        print("driver underneath it.")
        return 1

    step("Devices DAQmx can see")
    try:
        system = nidaqmx.system.System.local()
        names = [d.name for d in system.devices]
        print(f"  driver version : {system.driver_version}")
        print(f"  devices        : {names or '(none)'}")
        if device_name not in names:
            print(f"\n  '{device_name}' is not among them. If the list is empty,")
            print("  the card is probably still bound to vfio-pci - DAQmx")
            print("  cannot see a device another driver owns. Check:")
            print("    ls -l /sys/bus/pci/devices/0000:82:00.0/driver")
            return 1
    except Exception as e:
        print(f"  FAILED: {e}")
        return 1

    device = nidaqmx.system.Device(device_name)

    if do_reset:
        step("reset_device()")
        try:
            device.reset_device()
            print("  ok - board returned to its power-on state")
        except Exception as e:
            print(f"  FAILED: {e}")

    step("self_test_device()  <- NI's own verdict on the hardware")
    try:
        device.self_test_device()
        print("  PASSED")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("\n  This is NI's supported driver reporting its own hardware")
        print("  as faulty. That is the single most useful line to put in a")
        print("  support case - it involves none of our code.")

    step("product type / serial")
    for attr in ("product_type", "serial_num"):
        try:
            value = getattr(device, attr)
            print(f"  {attr:<14}: {value if not isinstance(value, int) else hex(value)}")
        except Exception as e:
            print(f"  {attr:<14}: unavailable ({e})")

    step("ONE sample, software timed - the simplest acquisition there is")
    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                f"{device_name}/ai0",
                min_val=-10.0, max_val=10.0,
                terminal_config=TerminalConfiguration.RSE,
            )
            print(f"  ai0 reads {task.read():+.6f} V")
            print("  OK - the converter works. Nothing is connected, so any")
            print("  value is fine; that it returned at all is the result.")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("\n  No clock, no buffer, no timing engine involved. A failure")
        print("  here means the analog input path itself is not working.")

    step("1000 samples at 10 kHz - what pscan actually does")
    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                f"{device_name}/ai0",
                min_val=-10.0, max_val=10.0,
                terminal_config=TerminalConfiguration.RSE,
            )
            task.timing.cfg_samp_clk_timing(
                rate=10000.0,
                sample_mode=AcquisitionType.FINITE,
                samps_per_chan=1000,
            )
            data = task.read(number_of_samples_per_channel=1000, timeout=10.0)
            lo, hi = min(data), max(data)
            mean = sum(data) / len(data)
            print(f"  samples  {len(data)}")
            print(f"  mean     {mean:+.6f} V")
            print(f"  min,max  {lo:+.6f}, {hi:+.6f} V")
            print(f"  span     {hi - lo:.6f} V")
            if hi == lo:
                print("\n  Every sample identical. That is what the MHDDK shim")
                print("  saw too (all 0x0000), and it is not what a floating")
                print("  input does - a real converter shows noise.")
            else:
                print("\n  Varying samples: the converter and the timing engine")
                print("  both work.")
        timed_ok = True
    except Exception as e:
        print(f"  FAILED: {e}")
        timed_ok = False

    # Only when the pattern actually points at DMA: the converter answers a
    # software-timed read but a buffered one fails.
    if not timed_ok:
        iommu_report()

    step("Summary")
    print("  If the single read works and the timed one does not, the fault")
    print("  is in the timing engine, not the converter.")
    print("  If self-test fails, raise it with NI - that verdict is theirs,")
    print("  not ours, and this card is known good on Windows.")
    print("  If everything passes, pscan's own task setup is the difference")
    print("  and that is ours to fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
