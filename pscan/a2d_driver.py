import ctypes
import ctypes.util
import struct
import sys
import os
import select

# Comedi Trigger Constants
TRIG_NONE  = 0x0001
TRIG_NOW   = 0x0002
TRIG_FOLLOW= 0x0004
TRIG_TIME  = 0x0008
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020
TRIG_EXT   = 0x0040
TRIG_INT   = 0x0080

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

class NIDriver:
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
        self.libcomedi.comedi_fileno.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        
        self.libcomedi.comedi_data_read.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, 
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)
        ]
        self.device_node = device_node

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0 
        
        # 1. ATTEMPT HIGH-SPEED ASYNC ACQUISITION
        success = self._try_async(dev_ptr, subdevice, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace)
        
        # 2. SEAMLESS FALLBACK
        if not success:
            print("[WARNING] Async DMA failed! Seamlessly falling back to synchronous polling loop.")
            self.libcomedi.comedi_close(dev_ptr)
            dev_ptr = self.libcomedi.comedi_open(self.device_node)
            self._do_sync(dev_ptr, subdevice, channel, range_val, n_samples, output_filename, is_first_trace)

        self.libcomedi.comedi_close(dev_ptr)
        return True

    def _try_async(self, dev_ptr, subdevice, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace):
        pack_val = ((0 & 0x3) << 24) | ((range_val & 0xff) << 16) | (channel & 0xffff)
        chanlist = (ctypes.c_uint * 1)(pack_val)

        cmd = comedi_cmd_struct()
        period_ns = int(1e9 / sample_rate) if sample_rate > 0 else 100000

        # 1. Get the perfect baseline template from Linux (Sets hidden PCI flags!)
        if self.libcomedi.comedi_get_cmd_generic_timed(dev_ptr, subdevice, ctypes.byref(cmd), 1, period_ns) < 0:
            return False

        # 2. Overwrite the sources to guarantee instant DMA start
        cmd.start_src = TRIG_NOW
        cmd.start_arg = 0
        
        # 3. DESTROY THE RACE CONDITION
        # Force the hardware to negotiate the exact physical speed limit (e.g. 800ns)
        cmd.convert_src = TRIG_TIMER
        cmd.convert_arg = 1
        
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = n_samples

        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1

        # 4. The Negotiation Dance (Loop until hardware is perfectly satisfied)
        is_valid = False
        for _ in range(5):
            if self.libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd)) == 0:
                is_valid = True
                break

        if not is_valid:
            return False

        # 5. Execute Hardware Command
        if self.libcomedi.comedi_command(dev_ptr, ctypes.byref(cmd)) < 0:
            return False 

        # 6. Read from DMA Buffer
        fd = self.libcomedi.comedi_fileno(dev_ptr)
        bytes_to_read = n_samples * 2
        raw_bytes = b""
        safe_timeout = (n_samples / sample_rate) + 0.5 if sample_rate > 0 else 1.0

        while len(raw_bytes) < bytes_to_read:
            ready, _, _ = select.select([fd], [], [], safe_timeout)
            
            if not ready:
                self.libcomedi.comedi_cancel(dev_ptr, subdevice)
                return False 
                
            chunk = os.read(fd, bytes_to_read - len(raw_bytes))
            if not chunk: break
            raw_bytes += chunk

        if len(raw_bytes) < bytes_to_read:
            return False

        actual_samples = len(raw_bytes) // 2
        readings = struct.unpack(f"<{actual_samples}H", raw_bytes)
        self._process_and_save(readings, range_val, output_filename, is_first_trace)
        return True

    def _do_sync(self, dev_ptr, subdevice, channel, range_val, n_samples, output_filename, is_first_trace):
        data_out = ctypes.c_uint()
        readings = []

        for _ in range(n_samples):
            res = self.libcomedi.comedi_data_read(dev_ptr, subdevice, channel, range_val, 0, ctypes.byref(data_out))
            if res >= 0:
                readings.append(data_out.value & 0xFFFF)
            else:
                readings.append(32768)

        self._process_and_save(readings, range_val, output_filename, is_first_trace)

    def _process_and_save(self, readings, range_val, output_filename, is_first_trace):
        ni_ranges = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}
        v_max = ni_ranges.get(range_val, 10.0)
        
        float_readings = [((r / 65535.0) * (2.0 * v_max)) - v_max for r in readings]
        packed_data = struct.pack(f"<{len(float_readings)}f", *float_readings)

        mode = "wb" if is_first_trace else "ab"
        with open(output_filename, mode) as f:
            f.write(packed_data)
