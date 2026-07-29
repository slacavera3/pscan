import ctypes
import os
import time
import numpy as np

# --- Tucsen API Constants mapped from TUDefine.h ---
TUCCM_SEQUENCE         = 0x00
TUCCM_TRIGGER_STANDARD = 0x01

TUGAIN_HDR  = 0x00
TUGAIN_HIGH = 0x01
TUGAIN_LOW  = 0x02

TUFRM_FMT_RAW   = 0x10
TUFRM_FMT_USUAl = 0x11 

TUIDP_GLOBALGAIN = 0x00
TUIDP_EXPOSURETM = 0x01

TUIDC_BITOFDEPTH = 0x02

# --- C-Struct Definitions ---
class TUCAM_INIT(ctypes.Structure):
    _fields_ = [("uiCamCount", ctypes.c_uint32), ("pstrConfigPath", ctypes.c_char_p)]

class TUCAM_OPEN(ctypes.Structure):
    _fields_ = [("uiIdxOpen", ctypes.c_uint32), ("hIdxTUCam", ctypes.c_void_p)]

class TUCAM_FRAME(ctypes.Structure):
    _fields_ = [
        ("szSignature", ctypes.c_char * 8), ("usHeader", ctypes.c_ushort),
        ("usOffset", ctypes.c_ushort), ("usWidth", ctypes.c_ushort),
        ("usHeight", ctypes.c_ushort), ("uiWidthStep", ctypes.c_uint32),
        ("ucDepth", ctypes.c_ubyte), ("ucFormat", ctypes.c_ubyte),
        ("ucChannels", ctypes.c_ubyte), ("ucElemBytes", ctypes.c_ubyte),
        ("ucFormatGet", ctypes.c_ubyte), ("uiIndex", ctypes.c_uint32),
        ("uiImgSize", ctypes.c_uint32), ("uiRsdSize", ctypes.c_uint32),
        ("uiHstSize", ctypes.c_uint32), ("pBuffer", ctypes.c_void_p)
    ]

class TucsenCamera:
    def __init__(self):
        sdk_dir = os.environ.get("TUCSEN_SDK_DIR", "/opt/tucsen/sdk/lib")
        lib_path = os.path.join(sdk_dir, "libTUCam.so")
        
        if not os.path.exists(lib_path):
            raise FileNotFoundError(f"Tucsen driver not found at {lib_path}. Set TUCSEN_SDK_DIR.")
            
        self.tucam = ctypes.CDLL(lib_path)
        self.h_cam = None
        self.is_initialized = False
        self.trigger_mode = TUCCM_SEQUENCE
        
        # --- Explicitly define C++ argument types to prevent segmentation faults ---
        self.tucam.TUCAM_Prop_SetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_double, ctypes.c_int32
        ]
        self.tucam.TUCAM_Prop_SetValue.restype = ctypes.c_int32

        self.tucam.TUCAM_Capa_SetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32
        ]
        self.tucam.TUCAM_Capa_SetValue.restype = ctypes.c_int32

    def connect(self):
        print("Initializing Tucsen Dhyana 95 V2...")
        init_api = TUCAM_INIT(0, None)
        if self.tucam.TUCAM_Api_Init(ctypes.byref(init_api)) != 1 or init_api.uiCamCount == 0:
            raise RuntimeError("Failed to initialize Tucsen API or no cameras found.")
            
        open_cam = TUCAM_OPEN(0, None)
        if self.tucam.TUCAM_Dev_Open(ctypes.byref(open_cam)) != 1:
            raise RuntimeError("Found Tucsen camera but failed to open it.")
            
        self.h_cam = open_cam.hIdxTUCam
        self.is_initialized = True
        print("Tucsen initialized successfully.")

    def setup(self, exposure=0.1, gain_mode="hdr", bit_depth=16, is_master=True):
        if not self.is_initialized:
            return
            
        print(f"Configuring Tucsen: Exp={exposure}s, GainMode={gain_mode}, BitDepth={bit_depth}-bit")
        
        # 1. Map string to exact Enum (Your Fix)
        gain_enum = TUGAIN_HDR
        if gain_mode.lower() == "high":
            gain_enum = TUGAIN_HIGH
        elif gain_mode.lower() == "low":
            gain_enum = TUGAIN_LOW

        # 2. Assign Camera State Logic
        self.trigger_mode = TUCCM_SEQUENCE if is_master else TUCCM_TRIGGER_STANDARD

        # 3. Apply settings to hardware via API
        # Set Bit Depth (Capability)
        self.tucam.TUCAM_Capa_SetValue(self.h_cam, TUIDC_BITOFDEPTH, bit_depth)
        
        # Set Gain Mode (Property)
        self.tucam.TUCAM_Prop_SetValue(self.h_cam, TUIDP_GLOBALGAIN, float(gain_enum), 0)
        
        # Set Exposure Time (Property) 
        # Note: Depending on firmware, Tucsen APIs often expect exposure in milliseconds. 
        # If your exposure behaves unexpectedly, you may need to pass `float(exposure * 1000.0)`.
        self.tucam.TUCAM_Prop_SetValue(self.h_cam, TUIDP_EXPOSURETM, float(exposure), 0)

    def acquire(self):
        if not self.is_initialized:
            return None

        frame = TUCAM_FRAME()
        frame.pBuffer = None
        frame.ucFormatGet = TUFRM_FMT_USUAl
        frame.uiRsdSize = 1 
        
        self.tucam.TUCAM_Buf_Alloc(self.h_cam, ctypes.byref(frame))
        self.tucam.TUCAM_Cap_Start(self.h_cam, self.trigger_mode) 
        
        ret = self.tucam.TUCAM_Buf_WaitForFrame(self.h_cam, ctypes.byref(frame), 2000)
        
        image_copy = None
        if ret == 1 and frame.pBuffer:
            buffer_ptr = ctypes.cast(frame.pBuffer, ctypes.POINTER(ctypes.c_uint16))
            image_array = np.ctypeslib.as_array(buffer_ptr, shape=(frame.usHeight, frame.usWidth))
            image_copy = image_array.copy() 
        else:
            print("[ERROR] Tucsen frame retrieval timed out.")

        self.tucam.TUCAM_Cap_Stop(self.h_cam)
        self.tucam.TUCAM_Buf_Release(self.h_cam)
        
        return image_copy

    def shutdown(self):
        if self.is_initialized:
            print("Shutting down Tucsen camera...")
            self.tucam.TUCAM_Dev_Close(self.h_cam)
            self.tucam.TUCAM_Api_Uninit()
            self.is_initialized = False
