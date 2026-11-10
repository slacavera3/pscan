"""Which card, reached how - and everything a card driver should not repeat.

pscan used to have one answer: libcomedi, on /dev/comedi0. That stopped being
enough. comedi's ni_pcimio driver covers the NI E and M series and no X Series
board, so a PCIe-6351 is unreachable through it and always will be, while the
comedi cards already in service work fine and are not going anywhere.

There is one driver file per piece of hardware:

    pscan/a2d_driver.py          comedi cards           (NIDriver)
    pscan/daqmx_driver.py        NI-DAQmx cards         (DaqmxDriver)
    pscan/d2aphidget_driver.py   the Phidget AO board   (D2APhidgetDriver)

This module is the part that is not about any one of them: the acquisition
contract every a2d/d2a block is written against, the shared plumbing, and the
decision about which card a block means.

Two things here are load-bearing:

  * THE OUTPUT FORMAT. A .d file written through DAQmx must be byte-identical
    in layout to one written through comedi, because the MATLAB side
    downstream has no idea which produced it and must never need to. Drivers
    therefore never touch the output file - they return volts, and
    _queue_trace() below packs and writes them. That is the only place the
    format is decided.

  * REFUSING TO GUESS. On an instrument, silently picking the wrong card does
    not raise an error. It produces a complete, plausible-looking set of .d
    files containing the wrong signal, and nothing downstream can tell. So
    when more than one card could serve a block, select_device() stops and
    prints the two lines to paste into the .con file.

The registry is deliberately lazy. The drivers import DAQBackend from here, so
resolving them at module scope would be a circular import - and it also means
`import pscan.daq` does not drag in every ctypes binding on the machine to
answer a question about one card.
"""

import importlib
import queue
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

# How a single-ended input is wired. comedi calls these analog references and
# DAQmx calls them terminal configurations; they mean the same four things, so
# the .con file names them once and each driver maps them to its own constant.
TERMINALS = ('rse', 'nrse', 'diff', 'pseudodiff')

# What the comedi path did unconditionally before this key existed, kept as
# the default so an existing .con file acquires exactly as it used to.
DEFAULT_TERMINAL = 'rse'

# backend name -> (module, class). Resolved on demand by backend_class().
_BACKEND_SPECS = {
    'comedi': ('pscan.a2d_driver', 'NIDriver'),
    'daqmx': ('pscan.daqmx_driver', 'DaqmxDriver'),
    'nixseries': ('pscan.nixseries_driver', 'NIXSeriesDriver'),
    'phidget': (__name__, 'PhidgetD2A'),
}

#: Backend names a .con file may ask for. A plain tuple, so validating a
#: config costs no imports at all.
BACKEND_NAMES = tuple(sorted(_BACKEND_SPECS))

#: Default address per backend, used when a backend is named without a device.
#: daqmx has none: device names come from NI and are whatever the driver
#: assigned, so its connect() resolves a lone device instead.
DEFAULT_ADDRESS = {
    'comedi': '/dev/comedi0',
    'phidget': 'phidget0',
    'daqmx': None,
    # nixseries addresses are PCI slots from /sys/bus/pci/devices, so there is
    # nothing sensible to default to; its connect() resolves a lone NI card.
    'nixseries': None,
}

# Backends registered at runtime rather than shipped in source - currently
# only the fake card the tests inject. Keeping it out of _BACKEND_SPECS is
# what stops a simulated card ever standing in for a real one in a scan.
_registered = {}

# (backend name, address) -> open session. A card is opened once per process:
# a PCIe-6351 carries 16 AI and 2 AO on one board, so a scan that reads a
# photodiode and drives a mirror from the same card must not open it twice.
_sessions = {}


class DaqSelectionError(RuntimeError):
    """Raised when which card to use cannot be settled without guessing."""


@dataclass(frozen=True)
class DeviceInfo:
    """What a driver can say about one card without acquiring from it.

    Printed by `pydaq list` and once at connect time. Months later that
    log line is the only record of which card and which driver produced a
    given .d file.
    """
    backend: str
    address: str
    model: str
    n_ai: int = 0
    n_ao: int = 0
    ai_ranges: Tuple[Tuple[float, float], ...] = ()
    ao_ranges: Tuple[Tuple[float, float], ...] = ()
    max_ai_rate: Optional[float] = None

    def summary(self):
        parts = [f"{self.backend:<8} {self.address:<14} {self.model}"]
        if self.n_ai:
            rate = (f", {self.max_ai_rate / 1e6:g} MS/s"
                    if self.max_ai_rate else "")
            parts.append(f"{self.n_ai} AI ({format_ranges(self.ai_ranges)}{rate})")
        if self.n_ao:
            parts.append(f"{self.n_ao} AO ({format_ranges(self.ao_ranges)})")
        return "  |  ".join(parts)


def format_ranges(ranges):
    if not ranges:
        return "ranges unknown"
    return ", ".join(
        f"{lo:g}..{hi:g}V" if lo != -hi else f"+/-{hi:g}V"
        for lo, hi in ranges
    )


@dataclass(frozen=True)
class A2DConfig:
    """One a2d block's settings for grabbing DC photovoltage off a detector.

    Frozen and hashable on purpose: drivers key their cached hardware setup on
    it, so re-acquiring with identical settings - which is what every trace
    after the first in a scan does - skips re-negotiating with the card. That
    caching is the single biggest lever on per-trace cost.
    """
    channel: int
    n_samples: int
    sample_rate: float
    range_index: Optional[int] = None
    v_min: Optional[float] = None
    v_max: Optional[float] = None
    terminal: str = DEFAULT_TERMINAL


class AsyncWriter:
    """Background disk writer, one thread per open card.

    Exists so a trace's disk write does not sit between the acquisition
    returning and the stage being told to move. Queueing costs microseconds;
    the write lands while the stage is already travelling to the next
    coordinate. ctypes releases the GIL around the driver call, so the
    acquisition thread and this one genuinely overlap.
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        while True:
            task = self._queue.get()
            if task is None:  # poison pill
                self._queue.task_done()
                break

            filename, mode, data = task
            try:
                with open(filename, mode) as f:
                    f.write(data)
            except Exception as e:
                print(f"\n[ERROR] Async Write Failed for {filename}: {e}")
            finally:
                self._queue.task_done()

    def put(self, filename, mode, data):
        self._queue.put((filename, mode, data))

    def close(self, timeout=3.0):
        """Flush and stop. Called before the hardware closes, never after."""
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)


def resolve_range(ranges, range_index=None, v_min=None, v_max=None):
    """Pick the input range to use, from an index or from explicit volts.

    `ranges` is the card's own range list, in the card's own order, as
    (low, high) volt pairs. comedi indexes that list natively and DAQmx does
    not index at all, so resolving to (index, low, high) here is what lets one
    .con file mean the same thing on both.

    Explicit volts win over an index and select the NARROWEST range that still
    contains the request - for a photodiode a tighter range is more
    resolution, so "I need +/-2V" should not land on +/-10V. Giving only one
    side mirrors it, so `v_max 2` means -2..2.

    Returns (index, low, high). Raises ValueError when the request cannot be
    met, which is worth failing over: quietly widening the range would cost
    the caller bits of resolution they explicitly asked for.
    """
    if not ranges:
        raise ValueError("the card reported no input ranges")

    if v_min is None and v_max is not None:
        v_min = -abs(v_max)
    elif v_max is None and v_min is not None:
        v_max = abs(v_min)

    if v_min is not None and v_max is not None:
        if v_max <= v_min:
            raise ValueError(f"v_min {v_min:g} is not below v_max {v_max:g}")

        fits = [
            (hi - lo, i, lo, hi)
            for i, (lo, hi) in enumerate(ranges)
            if lo <= v_min and hi >= v_max
        ]
        if not fits:
            widest = max(ranges, key=lambda r: r[1] - r[0])
            raise ValueError(
                f"no range on this card covers {v_min:g}..{v_max:g} V; the "
                f"widest it offers is {widest[0]:g}..{widest[1]:g} V"
            )
        _span, index, lo, hi = min(fits)
        return index, lo, hi

    if range_index is None:
        range_index = 0

    if not 0 <= range_index < len(ranges):
        raise ValueError(
            f"range {range_index} is out of bounds: this card reports "
            f"{len(ranges)} range(s) ({format_ranges(ranges)}). Use an index "
            f"in 0..{len(ranges) - 1}, or say the volts you want with "
            f"'v_min'/'v_max' and let pscan pick."
        )

    lo, hi = ranges[range_index]
    return range_index, lo, hi


def clamp_volts(volts, ranges):
    """Hold a requested output voltage inside what the card can do.

    Returns (clamped, was_clamped). Driving a mirror or a piezo past its range
    is cheap to catch here and expensive to catch on the bench, so callers
    report was_clamped.
    """
    if not ranges:
        return volts, False

    lo = min(r[0] for r in ranges)
    hi = max(r[1] for r in ranges)
    clamped = max(lo, min(hi, volts))
    return clamped, clamped != volts


class DAQBackend:
    """One open session with one card.

    Drivers implement discovery, connect/disconnect, describe(),
    `_acquire_volts()` and `_write_ao()`. Everything else - the output format,
    the placeholder block, range resolution, output clamping, warn-once - is
    settled here so the drivers cannot drift apart on it.
    """

    #: Short name used in a .con file's `backend` line.
    name = "base"

    def __init__(self, address=None):
        self.address = address
        self._writer = AsyncWriter()
        self._connected = False
        self._warned = set()
        # Last voltage commanded per AO channel. Most cards cannot read their
        # own output back, so this is what read_d2a() reports by default.
        self._last_ao = {}

    # ------------------------------------------------------------------
    # Discovery. Classmethods: these answer "what is on this machine?" and
    # must not need a session to already exist.
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        """True when this driver's library is present and loadable."""
        return False

    @classmethod
    def unavailable_reason(cls):
        """Why available() is False, phrased for someone to act on."""
        return f"the {cls.name} backend is not implemented"

    @classmethod
    def enumerate(cls):
        """Every card this driver can see, as DeviceInfo. Never raises."""
        return []

    @classmethod
    def discoverable(cls):
        """Whether auto-detection may enumerate this driver unasked.

        False for drivers whose enumeration is intrusive. Opening the Phidget
        board claims it, and it may be holding a piezo at a position right
        now - not something to disturb while merely answering "what hardware
        is here?". Those are reachable only by being named.
        """
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        raise NotImplementedError

    def disconnect(self):
        """Flush pending writes, then release the hardware. In that order."""
        self._writer.close()
        self._close_device()
        self._connected = False

    def _close_device(self):
        raise NotImplementedError

    @property
    def connected(self):
        return self._connected

    def describe(self):
        """DeviceInfo for this open session."""
        raise NotImplementedError

    def ai_ranges(self):
        return self.describe().ai_ranges

    def ao_ranges(self):
        return self.describe().ao_ranges

    def _warn_once(self, key, message):
        """Print a warning once per session, not once per trace.

        A scan is thousands of traces; a per-trace warning buries everything
        else in the log.
        """
        if key not in self._warned:
            self._warned.add(key)
            print(f"\n[WARNING] {self.name.upper()}: {message}")

    # ------------------------------------------------------------------
    # a2d - DC photovoltage off a detector
    # ------------------------------------------------------------------

    def acquire_a2d(self, channel, range_val=None, n_samples=100,
                    sample_rate=10000, output_filename=None,
                    is_first_trace=True, v_min=None, v_max=None,
                    terminal=DEFAULT_TERMINAL):
        """Acquire one trace and queue it to disk as float32 volts.

        The signature keeps `range_val`'s name and position from the original
        comedi driver, so main.py and the dev/ timing scripts call this
        unchanged.

        Returns True when a trace was recorded. False means nothing was
        written, and the caller must write the placeholder block - skipping it
        would leave the .d file one trace short and silently misalign every
        later trace against its coordinate.
        """
        if not self._connected:
            print("[ERROR] A2D Driver not connected. Call connect() first.")
            return False

        if terminal not in TERMINALS:
            raise ValueError(
                f"terminal '{terminal}' is not one of: {', '.join(TERMINALS)}"
            )

        config = A2DConfig(
            channel=channel,
            n_samples=n_samples,
            sample_rate=sample_rate,
            range_index=range_val,
            v_min=v_min,
            v_max=v_max,
            terminal=terminal,
        )

        try:
            volts = self._acquire_volts(config)
        except KeyboardInterrupt:
            # Re-raised so main() can stop the stage and the scope too, rather
            # than this driver swallowing the interrupt and the scan carrying
            # on with the user's finger still on Ctrl+C.
            raise
        except Exception as e:
            print(f"[ERROR] {self.name} acquisition failed: {e}")
            return False

        if volts is None:
            return False

        if len(volts) != n_samples:
            print(
                f"[ERROR] {self.name} returned {len(volts)} of {n_samples} "
                f"samples; discarding the trace rather than writing a short "
                f"block."
            )
            return False

        self._queue_trace(output_filename, volts, is_first_trace)
        return True

    def _queue_trace(self, output_filename, volts, is_first_trace):
        """Pack volts as float32 and hand the bytes to the writer thread.

        THE one place the .d format is decided. The astype/tobytes happens on
        the calling thread, so the bytes handed to the queue are already
        independent of whatever buffer the driver filled - a driver may reuse
        its read buffer for the next trace without copying it first.
        """
        packed = np.asarray(volts, dtype=np.float32).tobytes()
        mode = "wb" if is_first_trace else "ab"
        self._writer.put(output_filename, mode, packed)

    def write_placeholder_trace(self, output_filename, n_samples):
        """Append a NaN-filled block so a failed trace does not shift the file.

        NaN rather than zero, so padding is obvious in MATLAB instead of
        looking like a real measurement of nothing.
        """
        blank = np.full(n_samples, np.nan, dtype=np.float32)
        self._writer.put(output_filename, "ab", blank.tobytes())
        return True

    def _acquire_volts(self, config):
        """Acquire one trace, returning a numpy array of volts, or None.

        Drivers implement this and nothing about the output file. The returned
        array may be a reused internal buffer; see _queue_trace().
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # d2a - applying an analog voltage to hardware
    # ------------------------------------------------------------------

    def write_d2a(self, channel, volts):
        """Apply a DC voltage to an analog output channel.

        Clamping to the card's range happens here so every driver reports an
        out-of-range request the same way.
        """
        if not self._connected:
            print(f"[ERROR] D2A backend '{self.name}' is not connected.")
            return False

        safe, was_clamped = clamp_volts(volts, self.ao_ranges())
        if was_clamped:
            print(
                f"[WARNING] {self.name} AO{channel}: {volts:.4f}V is outside "
                f"the card's output range; clamping to {safe:.4f}V."
            )

        try:
            ok = self._write_ao(channel, safe)
        except Exception as e:
            print(f"[ERROR] {self.name} AO{channel} write failed: {e}")
            return False

        if ok:
            self._last_ao[channel] = safe
        return ok

    #: Does an analog output keep its voltage once this process exits?
    #:
    #: True for most backends, and the behaviour pscan wants: a run ending
    #: must not slam a mirror or a piezo back through its range. The Phidget
    #: path has always worked this way.
    #:
    #: False only where the platform takes it away regardless - the X Series
    #: backend reaches the card through vfio-pci, which resets the device
    #: when its last file descriptor closes, zeroing the DACs. That is
    #: deliberate kernel behaviour and cannot be opted out of.
    #:
    #: Declared because 'pydaq set' used to tell everyone "the output resets
    #: as the card closes", which was written for the X Series path and is
    #: simply untrue under NI-DAQmx, where the voltage stays up.
    outputs_survive_close = True

    def _write_ao(self, channel, volts):
        raise NotImplementedError

    def read_d2a(self, channel):
        """The voltage an output is holding, or None if genuinely unknown.

        Most cards cannot be asked, so the default answer is the last value
        this session commanded. None means nothing has been written and the
        card cannot be read - callers print that rather than guessing zero. A
        piezo parked at 2V reported as 0V is how you drive it into a stop.
        """
        return self._last_ao.get(channel)

    def read_d2a_is_measured(self):
        """True when read_d2a() queries the hardware instead of recalling."""
        return False

    def __repr__(self):
        state = "connected" if self._connected else "closed"
        return f"<{type(self).__name__} {self.address} ({state})>"


class PhidgetD2A(DAQBackend):
    """The Phidget analog board, behind the d2a interface.

    An adapter, not a driver: pscan/d2aphidget_driver.py is the one and only
    Phidget binding and stays that way. This exists so `action d2a` can target
    that board or a PCIe-6351's AO without the scan loop caring which.

    The trick that makes it thin is um_per_volt=1.0, which turns the driver's
    set_position_um() into a plain volts setter while leaving its 0..5V piezo
    clamp and its 0.05s settle exactly as they are.

    That 0..5V window is deliberate and is NOT the board's full -10..+10V: it
    was chosen to protect a 100um piezo, and widening it to what the hardware
    can do would let a mistyped coordinate drive the piezo past its travel.

    Only channel 0 is reachable, because that is what the driver hardcodes.
    Multi-channel support means editing the driver, which is the right place
    for it.
    """

    name = "phidget"

    #: The piezo-safe window the driver enforces.
    AO_RANGE = (0.0, 5.0)

    def __init__(self, address="phidget0"):
        super().__init__(address=address)
        self._driver = None

    @classmethod
    def available(cls):
        import ctypes
        try:
            ctypes.CDLL("libphidget21.so")
            return True
        except OSError:
            return False

    @classmethod
    def unavailable_reason(cls):
        return ("libphidget21.so could not be loaded. Run 'bash install.sh' "
                "once to build and install it.")

    @classmethod
    def discoverable(cls):
        return False

    @classmethod
    def enumerate(cls):
        """Report the board without opening it. See discoverable()."""
        if not cls.available():
            return []
        return [DeviceInfo(
            backend=cls.name,
            address="phidget0",
            model="PhidgetAnalog (not probed)",
            n_ao=1,
            ao_ranges=(cls.AO_RANGE,),
        )]

    def connect(self):
        if self._connected:
            return

        from pscan.d2aphidget_driver import D2APhidgetDriver

        # um_per_volt=1.0 makes set_position_um() a volts pass-through. The
        # driver connects in its constructor, as it always has.
        self._driver = D2APhidgetDriver(um_per_volt=1.0)
        self._connected = True

    def _close_device(self):
        if self._driver is not None:
            # Leaves the output energized - Phidget21's own behaviour, and the
            # right one: a run ending should not drop the piezo to 0V.
            self._driver.close()
            self._driver = None

    def describe(self):
        return DeviceInfo(
            backend=self.name,
            address=self.address,
            model="PhidgetAnalog",
            n_ao=1,
            ao_ranges=(self.AO_RANGE,),
        )

    def _write_ao(self, channel, volts):
        if channel != 0:
            raise RuntimeError(
                "the Phidget driver drives channel 0 only; 'channel "
                f"{channel}' needs multi-channel support adding to "
                "pscan/d2aphidget_driver.py"
            )
        return self._driver.set_position_um(volts)

    def read_d2a(self, channel):
        """Ask the board what it is actually holding.

        The Phidget is the one card here that can be read back, which is why
        the driver can report the piezo's state at startup without moving it.
        Falls back to the last commanded value if the query fails.
        """
        if self._connected and channel == 0:
            import ctypes
            try:
                value = ctypes.c_double()
                if self._driver.phidget21.CPhidgetAnalog_getVoltage(
                    self._driver.handle, channel, ctypes.byref(value)
                ) == 0:
                    return float(value.value)
            except Exception:
                pass
        return self._last_ao.get(channel)

    def read_d2a_is_measured(self):
        return self._connected

    def _acquire_volts(self, config):
        raise RuntimeError(
            "the Phidget analog board has no analog input; an 'action a2d' "
            "block cannot use the phidget backend"
        )


def register_backend(cls):
    """Add a backend that is not shipped in pscan/ - i.e. the tests' fake card.

    Kept separate from _BACKEND_SPECS on purpose. A simulated card that could
    be auto-selected in a real scan would produce a full set of plausible .d
    files containing nothing but synthesised data, which is the worst failure
    mode this program has.
    """
    _registered[cls.name] = cls
    return cls


def unregister_backend(name):
    """Remove a registered backend and close anything still open on it."""
    _registered.pop(name, None)
    for key in [k for k in _sessions if k[0] == name]:
        session = _sessions.pop(key)
        try:
            session.disconnect()
        except Exception:
            pass


def backend_names():
    """Every backend name usable right now, including registered ones."""
    return tuple(sorted(set(BACKEND_NAMES) | set(_registered)))


def backend_class(name):
    """The class for one backend name, imported on demand."""
    if name in _registered:
        return _registered[name]

    try:
        module_name, attr = _BACKEND_SPECS[name]
    except KeyError:
        raise DaqSelectionError(
            f"unknown backend '{name}'. Known backends: "
            f"{', '.join(backend_names())}"
        ) from None

    if module_name == __name__:
        # PhidgetD2A is an adapter and lives in this module, so there is
        # nothing to import for it.
        return globals()[attr]
    return getattr(importlib.import_module(module_name), attr)


def available_backends():
    """{name: reason} per backend, 'available' when usable.

    "No cards found" and "the driver isn't installed" need completely
    different responses and look identical from the outside, so `pydaq
    list` prints this.
    """
    status = {}
    for name in backend_names():
        cls = backend_class(name)
        status[name] = ("available" if cls.available()
                        else cls.unavailable_reason())
    return status


def discover(need_ai=False, need_ao=False, include_hidden=False):
    """Every card that could serve the requested role.

    `include_hidden` adds the backends auto-detection skips - the Phidget
    board and any registered fake card. That is what the CLI wants when
    listing everything, and what auto-detection must never do.
    """
    found = []
    for name in backend_names():
        cls = backend_class(name)
        if not include_hidden and not cls.discoverable():
            continue
        if not cls.available():
            continue
        try:
            found.extend(cls.enumerate())
        except Exception as e:
            print(f"[WARNING] Enumerating the {name} backend failed: {e}")

    if need_ai:
        found = [d for d in found if d.n_ai > 0]
    if need_ao:
        found = [d for d in found if d.n_ao > 0]
    return found


def format_candidates(devices, role):
    """Render candidate cards plus the config lines that pin one down."""
    lines = []
    for info in devices:
        lines.append(f"    {info.summary()}")
        lines.append(f"        -> add to the 'action {role}' block:  "
                     f"backend {info.backend}   /   device {info.address}")
    return "\n".join(lines)


def select_device(backend=None, address=None, need_ai=False, need_ao=False,
                  role='a2d'):
    """Settle on (backend class, address) for one config block.

    Naming both a backend and a device is taken at face value and not verified
    here: connect() fails more informatively than a pre-flight enumeration
    could, and it means a card enumeration cannot describe is still usable.
    """
    if backend is not None:
        cls = backend_class(backend)

        if not cls.available():
            raise DaqSelectionError(
                f"the '{backend}' backend was requested but is not usable: "
                f"{cls.unavailable_reason()}"
            )

        if address is not None:
            return cls, address

        if not cls.discoverable():
            return cls, DEFAULT_ADDRESS.get(backend)

        candidates = [
            d for d in cls.enumerate()
            if (not need_ai or d.n_ai > 0) and (not need_ao or d.n_ao > 0)
        ]
        if len(candidates) == 1:
            return cls, candidates[0].address
        if not candidates:
            # Fall back to the default node rather than refusing: enumeration
            # can come up empty on a card it cannot describe, and connect()
            # gives the better error.
            return cls, DEFAULT_ADDRESS.get(backend)

        raise DaqSelectionError(
            f"the '{backend}' backend sees {len(candidates)} cards that could "
            f"serve this 'action {role}' block, and no 'device' line said "
            f"which:\n{format_candidates(candidates, role)}"
        )

    # Auto. An address alone is enough to infer the backend, because comedi
    # addresses are device nodes and DAQmx addresses are not.
    if address is not None:
        inferred = 'comedi' if str(address).startswith('/dev/') else 'daqmx'
        return select_device(
            backend=inferred, address=address,
            need_ai=need_ai, need_ao=need_ao, role=role,
        )

    candidates = discover(need_ai=need_ai, need_ao=need_ao)

    if not candidates:
        detail = "\n".join(
            f"    {name:<8} {reason}"
            for name, reason in sorted(available_backends().items())
            if backend_class(name).discoverable()
        )
        wanted = "analog input" if need_ai else "analog output"
        raise DaqSelectionError(
            f"no card with {wanted} was found for this 'action {role}' "
            f"block.\n  Backend status:\n{detail}\n"
            f"  Run 'pydaq list' to see everything pscan can find."
        )

    if len(candidates) == 1:
        info = candidates[0]
        return backend_class(info.backend), info.address

    raise DaqSelectionError(
        f"{len(candidates)} cards could serve this 'action {role}' block, so "
        f"pscan will not pick one for you - the wrong choice produces a full "
        f"set of plausible files containing the wrong signal.\n"
        f"{format_candidates(candidates, role)}"
    )


def open_device(backend=None, address=None, need_ai=False, need_ao=False,
                role='a2d', announce=True):
    """Return a connected session for the card this block should use.

    Sessions are cached per (backend, address) for the life of the process, so
    a config with several a2d and d2a blocks on one card opens it once.
    """
    cls, resolved = select_device(
        backend=backend, address=address,
        need_ai=need_ai, need_ao=need_ao, role=role,
    )

    # Keyed on what was asked for, so a later block asking the same question
    # finds this session.
    request_key = (cls.name, resolved)
    session = _sessions.get(request_key)
    if session is not None:
        return session

    session = cls(resolved) if resolved is not None else cls()
    session.connect()

    # Registered under BOTH keys, deliberately. A backend may resolve its own
    # address during connect() - daqmx picking the only device it can see, or
    # a driver defaulting its node - so the address it ends up with is not the
    # one the lookup asked about, and `resolved` is often None.
    #
    # Getting this wrong is not a cosmetic bug: a second session means a
    # second writer thread, both stored under the same resolved key so only
    # the last survives in _sessions, and close_all() then flushes only that
    # one. The earlier sessions' queued traces are lost when the process
    # exits, because the writer threads are daemons. A scan that looked like
    # it ran would produce a near-empty .d file.
    _sessions[request_key] = session
    _sessions[(cls.name, session.address)] = session

    if announce:
        try:
            print(f" -> DAQ: {session.describe().summary()}")
        except Exception:
            print(f" -> DAQ: {cls.name} {session.address}")

    return session


def close_all():
    """Disconnect every open session. Safe to call more than once.

    Deduplicated by identity, because open_device() registers each session
    under two keys and disconnecting one twice would flush an already-closed
    writer.
    """
    errors = []
    seen = set()

    for key, session in list(_sessions.items()):
        _sessions.pop(key, None)
        if id(session) in seen:
            continue
        seen.add(id(session))

        try:
            session.disconnect()
        except Exception as e:
            errors.append(f"{key[0]} {key[1]}: {e}")

    for message in errors:
        print(f"[WARNING] DAQ disconnect failed for {message}")


def open_sessions():
    """The distinct sessions currently open, for teardown reporting."""
    unique = {}
    for session in _sessions.values():
        unique[id(session)] = session
    return unique
