"""Command line entry point: flyscan [--dry-run] <config.con>

--dry-run validates the config and reports exactly what the scan would do -
geometry, time split and data volume - without opening any hardware. Run it
before committing a long scan.
"""
import argparse
import sys

from flyscan.config import parse_fly_config, FlyConfigError
from flyscan.raster import DEFAULT_SLOW_STEP_S


def _duration(seconds):
    if seconds < 1.0:
        return f"{seconds * 1e3:.0f} ms"
    if seconds < 90.0:
        return f"{seconds:.1f} s"
    if seconds < 5400.0:
        return f"{seconds / 60.0:.1f} min"
    return f"{seconds / 3600.0:.2f} h"


def _size(num_bytes):
    if num_bytes < 1e6:
        return f"{num_bytes / 1e3:.1f} kB"
    if num_bytes < 1e9:
        return f"{num_bytes / 1e6:.1f} MB"
    return f"{num_bytes / 1e9:.2f} GB"


def report(config, slow_step_s=DEFAULT_SLOW_STEP_S):
    """The dry-run report: what this config will actually produce."""
    plan = config.plan
    timing = plan.timing(config.channels, config.transfer_mb_s, slow_step_s)

    span_x = plan.fast_span_mm * 1000.0
    span_y = abs(plan.slow_stop_mm - plan.slow_start_mm) * 1000.0
    out = []

    rule = "=" * 72
    out.append(f"\n{rule}\n DRY RUN - {config.path}\n{rule}")

    out.append("\nWHAT THIS PRODUCES")
    out.append(
        f"  {plan.segments} x {plan.lines} copeaks over "
        f"{span_x:.1f} x {span_y:.1f} um"
    )
    out.append(
        f"  native sampling {plan.native_pitch_um:.3f} um along the fast "
        f"axis, {plan.slow_pitch_mm * 1000.0:.3f} um between lines"
    )
    if plan.bin_um is not None:
        out.append(
            f"  binned to {plan.bin_um * 1000:.0f} nm steps: "
            f"{plan.binned_pixels_per_line} x {plan.lines} pixels, "
            f"{plan.copeaks_per_bin:.1f} copeaks averaged each, "
            f"relative SNR {plan.relative_snr:.2f}x"
        )
    else:
        out.append(
            f"  no 'bin' declared, so one copeak per output pixel "
            f"(relative SNR 1.00x)"
        )
    out.append(
        f"  {'bidirectional' if plan.bidirectional else 'unidirectional'}, "
        f"run-up {plan.runup_mm * 1000:.0f} um either end"
    )
    needed = plan.minimum_runup_mm
    if needed is not None:
        out.append(
            f"  run-up needed to reach {plan.velocity_mm_s:g} mm/s: "
            f"{needed * 1000:.1f} um"
        )

    out.append("\nTIME")
    out.append(
        f"  per line   {_duration(timing['per_line_s'])}"
        f"   = {_duration(timing['flight_s'])} scanning"
        f" + {_duration(timing['transfer_s'])} transfer"
        + (f" + {_duration(timing['other_s'])} slow-axis"
           if timing['other_s'] > 0 else "")
    )
    out.append(f"  total      {_duration(timing['total_s'])}")
    out.append(
        f"               {_duration(timing['total_flight_s'])} scanning, "
        f"{_duration(timing['total_transfer_s'])} transfer"
        + (f", {_duration(timing['total_other_s'])} slow-axis"
           if timing['total_other_s'] > 0 else "")
    )
    ratio = timing['transfer_s'] / timing['flight_s'] if timing['flight_s'] else 0
    out.append(
        f"  transfer is {ratio:.1f}x the flight time, so this scan is "
        + ("transfer-bound" if ratio > 1 else "motion-bound")
    )
    out.append(
        f"  (transfer assumes {config.transfer_mb_s:g} MB/s - measure yours)"
    )

    out.append("\nDATA")
    for channel in config.channels.channels:
        factor = config.channels.sparsing[channel]
        points = plan.points_per_segment // factor or 1
        out.append(
            f"  {channel} ({config.channels.roles[channel]})"
            f"  {plan.segments} x {points} pts"
            + (f", sparsed {factor}x" if factor > 1 else "")
        )
    out.append(
        f"  {_size(timing['bytes_per_line'])} per line x {plan.lines} lines "
        f"= {_size(timing['total_bytes'])} total"
    )
    out.append(f"  output: {config.output_path}")

    out.append("\nSCOPE")
    out.append(
        f"  SEQ ON,{plan.segments} - {plan.points_per_segment} points per "
        f"segment over a {plan.segment_seconds * 1e6:.0f} us window "
        f"at {plan.sample_rate_hz / 1e6:g} MS/s"
    )
    out.append(
        f"  confirm your scope accepts {plan.segments} segments; "
        f"configure_line() refuses a short grant rather than misaligning"
    )

    if config.autofocus is not None:
        out.append("\nFOCUS")
        for line in config.autofocus.summary().splitlines():
            out.append(f"  {line}")

    if config.setup_pipeline or config.teardown_pipeline:
        out.append("\nPIPELINES")
        for label, pipeline in (("setup", config.setup_pipeline),
                                ("teardown", config.teardown_pipeline)):
            if pipeline:
                out.append(f"  {label}: "
                           + ", ".join(a['type'] for a in pipeline))

    if config.warnings:
        out.append("\nWARNINGS")
        for warning in config.warnings:
            out.append(f"  - {warning}")

    out.append(f"\n{rule}")
    out.append(" No hardware was opened. Remove --dry-run to run this scan.")
    out.append(rule)
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='flyscan',
        description="On-the-fly ASOPS line scanning.",
    )
    parser.add_argument('config', help="path to a .con configuration file")
    parser.add_argument(
        '--dry-run', action='store_true',
        help="validate and report what the scan would do, then exit without "
             "opening any hardware",
    )
    parser.add_argument(
        '--counts-per-mm', type=float, required=True,
        help="encoder counts per mm for YOUR stage. Required and "
             "deliberately has no default: a wrong value silently rescales "
             "every position. See flyscan/HARDWARE_CHECKLIST.md item 1.",
    )
    parser.add_argument(
        '--slow-step', type=float, default=DEFAULT_SLOW_STEP_S,
        help=f"assumed slow-axis step time in seconds "
             f"(default {DEFAULT_SLOW_STEP_S}); only affects the time "
             f"estimate, and usually hides inside the transfer",
    )
    args = parser.parse_args(argv)

    try:
        config = parse_fly_config(args.config, args.counts_per_mm)
    except FlyConfigError as e:
        print(f"\n[CONFIG ERROR] {e}")
        return 1

    if args.dry_run:
        print(report(config, args.slow_step))
        return 0

    print(report(config, args.slow_step))
    print("\nRunning a scan from the CLI is not wired up yet - the "
          "acquisition layers exist but the autofocus strategy is still "
          "being settled. Use --dry-run, or drive RasterScan directly.")
    return 2


if __name__ == '__main__':
    sys.exit(main())
