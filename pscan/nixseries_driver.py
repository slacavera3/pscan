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
  * The card bound to vfio-pci, and membership of the 'pscan' group.
    install.sh arranges both; group membership needs a fresh login. No root
    and no BIOS change, which is the point - Secure Boot can stay on.
  * An IOMMU. vfio-pci requires one, and it is also what makes kernel
    lockdown willing to allow userspace device access.

dev/check_vfio.py diagnoses the whole chain - IOMMU group, lockdown mode,
binding, container, group viability and the BAR mapping - without going
through pscan at all.

ANALOG OUTPUTS DO NOT OUTLIVE THE PROCESS. vfio-pci resets the card when its
last file descriptor closes - deliberately, so a userspace driver cannot
leave hardware running unattended - and that reset zeroes the DACs. There is
no way to opt out.

A scan is unaffected: the session stays open from the first coordinate to the
last, so a mirror or piezo holds throughout. But the output WILL drop to zero
when pscan exits, which differs from the Phidget path. That matters more for
a sample-positioning piezo than for a scanning mirror, and it is worth
knowing before moving a piezo onto this card. 'pydaq set --hold' keeps the
card open so a voltage can be applied and measured.

NO KERNEL MODULE. Although the download is the kernel-module flavour, only
its osiBus.cpp and headers are used; libs/xseries_shim/osiUserCode_vfio.cpp
replaces NI's bus implementation and reaches the card through vfio-pci
instead of nirlpk.ko. nirlpk.ko is 2011 source that does not build against a
modern kernel, and on a Secure Boot machine could not be loaded anyway, being
unsigned.

VFIO also sidesteps kernel lockdown, which blocks mapping a BAR through
/sys/bus/pci/devices/<slot>/resource0 with EPERM that root cannot override.
The IOMMU is what makes lockdown willing to permit device access at all - and
it means a scan needs no root, given the udev rule install.sh writes.

Building nirlpk.ko and using NI's implementation instead remains possible -
PSCAN_XSERIES_USE_NIRLPK=1 at install time - and is the only route to DMA.

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

#: Every entry point this module binds. Checked as a set the moment the
#: library loads, so a stale .so is reported once, by name, instead of
#: surfacing as an AttributeError at whichever call site gets there first.
_REQUIRED_SYMBOLS = (
    "xseries_open",
    "xseries_close",
    "xseries_is_open",
    "xseries_last_error",
    "xseries_info",
    "xseries_selftest",
    "xseries_read_ai",
    "xseries_write_ao",
    "xseries_ao_shadow",
    # Not used for anything except proving the library is current. Listing it
    # here means a .so older than this file is refused by name, instead of
    # running happily and producing results for code that is not in it.
    "xseries_build_id",
    "xseries_snapshot_registers",
)


class _SnapshotStruct(ctypes.Structure):
    """Mirrors `struct xseries_snapshot` in the shim. Order matters."""
    _fields_ = [
        ("chinch_signature", ctypes.c_uint),
        ("stc3_signature", ctypes.c_uint),
        ("ao_dac_shadow", ctypes.c_uint),
        ("ao_config_bank", ctypes.c_uint),
        ("ao_trigger_select", ctypes.c_uint),
        ("ao_fifo_status", ctypes.c_uint),
        ("wdt_state", ctypes.c_uint),
        ("wdt_expired_count", ctypes.c_uint),
    ]

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

# X Series models by PCI SUBSYSTEM device id, transcribed from the DDK's
# Examples/devices.cpp, as (name, AI channels, AO channels).
#
# The PCI device id is useless for identification: every X Series board
# reports the same generic value (0xc4c4), which is why lspci calls a
# PCIe-6351 a "PXIe/PCIe Device". The model is in the subsystem id, and that
# is readable from sysfs - so enumeration can name the card accurately
# without opening it, mapping registers or needing root.
#
# Only used for display before connect(); once open, the card is asked
# directly via the DDK and that answer wins.
X_SERIES_BY_SUBSYSTEM = {
    0x7425: ("PCIe-6320", 16, 0),
    0x7427: ("PCIe-6321", 16, 2),
    0x7429: ("PCIe-6323", 32, 4),
    0x7428: ("PXIe-6323", 32, 4),
    0x742B: ("PCIe-6341", 16, 2),
    0x742D: ("PCIe-6343", 32, 4),
    0x742A: ("PXIe-6341", 16, 2),
    0x742C: ("PXIe-6343", 32, 4),
    0x742F: ("PCIe-6351", 16, 2),
    0x7431: ("PCIe-6353", 32, 4),
    0x7436: ("PXIe-6356", 8, 2),
    0x7437: ("PXIe-6358", 16, 4),
    0x7433: ("PCIe-6361", 16, 2),
    0x7435: ("PCIe-6363", 32, 4),
    0x7432: ("PXIe-6361", 16, 2),
    0x7434: ("PXIe-6363", 32, 4),
    0x7438: ("PXIe-6366", 8, 2),
    0x7439: ("PXIe-6368", 16, 4),
    0x74DD: ("PXIe-6376", 8, 2),
    0x74DE: ("PXIe-6378", 16, 4),
}


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

    # A library older than this Python is a real hazard here: the shim and the
    # driver are built and installed separately, so a part-finished install
    # leaves them out of step. Binding a symbol that is not there raises a
    # bare AttributeError from whichever line happens to reach it first, which
    # says nothing about the cause. Name it instead.
    missing = [name for name in _REQUIRED_SYMBOLS
               if not hasattr(lib, name)]
    if missing:
        _load_error = (
            f"{_LIB_NAME} is older than this version of pscan: it does not "
            f"export {', '.join(missing)}. The shim and the Python package "
            f"are installed separately, so one can lag the other. Rebuild "
            f"both with 'bash install.sh' - note that '--drivers-only' does "
            f"the opposite half and will not fix this."
        )
        raise RuntimeError(_load_error)

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

    lib.xseries_ao_shadow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,                  # channel
        ctypes.POINTER(ctypes.c_uint),    # raw shadow register
        ctypes.POINTER(ctypes.c_int),     # expected, or -1 if never written
        ctypes.POINTER(ctypes.c_double),  # volts last requested
    ]
    lib.xseries_ao_shadow.restype = ctypes.c_int

    lib.xseries_build_id.argtypes = []
    lib.xseries_build_id.restype = ctypes.c_char_p

    lib.xseries_snapshot_registers.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_SnapshotStruct)
    ]
    lib.xseries_snapshot_registers.restype = ctypes.c_int

    lib.xseries_selftest.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_double),  # temperature, degrees C
        ctypes.POINTER(ctypes.c_double),  # the sensor's mean voltage
    ]
    lib.xseries_selftest.restype = ctypes.c_int

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

    def attribute(entry, name):
        with open(os.path.join(root, entry, name)) as f:
            return int(f.read().strip(), 16)

    for entry in sorted(os.listdir(root)):
        try:
            if attribute(entry, "vendor") != 0x1093:
                continue
            # The subsystem id is what identifies the model; the device id is
            # the same generic value across the whole X Series.
            subsystem = attribute(entry, "subsystem_device")
        except (OSError, ValueError):
            continue
        found.append((entry, subsystem))
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


def _debug():
    return os.environ.get("PSCAN_XSERIES_DEBUG") is not None


def pin_power_state(slot):
    """Stop the kernel suspending the card while we are using it.

    Linux PCI runtime power management can put an idle device into D3 to save
    power, and vfio-pci has supported that since 6.1. A suspended card loses
    its analog outputs and comes back with its registers at reset values -
    which looks exactly like an output that mysteriously drops to zero a few
    seconds after being set, while every register access still appears to
    work because the access itself wakes the device up.

    pscan holds outputs steady for the length of a scan, with no register
    traffic in between, which is precisely the idle pattern that triggers
    this. So the device is pinned to 'on' for as long as it is ours.

    Best effort: it needs root or a udev rule, and on a kernel or device
    without runtime PM the file is simply absent. Returns what happened, for
    the debug line - this must never stop a scan starting.
    """
    path = os.path.join("/sys/bus/pci/devices", slot, "power", "control")
    try:
        with open(path) as f:
            before = f.read().strip()
    except OSError as e:
        return f"runtime PM not exposed ({e.strerror})"

    if before == "on":
        return "runtime PM already off (control=on)"

    try:
        with open(path, "w") as f:
            f.write("on")
    except OSError as e:
        return (f"control={before}, but could not pin it ({e.strerror}); "
                f"the card may suspend mid-scan")

    try:
        with open(path) as f:
            after = f.read().strip()
    except OSError:
        after = "?"
    return f"runtime PM pinned: control {before} -> {after}"


def power_status(slot):
    """The card's runtime-PM state, for diagnostics. '?' if not exposed."""
    path = os.path.join("/sys/bus/pci/devices", slot, "power", "runtime_status")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "?"


class NIXSeriesDriver(DAQBackend):
    """One X Series card, reached through the MHDDK shim."""

    name = "nixseries"

    # vfio-pci resets the device when its last file descriptor closes, which
    # zeroes the DACs. Nothing this driver does can prevent it.
    outputs_survive_close = False

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
        for slot, subsystem in _pci_devices():
            known = X_SERIES_BY_SUBSYSTEM.get(subsystem)
            if known is not None:
                model, n_ai, n_ao = known
            else:
                # An NI card this table does not list. Still offered, because
                # connect() asks the card itself and may well identify it -
                # the table is only for naming it beforehand. One of each
                # channel so a block looking for either still considers it.
                model = f"NI card, subsystem 0x{subsystem:04x} (unrecognised)"
                n_ai, n_ao = 1, 1

            found.append(DeviceInfo(
                backend=cls.name,
                address=slot,
                model=model,
                n_ai=n_ai,
                n_ao=n_ao,
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

        # Before opening: a card the kernel is free to suspend will drop its
        # outputs and reset its registers partway through a scan.
        note = pin_power_state(self.address)
        if _debug():
            print(f"[xseries] {self.address}: {note}")
            print(f"[xseries] runtime status: {power_status(self.address)}")

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
            # The outputs drop to 0V here whatever the shim does: vfio-pci
            # resets the device when its last file descriptor closes, by
            # design, so a userspace driver cannot leave hardware driving
            # unattended. An output on this backend lasts as long as the
            # process does - see `pydaq set --hold`.
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
    # Diagnostics
    # ------------------------------------------------------------------

    def selftest(self):
        """Read the card's internal temperature sensor.

        Returns (temperature_c, mean_volts).

        The point of this is that the sensor is INTERNAL: it goes through the
        same ADC, timing engine and FIFO as a real measurement, but involves
        no terminal block, umbilical or wiring. So a plausible temperature
        proves the acquisition chain works end to end and narrows any problem
        to selecting an external channel, while a reading of zero says the
        ADC is not converting at all.
        """
        lib = _load()
        temperature = ctypes.c_double(0.0)
        volts = ctypes.c_double(0.0)

        rc = lib.xseries_selftest(
            self._handle, ctypes.byref(temperature), ctypes.byref(volts)
        )
        if rc != 0:
            raise RuntimeError(self._error("X Series self-test failed"))

        return temperature.value, volts.value

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
        # Always full scale, deliberately, and not the narrowest range that
        # fits the value.
        #
        # The range lives in the channel's config bank, and changing it
        # reprograms that bank - which runs the DDK's aoHelper::reset() and
        # drops the output through 0V on the way. Choosing per value means an
        # axis sweeping -6V to +6V crosses the 5V boundary twice a line and
        # glitches the mirror twice a line, at the same two positions of every
        # line: an artefact that looks like the sample, not the rig.
        #
        # What that would buy is a half-size DAC step, 152uV against 305uV.
        # On a 20um/V axis that is 3nm against 6nm, both far below anything
        # the mechanics can hold, so there is nothing here worth a glitch.
        #
        # If a use ever does need the finer step, the range belongs to the
        # axis - from the `limits` of its d2a block, decided once when the
        # axis is built - not to the individual value.
        index = 0

        rc = _load().xseries_write_ao(
            self._handle, channel, index, float(volts)
        )
        if rc != 0:
            raise RuntimeError(self._error(f"X Series AO{channel} write failed"))
        return True

    def ao_shadow(self, channel):
        """What the DAC is holding now, read back from the hardware.

        Returns (shadow, expected, volts, agrees). `shadow` is the raw
        AO_DacShadow register - offset binary, 16 bits - and `expected` is
        the offset-binary form of the last code written, or None if nothing
        has been written to this channel yet.

        The two registers use different encodings: AO_Direct_Data takes the
        DDK scaler's two's complement, AO_DacShadow reports offset binary, so
        a healthy channel reads back the written code with bit 15 flipped.
        Comparing them raw says "mismatch" on a perfectly good DAC.
        """
        if self._handle is None:
            raise RuntimeError("X Series session is not connected")

        shadow = ctypes.c_uint()
        expected = ctypes.c_int()
        volts = ctypes.c_double()
        rc = _load().xseries_ao_shadow(
            self._handle, channel,
            ctypes.byref(shadow), ctypes.byref(expected), ctypes.byref(volts),
        )
        if rc < 0:
            raise RuntimeError(self._error(f"X Series AO{channel} readback failed"))

        return (
            shadow.value,
            None if expected.value < 0 else expected.value,
            volts.value,
            rc == 0,
        )

    def snapshot(self, channel=0):
        """Key registers read in one pass, as a plain dict.

        For diagnosing an output that changes on its own: the signatures say
        whether the card was reset, the config bank whether the AO subsystem
        was reset, and the FIFO and trigger registers whether something is
        still driving the DAC.
        """
        if self._handle is None:
            raise RuntimeError("X Series session is not connected")

        raw = _SnapshotStruct()
        if _load().xseries_snapshot_registers(
                self._handle, channel, ctypes.byref(raw)) != 0:
            raise RuntimeError(self._error("register snapshot failed"))

        return {name: getattr(raw, name) for name, _t in raw._fields_}

    def read_d2a(self, channel):
        """The voltage ao<channel> is holding, confirmed against the card.

        Unlike the base class's recollection, this checks the DAC shadow
        register really still holds the code that was written. If it does
        not, the card has lost the value and reporting the commanded one
        would be a lie - so None is returned, and callers that need to know
        where an output is will say they cannot tell rather than guess.
        """
        try:
            _shadow, expected, volts, agrees = self.ao_shadow(channel)
        except RuntimeError:
            return self._last_ao.get(channel)

        if expected is None:
            return None
        return volts if agrees else None

    def read_d2a_is_measured(self):
        return self._handle is not None
