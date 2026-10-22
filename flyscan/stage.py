"""Constant-velocity line moves on an APT brushless controller.

Builds on pscan.stage_driver.ThorlabsStage, which already carries a correct
APT packet framer - that is the fiddly part and there is no reason to write
it twice.

Two design decisions worth knowing about:

The move is SPLIT into a preload and a trigger. pscan's move_absolute()
writes the target, sleeps 50 ms, then writes the execute command. In a fly
scan that 50 ms sits between arming the scope and the stage actually
starting, and at 10 mm/s it is 500 um of travel - an entire line's worth of
position error. Preloading the target before the scope is armed leaves one
6-byte write on the critical path, about 0.5 ms at 115200 baud, and it is
repeatable rather than at the mercy of how long a sleep takes to wake.

Velocity is CALIBRATED, not computed from a documented constant. The APT
velocity and acceleration scaling factors differ from the position scaling
factor, and differ again between controller families; a wrong one flies the
stage at the wrong speed, which is invisible in the data when position is
inferred from time. So the raw-units interface is the primitive, and a
calibration measures the mapping to mm/s on the actual hardware.
"""
import json
import struct
import time

from pscan.stage_driver import ThorlabsStage

# Velocity parameter messages.
MSG_SET_VELPARAMS = 0x0413
MSG_REQ_VELPARAMS = 0x0414
MSG_GET_VELPARAMS = 0x0415

# SET_MOVEABSPARAMS loads a target; MOVE_ABSOLUTE (no-param form) runs it.
MSG_SET_MOVEABSPARAMS = 0x0450
MSG_MOVE_ABSOLUTE = 0x0453

# How long to poll for during a profile, between position reads.
PROFILE_POLL_INTERVAL_S = 0.002

# Fraction trimmed from each end of a profile before fitting velocity, to
# exclude the acceleration and deceleration ramps.
PROFILE_TRIM_FRACTION = 0.2


class VelocityCalibration:
    """Measured mapping between raw APT velocity units and mm/s.

    `counts_per_mm` is kept here too, deliberately with no default: pscan
    hardcodes 34304.0, which is the Z8xx/TDC001 DC-servo figure and is not
    to be assumed for an MLS203-family stage on a BBD controller. A wrong
    value scales every position and every velocity silently.
    """

    def __init__(self, counts_per_mm, velocity_scale=None,
                 acceleration_scale=None, residual_rms=None, notes=""):
        self.counts_per_mm = float(counts_per_mm)
        self.velocity_scale = velocity_scale
        self.acceleration_scale = acceleration_scale
        self.residual_rms = residual_rms
        self.notes = notes

    @property
    def is_calibrated(self):
        return self.velocity_scale is not None

    def raw_velocity(self, mm_per_s):
        if not self.is_calibrated:
            raise RuntimeError(
                "No velocity calibration. Run calibrate_velocity_scale() "
                "first, or pass raw units directly."
            )
        return int(round(mm_per_s * self.velocity_scale))

    def mm_per_s(self, raw):
        if not self.is_calibrated:
            raise RuntimeError("No velocity calibration.")
        return raw / self.velocity_scale

    def to_json(self, path):
        with open(path, 'w') as f:
            json.dump({
                'counts_per_mm': self.counts_per_mm,
                'velocity_scale': self.velocity_scale,
                'acceleration_scale': self.acceleration_scale,
                'residual_rms': self.residual_rms,
                'notes': self.notes,
            }, f, indent=2)

    @classmethod
    def from_json(cls, path):
        with open(path) as f:
            return cls(**json.load(f))


class FlyStage(ThorlabsStage):
    """A ThorlabsStage that can also fly a line at constant velocity."""

    # ------------------------------------------------- velocity parameters

    def set_velocity_params_raw(self, axis, max_velocity, acceleration,
                                min_velocity=0):
        """Set velocity parameters in the controller's own units.

        The raw interface is the primitive because the conversion to mm/s is
        not trustworthy until measured. See VelocityCalibration.
        """
        dest = self._get_dest_channel(axis)
        payload = struct.pack(
            '<Hlll', 0x01, int(min_velocity), int(acceleration),
            int(max_velocity)
        )
        header = struct.pack(
            '<HHBB', MSG_SET_VELPARAMS, len(payload),
            dest | 0x80, self.SOURCE_PC
        )
        self.ser.write(header + payload)

    def get_velocity_params_raw(self, axis, timeout_s=1.0):
        """Read velocity parameters back, or None if no reply arrives."""
        dest = self._get_dest_channel(axis)
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack(
            '<HBBBB', MSG_REQ_VELPARAMS, 0x01, 0x00, dest, self.SOURCE_PC
        ))

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            packet = self._read_packet(timeout_s=remaining)
            if packet is None:
                return None

            msg_id, body = packet
            if msg_id == MSG_GET_VELPARAMS and len(body) >= 14:
                _, min_vel, accel, max_vel = struct.unpack_from('<Hlll', body, 0)
                return {
                    'min_velocity': min_vel,
                    'acceleration': accel,
                    'max_velocity': max_vel,
                }

    # ------------------------------------------------------ split the move

    def preload_target(self, axis, position_mm, counts_per_mm):
        """Load a move target without executing it.

        Kept off the critical path so that trigger_move() is a single short
        write. This is the half of pscan's move_absolute() that carries the
        50 ms sleep with it.
        """
        dest = self._get_dest_channel(axis)
        counts = int(round(position_mm * counts_per_mm))

        payload = struct.pack('<Hl', 0x01, counts)
        header = struct.pack(
            '<HHBB', MSG_SET_MOVEABSPARAMS, len(payload),
            dest | 0x80, self.SOURCE_PC
        )
        self.ser.write(header + payload)
        # Give the controller time to digest it. This is the sleep that used
        # to sit between arming and motion; here it is before arming.
        time.sleep(0.05)
        return {'target_mm': position_mm, 'target_counts': counts}

    def trigger_move(self, axis):
        """Execute a preloaded move. One 6-byte write, and a timestamp.

        The returned timestamp is the host's best estimate of when motion
        was commanded. It is not when motion began - there is still
        controller latency and the acceleration ramp - but it is the
        reference the scope's trigger times are compared against, so it is
        worth recording per line.
        """
        dest = self._get_dest_channel(axis)
        packet = struct.pack(
            '<HBBBB', MSG_MOVE_ABSOLUTE, 0x01, 0x00, dest, self.SOURCE_PC
        )

        commanded_at = time.perf_counter()
        self.ser.write(packet)
        return commanded_at

    # ----------------------------------------------------------- flying

    def prepare_line(self, axis, start_mm, stop_mm, runup_mm, counts_per_mm,
                     tolerance_mm=0.001, timeout_s=15.0, dwell_s=0.02):
        """Move to the launch point and preload the end of the fly move.

        Returns the launch and target positions. The run-up deliberately
        takes the stage outside the nominal scan range so that it is already
        at constant velocity when the first pixel is acquired, so any soft
        limit has to cover the extended travel, not just start..stop.
        """
        direction = 1.0 if stop_mm >= start_mm else -1.0
        launch_mm = start_mm - direction * runup_mm
        target_mm = stop_mm + direction * runup_mm

        self.move_absolute(axis, launch_mm, counts_per_mm)
        reached = self.wait_for_move(
            axis, launch_mm, counts_per_mm,
            tolerance_mm=tolerance_mm, timeout_s=timeout_s, dwell_s=dwell_s,
        )
        self.preload_target(axis, target_mm, counts_per_mm)

        return {
            'launch_mm': launch_mm,
            'target_mm': target_mm,
            'direction': direction,
            'at_launch': reached,
        }

    def fly_line(self, axis, plan, scope, channels, sparsing=None,
                 after_capture=None):
        """Run one line: arm the scope, release the stage, collect.

        `plan` is what prepare_line() returned. The ordering is the whole
        point - the scope is armed first because the ASOPS sweep clock runs
        continuously and segments start filling immediately, then the stage
        is released with one short write, so the gap between the two is as
        small and as repeatable as it can be made.

        `after_capture` runs once the segments are captured but before the
        transfer starts. That window is worth using: the acquisition is
        already complete, so the slow axis can be stepping towards the next
        line while this one is still coming down the wire, and transfer is
        the slowest part of a fly scan.

        Returns the scope's line plus the commanded-at timestamp, which is
        what lets the segment trigger times be referred back to the motion.
        """
        scope.arm_line()
        commanded_at = self.trigger_move(axis)

        scope.wait_for_line()
        if after_capture is not None:
            after_capture()
        line = scope.download_line(channels, sparsing)

        line['motion'] = {
            'commanded_at': commanded_at,
            'launch_mm': plan['launch_mm'],
            'target_mm': plan['target_mm'],
            'direction': plan['direction'],
        }
        return line

    # ------------------------------------------------------- measurement

    def profile_move(self, axis, target_mm, counts_per_mm, timeout_s=30.0,
                     tolerance_mm=0.002):
        """Poll position throughout a move. Returns (times, positions_mm).

        Serial round trips cap this at a few hundred hertz, which is ample
        to see the acceleration ramp and to fit the constant-velocity
        section, and it needs no extra hardware.
        """
        self.move_absolute(axis, target_mm, counts_per_mm)

        times, positions = [], []
        t0 = time.perf_counter()
        deadline = t0 + timeout_s

        while time.perf_counter() < deadline:
            position = self.get_position(axis, counts_per_mm)
            if position is not None:
                times.append(time.perf_counter() - t0)
                positions.append(position)
                if abs(position - target_mm) <= tolerance_mm:
                    break
            time.sleep(PROFILE_POLL_INTERVAL_S)

        return times, positions

    @staticmethod
    def velocity_from_profile(times, positions,
                              trim_fraction=PROFILE_TRIM_FRACTION):
        """Fit velocity to the middle of a profile, excluding the ramps.

        Also returns the RMS residual about that straight line, which is a
        direct measure of velocity ripple over the flown section - the
        number that decides whether position has to be measured rather than
        inferred from time.
        """
        if len(times) < 8:
            return None

        trim = int(len(times) * trim_fraction)
        t = times[trim:len(times) - trim] or times
        x = positions[trim:len(positions) - trim] or positions
        n = len(t)
        if n < 4:
            return None

        mean_t = sum(t) / n
        mean_x = sum(x) / n
        covariance = sum((ti - mean_t) * (xi - mean_x) for ti, xi in zip(t, x))
        variance = sum((ti - mean_t) ** 2 for ti in t)
        if variance == 0:
            return None

        slope = covariance / variance
        intercept = mean_x - slope * mean_t

        residuals = [xi - (slope * ti + intercept) for ti, xi in zip(t, x)]
        rms = (sum(r * r for r in residuals) / n) ** 0.5

        return {
            'velocity_mm_s': slope,
            'residual_rms_mm': rms,
            'n_points': n,
            'span_s': t[-1] - t[0],
        }

    def calibrate_velocity_scale(self, axis, counts_per_mm, raw_values,
                                 start_mm, distance_mm, settle_s=0.3,
                                 verbose=True):
        """Measure actual velocity for a set of raw parameter values.

        Returns a VelocityCalibration plus the per-point measurements, so a
        poor fit is visible rather than quietly believed. This exists
        because the APT velocity scaling factor for a given controller and
        stage pairing is not safe to take on trust.
        """
        direction = 1.0 if distance_mm >= 0 else -1.0
        measurements = []

        for raw in raw_values:
            self.move_absolute(axis, start_mm, counts_per_mm)
            self.wait_for_move(axis, start_mm, counts_per_mm)
            time.sleep(settle_s)

            self.set_velocity_params_raw(axis, max_velocity=raw,
                                         acceleration=raw)
            time.sleep(0.05)

            times, positions = self.profile_move(
                axis, start_mm + distance_mm, counts_per_mm
            )
            fit = self.velocity_from_profile(times, positions)
            if fit is None:
                if verbose:
                    print(f"  raw {raw}: profile too short to fit, skipped")
                continue

            measured = abs(fit['velocity_mm_s'])
            measurements.append({'raw': raw, 'measured_mm_s': measured,
                                 'residual_rms_mm': fit['residual_rms_mm']})
            if verbose:
                print(f"  raw {raw}: {measured:.4f} mm/s "
                      f"(ripple {fit['residual_rms_mm'] * 1e3:.2f} um rms)")

        if len(measurements) < 2:
            raise RuntimeError(
                "Need at least two usable points to fit a velocity scale; "
                "got {}. Try a longer distance_mm so the constant-velocity "
                "section is long enough to fit.".format(len(measurements))
            )

        # Fit raw = scale * measured, through the origin: zero commanded
        # velocity is zero actual velocity, so an intercept would be a sign
        # something is wrong rather than something to absorb.
        numerator = sum(m['raw'] * m['measured_mm_s'] for m in measurements)
        denominator = sum(m['measured_mm_s'] ** 2 for m in measurements)
        scale = numerator / denominator

        residuals = [m['raw'] - scale * m['measured_mm_s'] for m in measurements]
        rms = (sum(r * r for r in residuals) / len(residuals)) ** 0.5

        calibration = VelocityCalibration(
            counts_per_mm=counts_per_mm,
            velocity_scale=scale,
            residual_rms=rms,
            notes=(f"Fitted from {len(measurements)} points on axis {axis}, "
                   f"direction {'+' if direction > 0 else '-'}, "
                   f"{abs(distance_mm)} mm travel."),
        )
        return calibration, measurements
