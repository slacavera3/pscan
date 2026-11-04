"""The Phidget analog output board, as a d2a-only backend.

This is the board that has always driven the piezo, reached through
libphidget21 by ctypes. Nothing about it changed; it just sits behind the same
interface as the DAQ cards now, so `action d2a` can target it or a PCIe-6351
without the scan loop caring which.

Two things are deliberately preserved from the original driver:

  * The output range is declared as 0..5V, not the board's full -10..+10V.
    That 0..5V was a clamp protecting the piezo, chosen for a 100um stage, and
    quietly widening it to what the board can do would let a mistyped
    coordinate drive the piezo past its travel. A config that genuinely needs
    more says so with its own 'limits' line.

  * Closing the session leaves the output energized. Phidget21 behaves that
    way and the piezo should not drop to 0V because a scan ended.

There is no analog input here, so an 'action a2d' block can never resolve to
this backend.
"""

import ctypes

from pscan.daq.base import DAQBackend, DeviceInfo

# The piezo-safe window the original driver enforced. See the module docstring.
DEFAULT_AO_RANGE = (0.0, 5.0)

# Phidget's "wait for the board to show up" budget, in ms, as the original
# driver used.
ATTACH_TIMEOUT_MS = 5000

_LIB_NAME = "libphidget21.so"


def _load_libphidget():
    lib = ctypes.CDLL(_LIB_NAME)

    lib.CPhidgetAnalog_create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.CPhidgetAnalog_create.restype = ctypes.c_int

    lib.CPhidget_open.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.CPhidget_open.restype = ctypes.c_int

    lib.CPhidget_waitForAttachment.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.CPhidget_waitForAttachment.restype = ctypes.c_int

    lib.CPhidgetAnalog_setEnabled.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int
    ]
    lib.CPhidgetAnalog_setEnabled.restype = ctypes.c_int

    lib.CPhidgetAnalog_setVoltage.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_double
    ]
    lib.CPhidgetAnalog_setVoltage.restype = ctypes.c_int

    lib.CPhidgetAnalog_getVoltage.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_double)
    ]
    lib.CPhidgetAnalog_getVoltage.restype = ctypes.c_int

    lib.CPhidget_close.argtypes = [ctypes.c_void_p]
    lib.CPhidget_close.restype = ctypes.c_int

    return lib


class PhidgetBackend(DAQBackend):
    name = "phidget"

    def __init__(self, address="phidget0", ao_range=DEFAULT_AO_RANGE):
        super().__init__(address=address)
        self._lib = None
        self._handle = None
        self._ao_range = tuple(ao_range)
        self._enabled = set()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def available(cls):
        try:
            _load_libphidget()
            return True
        except OSError:
            return False

    @classmethod
    def unavailable_reason(cls):
        return ("libphidget21.so could not be loaded. Run 'bash install.sh' "
                "once from the stable checkout to build and install it.")

    @classmethod
    def discoverable(cls):
        # Auto-detection must not open this board. Opening it claims the
        # device, and it may be holding a piezo at a position right now -
        # which is not something to disturb while merely answering "what
        # hardware is on this machine?". It is reachable by being named.
        return False

    @classmethod
    def enumerate(cls):
        """Report the board without opening it.

        Deliberately does not probe: see discoverable(). The channel count is
        the 1002's four outputs; whether a board is actually attached is only
        discovered at connect time.
        """
        if not cls.available():
            return []
        return [DeviceInfo(
            backend=cls.name,
            address="phidget0",
            model="PhidgetAnalog (not probed)",
            n_ai=0,
            n_ao=4,
            ao_ranges=(DEFAULT_AO_RANGE,),
        )]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        if self._connected:
            return

        try:
            self._lib = _load_libphidget()
        except OSError as e:
            raise RuntimeError(self.unavailable_reason()) from e

        print("[Phidget D2A] Connecting to Analog Output board...")
        handle = ctypes.c_void_p()

        if self._lib.CPhidgetAnalog_create(ctypes.byref(handle)) != 0:
            raise RuntimeError("Failed to create Phidget handle.")
        if self._lib.CPhidget_open(handle, -1) != 0:
            raise RuntimeError("Failed to open Phidget connection.")
        if self._lib.CPhidget_waitForAttachment(handle, ATTACH_TIMEOUT_MS) != 0:
            raise RuntimeError(
                f"Hardware failed to attach within "
                f"{ATTACH_TIMEOUT_MS / 1000:.0f} seconds."
            )

        self._handle = handle
        self._connected = True
        print("[Phidget D2A] Connected.")

    def _close_device(self):
        # The output is left energized on purpose - see the module docstring.
        if self._handle:
            if self._lib.CPhidget_close(self._handle) == 0:
                print("[Phidget D2A] Disconnected (output remains energized).")
            else:
                print("[Phidget D2A] Error closing connection.")
            self._handle = None

    def describe(self):
        return DeviceInfo(
            backend=self.name,
            address=self.address,
            model="PhidgetAnalog",
            n_ai=0,
            n_ao=4,
            ao_ranges=(self._ao_range,),
        )

    # ------------------------------------------------------------------
    # d2a
    # ------------------------------------------------------------------

    def _write_ao(self, channel, volts):
        # Enabling is idempotent but is a USB round trip, so it is done once
        # per channel rather than on every step of a scan.
        if channel not in self._enabled:
            self._lib.CPhidgetAnalog_setEnabled(self._handle, channel, 1)
            self._enabled.add(channel)

        if self._lib.CPhidgetAnalog_setVoltage(
            self._handle, channel, float(volts)
        ) != 0:
            print(f"[Phidget D2A] Error setting voltage on channel {channel}.")
            return False
        return True

    def read_d2a(self, channel):
        """Query the board for the voltage it is actually holding.

        The Phidget is the one backend here that can be asked, which is why
        the original driver could report the piezo's state at startup without
        moving it.
        """
        if not self._connected:
            return self._last_ao.get(channel)

        value = ctypes.c_double()
        if self._lib.CPhidgetAnalog_getVoltage(
            self._handle, channel, ctypes.byref(value)
        ) == 0:
            return float(value.value)
        return self._last_ao.get(channel)

    def read_d2a_is_measured(self):
        return self._connected

    # ------------------------------------------------------------------
    # a2d - there isn't any
    # ------------------------------------------------------------------

    def _acquire_volts(self, config):
        raise RuntimeError(
            "the Phidget analog board has no analog input; an 'action a2d' "
            "block cannot use the phidget backend"
        )
