"""A stand-in for the PicoSDK, so the USB scope path can be tested anywhere.

It implements exactly the surface pscan.picoscope_driver.PicoBackend exposes -
the ctypes layer and nothing above it - which is the seam the driver was
written around: every decision worth testing (which range, which timebase,
what lands in the file, what happens when a trigger never arrives) lives
above this line, and none of it needs a scope on the bench.

It lives in tests/ rather than pscan/ for the same reason tests/fake_card.py
does: a simulated instrument that could be picked up during a real scan would
produce a full set of plausible-looking files containing nothing but a
synthesised sine.
"""

import numpy as np

from pscan.picoscope_driver import SERIES, PicoError


class FakePicoBackend:
    """One imaginary PicoScope, with a 5000A-style timebase ladder."""

    def __init__(self, series='ps5000a', serials=('AB123/456',),
                 max_adc=32512, segment_capacity=1 << 20,
                 ready=True, overflow_mask=0, short_by=0):
        if series not in SERIES:
            raise PicoError(f"fake backend: unknown series {series}")
        self.series = series
        self.spec = SERIES[series]
        self.library_path = None
        self.serials = list(serials)

        self._max_adc = max_adc
        self._segment_capacity = segment_capacity

        # Knobs the failure-path tests turn.
        self.ready = ready            # False -> a trigger that never fires
        self.overflow_mask = overflow_mask  # bit per clipped channel
        self.short_by = short_by      # samples withheld from GetValuesBulk

        self._handle = None
        self.opened_with = None
        self.channels = {}            # index -> (enabled, coupling, range)
        self.trigger = None
        self.segments = 1
        self.captures = None
        self.buffers = {}             # (channel, segment, mode) -> int16 view
        self.runs = 0
        self.stops = 0
        self.closed = False

        # Failure knobs for the fly path: a short capture count means a delay
        # sweep was missed, and no offsets means the unit will not report
        # them. Both have to be survivable, in different ways.
        self.captured_override = None
        self.offsets_unavailable = False
        self.retrievals = []          # (channel, ratio, mode) per bulk call

    # -- plumbing ----------------------------------------------------------

    def load(self):
        self.library_path = f"fake-lib{self.series}.so"

    @property
    def is_open(self):
        return self._handle is not None

    # -- discovery and lifetime -------------------------------------------

    def enumerate_units(self):
        return list(self.serials)

    def open(self, serial=None, resolution_bits=8):
        if serial is not None and serial not in self.serials:
            raise PicoError(f"fake backend: no unit {serial}")
        self.opened_with = (serial, resolution_bits)
        self._handle = 1

    def close(self):
        self._handle = None
        self.closed = True

    def unit_info(self, selector):
        return {0: '2.1.0', 3: 'FAKE2000', 4: self.serials[0]}.get(selector, '?')

    def max_adc(self):
        return self._max_adc

    # -- configuration -----------------------------------------------------

    def set_channel(self, index, enabled, coupling=1, range_index=8,
                    offset_v=0.0):
        if index >= self.spec['n_channels']:
            raise PicoError(f"fake backend: no channel {index}")
        self.channels[index] = (bool(enabled), coupling, range_index)

    def set_simple_trigger(self, enabled, source=0, threshold_adc=0,
                           direction=2, delay_samples=0, auto_trigger_ms=0):
        self.trigger = (
            (source, threshold_adc, direction, auto_trigger_ms)
            if enabled else None
        )

    def timebase(self, timebase, n_samples):
        """The ps5000a 8-bit ladder: 1/2/4 ns, then (n-2)/125 MHz.

        Answered against the segmentation currently in force, as a real unit
        does - how much memory a segment has depends on how many there are.
        """
        capacity = self._segment_capacity // max(1, self.segments)
        if timebase < 0 or timebase > 0xFFFFFFFF:
            return None
        if n_samples > capacity:
            return None
        if timebase < 3:
            interval = (2 ** timebase) * 1e-9
        else:
            interval = (timebase - 2) / 125e6
        return interval, capacity

    def memory_segments(self, n_segments):
        self.segments = n_segments
        return self._segment_capacity // max(1, n_segments)

    def set_no_of_captures(self, n_captures):
        self.captures = n_captures

    # -- acquisition -------------------------------------------------------

    def run_block(self, pre_samples, post_samples, timebase):
        self.runs += 1
        self.armed = (pre_samples, post_samples, timebase)
        self.buffers = {}
        return 0.0

    def is_ready(self):
        return self.ready

    def set_data_buffers(self, channel_index, segments, mode=0):
        # Row views, so writing into them writes into the caller's buffer,
        # which is what the real driver does with the addresses it is given.
        for segment in range(segments.shape[0]):
            self.buffers[(channel_index, segment, mode)] = segments[segment]

    def clear_data_buffers(self, channel_index, n_segments, mode=0):
        for segment in range(n_segments):
            self.buffers.pop((channel_index, segment, mode), None)

    def waveform(self, channel, segment, n_samples):
        """The deterministic signal this fake pretends to measure.

        The value at sample i on channel c of segment s is

            round(0.4 * max_adc * sin(2*pi*(c+1)*i/n)) + s

        so a test can tell channels and segments apart, can predict exactly
        what averaging a group of segments produces, and can check that a
        decimated channel really holds every Nth sample rather than the
        first few.
        """
        index = np.arange(n_samples)
        wave = np.rint(
            0.4 * self._max_adc
            * np.sin(2 * np.pi * (channel + 1) * index / n_samples)
        ).astype(np.int16)
        return wave + np.int16(segment)

    def get_values_bulk(self, n_samples, n_segments, ratio=1, mode=0):
        """Fill the buffers registered for THIS ratio mode, and only those.

        Which is how a real unit behaves, and the reason the fly path clears
        a channel's buffers once it has read them.
        """
        ratio = max(1, int(ratio))
        count = n_samples - self.short_by

        for (channel, segment, buffer_mode), view in self.buffers.items():
            if buffer_mode != mode:
                continue
            self.retrievals.append((channel, ratio, mode))
            full = self.waveform(channel, segment, n_samples * ratio)
            view[:] = full[::ratio][:view.size]

        return count, [self.overflow_mask] * n_segments

    def get_no_of_captures(self):
        if self.captured_override is not None:
            return self.captured_override
        return self.captures if self.captures is not None else self.segments

    def trigger_time_offsets(self, n_segments):
        if self.offsets_unavailable:
            return None
        # A plausible sub-sample remainder: a few hundred picoseconds that
        # differs per segment, so a test can tell it from a zero column.
        return [(segment + 1) * 1e-10 for segment in range(n_segments)]

    def stop(self):
        self.stops += 1


class MissingLibraryBackend:
    """A series whose PicoSDK library is not installed on this machine.

    The normal case on a real rig: a bench with a 5000A has libps5000a and
    none of the others, so discovery has to walk past three failures to find
    the one unit.
    """

    def __init__(self, series):
        self.series = series
        self.spec = SERIES[series]
        self.library_path = None

    def load(self):
        raise PicoError(f"lib{self.series} is not installed")


def factory(units=None, **kwargs):
    """A backend_factory for PicoScope.discover().

    `units` maps a series to the serials attached to it; a series left out
    has no library installed. Default: one 5000A unit.
    """
    if units is None:
        units = {'ps5000a': ['AB123/456']}

    def make(series):
        if series not in units:
            return MissingLibraryBackend(series)
        return FakePicoBackend(series=series, serials=units[series], **kwargs)
    return make
