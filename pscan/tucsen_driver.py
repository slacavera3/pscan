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
        self.is_capturing = False # NEW
        self.frame = TUCAM_FRAME() # NEW
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
        
        # --- Suppress noisy C-level stdout/stderr ---
        import sys
        fd_out, fd_err = sys.stdout.fileno(), sys.stderr.fileno()
        saved_out, saved_err = os.dup(fd_out), os.dup(fd_err)
        devnull = os.open(os.devnull, os.O_WRONLY)
        
        os.dup2(devnull, fd_out)
        os.dup2(devnull, fd_err)
        
        try:
            init_api = TUCAM_INIT(0, None)
            res_init = self.tucam.TUCAM_Api_Init(ctypes.byref(init_api))
            
            open_cam = TUCAM_OPEN(0, None)
            res_open = self.tucam.TUCAM_Dev_Open(ctypes.byref(open_cam))
        finally:
            # Restore normal terminal output
            os.dup2(saved_out, fd_out)
            os.dup2(saved_err, fd_err)
            os.close(devnull)
            os.close(saved_out)
            os.close(saved_err)
        # ---------------------------------------------

        if res_init != 1 or init_api.uiCamCount == 0:
            raise RuntimeError("Failed to initialize Tucsen API or no cameras found.")
            
        if res_open != 1:
            raise RuntimeError("Found Tucsen camera but failed to open it.")
            
        self.h_cam = open_cam.hIdxTUCam
        self.is_initialized = True
        print("Tucsen initialized successfully.")

    def setup(self, exposure=0.1, gain_mode="hdr", bit_depth=16, is_master=True):
        if not self.is_initialized:
            return
            
        print(f"Configuring Tucsen: Exp={exposure}s, GainMode={gain_mode}, BitDepth={bit_depth}-bit")
        
        gain_enum = TUGAIN_HDR
        if gain_mode.lower() == "high":
            gain_enum = TUGAIN_HIGH
        elif gain_mode.lower() == "low":
            gain_enum = TUGAIN_LOW

        self.trigger_mode = TUCCM_SEQUENCE if is_master else TUCCM_TRIGGER_STANDARD

        self.tucam.TUCAM_Capa_SetValue(self.h_cam, TUIDC_BITOFDEPTH, bit_depth)
        self.tucam.TUCAM_Prop_SetValue(self.h_cam, TUIDP_GLOBALGAIN, float(gain_enum), 0)
        
        # FIX: Convert seconds to milliseconds for the SDK
        exp_ms = float(exposure * 1000.0)
        self.tucam.TUCAM_Prop_SetValue(self.h_cam, TUIDP_EXPOSURETM, exp_ms, 0)

        # Start the stream ONCE, but suppress the "4 frames!" output
        if not self.is_capturing:
            import sys
            fd_out, fd_err = sys.stdout.fileno(), sys.stderr.fileno()
            saved_out, saved_err = os.dup(fd_out), os.dup(fd_err)
            devnull = os.open(os.devnull, os.O_WRONLY)
            
            os.dup2(devnull, fd_out)
            os.dup2(devnull, fd_err)
            
            try:
                self.frame.pBuffer = None
                self.frame.ucFormatGet = TUFRM_FMT_USUAl
                self.frame.uiRsdSize = 1
                
                self.tucam.TUCAM_Buf_Alloc(self.h_cam, ctypes.byref(self.frame))
                self.tucam.TUCAM_Cap_Start(self.h_cam, self.trigger_mode) 
                self.is_capturing = True

                # Catch the delayed async C++ teardown logs
                time.sleep(0.1)
            finally:
                os.dup2(saved_out, fd_out)
                os.dup2(saved_err, fd_err)
                os.close(devnull)
                os.close(saved_out)
                os.close(saved_err)

    def _frame_to_array(self):
        """Copy the current frame out of the SDK's buffer.

        Honours ucElemBytes for the sample width and uiWidthStep for the row
        stride. The previous version always read uint16 from tightly packed
        rows, so 'bit_depth 8' in the config produced garbage, and any row
        padding the SDK added would have been folded into the image.
        """
        height = self.frame.usHeight
        width = self.frame.usWidth
        elem = self.frame.ucElemBytes or 2
        dtype = np.uint8 if elem == 1 else np.uint16

        # uiWidthStep is the byte distance between row starts, which can
        # exceed width * elem when rows are padded.
        stride = self.frame.uiWidthStep or (width * elem)

        raw = ctypes.cast(self.frame.pBuffer, ctypes.POINTER(ctypes.c_ubyte))
        flat = np.ctypeslib.as_array(raw, shape=(height * stride,))

        # Slice each row's real pixels out of its padded row, then copy: the
        # result must not alias the SDK buffer, which is reused next frame.
        rows = np.ascontiguousarray(flat.reshape(height, stride)[:, :width * elem])
        return rows.view(dtype).copy()

    def acquire(self):
        if not self.is_initialized or not self.is_capturing:
            return None

        # FAST PATH: Just wait for the next frame from the already-running stream
        ret = self.tucam.TUCAM_Buf_WaitForFrame(self.h_cam, ctypes.byref(self.frame), 2000)

        image_copy = None
        if ret == 1 and self.frame.pBuffer:
            image_copy = self._frame_to_array()
        else:
            print("[ERROR] Tucsen frame retrieval timed out.")

        # NO MORE STOPPING OR RELEASING HERE

        return image_copy

    def shutdown(self):
        if self.is_initialized:
            print("Shutting down Tucsen camera...")
            
            # Suppress the teardown thread logs
            import sys
            fd_out, fd_err = sys.stdout.fileno(), sys.stderr.fileno()
            saved_out, saved_err = os.dup(fd_out), os.dup(fd_err)
            devnull = os.open(os.devnull, os.O_WRONLY)
            
            os.dup2(devnull, fd_out)
            os.dup2(devnull, fd_err)
            
            try:
                if getattr(self, 'is_capturing', False):
                    self.tucam.TUCAM_Cap_Stop(self.h_cam)
                    self.tucam.TUCAM_Buf_Release(self.h_cam)
                    self.is_capturing = False
                    
                self.tucam.TUCAM_Dev_Close(self.h_cam)
                self.tucam.TUCAM_Api_Uninit()
                self.is_initialized = False

                # Catch the delayed async C++ teardown logs
                time.sleep(0.1)
            finally:
                os.dup2(saved_out, fd_out)
                os.dup2(saved_err, fd_err)
                os.close(devnull)
                os.close(saved_out)
                os.close(saved_err)
