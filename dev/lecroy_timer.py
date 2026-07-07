import vxi11
import struct
import numpy as np
import os
import math
import re
import time

class LeCroyTimer:
    def __init__(self, ip_address):
        self.ip = ip_address
        self.instr = None
        self.required_triggers = 1  
        self.active_sweeps_target = 1000  

    def connect(self):
        t0 = time.perf_counter()
        if self.instr is None:
            self.instr = vxi11.Instrument(self.ip)
            self.instr.timeout = 30  
            self.instr.write("CHDR OFF")
            self.instr.write("CFMT DEF9,WORD,BIN")
        t1 = time.perf_counter()
        print(f"[Timer] Network Connection & Init : {t1-t0:.4f} seconds")

    def disconnect(self):
        if self.instr is not None:
            self.instr.write("TRMD AUTO") 
            self.instr.close()
            self.instr = None

    def acquire_multi_channel_timed(self, channels, output_base_name, sweeps=1000):
        if self.instr is None:
            raise RuntimeError("Scope not connected.")
            
        print(f"\n--- Profiling Acquisition ({sweeps} Sweeps Target) ---")
        total_start = time.perf_counter()
        
        try:
            # =================================================================
            # 1. SETUP BLOCK
            # =================================================================
            t0_setup = time.perf_counter()
            self.instr.write("CHDR SHORT")
            
            self.instr.write("SEQ?")
            seq_resp = self.instr.read().strip()
            timebase_segments = 1
            if "ON" in seq_resp.upper():
                match = re.search(r'ON,\s*(\d+)', seq_resp.upper())
                if match: timebase_segments = int(match.group(1))

            self.instr.write("F1:DEF?")
            scpi_resp = self.instr.read().strip()
            
            current_hw_sweeps = 1000
            match = re.search(r'SWEEPS\s*,\s*(\d+)', scpi_resp, re.IGNORECASE)
            if match: current_hw_sweeps = int(match.group(1))
            
            self.active_sweeps_target = int(sweeps)
            if current_hw_sweeps != self.active_sweeps_target:
                new_scpi_cmd = re.sub(r'(SWEEPS\s*,\s*)\d+', f'\\g<1>{self.active_sweeps_target}', scpi_resp, flags=re.IGNORECASE)
                if not new_scpi_cmd.upper().startswith("F1:DEF"):
                    new_scpi_cmd = f"F1:DEF {new_scpi_cmd}"
                self.instr.write(new_scpi_cmd)
                time.sleep(0.5) 
                
            self.instr.write("CHDR OFF")
            self.required_triggers = max(1, math.ceil(self.active_sweeps_target / timebase_segments))
            t1_setup = time.perf_counter()
            print(f"[Timer] SCPI Query & UI Setup   : {t1_setup-t0_setup:.4f} seconds")

            # =================================================================
            # 2. ACQUISITION BLOCK (The Hardware Block)
            # =================================================================
            t0_acq = time.perf_counter()
            self.instr.write("CLSW")
            for loop_idx in range(self.required_triggers):
                t_loop_start = time.perf_counter()
                self.instr.write("TRMD SINGLE")
                self.instr.write("ARM; WAIT; *OPC?")
                self.instr.read() # This blocks until the scope says it's physically done
                t_loop_end = time.perf_counter()
                print(f"[Timer] -> Hardware Burst {loop_idx+1}/{self.required_triggers} : {t_loop_end-t_loop_start:.4f} seconds")
            t1_acq = time.perf_counter()
            print(f"[Timer] Total Hardware Acq Time : {t1_acq-t0_acq:.4f} seconds")
            
        except Exception as e:
            print(f"\nTrigger Timeout Error: {e}")
            return False

        # =================================================================
        # 3. NETWORK DOWNLOAD BLOCK
        # =================================================================
        t0_net = time.perf_counter()
        meta_list = []
        adc_data_list = []
        
        for channel in channels:
            t_ch_start = time.perf_counter()
            self.instr.write(f"{channel}:WF?")
            raw_data = self.instr.read_raw()
            t_ch_end = time.perf_counter()
            print(f"[Timer] -> Ethernet DL ({channel})    : {t_ch_end-t_ch_start:.4f} seconds ({len(raw_data)/1024/1024:.2f} MB)")
            
            # Parsing Block
            t_parse_start = time.perf_counter()
            wd_idx = raw_data.find(b'WAVEDESC')
            trc = raw_data[wd_idx:]
            fmt = '<' if struct.unpack_from('h', trc, 32)[0] == 1 else '>'
            comm_type = struct.unpack_from(fmt + 'h', trc, 34)[0]
            dtype = np.dtype(fmt + 'i2') if comm_type == 1 else np.dtype('i1')

            offs = [struct.unpack_from(fmt + 'i', trc, o)[0] for o in [36, 40, 44, 48, 52, 56]]
            data_offset = sum(offs)
            segments = struct.unpack_from(fmt + 'i', trc, 144)[0]
            t_pts = struct.unpack_from(fmt + 'i', trc, 116)[0]
            pts_per_seg = t_pts // segments if segments > 0 else t_pts

            raw_adc = np.frombuffer(trc, dtype=dtype, offset=data_offset, count=t_pts)
            raw_adc = raw_adc.astype(np.int16).reshape(segments, pts_per_seg)
            adc_data_list.append(raw_adc)
            t_parse_end = time.perf_counter()
            print(f"[Timer] -> Binary Parse ({channel})   : {t_parse_end-t_parse_start:.4f} seconds")

        t1_net = time.perf_counter()

        # =================================================================
        # 4. DISK WRITE BLOCK
        # =================================================================
        t0_disk = time.perf_counter()
        stacked = np.stack(adc_data_list, axis=1)
        flat_matrix = stacked.reshape(-1, pts_per_seg)
        
        with open(f"{output_base_name}.dat", "wb") as f:
            f.write(flat_matrix.tobytes())
            
        t1_disk = time.perf_counter()
        print(f"[Timer] Disk Write (.dat file)  : {t1_disk-t0_disk:.4f} seconds")
        
        total_end = time.perf_counter()
        print(f"\n[Timer] TOTAL TRACE PIPELINE    : {total_end-total_start:.4f} seconds")
        return True

if __name__ == "__main__":
    ip = "192.168.74.2"  # Change if necessary
    timer_scope = LeCroyTimer(ip)
    timer_scope.connect()
    
    # Simulating a 1000 sweep trace just like your config file
    timer_scope.acquire_multi_channel_timed(
        channels=['F1'], 
        output_base_name="timing_test_output", 
        sweeps=1000
    )
    
    timer_scope.disconnect()
