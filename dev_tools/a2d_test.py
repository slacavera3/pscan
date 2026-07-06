import ctypes
import ctypes.util
import os
import select
import time

TRIG_INT   = 0x0080
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020
INSN_READ  = 1

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

class comedi_insn(ctypes.Structure):
    _fields_ = [("insn", ctypes.c_uint), ("n", ctypes.c_uint),
                ("data", ctypes.POINTER(ctypes.c_uint)),
                ("subdev", ctypes.c_uint), ("chanspec", ctypes.c_uint),
                ("unused", ctypes.c_uint * 3)]

def run_test():
    # Load libcomedi with use_errno=True so we can pull exact Linux crash codes
    lib = ctypes.CDLL(ctypes.util.find_library('comedi') or '/usr/lib/x86_64-linux-gnu/libcomedi.so.0', use_errno=True)
    
    lib.comedi_open.restype = ctypes.c_void_p
    dev = lib.comedi_open(b"/dev/comedi0")
    if not dev:
        print("Failed to open /dev/comedi0")
        return

    # Matches exactly what is in your .con file (Channel 0, Range 1)
    pack_val = (0 << 24) | (1 << 16) | 0

    print("\n--- TEST 1: The Baseline 'Slow' Loop ---")
    print("Executing 1,000 standard reads to measure pure hardware latency...")
    data_out = ctypes.c_uint()
    start_time = time.time()
    for i in range(1000):
        lib.comedi_data_read(dev, 0, 0, 1, 0, ctypes.byref(data_out))
    elapsed = time.time() - start_time
    print(f"Time taken: {elapsed:.5f} seconds.")
    if elapsed > 1.0:
        print("-> DIAGNOSTIC: The hardware registers are inherently slow. If DMA doesn't work, we likely have an IRQ motherboard issue.")

    print("\n--- TEST 2: Burst Read Error Extraction ---")
    n_samples = 1000
    data_array = (ctypes.c_uint * n_samples)()
    insn = comedi_insn(insn=INSN_READ, n=n_samples, data=ctypes.cast(data_array, ctypes.POINTER(ctypes.c_uint)), subdev=0, chanspec=pack_val)
    
    res = lib.comedi_do_insn(dev, ctypes.byref(insn))
    if res < 0:
        err = ctypes.get_errno()
        print(f"Burst Failed! Linux Error Code: {err} ({os.strerror(err)})")
        if err == 22: 
            print("-> DIAGNOSTIC: 'Invalid Argument' means the kernel explicitly bans array-bursts for this specific card model.")

    print("\n--- TEST 3: DMA with TRIG_INT (The Internal Pulse) ---")
    cmd = comedi_cmd_struct()
    lib.comedi_get_cmd_generic_timed(dev, 0, ctypes.byref(cmd), 1, 100000)

    cmd.start_src = TRIG_INT
    cmd.start_arg = 0
    cmd.scan_begin_src = TRIG_TIMER
    cmd.scan_begin_arg = 100000
    cmd.convert_src = TRIG_TIMER
    cmd.convert_arg = 800
    cmd.scan_end_src = TRIG_COUNT
    cmd.scan_end_arg = 1
    cmd.stop_src = TRIG_COUNT
    cmd.stop_arg = 1000

    chanlist = (ctypes.c_uint * 1)(pack_val)
    cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
    cmd.chanlist_len = 1

    lib.comedi_command_test(dev, ctypes.byref(cmd))
    lib.comedi_command_test(dev, ctypes.byref(cmd))

    if lib.comedi_command(dev, ctypes.byref(cmd)) < 0:
        print(f"Hardware rejected TRIG_INT DMA. Error: {os.strerror(ctypes.get_errno())}")
    else:
        print("Command armed successfully. Firing comedi_internal_trigger...")
        lib.comedi_internal_trigger(dev, 0, 0)
        
        fd = lib.comedi_fileno(dev)
        raw_bytes = b""
        start_time = time.time()
        
        while len(raw_bytes) < 2000:
            lib.comedi_poll(dev, 0)
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                chunk = os.read(fd, 2000 - len(raw_bytes))
                if chunk: raw_bytes += chunk
            
            if (time.time() - start_time) > 2.0:
                print(f"DMA TIMEOUT. Read {len(raw_bytes)} / 2000 bytes.")
                lib.comedi_cancel(dev, 0)
                break
        else:
            print(f"DMA SUCCESS! Read 2000 bytes in {time.time() - start_time:.4f} seconds.")

    lib.comedi_close(dev)

if __name__ == "__main__":
    run_test()
