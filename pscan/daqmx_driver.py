"""NI-DAQmx cards: the PCIe-6351 and anything else comedi cannot reach.

comedi's ni_pcimio driver covers the NI E and M series and no X Series board,
so this is the only route to a PCIe-6351 on Linux - and will be the only route
to essentially every NI card bought from here on. It also drives the M Series
cards comedi handles (a PCIe-6251 is reachable either way), which is worth
knowing: it means one driver can cover a mixed rig if you pin the blocks.

Performance notes, because this runs once per coordinate and a scan is
thousands of coordinates:

  * The task is built once per acquisition setting and then COMMITTED.
    DAQmx's verify/reserve/program work is what makes task setup expensive -
    tens of milliseconds - and committing does it up front, leaving start() as
    little more than a register write. A naive implementation that builds a
    Task per trace pays that cost thousands of times; that, not the choice of
    Python binding, is what would make this slower than the comedi path.

  * Samples are read through a stream reader into a preallocated float64
    array. task.read() returns a Python list, which is N float objects per
    trace - unnoticeable at 100 samples, expensive at 100k. The buffer is
    handed straight back to pscan.daq, which packs it to float32 on this
    thread before the next trace can touch it, so no defensive copy is made.

  * No DAQmx property is read inside the acquisition loop. Property access on
    a task is a real Get call into the driver, so everything needed per trace
    is resolved at setup and cached.

nidaqmx-python is itself a ctypes wrapper over the same libnidaqmx, so this
costs nothing in call overhead against hand-rolled ctypes; it just saves
owning the task and error machinery. It is imported lazily because a rig with
only comedi cards has no reason to have it installed, and a module-level
import would break those rigs outright.
"""

import numpy as np

from pscan.daq import DAQBackend, DeviceInfo, resolve_range

# Populated by _load(). Module-level so repeated calls are free and so
# enumeration does not need a session.
_nidaqmx = None
_constants = None
_stream_readers = None
_load_error = None

#: Spellings each terminal configuration has had across nidaqmx releases.
#: Resolved per terminal rather than all at once, so a version that renamed
#: one does not break the others - asking for 'rse' must not fail because
#: 'pseudodiff' could not be found.
_TERMINAL_ALIASES = {
    'rse': ('RSE',),
    'nrse': ('NRSE',),
    'diff': ('DIFF', 'DIFFERENTIAL'),
    'pseudodiff': ('PSEUDO_DIFF', 'PSEUDO_DIFFERENTIAL', 'PSEUDODIFFERENTIAL'),
}


def _load():
    """Import nidaqmx on first use, remembering why it failed if it did.

    Two distinct failures look the same from outside and need different fixes:
    the Python package being absent (pip) and the NI-DAQmx driver itself being
    absent (NI's repository, plus a kernel module build). The error text
    distinguishes them.
    """
    global _nidaqmx, _constants, _stream_readers, _load_error

    if _nidaqmx is not None:
        return _nidaqmx
    if _load_error is not None:
        raise RuntimeError(_load_error)

    try:
        import nidaqmx
        from nidaqmx import constants, stream_readers
    except ImportError as e:
        _load_error = (
            "the 'nidaqmx' Python package is not installed. Re-run "
            "'bash install.sh', which installs it "
            "once the NI-DAQmx driver is present."
        )
        raise RuntimeError(_load_error) from e
    except Exception as e:
        # Not just OSError: depending on the version, nidaqmx raises its own
        # DaqNotFoundError at import time when the C library is absent. The
        # base class means a missing driver gives the message below rather
        # than an unhandled exception from inside the package.
        _load_error = (
            f"the 'nidaqmx' package is installed but the NI-DAQmx driver "
            f"library could not be loaded ({type(e).__name__}: {e}). Run "
            f"'bash install.sh' to install the driver, then reboot."
        )
        raise RuntimeError(_load_error) from e

    _nidaqmx = nidaqmx
    _constants = constants
    _stream_readers = stream_readers
    return _nidaqmx


def _terminal_constant(terminal):
    """The DAQmx constant for one terminal configuration."""
    config = _constants.TerminalConfiguration
    for alias in _TERMINAL_ALIASES[terminal]:
        value = getattr(config, alias, None)
        if value is not None:
            return value

    raise RuntimeError(
        f"this version of nidaqmx has no terminal configuration matching "
        f"'{terminal}' (looked for "
        f"{', '.join(_TERMINAL_ALIASES[terminal])}). Use one of the others, "
        f"or upgrade the nidaqmx package."
    )


def _pair_ranges(flat):
    """DAQmx reports ranges as a flat [min, max, min, max, ...] list."""
    values = list(flat or ())
    # Left in the card's own order, deliberately: resolve_range() does not
    # care, but `range <index>` in a .con file indexes this list, so sorting
    # it would silently change what an existing index means.
    return tuple(
        (float(values[i]), float(values[i + 1]))
        for i in range(0, len(values) - 1, 2)
    )


def _describe(dev):
    """DeviceInfo for one nidaqmx Device. Read once, never per trace."""
    def safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    return DeviceInfo(
        backend=DaqmxDriver.name,
        address=dev.name,
        model=safe(lambda: dev.product_type, "unknown"),
        n_ai=len(safe(lambda: dev.ai_physical_chans, []) or []),
        n_ao=len(safe(lambda: dev.ao_physical_chans, []) or []),
        ai_ranges=_pair_ranges(safe(lambda: dev.ai_voltage_rngs, [])),
        ao_ranges=_pair_ranges(safe(lambda: dev.ao_voltage_rngs, [])),
        max_ai_rate=safe(lambda: float(dev.ai_max_single_chan_rate)),
    )


class _A2DTask:
    """A committed DAQmx input task plus the buffer its reader fills."""

    def __init__(self, task, reader, buffer, timeout):
        self.task = task
        self.reader = reader
        self.buffer = buffer
        self.timeout = timeout

    def close(self):
        try:
            self.task.close()
        except Exception:
            pass


class DaqmxDriver(DAQBackend):
    name = "daqmx"

    def __init__(self, address=None):
        super().__init__(address=address)
        self._info = None
        self._tasks = {}      # A2DConfig -> _A2DTask
        self._ao_tasks = {}   # channel -> nidaqmx.Task

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        try:
            _load()
            return True
        except RuntimeError:
            return False

    @classmethod
    def unavailable_reason(cls):
        try:
            _load()
        except RuntimeError as e:
            return str(e)
        return "available"

    @classmethod
    def enumerate(cls):
        try:
            nidaqmx = _load()
        except RuntimeError:
            return []

        try:
            system = nidaqmx.system.System.local()
            return [_describe(dev) for dev in system.devices]
        except Exception:
            # A driver installed but not functioning - usually the kernel
            # modules not rebuilt after a kernel upgrade - must not take
            # enumeration of the other backends down with it.
            return []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return

        nidaqmx = _load()
        system = nidaqmx.system.System.local()
        names = [dev.name for dev in system.devices]

        if not names:
            raise RuntimeError(
                "NI-DAQmx sees no devices. Check the card is seated and that "
                "the driver's kernel modules are built for the running kernel "
                "('sudo dkms status'). X Series boards on kernels newer than "
                "an LTS release may also need the 'iommu=pt' kernel parameter."
            )

        if self.address is None:
            if len(names) > 1:
                raise RuntimeError(
                    f"NI-DAQmx sees {len(names)} devices ({', '.join(names)}) "
                    f"and no 'device' line said which to use."
                )
            self.address = names[0]

        if self.address not in names:
            raise RuntimeError(
                f"NI-DAQmx has no device named '{self.address}'. It sees: "
                f"{', '.join(names)}. Device names come from NI MAX / nidaqmx "
                f"and are not the /dev nodes comedi uses."
            )

        self._info = _describe(system.devices[self.address])
        self._connected = True

    def _close_device(self):
        for task in self._tasks.values():
            task.close()
        self._tasks.clear()

        # Output tasks are closed but the voltage is deliberately not zeroed.
        # DAQmx leaves the DAC holding its last value, matching what the
        # Phidget piezo path has always done: a scan ending should not slam a
        # mirror or a piezo back to 0V.
        for task in self._ao_tasks.values():
            try:
                task.close()
            except Exception:
                pass
        self._ao_tasks.clear()

    def describe(self):
        if self._info is None:
            raise RuntimeError("daqmx session is not connected")
        return self._info

    # ------------------------------------------------------------------
    # a2d
    # ------------------------------------------------------------------

    def _build_task(self, config):
        nidaqmx = _load()

        _index, lo, hi = resolve_range(
            self._info.ai_ranges,
            range_index=config.range_index,
            v_min=config.v_min,
            v_max=config.v_max,
        )

        task = nidaqmx.Task()
        try:
            task.ai_channels.add_ai_voltage_chan(
                f"{self.address}/ai{config.channel}",
                min_val=lo,
                max_val=hi,
                terminal_config=_terminal_constant(config.terminal),
            )
            task.timing.cfg_samp_clk_timing(
                rate=float(config.sample_rate),
                sample_mode=_constants.AcquisitionType.FINITE,
                samps_per_chan=int(config.n_samples),
            )
            # The whole point: do the verify/reserve/program work now, once,
            # so every start() during the scan is cheap.
            task.control(_constants.TaskMode.TASK_COMMIT)
        except Exception:
            task.close()
            raise

        reader = _stream_readers.AnalogSingleChannelReader(task.in_stream)
        buffer = np.zeros(config.n_samples, dtype=np.float64)

        # The acquisition takes n/rate seconds; the margin covers a trace
        # waiting on an external clock or a slow start. Floor of 5s so a very
        # short trace still gets a sane timeout rather than a millisecond one.
        physical = config.n_samples / float(config.sample_rate or 1)
        timeout = max(5.0, 2.0 * physical + 1.0)

        return _A2DTask(task, reader, buffer, timeout)

    def _ensure_task(self, config):
        task = self._tasks.get(config)
        if task is None:
            task = self._build_task(config)
            self._tasks[config] = task
        return task

    def _acquire_volts(self, config):
        entry = self._ensure_task(config)

        entry.task.start()
        try:
            entry.reader.read_many_sample(
                entry.buffer,
                number_of_samples_per_channel=config.n_samples,
                timeout=entry.timeout,
            )
        except KeyboardInterrupt:
            print("\n[WARNING] Keyboard Interrupt detected! Stopping DAQmx task...")
            raise
        except Exception as e:
            # A timeout here is the DAQmx analogue of the comedi DMA timeout:
            # report it and let the caller write a placeholder, rather than
            # letting a short or absent trace reach the .d file.
            print(f"[ERROR] DAQmx read failed: {e}")
            return None
        finally:
            try:
                entry.task.stop()
            except Exception:
                pass

        # Returned without copying. pscan.daq packs it to float32 on this
        # thread before returning, so the buffer is free to be reused by the
        # next trace.
        return entry.buffer

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def _ensure_ao_task(self, channel):
        task = self._ao_tasks.get(channel)
        if task is not None:
            return task

        nidaqmx = _load()
        ranges = self._info.ao_ranges
        if not ranges:
            raise RuntimeError(f"{self.address} reports no analog output ranges")

        lo = min(r[0] for r in ranges)
        hi = max(r[1] for r in ranges)

        task = nidaqmx.Task()
        try:
            task.ao_channels.add_ao_voltage_chan(
                f"{self.address}/ao{channel}", min_val=lo, max_val=hi
            )
            # No timing configured, so this is an on-demand task: each write
            # updates the DAC immediately and the output holds that value.
            task.start()
        except Exception:
            task.close()
            raise

        self._ao_tasks[channel] = task
        return task

    def _write_ao(self, channel, volts):
        self._ensure_ao_task(channel).write(float(volts))
        return True
