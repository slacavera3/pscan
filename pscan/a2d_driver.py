import ctypes
import ctypes.util
import struct
import sys
import os

# Comedi Trigger Constants
TRIG_NONE  = 0x0001
TRIG_NOW   = 0x0002
TRIG_FOLLOW= 0x0004
TRIG_TIME  = 0x0008
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020
TRIG_EXT   = 0x0040
TRIG_INT   = 0x0080

# Map the exact Linux Comedi Command C-Struct
class comedi_cmd_struct(ctypes.Structure):
    _fields_ = [
        ("subdev", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("start_src", ctypes.c_uint),
        ("start_arg", ctypes.c_uint),
        ("scan_begin_src", ctypes.c_uint),
        ("scan_begin_arg", ctypes.c_uint),
        ("convert_src", ctypes.c_uint),
        ("convert_arg", ctypes.c_uint),
        ("scan_end_src", ctypes.c_uint),
        ("scan_end_arg", ctypes.c_uint),
        ("stop_src", ctypes.c_uint),
        ("stop_arg", ctypes.c_uint),
        ("chanlist", ctypes.POINTER(ctypes.c_uint)),
        ("chanlist_len", ctypes.c_uint),
        ("data", ctypes.POINTER(ctypes.c_short)),
        ("data_len", ctypes.c_uint),
    ]

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

        # Standard Init/Teardown
        self.libcomedi.comedi_open.argtypes = [ctypes.c_char_p]
        self.libcomedi.comedi_open.restype = ctypes.c_void_p
        
        self.libcomedi.comedi_close.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_close.restype = ctypes.c_int

        # Async Hardware Command Functions
        self.libcomedi.comedi_get_cmd_generic_timed.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(comedi_cmd_struct),
            ctypes.c_uint, ctypes.c_uint
        ]
        self.libcomedi.comedi_get_cmd_generic_timed.restype = ctypes.c_int

        self.libcomedi.comedi_command_test.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
        self.libcomedi.comedi_command_test.restype = ctypes.c_int

        self.libcomedi.comedi_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
        self.libcomedi.comedi_command.restype = ctypes.c_int

        self.libcomedi.comedi_fileno.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_fileno.restype = ctypes.c_int

        self.device_node = device_node

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0 
        aref_ground = 0 # Matches hardcoded legacy C++ configuration

        # Emulate the C CR_PACK macro to build the channel configuration
        pack_val = ((aref_ground & 0x3) << 24) | ((range_val & 0xff) << 16) | (channel & 0xffff)
        chanlist = (ctypes.c_uint * 1)(pack_val)

        # Build the hardware recipe
        cmd = comedi_cmd_struct()
        period_ns = int(1e9 / sample_rate) if sample_rate > 0 else int(1e5)

        # 1. Ask Linux to populate a generic timed command template
        res = self.libcomedi.comedi_get_cmd_generic_timed(
            dev_ptr, subdevice, ctypes.byref(cmd), 1, period_ns
        )
        if res < 0:
            print("Error: Could not initialize hardware-timed command.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        # 2. Tell the command to stop exactly after n_samples
        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = n_samples

        # 3. Test and push the command to the NI Hardware
        self.libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
        if self.libcomedi.comedi_command(dev_ptr, ctypes.byref(cmd)) < 0:
            print("Error: NI Hardware rejected the async command.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        # 4. Read the raw binary stream directly from the Linux file descriptor
        fd = self.libcomedi.comedi_fileno(dev_ptr)
        bytes_to_read = n_samples * 2  # 16-bit samples = 2 bytes per point
        raw_bytes = b""

        while len(raw_bytes) < bytes_to_read:
            # os.read blocks naturally until the NI card pushes data into RAM
            chunk = os.read(fd, bytes_to_read - len(raw_bytes))
            if not chunk:
                break
            raw_bytes += chunk

        self.libcomedi.comedi_close(dev_ptr)

        # Unpack the binary chunk into unsigned 16-bit integers
        actual_samples = len(raw_bytes) // 2
        readings = struct.unpack(f"<{actual_samples}H", raw_bytes)

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
