import ctypes
import ctypes.util
import struct
import time
import sys

class NIDriver:
    def __init__(self, device_node=b"/dev/comedi0"):
        lib_path = ctypes.util.find_library('comedi')
        if not lib_path:
            lib_path = '/usr/lib/x86_64-linux-gnu/libcomedi.so.0'
            
        try:
            self.libcomedi = ctypes.CDLL(lib_path)
        except OSError:
            print("Error: Could not load libcomedi.")
            sys.exit(1)

        self.libcomedi.comedi_open.argtypes = [ctypes.c_char_p]
        self.libcomedi.comedi_open.restype = ctypes.c_void_p
        
        self.libcomedi.comedi_data_read.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, 
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)
        ]
        self.libcomedi.comedi_data_read.restype = ctypes.c_int
        
        self.libcomedi.comedi_close.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_close.restype = ctypes.c_int

        self.device_node = device_node

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0 
        aref_ground = 0 # Matches hardcoded legacy C++ configuration
        data_out = ctypes.c_uint()
        readings = []

        delay = 1.0 / sample_rate if sample_rate > 0 else 0

        # Acquisition loop
        for _ in range(n_samples):
            result = self.libcomedi.comedi_data_read(
                dev_ptr, subdevice, channel, range_val, aref_ground, ctypes.byref(data_out)
            )
            if result >= 0:
                readings.append(data_out.value & 0xFFFF)
            else:
                readings.append(32768) 
            
            if delay > 0:
                #time.sleep(delay)

        self.libcomedi.comedi_close(dev_ptr)

        # Voltage scaling using standard NI offset binary mapping
        ni_ranges = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}
        v_max = ni_ranges.get(range_val, 10.0)
        
        float_readings = []
        for raw_int in readings:
            voltage = ((raw_int / 65535.0) * (2.0 * v_max)) - v_max
            float_readings.append(voltage)

        # Pack data into 32-bit floats ("<f") to feed MATLAB's fread format seamlessly
        binary_format = f"<{len(float_readings)}f"
        packed_data = struct.pack(binary_format, *float_readings)

        mode = "wb" if is_first_trace else "ab"
        with open(output_filename, mode) as f:
            f.write(packed_data)

        return True
