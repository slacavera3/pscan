import time
import ctypes

class D2APhidgetDriver:
    def __init__(self, um_per_volt=20.0):
        """
        um_per_volt: 20.0 (Based on 100um range over 5V)
        """
        self.um_per_volt = um_per_volt
        self.min_volts = 0.0
        self.max_volts = 5.0
        
        try:
            self.phidget21 = ctypes.CDLL("libphidget21.so")
        except OSError:
            raise RuntimeError("Could not find libphidget21.so. Ensure it is installed via ldconfig.")

        # Define C signatures
        self.phidget21.CPhidgetAnalog_create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.phidget21.CPhidget_open.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.phidget21.CPhidget_waitForAttachment.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.phidget21.CPhidgetAnalog_setEnabled.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.phidget21.CPhidgetAnalog_getVoltage.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_double)]
        self.phidget21.CPhidgetAnalog_setVoltage.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_double]
        self.phidget21.CPhidget_close.argtypes = [ctypes.c_void_p]

        self.handle = ctypes.c_void_p()
        
        try:
            print("[Phidget D2A] Connecting to Analog Output board...")
            
            if self.phidget21.CPhidgetAnalog_create(ctypes.byref(self.handle)) != 0:
                raise RuntimeError("Failed to create Phidget handle.")
                
            if self.phidget21.CPhidget_open(self.handle, -1) != 0:
                raise RuntimeError("Failed to open Phidget connection.")
                
            if self.phidget21.CPhidget_waitForAttachment(self.handle, 5000) != 0:
                raise RuntimeError("Hardware failed to attach within 5 seconds.")
                
            # Enable channel 0 (PTRUE = 1)
            self.phidget21.CPhidgetAnalog_setEnabled(self.handle, 0, 1)
            
            # Read current voltage to confirm connection without altering the piezo state
            current_v = ctypes.c_double()
            if self.phidget21.CPhidgetAnalog_getVoltage(self.handle, 0, ctypes.byref(current_v)) == 0:
                print(f"[Phidget D2A] Connected. Current piezo state: {current_v.value:.3f}V.")
            else:
                print("[Phidget D2A] Connected. (State unknown until first scan step).")
                
        except RuntimeError as e:
            print(f"[Phidget D2A] Connection failed: {e}")
            raise

    def set_position_um(self, target_um):
        """Converts microns to voltage, clamps it safely, and applies it."""
        target_v = target_um / self.um_per_volt
        safe_v = max(self.min_volts, min(self.max_volts, target_v))
        
        if safe_v != target_v:
            print(f"[WARNING] Requested {target_um}um ({target_v:.3f}V) is out of bounds! Clamping to {safe_v:.3f}V.")
            
        if self.phidget21.CPhidgetAnalog_setVoltage(self.handle, 0, float(safe_v)) != 0:
            print("[Phidget D2A] Error setting voltage.")
            return False
            
        time.sleep(0.05) # Settle time for the piezo
        return True

    def close(self):
        """Closes the connection while leaving the piezo energized (Phidget21 legacy behavior)."""
        if self.handle:
            if self.phidget21.CPhidget_close(self.handle) == 0:
                print("[Phidget D2A] Disconnected (Piezo remains energized).")
            else:
                print("[Phidget D2A] Error closing connection.")
