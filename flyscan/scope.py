"""One raster line per oscilloscope acquisition, segmented.

Each segment is one ASOPS delay sweep, triggered by the 10 kHz delay clock
the rig already provides. So segments arrive pre-aligned in delay - there is
no peak finding needed to cut the record up - and a whole line comes back in
a single transfer rather than one round trip per pixel. Getting Python out of
the per-pixel loop is the entire point of flying.

TWO INSTRUMENTS, ONE CONTRACT. FlyScope drives a LeCroy over VXI-11 in
Sequence mode; FlyPicoScope drives a USB PicoScope in rapid block mode. The
contract flyscan.stage and flyscan.raster use is the same either way:

    configure_line() -> arm_line() -> wait_for_line() -> download_line()

and download_line() returns the same {channel: {'samples', 'meta'}} plus
'trigtime' and 'achieved', so nothing above this module knows which scope it
is talking to.

Deliberately NOT built on pscan.lecroy_driver: that driver is organised
around maths-function sweep counts and per-coordinate appends to a .dat, and
neither applies here. FlyPicoScope, by contrast, IS built on
pscan.picoscope_driver - but only on its ctypes layer, which is a hardware
binding with no acquisition policy in it. One binding per piece of hardware,
two acquisition policies on top, which is the same split pscan.daq makes.

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

import numpy as np

from flyscan.wavedesc import parse_waveform

# vxi11 is imported inside FlyScope.connect(), not here. At module scope it
# made this file - and so the whole fly-line contract, FlyPicoScope included -
# unimportable on a machine with no VXI-11 stack installed. That is the same
# mistake pscan.main calls out for cameras and serial ports: the code that
# can be tested anywhere should not be held hostage by one instrument's
# transport. A USB rig needs no VXI-11 at all.

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


# USB moves a line a great deal faster than VXI-11 does, so the LeCroy's
# 4 MB/s default would make every PicoScope timeout and time estimate wrong
# in the pessimistic direction. Still only used for sizing and reporting -
# measure yours and put 'transfer_rate' in the config.
DEFAULT_USB_TRANSFER_MB_S = 30.0


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
        self.delay_rate = None
        self._expected_interval = None

    # ------------------------------------------------------------ session

    def connect(self):
        if self.instr is not None:
            return

        import vxi11

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
                       delay_rate):
        """Set up one line: `segments` sweeps of `segment_seconds` each.

        segment_seconds is how much of each delay window to keep, expressed
        in lab time - the ASOPS stretch means a few ns of real delay is tens
        of microseconds here. The window is one laser period, so 12.5 ns on
        an 80 MHz bench and 10 ns on a 100 MHz one; the scope is told lab
        seconds either way and does not need to know which. Capturing less
        than the full sweep is where most of the data saving comes from.

        delay_rate is the ASOPS delay clock in Hz, used only to size the
        timeout, since that is what sets how long a line takes to fill.
        """
        if self.instr is None:
            raise RuntimeError("Scope not connected. Call connect() first.")
        if segments < 1:
            raise ValueError(f"segments must be at least 1, not {segments}")
        if segment_seconds <= 0 or sample_rate <= 0 or delay_rate <= 0:
            raise ValueError("segment_seconds, sample_rate and delay_rate "
                             "must all be positive")

        self.segments = segments
        self.sample_rate = sample_rate
        self.delay_rate = delay_rate
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

    def configure_flat_line(self, line_seconds, sample_rate, delay_rate):
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
        if line_seconds <= 0 or sample_rate <= 0 or delay_rate <= 0:
            raise ValueError("line_seconds, sample_rate and delay_rate must "
                             "all be positive")

        self.segments = 1
        self.sample_rate = sample_rate
        self.delay_rate = delay_rate
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
        fill_s = self.segments / self.delay_rate

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
        Omitting it is useful on the bench, where the delay clock alone will
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
            'fill_s': self.segments / self.delay_rate,
            'estimated_transfer_s': megabytes / max(self.transfer_mb_s, 0.1),
        }

class FlyPicoScope:
    """One raster line per PicoScope rapid-block acquisition, over USB.

    Rapid block mode is the same idea as LeCroy Sequence mode: arm once, let
    N triggers each fill one segment, read the lot back in one transfer. So
    the mapping onto a fly line is direct - one segment per copeak, the ASOPS
    delay clock on the trigger input.

    THREE THINGS DIFFER FROM THE LECROY, and they are the whole of what this
    class has to say for itself:

    1. NOTHING IS INHERITED. A LeCroy fly line runs on whatever the front
       panel was left set to, which is why flyscan/scope_probe.py exists -
       trigger source, slope, level and coupling decide the entire
       position-to-sample mapping and flyscan never set them. A PicoScope has
       no front panel: every one of those settings comes from the config
       block and is applied here, so there is nothing to inherit and nothing
       to probe. That removes checklist item 12b rather than answering it.

    2. SEGMENT TIMES ARE DERIVED, NOT MEASURED. A LeCroy's TRIGTIME array
       timestamps every segment against the first trigger, which is what
       places each copeak along the line. Block mode has no equivalent: it
       reports each segment's sub-sample trigger OFFSET and not the time
       between triggers. But the triggers come from the ASOPS delay clock at
       a known fixed rate, so segment i fires at i / delay_rate by
       construction - exact up to clock jitter, and the one thing that could
       break it is a missed trigger, which is caught by reading the capture
       count back. So trigtime column 1 is derived from the clock, column 2
       is the measured offset where the unit reports it, and the line records
       `trigtime_source` so nothing downstream mistakes the first column for
       a measurement.

    3. SPARSING IS DECIMATION IN THE SCOPE. WFSU's SP factor becomes
       RATIO_MODE_DECIMATE, which has the same effect on the transfer - the
       discarded samples never cross the bus. It is per retrieval rather than
       per channel, so channels are read back one at a time and each one's
       buffers are cleared afterwards; a channel left registered would be
       transferred again at the next one's ratio.
    """

    def __init__(self, channels, series=None, serial=None, v_max=None,
                 coupling=None, trigger=None, resolution_bits=None,
                 timeout_s=None, transfer_mb_s=DEFAULT_USB_TRANSFER_MB_S,
                 backend=None, backend_factory=None):
        from pscan import picoscope_driver as pico

        self._pico = pico
        if not channels:
            raise ValueError("a fly line needs at least one scope channel")

        self.channels = [str(c).strip().upper() for c in channels]
        self.series = series
        self.serial = serial
        self.transfer_mb_s = float(transfer_mb_s)
        self.resolution_bits = int(
            pico.DEFAULT_RESOLUTION_BITS if resolution_bits is None
            else resolution_bits
        )

        # Per channel, by NAME: flyscan addresses channels by name
        # everywhere - roles, sparsing, the stored datasets - so ranges and
        # coupling are keyed the same way rather than positionally.
        self.v_max = dict(v_max or {})
        self.coupling = dict(coupling or {})

        # Required, not defaulted: without a trigger every segment would
        # start when the software asked rather than on a delay sweep, and the
        # delay axis of the whole file would be meaningless.
        if not trigger:
            raise ValueError(
                "a fly line on a PicoScope needs a trigger - the ASOPS delay "
                "clock. Without one, segments are not delay sweeps and the "
                "delay axis is meaningless."
            )
        self.trigger = trigger

        # The explicit per-line timeout, if the config gave one. Otherwise
        # sized from the fill and transfer time, as the LeCroy path does.
        self.timeout_s = None if timeout_s is None else float(timeout_s)

        self.backend = backend
        self._backend_factory = backend_factory or pico.PicoBackend

        # Filled in by configure_line(), like FlyScope.
        self.segments = None
        self.points_per_segment = None
        self.sample_rate = None
        self.delay_rate = None
        self._expected_interval = None

        self._timebase = None
        self._interval = None
        self._max_adc = None
        self._range_index = {}
        self._indices = {}
        self._buffers = {}
        self._line_timeout_s = TIMEOUT_FLOOR_S
        self._armed_at = None
        self._offsets_unavailable = False
        self._last_overflow = None

    # ------------------------------------------------------------ session

    def connect(self):
        pico = self._pico

        if self.backend is None:
            self.backend, self.serial = pico.discover(
                self.series, self.serial,
                backend_factory=self._backend_factory,
            )
        elif self.backend.library_path is None:
            self.backend.load()
        self.series = self.backend.series

        self.backend.open(serial=self.serial,
                          resolution_bits=self.resolution_bits)

        model = self.backend.unit_info(pico.PICO_VARIANT_INFO)
        batch = self.backend.unit_info(pico.PICO_BATCH_AND_SERIAL)
        self.serial = self.serial or batch
        print(f"  PicoScope {model} (serial {batch}), {self.series} driver, "
              f"via {self.backend.library_path}")

    def disconnect(self):
        if self.backend is None or not self.backend.is_open:
            return
        try:
            # Leave the unit idle rather than armed and waiting on a trigger.
            self.backend.stop()
        finally:
            self.backend.close()

    # ------------------------------------------------------------- config

    def configure_line(self, segments, segment_seconds, sample_rate,
                       delay_rate):
        """Set up one line: `segments` sweeps of `segment_seconds` each.

        Same arguments and same return as FlyScope.configure_line(), and the
        same discipline: every number is read back off the unit rather than
        assumed, because a short segment count misaligns a whole line and a
        timebase the unit substituted silently rescales the delay axis.
        """
        pico = self._pico

        if self.backend is None or not self.backend.is_open:
            raise RuntimeError("Scope not connected. Call connect() first.")
        if segments < 1:
            raise ValueError(f"segments must be at least 1, not {segments}")
        if segment_seconds <= 0 or sample_rate <= 0 or delay_rate <= 0:
            raise ValueError("segment_seconds, sample_rate and delay_rate "
                             "must all be positive")

        self.segments = int(segments)
        self.sample_rate = float(sample_rate)
        self.delay_rate = float(delay_rate)
        self.points_per_segment = max(1, int(round(segment_seconds * sample_rate)))
        self._expected_interval = 1.0 / sample_rate

        self._apply_channels()
        self._apply_trigger()

        # Memory is split before the timebase is chosen, so the ladder is
        # asked about with the segmentation that will actually be in force.
        per_segment = self.backend.memory_segments(self.segments)
        if per_segment < self.points_per_segment:
            raise ScopeConfigError(
                f"This unit holds {per_segment} sample(s) per segment with "
                f"its memory split {self.segments} ways, but a line needs "
                f"{self.points_per_segment}. Every segment is one copeak, so "
                f"a shorter one cannot simply be truncated. Shorten "
                f"segment_window, lower sample_rate, or fly a shorter line."
            )
        self.backend.set_no_of_captures(self.segments)

        self._timebase, self._interval = self._resolve_timebase()
        if not self._close(self._interval, self._expected_interval):
            print(
                f"[WARNING] Asked for {self.sample_rate / 1e6:g} MS/s and the "
                f"unit's nearest timebase is "
                f"{1.0 / self._interval / 1e6:g} MS/s. The delay axis is "
                f"{self._interval:.4E}s per sample, not "
                f"{self._expected_interval:.4E}s; every file records the "
                f"achieved interval, but the config and the data will not "
                f"agree until you state a rate this unit has."
            )

        # Buffers are allocated on the first line and reused for the rest -
        # a fresh multi-megabyte allocation per line is pure per-line cost,
        # and a fly scan is thousands of lines. Not allocated here because
        # the sparsing factors arrive with download_line(), and a channel
        # sparsed 100x needs a hundredth of the memory: at thousands of
        # segments, sizing every channel for the full record would be tens
        # of megabytes held for nothing.
        self._buffers = {}

        self._line_timeout_s = self._timeout_for_line()
        return {
            'segments': self.segments,
            'points_per_segment': self.points_per_segment,
            'requested_interval': self._expected_interval,
            'timeout_s': self._line_timeout_s,
        }

    def configure_flat_line(self, line_seconds, sample_rate, delay_rate):
        """Capture a whole line as ONE record, segmentation off.

        The diagnostic counterpart to configure_line(), as on the LeCroy: it
        keeps every microsecond including the parts of the delay window a
        segmented capture throws away, which is how you find out where the
        signal actually sits.

        On a PicoScope this is one segment rather than a different mode -
        there is no SEQ to turn off - so it needs no separate path and
        nothing can be left segmented behind it.
        """
        return self.configure_line(
            segments=1, segment_seconds=line_seconds,
            sample_rate=sample_rate, delay_rate=delay_rate,
        )

    def _apply_channels(self):
        """Enable exactly the configured channels, with their ranges."""
        pico = self._pico

        self._indices = {
            name: pico.channel_index(name, self.series)
            for name in self.channels
        }
        self._range_index = {
            name: pico.resolve_range(
                float(self.v_max.get(name, pico.DEFAULT_V_MAX)), self.series
            )
            for name in self.channels
        }

        # Unused inputs are switched OFF explicitly, not left as found: on a
        # PicoScope the enabled channel count decides which timebases and how
        # much capture memory exist, so an unknown channel state makes the
        # sample rate and the segment count unreproducible.
        wanted = {index: name for name, index in self._indices.items()}
        for index in range(pico.SERIES[self.series]['n_channels']):
            name = wanted.get(index)
            if name is None:
                self.backend.set_channel(index, False)
                continue
            coupling = str(self.coupling.get(name, pico.DEFAULT_COUPLING)).lower()
            if coupling not in pico.COUPLINGS:
                raise ValueError(
                    f"coupling for channel {name} must be dc or ac, "
                    f"not '{coupling}'"
                )
            self.backend.set_channel(
                index, True, coupling=pico.COUPLINGS[coupling],
                range_index=self._range_index[name],
            )

        self._max_adc = self.backend.max_adc()

    def _apply_trigger(self):
        """Arm on the delay clock, from the config's 'trigger' line."""
        pico = self._pico

        source = str(self.trigger['source']).strip().upper()
        edge = str(self.trigger['direction']).strip().lower()
        if edge not in pico.EDGES:
            raise ValueError(
                f"trigger edge must be one of {', '.join(sorted(pico.EDGES))}, "
                f"not '{edge}'"
            )
        level = float(self.trigger['level'])

        if source in ('EXT', 'EXTERNAL'):
            index = pico.SERIES[self.series]['external']
            full_scale = pico.EXTERNAL_FULL_SCALE_V
        else:
            if source not in self._indices:
                raise ValueError(
                    f"the trigger names channel {source}, which this scan "
                    f"does not record. A PicoScope can only trigger on a "
                    f"channel that is switched on."
                )
            index = self._indices[source]
            full_scale = pico.RANGE_VOLTS[self._range_index[source]]

        threshold = int(round(level / full_scale * self._max_adc))
        if abs(threshold) > self._max_adc:
            raise ValueError(
                f"a {level:g} V trigger level is outside the "
                f"+/-{full_scale:g} V the trigger source can see, so it would "
                f"never fire and no segment would ever fill."
            )

        # auto_trigger_ms=0: never self-trigger. A PicoScope that gave up
        # waiting and triggered itself would hand back a segment that is not
        # a delay sweep, which is the same failure TRMD AUTO causes on a
        # LeCroy - and it would look like perfectly good data.
        self.backend.set_simple_trigger(
            True, source=index, threshold_adc=threshold,
            direction=pico.EDGES[edge], auto_trigger_ms=0,
        )

    def _resolve_timebase(self):
        """(timebase, interval_s): the fastest rate not slower than asked.

        The ladder is model-dependent and narrows as channels are enabled, so
        the only honest way to pick a rung is to ask the unit. Intervals rise
        monotonically with the index, so bracket by doubling and bisect:
        tens of driver calls once per scan rather than a scan through a
        hundred thousand indices for a slow rate.
        """
        target = self._expected_interval
        points = self.points_per_segment

        lowest = None
        for timebase in range(0, 64):
            if self.backend.timebase(timebase, points) is not None:
                lowest = timebase
                break
        if lowest is None:
            raise ScopeConfigError(
                f"the unit accepted no timebase for {points} samples on "
                f"{len(self.channels)} channel(s). Fewer samples per segment "
                f"or fewer channels will."
            )

        fastest = self.backend.timebase(lowest, points)[0]
        if fastest > target:
            raise ScopeConfigError(
                f"the fastest this unit can sample {points} point(s) on "
                f"{len(self.channels)} channel(s) is "
                f"{1.0 / fastest / 1e6:g} MS/s, and the scan asks for "
                f"{self.sample_rate / 1e6:g} MS/s. The sample rate sets the "
                f"delay resolution, so this is refused rather than quietly "
                f"halved: lower sample_rate, shorten segment_window, or "
                f"record fewer channels."
            )

        low = high = lowest
        probe = max(1, lowest)
        while True:
            probe = min(probe * 2, 0xFFFFFFFF)
            answer = self.backend.timebase(probe, points)
            if answer is None or answer[0] > target:
                high = probe
                break
            low = probe
            if probe >= 0xFFFFFFFF:
                high = probe
                break

        best = low
        while low + 1 < high:
            middle = (low + high) // 2
            answer = self.backend.timebase(middle, points)
            if answer is not None and answer[0] <= target:
                low = best = middle
            else:
                high = middle

        return best, self.backend.timebase(best, points)[0]

    def _timeout_for_line(self):
        """How long to wait for a line to fill, with margin.

        Only the fill: unlike VXI-11, the transfer is a separate blocking
        call and is not what this bounds. What decides it is the delay clock,
        since the line is complete when `segments` sweeps have arrived - so a
        timeout means triggers are missing, not that the bus is slow.
        """
        if self.timeout_s is not None:
            return self.timeout_s

        fill_s = self.segments / self.delay_rate
        return max(TIMEOUT_FLOOR_S, TIMEOUT_MARGIN * fill_s)

    # -------------------------------------------------------- acquisition

    def arm_line(self):
        """Arm for one line and return immediately.

        Separate from wait_for_line() for the same reason as on the LeCroy:
        the stage has to be set moving in between, and the ASOPS clock starts
        filling segments the moment the unit is armed.
        """
        if self.segments is None:
            raise RuntimeError("Call configure_line() before arming.")

        self.backend.run_block(
            0, self.points_per_segment, self._timebase
        )
        self._armed_at = time.perf_counter()

    def wait_for_line(self):
        """Block until every segment has been triggered and captured."""
        if self._armed_at is None:
            raise RuntimeError("Call arm_line() before waiting.")

        deadline = self._armed_at + self._line_timeout_s
        # Sleep through the part whose duration the delay clock already
        # decides, then poll: a line takes segments / delay_rate to fill, so
        # polling the whole way through would be a busy loop for the entire
        # flight.
        fill_s = self.segments / self.delay_rate
        remaining = fill_s * 0.9 - (time.perf_counter() - self._armed_at)
        if remaining > 0.002:
            time.sleep(remaining)

        poll = 0
        while not self.backend.is_ready():
            if time.perf_counter() > deadline:
                self.backend.stop()
                raise RuntimeError(
                    f"the unit did not finish a line within "
                    f"{self._line_timeout_s:.1f}s. With no trigger arriving "
                    f"it never self-triggers, by design, so this usually "
                    f"means the delay clock is not reaching the trigger "
                    f"input."
                )
            poll += 1
            time.sleep(0.0005 if poll > 40 else 0.0)

    def download_line(self, channels, sparsing=None):
        """Transfer one line and assemble it.

        Returns {channel: {'samples', 'meta'}} plus 'trigtime', 'achieved'
        and 'trigtime_source' - the same shape FlyScope returns, so
        flyscan.raster cannot tell the two apart.
        """
        pico = self._pico
        sparsing = sparsing or {}

        for channel in channels:
            if channel not in self._indices:
                raise ValueError(
                    f"channel {channel} was not configured for this scan; "
                    f"configured: {', '.join(self.channels)}"
                )

        # Read the capture count back before trusting any of it. Every
        # segment is one copeak, so a short count does not shorten the line -
        # it misaligns every pixel in it.
        captured = self.backend.get_no_of_captures()
        if captured != self.segments:
            self.backend.stop()
            raise RuntimeError(
                f"the unit captured {captured} of {self.segments} segments, "
                f"so {self.segments - captured} delay sweep(s) never "
                f"triggered. Every segment is one pixel, so the line would "
                f"be misaligned rather than merely short."
            )

        result = {}
        overflowed = 0

        for channel in channels:
            factor = max(1, int(sparsing.get(channel, 1)))
            index = self._indices[channel]
            mode = (pico.RATIO_MODE_NONE if factor == 1
                    else pico.RATIO_MODE_DECIMATE)
            points = (self.points_per_segment if factor == 1
                      else self.points_per_segment // factor)
            if points < 1:
                raise ValueError(
                    f"sparsing {channel} by {factor} leaves nothing of a "
                    f"{self.points_per_segment}-point segment"
                )

            view = self._buffer_for(channel, points)
            self.backend.set_data_buffers(index, view, mode=mode)

            returned, overflow = self.backend.get_values_bulk(
                points, self.segments, ratio=factor, mode=mode
            )

            # Cleared before the next channel: a retrieval fills every buffer
            # still registered for its ratio mode, so leaving this one in
            # place would transfer it again at the next channel's ratio,
            # into a buffer sized for its own.
            self.backend.clear_data_buffers(index, self.segments, mode=mode)

            if returned != points:
                raise RuntimeError(
                    f"{channel}: {returned} of {points} sample(s) came back "
                    f"per segment. A short segment would misplace every "
                    f"sample after it in the delay axis."
                )

            for flags in overflow:
                overflowed |= int(flags)

            meta = self._meta_for(channel, factor, points)
            meta['requested_sparsing'] = factor
            result[channel] = {
                # A copy, not the live buffer: the buffer is reused on the
                # next line, and raster hands this array to the writer.
                'samples': np.array(view, dtype=np.int16),
                'meta': meta,
            }

        # Trigger offsets before the stop: they belong to the run that is
        # still open, and the vendor's own rapid-block sequence reads them
        # between the retrieval and the stop.
        trigtime, source = self._trigtime()
        self.backend.stop()

        # Reported once per distinct result rather than once per line: a
        # scan is thousands of lines, and a steady problem printing on every
        # one of them is both noise and stdout I/O inside the line loop. A
        # NEW set of clipped channels still surfaces.
        if overflowed != self._last_overflow:
            self._last_overflow = overflowed
            clipped = [c for c in channels
                       if overflowed & (1 << self._indices[c])]
            if clipped:
                print(f"[WARNING] {', '.join(clipped)} clipped, from this "
                      f"line on; the peaks are flat. Widen v_max for those "
                      f"channels.")

        result['trigtime'] = trigtime
        result['trigtime_source'] = source

        # The same fields FlyScope reports, read off what came back rather
        # than off what was asked for - which is the point of the dict.
        result['achieved'] = {}
        for channel in channels:
            meta = result[channel]['meta']
            result['achieved'][channel] = {
                'segments': meta['segments'],
                'points_per_segment': meta['points_per_segment'],
                'horiz_interval': meta['horiz_interval'],
                'sparsing_factor': meta['sparsing_factor'],
                'requested_sparsing': meta['requested_sparsing'],
                'sweeps_per_acq': meta['sweeps_per_acq'],
                'nominal_bits': meta['nominal_bits'],
            }
        return result

    def _buffer_for(self, channel, points):
        """This channel's capture buffer, allocated once and then reused.

        Sized to the DECIMATED length, so a 100x-sparsed channel holds a
        hundredth of what the signal channel does. Re-allocated only if the
        geometry changes, which in a scan it does not.
        """
        buffer = self._buffers.get(channel)
        if buffer is None or buffer.shape != (self.segments, points):
            buffer = np.zeros((self.segments, points), dtype=np.int16)
            self._buffers[channel] = buffer
        return buffer

    def _meta_for(self, channel, factor, points):
        """The LeCroy wavedesc fields, as a PicoScope can honestly fill them."""
        pico = self._pico
        full_scale = pico.RANGE_VOLTS[self._range_index[channel]]
        return {
            'segments': self.segments,
            'points_per_segment': points,
            'total_points': self.segments * points,
            'sample_bytes': 2,
            'byte_order': 'little',
            'sparsing_factor': factor,
            # No hardware averaging in this path: one sweep per segment, which
            # is what a fly line wants - averaging happens over copeaks when
            # the map is binned.
            'sweeps_per_acq': 1,
            'vertical_gain': full_scale / self._max_adc,
            # Zero: no analog offset is applied, so counts are symmetric
            # about ground and volts = v_gain * counts.
            'vertical_offset': 0.0,
            'max_value': float(self._max_adc),
            'min_value': float(-self._max_adc),
            # The API does not report the ADC width except on the series
            # where it is switchable, and a number guessed from a model
            # string would be worse than none.
            'nominal_bits': (self.resolution_bits
                             if pico.SERIES[self.series]['resolution_arg']
                             else 0),
            'horiz_interval': self._interval * factor,
            'horiz_offset': 0.0,
            'instrument': f"picoscope {self.series}",
        }

    def _trigtime(self):
        """((segments, 2) trigger times, how they were obtained).

        Column 1 is the time of each segment's trigger relative to the
        first, which is what places a copeak along the line. Block mode does
        not report it, so it comes from the delay clock: segment i fires at
        i / delay_rate, which is exact for a fixed-rate clock and is only
        wrong if a trigger was missed - and a missed trigger shows up as a
        short capture count, which download_line() has already refused.

        Column 2 is the sub-sample offset between the trigger and the first
        sample, which the unit DOES report. Where it will not, the column is
        zero and the source says so rather than implying a measurement.
        """
        times = np.arange(self.segments, dtype=np.float64) / self.delay_rate

        offsets = None
        if not self._offsets_unavailable:
            offsets = self.backend.trigger_time_offsets(self.segments)
            if offsets is None:
                # Asked once per scan, not once per line.
                self._offsets_unavailable = True

        if offsets is None:
            column2 = np.zeros(self.segments, dtype=np.float64)
            source = 'delay_clock'
        else:
            column2 = np.asarray(offsets, dtype=np.float64)
            source = 'delay_clock+measured_offset'

        return np.column_stack((times, column2)), source

    def acquire_line(self, channels, sparsing=None, on_armed=None):
        """Arm, run `on_armed`, wait, download."""
        self.arm_line()
        if on_armed is not None:
            on_armed()
        self.wait_for_line()
        return self.download_line(channels, sparsing)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _close(value, target):
        if target == 0:
            return value == 0
        return abs(value - target) / abs(target) <= INTERVAL_TOLERANCE

    def line_budget(self, channels, sparsing=None):
        """Points and bytes this configuration will move, before running it.

        Decimation happens in the scope, so sparsing reduces what crosses the
        bus here exactly as WFSU does on a LeCroy.
        """
        sparsing = sparsing or {}
        total_points = 0
        per_channel = {}

        for channel in channels:
            factor = max(1, int(sparsing.get(channel, 1)))
            points = self.segments * max(1, self.points_per_segment // factor)
            per_channel[channel] = points
            total_points += points

        megabytes = total_points * 2 / 1e6
        return {
            'per_channel_points': per_channel,
            'total_points': total_points,
            'megabytes': megabytes,
            'fill_s': self.segments / self.delay_rate,
            'estimated_transfer_s': megabytes / max(self.transfer_mb_s, 0.1),
        }
