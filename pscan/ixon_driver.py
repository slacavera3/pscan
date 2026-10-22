import os
import sys
import time
import numpy as np

# Safely import pyAndorSDK2 (Windows 7 Only)
try:
    if os.name == 'nt':
        sdk_path = r"c:\Program Files\Micro-Manager-2.0gamma\Andor SDK\Python\pyAndorSDK2"
        if sdk_path not in sys.path:
            sys.path.insert(0, sdk_path)
        from pyAndorSDK2 import atmcd, atmcd_codes, atmcd_errors
        ANDOR_AVAILABLE = True
    else:
        ANDOR_AVAILABLE = False
except ImportError:
    ANDOR_AVAILABLE = False
    print("[SYSTEM] pyAndorSDK2 not found. iXon driver disabled on this OS.")

# Bounded TEC cooldown. This loop used to be unbounded, so an unreachable
# target - a cooler that bottoms out above the requested temperature, or poor
# airflow on a warm day - would spin here forever with the shutter open.
COOLDOWN_TIMEOUT_S = 600.0
COOLDOWN_POLL_S = 3.0

# Give up early once the temperature stops falling: a plateau means the TEC is
# already at its limit, so waiting out the full timeout achieves nothing. The
# window is generous because the approach to target genuinely slows down near
# the end, and mistaking that for a plateau would abandon cooling a degree or
# two short. Widen it if a healthy cooldown ever trips the warning.
COOLDOWN_PLATEAU_S = 180.0
COOLDOWN_PLATEAU_DELTA_C = 0.5


class IXonCamera:
    def __init__(self, dll_path=r"c:\Program Files\Micro-Manager-2.0gamma\Andor SDK\Python\pyAndorSDK2\pyAndorSDK2\libs\Windows\64"):
        if not ANDOR_AVAILABLE:
            raise RuntimeError("Andor SDK not available on this system.")
            
        self.cam = atmcd(userPath=dll_path)
        self.codes = atmcd_codes
        self.errors = atmcd_errors
        self.is_initialized = False
        self.image_size = 0
        self.width = 0
        self.height = 0

    def connect(self):
        print("Initializing iXon Camera...")
        if self.cam.Initialize("") != self.errors.Error_Codes.DRV_SUCCESS:
            raise RuntimeError("Failed to initialize iXon camera.")
        self.is_initialized = True
        print("iXon initialized successfully.")

    def setup(self, exposure=0.1, em_gain=1, shutter_open=True, target_temp=None):
        if not self.is_initialized:
            return

        # Handle TEC Cooling if specified in the confile
        if target_temp is not None:
            print(f"\nConfiguring iXon TEC Cooling to {target_temp}°C...")
            self.cam.SetTemperature(target_temp)
            self.cam.CoolerON()
            
            print("Waiting for temperature to stabilize (this may take a few minutes)...")
            deadline = time.monotonic() + COOLDOWN_TIMEOUT_S
            stabilized = False
            last_temp = None
            best_temp = None
            best_time = time.monotonic()

            while time.monotonic() < deadline:
                ret, current_temp = self.cam.GetTemperature()
                last_temp = current_temp

                # In the Andor SDK, 20036 is the standard DRV_TEMP_STABILIZED code
                if ret == 20036 or ret == getattr(self.errors.Error_Codes, 'DRV_TEMP_STABILIZED', -1):
                    print(f" -> iXon temperature stabilized at {current_temp}°C.")
                    stabilized = True
                    break

                if current_temp is not None:
                    now = time.monotonic()
                    if best_temp is None or current_temp <= best_temp - COOLDOWN_PLATEAU_DELTA_C:
                        best_temp = current_temp
                        best_time = now
                    elif now - best_time >= COOLDOWN_PLATEAU_S:
                        print(
                            f" -> Temperature has held near {current_temp}°C for "
                            f"{COOLDOWN_PLATEAU_S:.0f}s without stabilizing; "
                            f"the cooler looks to be at its limit."
                        )
                        break

                print(f" -> Cooling... Current temp: {current_temp}°C (Target: {target_temp}°C)")
                time.sleep(COOLDOWN_POLL_S)

            if not stabilized:
                # A warm sensor is a data-quality problem, not a reason to lose
                # the run, so carry on and say so loudly.
                print(
                    f"\n[WARNING] iXon did not stabilize at {target_temp}°C "
                    f"(last read {last_temp}°C). Continuing anyway - dark "
                    f"current will be higher than intended, so treat absolute "
                    f"intensities with care."
                )

        print(f"\nConfiguring iXon: Exp={exposure}s, EM={em_gain}")
        self.cam.SetAcquisitionMode(self.codes.Acquisition_Mode.SINGLE_SCAN)
        self.cam.SetReadMode(self.codes.Read_Mode.IMAGE)
        self.cam.SetTriggerMode(self.codes.Trigger_Mode.INTERNAL)
        
        # 1 = Permanently Open, 0 = Auto (closes between scans)
        shutter_mode = 1 if shutter_open else 0
        self.cam.SetShutter(1, shutter_mode, 50, 50)
        
        self.cam.SetExposureTime(exposure)
        self.cam.SetOutputAmplifier(0) 
        self.cam.SetEMCCDGain(em_gain)
        
        _, xpixels, ypixels = self.cam.GetDetector()
        self.width = xpixels
        self.height = ypixels
        self.image_size = xpixels * ypixels
        self.cam.SetImage(1, 1, 1, xpixels, 1, ypixels)

    def acquire(self):
        """Fires the camera and blocks until the single frame is retrieved."""
        if not self.is_initialized:
            return None

        self.cam.StartAcquisition()
        self.cam.WaitForAcquisition()

        ret, arr = self.cam.GetOldestImage16(self.image_size)
        if ret == self.errors.Error_Codes.DRV_SUCCESS:
            return np.array(arr, dtype=np.uint16).reshape((self.height, self.width))
        else:
            print(f"[ERROR] iXon retrieval failed with code: {ret}")
            return None

    def shutdown(self):
        if self.is_initialized:
            print("Closing iXon shutter and shutting down...")
            self.cam.SetShutter(1, 2, 50, 50) # Force close
            self.cam.CoolerOFF() # Safely turn off TEC cooler
            self.cam.ShutDown()
            self.is_initialized = False
