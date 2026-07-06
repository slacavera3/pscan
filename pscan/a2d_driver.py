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

        self.libcomedi.comedi_open.argtypes = [ctypes.c_char_p]
        self.libcomedi.comedi_open.restype = ctypes.c_void_p
        
        self.libcomedi.comedi_close.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_close.restype = ctypes.c_int

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

        self.libcomedi.comedi_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.libcomedi.comedi_cancel.restype = ctypes.c_int

        # NEW: Binding for the internal software trigger
        self.libcomedi.comedi_internal_trigger.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        self.libcomedi.comedi_internal_trigger.restype = ctypes.c_int

        self.device_node = device_node

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0 
        aref_ground = 0

        pack_val = ((aref_ground & 0x3) << 24) | ((range_val & 0xff) << 16) | (channel & 0xffff)
        chanlist = (ctypes.c_uint * 1)(pack_val)

        cmd = comedi_cmd_struct()
        period_ns = int(1e9 / sample_rate) if sample_rate > 0 else int(1e5)

        res = self.libcomedi.comedi_get_cmd_generic_timed(
            dev_ptr, subdevice, ctypes.byref(cmd), 1, period_ns
        )
        if res < 0:
            print("Error: Could not initialize hardware-timed command.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        # Request instant start
        cmd.start_src = TRIG_NOW
        cmd.start_arg = 0
        
        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = n_samples

        # Let Comedi negotiate the constraints with the NI hardware
        self.libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
        self.libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))

        if self.libcomedi.comedi_command(dev_ptr, ctypes.byref(cmd)) < 0:
            print("Error: NI Hardware rejected the async command.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        # NEW: Fire the trigger if the hardware negotiated TRIG_INT
        if cmd.start_src == TRIG_INT:
            self.libcomedi.comedi_internal_trigger(dev_ptr, subdevice, cmd.start_arg)
        elif cmd.start_src == TRIG_EXT:
            print("[WARNING] NI Card is waiting for an EXTERNAL hardware pulse to start!")

        fd = self.libcomedi.comedi_fileno(dev_ptr)
        bytes_to_read = n_samples * 2
        raw_bytes = b""

        safe_timeout = (n_samples / sample_rate) + 1.5 if sample_rate > 0 else 2.0

        while len(raw_bytes) < bytes_to_read:
            ready, _, _ = select.select([fd], [], [], safe_timeout)
            
            if not ready:
                print(f"\n[ERROR] NI Card timed out! Expected {n_samples} points, got {len(raw_bytes)//2}.")
                self.libcomedi.comedi_cancel(dev_ptr, subdevice)
                self.libcomedi.comedi_close(dev_ptr)
                return False
                
            chunk = os.read(fd, bytes_to_read - len(raw_bytes))
            if not chunk:
                break
            raw_bytes += chunk

        self.libcomedi.comedi_close(dev_ptr)

        if len(raw_bytes) < bytes_to_read:
            print(f"[ERROR] Incomplete data stream.")
            return False

        actual_samples = len(raw_bytes) // 2
        readings = struct.unpack(f"<{actual_samples}H", raw_bytes)

        ni_ranges = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}
        v_max = ni_ranges.get(range_val, 10.0)
        
        float_readings = []
        for raw_int in readings:
            voltage = ((raw_int / 65535.0) * (2.0 * v_max)) - v_max
            float_readings.append(voltage)

        binary_format = f"<{len(float_readings)}f"
        packed_data = struct.pack(binary_format, *float_readings)

        mode = "wb" if is_first_trace else "ab"
        with open(output_filename, mode) as f:
            f.write(packed_data)

        return True
