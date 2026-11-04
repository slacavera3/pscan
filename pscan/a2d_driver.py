"""The comedi card: DC photovoltage off a detector, and analog out.

The incumbent, and still the driver for every card already in service. The
acquisition sequence below is the one that was already running - the same
generic-timed template, the same four command_test passes, the same
select/read loop, the same calibration queries. Behaviour is unchanged except
for two things that cannot alter the data:

  * The negotiated command struct is cached per acquisition setting. A scan
    re-acquires with identical settings thousands of times, and every one of
    those was re-running comedi_get_cmd_generic_timed plus four
    comedi_command_test passes - a handful of ioctls - to arrive at the same
    struct. The negotiation is deterministic for a given setting, so the
    result is reused and only comedi_command is re-issued.

  * The read loop accumulates into a bytearray instead of rebinding a bytes
    object. `raw_bytes += chunk` copies everything read so far on every chunk,
    which is quadratic in trace length: invisible at 1000 samples, real at
    100k.

comedi is a dead end for new NI hardware - its ni_pcimio driver covers the E
and M series and no X Series board, so a PCIe-6351 is invisible to it. Nothing
is wrong with it for the cards it does support, and those are still in use.
For anything it cannot reach, see pscan/daqmx_driver.py.

`NIDriver` keeps its original name and byte-string constructor, so the dev/
timing scripts and any local experiment that imports it are unaffected.
"""

import ctypes
import ctypes.util
import os
import select

import numpy as np

from pscan.daq import DAQBackend, DeviceInfo, resolve_range

# Comedi trigger constants.
TRIG_NOW = 0x0002
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020

# Subdevice types, from comedi.h.
COMEDI_SUBD_AI = 1
COMEDI_SUBD_AO = 2

# Analog reference, from comedi.h. A .con file's terminal names map onto
# these; AREF_GROUND is what the driver used unconditionally before the
# terminal key existed, so 'rse' is the default and nothing changes.
AREF = {
    'rse': 0,         # AREF_GROUND
    'nrse': 1,        # AREF_COMMON
    'diff': 2,        # AREF_DIFF
    'pseudodiff': 3,  # AREF_OTHER
}

# Used only when the card cannot report its own calibration. These were the
# hardcoded values the conversion used unconditionally, which is wrong for any
# card whose resolution or range list differs - a 12-bit card packing samples
# into 16 bits read 16x low.
FALLBACK_MAXDATA = 65535
FALLBACK_RANGES = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}

# Device nodes probed when enumerating. comedi creates these statically, so
# probing is the only way to find out what is there.
PROBE_NODES = tuple(f"/dev/comedi{i}" for i in range(4))

_LIB_FALLBACK = '/usr/lib/x86_64-linux-gnu/libcomedi.so.0'


class comedi_range(ctypes.Structure):
    """What comedi_get_range() points at."""
    _fields_ = [
        ("min", ctypes.c_double),
        ("max", ctypes.c_double),
        ("unit", ctypes.c_uint),
    ]


class comedi_cmd_struct(ctypes.Structure):
    _fields_ = [
        ("subdev", ctypes.c_uint), ("flags", ctypes.c_uint),
        ("start_src", ctypes.c_uint), ("start_arg", ctypes.c_uint),
        ("scan_begin_src", ctypes.c_uint), ("scan_begin_arg", ctypes.c_uint),
        ("convert_src", ctypes.c_uint), ("convert_arg", ctypes.c_uint),
        ("scan_end_src", ctypes.c_uint), ("scan_end_arg", ctypes.c_uint),
        ("stop_src", ctypes.c_uint), ("stop_arg", ctypes.c_uint),
        ("chanlist", ctypes.POINTER(ctypes.c_uint)), ("chanlist_len", ctypes.c_uint),
        ("data", ctypes.POINTER(ctypes.c_short)), ("data_len", ctypes.c_uint),
    ]


def _load_libcomedi():
    """Load libcomedi and declare the signatures used here.

    Module-level rather than per-instance so enumeration can load the library
    without constructing a session.
    """
    lib_path = ctypes.util.find_library('comedi') or _LIB_FALLBACK
    lib = ctypes.CDLL(lib_path)

    lib.comedi_open.argtypes = [ctypes.c_char_p]
    lib.comedi_open.restype = ctypes.c_void_p
    lib.comedi_close.argtypes = [ctypes.c_void_p]
    lib.comedi_fileno.argtypes = [ctypes.c_void_p]
    lib.comedi_fileno.restype = ctypes.c_int

    lib.comedi_get_board_name.argtypes = [ctypes.c_void_p]
    lib.comedi_get_board_name.restype = ctypes.c_char_p

    lib.comedi_find_subdevice_by_type.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
    ]
    lib.comedi_find_subdevice_by_type.restype = ctypes.c_int

    lib.comedi_get_n_channels.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.comedi_get_n_channels.restype = ctypes.c_int

    lib.comedi_get_n_ranges.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint
    ]
    lib.comedi_get_n_ranges.restype = ctypes.c_int

    lib.comedi_get_range.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
    ]
    lib.comedi_get_range.restype = ctypes.POINTER(comedi_range)

    lib.comedi_get_maxdata.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint
    ]
    lib.comedi_get_maxdata.restype = ctypes.c_uint

    lib.comedi_get_cmd_generic_timed.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(comedi_cmd_struct),
        ctypes.c_uint, ctypes.c_uint
    ]
    lib.comedi_command_test.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)
    ]
    lib.comedi_command.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)
    ]
    lib.comedi_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.comedi_poll.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    lib.comedi_poll.restype = ctypes.c_int

    lib.comedi_data_write.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_uint
    ]
    lib.comedi_data_write.restype = ctypes.c_int

    return lib


def _read_ranges(lib, dev_ptr, subdevice, channel=0):
    """The (low, high) volt pairs a subdevice offers, in the card's order.

    An empty tuple means the query failed, which the caller handles rather
    than guessing - a wrong range list is a silently wrong voltage scale.
    """
    if subdevice < 0:
        return ()

    n = lib.comedi_get_n_ranges(dev_ptr, subdevice, channel)
    if n <= 0:
        return ()

    ranges = []
    for i in range(n):
        rng = lib.comedi_get_range(dev_ptr, subdevice, channel, i)
        if not rng:
            continue
        lo, hi = float(rng.contents.min), float(rng.contents.max)
        if hi > lo:
            ranges.append((lo, hi))
    return tuple(ranges)


def _describe_open_device(lib, dev_ptr, address):
    ai = lib.comedi_find_subdevice_by_type(dev_ptr, COMEDI_SUBD_AI, 0)
    ao = lib.comedi_find_subdevice_by_type(dev_ptr, COMEDI_SUBD_AO, 0)

    name = lib.comedi_get_board_name(dev_ptr)
    model = name.decode(errors='replace') if name else "unknown"

    return DeviceInfo(
        backend=NIDriver.name,
        address=address,
        model=model,
        n_ai=max(0, lib.comedi_get_n_channels(dev_ptr, ai)) if ai >= 0 else 0,
        n_ao=max(0, lib.comedi_get_n_channels(dev_ptr, ao)) if ao >= 0 else 0,
        ai_ranges=_read_ranges(lib, dev_ptr, ai),
        ao_ranges=_read_ranges(lib, dev_ptr, ao),
        # comedi reports timing per-command rather than as a headline figure,
        # so there is no honest single number to print here.
        max_ai_rate=None,
    )


class NIDriver(DAQBackend):
    """A comedi card. Takes either a str or the original bytes device node."""

    name = "comedi"

    def __init__(self, device_node=b"/dev/comedi0"):
        address = (device_node.decode()
                   if isinstance(device_node, bytes) else device_node)
        super().__init__(address=address)

        self.libcomedi = _load_libcomedi()
        self.dev_ptr = None
        self.fd = None
        self._info = None
        self._ai_subdev = -1
        self._ao_subdev = -1
        # A2DConfig -> (cmd struct, chanlist buffer, range index, maxdata,
        # v_min, v_max). The chanlist buffer is held because cmd.chanlist
        # points into it; dropping it would leave the struct pointing at
        # freed memory.
        self._commands = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        try:
            _load_libcomedi()
            return True
        except OSError:
            return False

    @classmethod
    def unavailable_reason(cls):
        return ("libcomedi could not be loaded. Install it with "
                "'sudo apt install -y libcomedi0 libcomedi-dev'.")

    @classmethod
    def enumerate(cls):
        try:
            lib = _load_libcomedi()
        except OSError:
            return []

        found = []
        for node in PROBE_NODES:
            if not os.path.exists(node):
                continue
            dev_ptr = lib.comedi_open(node.encode())
            if not dev_ptr:
                continue
            try:
                found.append(_describe_open_device(lib, dev_ptr, node))
            except Exception:
                # A node that answers but cannot be described is not worth
                # failing enumeration over; the others may be fine.
                pass
            finally:
                lib.comedi_close(dev_ptr)
        return found

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        if self.dev_ptr is not None:
            return

        self.dev_ptr = self.libcomedi.comedi_open(self.address.encode())
        if not self.dev_ptr:
            raise RuntimeError(
                f"Failed to open card at {self.address}. If the card is "
                f"present, this usually means no comedi driver claimed it - "
                f"NI X Series boards (PCIe-635x/636x) have no comedi driver "
                f"and need the daqmx backend."
            )

        self.fd = self.libcomedi.comedi_fileno(self.dev_ptr)
        self._ai_subdev = self.libcomedi.comedi_find_subdevice_by_type(
            self.dev_ptr, COMEDI_SUBD_AI, 0
        )
        self._ao_subdev = self.libcomedi.comedi_find_subdevice_by_type(
            self.dev_ptr, COMEDI_SUBD_AO, 0
        )
        self._info = _describe_open_device(
            self.libcomedi, self.dev_ptr, self.address
        )

        # Immediately clear any lingering command from a previously aborted run.
        if self._ai_subdev >= 0:
            self.libcomedi.comedi_cancel(self.dev_ptr, self._ai_subdev)

        self._connected = True

    def _close_device(self):
        if self.dev_ptr:
            if self._ai_subdev >= 0:
                self.libcomedi.comedi_cancel(self.dev_ptr, self._ai_subdev)
            self.libcomedi.comedi_close(self.dev_ptr)
            self.dev_ptr = None
            self.fd = None
            self._commands.clear()

    def describe(self):
        if self._info is None:
            raise RuntimeError("comedi session is not connected")
        return self._info

    # ------------------------------------------------------------------
    # Calibration
    # ------------------------------------------------------------------

    def _calibration(self, subdevice, channel, range_index):
        """Ask the card for its full-scale count and the range's voltages.

        Falls back to the hardcoded constants, with a warning, if either query
        is unavailable - so a card that does not support them still works
        exactly as it did before the queries were added.
        """
        maxdata = None
        try:
            value = self.libcomedi.comedi_get_maxdata(
                self.dev_ptr, subdevice, channel
            )
            if value > 0:
                maxdata = float(value)
        except Exception:
            pass

        v_min = v_max = None
        try:
            rng = self.libcomedi.comedi_get_range(
                self.dev_ptr, subdevice, channel, range_index
            )
            if rng:
                v_min, v_max = float(rng.contents.min), float(rng.contents.max)
        except Exception:
            pass

        if maxdata is None:
            maxdata = float(FALLBACK_MAXDATA)
            self._warn_once(
                'maxdata',
                f"could not read maxdata from the card; assuming "
                f"{FALLBACK_MAXDATA} counts at full scale. Voltages will be "
                f"wrong if this card is not 16-bit."
            )

        if v_min is None or v_max is None or v_max <= v_min:
            v_max = FALLBACK_RANGES.get(range_index, 10.0)
            v_min = -v_max
            self._warn_once(
                'range',
                f"could not read range {range_index} from the card; assuming "
                f"{v_min:g}..{v_max:g} V."
            )

        return maxdata, v_min, v_max

    @staticmethod
    def _to_volts(raw_array, maxdata, v_min, v_max):
        """comedi_to_phys()'s conversion, vectorised over a whole trace.

        Interpolating between the range's own min and max also gets unipolar
        ranges right; the original code assumed every range was symmetric
        about zero.
        """
        return v_min + (raw_array / maxdata) * (v_max - v_min)

    # ------------------------------------------------------------------
    # a2d
    # ------------------------------------------------------------------

    def _negotiate(self, config):
        """Build (and cache) the command struct for one acquisition setting."""
        cached = self._commands.get(config)
        if cached is not None:
            return cached

        if self._ai_subdev < 0:
            raise RuntimeError(
                f"{self.address} reports no analog input subdevice"
            )

        ranges = self._info.ai_ranges
        if ranges:
            range_index, v_min, v_max = resolve_range(
                ranges,
                range_index=config.range_index,
                v_min=config.v_min,
                v_max=config.v_max,
            )
        else:
            # No range list to resolve against. Honour a bare index as the old
            # code did, and let _calibration() warn about the fallback table.
            if config.v_min is not None or config.v_max is not None:
                raise RuntimeError(
                    "this card did not report its range list, so 'v_min'/"
                    "'v_max' cannot be resolved. Use a bare 'range <index>'."
                )
            range_index = config.range_index or 0
            v_min = v_max = None

        aref = AREF[config.terminal]
        chan_spec = config.channel | (range_index << 16) | (aref << 24)
        chanlist = (ctypes.c_uint * 1)(chan_spec)
        ns_interval = (
            int(1e9 / config.sample_rate) if config.sample_rate > 0 else 100000
        )

        cmd = comedi_cmd_struct()
        if self.libcomedi.comedi_get_cmd_generic_timed(
            self.dev_ptr, self._ai_subdev, ctypes.byref(cmd), 1, ns_interval
        ) < 0:
            raise RuntimeError("failed to get generic timed template")

        cmd.start_src = TRIG_NOW
        cmd.start_arg = 0
        cmd.scan_begin_src = TRIG_TIMER
        cmd.scan_begin_arg = ns_interval
        cmd.convert_src = TRIG_TIMER
        cmd.convert_arg = 800
        cmd.scan_end_src = TRIG_COUNT
        cmd.scan_end_arg = 1
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = config.n_samples

        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1

        self.libcomedi.comedi_cancel(self.dev_ptr, self._ai_subdev)

        # The driver rewrites the struct towards something it will accept, and
        # does so in stages; four passes is what the original used and is the
        # documented way to let it converge.
        for _ in range(4):
            self.libcomedi.comedi_command_test(self.dev_ptr, ctypes.byref(cmd))

        maxdata, cal_min, cal_max = self._calibration(
            self._ai_subdev, config.channel, range_index
        )
        if v_min is None:
            v_min, v_max = cal_min, cal_max

        entry = (cmd, chanlist, range_index, maxdata, v_min, v_max)
        self._commands[config] = entry
        return entry

    def _acquire_volts(self, config):
        cmd, _chanlist, _range_index, maxdata, v_min, v_max = self._negotiate(
            config
        )

        if self.libcomedi.comedi_command(self.dev_ptr, ctypes.byref(cmd)) < 0:
            print("[ERROR] Hardware rejected the negotiated DMA command.")
            return None

        bytes_to_read = config.n_samples * 2
        raw = bytearray()

        try:
            while len(raw) < bytes_to_read:
                r, _, _ = select.select([self.fd], [], [], 2.0)

                if not r:
                    self.libcomedi.comedi_poll(self.dev_ptr, self._ai_subdev)
                    r_retry, _, _ = select.select([self.fd], [], [], 0.5)
                    if not r_retry:
                        print(
                            f"[ERROR] DMA Stream Timeout. Read {len(raw)}/"
                            f"{bytes_to_read} bytes."
                        )
                        break

                chunk = os.read(self.fd, bytes_to_read - len(raw))
                if not chunk:
                    break
                raw += chunk

        except KeyboardInterrupt:
            print("\n[WARNING] Keyboard Interrupt detected! Cancelling NI DMA stream...")
            raise

        finally:
            # Guarantees the hardware state is scrubbed clean regardless of
            # errors or success.
            self.libcomedi.comedi_cancel(self.dev_ptr, self._ai_subdev)

        if len(raw) < bytes_to_read:
            return None

        raw_array = np.frombuffer(bytes(raw), dtype=np.uint16)
        return self._to_volts(raw_array, maxdata, v_min, v_max)

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def _write_ao(self, channel, volts):
        if self._ao_subdev < 0:
            raise RuntimeError(
                f"{self.address} reports no analog output subdevice"
            )

        ranges = self._info.ao_ranges
        if not ranges:
            raise RuntimeError(
                f"{self.address} did not report its output range list, so a "
                f"voltage cannot be converted to counts"
            )

        # Pick the narrowest output range containing the request, then do
        # comedi_from_phys()'s conversion by hand - libcomedi's own helper
        # takes the range struct by value, which ctypes can do but not
        # portably across comedi versions.
        index, lo, hi = resolve_range(
            ranges, v_min=min(volts, 0.0), v_max=max(volts, 0.0)
        )
        maxdata = self.libcomedi.comedi_get_maxdata(
            self.dev_ptr, self._ao_subdev, channel
        )
        if maxdata <= 0:
            maxdata = FALLBACK_MAXDATA

        counts = int(round((volts - lo) / (hi - lo) * maxdata))
        counts = max(0, min(int(maxdata), counts))

        rc = self.libcomedi.comedi_data_write(
            self.dev_ptr, self._ao_subdev, channel, index, AREF['rse'], counts
        )
        if rc < 0:
            print(f"[ERROR] comedi_data_write failed on AO{channel}.")
            return False
        return True
