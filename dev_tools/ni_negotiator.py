import ctypes
import ctypes.util

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

def run_negotiation():
    lib = ctypes.CDLL(ctypes.util.find_library('comedi') or '/usr/lib/x86_64-linux-gnu/libcomedi.so.0')
    lib.comedi_open.restype = ctypes.c_void_p
    
    dev = lib.comedi_open(b"/dev/comedi0")
    if not dev:
        print("Failed to open /dev/comedi0")
        return

    cmd = comedi_cmd_struct()
    chanlist = (ctypes.c_uint * 1)(0)
    
    # Start instantly
    cmd.start_src = TRIG_NOW
    cmd.start_arg = 0
    
    # Pace scans at exactly 10kHz (100,000 nanoseconds)
    cmd.scan_begin_src = TRIG_TIMER
    cmd.scan_begin_arg = 100000 
    
    # Convert instantly... but we set it to 1 nanosecond!
    # The hardware physically cannot do 1ns, so it will overwrite this 
    # with its absolute minimum physical limit during Test 3.
    cmd.convert_src = TRIG_TIMER
    cmd.convert_arg = 1 
    
    # End scan after 1 channel
    cmd.scan_end_src = TRIG_COUNT
    cmd.scan_end_arg = 1
    
    # Stop after 1000 total scans
    cmd.stop_src = TRIG_COUNT
    cmd.stop_arg = 1000
    
    cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
    cmd.chanlist_len = 1

    print(f"--- INITIAL REQUEST ---")
    print(f"Scan Pacing: {cmd.scan_begin_arg} ns")
    print(f"Conversion:  {cmd.convert_arg} ns (Deliberately impossible)")

    # Run test 1: Check Sources
    lib.comedi_command_test(dev, ctypes.byref(cmd))
    # Run test 2: Check Exclusivity
    lib.comedi_command_test(dev, ctypes.byref(cmd))
    # Run test 3: ARGUMENT MUTATION (The hardware enforces its limits here)
    res3 = lib.comedi_command_test(dev, ctypes.byref(cmd))
    # Run test 4: Final minor tweaks/rounding
    res4 = lib.comedi_command_test(dev, ctypes.byref(cmd))

    print(f"\n--- HARDWARE NEGOTIATED LIMITS ---")
    print(f"Scan Pacing: {cmd.scan_begin_arg} ns")
    print(f"Conversion:  {cmd.convert_arg} ns (<- THIS IS YOUR PHYSICAL SPEED LIMIT)")
    print(f"Final Readiness Code: {res4} (0 means perfect and ready to arm)")

    lib.comedi_close(dev)

if __name__ == "__main__":
    run_negotiation()
