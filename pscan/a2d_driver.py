import ctypes
import ctypes.util
import struct
import sys
import os
import select

# Comedi Trigger Constants
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
        self.libcomedi.comedi_close.restype = ctypes.c_int
        
        self.libcomedi.comedi_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
        self.libcomedi.comedi_command.restype = ctypes.c_int
        self.libcomedi.comedi_cancel.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.libcomedi.comedi_cancel.restype = ctypes.c_int
        self.libcomedi.comedi_fileno.argtypes = [ctypes.c_void_p]
        self.libcomedi.comedi_fileno.restype = ctypes.c_int

        self.device_node = device_node

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        dev_ptr = self.libcomedi.comedi_open(self.device_node)
        if not dev_ptr:
            print(f"Failed to open NI card at {self.device_node.decode()}")
            return False

        subdevice = 0
        fd = self.libcomedi.comedi_fileno(dev_ptr)
        
        # Build Comedi Channel Specifier
        # (chan) | (range << 16) | (aref_ground << 24)
        chan_spec = channel | (range_val << 16) | (0 << 24)
        chanlist = (ctypes.c_uint * 1)(chan_spec)

        # Convert requested sample rate into hardware timer intervals (nanoseconds)
        ns_interval = int(1e9 / sample_rate) if sample_rate > 0 else 10000

        # Construct high-speed DMA Command Structure
        cmd = comedi_cmd_struct()
        cmd.subdev = subdevice
        cmd.flags = 0
        cmd.start_src = TRIG_NOW
        cmd.start_arg = 0
        cmd.scan_begin_src = TRIG_TIMER
        cmd.scan_begin_arg = ns_interval
        cmd.convert_src = TRIG_NOW
        cmd.convert_arg = 0
        cmd.scan_end_src = TRIG_COUNT
        cmd.scan_end_arg = 1
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = n_samples
        cmd.chanlist = chanlist
        cmd.chanlist_len = 1

        # Cancel any leftover triggers on the subdevice before initializing
        self.libcomedi.comedi_cancel(dev_ptr, subdevice)

        # Arm the hardware DMA command
        if self.libcomedi.comedi_command(dev_ptr, ctypes.byref(cmd)) < 0:
            print("DMA command failing or unsupported. Falling back to clean synchronous sync.")
            self.libcomedi.comedi_close(dev_ptr)
            return False

        # Read streaming bytes directly via DMA stream file descriptor
        bytes_to_read = n_samples * 2
        raw_bytes = b""
        
        # High-performance event loop using system select
        while len(raw_bytes) < bytes_to_read:
            r, _, _ = select.select([fd], [], [], 2.0)
            if not r:
                print("DMA Stream Timeout: Hardware failed to feed buffer.")
                self.libcomedi.comedi_cancel(dev_ptr, subdevice)
                self.libcomedi.comedi_close(dev_ptr)
                return False
                
            chunk = os.read(fd, bytes_to_read - len(raw_bytes))
            if not chunk: 
                break
            raw_bytes += chunk

        # Clean shutdown of active triggers
        self.libcomedi.comedi_cancel(dev_ptr, subdevice)
        self.libcomedi.comedi_close(dev_ptr)

        if len(raw_bytes) < bytes_to_read:
            return False

        # Unpack raw 16-bit offset-binary values from the hardware stream
        actual_samples = len(raw_bytes) // 2
        readings = struct.unpack(f"<{actual_samples}H", raw_bytes)

        # Scale voltage values
        ni_ranges = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}
        v_max = ni_ranges.get(range_val, 10.0)
        
        float_readings = [((r / 65535.0) * (2.0 * v_max)) - v_max for r in readings]
        packed_data = struct.pack(f"<{len(float_readings)}f", *float_readings)

        mode = "wb" if is_first_trace else "ab"
        with open(output_filename, mode) as f:
            f.write(packed_data)

        return True
