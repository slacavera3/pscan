"""Backend-neutral plumbing shared by every DAQ card pscan can talk to.

pscan grew up against one card reached through libcomedi, so "acquire a trace"
and "drive comedi" were the same code. They are not the same thing, and the
difference now matters: comedi's ni_pcimio driver covers the NI E and M series
and stops there. An X Series board such as the PCIe-6351 has no comedi driver
at all, so a second path to the hardware is not optional.

What lives here is everything that is about acquiring and recording a trace
rather than about any particular driver:

  * the async disk writer, so a trace lands on disk off the acquisition thread
  * the NaN placeholder block, so a failed trace does not shift the file
  * range resolution, which reconciles comedi's range *indices* against
    DAQmx's explicit volts so a .con file can say either
  * the float32-volts output format

That last one is the hard constraint. A .d file written through DAQmx has to
be byte-identical in layout to one written through comedi, because the MATLAB
side downstream has no idea which backend produced it and must not need to.
Backends therefore never touch the output file: they hand back volts and this
module packs and queues them.
"""

import queue
import threading
from dataclasses import dataclass, field
from typing import Optional, Sequence, Tuple

import numpy as np


# How a single-ended input is wired. comedi calls these analog references and
# DAQmx calls them terminal configurations; they mean the same four things, so
# the .con file names them once and each backend maps them to its own constant.
TERMINALS = ('rse', 'nrse', 'diff', 'pseudodiff')

# What the comedi path did unconditionally before any of this existed, kept as
# the default so an existing .con file acquires exactly as it used to.
DEFAULT_TERMINAL = 'rse'


@dataclass(frozen=True)
class DeviceInfo:
    """What a backend can say about one card without acquiring from it.

    Printed by `pydaq-dev list` and at connect time, so a run's log records
    which card and which driver actually produced the data. That line is the
    difference between a confusing .d file and an obvious one.
    """
    backend: str
    address: str
    model: str
    n_ai: int = 0
    n_ao: int = 0
    ai_ranges: Tuple[Tuple[float, float], ...] = ()
    ao_ranges: Tuple[Tuple[float, float], ...] = ()
    max_ai_rate: Optional[float] = None

    def selector(self):
        """The two .con lines that pin a config to exactly this card."""
        return f"backend {self.backend}\ndevice {self.address}"

    def summary(self):
        parts = [f"{self.backend:<8} {self.address:<14} {self.model}"]
        if self.n_ai:
            rng = _format_ranges(self.ai_ranges)
            rate = (f", {self.max_ai_rate / 1e6:g} MS/s"
                    if self.max_ai_rate else "")
            parts.append(f"{self.n_ai} AI ({rng}{rate})")
        if self.n_ao:
            parts.append(f"{self.n_ao} AO ({_format_ranges(self.ao_ranges)})")
        return "  |  ".join(parts)


def _format_ranges(ranges):
    if not ranges:
        return "ranges unknown"
    return ", ".join(
        f"{lo:g}..{hi:g}V" if lo != -hi else f"+/-{hi:g}V"
        for lo, hi in ranges
    )


@dataclass(frozen=True)
class A2DConfig:
    """One a2d block's acquisition settings, in backend-neutral terms.

    Frozen and hashable on purpose: backends key their cached hardware setup
    on it, so re-acquiring with identical settings - which is what every trace
    after the first in a scan does - skips re-negotiating with the card.
    """
    channel: int
    n_samples: int
    sample_rate: float
    range_index: Optional[int] = None
    v_min: Optional[float] = None
    v_max: Optional[float] = None
    terminal: str = DEFAULT_TERMINAL


class AsyncWriter:
    """Background disk writer, one thread per backend session.

    Lifted unchanged in behaviour from the comedi driver, where it exists so a
    trace's disk write does not sit between the acquisition returning and the
    stage being told to move. Queueing costs microseconds; the write happens
    while the stage is already travelling to the next coordinate.

    ctypes releases the GIL around the driver call, so the acquisition thread
    and this one genuinely overlap.
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
        """Flush and stop. Called before the hardware is closed, never after.

        Pending writes hold references to buffers the driver may own, so they
        have to land while the device session is still alive.
        """
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)


def resolve_range(ranges, range_index=None, v_min=None, v_max=None):
    """Pick the input range to use, from an index or from explicit volts.

    `ranges` is the card's own range list, in the card's own order, as
    (low, high) volt pairs. comedi indexes that list natively and DAQmx does
    not index at all, so resolving to (index, low, high) here is what lets one
    .con file mean the same thing on both.

    Explicit volts win over an index, and select the *narrowest* range that
    still contains the request - for a photodiode a tighter range is more
    resolution, so "I need +/-2V" should not land on the +/-10V setting. Giving
    only one side mirrors it, so `v_max 2` means -2..2.

    Returns (index, low, high). Raises ValueError when the request cannot be
    satisfied, which is worth failing over: silently widening the range would
    quietly cost the caller bits of resolution they asked for.
    """
    if not ranges:
        raise ValueError("the card reported no input ranges")

    if v_min is None and v_max is not None:
        v_min = -abs(v_max)
    elif v_max is None and v_min is not None:
        v_max = abs(v_min)

    if v_min is not None and v_max is not None:
        if v_max <= v_min:
            raise ValueError(
                f"v_min {v_min:g} is not below v_max {v_max:g}"
            )

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
            f"{len(ranges)} range(s) ({_format_ranges(ranges)}). Use an index "
            f"in 0..{len(ranges) - 1}, or say the volts you want with "
            f"'v_min'/'v_max' and let pscan pick."
        )

    lo, hi = ranges[range_index]
    return range_index, lo, hi


def clamp_volts(volts, ranges, label="output"):
    """Hold a requested output voltage inside what the card can actually do.

    Returns (clamped, was_clamped). Driving a mirror or a piezo past its
    range is the kind of mistake that is cheap to catch here and expensive to
    catch on the bench, so the caller is expected to report was_clamped.
    """
    if not ranges:
        return volts, False

    lo = min(r[0] for r in ranges)
    hi = max(r[1] for r in ranges)
    clamped = max(lo, min(hi, volts))
    return clamped, clamped != volts


class DAQBackend:
    """One open session with one card.

    Subclasses implement discovery, connect/disconnect, `_acquire_volts()` and
    `_write_ao()`; everything else - the output file format, the placeholder
    block, range resolution, warn-once - is settled here so the backends
    cannot drift apart on it.

    A session is shared between the a2d and d2a sides of the same card. The
    PCIe-6351 has 16 AI and 2 AO on one board, so a scan that reads a
    photodiode and drives a mirror from it must not open the device twice.
    """

    #: Short name used in the .con file's `backend` line.
    name = "base"

    def __init__(self, address=None):
        self.address = address
        self._writer = AsyncWriter()
        self._connected = False
        self._warned = set()
        # Last voltage commanded per AO channel. Most cards cannot read their
        # own output back, so this is what read_d2a() reports by default - it
        # is what we *told* the card, which is enough for a relative step and
        # is labelled as such wherever it is printed.
        self._last_ao = {}

    # ------------------------------------------------------------------
    # Discovery. Classmethods, because these answer "what is on this
    # machine?" and must not require a session to already exist.
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        """True when this backend's library is present and loadable."""
        return False

    @classmethod
    def unavailable_reason(cls):
        """Why `available()` is False, phrased for a user to act on."""
        return f"the {cls.name} backend is not implemented"

    @classmethod
    def enumerate(cls):
        """Every card this backend can see, as DeviceInfo. Never raises."""
        return []

    @classmethod
    def discoverable(cls):
        """Whether auto-detection may enumerate this backend unasked.

        False for backends whose enumeration is intrusive. Opening a Phidget
        analog board claims it and can nudge its outputs, and the board may
        currently be holding a piezo at a position - not something to do while
        merely answering "what hardware is here?". Those backends are reachable
        only by being named explicitly.
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

    # ------------------------------------------------------------------
    # Shared reporting
    # ------------------------------------------------------------------

    def _warn_once(self, key, message):
        """Print a warning once per session, not once per trace.

        A scan is thousands of traces. A per-trace warning about a calibration
        query is noise that buries everything else in the log.
        """
        if key not in self._warned:
            self._warned.add(key)
            print(f"\n[WARNING] {self.name.upper()}: {message}")

    # ------------------------------------------------------------------
    # a2d
    # ------------------------------------------------------------------

    def acquire_a2d(self, channel, range_val=None, n_samples=100,
                    sample_rate=10000, output_filename=None,
                    is_first_trace=True, v_min=None, v_max=None,
                    terminal=DEFAULT_TERMINAL):
        """Acquire one trace and queue it to disk as float32 volts.

        The signature keeps `range_val`'s name and position from the original
        comedi driver so main.py and the dev/ timing scripts call this
        unchanged.

        Returns True when a trace was recorded. False means nothing was
        written, and the caller is responsible for the placeholder block -
        skipping it would leave the .d file one trace short and silently
        misalign every later trace against its coordinate.
        """
        if not self._connected:
            print(f"[ERROR] A2D Driver not connected. Call connect() first.")
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
            # than this backend swallowing the interrupt and the scan carrying
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

        Every backend's output goes through here, so the .d layout cannot
        depend on which driver produced it.
        """
        packed = np.asarray(volts, dtype=np.float32).tobytes()
        mode = "wb" if is_first_trace else "ab"
        self._writer.put(output_filename, mode, packed)

    def write_placeholder_trace(self, output_filename, n_samples):
        """Append a NaN-filled block so a failed trace does not shift the file.

        NaN rather than zero so a padded block is obvious in MATLAB instead of
        looking like a real measurement of nothing.
        """
        blank = np.full(n_samples, np.nan, dtype=np.float32)
        self._writer.put(output_filename, "ab", blank.tobytes())
        return True

    def _acquire_volts(self, config):
        """Acquire one trace, returning a numpy array of volts, or None.

        Backends implement this and nothing about the output file.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def write_d2a(self, channel, volts):
        """Apply a DC voltage to an analog output channel.

        Returns True on success. Clamping to the card's range happens here so
        every backend reports an out-of-range request the same way.
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

    def _write_ao(self, channel, volts):
        raise NotImplementedError

    def read_d2a(self, channel):
        """The voltage an output channel is holding, or None if unknown.

        Most cards cannot read their own output back, so the default answer is
        the last value this session commanded. None means genuinely unknown -
        nothing has been written yet and the card cannot be asked - and
        callers print it as such rather than guessing zero. A piezo parked at
        2V reported as 0V is how you drive it into a hard stop.
        """
        return self._last_ao.get(channel)

    def read_d2a_is_measured(self):
        """True when read_d2a() queries the hardware instead of recalling."""
        return False

    def __repr__(self):
        state = "connected" if self._connected else "closed"
        return f"<{type(self).__name__} {self.address} ({state})>"
