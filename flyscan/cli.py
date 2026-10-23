"""Command line entry point: flyscan [--dry-run] <config.con>

--dry-run validates the config and reports exactly what the scan would do -
geometry, time split and data volume - without opening any hardware. Run it
before committing a long scan.
"""
import argparse
import subprocess
import sys
import time

from flyscan.config import parse_fly_config, FlyConfigError
from flyscan.raster import DEFAULT_SLOW_STEP_S, RasterScan, build_writer


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


def _run_pipeline(pipeline, label):
    """Run the setup or teardown actions the fly runner supports.

    Only the blocks that make sense once per scan. Anything else is skipped
    with a warning rather than silently ignored.
    """
    for action in pipeline:
        kind = action['type']
        params = action['params']

        if kind == 'script':
            print(f"  [{label}] {params['cmd']}")
            subprocess.run(params['cmd'], shell=True, check=True,
                           capture_output=True, text=True)
        elif kind == 'delay':
            print(f"  [{label}] delay {params['seconds']}s")
            time.sleep(params['seconds'])
        else:
            print(f"  [WARNING] '{kind}' is not wired into the fly runner "
                  f"yet and was skipped.")


def _apply_velocity(stage, plan, calibration):
    """Command the fast axis velocity, and read it back.

    Only max_velocity is changed: the acceleration scaling factor is a
    different constant from the velocity one and is not calibrated, so
    whatever the controller already holds is left alone rather than
    guessed at.
    """
    raw = calibration.raw_velocity(plan.velocity_mm_s)
    current = stage.get_velocity_params_raw(plan.fast_axis)
    if current is None:
        raise RuntimeError(
            "stage did not report its velocity parameters, so they cannot "
            "be set safely"
        )

    print(f"  setting {plan.fast_axis} to {plan.velocity_mm_s} mm/s "
          f"(raw {raw}; acceleration left at {current['acceleration']})")
    stage.set_velocity_params_raw(
        plan.fast_axis, max_velocity=raw,
        acceleration=current['acceleration'],
        min_velocity=current['min_velocity'],
    )
    time.sleep(0.05)

    back = stage.get_velocity_params_raw(plan.fast_axis)
    if back is None or back['max_velocity'] != raw:
        got = 'no reply' if back is None else back['max_velocity']
        raise RuntimeError(
            f"velocity readback was {got}, not the {raw} requested. The "
            f"segment count is derived from velocity, so a mismatch would "
            f"misplace every pixel."
        )
    print(f"  confirmed {back['max_velocity']}")


def run_scan(config, args):
    """Connect, scan, tear down. Returns a process exit code."""
    from flyscan.scope import FlyScope
    from flyscan.stage import FlyStage, VelocityCalibration

    plan = config.plan
    print(report(config, args.slow_step))

    if config.autofocus is not None:
        print("\n[NOTE] This config has an 'action autofocus' block, but "
              "autofocus is not implemented yet. Focus will be left exactly "
              "where it is for the whole scan.")

    calibration = None
    if args.velocity_cal:
        calibration = VelocityCalibration.from_json(args.velocity_cal)
        if not calibration.is_calibrated:
            print(f"\n[CONFIG ERROR] {args.velocity_cal} has no velocity "
                  f"scale in it.")
            return 1
    else:
        print(
            f"\n[WARNING] No --velocity-cal given, so the stage velocity is "
            f"NOT being commanded.\n"
            f"          This scan assumes the fast axis is ALREADY set to "
            f"{plan.velocity_mm_s} mm/s.\n"
            f"          The {plan.segments} segments are derived from that "
            f"velocity, so if the\n"
            f"          stage runs at a different speed every pixel lands in "
            f"the wrong place."
        )

    if not args.yes:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer != 'y':
            print("Nothing was opened.")
            return 0

    stage = scope = writer = None
    scan_started = False
    try:
        print("\nConnecting to stage...")
        stage = FlyStage()
        stage.connect()
        if calibration is not None:
            _apply_velocity(stage, plan, calibration)

        print(f"Connecting to scope at {config.scope_ip}...")
        scope = FlyScope(config.scope_ip, transfer_mb_s=config.transfer_mb_s)
        scope.connect()

        writer = build_writer(config.output_path, plan,
                              overwrite=args.overwrite,
                              notes=f"from {config.path}")

        if config.setup_pipeline:
            print("\nSetup:")
            _run_pipeline(config.setup_pipeline, 'setup')

        print("\nScanning:")
        scan = RasterScan(stage, scope, writer, plan, config.channels)
        scan_started = True
        summary = scan.run(progress=True, spot_um=config.spot_um)

        if config.teardown_pipeline:
            print("\nTeardown:")
            _run_pipeline(config.teardown_pipeline, 'teardown')

        print(f"\n{summary['lines_written']}/{summary['lines_requested']} "
              f"lines in {_duration(summary['elapsed_s'])} "
              f"({_duration(summary['seconds_per_line'])} per line)")
        if summary['failures']:
            print(f"[WARNING] {len(summary['failures'])} line(s) failed: "
                  + ", ".join(str(f['line']) for f in summary['failures']))
        print(f"Wrote {config.output_path}")

        if summary['aborted']:
            return 3
        return 0 if summary['lines_written'] == summary['lines_requested'] else 3

    except FileExistsError as e:
        print(f"\n[ERROR] {e}")
        print("Pass --overwrite to replace it.")
        return 1
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        return 1
    finally:
        # RasterScan closes the writer and stops the stage itself, but only
        # once it has started; anything failing before that still has to be
        # unwound here.
        if writer is not None and not scan_started:
            try:
                writer.close()
            except Exception as e:
                print(f"[WARNING] closing the file failed: {e}")
        for label, handle in (("scope", scope), ("stage", stage)):
            if handle is None:
                continue
            try:
                handle.disconnect()
            except Exception as e:
                print(f"[WARNING] disconnecting the {label} failed: {e}")


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
    parser.add_argument(
        '--velocity-cal', metavar='JSON',
        help="a VelocityCalibration file from "
             "FlyStage.calibrate_velocity_scale(). Without it the stage "
             "velocity is NOT commanded and the scan assumes the fast axis "
             "is already running at the configured speed.",
    )
    parser.add_argument(
        '--overwrite', action='store_true',
        help="replace the output file if it already exists",
    )
    parser.add_argument(
        '-y', '--yes', action='store_true',
        help="skip the confirmation prompt before the stage moves",
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

    return run_scan(config, args)


if __name__ == '__main__':
    sys.exit(main())
