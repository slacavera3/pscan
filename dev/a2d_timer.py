import ctypes
import ctypes.util
import struct
import sys
import os
import select
import numpy as np
import time

TRIG_NOW   = 0x0002
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020

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

class NIDriverTimer:
    def __init__(self, device_node=b"/dev/comedi0"):
        lib_path = ctypes.util.find_library('comedi') or '/usr/lib/x86_64-linux-gnu/libcomedi.so.0'
        try:
            self.libcomedi = ctypes.CDLL(lib_path)
        except OSError:
            print("Error: Could not load libcomedi.")
            sys.exit(1)

        self.libcomedi.comedi_open.argtypes = [ctypes.c_char_p]
        self.libcomedi.comedi_open.restype = ctypes.c_void_p
        self.libcomedi.comedi_close.argtypes = [ctypes.c_void_p]
        
        self.libcomedi.comedi_get_cmd_generic_timed.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(comedi_cmd_struct), ctypes.c_uint, ctypes.c_uint
        ]
        self.libcomedi.comedi_command_test.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
        self.libcomedi.comedi_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
        self.libcomedi.comedi_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.libcomedi.comedi_fileno.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_poll.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.libcomedi.comedi_poll.restype = ctypes.c_int

        self.device_node = device_node

    def acquire_a2d_timed(self, channel, range_val, n_samples, sample_rate, output_filename):
        print(f"\n--- Profiling A2D Acquisition ({n_samples} samples @ {sample_rate} Hz) ---")
        total_start = time.perf_counter()
        
        # =================================================================
        # 1. INIT & OPEN
        # =================================================================
        t0_init = time.perf_counter()
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0
        fd = self.libcomedi.comedi_fileno(dev_ptr)
        t1_init = time.perf_counter()
        print(f"[Timer] Device Open & Init      : {t1_init-t0_init:.4f} seconds")

        # =================================================================
        # 2. HARDWARE NEGOTIATION
        # =================================================================
        t0_neg = time.perf_counter()
        chan_spec = channel | (range_val << 16) | (0 << 24)
        chanlist = (ctypes.c_uint * 1)(chan_spec)
        ns_interval = int(1e9 / sample_rate) if sample_rate > 0 else 100000

        cmd = comedi_cmd_struct()
        if self.libcomedi.comedi_get_cmd_generic_timed(dev_ptr, subdevice, ctypes.byref(cmd), 1, ns_interval) < 0:
            print("[ERROR] Failed to get generic timed template.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        cmd.start_src, cmd.start_arg = TRIG_NOW, 0
        cmd.scan_begin_src, cmd.scan_begin_arg = TRIG_TIMER, ns_interval
        cmd.convert_src, cmd.convert_arg = TRIG_TIMER, 800         
        cmd.scan_end_src, cmd.scan_end_arg = TRIG_COUNT, 1
        cmd.stop_src, cmd.stop_arg = TRIG_COUNT, n_samples
        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1

        self.libcomedi.comedi_cancel(dev_ptr, subdevice)
        for _ in range(4):
            self.libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
        t1_neg = time.perf_counter()
        print(f"[Timer] Comedi Struct Negotiate : {t1_neg-t0_neg:.4f} seconds")

        # =================================================================
        # 3. DMA STREAM LOOP
        # =================================================================
        t0_dma = time.perf_counter()
        if self.libcomedi.comedi_command(dev_ptr, ctypes.byref(cmd)) < 0:
            print("[ERROR] Hardware rejected the negotiated DMA command.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        bytes_to_read = n_samples * 2
        raw_bytes = b""
        
        while len(raw_bytes) < bytes_to_read:
            r, _, _ = select.select([fd], [], [], 2.0)
            if not r:
                self.libcomedi.comedi_poll(dev_ptr, subdevice)
                r_retry, _, _ = select.select([fd], [], [], 0.5)
                if not r_retry:
                    print(f"[ERROR] Timeout. Read {len(raw_bytes)}/{bytes_to_read} bytes.")
                    break
            chunk = os.read(fd, bytes_to_read - len(raw_bytes))
            if not chunk: break
            raw_bytes += chunk

        self.libcomedi.comedi_cancel(dev_ptr, subdevice)
        self.libcomedi.comedi_close(dev_ptr)
        t1_dma = time.perf_counter()
        
        physical_time = n_samples / sample_rate
        print(f"[Timer] DMA Stream Loop         : {t1_dma-t0_dma:.4f} seconds (Expected physics: {physical_time:.4f}s)")

        if len(raw_bytes) < bytes_to_read:
            return False

        # =================================================================
        # 4. MATH & VECTORIZATION
        # =================================================================
        t0_math = time.perf_counter()
        raw_array = np.frombuffer(raw_bytes, dtype=np.uint16)
        ni_ranges = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}
        v_max = ni_ranges.get(range_val, 10.0)
        
        float_array = ((raw_array / 65535.0) * (2.0 * v_max)) - v_max
        packed_data = float_array.astype(np.float32).tobytes()
        t1_math = time.perf_counter()
        print(f"[Timer] Numpy Vectorized Math   : {t1_math-t0_math:.4f} seconds")

        # =================================================================
        # 5. DISK WRITE
        # =================================================================
        t0_disk = time.perf_counter()
        with open(output_filename, "wb") as f:
            f.write(packed_data)
        t1_disk = time.perf_counter()
        print(f"[Timer] Disk Write (.d file)    : {t1_disk-t0_disk:.4f} seconds")

        total_end = time.perf_counter()
        print(f"\n[Timer] TOTAL A2D PIPELINE      : {total_end-total_start:.4f} seconds")
        return True

if __name__ == "__main__":
    # Simulating a standard 1,000 sample read at 10 kHz
    timer_ni = NIDriverTimer()
    timer_ni.acquire_a2d_timed(
        channel=0, 
        range_val=0, 
        n_samples=1000, 
        sample_rate=10000, 
        output_filename="timing_test_a2d.d"
    )
