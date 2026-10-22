import ctypes
import ctypes.util
import struct
import sys
import os
import select
import numpy as np
import threading
import queue

# Comedi Trigger Constants
TRIG_NOW   = 0x0002
TRIG_TIMER = 0x0010
TRIG_COUNT = 0x0020

# Used only when the card cannot report its own calibration. These were the
# hardcoded values the conversion used unconditionally, which is wrong for
# any card whose resolution or range list differs - a 12-bit card packing
# samples into 16 bits read 16x low.
FALLBACK_MAXDATA = 65535
FALLBACK_RANGES = {0: 10.0, 1: 5.0, 2: 1.0, 3: 0.2}


class comedi_range(ctypes.Structure):
    """What comedi_get_range() points at."""
    _fields_ = [
        ("min", ctypes.c_double),
        ("max", ctypes.c_double),
        ("unit", ctypes.c_uint),
    ]


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
        except OSError as e:
            # Raise rather than sys.exit, so main()'s teardown still runs for
            # anything already connected.
            raise RuntimeError("Could not load libcomedi.") from e

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

        # Calibration queries, so the volts conversion comes from the card
        # rather than from constants in this file.
        self.libcomedi.comedi_get_maxdata.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint
        ]
        self.libcomedi.comedi_get_maxdata.restype = ctypes.c_uint
        self.libcomedi.comedi_get_range.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
        ]
        self.libcomedi.comedi_get_range.restype = ctypes.POINTER(comedi_range)

        self.device_node = device_node
        self.dev_ptr = None
        self.fd = None
        self._warned = set()

        # Async Writer Thread Setup
        self.write_queue = queue.Queue()
        self.writer_thread = threading.Thread(target=self._async_writer, daemon=True)
        self.writer_thread.start()

    def _async_writer(self):
        """Background thread that safely drains the disk write queue."""
        while True:
            task = self.write_queue.get()
            if task is None:  # Poison pill to gracefully shutdown
                self.write_queue.task_done()
                break
            
            filename, mode, data = task
            try:
                with open(filename, mode) as f:
                    f.write(data)
            except Exception as e:
                print(f"\n[ERROR] Async Write Failed for {filename}: {e}")
            finally:
                self.write_queue.task_done()

    def connect(self):
        if self.dev_ptr is None:
            self.dev_ptr = self.libcomedi.comedi_open(self.device_node)
            if not self.dev_ptr:
                raise RuntimeError(f"Failed to open NI card at {self.device_node.decode()}")
            
            self.fd = self.libcomedi.comedi_fileno(self.dev_ptr)
            
            # Immediately clear any lingering commands from a previously aborted run
            self.libcomedi.comedi_cancel(self.dev_ptr, 0)

    def disconnect(self):
        # 1. Flush the writer queue safely (wait for all pending disk writes to finish)
        self.write_queue.put(None)
        if self.writer_thread.is_alive():
            self.writer_thread.join(timeout=3.0)

        # 2. Cleanup hardware
        if self.dev_ptr:
            self.libcomedi.comedi_cancel(self.dev_ptr, 0)
            self.libcomedi.comedi_close(self.dev_ptr)
            self.dev_ptr = None
            self.fd = None

    def _warn_once(self, key, message):
        """Print a calibration warning once rather than once per trace."""
        if key not in self._warned:
            self._warned.add(key)
            print(f"\n[WARNING] A2D: {message}")

    def _calibration(self, subdevice, channel, range_val):
        """Ask the card for its full-scale count and the range's voltages.

        Returns (maxdata, v_min, v_max). Falls back to the old hardcoded
        constants, with a warning, if either query is unavailable - so a card
        that does not support them still works exactly as before.
        """
        maxdata = None
        try:
            value = self.libcomedi.comedi_get_maxdata(
                self.dev_ptr, subdevice, channel
            )
            if value > 0:
                maxdata = float(value)
        except Exception:
            pass

        v_min = v_max = None
        try:
            rng = self.libcomedi.comedi_get_range(
                self.dev_ptr, subdevice, channel, range_val
            )
            if rng:
                v_min, v_max = float(rng.contents.min), float(rng.contents.max)
        except Exception:
            pass

        if maxdata is None:
            maxdata = float(FALLBACK_MAXDATA)
            self._warn_once(
                'maxdata',
                f"could not read maxdata from the card; assuming "
                f"{FALLBACK_MAXDATA} counts at full scale. Voltages will be "
                f"wrong if this card is not 16-bit."
            )

        if v_min is None or v_max is None or v_max <= v_min:
            v_max = FALLBACK_RANGES.get(range_val, 10.0)
            v_min = -v_max
            self._warn_once(
                'range',
                f"could not read range {range_val} from the card; assuming "
                f"{v_min:g}..{v_max:g} V."
            )

        return maxdata, v_min, v_max

    def _to_volts(self, raw_array, maxdata, v_min, v_max):
        """comedi_to_phys()'s conversion, vectorised over a whole trace.

        Interpolating between the range's own min and max also gets unipolar
        ranges right; the old code assumed every range was symmetric about
        zero.
        """
        return v_min + (raw_array / maxdata) * (v_max - v_min)

    def write_placeholder_trace(self, output_filename, n_samples):
        """Append a NaN-filled block so a failed trace does not shift the file.

        Skipping a failed acquisition would leave the .d file one block short
        and misalign every later trace against its coordinate. NaN is used
        rather than zero so a padded block is obvious in MATLAB.
        """
        blank = np.full(n_samples, np.nan, dtype=np.float32)
        self.write_queue.put((output_filename, "ab", blank.tobytes()))
        return True

    def acquire_a2d(self, channel, range_val, n_samples, sample_rate, output_filename, is_first_trace=True):
        if not self.dev_ptr:
            print("[ERROR] A2D Driver not connected. Call connect() first.")
            return False

        subdevice = 0
        chan_spec = channel | (range_val << 16) | (0 << 24)
        chanlist = (ctypes.c_uint * 1)(chan_spec)
        ns_interval = int(1e9 / sample_rate) if sample_rate > 0 else 100000

        cmd = comedi_cmd_struct()
        
        if self.libcomedi.comedi_get_cmd_generic_timed(self.dev_ptr, subdevice, ctypes.byref(cmd), 1, ns_interval) < 0:
            print("[ERROR] Failed to get generic timed template.")
            return False

        cmd.start_src = TRIG_NOW
        cmd.start_arg = 0
        cmd.scan_begin_src = TRIG_TIMER
        cmd.scan_begin_arg = ns_interval
        cmd.convert_src = TRIG_TIMER  
        cmd.convert_arg = 800         
        cmd.scan_end_src = TRIG_COUNT
        cmd.scan_end_arg = 1
        cmd.stop_src = TRIG_COUNT
        cmd.stop_arg = n_samples

        cmd.chanlist = ctypes.cast(chanlist, ctypes.POINTER(ctypes.c_uint))
        cmd.chanlist_len = 1

        self.libcomedi.comedi_cancel(self.dev_ptr, subdevice)

        for _ in range(4):
            self.libcomedi.comedi_command_test(self.dev_ptr, ctypes.byref(cmd))

        if self.libcomedi.comedi_command(self.dev_ptr, ctypes.byref(cmd)) < 0:
            print("[ERROR] Hardware rejected the negotiated DMA command.")
            return False

        bytes_to_read = n_samples * 2
        raw_bytes = b""
        
        try:
            while len(raw_bytes) < bytes_to_read:
                r, _, _ = select.select([self.fd], [], [], 2.0)
                
                if not r:
                    self.libcomedi.comedi_poll(self.dev_ptr, subdevice)
                    r_retry, _, _ = select.select([self.fd], [], [], 0.5)
                    if not r_retry:
                        print(f"[ERROR] DMA Stream Timeout. Read {len(raw_bytes)}/{bytes_to_read} bytes.")
                        break
                    
                chunk = os.read(self.fd, bytes_to_read - len(raw_bytes))
                if not chunk: 
                    break
                raw_bytes += chunk
                
        except KeyboardInterrupt:
            print("\n[WARNING] Keyboard Interrupt detected! Cancelling NI DMA stream...")
            raise  # Re-raise so main.py can catch it and shutdown the stage/scope too

        finally:
            # This guarantees the hardware state is scrubbed clean, regardless of errors or success
            self.libcomedi.comedi_cancel(self.dev_ptr, subdevice)

        if len(raw_bytes) < bytes_to_read:
            return False

        # Math Vectorization, using the card's own calibration. This is
        # comedi_to_phys()'s conversion, which also gets unipolar ranges
        # right - the old symmetric +/-v_max assumption did not.
        maxdata, v_min, v_max = self._calibration(subdevice, channel, range_val)
        raw_array = np.frombuffer(raw_bytes, dtype=np.uint16)
        float_array = self._to_volts(raw_array, maxdata, v_min, v_max)
        packed_data = float_array.astype(np.float32).tobytes()

        # Hand off disk I/O to the background thread instantly
        mode = "wb" if is_first_trace else "ab"
        self.write_queue.put((output_filename, mode, packed_data))

        return True
