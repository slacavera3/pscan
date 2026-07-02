import vxi11
import struct
import numpy as np
import os
import re

class LeCroyScope:
    def __init__(self, ip_address):
        self.ip = ip_address
        self.instr = None

    def connect(self):
        if self.instr is None:
            self.instr = vxi11.Instrument(self.ip)
            self.instr.timeout = 30  
            self.instr.write("CHDR OFF")
            self.instr.write("CFMT DEF9,WORD,BIN")

    def disconnect(self):
        if self.instr is not None:
            self.instr.close()
            self.instr = None

    def _write_multi_matlab_metadata(self, base_fn, channels, metas, sum_meta):
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
scp.n_averages=-1;
scp.multitraces=1;
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
            # 1. CALCULATE REQUIRED TRIGGERS BASED ON SCRAPED NUMBER OF SEGMENTS
            # UNIVERSAL LECROY SWEEPS INJECTION
            triggers_required = 1
            if sweeps is not None:
                # Dynamically query the current segment count
                self.instr.write("SEQ?")
                seq_resp = self.instr.read().strip()
                
                segments = 1
                if "ON" in seq_resp.upper():
                    match = re.search(r'ON,\s*(\d+)', seq_resp.upper())
                    if match:
                        segments = int(match.group(1))
                
                for ch in channels:
                    ch_clean = ch.strip().upper()
                    if ch_clean.startswith('F'):
                        print(f" -> Forcing {sweeps} sweeps via VBS on {ch_clean}...")
                        self.instr.write(f'VBS "app.Math.{ch_clean}.Operator1.Sweeps={int(sweeps)}" ')
                        self.instr.write(f'VBS "app.Math.{ch_clean}.Operator2.Sweeps={int(sweeps)}" ')
                        self.instr.write(f'VBS "app.Math.{ch_clean}.Math.Average.Sweeps={int(sweeps)}" ')
                
                # Dynamically calculate triggers based on active segments
                if segments > 0:
                    triggers_required = max(1, int(sweeps // segments))

                # Force the VBS UI update with strict double quotes
                for ch in channels:
                    ch_clean = ch.strip().upper()
                    if ch_clean.startswith('F'):
                        self.instr.write(f"""VBS "app.Math.{ch_clean}.Operator1.Sweeps = {sweeps}" """)
                
                # If Sweeps = 5000, we must fire the 1000-segment sequence 5 times.
                triggers_required = max(1, int(sweeps // 1000))

            # 2. CLEAR SWEEPS ONCE
            self.instr.write("CLSW")
            
            # 3. LOOP ACQUISITION TO REACH TARGET SWEEPS
            for i in range(triggers_required):
                self.instr.write("TRMD SINGLE")
                self.instr.write("ARM; WAIT; *OPC?")
                self.instr.read()
            
        except Exception as e:
            print(f"\nTrigger Timeout Error: {e}")
            print(" -> Scope did not finish acquiring in time.")
            print(" -> Check if your laser/trigger source is firing!")
            return False

        meta_list = []
        adc_data_list = []
        
        for channel in channels:
            try:
                self.instr.write(f"{channel}:WF?")
                raw_data = self.instr.read_raw()
                
                wd_idx = raw_data.find(b'WAVEDESC')
                if wd_idx == -1:
                    print(f"Error: WAVEDESC missing for {channel}.")
                    return False
                    
                trc = raw_data[wd_idx:]
                fmt = '<' if struct.unpack_from('h', trc, 32)[0] == 1 else '>'
                comm_type = struct.unpack_from(fmt + 'h', trc, 34)[0]
                
                dtype = np.dtype(fmt + 'i2') if comm_type == 1 else np.dtype('i1')

                offs = [
                    struct.unpack_from(fmt + 'i', trc, o)[0] 
                    for o in [36, 40, 44, 48, 52, 56]
                ]
                data_offset = sum(offs)

                segments = struct.unpack_from(fmt + 'i', trc, 144)[0]
                t_pts = struct.unpack_from(fmt + 'i', trc, 116)[0]
                
                if t_pts == 0:
                    print(f"\nError: Scope returned 0 points for {channel}.")
                    return False
                
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
                
                raw_adc = np.frombuffer(
                    trc, dtype=dtype, offset=data_offset, count=t_pts
                )
                raw_adc = raw_adc.astype(np.int16)
                raw_adc = raw_adc.reshape(segments, pts_per_seg)
                adc_data_list.append(raw_adc)

            except Exception as e:
                print(f"Error acquiring {channel}: {e}")
                return False
        
        if not adc_data_list:
            return False

        stacked = np.stack(adc_data_list, axis=1)
        flat_matrix = stacked.reshape(-1, pts_per_seg)
        binary_bytes = flat_matrix.tobytes()
        
        if save_for_matlab:
            dat_filename = f"{output_base_name}.dat"
            mode = "wb" if is_first_trace else "ab"
            with open(dat_filename, mode) as f:
                f.write(binary_bytes)
                
            if is_first_trace:
                fm = meta_list[0]
                tot_traces = total_loops * fm['n_traces'] * len(channels)
                
                summary_meta = {
                    'h_interval': fm['h_interval'],
                    'h_offset': fm['h_offset'],
                    'points': fm['points'],
                    'n_traces': tot_traces
                }
                self._write_multi_matlab_metadata(
                    output_base_name, channels, meta_list, summary_meta
                )
                
        return True
