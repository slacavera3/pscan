import vxi11
import struct
import numpy as np
import os
import math
import re
import time
import threading
import queue

class LeCroyScope:
    def __init__(self, ip_address):
        self.ip = ip_address
        self.instr = None
        self.required_triggers = 1
        self.active_sweeps_target = 1000
        self.last_trace_shape = None
        self.last_trigtime_shape = None
        self._last_sweeps_seen = None

        # Trace on/off state of each configured channel as found at the start
        # of the run, so disconnect() can restore it.
        self._prior_traces = {}

        # Everything finalize_matlab_metadata() needs to rewrite the .m with a
        # truthful trace count, captured on the first trace.
        self._m_context = None
        self.traces_written = 0

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
                print(f"\n[ERROR] Scope Async Write Failed for {filename}: {e}")
            finally:
                self.write_queue.task_done()

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
        self.write_queue.put(None)
        if self.writer_thread.is_alive():
            self.writer_thread.join(timeout=3.0)

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
        """Append a zero-filled trace matching the last successful geometry.

        Used when an acquisition fails mid-scan: writing nothing would leave
        the .dat one trace short, so every later trace would map to the wrong
        coordinate. Padding keeps trace index and coordinate aligned. The
        padded indices are logged to <base>_dropped.txt by the caller, since
        zeros are otherwise indistinguishable from real data.
        """
        if self.last_trace_shape is None:
            return False

        blank = np.zeros(self.last_trace_shape, dtype=np.int16)
        self.write_queue.put((f"{output_base_name}.dat", "ab", blank.tobytes()))

        # The trigtime sidecar is appended per trace, so it has to be padded
        # in step or it drifts out of alignment with the .dat.
        if self.last_trigtime_shape is not None:
            blank_tt = np.full(self.last_trigtime_shape, np.nan, dtype=np.float64)
            self.write_queue.put((
                f"{output_base_name}_trigtime.dat", "ab", blank_tt.tobytes()
            ))

        # A placeholder still occupies a trace slot in the file.
        self.traces_written += 1
        return True

    def finalize_matlab_metadata(self):
        """Rewrite the .m with the trace count actually recorded.

        The .m is written on the first trace from the predicted grid size, so
        an aborted or short run left it claiming more traces than the .dat
        holds - and the trigtime sidecar with it, since a loader derives the
        sidecar length from n_traces. Called at teardown however the run ends.
        Returns True only if it had to correct something.
        """
        ctx = self._m_context
        if ctx is None:
            return False

        actual = self.traces_written * ctx['segments'] * len(ctx['channels'])
        if actual == ctx['summary']['n_traces']:
            return False

        self._write_multi_matlab_metadata(
            ctx['base'], ctx['channels'], ctx['metas'],
            dict(ctx['summary'], n_traces=actual),
            segments=ctx['segments'], sweeps=ctx['sweeps'],
            trigtime_rows=ctx['trigtime_rows'],
        )

        # Record what is now on disk so a repeat call is a no-op.
        ctx['summary']['n_traces'] = actual
        return True

    def _write_multi_matlab_metadata(self, base_fn, channels, metas, sum_meta,
                                     segments, sweeps, trigtime_rows=0):
        m_filename = f"{base_fn}.m"
        dat_filename = f"{base_fn}.dat"
        func_name = os.path.basename(base_fn).replace('-', '_')

        vgain_str = ",".join([f"{m['v_gain']:e}" for m in metas])
        voff_str = ",".join([f"{m['v_offset']:e}" for m in metas])
        ch_str = ",".join([f"'{ch}'" for ch in channels])

        # Safely parse the sweeps into a comma-separated string for the MATLAB array
        if isinstance(sweeps, list):
            sweeps_str = ",".join(map(str, sweeps))
        else:
            sweeps_str = str(sweeps)

        # Empty name means no sidecar was written, which is the normal case
        # for single-shot / real-time acquisitions.
        tt_name = f"{base_fn}_trigtime.dat" if trigtime_rows else ""

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
scp.n_averages=[{sweeps_str}];
scp.multitraces={segments};
scp.channels={{{ch_str}}};
scp.trigtimename='{tt_name}';
scp.trigtime_rows_per_trace={trigtime_rows};
scp.trigtime_cols=2;
"""
        with open(m_filename, "w") as f:
            f.write(m_content)

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
        binary_bytes = flat_matrix.tobytes()

        sweeps_done = [m['sweeps_done'] for m in meta_list]
        self._report_sweep_shortfall(channels, sweeps_done)

        # Channel shapes are known to agree by this point, so the trigtime
        # array is the same for every channel of this acquisition and one
        # copy is enough.
        trigtime = meta_list[0]['trigtime']

        # Remember the geometry so a later failed trace can be padded rather
        # than skipped, which would shift every subsequent trace in the file.
        self.last_trace_shape = flat_matrix.shape
        self.last_trigtime_shape = None if trigtime is None else trigtime.shape

        if save_for_matlab:
            dat_filename = f"{output_base_name}.dat"
            mode = "wb" if is_first_trace else "ab"

            if is_first_trace:
                self.traces_written = 0
            self.traces_written += 1

            # Toss the heavy binary payload into the background queue instantly
            self.write_queue.put((dat_filename, mode, binary_bytes))

            # Trigger timing goes to a sidecar rather than into the .dat, so
            # the existing binary layout and MATLAB loader keep working.
            if trigtime is not None:
                self.write_queue.put((
                    f"{output_base_name}_trigtime.dat", mode, trigtime.tobytes()
                ))

            if is_first_trace:
                fm = meta_list[0]
                tot_traces = total_loops * fm['n_traces'] * len(channels)
                
                summary_meta = {
                    'h_interval': fm['h_interval'],
                    'h_offset': fm['h_offset'],
                    'points': fm['points'],
                    'n_traces': tot_traces
                }
                
                trigtime_rows = 0 if trigtime is None else trigtime.shape[0]

                # Sweep counts now come from SWEEPS_PER_ACQ in the waveform
                # itself rather than from the configured F1:DEF? target.
                self._write_multi_matlab_metadata(
                    output_base_name, channels, meta_list, summary_meta,
                    segments=fm['n_traces'], sweeps=sweeps_done,
                    trigtime_rows=trigtime_rows
                )

                # Keep only the fields the writer reads, so this does not pin
                # the per-channel trigtime arrays for the whole run.
                self._m_context = {
                    'base': output_base_name,
                    'channels': list(channels),
                    'metas': [
                        {'v_gain': m['v_gain'], 'v_offset': m['v_offset']}
                        for m in meta_list
                    ],
                    'summary': dict(summary_meta),
                    'segments': fm['n_traces'],
                    'sweeps': list(sweeps_done),
                    'trigtime_rows': trigtime_rows,
                }
                
        return True
