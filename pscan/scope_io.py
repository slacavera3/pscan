"""The scope output format: the .dat, its .m sidecar, and nothing else.

This is to oscilloscopes what pscan/daq.py is to DAQ cards. A LeCroy reached
over VXI-11 and a PicoScope reached over USB produce the same files, in the
same layout, and the MATLAB side downstream has no idea which wrote them and
must never need to. So the format is decided here, once:

  <base>.dat            int16 ADC counts, one row per (segment, channel),
                        appended per trace
  <base>_trigtime.dat   float64 (trigger_time, trigger_offset) pairs, one row
                        per segment, appended per trace - only written by a
                        scope that can actually report them
  <base>.m              a MATLAB function returning the geometry and the
                        volts-per-count needed to read the .dat back

A driver's job is to produce ADC counts and say what they mean; it never
opens the output file. That is what keeps two different instruments
byte-compatible, and it is why the duplicated copy of this code that used to
live inside lecroy_driver.py is gone.

Everything is written through one background thread. Disk writes at
experiment speed are the single largest avoidable per-trace cost in pscan, and
the acquisition loop must not wait on them.
"""

import os
import queue
import threading

import numpy as np


class ScopeTraceWriter:
    """Owns one scope stream's files: the queue, the .dat and the .m.

    One instance per output base name, held by the driver that feeds it.
    """

    def __init__(self):
        # Geometry of the last trace actually recorded, so a later failed
        # acquisition can be padded to the same shape rather than skipped -
        # skipping would leave the .dat one trace short and map every later
        # trace to the wrong coordinate.
        self.last_trace_shape = None
        self.last_trigtime_shape = None

        # Traces on disk, placeholders included. main() reports this and
        # finalize_matlab_metadata() rewrites the .m from it.
        self.traces_written = 0

        # Everything finalize_matlab_metadata() needs to rewrite the .m with a
        # truthful trace count, captured on the first trace.
        self._m_context = None

        self.write_queue = queue.Queue()
        self.writer_thread = threading.Thread(
            target=self._async_writer, daemon=True
        )
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

    def flush(self):
        """Wait for every queued write to reach disk, then stop the thread.

        Called from a driver's disconnect(). Until this returns, bytes handed
        to the writer are not guaranteed to be in the file.
        """
        self.write_queue.put(None)
        if self.writer_thread.is_alive():
            self.writer_thread.join(timeout=3.0)

    def remember_geometry(self, trace_shape, trigtime_shape=None):
        """Record the shape a placeholder would have to match."""
        self.last_trace_shape = trace_shape
        self.last_trigtime_shape = trigtime_shape

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

    def record_trace(self, output_base_name, channels, metas, matrix,
                     is_first_trace, total_loops, h_interval, h_offset,
                     points, segments, sweeps, trigtime=None):
        """Append one trace and, on the first one, write the .m beside it.

        matrix is int16 ADC counts shaped (segments * len(channels), points),
        in the order (segment 0 ch 0, segment 0 ch 1, ... segment 1 ch 0 ...).
        metas carries one {'v_gain', 'v_offset'} per channel, in channel
        order: volts = v_gain * counts - v_offset, which is the LeCroy
        WAVEDESC convention every analysis script in the lab already reads.

        total_loops is the predicted trace count for the whole run, which is
        what the first .m claims; finalize_matlab_metadata() corrects it at
        teardown from what was actually recorded.
        """
        self.remember_geometry(
            matrix.shape, None if trigtime is None else trigtime.shape
        )

        dat_filename = f"{output_base_name}.dat"
        mode = "wb" if is_first_trace else "ab"

        # A re-run of the same config truncates rather than appending to the
        # file an earlier run left behind.
        if is_first_trace:
            self.traces_written = 0
        self.traces_written += 1

        # Toss the heavy binary payload into the background queue instantly.
        # tobytes() here and not in the writer thread: a driver may hand this
        # a view of a capture buffer it is about to reuse, so the copy has to
        # happen before this returns.
        self.write_queue.put((dat_filename, mode, matrix.tobytes()))

        # Trigger timing goes to a sidecar rather than into the .dat, so the
        # existing binary layout and MATLAB loader keep working.
        if trigtime is not None:
            self.write_queue.put((
                f"{output_base_name}_trigtime.dat", mode, trigtime.tobytes()
            ))

        if not is_first_trace:
            return

        summary_meta = {
            'h_interval': h_interval,
            'h_offset': h_offset,
            'points': points,
            'n_traces': total_loops * segments * len(channels),
        }
        trigtime_rows = 0 if trigtime is None else trigtime.shape[0]

        self._write_multi_matlab_metadata(
            output_base_name, channels, metas, summary_meta,
            segments=segments, sweeps=sweeps, trigtime_rows=trigtime_rows
        )

        # Keep only the fields the writer reads, so this does not pin the
        # per-channel sample or trigtime arrays for the whole run.
        self._m_context = {
            'base': output_base_name,
            'channels': list(channels),
            'metas': [
                {'v_gain': m['v_gain'], 'v_offset': m['v_offset']}
                for m in metas
            ],
            'summary': dict(summary_meta),
            'segments': segments,
            'sweeps': list(sweeps) if isinstance(sweeps, (list, tuple)) else sweeps,
            'trigtime_rows': trigtime_rows,
        }

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
        if isinstance(sweeps, (list, tuple)):
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
