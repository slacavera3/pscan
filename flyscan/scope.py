"""One raster line per oscilloscope acquisition, in Sequence mode.

Each segment is one ASOPS delay sweep, triggered by the 10 kHz sweep clock
the rig already provides. So segments arrive pre-aligned in delay - there is
no peak finding needed to cut the record up - and a whole line comes back in
a single transfer rather than one round trip per pixel. Getting Python out of
the per-pixel loop is the entire point of flying.

Deliberately NOT built on pscan.lecroy_driver: that driver is organised
around maths-function sweep counts and per-coordinate appends to a .dat, and
neither applies here.

Two things this module always does rather than assumes, because both fail
silently and both corrupt a whole scan:
  - it reads the segment count AND the per-segment length back after asking
    for them, since a scope that caps segments on available memory would
    otherwise hand back fewer segments than there are pixels, misaligning
    every line, and one that sizes segments itself would quietly acquire a
    multiple of the intended data;
  - it reads the achieved sample interval and sparsing out of the returned
    waveform, rather than trusting that a requested timebase was honoured.
"""
import math
import re
import time

import vxi11

from flyscan.wavedesc import parse_waveform

# A LeCroy timebase is ten divisions wide.
DIVISIONS = 10

# Floor for the VXI-11 timeout, so a short line does not get a tight one.
TIMEOUT_FLOOR_S = 10.0

# Multiplier on the estimated acquisition-plus-transfer time.
TIMEOUT_MARGIN = 3.0

# Only used to size the timeout. Measure yours and pass it in; VXI-11 on
# this hardware is typically a few MB/s, which is why transfer rather than
# flight dominates a fly scan.
DEFAULT_TRANSFER_MB_S = 4.0

# Fractional tolerance on the sample interval the scope actually delivers.
INTERVAL_TOLERANCE = 0.05


class ScopeConfigError(RuntimeError):
    """The scope did not accept a setting that the scan depends on."""


class FlyScope:
    def __init__(self, ip_address, transfer_mb_s=DEFAULT_TRANSFER_MB_S):
        self.ip = ip_address
        self.instr = None
        self.transfer_mb_s = transfer_mb_s

        # Filled in by configure_line().
        self.segments = None
        self.points_per_segment = None
        self.sample_rate = None
        self.sweep_rate = None
        self._expected_interval = None

    # ------------------------------------------------------------ session

    def connect(self):
        if self.instr is not None:
            return

        self.instr = vxi11.Instrument(self.ip)
        self.instr.timeout = TIMEOUT_FLOOR_S
        # Headers off throughout: every query below is parsed with a regex
        # that tolerates a prefix, so there is no need to toggle CHDR and
        # therefore no chance of leaving it toggled.
        self.instr.write("CHDR OFF")
        self.instr.write("CFMT DEF9,WORD,BIN")

    def disconnect(self):
        if self.instr is None:
            return
        try:
            # Leave the scope free-running rather than armed and waiting.
            self.instr.write("SEQ OFF")
            self.instr.write("TRMD AUTO")
        finally:
            self.instr.close()
            self.instr = None

    def _ask(self, query):
        self.instr.write(query)
        return self.instr.read().strip()

    # ------------------------------------------------------------- config

    def configure_line(self, segments, segment_seconds, sample_rate,
                       sweep_rate):
        """Set up one line: `segments` sweeps of `segment_seconds` each.

        segment_seconds is how much of each 12.5 ns delay window to keep,
        expressed in lab time - the ASOPS stretch means a few ns of real
        delay is tens of microseconds here. Capturing less than the full
        100 us sweep is where most of the data saving comes from.

        sweep_rate is the ASOPS sweep clock in Hz, used only to size the
        timeout, since that is what sets how long a line takes to fill.
        """
        if self.instr is None:
            raise RuntimeError("Scope not connected. Call connect() first.")
        if segments < 1:
            raise ValueError(f"segments must be at least 1, not {segments}")
        if segment_seconds <= 0 or sample_rate <= 0 or sweep_rate <= 0:
            raise ValueError("segment_seconds, sample_rate and sweep_rate "
                             "must all be positive")

        self.segments = segments
        self.sample_rate = sample_rate
        self.sweep_rate = sweep_rate
        self.points_per_segment = max(1, int(round(segment_seconds * sample_rate)))
        self._expected_interval = 1.0 / sample_rate

        # Both numbers in one command. MSIZ alone is not enough: what it
        # means varies by model - the acquisition total on some, the
        # per-segment length on others - and on a WaveRunner Xi-A it is
        # silently ignored once Sequence mode is on. There, 'SEQ ON,10000'
        # made the scope divide its 25 Mpts across the segments by itself,
        # giving 2500 points each and quietly acquiring five times the
        # intended data against a transfer budget sized for 500. The
        # three-argument form states the segment length outright, so no
        # model has to infer it.
        self.instr.write(f"SEQ ON,{segments},{self.points_per_segment}")
        self.instr.write(f"TDIV {segment_seconds / DIVISIONS:.6E}")

        # Read back after TDIV, not before: the timebase can itself move the
        # memory the scope is willing to give a segment.
        granted, granted_points = self._read_sequence()
        if granted is None:
            raise ScopeConfigError(
                "Scope did not report Sequence mode as on. A fly line needs "
                "it; check the acquisition mode and available memory."
            )
        if granted != segments:
            raise ScopeConfigError(
                f"Asked for {segments} segments and got {granted}. Every "
                f"pixel in the line maps to a segment, so a short count "
                f"would misalign the whole line. Reduce pixels per line, "
                f"shorten the capture window, or fit more memory."
            )

        # Older firmware that does not take a length with SEQ leaves the
        # third field alone, so fall back to MSIZ and look again. One of the
        # two reaches every model; which one did is not worth caring about,
        # as long as the result is checked.
        if granted_points is not None and granted_points != self.points_per_segment:
            self.instr.write(f"MSIZ {self.points_per_segment}")
            granted, granted_points = self._read_sequence()

        if granted_points is not None and granted_points != self.points_per_segment:
            raise ScopeConfigError(
                f"Asked for {self.points_per_segment} points per segment and "
                f"got {granted_points}. At {segments} segments that is "
                f"{segments * granted_points / 1e6:.1f} Mpts per channel "
                f"instead of {segments * self.points_per_segment / 1e6:.1f}, "
                f"so the transfer would overrun its timeout and the delay "
                f"axis would not be what was asked for. Shorten "
                f"segment_window, lower sample_rate, or use fewer segments."
            )

        self.instr.timeout = self._timeout_for_line()
        return {
            'segments': segments,
            'points_per_segment': self.points_per_segment,
            'requested_interval': self._expected_interval,
            'timeout_s': self.instr.timeout,
        }

    def configure_flat_line(self, line_seconds, sample_rate, sweep_rate):
        """Capture a whole line as ONE real-time record, Sequence mode off.

        The diagnostic counterpart to configure_line(): it keeps every
        microsecond including the parts of the delay window a segmented
        capture throws away, which is how you find out where the signal
        actually sits, what the real peak-to-peak spacing is, and whether a
        segment window is clipping anything. Costs roughly twice the data of
        a half-window segmented line, so it is for characterisation rather
        than production scanning.
        """
        if self.instr is None:
            raise RuntimeError("Scope not connected. Call connect() first.")
        if line_seconds <= 0 or sample_rate <= 0 or sweep_rate <= 0:
            raise ValueError("line_seconds, sample_rate and sweep_rate must "
                             "all be positive")

        self.segments = 1
        self.sample_rate = sample_rate
        self.sweep_rate = sweep_rate
        self.points_per_segment = max(1, int(round(line_seconds * sample_rate)))
        self._expected_interval = 1.0 / sample_rate

        self.instr.write("SEQ OFF")
        self.instr.write(f"TDIV {line_seconds / DIVISIONS:.6E}")
        self.instr.write(f"MSIZ {self.points_per_segment}")

        if self._read_segment_count() is not None:
            raise ScopeConfigError(
                "Scope still reports Sequence mode as on after SEQ OFF; a "
                "flat capture would come back segmented."
            )

        self.instr.timeout = self._timeout_for_line()
        return {
            'segments': 1,
            'points_per_segment': self.points_per_segment,
            'requested_interval': self._expected_interval,
            'timeout_s': self.instr.timeout,
        }

    def _read_sequence(self):
        """(segments, points_per_segment) as reported, or (None, None) if off.

        The third field is the per-segment length and comes back in whatever
        notation the scope likes - '500', '2.5E+3', '25E+3' have all been
        seen - so it is parsed as a float and rounded. It is optional: a
        scope that omits it gives None, which the caller treats as 'this
        model does not tell us' rather than as a mismatch.
        """
        reply = self._ask("SEQ?").upper()
        if 'ON' not in reply:
            return None, None

        match = re.search(
            r'ON\s*,\s*(\d+)\s*(?:,\s*([0-9.]+(?:E[+-]?\d+)?))?', reply
        )
        if not match:
            return None, None

        segments = int(match.group(1))
        points = int(round(float(match.group(2)))) if match.group(2) else None
        return segments, points

    def _read_segment_count(self):
        """Segment count the scope reports, or None if Sequence mode is off."""
        return self._read_sequence()[0]

    def _timeout_for_line(self, channels=None, sparsing=None):
        """Cover filling the line and transferring it, with margin.

        The fixed 30s that pscan uses would time out mid-acquisition on a
        long line, and be needlessly tight on a short one. Sized for one
        channel at configure time and re-sized in download_line() once the
        real channel list is known, since three channels move three times
        the bytes.
        """
        fill_s = self.segments / self.sweep_rate

        if channels is None:
            payload_points = self.segments * self.points_per_segment
        else:
            payload_points = self.line_budget(channels, sparsing)['total_points']

        transfer_s = (payload_points * 2 / 1e6) / max(self.transfer_mb_s, 0.1)
        return max(TIMEOUT_FLOOR_S, TIMEOUT_MARGIN * (fill_s + transfer_s))

    # -------------------------------------------------------- acquisition

    def arm_line(self):
        """Arm for one line and return immediately.

        Separate from wait_for_line() because the stage has to be set moving
        in between - pscan's combined 'ARM; WAIT; *OPC?' would block before
        the move ever started.
        """
        if self.segments is None:
            raise RuntimeError("Call configure_line() before arming.")

        self.instr.write("CLSW")
        self.instr.write("TRMD SINGLE")
        self.instr.write("ARM")

    def wait_for_line(self):
        """Block until all segments have been triggered and captured."""
        self.instr.write("WAIT; *OPC?")
        self.instr.read()

    def download_line(self, channels, sparsing=None):
        """Transfer one line and parse it.

        `sparsing` maps a channel to its decimation factor on transfer.
        WFSU is a global setting applied to whatever is queried next, so a
        per-channel factor is just a matter of setting it before each query
        - which is how a slowly-varying probe-DC or position channel can
        ride along at a few percent of the signal channel's cost.

        Returns {channel: {'samples', 'meta'}} plus 'trigtime' and
        'achieved', where 'achieved' records what the scope actually did.
        """
        sparsing = sparsing or {}
        result = {}
        trigtime = None

        # Re-size now that the channel list is known: the configure-time
        # estimate assumed a single channel.
        self.instr.timeout = self._timeout_for_line(channels, sparsing)

        for channel in channels:
            factor = max(1, int(sparsing.get(channel, 1)))
            # NP,0 = all points, FP,0 = from the first, SN,0 = all segments.
            self.instr.write(f"WFSU SP,{factor},NP,0,FP,0,SN,0")

            self.instr.write(f"{channel}:WF?")
            parsed = parse_waveform(self.instr.read_raw())

            meta = parsed['meta']
            meta['requested_sparsing'] = factor
            result[channel] = {'samples': parsed['samples'], 'meta': meta}

            # Every channel of one acquisition shares the trigger times, so
            # the first one that carries them is enough.
            if trigtime is None and parsed['trigtime'] is not None:
                trigtime = parsed['trigtime']

        result['trigtime'] = trigtime
        result['achieved'] = self._check_achieved(result, channels, sparsing)
        return result

    def _check_achieved(self, result, channels, sparsing):
        """Compare what came back against what was asked for."""
        achieved = {}

        for channel in channels:
            meta = result[channel]['meta']
            factor = max(1, int(sparsing.get(channel, 1)))
            expected_interval = self._expected_interval * factor

            interval = meta['horiz_interval']
            # horiz_interval already accounts for sparsing on some firmware
            # and not others, so accept either reading and record which.
            matches_sparsed = self._close(interval, expected_interval)
            matches_raw = self._close(interval, self._expected_interval)

            if not (matches_sparsed or matches_raw):
                print(
                    f"[WARNING] {channel}: sample interval {interval:.3E}s is "
                    f"neither the requested {self._expected_interval:.3E}s nor "
                    f"that sparsed by {factor}. The scope chose its own "
                    f"timebase; treat the delay axis as {interval:.3E}s."
                )

            if meta['segments'] != self.segments:
                print(
                    f"[WARNING] {channel}: {meta['segments']} segments "
                    f"returned, {self.segments} configured. Position mapping "
                    f"will be wrong unless this is reconciled."
                )

            achieved[channel] = {
                'segments': meta['segments'],
                'points_per_segment': meta['points_per_segment'],
                'horiz_interval': interval,
                'sparsing_factor': meta['sparsing_factor'],
                'requested_sparsing': factor,
                'sweeps_per_acq': meta['sweeps_per_acq'],
                'nominal_bits': meta['nominal_bits'],
            }

        return achieved

    @staticmethod
    def _close(value, target):
        if target == 0:
            return value == 0
        return abs(value - target) / abs(target) <= INTERVAL_TOLERANCE

    def acquire_line(self, channels, sparsing=None, on_armed=None):
        """Arm, run `on_armed`, wait, download.

        `on_armed` is where the stage move goes: it is called once the scope
        is armed and before the wait, which is the only window in which
        starting the move does not either miss triggers or deadlock.
        Omitting it is useful on the bench, where the sweep clock alone will
        fill the segments.
        """
        self.arm_line()
        if on_armed is not None:
            on_armed()
        self.wait_for_line()
        return self.download_line(channels, sparsing)

    # ------------------------------------------------------------ helpers

    def line_budget(self, channels, sparsing=None):
        """Points and bytes this configuration will move, before running it.

        Cheap enough to print before every line, and it is the number that
        decides whether a scan is transfer-bound.
        """
        sparsing = sparsing or {}
        total_points = 0
        per_channel = {}

        for channel in channels:
            factor = max(1, int(sparsing.get(channel, 1)))
            points = self.segments * math.ceil(self.points_per_segment / factor)
            per_channel[channel] = points
            total_points += points

        megabytes = total_points * 2 / 1e6
        return {
            'per_channel_points': per_channel,
            'total_points': total_points,
            'megabytes': megabytes,
            'fill_s': self.segments / self.sweep_rate,
            'estimated_transfer_s': megabytes / max(self.transfer_mb_s, 0.1),
        }
