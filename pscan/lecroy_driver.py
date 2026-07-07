import vxi11
import struct
import numpy as np
import os
import math
import re
import time

class LeCroyScope:
    def __init__(self, ip_address):
        self.ip = ip_address
        self.instr = None
        self.required_triggers = 1  
        self.active_sweeps_target = 1000  

    def connect(self):
        if self.instr is None:
            self.instr = vxi11.Instrument(self.ip)
            self.instr.timeout = 30  
            self.instr.write("CHDR OFF")
            self.instr.write("CFMT DEF9,WORD,BIN")

    def disconnect(self):
        if self.instr is not None:
            self.instr.write("TRMD AUTO") 
            self.instr.close()
            self.instr = None

    def _write_multi_matlab_metadata(self, base_fn, channels, metas, sum_meta, segments, sweeps):
        m_filename = f"{base_fn}.m"
        dat_filename = f"{base_fn}.dat"
        func_name = os.path.basename(base_fn).replace('-', '_')
        
        vgain_str = ",".join([f"{m['v_gain']:e}" for m in metas])
        voff_str = ",".join([f"{m['v_offset']:e}" for m in metas])
        ch_str = ",".join([f"'{ch}'" for ch in channels])
        
        m_content = f"""% scope metadata file version 1.0 June 2026
function scp={func_name}()
scp.version=1.0;
scp.hint={sum_meta['h_interval']:e};
scp.hoff={sum_meta['h_offset']:e};
scp.vgain=[{vgain_str}];
scp.voff=[{voff_str}];
scp.points_per_trace={sum_meta['points']};
scp.n_traces={sum_meta['n_traces']};
scp.n_channels={len(channels)};
scp.format=2;
scp.dataname='{dat_filename}';
scp.n_averages={sweeps};
scp.multitraces={segments};
scp.channels={{{ch_str}}};
"""
        with open(m_filename, "w") as f:
            f.write(m_content)

    def acquire_multi_channel(self, channels, output_base_name, 
                              save_for_matlab=True, is_first_trace=True, 
                              total_loops=1, sweeps=None):
        if self.instr is None:
            raise RuntimeError("Scope not connected. Call connect() first.")
            
        try:
            # =================================================================
            # SETUP BLOCK: SCPI TRUTH EXTRACTION AND OVERRIDE
            # =================================================================
            if is_first_trace:
                self.instr.write("CHDR SHORT")
                
                # 1. Get the Hardcoded Timebase Segments (Burst Size)
                self.instr.write("SEQ?")
                seq_resp = self.instr.read().strip()
                timebase_segments = 1
                if "ON" in seq_resp.upper():
                    match = re.search(r'ON,\s*(\d+)', seq_resp.upper())
                    if match:
                        timebase_segments = int(match.group(1))

                # 2. Get the current Math Sweeps directly from SCPI
                self.instr.write("F1:DEF?")
                scpi_resp = self.instr.read().strip()
                
                current_hw_sweeps = 1000
                match = re.search(r'SWEEPS\s*,\s*(\d+)', scpi_resp, re.IGNORECASE)
                if match:
                    current_hw_sweeps = int(match.group(1))
                
                # 3. Determine the Target Sweeps (IGNORING parser -1 defaults)
                if sweeps is not None and int(sweeps) > 0:
                    # Confile is King
                    self.active_sweeps_target = int(sweeps)
                    print(f"Scope Status: Confile overrides and dictates {self.active_sweeps_target} total sweeps.")
                    
                    if current_hw_sweeps != self.active_sweeps_target:
                        new_scpi_cmd = re.sub(r'(SWEEPS\s*,\s*)\d+', f'\\g<1>{self.active_sweeps_target}', scpi_resp, flags=re.IGNORECASE)
                        if not new_scpi_cmd.upper().startswith("F1:DEF"):
                            new_scpi_cmd = f"F1:DEF {new_scpi_cmd}"
                        self.instr.write(new_scpi_cmd)
                        time.sleep(0.5) 
                else:
                    # Scope UI is King (Catches None and -1)
                    self.active_sweeps_target = current_hw_sweeps
                    print(f"Scope Status: Reading UI. Scope dictates {self.active_sweeps_target} total sweeps.")

                self.instr.write("CHDR OFF")

                # 4. Calculate Loops
                self.required_triggers = max(1, math.ceil(self.active_sweeps_target / timebase_segments))
                print(f" -> Timebase burst size is {timebase_segments} segments.")
                print(f" -> Python will loop {self.required_triggers} time(s) per pixel.")

            # =================================================================
            # ACQUISITION BLOCK: LOOP THE BURSTS
            # =================================================================
            self.instr.write("CLSW")
            for _ in range(self.required_triggers):
                self.instr.write("TRMD SINGLE")
                self.instr.write("ARM; WAIT; *OPC?")
                self.instr.read()
            
        except Exception as e:
            print(f"\nTrigger Timeout Error: {e}")
            return False

        meta_list = []
        adc_data_list = []
        
        for channel in channels:
            try:
                self.instr.write(f"{channel}:WF?")
                raw_data = self.instr.read_raw()
                
                wd_idx = raw_data.find(b'WAVEDESC')
                if wd_idx == -1: return False
                    
                trc = raw_data[wd_idx:]
                fmt = '<' if struct.unpack_from('h', trc, 32)[0] == 1 else '>'
                comm_type = struct.unpack_from(fmt + 'h', trc, 34)[0]
                dtype = np.dtype(fmt + 'i2') if comm_type == 1 else np.dtype('i1')

                offs = [struct.unpack_from(fmt + 'i', trc, o)[0] for o in [36, 40, 44, 48, 52, 56]]
                data_offset = sum(offs)

                segments = struct.unpack_from(fmt + 'i', trc, 144)[0]
                t_pts = struct.unpack_from(fmt + 'i', trc, 116)[0]
                
                if t_pts == 0: return False
                
                pts_per_seg = t_pts // segments if segments > 0 else t_pts

                v_gain = struct.unpack_from(fmt + 'f', trc, 156)[0]
                v_off = struct.unpack_from(fmt + 'f', trc, 160)[0]
                h_int = struct.unpack_from(fmt + 'f', trc, 176)[0]
                h_off = struct.unpack_from(fmt + 'd', trc, 180)[0]
                
                meta_list.append({
                    'v_gain': v_gain, 'v_offset': v_off,
                    'h_interval': h_int, 'h_offset': h_off,
                    'points': pts_per_seg, 'n_traces': segments
                })
                
                raw_adc = np.frombuffer(trc, dtype=dtype, offset=data_offset, count=t_pts)
                raw_adc = raw_adc.astype(np.int16).reshape(segments, pts_per_seg)
                adc_data_list.append(raw_adc)

            except Exception as e:
                print(f"Error acquiring {channel}: {e}")
                return False
        
        if not adc_data_list: return False

        stacked = np.stack(adc_data_list, axis=1)
        flat_matrix = stacked.reshape(-1, pts_per_seg)
        
        if save_for_matlab:
            mode = "wb" if is_first_trace else "ab"
            with open(f"{output_base_name}.dat", mode) as f:
                f.write(flat_matrix.tobytes())
                
            if is_first_trace:
                fm = meta_list[0]
                tot_traces = total_loops * fm['n_traces'] * len(channels)
                
                self._write_multi_matlab_metadata(
                    output_base_name, channels, meta_list, 
                    {'h_interval': fm['h_interval'], 'h_offset': fm['h_offset'], 
                     'points': fm['points'], 'n_traces': tot_traces},
                    segments=fm['n_traces'], sweeps=self.active_sweeps_target
                )
                
        return True
