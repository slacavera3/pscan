"""PicoScope USB oscilloscopes, through the PicoSDK C libraries via ctypes.

The LeCroy is a benchtop instrument on the lab network; a PicoScope is a USB
box with no screen and no front panel, so everything the LeCroy answers from
its own settings - range, coupling, timebase, trigger, how many captures -
has to be stated in the .con file instead. That is the only real difference
between 'action scope' and 'action picoscope'.

WHAT IT PRODUCES IS THE SAME FILE. Volts land in <base>_pico0.dat as int16
ADC counts with the per-channel scaling in <base>_pico0.m, written by
pscan.scope_io - the same writer the LeCroy path uses. A MATLAB script that
reads a LeCroy run reads a PicoScope run unchanged.

WHY ctypes AND NOT THE picosdk PYTHON WRAPPER. Same reason the Phidget board
is driven through ctypes and libphidget21 rather than Phidget22: the C
libraries are what the vendor's own installer puts on the machine, so binding
to them directly means the Python side has nothing to install and nothing to
keep in version step. install.sh adds Pico's apt repository; on Windows the
PicoSDK installer or the PicoScope application puts the DLLs in place.

SUPPORTED SERIES: the 2000A, 3000A, 4000A and 5000A families (libps2000a,
libps3000a, libps4000a, libps5000a). Their block-mode APIs differ only in the
handful of places this file's SERIES table names. The older non-A libraries
(libps2000, libps3000) are a different API and are NOT supported: a 2206A is
fine, a 2205 is not.

HOW A UNIT IS CHOSEN. Same rule pscan.daq applies to DAQ cards, for the same
reason: enumerate, and refuse to guess. Exactly one unit is used and named in
the log; more than one stops the run and prints the lines to paste; none
stops the run and says which libraries were missing. Quietly recording the
wrong instrument produces a complete, plausible-looking set of files that
nothing downstream can tell is wrong.
"""

import ctypes
import ctypes.util
import glob
import os
import time

import numpy as np

from pscan.scope_io import ScopeTraceWriter


# --- PICO_STATUS codes ------------------------------------------------------
# Only the ones acted on, by number. Anything else is reported as a hex code
# rather than decoded with a name this file cannot verify - a wrong name in
# an error message is worse than no name.
PICO_OK = 0x00000000
PICO_NOT_FOUND = 0x00000003
PICO_NOT_RESPONDING = 0x00000007
PICO_POWER_SUPPLY_CONNECTED = 0x00000119
PICO_POWER_SUPPLY_NOT_CONNECTED = 0x0000011A
PICO_USB3_0_DEVICE_NON_USB3_0_PORT = 0x0000011E

STATUS_NAMES = {
    PICO_OK: 'PICO_OK',
    PICO_NOT_FOUND: 'PICO_NOT_FOUND',
    PICO_NOT_RESPONDING: 'PICO_NOT_RESPONDING',
    PICO_POWER_SUPPLY_CONNECTED: 'PICO_POWER_SUPPLY_CONNECTED',
    PICO_POWER_SUPPLY_NOT_CONNECTED: 'PICO_POWER_SUPPLY_NOT_CONNECTED',
    PICO_USB3_0_DEVICE_NON_USB3_0_PORT:
        'PICO_USB3_0_DEVICE_NON_USB3_0_PORT',
}

# Opening a 3000A/4000A/5000A unit on bus power alone returns one of these
# instead of PICO_OK. The unit IS open and the handle IS valid; the driver
# just wants the condition acknowledged with ChangePowerSource before it will
# run. Without this a perfectly healthy USB-powered scope looks like a
# failure to open.
POWER_STATUSES = (
    PICO_POWER_SUPPLY_NOT_CONNECTED,
    PICO_USB3_0_DEVICE_NON_USB3_0_PORT,
)

# PICO_INFO selectors for GetUnitInfo.
PICO_DRIVER_VERSION = 0
PICO_VARIANT_INFO = 3
PICO_BATCH_AND_SERIAL = 4

# Full-scale volts for each range index. One table for every series: the
# enums are spelled per-family (PS2000A_RANGE, PICO_CONNECT_PROBE_RANGE on
# the 4000A) but the ordering is identical, so the index means the same thing
# everywhere. Indices above a series' max_range do not exist on it.
RANGE_VOLTS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0,
               50.0, 100.0, 200.0)

COUPLINGS = {'ac': 0, 'dc': 1}

# PS*_THRESHOLD_DIRECTION. Only the edge and level directions are offered;
# window and runt triggers need the full trigger-condition API, not
# SetSimpleTrigger.
EDGES = {
    'rising': 2,
    'falling': 3,
    'above': 0,
    'below': 1,
    'rising_or_falling': 4,
}

# PS*_RATIO_MODE_NONE: every sample, no aggregation or decimation in the
# driver. Averaging, where asked for, is done here over whole captures so the
# ADC counts in the file keep the one scaling the .m declares.
RATIO_MODE_NONE = 0

# PS*_RATIO_MODE_DECIMATE: return every Nth sample, discarding the rest IN
# THE SCOPE, so the transfer shrinks with the ratio. Used by flyscan to let a
# slowly-varying probe-DC or position channel ride along at a few percent of
# the signal channel's transfer cost - the same job WFSU's SP factor does on
# a LeCroy. Not used by pscan, which transfers every sample of every channel.
RATIO_MODE_DECIMATE = 2

# PS*_TIME_UNITS, as a multiplier into seconds. Used to scale the per-segment
# trigger-time offsets, which are reported as an integer plus a unit.
TIME_UNIT_SECONDS = (1e-15, 1e-12, 1e-9, 1e-6, 1e-3, 1.0)

# The external trigger input is not a measuring channel and has no range
# setting: its threshold is expressed in ADC counts across a fixed +/-5 V
# span on every series this file drives.
EXTERNAL_FULL_SCALE_V = 5.0

#: Per-family differences, which are the whole of what varies between the
#: four supported libraries:
#:
#:   n_channels      analog inputs, and so the valid channel letters
#:   external        the PS*_CHANNEL value of the external trigger input
#:   max_range       highest valid range index (see RANGE_VOLTS)
#:   oversample      GetTimebase2 and RunBlock still carry the deprecated
#:                   oversample argument on the 2000A and 3000A
#:   resolution_arg  ps5000aOpenUnit takes the ADC resolution; the others
#:                   have a fixed 8-bit (2000A/3000A) or 12/14-bit (4000A)
#:                   converter
#:   power_source    has ChangePowerSource, needed to run on bus power
SERIES = {
    'ps2000a': {'n_channels': 4, 'external': 4, 'max_range': 11,
                'oversample': True, 'resolution_arg': False,
                'power_source': False},
    'ps3000a': {'n_channels': 4, 'external': 4, 'max_range': 11,
                'oversample': True, 'resolution_arg': False,
                'power_source': True},
    'ps4000a': {'n_channels': 8, 'external': 8, 'max_range': 13,
                'oversample': False, 'resolution_arg': False,
                'power_source': True},
    'ps5000a': {'n_channels': 4, 'external': 4, 'max_range': 11,
                'oversample': False, 'resolution_arg': True,
                'power_source': True},
}

#: Search order when no 'series' line pins one. Only used for discovery.
SERIES_ORDER = ('ps2000a', 'ps3000a', 'ps4000a', 'ps5000a')

#: PS5000A_DEVICE_RESOLUTION, keyed by the bit count a config would say.
RESOLUTIONS = {8: 0, 12: 1, 14: 2, 15: 3, 16: 4}

#: Defaults for everything a .con block may leave out.
DEFAULT_SAMPLE_RATE = 1e6
DEFAULT_N_SAMPLES = 1000
DEFAULT_V_MAX = 5.0
DEFAULT_COUPLING = 'dc'
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_RESOLUTION_BITS = 8

# ctypes shorthands
_i16 = ctypes.c_int16
_i32 = ctypes.c_int32
_u32 = ctypes.c_uint32
_f32 = ctypes.c_float
_P_i16 = ctypes.POINTER(ctypes.c_int16)
_P_i32 = ctypes.POINTER(ctypes.c_int32)
_P_u32 = ctypes.POINTER(ctypes.c_uint32)
_P_f32 = ctypes.POINTER(ctypes.c_float)
_STATUS = ctypes.c_uint32


class PicoError(RuntimeError):
    """A PicoSDK call failed, or no usable unit could be found."""


def status_text(status):
    """A PICO_STATUS as something a log line can carry."""
    name = STATUS_NAMES.get(status)
    return name if name else f"status 0x{status:08X} (see PicoStatus.h)"


def rate_text(interval_s):
    """A sample interval rendered as a rate, for a log line."""
    if not interval_s or interval_s <= 0:
        return "? MS/s"
    return f"{1.0 / interval_s / 1e6:.6g} MS/s"


def channel_index(name, series=None):
    """'A' -> 0. Validated against the series' channel count when known."""
    letter = str(name).strip().upper()
    if len(letter) != 1 or not ('A' <= letter <= 'Z'):
        raise PicoError(
            f"'{name}' is not a PicoScope channel. Channels are single "
            f"letters: A, B, C, D (and E..H on a 4000A)."
        )
    index = ord(letter) - ord('A')
    if series is not None:
        n = SERIES[series]['n_channels']
        if index >= n:
            last = chr(ord('A') + n - 1)
            raise PicoError(
                f"channel {letter} does not exist on a {series} unit, which "
                f"has {n} input(s): A..{last}"
            )
    return index


def resolve_range(v_max, series):
    """The narrowest range index whose full scale contains +/- v_max.

    The same rule pscan.daq applies to a DAQ card: a tighter range is more
    resolution, and asking for more than the hardware has fails loudly rather
    than being quietly widened - or quietly clipped.
    """
    limit = SERIES[series]['max_range']
    for index, volts in enumerate(RANGE_VOLTS[:limit + 1]):
        if volts >= v_max - 1e-12:
            return index
    raise PicoError(
        f"no {series} range reaches +/-{v_max:g} V; the widest is "
        f"+/-{RANGE_VOLTS[limit]:g} V. Use an attenuating probe, or lower "
        f"'v_max'."
    )


def _library_candidates(series):
    """Filenames to try for one series' shared library, best guess first.

    find_library() is first because that is what the vendor's own Python
    wrapper uses and it respects a working ldconfig or PATH. The explicit
    paths after it are what a real install actually looks like, because the
    packaged .so is versioned (libps2000a.so.2) and find_library does not
    always resolve those, and because the Windows SDK installs outside PATH.
    """
    candidates = []

    found = ctypes.util.find_library(series)
    if found:
        candidates.append(found)

    if os.name == 'nt':
        candidates.append(f"{series}.dll")
        roots = {
            os.environ.get('ProgramW6432'),
            os.environ.get('ProgramFiles'),
            os.environ.get('ProgramFiles(x86)'),
        }
        for root in filter(None, roots):
            pico = os.path.join(root, 'Pico Technology')
            # The SDK's own lib directory, then whatever PicoScope
            # application directory happens to be installed beside it.
            candidates.extend(sorted(glob.glob(
                os.path.join(pico, 'SDK', 'lib', f"{series}.dll"))))
            candidates.extend(sorted(glob.glob(
                os.path.join(pico, '*', f"{series}.dll"))))
    else:
        candidates.append(f"lib{series}.so")
        # Sorted in reverse so libps2000a.so.2 is preferred over .so.1.
        candidates.extend(sorted(
            glob.glob(f"/opt/picoscope/lib/lib{series}.so*"), reverse=True
        ))

    # Deduplicate, keeping order.
    seen, ordered = set(), []
    for name in candidates:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def load_library(series):
    """(loaded library, path) for one series, or raise saying what to install."""
    loader = ctypes.WinDLL if os.name == 'nt' else ctypes.CDLL
    attempts = []
    for name in _library_candidates(series):
        try:
            return loader(name), name
        except OSError as e:
            attempts.append(f"{name}: {e}")

    raise PicoError(
        f"the PicoSDK library for the {series} series is not installed "
        f"(tried {len(attempts)} name(s)). On Linux: 'bash install.sh', "
        f"which adds Pico's apt repository and installs lib{series}. On "
        f"Windows: install PicoSDK or the PicoScope application from "
        f"picotech.com. Attempts: " + "; ".join(attempts)
    )


class PicoBackend:
    """One loaded PicoSDK library and at most one open unit.

    Everything ctypes lives here, behind plain Python arguments and return
    values, so the acquisition logic above it can be exercised against a
    stand-in - which is what tests/fake_picoscope.py is. Nothing in this
    class decides policy; it declares signatures and reports status codes.
    """

    def __init__(self, series):
        if series not in SERIES:
            raise PicoError(
                f"'{series}' is not a supported PicoScope series. Supported: "
                f"{', '.join(SERIES_ORDER)}. The older non-A libraries "
                f"(ps2000, ps3000) are a different API and are not driven by "
                f"this file."
            )
        self.series = series
        self.spec = SERIES[series]
        self.library_path = None
        self._lib = None
        self._symbols = {}
        self._handle = None

    # -- plumbing ----------------------------------------------------------

    def load(self):
        self._lib, self.library_path = load_library(self.series)

    def _sym(self, suffix, argtypes):
        """One PicoSDK entry point, argtypes declared once and cached."""
        if suffix not in self._symbols:
            try:
                fn = getattr(self._lib, f"{self.series}{suffix}")
            except AttributeError:
                raise PicoError(
                    f"{self.library_path} has no {self.series}{suffix}. The "
                    f"installed PicoSDK is too old for this driver."
                ) from None
            fn.restype = _STATUS
            fn.argtypes = argtypes
            self._symbols[suffix] = fn
        return self._symbols[suffix]

    def _check(self, status, what):
        if status != PICO_OK:
            raise PicoError(f"{self.series} {what}: {status_text(status)}")

    @property
    def handle(self):
        if self._handle is None:
            raise PicoError(f"no {self.series} unit is open")
        return self._handle

    @property
    def is_open(self):
        return self._handle is not None

    # -- discovery and lifetime -------------------------------------------

    def enumerate_units(self):
        """Serial numbers of attached units, without opening any of them."""
        count = _i16(0)
        buffer = ctypes.create_string_buffer(1024)
        length = _i16(ctypes.sizeof(buffer))

        status = self._sym(
            'EnumerateUnits', [_P_i16, ctypes.c_char_p, _P_i16]
        )(ctypes.byref(count), buffer, ctypes.byref(length))

        if status == PICO_NOT_FOUND or count.value <= 0:
            return []
        self._check(status, "EnumerateUnits")

        serials = buffer.value.decode('ascii', 'replace')
        return [s.strip() for s in serials.split(',') if s.strip()]

    def open(self, serial=None, resolution_bits=DEFAULT_RESOLUTION_BITS):
        encoded = None if serial is None else str(serial).encode('ascii')
        handle = _i16(0)

        if self.spec['resolution_arg']:
            if resolution_bits not in RESOLUTIONS:
                raise PicoError(
                    f"{self.series} resolution must be one of "
                    f"{', '.join(str(b) for b in sorted(RESOLUTIONS))} bits, "
                    f"not {resolution_bits}"
                )
            status = self._sym(
                'OpenUnit', [_P_i16, ctypes.c_char_p, _i32]
            )(ctypes.byref(handle), encoded, RESOLUTIONS[resolution_bits])
        else:
            status = self._sym(
                'OpenUnit', [_P_i16, ctypes.c_char_p]
            )(ctypes.byref(handle), encoded)

        if status in POWER_STATUSES and handle.value > 0:
            # Open, but running on bus power. Acknowledge it rather than
            # treating a healthy USB-powered unit as a failure.
            self._handle = handle.value
            if not self.spec['power_source']:
                raise PicoError(
                    f"{self.series} reported {status_text(status)} but has no "
                    f"ChangePowerSource call to accept it"
                )
            self._check(
                self._sym('ChangePowerSource', [_i16, _u32])(
                    self.handle, status
                ),
                "ChangePowerSource",
            )
            return

        self._check(status, "OpenUnit")
        if handle.value <= 0:
            raise PicoError(
                f"{self.series} OpenUnit returned handle {handle.value}"
            )
        self._handle = handle.value

    def close(self):
        if self._handle is None:
            return
        try:
            self._sym('CloseUnit', [_i16])(self._handle)
        finally:
            self._handle = None

    def unit_info(self, selector):
        buffer = ctypes.create_string_buffer(64)
        required = _i16(0)
        status = self._sym(
            'GetUnitInfo', [_i16, ctypes.c_char_p, _i16, _P_i16, _u32]
        )(self.handle, buffer, ctypes.sizeof(buffer),
          ctypes.byref(required), selector)
        if status != PICO_OK:
            return "?"
        return buffer.value.decode('ascii', 'replace').strip()

    def max_adc(self):
        value = _i16(0)
        self._check(
            self._sym('MaximumValue', [_i16, _P_i16])(
                self.handle, ctypes.byref(value)),
            "MaximumValue",
        )
        return value.value

    # -- configuration -----------------------------------------------------

    def set_channel(self, index, enabled, coupling=1, range_index=8,
                    offset_v=0.0):
        self._check(
            self._sym(
                'SetChannel', [_i16, _i32, _i16, _i32, _i32, _f32]
            )(self.handle, index, 1 if enabled else 0, coupling,
              range_index, offset_v),
            f"SetChannel({index})",
        )

    def set_simple_trigger(self, enabled, source=0, threshold_adc=0,
                           direction=EDGES['rising'], delay_samples=0,
                           auto_trigger_ms=0):
        self._check(
            self._sym(
                'SetSimpleTrigger',
                [_i16, _i16, _i32, _i16, _i32, _u32, _i16]
            )(self.handle, 1 if enabled else 0, source, threshold_adc,
              direction, delay_samples, auto_trigger_ms),
            "SetSimpleTrigger",
        )

    def timebase(self, timebase, n_samples):
        """(interval_s, max_samples), or None if the unit refuses this one.

        A refusal is ordinary: the fastest timebases are only available with
        fewer channels enabled, so the answer depends on the configuration
        already applied.
        """
        interval_ns = _f32(0.0)
        max_samples = _i32(0)

        if self.spec['oversample']:
            status = self._sym(
                'GetTimebase2', [_i16, _u32, _i32, _P_f32, _i16, _P_i32, _u32]
            )(self.handle, timebase, n_samples, ctypes.byref(interval_ns),
              1, ctypes.byref(max_samples), 0)
        else:
            status = self._sym(
                'GetTimebase2', [_i16, _u32, _i32, _P_f32, _P_i32, _u32]
            )(self.handle, timebase, n_samples, ctypes.byref(interval_ns),
              ctypes.byref(max_samples), 0)

        if status != PICO_OK:
            return None
        return interval_ns.value * 1e-9, max_samples.value

    def memory_segments(self, n_segments):
        """Split capture memory n ways; returns samples available per segment."""
        max_samples = _i32(0)
        self._check(
            self._sym('MemorySegments', [_i16, _u32, _P_i32])(
                self.handle, n_segments, ctypes.byref(max_samples)),
            f"MemorySegments({n_segments})",
        )
        return max_samples.value

    def set_no_of_captures(self, n_captures):
        self._check(
            self._sym('SetNoOfCaptures', [_i16, _u32])(
                self.handle, n_captures),
            f"SetNoOfCaptures({n_captures})",
        )

    # -- acquisition -------------------------------------------------------

    def run_block(self, pre_samples, post_samples, timebase):
        """Arm the unit. Returns the capture duration the driver predicts, s."""
        indisposed_ms = _i32(0)

        if self.spec['oversample']:
            status = self._sym(
                'RunBlock',
                [_i16, _i32, _i32, _u32, _i16, _P_i32, _u32,
                 ctypes.c_void_p, ctypes.c_void_p]
            )(self.handle, pre_samples, post_samples, timebase, 1,
              ctypes.byref(indisposed_ms), 0, None, None)
        else:
            status = self._sym(
                'RunBlock',
                [_i16, _i32, _i32, _u32, _P_i32, _u32,
                 ctypes.c_void_p, ctypes.c_void_p]
            )(self.handle, pre_samples, post_samples, timebase,
              ctypes.byref(indisposed_ms), 0, None, None)

        self._check(status, "RunBlock")
        return indisposed_ms.value / 1000.0

    def is_ready(self):
        ready = _i16(0)
        self._check(
            self._sym('IsReady', [_i16, _P_i16])(
                self.handle, ctypes.byref(ready)),
            "IsReady",
        )
        return ready.value != 0

    # SetDataBuffer takes one segment at a time - there is no range form in
    # this API - so a rapid-block line of N segments costs N calls per
    # channel. A fly line is thousands of segments across three or four
    # channels, which puts those calls squarely in the per-line path, so the
    # two methods below do the whole channel in one pass: the symbol is
    # resolved once, the argtypes list is built once, and each iteration
    # passes a plain integer address computed from the array's own stride
    # rather than boxing a fresh ctypes pointer. Same calls to the driver,
    # much less Python around them.
    _BUFFER_ARGS = [_i16, _i32, ctypes.c_void_p, _i32, _u32, _i32]

    def set_data_buffers(self, channel_index, segments, mode=RATIO_MODE_NONE):
        """Register one buffer per segment for a channel.

        `segments` is a 2-D int16 array (segment, sample) whose rows are
        each contiguous; the caller keeps it alive, since the driver is being
        handed its addresses. The registration is redone before every
        retrieval rather than once at setup: a stale registration the driver
        has forgotten returns PICO_OK and leaves the buffer holding the
        PREVIOUS acquisition, which is the one failure mode that would reach
        a data file silently.

        `mode` must match the ratio mode the retrieval asks for, which is
        what lets one channel be decimated while another is not.
        """
        fn = self._sym('SetDataBuffer', self._BUFFER_ARGS)
        handle = self.handle
        n_segments, length = segments.shape
        base = segments.ctypes.data
        stride = segments.strides[0]

        for segment in range(n_segments):
            status = fn(handle, channel_index, base + segment * stride,
                        length, segment, mode)
            if status != PICO_OK:
                self._check(
                    status,
                    f"SetDataBuffer(ch {channel_index}, segment {segment})",
                )

    def clear_data_buffers(self, channel_index, n_segments,
                           mode=RATIO_MODE_NONE):
        """Tell the driver to stop writing to this channel's buffers.

        A NULL buffer is how the API says "skip this one". Needed because a
        retrieval fills EVERY buffer still registered for the ratio mode it
        is asked for, so a channel left registered would be transferred
        again - at the next channel's decimation ratio, into a buffer sized
        for its own. That is both wasted bus bandwidth and wrong data.
        """
        fn = self._sym('SetDataBuffer', self._BUFFER_ARGS)
        handle = self.handle

        for segment in range(n_segments):
            status = fn(handle, channel_index, None, 0, segment, mode)
            if status != PICO_OK:
                self._check(
                    status,
                    f"SetDataBuffer(ch {channel_index}, segment {segment}, "
                    f"NULL)",
                )

    def get_values_bulk(self, n_samples, n_segments, ratio=1,
                        mode=RATIO_MODE_NONE):
        """Fill the registered buffers. Returns (samples, overflow flags).

        `n_samples` is how many the caller expects back, which with
        RATIO_MODE_DECIMATE is the DECIMATED length, not the record length.
        """
        samples = _u32(n_samples)
        overflow = (_i16 * n_segments)()

        self._check(
            self._sym(
                'GetValuesBulk', [_i16, _P_u32, _u32, _u32, _u32, _i32, _P_i16]
            )(self.handle, ctypes.byref(samples), 0, n_segments - 1,
              max(1, int(ratio)), mode, overflow),
            "GetValuesBulk",
        )
        return samples.value, list(overflow)

    def get_no_of_captures(self):
        """How many captures the unit actually collected.

        The direct analogue of reading SEQ? back on a LeCroy: every segment
        is one pixel of a fly line, so a short count misaligns the whole
        line and has to be caught rather than assumed away.
        """
        captures = _u32(0)
        self._check(
            self._sym('GetNoOfCaptures', [_i16, _P_u32])(
                self.handle, ctypes.byref(captures)),
            "GetNoOfCaptures",
        )
        return captures.value

    def trigger_time_offsets(self, n_segments):
        """Each segment's trigger-to-first-sample offset, in seconds.

        This is the sub-sample part only: the trigger point does not land on
        a sample boundary, and this is that remainder. It is NOT the time
        between triggers - block mode does not report that - so a caller
        that needs segment timing has to get the interval from the clock
        driving the triggers.

        Returns None when the unit will not answer, which is not worth
        failing a line over.
        """
        times = (ctypes.c_int64 * n_segments)()
        units = (_i32 * n_segments)()

        status = self._sym(
            'GetValuesTriggerTimeOffsetBulk64',
            [_i16, ctypes.POINTER(ctypes.c_int64),
             ctypes.POINTER(_i32), _u32, _u32]
        )(self.handle, times, units, 0, n_segments - 1)

        if status != PICO_OK:
            return None

        seconds = []
        for raw, unit in zip(times, units):
            if 0 <= unit < len(TIME_UNIT_SECONDS):
                seconds.append(raw * TIME_UNIT_SECONDS[unit])
            else:
                seconds.append(0.0)
        return seconds

    def stop(self):
        self._check(self._sym('Stop', [_i16])(self.handle), "Stop")


def discover(series=None, serial=None, backend_factory=PicoBackend):
    """Find the one unit this run means, or say why that is not possible.

    Returns (backend, serial). `series` and `serial` are the .con file's two
    ways of pinning a unit when a bench has more than one.
    """
    wanted = [series] if series else list(SERIES_ORDER)
    candidates = []
    problems = []
    loaded = {}

    for name in wanted:
        backend = backend_factory(name)
        try:
            backend.load()
        except PicoError as e:
            # The ordinary case on a real bench: a rig with a 5000A has
            # libps5000a installed and none of the other three.
            problems.append(str(e))
            continue
        loaded[name] = backend
        try:
            for found in backend.enumerate_units():
                candidates.append((name, found))
        except PicoError as e:
            problems.append(f"{name}: {e}")

    if serial is not None:
        matched = [c for c in candidates if c[1] == serial]
        if not matched:
            attached = ", ".join(f"{s} {n}" for s, n in candidates) or "none"
            raise PicoError(
                f"no PicoScope with serial '{serial}' is attached "
                f"(found: {attached}). Check the 'serial' line, or drop it to "
                f"use whichever unit is connected."
            )
        candidates = matched

    if not candidates:
        detail = ("\n  " + "\n  ".join(problems)) if problems else ""
        if not loaded:
            # Nothing even loaded, so this is an install problem and not a
            # missing scope. Said first, because the cable is the wrong
            # thing to go and check.
            raise PicoError(
                "no PicoSDK library could be loaded, so no PicoScope can be "
                "reached whatever is plugged in. On Linux run "
                "'bash install.sh', which adds Pico's apt repository and "
                "installs the libps*a packages; on Windows install PicoSDK "
                "or the PicoScope application from picotech.com." + detail
            )
        raise PicoError(
            "no PicoScope was found on USB. Check the cable and that the "
            "unit's light is on; a 2000/3000 series unit WITHOUT an 'A' in "
            "its model number uses a different PicoSDK library that pscan "
            "does not drive." + detail
        )

    if len(candidates) > 1:
        lines = "\n".join(
            f"    {s} unit {n}\n"
            f"        -> add to the 'action picoscope' block:  "
            f"series {s}   /   serial {n}"
            for s, n in candidates
        )
        raise PicoError(
            f"{len(candidates)} PicoScope units are attached, so which one "
            f"this block means is ambiguous. Recording the wrong one "
            f"produces a complete, plausible-looking file that nothing "
            f"downstream can tell is wrong, so the run stops here. "
            f"Pin it:\n{lines}"
        )

    found_series, found_serial = candidates[0]
    return loaded[found_series], found_serial


class PicoScope:
    """One USB PicoScope, driven in block mode, writing the scope format.

    The interface is deliberately the same as LeCroyScope's, so the
    orchestrator's per-coordinate branch is one branch and not two:
    connect(), acquire_multi_channel(), write_placeholder_trace(),
    finalize_matlab_metadata(), traces_written, disconnect().
    """

    def __init__(self, channels, series=None, serial=None,
                 resolution_bits=DEFAULT_RESOLUTION_BITS, v_max=None,
                 coupling=None, sample_rate=DEFAULT_SAMPLE_RATE,
                 n_samples=DEFAULT_N_SAMPLES, pretrigger=0.0, trigger=None,
                 timeout=DEFAULT_TIMEOUT_S, captures=1, averages=1,
                 backend=None, backend_factory=PicoBackend):
        if not channels:
            raise PicoError("an 'action picoscope' block lists no channels")

        self.channels = [str(c).strip().upper() for c in channels]
        self.series = series
        self.serial = serial
        self.resolution_bits = int(resolution_bits)
        self.sample_rate = float(sample_rate)
        self.n_samples = int(n_samples)
        self.pretrigger = float(pretrigger)
        self.trigger = trigger
        self.timeout = float(timeout)
        self.captures = int(captures)
        self.averages = int(averages)

        # The .con parser rejects all of this with a message naming the block,
        # so reaching here means the driver was built by something other than
        # a validated config - a bring-up script, or a test. Checked anyway,
        # because the alternative is a ctypes call with a nonsense argument.
        if self.series is not None and self.series not in SERIES:
            raise PicoError(
                f"'{self.series}' is not a supported PicoScope series. "
                f"Supported: {', '.join(SERIES_ORDER)}."
            )
        for label, value in (('sample_rate', self.sample_rate),
                             ('timeout', self.timeout)):
            if value <= 0:
                raise PicoError(f"{label} must be positive, not {value:g}")
        for label, value in (('n_samples', self.n_samples),
                             ('captures', self.captures),
                             ('averages', self.averages)):
            if value < 1:
                raise PicoError(f"{label} must be at least 1, not {value}")
        if not 0.0 <= self.pretrigger <= 100.0:
            raise PicoError(
                f"pretrigger is a percentage of the record, 0..100, not "
                f"{self.pretrigger:g}"
            )
        if self.trigger:
            missing = {'source', 'level', 'direction'} - set(self.trigger)
            if missing:
                raise PicoError(
                    f"a trigger needs a source, a level and a direction; "
                    f"missing: {', '.join(sorted(missing))}"
                )
            edge = str(self.trigger['direction']).strip().lower()
            if edge not in EDGES:
                raise PicoError(
                    f"trigger direction must be one of "
                    f"{', '.join(sorted(EDGES))}, not '{edge}'"
                )

        # One value per channel, broadcast from a single value.
        self.v_max = self._per_channel(
            v_max, DEFAULT_V_MAX, 'v_max', cast=float)
        self.coupling = self._per_channel(
            coupling, DEFAULT_COUPLING, 'coupling',
            cast=lambda value: str(value).strip().lower())
        for name in self.coupling:
            if name not in COUPLINGS:
                raise PicoError(
                    f"coupling must be dc or ac, not '{name}'"
                )

        self.backend = backend
        self._backend_factory = backend_factory

        # Resolved by _configure() on the first trace and reused after that:
        # re-deriving the timebase, or even re-deriving the channel indices,
        # at every coordinate would put work in the per-trace path for no
        # gain.
        self._configured = None
        self._indices = []
        self._buffer = None
        self._max_adc = None
        self._interval = None
        self._timebase = None
        self._pre_samples = 0
        self._range_index = []
        self._last_overflow = None

        self._writer = ScopeTraceWriter()

    # -- construction from a config block ---------------------------------

    @classmethod
    def from_block(cls, params, **overrides):
        """Build from a compiled 'action picoscope' block."""
        kwargs = dict(
            channels=params['channels'],
            series=params.get('series'),
            serial=params.get('serial'),
            resolution_bits=params.get('resolution') or DEFAULT_RESOLUTION_BITS,
            v_max=params.get('v_max'),
            coupling=params.get('coupling'),
            sample_rate=params.get('sample_rate') or DEFAULT_SAMPLE_RATE,
            n_samples=params.get('n_samples') or DEFAULT_N_SAMPLES,
            pretrigger=params.get('pretrigger') or 0.0,
            trigger=params.get('trigger'),
            timeout=params.get('timeout') or DEFAULT_TIMEOUT_S,
            captures=params.get('captures') or 1,
            averages=params.get('averages') or 1,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _per_channel(self, value, default, label, cast):
        if value is None:
            values = [default]
        elif isinstance(value, (list, tuple)):
            values = list(value)
        else:
            values = [value]

        if len(values) == 1:
            values = values * len(self.channels)
        if len(values) != len(self.channels):
            raise PicoError(
                f"'{label}' lists {len(values)} value(s) for "
                f"{len(self.channels)} channel(s). Give one value for all of "
                f"them, or one per channel in the same order."
            )
        return [cast(v) for v in values]

    # -- what the orchestrator reads --------------------------------------

    @property
    def traces_written(self):
        return self._writer.traces_written

    @property
    def last_trace_shape(self):
        return self._writer.last_trace_shape

    def describe(self):
        interval = self._interval or (1.0 / self.sample_rate)
        return (
            f"{self.series or 'picoscope'} {self.serial or ''} "
            f"{'+'.join(self.channels)}, {self.n_samples} samples @ "
            f"{rate_text(interval)}"
        ).replace('  ', ' ')

    # -- lifetime ----------------------------------------------------------

    def connect(self):
        if self.backend is None:
            self.backend, self.serial = discover(
                self.series, self.serial,
                backend_factory=self._backend_factory,
            )
            self.series = self.backend.series
        elif self.backend.library_path is None:
            self.backend.load()
            self.series = self.backend.series

        self.backend.open(
            serial=self.serial, resolution_bits=self.resolution_bits
        )

        model = self.backend.unit_info(PICO_VARIANT_INFO)
        batch = self.backend.unit_info(PICO_BATCH_AND_SERIAL)
        driver = self.backend.unit_info(PICO_DRIVER_VERSION)
        self.serial = self.serial or batch
        print(
            f" -> PicoScope {model} (serial {batch}), {self.series} driver "
            f"{driver}, via {self.backend.library_path}"
        )

    def disconnect(self):
        # Writes first: until the queue drains, the bytes of the last trace
        # are not guaranteed to be in the file.
        self._writer.flush()

        # A connect() that failed part way leaves a backend with nothing
        # open, which is not something to report as a teardown failure.
        if self.backend is None or not self.backend.is_open:
            return
        try:
            # Leaves the unit idle rather than armed and waiting on a
            # trigger that is not coming.
            self.backend.stop()
        except PicoError as e:
            print(f"\n[WARNING] PicoScope stop failed: {e}")
        finally:
            self.backend.close()

    # -- the shared scope contract ----------------------------------------

    def write_placeholder_trace(self, output_base_name):
        return self._writer.write_placeholder_trace(output_base_name)

    def finalize_matlab_metadata(self):
        return self._writer.finalize_matlab_metadata()

    # -- configuration, once per geometry ---------------------------------

    def _configure(self, channels, averages):
        """Apply channels, trigger, timebase and capture memory to the unit.

        Run on the first trace and again only if the geometry changes, which
        keeps the per-coordinate path to arm / wait / read.
        """
        indices = [channel_index(c, self.series) for c in channels]
        self._range_index = [
            resolve_range(v, self.series) for v in self.v_max
        ]

        # Every input is set explicitly, the unused ones off: on a PicoScope
        # the enabled channel count is what decides which timebases and how
        # much capture memory are available, so leaving a channel in an
        # unknown state makes the sample rate unreproducible.
        wanted = dict(zip(indices, range(len(indices))))
        for index in range(SERIES[self.series]['n_channels']):
            position = wanted.get(index)
            if position is None:
                self.backend.set_channel(index, False)
            else:
                self.backend.set_channel(
                    index, True,
                    coupling=COUPLINGS[self.coupling[position]],
                    range_index=self._range_index[position],
                )

        self._max_adc = self.backend.max_adc()
        self._apply_trigger(indices)

        self._pre_samples = int(round(
            self.n_samples * self.pretrigger / 100.0
        ))

        # Capture memory is split before the timebase is chosen, in that
        # order: a record length the memory cannot hold is refused without
        # probing the timebase ladder first, and the ladder is then asked
        # about with the segmentation that will actually be in force.
        segments = self.captures * averages
        per_segment = self.backend.memory_segments(segments)
        if per_segment < self.n_samples:
            raise PicoError(
                f"this unit holds {per_segment} sample(s) per segment when "
                f"its memory is split {segments} ways "
                f"({self.captures} capture(s) x {averages} average(s)), but "
                f"the block asks for {self.n_samples}. Lower 'n_samples', "
                f"'captures' or 'averages'."
            )
        self.backend.set_no_of_captures(segments)

        self._timebase, self._interval = self._resolve_timebase()

        # Allocated once and reused: a fresh multi-megabyte buffer at every
        # coordinate is pure per-trace cost.
        self._buffer = np.zeros(
            (segments, len(indices), self.n_samples), dtype=np.int16
        )
        self._configured = (tuple(channels), averages)

        print(
            f" -> PicoScope armed: {'+'.join(channels)} at "
            f"{', '.join(f'+/-{RANGE_VOLTS[i]:g}V' for i in self._range_index)}"
            f", {self.n_samples} samples @ {rate_text(self._interval)} "
            f"(asked {rate_text(1.0 / self.sample_rate)}), "
            f"{self.captures} capture(s)"
            f"{f' x {averages} averages' if averages > 1 else ''}"
        )
        return indices

    def _apply_trigger(self, indices):
        """SetSimpleTrigger from the block's 'trigger' line, or disable it."""
        if not self.trigger:
            # Disabled means RunBlock captures immediately, which is what an
            # untriggered DC-ish measurement wants.
            self.backend.set_simple_trigger(False)
            return

        source = str(self.trigger['source']).strip().upper()
        direction = EDGES[str(self.trigger['direction']).strip().lower()]
        level = float(self.trigger['level'])

        if source in ('EXT', 'EXTERNAL'):
            source_index = SERIES[self.series]['external']
            full_scale = EXTERNAL_FULL_SCALE_V
        else:
            source_index = channel_index(source, self.series)
            if source_index not in indices:
                raise PicoError(
                    f"the trigger names channel {source}, which this block "
                    f"does not record. A PicoScope can only trigger on a "
                    f"channel that is switched on, so add {source} to "
                    f"'channels' or trigger on one that is there."
                )
            full_scale = RANGE_VOLTS[self._range_index[indices.index(source_index)]]

        threshold = int(round(level / full_scale * self._max_adc))
        if abs(threshold) > self._max_adc:
            raise PicoError(
                f"a {level:g} V trigger level is outside the "
                f"+/-{full_scale:g} V the trigger source can see, so it "
                f"would never fire."
            )

        # auto_trigger_ms=0 means wait forever; the block's own 'timeout' is
        # what ends the wait, so a missed trigger is reported as a dropped
        # trace rather than silently recording whatever the input was doing.
        self.backend.set_simple_trigger(
            True, source=source_index, threshold_adc=threshold,
            direction=direction, auto_trigger_ms=0,
        )

    def _resolve_timebase(self):
        """(timebase, interval_s): the fastest rate that is not slower than asked.

        Pico timebases are a discrete, model-dependent ladder, and the rungs
        available depend on how many channels are on - so the only honest way
        to pick one is to ask the unit. Intervals rise monotonically with the
        index, so bracket by doubling and then bisect: ~60 driver calls once
        per run, rather than a scan through 100k indices for a slow rate.
        """
        target = 1.0 / self.sample_rate

        lowest = None
        for timebase in range(0, 64):
            if self.backend.timebase(timebase, self.n_samples) is not None:
                lowest = timebase
                break
        if lowest is None:
            raise PicoError(
                f"the unit accepted no timebase for {self.n_samples} "
                f"samples on {len(self.channels)} channel(s). Fewer samples "
                f"or fewer channels will."
            )

        fastest = self.backend.timebase(lowest, self.n_samples)[0]
        if fastest > target:
            print(
                f"\n[WARNING] The fastest this PicoScope can sample "
                f"{self.n_samples} point(s) on {len(self.channels)} "
                f"channel(s) is {rate_text(fastest)}, but the config asks "
                f"for {rate_text(target)}. Using the fastest available; "
                f"scp.hint in the .m records the rate actually used."
            )
            return lowest, fastest

        # Bracket: low is known fast enough, high is known too slow.
        low, high = lowest, lowest
        probe = max(1, lowest)
        while True:
            probe = min(probe * 2, 0xFFFFFFFF)
            answer = self.backend.timebase(probe, self.n_samples)
            if answer is None or answer[0] > target:
                high = probe
                break
            low = probe
            if probe >= 0xFFFFFFFF:
                high = probe
                break

        # Bisect for the last index that is still fast enough.
        best = low
        while low + 1 < high:
            middle = (low + high) // 2
            answer = self.backend.timebase(middle, self.n_samples)
            if answer is not None and answer[0] <= target:
                low = best = middle
            else:
                high = middle

        interval = self.backend.timebase(best, self.n_samples)[0]
        return best, interval

    # -- one trace ---------------------------------------------------------

    def acquire_multi_channel(self, channels, output_base_name,
                              save_for_matlab=True, is_first_trace=True,
                              total_loops=1, sweeps=None):
        """Capture one trace and queue it. False means "padded, carry on".

        The signature is LeCroyScope's, so one branch in the orchestrator
        drives either instrument. `sweeps` is this block's average count;
        passing a different one reconfigures, which is what makes a config
        change between runs take effect without a reconnect.
        """
        if self.backend is None or not self.backend.is_open:
            raise PicoError("PicoScope not connected. Call connect() first.")

        channels = [str(c).strip().upper() for c in channels]
        if channels != self.channels:
            # v_max and coupling are per channel, in the block's order, so a
            # different channel list would silently apply the wrong range to
            # the wrong input.
            raise PicoError(
                f"this PicoScope session was configured for "
                f"{'+'.join(self.channels)} and was asked for "
                f"{'+'.join(channels)}. One 'action picoscope' block owns the "
                f"unit for the whole run."
            )

        averages = self.averages if sweeps in (None, '') else int(sweeps)
        if averages < 1:
            averages = 1

        # A geometry the unit cannot do will not start working at the next
        # coordinate, so a configuration failure raises rather than being
        # padded over as a dropped trace.
        if self._configured != (tuple(channels), averages):
            self.averages = averages
            self._indices = self._configure(channels, averages)
        indices = self._indices

        segments = self.captures * averages

        try:
            predicted = self.backend.run_block(
                self._pre_samples, self.n_samples - self._pre_samples,
                self._timebase,
            )
            if not self._wait_ready(predicted):
                self.backend.stop()
                print(
                    f"\n[WARNING] PicoScope did not finish a capture within "
                    f"{self.timeout:g}s. With a trigger configured this "
                    f"usually means it never fired."
                )
                return False

            # Re-registered every trace on purpose - see set_data_buffers().
            # The buffer is (segment, channel, sample), so one channel's
            # segments are a strided view rather than a copy.
            for position, index in enumerate(indices):
                self.backend.set_data_buffers(
                    index, self._buffer[:, position, :]
                )

            returned, overflow = self.backend.get_values_bulk(
                self.n_samples, segments
            )
            self.backend.stop()
        except PicoError as e:
            print(f"\n[ERROR] PicoScope acquisition failed: {e}")
            return False

        if returned != self.n_samples:
            # A short block would shift every later trace in the file, so it
            # is refused rather than written - the same rule pscan.daq
            # applies to a short .d trace.
            print(
                f"\n[WARNING] PicoScope returned {returned} of "
                f"{self.n_samples} sample(s); discarding the trace rather "
                f"than writing a short one."
            )
            return False

        self._report_overflow(channels, indices, overflow)

        counts = self._buffer
        if averages > 1:
            # Average whole captures, in counts, so the one scaling the .m
            # declares still maps the file to volts.
            counts = np.rint(
                counts.reshape(
                    self.captures, averages, len(indices), self.n_samples
                ).mean(axis=1)
            ).astype(np.int16)

        # (segment, channel, point) flattened the way the format wants it:
        # for each segment, one row per channel.
        #
        # With no averaging this is a VIEW of the capture buffer, not a copy,
        # which is deliberate - the buffer is reused at every coordinate and
        # copying megabytes per trace is exactly the kind of per-trace cost
        # pscan is trying not to pay. It is safe because record_trace() calls
        # tobytes() synchronously before it queues anything; if it is ever
        # changed to hand the array itself to the writer thread, this needs a
        # copy or the next capture will overwrite a trace mid-write.
        matrix = np.ascontiguousarray(
            counts.reshape(self.captures * len(indices), self.n_samples)
        )

        metas = [
            {'v_gain': RANGE_VOLTS[self._range_index[position]] / self._max_adc,
             # Zero because set_channel() applies no analog offset: the
             # counts are symmetric about ground, so volts = v_gain * counts.
             'v_offset': 0.0}
            for position in range(len(indices))
        ]

        if save_for_matlab:
            self._writer.record_trace(
                output_base_name, channels, metas, matrix,
                is_first_trace=is_first_trace, total_loops=total_loops,
                h_interval=self._interval,
                # Time of the first sample relative to the trigger: negative
                # when some of the record is pre-trigger, which is the same
                # convention the LeCroy's HORIZ_OFFSET uses.
                h_offset=-self._pre_samples * self._interval,
                points=self.n_samples, segments=self.captures,
                sweeps=[averages] * len(channels),
                # No trigtime sidecar: block mode reports each segment's
                # sub-sample trigger offset but not the time between
                # triggers, and half of a (trigger_time, trigger_offset) pair
                # is worse than none - a loader would silently take the
                # missing half for zero.
                trigtime=None,
            )
        else:
            self._writer.remember_geometry(matrix.shape, None)

        return True

    def _wait_ready(self, predicted_s):
        """Block until the capture is complete, or the block's timeout.

        Most of a long capture is spent waiting for the hardware, so sleeping
        through the part whose duration the driver already told us keeps this
        off the CPU; the tail is polled tightly because that is where the
        per-trace latency actually is.
        """
        deadline = time.perf_counter() + self.timeout

        if predicted_s > 0.002:
            time.sleep(min(predicted_s * 0.9, self.timeout))

        poll = 0
        while True:
            if self.backend.is_ready():
                return True
            if time.perf_counter() > deadline:
                return False
            poll += 1
            # Spin briefly, then back off, so a fast capture is not held up
            # by a sleep and a long trigger wait is not a busy loop.
            if poll > 200:
                time.sleep(0.001)
            elif poll > 50:
                time.sleep(0.0001)

    def _report_overflow(self, channels, indices, overflow):
        """Warn when an input clipped, once per distinct result.

        Clipping is the silent failure a scope config gets wrong: the file is
        complete, the trace looks plausible, and the peaks are flat. Reported
        once per distinct set of channels rather than once per trace, so a
        steady problem says so a single time but a new one still surfaces.
        """
        flagged = 0
        for flags in overflow:
            flagged |= int(flags)
        if flagged == self._last_overflow:
            return
        self._last_overflow = flagged
        if not flagged:
            return

        clipped = [
            name for name, index in zip(channels, indices)
            if flagged & (1 << index)
        ]
        if clipped:
            print(
                f"\n[WARNING] PicoScope input(s) {', '.join(clipped)} went "
                f"outside their range and are CLIPPED. The trace is still "
                f"recorded, with flat peaks. Widen 'v_max' for those "
                f"channels."
            )
