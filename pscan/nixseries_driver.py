"""NI X Series cards via the MHDDK, on any Linux distribution.

This is the driver for an NI X Series board - the PCIe-6351 and its 632x,
634x, 635x, 636x, 637x relatives - on a machine NI-DAQmx will not run on.

Why it exists. NI-DAQmx ships .debs for Ubuntu, RHEL and openSUSE only, so on
Debian there is nothing to install. comedi's ni_pcimio driver covers the E and
M series and has no X Series board in its table at all. That left NI's
Measurement Hardware Driver Development Kit, which documents the DAQ-STC3
registers and ships C++ source for them, and which NI describes as the only
Linux support they offer for custom drivers.

libs/xseries_shim/ builds that source, plus a small C API of our own, into
libpscan_xseries.so. This module is the ctypes binding for it - the same
arrangement pscan already uses for libcomedi, libphidget21 and libTUCam.

What it needs from the system:

  * libpscan_xseries.so, built by install.sh from the DDK in libs/vendor/
  * The DDK's Linux OS interface, for osiBus.cpp and its headers. Take
    "Linux (via kernel module)" VERSION 2.0 from NI's MHDDK page - the only
    64-bit-capable Linux option, since the "/dev/mem" flavour is 32-bit and
    targets kernel 2.4.
  * ROOT. The card's PCI BARs are mapped directly and the kernel does not let
    an unprivileged process do that.

NO KERNEL MODULE. Although the download is the kernel-module flavour, only
its osiBus.cpp and headers are used; libs/xseries_shim/osiUserCode_sysfs.cpp
replaces NI's bus implementation and maps the card through
/sys/bus/pci/devices/<slot>/resource0 instead of through nirlpk.ko. That
matters because nirlpk.ko is 2011 source that does not build against a modern
kernel, while the PCI sysfs interface is stable kernel ABI that does not rot.

Building nirlpk.ko and using NI's implementation instead remains possible -
PSCAN_XSERIES_USE_NIRLPK=1 at install time - and is the route to DMA, which
the sysfs path cannot offer.

DMA is not used either way. A finite trace has to fit in the card's AI FIFO,
which on a 6351 is several thousand samples against pscan's typical 1000, and
the acquisition is still hardware-timed off the 100 MHz timebase, so the
sample rate is exact. A trace longer than the FIFO is refused with a message
saying so rather than silently truncated.

STATUS: the shim is written against the DDK's aiex2 and aoex1 examples but has
not been compiled or run against hardware. Until it has, treat a reading from
this backend as unverified.
"""

import ctypes
import os

import numpy as np

from pscan.daq import DAQBackend, DeviceInfo, resolve_range

_LIB_NAME = "libpscan_xseries.so"

# nNISTC3::tInputRange, in the DDK's order. The index into this list is what
# a .con file's `range` line means on this backend, and resolve_range() picks
# one from explicit volts.
AI_RANGES = (
    (-10.0, 10.0),
    (-5.0, 5.0),
    (-2.0, 2.0),
    (-1.0, 1.0),
    (-0.5, 0.5),
    (-0.2, 0.2),
    (-0.1, 0.1),
)

# nNISTC3::tOutputRange. Only the two fixed ranges; the DDK's APFI external
# reference modes are not exposed.
AO_RANGES = (
    (-10.0, 10.0),
    (-5.0, 5.0),
)

# The shim's terminal codes, matching nAI::tAI_Config_Channel_Type_t. X Series
# AI has no pseudodifferential mode, so pscan's fourth terminal name has no
# mapping here and is refused.
TERMINAL_CODES = {
    'rse': 0,
    'nrse': 1,
    'diff': 2,
}

#: The DAQ-STC3 convert clock needs 400 timebase ticks on a multiplexed
#: device, which caps a single-channel sample rate at 100 MHz / 400.
MAX_RATE_HZ = 250000.0


class _DeviceInfoStruct(ctypes.Structure):
    """Mirrors xseries_device_info in the shim. Field order must match."""
    _fields_ = [
        ("product_id", ctypes.c_uint32),
        ("name", ctypes.c_char * 64),
        ("is_simultaneous", ctypes.c_uint32),
        ("n_ai_channels", ctypes.c_uint32),
        ("n_ao_channels", ctypes.c_uint32),
        ("n_adcs", ctypes.c_uint32),
        ("n_dacs", ctypes.c_uint32),
        ("ai_fifo_samples", ctypes.c_uint32),
    ]


_lib = None
_load_error = None


def _load():
    """Load the shim once, remembering why it failed if it did."""
    global _lib, _load_error

    if _lib is not None:
        return _lib
    if _load_error is not None:
        raise RuntimeError(_load_error)

    try:
        lib = ctypes.CDLL(_LIB_NAME)
    except OSError as e:
        _load_error = (
            f"{_LIB_NAME} could not be loaded ({e}). It is built from NI's X "
            f"Series DDK, which cannot be shipped in this repository - run "
            f"'bash install.sh', which asks for the DDK download and then "
            f"builds it."
        )
        raise RuntimeError(_load_error) from e

    lib.xseries_open.argtypes = [ctypes.c_char_p]
    lib.xseries_open.restype = ctypes.c_void_p

    lib.xseries_close.argtypes = [ctypes.c_void_p]
    lib.xseries_close.restype = None

    lib.xseries_is_open.argtypes = [ctypes.c_void_p]
    lib.xseries_is_open.restype = ctypes.c_int

    lib.xseries_last_error.argtypes = [ctypes.c_void_p]
    lib.xseries_last_error.restype = ctypes.c_char_p

    lib.xseries_info.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DeviceInfoStruct)
    ]
    lib.xseries_info.restype = ctypes.c_int

    lib.xseries_read_ai.argtypes = [
        ctypes.c_void_p,            # handle
        ctypes.c_uint32,            # channel
        ctypes.c_uint32,            # range index
        ctypes.c_uint32,            # n_samples
        ctypes.c_double,            # rate_hz
        ctypes.c_uint32,            # terminal
        ctypes.POINTER(ctypes.c_double),  # out
    ]
    lib.xseries_read_ai.restype = ctypes.c_int

    lib.xseries_write_ao.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_double
    ]
    lib.xseries_write_ao.restype = ctypes.c_int

    _lib = lib
    return _lib


def _pci_devices():
    """Every NI PCI device the kernel can see, as 'bus:device.function'.

    Read from sysfs rather than by shelling out to lspci, so enumeration
    needs no external command. NI's PCI vendor id is 0x1093.
    """
    found = []
    root = "/sys/bus/pci/devices"
    if not os.path.isdir(root):
        return found

    for entry in sorted(os.listdir(root)):
        try:
            with open(os.path.join(root, entry, "vendor")) as f:
                if int(f.read().strip(), 16) != 0x1093:
                    continue
            with open(os.path.join(root, entry, "device")) as f:
                device_id = int(f.read().strip(), 16)
        except (OSError, ValueError):
            continue
        found.append((entry, device_id))
    return found


def _resource_string(slot):
    """The DDK's board location for a sysfs slot like '0000:82:00.0'.

    main.cpp builds it as "PXI<bus>::<device>::INSTR" from DECIMAL bus and
    device numbers, while sysfs names the slot in hex - so 82:00.0 becomes
    PXI130::0::INSTR. Getting that conversion wrong points the driver at the
    wrong card, which is worth being explicit about.
    """
    _domain, bus_hex, rest = slot.split(":")
    device_hex = rest.split(".")[0]
    return f"PXI{int(bus_hex, 16)}::{int(device_hex, 16)}::INSTR"


class NIXSeriesDriver(DAQBackend):
    """One X Series card, reached through the MHDDK shim."""

    name = "nixseries"

    def __init__(self, address=None):
        super().__init__(address=address)
        self._handle = None
        self._info = None
        self._fifo_samples = 0

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
        """Report NI PCI devices without opening them.

        Opening needs root and maps registers, so enumeration stops at sysfs:
        it reports what is present and leaves identifying the exact model to
        connect(), which can ask the card.
        """
        if not cls.available():
            return []

        found = []
        for slot, device_id in _pci_devices():
            found.append(DeviceInfo(
                backend=cls.name,
                address=slot,
                model=f"NI PCI device 0x{device_id:04x} (not probed)",
                # Not yet known - the card has to be opened to ask. Declared
                # as capable of both so a block looking for either considers
                # it, rather than being filtered out before it can be tried.
                n_ai=1,
                n_ao=1,
                ai_ranges=AI_RANGES,
                ao_ranges=AO_RANGES,
                max_ai_rate=MAX_RATE_HZ,
            ))
        return found

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return

        lib = _load()

        if self.address is None:
            candidates = _pci_devices()
            if not candidates:
                raise RuntimeError(
                    "no NI PCI device found in /sys/bus/pci/devices. Check "
                    "the card is seated ('lspci | grep -i national')."
                )
            if len(candidates) > 1:
                slots = ", ".join(slot for slot, _ in candidates)
                raise RuntimeError(
                    f"{len(candidates)} NI PCI devices present ({slots}) and "
                    f"no 'device' line said which to use."
                )
            self.address = candidates[0][0]

        resource = _resource_string(self.address)
        handle = lib.xseries_open(resource.encode())
        if not handle:
            raise RuntimeError(
                f"could not allocate a session for {self.address}"
            )

        self._handle = handle
        if not lib.xseries_is_open(handle):
            message = (lib.xseries_last_error(handle) or b"").decode(
                errors="replace")
            lib.xseries_close(handle)
            self._handle = None
            raise RuntimeError(
                f"could not open {self.address} ({resource}): {message}"
            )

        raw = _DeviceInfoStruct()
        if lib.xseries_info(handle, ctypes.byref(raw)) != 0:
            raise RuntimeError(self._error("could not read device info"))

        self._fifo_samples = raw.ai_fifo_samples
        self._info = DeviceInfo(
            backend=self.name,
            address=self.address,
            model=raw.name.decode(errors="replace") or "unknown X Series",
            n_ai=raw.n_ai_channels,
            n_ao=raw.n_ao_channels,
            ai_ranges=AI_RANGES,
            ao_ranges=AO_RANGES,
            max_ai_rate=MAX_RATE_HZ,
        )
        self._connected = True

    def _close_device(self):
        if self._handle is not None:
            # The shim leaves the analog outputs where they are rather than
            # driving them to zero - see its xseries_close() comment, and note
            # that behaviour is still unverified on hardware.
            _load().xseries_close(self._handle)
            self._handle = None

    def describe(self):
        if self._info is None:
            raise RuntimeError("X Series session is not connected")
        return self._info

    def _error(self, prefix):
        message = (_load().xseries_last_error(self._handle) or b"").decode(
            errors="replace")
        return f"{prefix}: {message}" if message else prefix

    # ------------------------------------------------------------------
    # a2d
    # ------------------------------------------------------------------

    def _acquire_volts(self, config):
        lib = _load()

        index, _lo, _hi = resolve_range(
            AI_RANGES,
            range_index=config.range_index,
            v_min=config.v_min,
            v_max=config.v_max,
        )

        try:
            terminal = TERMINAL_CODES[config.terminal]
        except KeyError:
            raise RuntimeError(
                f"X Series analog input has no '{config.terminal}' terminal "
                f"mode; use one of: {', '.join(sorted(TERMINAL_CODES))}"
            ) from None

        # Checked here as well as in the shim so the message can name the
        # ceiling, and so a .con file's mistake is caught before the card is
        # touched.
        if config.sample_rate > MAX_RATE_HZ:
            raise RuntimeError(
                f"{config.sample_rate:g} Hz is above this backend's "
                f"{MAX_RATE_HZ:g} Hz ceiling for a single channel (the "
                f"convert clock needs 400 ticks of the 100 MHz timebase)."
            )

        if self._fifo_samples and config.n_samples > self._fifo_samples:
            raise RuntimeError(
                f"{config.n_samples} samples exceed the card's AI FIFO "
                f"({self._fifo_samples} samples). This backend has no DMA, so "
                f"a finite trace must fit in the FIFO - lower 'n_samples', or "
                f"split the measurement across several a2d blocks."
            )

        if config.n_samples < 2:
            raise RuntimeError(
                "a hardware-timed trace needs at least 2 samples"
            )

        # float64 because that is what the shim writes, and what the DDK's
        # scaler produces once calibration is applied. pscan.daq packs it to
        # float32 for the .d file.
        buffer = np.zeros(config.n_samples, dtype=np.float64)
        pointer = buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_double))

        rc = lib.xseries_read_ai(
            self._handle,
            config.channel,
            index,
            config.n_samples,
            float(config.sample_rate),
            terminal,
            pointer,
        )
        if rc != 0:
            print(f"[ERROR] {self._error('X Series acquisition failed')}")
            return None

        return buffer

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def _write_ao(self, channel, volts):
        # Narrowest output range that contains the request, so a small swing
        # gets the finer DAC resolution.
        index, _lo, _hi = resolve_range(
            AO_RANGES, v_min=min(volts, 0.0), v_max=max(volts, 0.0)
        )

        rc = _load().xseries_write_ao(
            self._handle, channel, index, float(volts)
        )
        if rc != 0:
            raise RuntimeError(self._error(f"X Series AO{channel} write failed"))
        return True
