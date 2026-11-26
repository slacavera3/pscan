import vxi11
import struct
import numpy as np
import math
import re
import time

from pscan.scope_io import ScopeTraceWriter

class LeCroyScope:
    def __init__(self, ip_address):
        self.ip = ip_address
        self.instr = None
        self.required_triggers = 1
        self.active_sweeps_target = 1000
        self._last_sweeps_seen = None

        # Trace on/off state of each configured channel as found at the start
        # of the run, so disconnect() can restore it.
        self._prior_traces = {}

        # The .dat, its trigtime sidecar and the .m are written by
        # pscan.scope_io, which is the one place that format is decided - a
        # USB PicoScope trace and a LeCroy trace have to be byte-compatible,
        # and they cannot be if each driver carries its own copy of the
        # writer. This driver's job is ADC counts plus what they mean.
        self._writer = ScopeTraceWriter()

    # The orchestrator reads these off the scope object, and so does the
    # placeholder path, so they stay on this class and defer to the writer
    # that actually knows what is on disk.
    @property
    def traces_written(self):
        return self._writer.traces_written

    @property
    def last_trace_shape(self):
        return self._writer.last_trace_shape

    @property
    def last_trigtime_shape(self):
        return self._writer.last_trigtime_shape

    def connect(self):
        if self.instr is None:
            self.instr = vxi11.Instrument(self.ip)
            self.instr.timeout = 30  
            self.instr.write("CHDR OFF")
            self.instr.write("CFMT DEF9,WORD,BIN")

            # THE FIX: Force the scope to send all points (NP=0)
            # with zero sparsing (SP=0) starting at index 0 (FP=0)
            self.instr.write("WFSU SP,0,NP,0,FP,0,SN,0")

    def disconnect(self):
        # 1. Flush the writer queue safely (wait for all pending disk writes to finish)
        self._writer.flush()

        # 2. Cleanup hardware
        if self.instr is not None:
            self.instr.write("TRMD AUTO")

            # Put back any channel we switched on for this run.
            for ch, was_on in self._prior_traces.items():
                if not was_on:
                    try:
                        self.instr.write(f"{ch}:TRA OFF")
                    except Exception as e:
                        print(f"\n[WARNING] Could not restore {ch} trace state: {e}")
            self._prior_traces = {}

            self.instr.close()
            self.instr = None

    def write_placeholder_trace(self, output_base_name):
        """Append a zero-filled trace matching the last successful geometry."""
        return self._writer.write_placeholder_trace(output_base_name)

    def finalize_matlab_metadata(self):
        """Rewrite the .m with the trace count actually recorded."""
        return self._writer.finalize_matlab_metadata()

    def _report_sweep_shortfall(self, channels, sweeps_done):
        """Warn when the scope accumulated fewer sweeps than were asked for.

        Printed once per distinct result rather than once per trace, so a
        steady shortfall says so a single time but a new one still surfaces.
        """
        actual = tuple(sweeps_done)
        if actual == self._last_sweeps_seen:
            return
        self._last_sweeps_seen = actual

        targets = getattr(self, 'channel_sweeps', None)
        if not targets or len(targets) != len(actual):
            return

        short = [
            (ch, want, got)
            for ch, want, got in zip(channels, targets, actual)
            # A sweep or two either side is just where the burst landed.
            if got < want - 2
        ]
        if short:
            detail = ", ".join(f"{ch}: {got}/{want}" for ch, want, got in short)
            print(
                f"\n[WARNING] Scope accumulated fewer sweeps than requested "
                f"({detail}). scp.n_averages records what was actually "
                f"achieved, so averaging is weaker than configured here."
            )

    def acquire_multi_channel(self, channels, output_base_name, 
                              save_for_matlab=True, is_first_trace=True, 
                              total_loops=1, sweeps=None):
        if self.instr is None:
            raise RuntimeError("Scope not connected. Call connect() first.")
            
        try:
            # =================================================================
            # SETUP BLOCK: PURE SCPI TRUTH EXTRACTION
            # =================================================================
            if is_first_trace:
                self.instr.write("CHDR SHORT")
                
                # 1. SCPI Timebase Segments (Burst Size)
                self.instr.write("SEQ?")
                seq_resp = self.instr.read().strip()
                timebase_segments = 1
                if "ON" in seq_resp.upper():
                    match = re.search(r'ON,\s*(\d+)', seq_resp.upper())
                    if match:
                        timebase_segments = int(match.group(1))

                # 2. Parse Conf file sweeps into a list (handles "1000" or "1000, 100")
                target_sweeps_list = []
                if sweeps is not None:
                    # Convert to string to safely split commas, then cast to ints
                    target_sweeps_list = [int(s.strip()) for s in str(sweeps).split(',') if s.strip().isdigit()]

                # 3. Dynamic SCPI Math Sweeps parsing
                self.channel_sweeps = []
                
                for i, ch in enumerate(channels):
                    is_math = ch.upper().startswith('F')
                    ch_target = 1000 # fallback default

                    # A channel whose trace is off still answers :WF? with a
                    # WAVEDESC, but the data behind it is stale rather than
                    # live, so the run records a plausible-looking file that
                    # never saw the acquisition. Turn every configured channel
                    # on, remembering what it was so disconnect() can put the
                    # scope back the way the user left it.
                    try:
                        self.instr.write(f"{ch}:TRA?")
                        tra_resp = self.instr.read().strip().upper()
                        self._prior_traces[ch] = 'OFF' not in tra_resp
                    except Exception as e:
                        # An unreadable state is not worth aborting the run
                        # over; skip the restore for this channel instead.
                        print(f"\n[WARNING] Could not read {ch} trace state: {e}")
                    self.instr.write(f"{ch}:TRA ON")

                    if is_math:
                        self.instr.write(f"{ch}:DEF?")
                        scpi_resp = self.instr.read().strip()
                        match = re.search(r'SWEEPS\s*,\s*(\d+)', scpi_resp, re.IGNORECASE)
                        if match:
                            ch_target = int(match.group(1))
                            
                        if i < len(target_sweeps_list) and target_sweeps_list[i] > 0:
                            req_target = target_sweeps_list[i]
                            if ch_target != req_target:
                                ch_target = req_target
                                new_scpi_cmd = re.sub(r'(SWEEPS\s*,\s*)\d+', f'\\g<1>{ch_target}', scpi_resp, flags=re.IGNORECASE)
                                if not new_scpi_cmd.upper().startswith(f"{ch}:DEF"):
                                    new_scpi_cmd = f"{ch}:DEF {new_scpi_cmd}"
                                self.instr.write(new_scpi_cmd)
                                time.sleep(0.1)
                    else:
                        # Physical channels (C1, C2) don't have SCPI sweep functions
                        if i < len(target_sweeps_list) and target_sweeps_list[i] > 0:
                            ch_target = target_sweeps_list[i]
                        else:
                            ch_target = 1 
                            
                    self.channel_sweeps.append(ch_target)
                    
                # Set the trigger loop count based on the channel that takes the longest
                self.active_sweeps_target = max(self.channel_sweeps) if self.channel_sweeps else 1000

                # 4. Calculate required arming loops
                self.required_triggers = max(1, math.ceil(self.active_sweeps_target / timebase_segments))
                
                # Warning if network lag is going to be brutal
                if timebase_segments == 1 and self.active_sweeps_target > 1:
                    print(f"\n[WARNING] Scope Sequence mode is OFF (or set to 1 segment)!")
                    print(f" -> Python must trigger the scope {self.required_triggers} times over the network. This will be incredibly slow!")

            # =================================================================
            # ACQUISITION BLOCK
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

                # WAVEDESC offset 32 is COMM_TYPE (0=int8, 1=int16) and offset
                # 34 is COMM_ORDER (0=HIFIRST/big-endian, 1=LOFIRST/little).
                # These were previously read the wrong way round, which only
                # worked because CFMT ...,WORD,... makes both fields 1. Under
                # CFMT ...,BYTE,... the old code chose big-endian, so the
                # block lengths below decoded to garbage and the frombuffer
                # offset ran past the buffer - every trace raised ValueError,
                # was swallowed as a plain acquisition failure, and silently
                # dropped. COMM_ORDER is itself written in the order it
                # describes, so its low byte identifies that order without
                # needing to know it first.
                fmt = '<' if trc[34] == 1 else '>'
                comm_type = struct.unpack_from(fmt + 'h', trc, 32)[0]
                dtype = np.dtype(fmt + 'i2') if comm_type == 1 else np.dtype('i1')

                # The six block lengths, in the order the blocks appear after
                # WAVEDESC: descriptor, user text, res_desc1, trigtime,
                # ris_time, res_array1. Their sum is where the sample data
                # starts, which is why they were being added up and otherwise
                # ignored.
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

                # SWEEPS_PER_ACQ: how many sweeps the scope actually
                # accumulated. The F1:DEF? regex above only reports the
                # configured target, so a trace cut short by a timeout still
                # claimed the full count.
                sweeps_done = struct.unpack_from(fmt + 'i', trc, 148)[0]

                # TRIGTIME_ARRAY sits between res_desc1 and ris_time, so it
                # starts after the first three blocks. In Sequence mode it
                # holds one (trigger_time, trigger_offset) float64 pair per
                # segment: trigger_time is that segment's trigger relative to
                # the first trigger of the acquisition, trigger_offset is the
                # sub-sample offset from trigger to first sample, which is
                # what lets per-segment trigger jitter be taken off the time
                # axis. Single-shot acquisitions carry no such block.
                trigtime = None
                if offs[3] >= 16:
                    n_tt = offs[3] // 16
                    tt_flat = np.frombuffer(
                        trc, dtype=np.dtype(fmt + 'f8'),
                        offset=sum(offs[0:3]), count=n_tt * 2
                    )
                    # astype() also normalises to native byte order, matching
                    # how the sample data is stored.
                    trigtime = tt_flat.reshape(n_tt, 2).astype(np.float64)

                meta_list.append({
                    'v_gain': v_gain, 'v_offset': v_off,
                    'h_interval': h_int, 'h_offset': h_off,
                    'points': pts_per_seg, 'n_traces': segments,
                    'sweeps_done': sweeps_done, 'trigtime': trigtime
                })

                raw_adc = np.frombuffer(trc, dtype=dtype, offset=data_offset, count=t_pts)
                raw_adc = raw_adc.astype(np.int16).reshape(segments, pts_per_seg)
                adc_data_list.append(raw_adc)

            except Exception as e:
                print(f"Error acquiring {channel}: {e}")
                return False
        
        if not adc_data_list: return False

        # np.stack needs every channel to agree on (segments, points). A math
        # trace averages across segments and comes back with subarray_count 1,
        # so pairing F1 with a raw channel in Sequence mode used to raise
        # ValueError out of this function and kill the whole scan.
        shapes = [arr.shape for arr in adc_data_list]
        if len(set(shapes)) > 1:
            detail = ", ".join(
                f"{ch}={s[0]}x{s[1]}" for ch, s in zip(channels, shapes)
            )
            print(f"\n[ERROR] Scope channel shapes disagree ({detail}).")
            print(" -> Math functions (F1/F2) collapse all segments into one,")
            print("    so they cannot be stacked with raw channels while")
            print("    Sequence mode is on. Put math and raw channels in")
            print("    separate 'action scope' blocks, or turn Sequence off.")
            return False

        pts_per_seg = shapes[0][1]
        stacked = np.stack(adc_data_list, axis=1)
        flat_matrix = stacked.reshape(-1, pts_per_seg)

        sweeps_done = [m['sweeps_done'] for m in meta_list]
        self._report_sweep_shortfall(channels, sweeps_done)

        # Channel shapes are known to agree by this point, so the trigtime
        # array is the same for every channel of this acquisition and one
        # copy is enough.
        trigtime = meta_list[0]['trigtime']

        if save_for_matlab:
            fm = meta_list[0]
            # Sweep counts come from SWEEPS_PER_ACQ in the waveform itself
            # rather than from the configured F1:DEF? target.
            self._writer.record_trace(
                output_base_name, channels, meta_list, flat_matrix,
                is_first_trace=is_first_trace, total_loops=total_loops,
                h_interval=fm['h_interval'], h_offset=fm['h_offset'],
                points=fm['points'], segments=fm['n_traces'],
                sweeps=sweeps_done, trigtime=trigtime,
            )
        else:
            # Still remember the geometry, so a later failed trace can be
            # padded rather than skipped - skipping would shift every
            # subsequent trace in the file.
            self._writer.remember_geometry(
                flat_matrix.shape,
                None if trigtime is None else trigtime.shape,
            )

        return True
