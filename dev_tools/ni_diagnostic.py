import ctypes
import ctypes.util
import sys

# Comedi Trigger Constants mapping
TRIG_STR = {
    1: "TRIG_NONE", 2: "TRIG_NOW", 4: "TRIG_FOLLOW", 
    8: "TRIG_TIME", 16: "TRIG_TIMER", 32: "TRIG_COUNT", 
    64: "TRIG_EXT", 128: "TRIG_INT"
}

class comedi_cmd_struct(ctypes.Structure):
    _fields_ = [
        ("subdev", ctypes.c_uint), ("flags", ctypes.c_uint),
        ("start_src", ctypes.c_uint), ("start_arg", ctypes.c_uint),
        ("scan_begin_src", ctypes.c_uint), ("scan_begin_arg", ctypes.c_uint),
        ("convert_src", ctypes.c_uint), ("convert_arg", ctypes.c_uint),
        ("scan_end_src", ctypes.c_uint), ("scan_end_arg", ctypes.c_uint),
        ("stop_src", ctypes.c_uint), ("stop_arg", ctypes.c_uint),
        ("chanlist", ctypes.POINTER(ctypes.c_uint)),
        ("chanlist_len", ctypes.c_uint),
        ("data", ctypes.POINTER(ctypes.c_short)),
        ("data_len", ctypes.c_uint),
    ]

def decode_flags(val):
    return " | ".join([name for bit, name in TRIG_STR.items() if val & bit]) or str(val)

def print_cmd(cmd, label):
    print(f"\n--- {label} ---")
    print(f"start_src:      {decode_flags(cmd.start_src)} (arg: {cmd.start_arg})")
    print(f"scan_begin_src: {decode_flags(cmd.scan_begin_src)} (arg: {cmd.scan_begin_arg})")
    print(f"convert_src:    {decode_flags(cmd.convert_src)} (arg: {cmd.convert_arg})")
    print(f"scan_end_src:   {decode_flags(cmd.scan_end_src)} (arg: {cmd.scan_end_arg})")
    print(f"stop_src:       {decode_flags(cmd.stop_src)} (arg: {cmd.stop_arg})")

def run_diagnostic():
    lib_path = ctypes.util.find_library('comedi') or '/usr/lib/x86_64-linux-gnu/libcomedi.so.0'
    libcomedi = ctypes.CDLL(lib_path)
    
    libcomedi.comedi_open.restype = ctypes.c_void_p
    libcomedi.comedi_command_test.argtypes = [ctypes.c_void_p, ctypes.POINTER(comedi_cmd_struct)]
    
    dev_ptr = libcomedi.comedi_open(b"/dev/comedi0")
    if not dev_ptr:
        print("Failed to open /dev/comedi0")
        return

    # Emulate the setup
    chanlist = (ctypes.c_uint * 1)(0)
    cmd = comedi_cmd_struct()
    
    res = libcomedi.comedi_get_cmd_generic_timed(dev_ptr, 0, ctypes.byref(cmd), 1, int(1e9 / 10000))
    if res < 0:
        print("comedi_get_cmd_generic_timed failed.")
        return
        
    cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
    cmd.chanlist_len = 1
    cmd.stop_src = 32 # TRIG_COUNT
    cmd.stop_arg = 1000

    print_cmd(cmd, "Initial Generic Command (What Linux guessed)")

    # Test 1: Ask the hardware to check the sources
    res1 = libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
    print_cmd(cmd, f"After Test 1 (Hardware Source Constraints) - Return Code: {res1}")

    # Test 2: Ask the hardware to resolve arguments
    res2 = libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
    print_cmd(cmd, f"After Test 2 (Hardware Argument Resolution) - Return Code: {res2}")

    # Test 3: Final check
    res3 = libcomedi.comedi_command_test(dev_ptr, ctypes.byref(cmd))
    print(f"\nFinal comedi_command_test readiness code: {res3} (0 means perfect)")

    libcomedi.comedi_close(dev_ptr)

if __name__ == "__main__":
    run_diagnostic()
