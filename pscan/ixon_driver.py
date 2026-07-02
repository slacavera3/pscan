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

    def setup(self, exposure=0.1, em_gain=72, kinetic_cycle=0.5, shutter_open=True):
        if not self.is_initialized:
            return

        print(f"Configuring iXon: Exp={exposure}s, EM={em_gain}, Cycle={kinetic_cycle}s")
        self.cam.SetAcquisitionMode(self.codes.Acquisition_Mode.SINGLE_SCAN)
        self.cam.SetReadMode(self.codes.Read_Mode.IMAGE)
        self.cam.SetTriggerMode(self.codes.Trigger_Mode.INTERNAL)
        
        # 1 = Permanently Open, 0 = Auto (closes between scans)
        shutter_mode = 1 if shutter_open else 0
        self.cam.SetShutter(1, shutter_mode, 50, 50)
        
        self.cam.SetExposureTime(exposure)
        self.cam.SetKineticCycleTime(kinetic_cycle)
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
        
        # Poll hardware until data is ready
        status = self.cam.GetStatus()
        while status == self.errors.Error_Codes.DRV_ACQUIRING:
            time.sleep(0.005)
            status = self.cam.GetStatus()

        ret, arr = self.cam.GetOldestImage16(self.image_size)
        if ret == self.errors.Error_Codes.DRV_SUCCESS:
            return np.array(arr).reshape((self.height, self.width))
        else:
            print(f"[ERROR] iXon retrieval failed with code: {ret}")
            return None

    def shutdown(self):
        if self.is_initialized:
            print("Closing iXon shutter and shutting down...")
            self.cam.SetShutter(1, 2, 50, 50) # Force close
            self.cam.ShutDown()
            self.is_initialized = False
