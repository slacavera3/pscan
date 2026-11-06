"""pydaq - inspect and poke the DAQ cards directly, outside a scan.

This is the bring-up tool. Before writing a .con file against a new card you
want to know: does pscan see it, under which backend, what are its channels
and ranges, is there actually a signal on the photodiode input, and does the
mirror move when told to. Doing that through a scan config means editing a
file and reading a log; doing it here is one command.

  pydaq list                       what pscan can see, and what it can't
  pydaq read --channel 0           acquire a short trace, print its stats
  pydaq set 1.5 --channel 0        hold an output at 1.5 V
  pydaq set +0.1 --channel 0       step it, relative to where it is

`list` opens nothing it does not have to, and never opens the Phidget board -
that board may be holding a piezo at a position right now, and enumerating
hardware is not a good enough reason to disturb it. Ask for it by name if you
mean it.
"""

import argparse
import sys
import time

import numpy as np

from pscan import daq
from pscan.daq import DEFAULT_TERMINAL, TERMINALS


def cmd_list(args):
    """Print every card pscan can find, and why a backend is unusable."""
    print("BACKENDS")
    for name, reason in sorted(daq.available_backends().items()):
        mark = "ok " if reason == "available" else "-- "
        hidden = ("" if daq.backend_class(name).discoverable()
                  else "  (by name only)")
        print(f"  {mark}{name:<9}{hidden}")
        if reason != "available":
            print(f"      {reason}")

    devices = daq.discover(include_hidden=args.all)
    print(f"\nDEVICES{' (including by-name-only backends)' if args.all else ''}")
    if not devices:
        print("  (none)")
        if not args.all:
            print("  Try 'pydaq list --all' to include the Phidget board."
                  "")
        return 0

    for info in devices:
        print(f"  {info.summary()}")
        print(f"      .con lines:  backend {info.backend}"
              f"{'' if info.address is None else f'   /   device {info.address}'}")
    return 0


def _open(args, need_ai=False, need_ao=False):
    role = 'a2d' if need_ai else 'd2a'
    try:
        return daq.open_device(
            backend=args.backend, address=args.device,
            need_ai=need_ai, need_ao=need_ao, role=role,
        )
    except daq.DaqSelectionError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"\n[ERROR] Could not open the card: {e}")
        sys.exit(1)


def cmd_read(args):
    """Acquire one short trace and report what was on the input.

    Deliberately prints statistics rather than samples: the question at
    bring-up is "is this plausibly my photodiode?", which a mean, a span and a
    standard deviation answer and 2000 numbers scrolling past do not.
    """
    session = _open(args, need_ai=True)

    print(f"\nReading ai{args.channel}: {args.samples} samples @ "
          f"{args.rate} Hz, {args.terminal}...")

    # Acquired to a scratch file because that is the acquisition path pscan
    # actually uses in a scan - reading through it here means this command
    # exercises the same code, not a shortcut around it.
    path = args.out
    ok = session.acquire_a2d(
        channel=args.channel,
        range_val=args.range,
        n_samples=args.samples,
        sample_rate=args.rate,
        output_filename=path,
        is_first_trace=True,
        v_min=args.v_min,
        v_max=args.v_max,
        terminal=args.terminal,
    )

    # The write happens on the backend's writer thread, so the file is not
    # there until the session is closed.
    daq.close_all()

    if not ok:
        print("\n[ERROR] The acquisition failed. Nothing was recorded.")
        return 1

    data = np.fromfile(path, dtype=np.float32)
    if data.size == 0:
        print(f"\n[ERROR] {path} is empty.")
        return 1

    print(f"\n  samples   {data.size}")
    print(f"  mean      {data.mean():+.6f} V")
    print(f"  min, max  {data.min():+.6f} V, {data.max():+.6f} V")
    print(f"  span      {data.max() - data.min():.6f} V")
    print(f"  std dev   {data.std():.6f} V")
    print(f"\n  Written to {path} as float32 volts - the same format a scan's "
          f".d file uses.")
    return 0


def cmd_set(args):
    """Hold an output channel at a voltage, absolutely or relatively."""
    session = _open(args, need_ao=True)

    text = args.volts
    relative = text[0] in '+-' and len(text) > 1

    try:
        value = float(text)
    except ValueError:
        print(f"\n[ERROR] '{text}' is not a voltage.")
        return 1

    if relative:
        current = session.read_d2a(args.channel)
        if current is None:
            print(
                f"\n[ERROR] A relative step needs to know where ao{args.channel} "
                f"is, and this card cannot be asked. Nothing has been set from "
                f"this session either. Give an absolute voltage instead."
            )
            return 1
        source = "measured" if session.read_d2a_is_measured() else "last set"
        print(f"  ao{args.channel} is at {current:+.4f} V ({source})")
        target = current + value
    else:
        target = value

    print(f"  Setting ao{args.channel} to {target:+.4f} V...")
    if not session.write_d2a(args.channel, target):
        print("\n[ERROR] The write failed.")
        return 1

    if args.hold:
        # Keeping the process alive keeps the card open, which is the only
        # way to hold an output on a VFIO-backed backend: vfio-pci resets
        # the device when its last file descriptor closes, deliberately, so
        # a userspace driver cannot leave hardware running unattended. That
        # reset zeroes the DACs.
        #
        # It is also how to MEASURE an output: without it the voltage exists
        # for the few milliseconds between the write and process exit, which
        # a handheld meter sees only as a flicker.
        print(f"\n  Holding {target:+.4f} V on ao{args.channel}.")
        print("  Measure it now. Ctrl-C to release.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  Released. The output resets as the card closes.")
        return 0

    print(f"  Done. Note the output may not survive this process exiting -")
    print(f"  use --hold to keep it applied, and see 'pydaq set --help'.")
    return 0


def cmd_selftest(args):
    """Exercise the acquisition chain using a signal internal to the card.

    Nothing need be connected. On an X Series card this reads the on-board
    temperature sensor through the same ADC, timing engine and FIFO a real
    measurement uses, so it separates "the card cannot acquire at all" from
    "the card cannot see what is wired to it" - two faults that look
    identical when every sample comes back as zero.
    """
    session = _open(args, need_ai=True)

    if not hasattr(session, 'selftest'):
        print(f"\nThe '{session.name}' backend has no self-test.")
        return 1

    try:
        temperature, volts = session.selftest()
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        return 1

    print(f"\n  sensor voltage  {volts:+.6f} V")
    print(f"  temperature     {temperature:.1f} C")

    if abs(volts) < 1e-6:
        print("\n  The sensor read 0 V, so the ADC is not converting even an")
        print("  internal signal. The fault is below channel selection -")
        print("  nothing to do with what is or is not wired up.")
        return 1

    if not 5.0 <= temperature <= 90.0:
        print(f"\n  {temperature:.1f} C is not a plausible board temperature.")
        print("  The chain runs but the conversion or scaling is wrong.")
        return 1

    print("\n  Plausible. The ADC, timing engine, FIFO and calibration all")
    print("  work, so any problem reading a real input is in selecting the")
    print("  external channel or in the signal itself.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='pydaq',
        description="Inspect and drive the DAQ cards pscan can reach.",
    )
    subs = parser.add_subparsers(dest='command', required=True)

    def add_target(sub):
        sub.add_argument(
            '--backend', choices=daq.backend_names(),
            help="which driver to use. Omit to auto-detect, which refuses to "
                 "guess when more than one card could serve the request.",
        )
        sub.add_argument(
            '--device',
            help="which card: a comedi node like /dev/comedi0, or a DAQmx "
                 "device name like Dev1.",
        )

    p_list = subs.add_parser('list', help="show the cards pscan can find")
    p_list.add_argument(
        '--all', action='store_true',
        help="also list the backends auto-detection skips (the Phidget "
             "analog board).",
    )
    p_list.set_defaults(func=cmd_list)

    p_read = subs.add_parser('read', help="acquire a trace and print its stats")
    add_target(p_read)
    p_read.add_argument('--channel', type=int, default=0,
                        help="analog input channel (default 0)")
    p_read.add_argument('--samples', type=int, default=1000,
                        help="samples to acquire (default 1000)")
    p_read.add_argument('--rate', type=float, default=10000,
                        help="sample rate in Hz (default 10000)")
    p_read.add_argument('--range', type=int, default=None, dest='range',
                        help="range index, as a .con file's 'range' line. "
                             "Prefer --v-min/--v-max.")
    p_read.add_argument('--v-min', type=float, default=None, dest='v_min',
                        help="lowest voltage to resolve a range for")
    p_read.add_argument('--v-max', type=float, default=None, dest='v_max',
                        help="highest voltage to resolve a range for")
    p_read.add_argument('--terminal', choices=TERMINALS,
                        default=DEFAULT_TERMINAL,
                        help=f"input wiring (default {DEFAULT_TERMINAL})")
    p_read.add_argument('--out', default='pydaq_read.d',
                        help="where to write the trace (default pydaq_read.d)")
    p_read.set_defaults(func=cmd_read)

    p_self = subs.add_parser(
        'selftest',
        help="check the acquisition chain with a signal internal to the card")
    add_target(p_self)
    p_self.set_defaults(func=cmd_selftest)

    p_set = subs.add_parser('set', help="hold an output at a voltage")
    add_target(p_set)
    p_set.add_argument('volts',
                       help="target voltage, or +X / -X to step from where "
                            "the channel is now")
    p_set.add_argument('--channel', type=int, default=0,
                       help="analog output channel (default 0)")
    p_set.add_argument(
        '--hold', action='store_true',
        help="keep the voltage applied until Ctrl-C. Needed to measure it, "
             "and on the nixseries backend needed at all: vfio-pci resets "
             "the card when the process exits, which zeroes the outputs.")
    p_set.set_defaults(func=cmd_set)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        return sys.exit(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return sys.exit(130)
    finally:
        daq.close_all()


if __name__ == '__main__':
    main()
