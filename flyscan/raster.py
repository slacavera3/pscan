"""Bidirectional line-by-line fly raster.

Ties the three layers together: flyscan.stage moves, flyscan.scope captures
one line per acquisition, flyscan.io_hdf5 writes it. Nothing here touches
instruments directly - it only sequences them, which is what makes it
testable without hardware.

Two things it exploits that a naive loop would not:

Bidirectional scanning needs no retrace. A forward line ends at
stop + runup, and the next (reverse) line launches from exactly there, so
the fast axis never travels back across the field. The move to the launch
point is still issued, because it costs one serial round trip and protects
against drift, but it is a no-op in the normal case.

The slow-axis step overlaps the transfer. Once the segments are captured
the acquisition is over, so stepping the slow axis while the line is still
downloading hides a ~50 ms move inside a ~200 ms transfer.

Choosing the parameters - velocity against spot size, sample rate against
the Brillouin frequency, memory and transfer budgets - belongs in
flyscan.budget. This module assumes that has been done and checks only what
it needs for its own correctness.
"""
import math
import time

from flyscan.budget import ROLE_AC, ROLE_DC, ROLE_TRIGGER, ROLE_SPARE
from flyscan.io_hdf5 import FlyscanWriter, StoredChannel
from flyscan.wavedesc import WavedescError

# Consecutive failed lines before giving up. A single bad line leaves a
# visible stripe in the map; a run of them means the rig needs attention.
MAX_CONSECUTIVE_FAILURES = 3

# Assumed step time for the slow axis, used only to say whether it hides
# inside the transfer. It usually does.
DEFAULT_SLOW_STEP_S = 0.05


class RasterError(RuntimeError):
    """The scan cannot continue."""


def _floor_with_slack(value, slack=1e-6):
    """Floor, but do not let float error cost a whole sample.

    A span written as 53.540 - 53.500 comes out as 39.99999999999915 um,
    and a plain int() would drop the fortieth sweep.
    """
    return max(1, int(value + slack))


class ChannelPlan:
    """Which scope channel carries what, and how hard to sparse each.

    The defaults are the four-channel plan the design settles on: signal at
    full rate, probe DC and the position-trigger train riding along at a few
    percent of the cost because both are slowly varying. Sparsing the
    trigger channel is bounded by the TTL pulse width - sparse harder than
    the pulse is wide and pulses are simply missed.
    """

    def __init__(self, ac='C1', dc='C2', trigger='C3', spare=None,
                 dc_sparsing=100, trigger_sparsing=20, spare_sparsing=1):
        self.roles = {}
        self.sparsing = {}

        for channel, role, factor in (
            (ac, ROLE_AC, 1),
            (dc, ROLE_DC, dc_sparsing),
            (trigger, ROLE_TRIGGER, trigger_sparsing),
            (spare, ROLE_SPARE, spare_sparsing),
        ):
            if channel is None:
                continue
            if channel in self.roles:
                raise ValueError(f"channel {channel} assigned twice")
            self.roles[channel] = role
            self.sparsing[channel] = max(1, int(factor))

        if ac is None:
            raise ValueError("a scan needs an AC signal channel")
        self.ac = ac

    @property
    def channels(self):
        """Channels in transfer order, signal first."""
        return list(self.roles)


class RasterPlan:
    """Geometry and timing of one bidirectional raster.

    Stage velocity is the dial. Everything about the fast axis follows from
    it: one ASOPS sweep covers v/f_sweep, that is the native sample spacing,
    and the number of those that fit in the line is the scope's segment
    count. Nothing here chooses a "pixel size" - copeaks are the native
    samples and the output grid is a binning decision made afterwards, which
    `bin_um` records so a finished file says what was intended.
    """

    def __init__(self, fast_axis, slow_axis, fast_start_mm, fast_stop_mm,
                 velocity_mm_s, slow_start_mm, slow_stop_mm, lines,
                 counts_per_mm, sample_rate_hz,
                 sweep_rate_hz=10e3, segment_seconds=50e-6, runup_mm=0.05,
                 bidirectional=True, fast_limits_mm=None, bin_um=None,
                 acceleration_mm_s2=None):
        self.fast_axis = fast_axis
        self.slow_axis = slow_axis
        self.fast_start_mm = float(fast_start_mm)
        self.fast_stop_mm = float(fast_stop_mm)
        self.slow_start_mm = float(slow_start_mm)
        self.slow_stop_mm = float(slow_stop_mm)
        self.lines = int(lines)
        self.velocity_mm_s = float(velocity_mm_s)
        self.counts_per_mm = float(counts_per_mm)
        self.sample_rate_hz = float(sample_rate_hz)
        self.sweep_rate_hz = float(sweep_rate_hz)
        self.segment_seconds = float(segment_seconds)
        self.runup_mm = float(runup_mm)
        self.bidirectional = bool(bidirectional)
        self.fast_limits_mm = fast_limits_mm
        self.bin_um = None if bin_um is None else float(bin_um)
        self.acceleration_mm_s2 = (None if acceleration_mm_s2 is None
                                   else float(acceleration_mm_s2))

        self.check()

    # --------------------------------------------------------- derived

    @property
    def fast_span_mm(self):
        return abs(self.fast_stop_mm - self.fast_start_mm)

    @property
    def native_pitch_um(self):
        """Distance one ASOPS sweep covers - the native sample spacing.

        Also the smear within a single sweep, which is why it is the one
        number the optics constrain.
        """
        return self.velocity_mm_s * 1000.0 / self.sweep_rate_hz

    # Kept under its old name because the smear interpretation is the one
    # the optical ceiling is phrased in.
    smear_um = native_pitch_um

    @property
    def segments(self):
        """Copeaks per line, which is the scope's segment count.

        Derived from velocity rather than chosen: the sweep clock decides
        how many samples fit in the line.
        """
        return _floor_with_slack(
            self.fast_span_mm * 1000.0 / self.native_pitch_um
        )

    @property
    def dwell_s(self):
        """Time per native sample - one ASOPS period, by construction."""
        return 1.0 / self.sweep_rate_hz

    @property
    def copeaks_per_bin(self):
        """How many native samples fall in one output pixel."""
        if self.bin_um is None:
            return 1.0
        return self.bin_um / self.native_pitch_um

    @property
    def relative_snr(self):
        """SNR against a single unaveraged sweep."""
        return math.sqrt(self.copeaks_per_bin)

    @property
    def binned_pixels_per_line(self):
        if self.bin_um is None:
            return self.segments
        return _floor_with_slack(self.fast_span_mm * 1000.0 / self.bin_um)

    @property
    def line_duration_s(self):
        return self.fast_span_mm / self.velocity_mm_s

    # ------------------------------------------------- time and data

    @property
    def points_per_segment(self):
        return max(1, int(round(self.segment_seconds * self.sample_rate_hz)))

    def bytes_per_line(self, channel_plan):
        """Bytes one line transfers, honouring each channel's sparsing."""
        total = 0
        for channel in channel_plan.channels:
            factor = max(1, int(channel_plan.sparsing[channel]))
            total += self.segments * math.ceil(
                self.points_per_segment / factor
            ) * 2
        return total

    def transfer_s(self, channel_plan, transfer_mb_s):
        return (self.bytes_per_line(channel_plan) / 1e6) / max(transfer_mb_s,
                                                               0.1)

    @property
    def minimum_runup_mm(self):
        """Distance needed to reach velocity, if acceleration is known."""
        if not self.acceleration_mm_s2:
            return None
        return self.velocity_mm_s ** 2 / (2.0 * self.acceleration_mm_s2)

    def timing(self, channel_plan, transfer_mb_s, slow_step_s=0.0):
        """Per-line and whole-scan time, split into where it goes."""
        flight = self.line_duration_s
        transfer = self.transfer_s(channel_plan, transfer_mb_s)
        # The slow-axis step is issued during the transfer, so it only costs
        # anything when it takes longer than the transfer does.
        other = max(0.0, slow_step_s - transfer)
        per_line = flight + transfer + other
        return {
            'flight_s': flight,
            'transfer_s': transfer,
            'other_s': other,
            'per_line_s': per_line,
            'total_s': per_line * self.lines,
            'total_flight_s': flight * self.lines,
            'total_transfer_s': transfer * self.lines,
            'total_other_s': other * self.lines,
            'bytes_per_line': self.bytes_per_line(channel_plan),
            'total_bytes': self.bytes_per_line(channel_plan) * self.lines,
        }

    @property
    def slow_pitch_mm(self):
        if self.lines < 2:
            return 0.0
        return (self.slow_stop_mm - self.slow_start_mm) / (self.lines - 1)

    @property
    def travel_extent_mm(self):
        """Full fast-axis travel including run-up and run-out."""
        low = min(self.fast_start_mm, self.fast_stop_mm) - self.runup_mm
        high = max(self.fast_start_mm, self.fast_stop_mm) + self.runup_mm
        return low, high

    def slow_position_mm(self, line_index):
        return self.slow_start_mm + line_index * self.slow_pitch_mm

    def line_geometry(self, line_index):
        """Fast-axis start, stop and direction for one line.

        Alternates on a bidirectional scan, which is what removes the
        retrace - and is also the thing that makes any fixed position
        latency show up as a comb artifact, since the sign of the offset
        flips with the direction.
        """
        reverse = self.bidirectional and (line_index % 2 == 1)
        if reverse:
            return self.fast_stop_mm, self.fast_start_mm, -1
        return self.fast_start_mm, self.fast_stop_mm, 1

    # --------------------------------------------------------- checking

    def check(self, spot_um=None):
        """Validate what this module depends on. Returns warnings.

        Parameter *choice* lives in flyscan.budget; these are the errors
        that would make the orchestration itself wrong.
        """
        if self.lines < 1:
            raise ValueError(f"lines must be at least 1, not {self.lines}")
        if self.velocity_mm_s <= 0:
            raise ValueError("velocity must be positive")
        if self.counts_per_mm <= 0:
            raise ValueError(
                "counts_per_mm must be positive and must be the value for "
                "YOUR stage - see flyscan/HARDWARE_CHECKLIST.md item 1"
            )
        if self.fast_span_mm == 0:
            raise ValueError("fast axis start and stop are the same position")
        if self.sample_rate_hz <= 0 or self.sweep_rate_hz <= 0:
            raise ValueError("sample rate and sweep rate must be positive")
        if self.runup_mm < 0:
            raise ValueError("runup cannot be negative")
        if self.fast_axis == self.slow_axis:
            raise ValueError(
                f"fast and slow axis are both '{self.fast_axis}'"
            )

        sweep_period = 1.0 / self.sweep_rate_hz
        if self.segment_seconds > sweep_period:
            raise ValueError(
                f"segment window {self.segment_seconds * 1e6:.1f} us exceeds "
                f"the {sweep_period * 1e6:.1f} us sweep period, so a segment "
                f"would run into the next sweep"
            )

        if self.fast_limits_mm is not None:
            low, high = self.travel_extent_mm
            limit_low, limit_high = self.fast_limits_mm
            if low < limit_low or high > limit_high:
                raise ValueError(
                    f"travel {low:.4f}..{high:.4f} mm (including "
                    f"{self.runup_mm} mm run-up either end) falls outside "
                    f"limits {limit_low}..{limit_high} mm"
                )

        if self.segments < 2:
            raise ValueError(
                f"velocity {self.velocity_mm_s} mm/s gives only "
                f"{self.segments} sweep(s) across a {self.fast_span_mm} mm "
                f"line. Slow down, or lengthen the line."
            )

        warnings = []

        # The one hard optical limit: past half a spot per sweep, the data
        # is smeared rather than merely coarse, and binning cannot undo it.
        if spot_um is not None and self.native_pitch_um > spot_um / 2.0:
            ceiling = spot_um / 2.0 * self.sweep_rate_hz / 1000.0
            warnings.append(
                f"one sweep covers {self.native_pitch_um:.3f} um against a "
                f"{spot_um:.2f} um spot - more than half a resolution "
                f"element, so binning cannot recover it. Cap velocity at "
                f"{ceiling:.3f} mm/s."
            )

        if self.bin_um is not None and self.bin_um < self.native_pitch_um:
            warnings.append(
                f"bin {self.bin_um:.3f} um is finer than the native sampling "
                f"of {self.native_pitch_um:.3f} um, so the output grid cannot "
                f"be filled. Slow down, or bin no finer than the sweep pitch."
            )

        needed = self.minimum_runup_mm
        if needed is not None and self.runup_mm < needed:
            warnings.append(
                f"run-up {self.runup_mm * 1000:.1f} um is shorter than the "
                f"{needed * 1000:.1f} um needed to reach "
                f"{self.velocity_mm_s} mm/s at "
                f"{self.acceleration_mm_s2} mm/s^2, so the first samples "
                f"will be taken while still accelerating"
            )
        return warnings

    def summary(self):
        low, high = self.travel_extent_mm
        lines = [
            f"fast axis {self.fast_axis}: {self.fast_start_mm:.4f} -> "
            f"{self.fast_stop_mm:.4f} mm at {self.velocity_mm_s:.4f} mm/s",
            f"  {self.segments} copeaks/line at "
            f"{self.native_pitch_um:.3f} um native spacing",
            f"slow axis {self.slow_axis}: {self.slow_start_mm:.4f} -> "
            f"{self.slow_stop_mm:.4f} mm, {self.lines} lines "
            f"@ {self.slow_pitch_mm * 1000.0:.3f} um",
            f"travel including run-up: {low:.4f} .. {high:.4f} mm",
            f"line duration {self.line_duration_s * 1e3:.1f} ms, "
            f"{'bidirectional' if self.bidirectional else 'unidirectional'}",
        ]
        if self.bin_um is not None:
            lines.insert(2, (
                f"  binned to {self.bin_um:.3f} um: "
                f"{self.binned_pixels_per_line} px/line, "
                f"{self.copeaks_per_bin:.1f} copeaks each, "
                f"relative SNR {self.relative_snr:.2f}x"
            ))
        return "\n".join(lines)


class RasterScan:
    """Run a raster, line by line, writing as it goes."""

    def __init__(self, stage, scope, writer, plan, channel_plan):
        self.stage = stage
        self.scope = scope
        self.writer = writer
        self.plan = plan
        self.channels = channel_plan

        self.lines_written = 0
        self.failures = []

    def run(self, progress=True, spot_um=None):
        """Acquire every line. Returns a summary dict.

        Stops the stage on any exit path, because a fly scan leaves it in
        continuous motion and an abort should not leave it running to a
        target with nobody watching.
        """
        plan, channels = self.plan, self.channels

        for warning in plan.check(spot_um=spot_um):
            print(f"[WARNING] {warning}")

        if progress:
            print(plan.summary())

        self.scope.configure_line(
            segments=plan.segments,
            segment_seconds=plan.segment_seconds,
            sample_rate=plan.sample_rate_hz,
            sweep_rate=plan.sweep_rate_hz,
        )

        started = time.perf_counter()
        aborted = False
        slow_commanded_to = None

        try:
            for index in range(plan.lines):
                slow_mm = plan.slow_position_mm(index)

                # The step for this line was commanded during the previous
                # line's transfer, so usually this only confirms it landed.
                if slow_commanded_to != slow_mm:
                    self.stage.move_absolute(
                        plan.slow_axis, slow_mm, plan.counts_per_mm
                    )
                self.stage.wait_for_move(
                    plan.slow_axis, slow_mm, plan.counts_per_mm
                )
                slow_commanded_to = slow_mm

                # _acquire_line issues the next line's step during this
                # line's transfer, and reports what it commanded, so the top
                # of the next pass knows it only has to wait.
                slow_commanded_to = self._acquire_line(
                    index, slow_mm, plan, channels, progress
                )

        except KeyboardInterrupt:
            aborted = True
            print("\n[WARNING] Raster aborted by user (Ctrl+C)")
        except RasterError as e:
            aborted = True
            print(f"\n[ABORTED] {e}")
        finally:
            self._halt(aborted)

        elapsed = time.perf_counter() - started
        return {
            'lines_requested': plan.lines,
            'lines_written': self.lines_written,
            'failures': list(self.failures),
            'aborted': aborted,
            'elapsed_s': elapsed,
            'seconds_per_line': (elapsed / self.lines_written
                                 if self.lines_written else float('nan')),
        }

    def _acquire_line(self, index, slow_mm, plan, channels, progress):
        """Acquire one line. Returns the slow position it left commanded."""
        fast_start, fast_stop, direction = plan.line_geometry(index)

        line_plan = self.stage.prepare_line(
            plan.fast_axis, fast_start, fast_stop, plan.runup_mm,
            plan.counts_per_mm, fly_velocity_mm_s=plan.velocity_mm_s,
        )

        next_slow = (plan.slow_position_mm(index + 1)
                     if index + 1 < plan.lines else None)

        # Flying from the wrong place produces a clean-looking line of the
        # wrong part of the sample, which is worse than no line at all.
        if not line_plan.get('at_launch', True):
            self._record_failure(
                index,
                RuntimeError(
                    f"stage never reached the launch point "
                    f"{line_plan['launch_mm']:.4f} mm, so this line would "
                    f"have been acquired somewhere else entirely"
                ),
            )
            return next_slow

        def step_slow_axis():
            if next_slow is not None:
                self.stage.move_absolute(
                    plan.slow_axis, next_slow, plan.counts_per_mm
                )

        t0 = time.perf_counter()
        try:
            line = self.stage.fly_line(
                plan.fast_axis, line_plan, self.scope,
                channels.channels, channels.sparsing,
                after_capture=step_slow_axis,
            )
        except (WavedescError, RuntimeError) as e:
            self._record_failure(index, e)
            return next_slow

        self._write_line(index, slow_mm, fast_start, direction, line, plan,
                         channels)

        if progress:
            taken = time.perf_counter() - t0
            done = index + 1
            remaining = (plan.lines - done) * taken
            print(f"  line {done}/{plan.lines} "
                  f"{'->' if direction > 0 else '<-'} "
                  f"slow {slow_mm:.4f} mm  "
                  f"{taken * 1e3:.0f} ms  "
                  f"(~{remaining:.0f}s left)")

        return next_slow

    def _write_line(self, index, slow_mm, fast_start, direction, line, plan,
                    channels):
        stored = []
        for channel in channels.channels:
            payload = line[channel]
            meta = payload['meta']
            stored.append(StoredChannel(
                name=channel,
                role=channels.roles[channel],
                data=payload['samples'],
                sparsing=channels.sparsing[channel],
                v_gain=meta['vertical_gain'],
                v_offset=meta['vertical_offset'],
                units='V',
            ))

        achieved = line['achieved'][channels.ac]
        motion = line['motion']

        self.writer.add_line(
            index=index,
            channels=stored,
            trigtime=line['trigtime'],
            # The writer stores positions in microns, not millimetres.
            slow_axis_um=slow_mm * 1000.0,
            direction=direction,
            velocity_mm_s=plan.velocity_mm_s,
            x_start_um=fast_start * 1000.0,
            sample_rate_hz=plan.sample_rate_hz,
            mode='sequence',
            counts_per_mm=plan.counts_per_mm,
            extra_attrs={
                'commanded_at_perf': motion['commanded_at'],
                'launch_mm': motion['launch_mm'],
                'target_mm': motion['target_mm'],
                'achieved_segments': achieved['segments'],
                'achieved_points_per_segment': achieved['points_per_segment'],
                'achieved_horiz_interval_s': achieved['horiz_interval'],
                'native_pitch_um': plan.native_pitch_um,
                'runup_mm': plan.runup_mm,
            },
        )
        self.lines_written += 1

    def _record_failure(self, index, error):
        self.failures.append({'line': index, 'error': str(error)})
        print(f"\n[WARNING] Line {index} failed: {error}")

        recent = [f['line'] for f in self.failures[-MAX_CONSECUTIVE_FAILURES:]]
        if (len(recent) == MAX_CONSECUTIVE_FAILURES
                and recent == list(range(index - MAX_CONSECUTIVE_FAILURES + 1,
                                         index + 1))):
            raise RasterError(
                f"{MAX_CONSECUTIVE_FAILURES} consecutive lines failed. "
                f"Stopping rather than rastering a field of gaps."
            )

    def _restore_velocity(self):
        """Put the fast axis back to travel speed.

        A fly scan leaves the axis at its scan velocity, which for a slow
        scan is 0.02 mm/s - useless for anyone who jogs the stage next. Only
        the fast axis is touched, because that is the only one this scan
        changed.
        """
        velocity = getattr(self.stage, 'travel_velocity_mm_s', None)
        if velocity and self.stage.set_velocity_mm_s(self.plan.fast_axis,
                                                     velocity):
            print(f" -> {self.plan.fast_axis} velocity restored to "
                  f"{velocity} mm/s for manual use")

    def _halt(self, aborted):
        """Stop motion, restore a usable velocity, and close the file."""
        steps = [("stage stop", lambda: self.stage.stop_all())]
        if getattr(self.stage, 'can_set_velocity', False):
            steps.append(("velocity restore", self._restore_velocity))
        steps.append(("writer close", lambda: self.writer.close()))

        for label, action in steps:
            try:
                action()
            except Exception as e:
                print(f"[WARNING] {label} failed during teardown: {e}")


def build_writer(path, plan, overwrite=False, notes=""):
    """A FlyscanWriter carrying this plan's timing and geometry.

    Keeps the root attributes of a finished file consistent with the plan
    that produced it, so a reader does not have to be told the geometry
    separately.
    """
    from flyscan.budget import AsopsTiming

    return FlyscanWriter(
        path,
        timing=AsopsTiming(sweep_rate_hz=plan.sweep_rate_hz),
        overwrite=overwrite,
        notes=notes,
        extra_attrs={
            'fast_axis': str(plan.fast_axis),
            'slow_axis': str(plan.slow_axis),
            'copeaks_per_line': plan.segments,
            'lines': plan.lines,
            'native_pitch_um': plan.native_pitch_um,
            'slow_pitch_um': plan.slow_pitch_mm * 1000.0,
            'velocity_mm_s': plan.velocity_mm_s,
            'smear_um': plan.smear_um,
            'bin_um': -1.0 if plan.bin_um is None else plan.bin_um,
            'relative_snr': plan.relative_snr,
            'segment_seconds': plan.segment_seconds,
            'bidirectional': int(plan.bidirectional),
            'runup_mm': plan.runup_mm,
        },
    )
