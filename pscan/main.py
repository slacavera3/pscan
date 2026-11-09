import sys
import os
import glob
import time
import subprocess
import traceback
import cv2
import shutil
from pscan.tucsen_driver import TucsenCamera
import queue
import threading

# Stage settling. The encoder is the authority: wait_for_move() polls the
# position counter until the axis is inside SETTLE_TOLERANCE_MM, then naps
# SETTLE_DWELL_S for mechanical ringdown. This replaced a blind 0.2s sleep
# that was too short for the fast axis returning to the start of the next
# row, which meant the first point of every row after the first was acquired
# while the stage was still moving.
SETTLE_TOLERANCE_MM = 0.001
SETTLE_DWELL_S = 0.02
SETTLE_TIMEOUT_S = 15.0

# Extra time at the very first coordinate, for everything to stabilise.
INITIAL_SETTLE_S = 1.5

# An analog output is open-loop, so there is no position to poll and arrival
# is a declared settle time. That time now lives on the axis itself, set by
# the 'settle' key in an 'action d2a' block - a galvo mirror and a piezo do
# not settle alike, and the old single constant here could not tell them
# apart. 'action d2a_phidget' defaults to PHIDGET_SETTLE_S in config_parser,
# which is this constant plus the 0.05s the Phidget driver used to sleep
# internally, so an existing piezo config waits exactly as long as it did.

# Paid once when the USB capture opens, not at every coordinate.
WEBCAM_WARMUP_S = 0.5
WEBCAM_DISCARD_FRAMES = 3

# Consecutive failed traces before giving up: a hardware fault that is not
# going to fix itself should not quietly pad out a whole scan.
MAX_CONSECUTIVE_DROPS = 10


class AcquisitionAborted(RuntimeError):
    """Raised when a trace cannot be recorded without desynchronising the file."""


def note_dropped_trace(call_state, source):
    """Record a padded trace and abort if failures are no longer isolated."""
    idx = call_state['current_trace_idx']
    call_state['dropped'].append((idx, source))
    call_state['consecutive_drops'] += 1

    print(
        f"\n[WARNING] {source.upper()} acquisition failed at trace {idx}. "
        f"Wrote a placeholder so later traces stay aligned to their "
        f"coordinates."
    )

    if call_state['consecutive_drops'] >= MAX_CONSECUTIVE_DROPS:
        raise AcquisitionAborted(
            f"{MAX_CONSECUTIVE_DROPS} consecutive {source} failures - the "
            f"hardware does not appear to be triggering. Aborting so the "
            f"rest of the scan is not padded with placeholder data."
        )

# Create a thread-safe, capped memory queue. 
# maxsize=50 safely caps volatile RAM usage at ~400 MB.
write_queue = queue.Queue(maxsize=50)

def disk_writer_worker():
    while True:
        item = write_queue.get()
        if item is None:  # Poison pill to shut down
            break
        filename, data = item

        with open(filename, 'wb') as f:
            # memoryview() passes a zero-copy point.
            # Python's native f.write strictly drops the GIL!
            f.write(memoryview(data))

        write_queue.task_done()

# Start the dedicated background I/O thread
writer_thread = threading.Thread(target=disk_writer_worker, daemon=True)
writer_thread.start()

# Cross-platform module imports
from pscan.config_parser import (
    parse_flat_con_file, categorize_pipeline,
    compile_pipeline_list, compile_stage_parameters,
    validate_blocks, CORE_TYPES, APT_AXES, PIEZO_AXIS
)
from pscan.lecroy_driver import LeCroyScope
from pscan.stage_driver import ThorlabsStage
from pscan import daq

# OS-Specific Andor Import
try:
    if os.name == 'nt':
        from pscan.ixon_driver import IXonCamera
        ANDOR_AVAILABLE = True
    else:
        ANDOR_AVAILABLE = False
except ImportError:
    ANDOR_AVAILABLE = False

def run_pipeline_sequence(pipeline, scope, ixon, tucsen, base_filename,
                          total_a2d_blocks, global_total_traces,
                          call_state, silent_acq=False, timer_mode=False):
    """Run one pass of a pipeline.

    The a2d session is no longer passed in: each 'action a2d' block carries
    its own, resolved by open_a2d_sessions() before the scan starts, because
    two a2d blocks may now sit on two different cards.
    """
    is_first = (call_state['current_trace_idx'] == 0)
    
    for step_idx, action in enumerate(pipeline):
        act_type = action['type']
        p = action['params']
        
        if timer_mode: 
            t_mod_start = time.perf_counter()
        
        if act_type == 'script':
            if not silent_acq: 
                print(f" -> Executing system command: '{p['cmd']}'...")
            subprocess.run(
                p['cmd'], shell=True, check=True, 
                capture_output=True, text=True
            )
            
        elif act_type == 'delay':
            time.sleep(p['seconds'])
            
        elif act_type == 'webcam':
            idx = call_state['current_trace_idx']
            img_filename = (
                f"{base_filename}_"
                f"webcam_trace{idx}.jpg"
            )
            if not silent_acq:
                print(f" -> Grabbing USB frame -> {img_filename}...")

            # The capture is opened once by configure_cameras(); re-opening it
            # here cost 0.3-1s of USB enumeration at every coordinate.
            cam = call_state.get('webcam')
            if cam is not None:
                ret, frame = cam.read()
                if ret:
                    cv2.imwrite(img_filename, frame)
                
        elif act_type == 'scope' and scope:
            output_name = f"{base_filename}_scope0"
            ok = scope.acquire_multi_channel(
                channels=p['channels'], output_base_name=output_name,
                save_for_matlab=True, is_first_trace=is_first,
                total_loops=global_total_traces,
                sweeps=p.get('sweeps')
            )

            # A failed acquisition writes nothing. Left unchecked that leaves
            # the .dat one trace short, so every later trace maps to the wrong
            # coordinate - a silently wrong map with no error anywhere.
            if ok:
                call_state['consecutive_drops'] = 0
            else:
                if is_first:
                    raise AcquisitionAborted(
                        "The first scope trace failed, so there is no trace "
                        "geometry to pad with and no .m file was written. "
                        "Check the trigger and re-run."
                    )
                if not scope.write_placeholder_trace(output_name):
                    raise AcquisitionAborted(
                        "A scope trace failed and no placeholder could be "
                        "written; aborting rather than letting the .dat "
                        "desynchronise from the coordinate grid."
                    )
                note_dropped_trace(call_state, 'scope')

        elif act_type == 'a2d':
            session = p.get('session')
            if session is None:
                raise AcquisitionAborted(
                    "An 'action a2d' block has no DAQ session. This means "
                    "open_a2d_sessions() did not run, which should be "
                    "impossible in a real scan."
                )

            idx_num = p['legacy_index']
            output_name = (
                f"{base_filename}_"
                f"a2d{idx_num}_{total_a2d_blocks}f.d"
            )
            ok = session.acquire_a2d(
                channel=p['channel'], range_val=p['range'],
                n_samples=p['n_samples'], sample_rate=p['sample_rate'],
                output_filename=output_name, is_first_trace=is_first,
                v_min=p.get('v_min'), v_max=p.get('v_max'),
                terminal=p.get('terminal', daq.DEFAULT_TERMINAL),
            )

            # Same hazard as the scope: a read timeout returns False and
            # writes nothing, shifting every subsequent trace in the file.
            if ok:
                call_state['consecutive_drops'] = 0
            else:
                if is_first:
                    raise AcquisitionAborted(
                        f"The first A2D trace failed on "
                        f"{session.describe().summary()}. Check the card and "
                        f"re-run."
                    )
                session.write_placeholder_trace(output_name, p['n_samples'])
                note_dropped_trace(call_state, 'a2d')

        elif act_type == 'ixon' and ixon:
            if not call_state.get('ixon_configured'):
                ixon.setup(
                    exposure=p['exposure'],
                    em_gain=p['em_gain'],
                    #kinetic_cycle=p['kinetic_cycle'],# removing bc redundant with stage settle time
                    shutter_open=p['shutter_open'],
                    target_temp=p['temperature']
                )
                call_state['ixon_configured'] = True

            idx = call_state['current_trace_idx']
            # Change from .npy to .bin
            img_filename = (
                f"{base_filename}_"
                f"ixon_trace{idx}.bin"
            )
            
            if not silent_acq:
                print(f" -> Acquiring iXon frame -> {img_filename}...")
                
            frame_data = ixon.acquire()
            if frame_data is not None:
                # Ditch np.save() and pass to the zero-copy background queue
                write_queue.put((img_filename, frame_data))
                record_camera_frame(call_state, 'ixon', frame_data)

        elif act_type == 'tucsen' and tucsen:
            if not call_state.get('tucsen_configured'):
                tucsen.setup(
                    exposure=p['exposure'],
                    gain_mode=p['gain_mode'],
                    bit_depth=p['bit_depth'],
                    is_master=p['is_master']
                )
                call_state['tucsen_configured'] = True

            idx = call_state['current_trace_idx']
            # Drop .npy and use raw binary .bin
            img_filename = f"{base_filename}_tucsen_trace{idx}.bin"
            
            if not silent_acq:
                print(f" -> Acquiring Tucsen frame -> {img_filename}...")
                
            frame_data = tucsen.acquire()
            if frame_data is not None:
                # Pass the memory reference to the queue (instantaneous, no pickling)
                write_queue.put((img_filename, frame_data))
                record_camera_frame(call_state, 'tucsen', frame_data)
                
        if timer_mode and silent_acq:
            t_mod_end = time.perf_counter()
            print(f"      [Timer] {act_type.upper()} module : {t_mod_end - t_mod_start:.4f}s")

def open_a2d_sessions(pipelines, announce=True):
    """Resolve and open the DAQ card each 'action a2d' block will read from.

    Done once, before anything moves, and the session is stashed on the
    block's own params. Two a2d blocks can now name two different cards, so
    there is no single "the" DAQ handle any more - and pscan.daq caches
    sessions per card, so several blocks on one card still open it once.

    Opening here rather than lazily at the first trace matters: a card that is
    missing, or a config ambiguous about which of two cards it means, should
    stop the run before the sample has been moved anywhere.
    """
    blocks = [a for a in pipelines if a['type'] == 'a2d']
    if not blocks:
        return

    print("\nConnecting to A2D hardware...")
    for action in blocks:
        p = action['params']
        p['session'] = daq.open_device(
            backend=p.get('backend'),
            address=p.get('device'),
            need_ai=True,
            role='a2d',
            announce=announce,
        )




# ---------------------------------------------------------------------------
# THE COORDINATE GRID
#
# This was three hardcoded nested loops over X, Y and the piezo. That was fine
# while there was exactly one analog output in the system. It stopped being
# fine once an 'action d2a' block could target any card's AO channel, because
# a galvo pair is two output axes and there was no third slot to put them in.
#
# So the grid is a list of axes, nested by axis id ascending: axis 0 is the
# outermost loop and the highest-numbered axis the innermost. Three properties
# of the old loop are preserved deliberately, each having been a fix for a
# real failure:
#
#   * Moves overlap. An X step and a Y row-return are both commanded before
#     either is waited on, so they travel at the same time. PendingSettle is
#     what generalises that to any number of axes.
#
#   * The encoder is the authority for a stage axis. A blind sleep was too
#     short for the fast axis traversing a whole row back to its start, so the
#     first point of every row after the first was acquired mid-flight.
#
#   * Restores happen at the end of their own axis's loop, so an inner axis
#     restores once per outer step, not once per run.
# ---------------------------------------------------------------------------


class PendingSettle:
    """Moves that have been commanded but not yet arrived.

    This is what lets an X step and a Y row-return overlap in flight: both are
    commanded, then both are waited on together, rather than each being waited
    out in turn.
    """

    def __init__(self):
        self._waiters = []
        self._dwell = 0.0

    def add_waiter(self, fn):
        """Register a blocking wait - an encoder poll, typically."""
        self._waiters.append(fn)

    def add_dwell(self, seconds):
        """Register an open-loop settle time.

        The longest wins rather than the sum: two outputs commanded in the
        same step settle simultaneously, so waiting out the slowest is
        correct and waiting out both in series is merely slower.
        """
        self._dwell = max(self._dwell, seconds)

    def drain(self):
        """Block until everything commanded has arrived, then forget it."""
        for fn in self._waiters:
            fn()
        dwell = self._dwell
        self._waiters, self._dwell = [], 0.0
        if dwell > 0:
            time.sleep(dwell)


class ScanAxis:
    """One axis of the coordinate grid."""

    def __init__(self, axis_id, params, label, unit):
        self.axis_id = axis_id
        self.params = params
        self.label = label
        self.unit = unit
        self.positions = params.get('positions') or [None]
        # Where the walk currently has this axis, for the per-trace log line.
        self.current = None

    @property
    def is_static(self):
        return self.positions == [None]

    def command(self, position, pending):
        """Start this axis moving to `position`. Must not block.

        Subclasses call super() first so `current` is recorded even for a
        static axis, whose position is None and which commands nothing.
        """
        self.current = position

    def restore(self):
        """Return to the start position, if the config asked for that."""

    def format(self, position):
        if position is None:
            return "Static"
        return f"{position:7.4f} {self.unit}"

    def describe(self):
        return f"axis {self.axis_id} ({self.label})"


class AptAxis(ScanAxis):
    """A Thorlabs APT stage axis, waited on by its encoder."""

    def __init__(self, axis_id, params, stage, counts_per_mm, state):
        super().__init__(
            axis_id, params, 'X' if axis_id == APT_AXES[0] else 'Y', 'mm'
        )
        self.stage = stage
        self.counts_per_mm = counts_per_mm
        self.state = state
        self.name = 'x' if axis_id == APT_AXES[0] else 'y'

    def command(self, position, pending):
        super().command(position, pending)
        if position is None:
            return
        self.stage.move_absolute(self.name, position, self.counts_per_mm)
        pending.add_waiter(lambda target=position: self._wait(target))

    def _wait(self, target):
        reached = self.stage.wait_for_move(
            self.name, target, self.counts_per_mm,
            tolerance_mm=SETTLE_TOLERANCE_MM,
            timeout_s=SETTLE_TIMEOUT_S,
            dwell_s=SETTLE_DWELL_S,
        )
        if reached:
            return

        self.state['settle_timeouts'] += 1
        if self.state['settle_timeouts'] == 3:
            print(
                f"[WARNING] Repeated settle timeouts. If the stage is "
                f"actually arriving, SETTLE_TOLERANCE_MM "
                f"({SETTLE_TOLERANCE_MM} mm) may be tighter than the "
                f"controller's deadband - loosen it rather than let every "
                f"point burn {SETTLE_TIMEOUT_S:.0f}s."
            )

    def restore(self):
        if not self.params.get('restore'):
            return
        start = self.params['start_pos']
        self.stage.move_absolute(self.name, start, self.counts_per_mm)
        # Waited inline rather than deferred: nothing is acquired between a
        # restore and the next outer step, so there is nothing to overlap
        # with, and this also stops the process exiting mid-travel.
        self._wait(start)


class D2AAxis(ScanAxis):
    """An analog output axis - a piezo, a scanning mirror, anything DC-driven.

    Open-loop by nature: there is no position to poll back, so arrival is a
    settle time the config declares. The axis owns the unit conversion, so the
    backend only ever deals in volts.
    """

    def __init__(self, axis_id, params, session):
        super().__init__(
            axis_id, params,
            # The piezo's axis keeps printing as Z so existing run logs read
            # the same; any other output axis is named after its id.
            'Z' if axis_id == PIEZO_AXIS else f'D{axis_id}',
            params.get('unit_label', 'V'),
        )
        self.session = session
        self.channel = params['channel']
        self.units = params.get('units', 'volts')
        self.um_per_volt = params.get('um_per_volt')
        self.settle = params.get('settle', 0.0)

    def to_volts(self, position):
        if self.units == 'um':
            return position / self.um_per_volt
        return position

    def command(self, position, pending):
        super().command(position, pending)
        if position is None:
            return

        volts = self.to_volts(position)
        if not self.session.write_d2a(self.channel, volts):
            # Unlike a failed trace, this cannot be padded over: the axis is
            # not where the scan believes it is, so every file written from
            # here on would be labelled with a coordinate it was not measured
            # at. Stopping is the only honest option.
            raise AcquisitionAborted(
                f"Could not set {self.describe()} to {position:g} "
                f"{self.unit} ({volts:+.4f} V). Aborting rather than "
                f"acquiring at an unknown coordinate."
            )
        pending.add_dwell(self.settle)

    def restore(self):
        if not self.params.get('restore'):
            return
        start = self.params['start_pos']
        self.session.write_d2a(self.channel, self.to_volts(start))
        if self.settle > 0:
            time.sleep(self.settle)

    def format(self, position):
        if position is None:
            return "Static"
        if self.units == 'um':
            return f"{position:7.2f} um"
        return f"{position:7.3f} V"

    def describe(self):
        return (f"axis {self.axis_id} ({self.label}, {self.session.name} "
                f"AO{self.channel})")


def build_axes(stage_params, stage, counts_per_mm, state, open_d2a):
    """Turn compiled axis params into the ordered nest main() iterates.

    Sorted by axis id, which is what makes the nesting order declarative: the
    config says 0, 1, 98, 99 and gets outer-to-inner in that order.

    `open_d2a(params)` supplies the backend session for an output axis. Passed
    in rather than called directly so the grid can be built, walked and tested
    without pscan.daq or any hardware.
    """
    axes = []
    for axis_id in sorted(stage_params):
        params = stage_params[axis_id]

        if params.get('type') == 'apt_stage':
            axes.append(AptAxis(axis_id, params, stage, counts_per_mm, state))

        elif params.get('type') == 'd2a':
            axes.append(D2AAxis(axis_id, params, open_d2a(params)))

    return axes


def walk_axes(axes, level, pending, body):
    """Iterate the nest, commanding each axis and acquiring at the leaves.

    Every commanded move is drained at the innermost level rather than at the
    level that commanded it, which is what keeps moves overlapping: nothing is
    acquired until everything in flight has arrived, but nothing waits any
    earlier than it has to.
    """
    if level == len(axes):
        pending.drain()
        body()
        return

    axis = axes[level]
    for position in axis.positions:
        axis.command(position, pending)
        walk_axes(axes, level + 1, pending, body)

    # Runs once per iteration of the enclosing axis, which is where the
    # hardcoded loop restored Y - after each row, inside the X loop.
    axis.restore()


def format_position_line(axes):
    """The coordinate as it appears in the per-trace log line."""
    if not axes:
        return "static (no axis declared)"
    return ", ".join(f"{ax.label}={ax.format(ax.current)}" for ax in axes)


def axis_display(axis_id, params):
    """(name, unit) for an axis, without needing its hardware open.

    print_dry_run() needs this: it describes the whole grid and must do so
    without opening a card to ask what the axis is.
    """
    if params.get('type') == 'apt_stage':
        return ('X' if axis_id == APT_AXES[0] else 'Y'), 'mm'

    label = 'Z/piezo' if axis_id == PIEZO_AXIS else f'D{axis_id}'
    return label, params.get('unit_label', 'V')


def axis_grid_size(stage_params):
    """How many coordinates the declared axes produce, before `count`."""
    total = 1
    for axis_id in sorted(stage_params):
        total *= len(stage_params[axis_id]['positions'])
    return total


def describe_action(action):
    """One line summarising what an action will actually do."""
    t = action['type']
    p = action['params']

    if t == 'scope':
        line = f"scope    {', '.join(p['channels'])} @ {p['ip']}"
        return line + (f"  (sweeps {p['sweeps']})" if p.get('sweeps') else "  (sweeps: scope default)")
    if t == 'a2d':
        card = p.get('backend') or 'auto'
        if p.get('device'):
            card += f" {p['device']}"
        if p.get('v_min') is not None or p.get('v_max') is not None:
            span = f"{p.get('v_min')}..{p.get('v_max')} V"
        else:
            span = f"range {p['range']}"
        return (f"a2d      ch{p['channel']}, {span}, {p['n_samples']} samples "
                f"@ {p['sample_rate']} Hz, {p.get('terminal', 'rse')}, "
                f"on {card}")
    if t == 'delay':
        return f"delay    {p['seconds']}s"
    if t == 'script':
        return f"script   {p['cmd']}"
    if t == 'webcam':
        return f"webcam   device {p['device']}"
    if t == 'ixon':
        temp = f"{p['temperature']}C" if p['temperature'] is not None else "no cooling"
        return (f"ixon     exp {p['exposure']}s, em_gain {p['em_gain']}, "
                f"shutter {'held open' if p['shutter_open'] else 'auto'}, {temp}")
    if t == 'tucsen':
        return (f"tucsen   exp {p['exposure']}s, gain {p['gain_mode']}, "
                f"{p['bit_depth']}-bit, {'master' if p['is_master'] else 'slave'}")
    return t


def format_positions(positions, unit, max_shown=6):
    """Render a coordinate list, abbreviating anything long."""
    if positions == [None]:
        return "static (this axis does not move)"

    if len(positions) <= max_shown:
        return ", ".join(f"{p:g}" for p in positions) + f" {unit}"

    head = ", ".join(f"{p:g}" for p in positions[:3])
    tail = ", ".join(f"{p:g}" for p in positions[-3:])
    return f"{head} ... [{len(positions) - 6} more] ... {tail} {unit}"


def predicted_outputs(base, pipelines, total_a2d_blocks, total_traces):
    """Every file a run would create, as (label, path_or_pattern, note)."""
    outputs = []

    if any(a['type'] == 'scope' for a in pipelines):
        outputs.append(("scope data", f"{base}_scope0.dat", None))
        outputs.append(("scope metadata", f"{base}_scope0.m", None))
        outputs.append(("segment trigger times", f"{base}_scope0_trigtime.dat",
                        "only written in Sequence mode"))

    for action in pipelines:
        if action['type'] == 'a2d':
            idx = action['params']['legacy_index']
            outputs.append(("a2d data",
                            f"{base}_a2d{idx}_{total_a2d_blocks}f.d", None))

    for kind, ext in (('ixon', 'bin'), ('tucsen', 'bin'), ('webcam', 'jpg')):
        if any(a['type'] == kind for a in pipelines):
            outputs.append((
                f"{kind} frames",
                f"{base}_{kind}_trace*.{ext}",
                f"{total_traces} file(s), one per trace",
            ))
            if ext == 'bin':
                # Raw frames carry no header, so the geometry goes beside them.
                outputs.append((f"{kind} metadata", f"{base}_{kind}.m", None))

    outputs.append(("dropped-trace manifest", f"{base}_dropped.txt",
                    "only if a trace fails"))
    return outputs


def existing_outputs(base, pipelines, total_a2d_blocks, total_traces):
    """Which of a run's predicted outputs already exist on disk.

    Driven from predicted_outputs() so this and --dry-run cannot drift. The
    old check looked only at the scope .dat and the first a2d .d, so camera
    frames, the .m stubs, and the trigtime sidecar were all overwritten in
    silence - and a stale sidecar from an earlier Sequence-mode run would
    survive a real-time run untouched.
    """
    found = []
    for _label, path, _note in predicted_outputs(
        base, pipelines, total_a2d_blocks, total_traces
    ):
        if '*' in path:
            matches = glob.glob(path)
            if matches:
                found.append(f"{path}  ({len(matches)} file(s))")
        elif os.path.exists(path):
            found.append(path)
    return found


def write_camera_metadata(base_filename, kind, info):
    """Record a camera's frame geometry next to its .bin files.

    The .bin stream has no header, so without this the sensor size and dtype
    have to be remembered by hand to reload a run. Written at teardown from
    what was actually captured, mirroring the scope's .m stub.
    """
    base = f"{base_filename}_{kind}"
    func_name = os.path.basename(base).replace('-', '_')
    height, width = info['shape']

    with open(f"{base}.m", 'w') as f:
        f.write(
            f"% camera metadata file version 1.0\n"
            f"function cam={func_name}()\n"
            f"cam.version=1.0;\n"
            f"cam.height={height};\n"
            f"cam.width={width};\n"
            f"cam.dtype='{info['dtype'].name}';\n"
            f"cam.bytes_per_pixel={info['dtype'].itemsize};\n"
            f"cam.n_frames={info['count']};\n"
            f"cam.dataname='{os.path.basename(base)}_trace%d.bin';\n"
            f"cam.note='one file per trace, row-major, trace index is 0-based';\n"
        )

    print(f" -> Wrote {base}.m ({info['count']} frame(s), {height}x{width}, "
          f"{info['dtype'].name})")


def record_camera_frame(call_state, kind, frame):
    """Remember a camera's geometry and running frame count for teardown."""
    info = call_state['camera_frames'].setdefault(
        kind, {'shape': frame.shape, 'dtype': frame.dtype, 'count': 0}
    )
    info['count'] += 1


def print_dry_run(con_filepath, base_filename, setup_pipeline,
                  acquisition_pipeline, teardown_pipeline, stage_params,
                  count_val, global_total_traces, total_a2d_blocks,
                  config_warnings):
    """Report exactly what a run would do, without opening any hardware."""
    rule = "=" * 72
    print(f"\n{rule}\n DRY RUN - {con_filepath}\n{rule}")

    print("\nWHEN EACH ACTION RUNS")
    print("  A block is 'setup' only if no core block precedes it, and")
    print("  'teardown' only if none follows it. Core blocks are:")
    print(f"  {', '.join(CORE_TYPES)}.")

    sections = [
        ("SETUP - once, before anything moves", setup_pipeline),
        (f"PER COORDINATE - {global_total_traces} time(s)", acquisition_pipeline),
        ("TEARDOWN - once, after the scan", teardown_pipeline),
    ]
    for heading, pipeline in sections:
        print(f"\n  {heading}:")
        if not pipeline:
            print("    (none)")
        for i, action in enumerate(pipeline, 1):
            print(f"    {i}. {describe_action(action)}")

    repeated = [a for a in acquisition_pipeline if a['type'] == 'script']
    if repeated:
        print(f"\n  [NOTE] {len(repeated)} script block(s) landed in the "
              f"per-coordinate pipeline, so each will run "
              f"{global_total_traces} times, not once. Move them before the "
              f"first core block to run them at setup, or after the last to "
              f"run them at teardown.")

    print("\nGRID")
    print("  Listed outermost first. Axis ids set the nesting order, so the "
          "highest-numbered")
    print("  axis is the innermost loop and moves most often.")

    if not stage_params:
        print("\n  (no axis declared - one coordinate, nothing moves)")
    else:
        print()

    axis_order = sorted(stage_params)
    for axis_id in axis_order:
        params = stage_params[axis_id]
        name, unit = axis_display(axis_id, params)
        positions = params['positions']

        limits = params.get('limits')
        bounds = (f"  [limits {limits[0]}..{limits[1]} {unit}]"
                  if limits else "  [no limits declared]")
        print(f"  {name:<8} {len(positions):>5} step(s): "
              f"{format_positions(positions, unit)}{bounds}")

        if params.get('type') == 'd2a':
            target = params.get('backend') or 'auto-detect'
            if params.get('device'):
                target += f" {params['device']}"
            scaling = (f", {params['um_per_volt']:g} um/V"
                       if params.get('units') == 'um' else "")
            print(f"           -> {target}, AO channel "
                  f"{params['channel']}, settle {params['settle']:g}s"
                  f"{scaling}"
                  f"{', restores to start' if params.get('restore') else ''}")

    factors = [str(len(stage_params[a]['positions'])) for a in axis_order]
    factors.append(str(count_val))
    print(f"\n  Repeats per coordinate (count): {count_val}")
    if len(factors) > 1:
        print(f"  Total traces: {' x '.join(factors)} = {global_total_traces}")
    else:
        # Nothing to multiply out, so showing "1 = 1" would just be noise.
        print(f"  Total traces: {global_total_traces}")

    if total_a2d_blocks or any(p.get('type') == 'd2a'
                               for p in stage_params.values()):
        print("\n  No card was opened to produce this report, so which "
              "backend each")
        print("  'auto' resolves to is not shown. Run 'pydaq list' to see "
              "that.")

    print("\nOUTPUT FILES")
    all_actions = setup_pipeline + acquisition_pipeline + teardown_pipeline
    for label, path, note in predicted_outputs(
        base_filename, all_actions, total_a2d_blocks, global_total_traces
    ):
        if '*' in path:
            found = len(glob.glob(path))
            status = f"[{found} ALREADY EXIST]" if found else "[new]"
        else:
            status = "[ALREADY EXISTS]" if os.path.exists(path) else "[new]"
        suffix = f"  - {note}" if note else ""
        print(f"  {status:<20} {path}{suffix}")

    if config_warnings:
        print("\nWARNINGS")
        for warning in config_warnings:
            print(f"  - {warning}")

    print(f"\n{rule}")
    print(" No hardware was opened. Remove --dry-run to run this scan.")
    print(rule)


def safe_teardown(label, fn):
    """Run one teardown step, reporting failures instead of propagating.

    A raise part way through the finally block would skip every later step,
    which is how a failed scope disconnect could leave the stage port open
    and the iXon shutter wherever it happened to be.
    """
    try:
        fn()
    except Exception as e:
        print(f"[WARNING] {label} failed during teardown: {e}")


def configure_cameras(pipelines, ixon, tucsen, call_state):
    """Configure cameras before any stage motion.

    Done up front because the iXon's TEC cooldown can take minutes. Running
    it from the acquisition loop left the sample parked at the first
    coordinate with the shutter open for the whole wait, and re-opened the
    USB capture device at every coordinate. The in-loop paths stay as
    fallbacks, and these flags make them no-ops.
    """
    kinds = {a['type'] for a in pipelines} & {'ixon', 'tucsen', 'webcam'}
    if not kinds:
        return

    print("\nConfiguring cameras before scan start...")

    for action in pipelines:
        p = action['params']

        if action['type'] == 'webcam' and not call_state.get('webcam_configured'):
            call_state['webcam_configured'] = True
            device = p['device']
            print(f"Opening USB webcam device {device}...")

            cam = cv2.VideoCapture(device)
            if not cam.isOpened():
                print(f"[WARNING] Could not open webcam device {device}; "
                      f"webcam frames will be skipped.")
                cam.release()
                cam = None
            else:
                # A freshly opened UVC capture hands back a few stale or black
                # frames. Discard them once here instead of paying an open,
                # a settle and a release at every coordinate.
                time.sleep(WEBCAM_WARMUP_S)
                for _ in range(WEBCAM_DISCARD_FRAMES):
                    cam.read()
            call_state['webcam'] = cam

        if action['type'] == 'ixon' and ixon and not call_state.get('ixon_configured'):
            ixon.setup(
                exposure=p['exposure'],
                em_gain=p['em_gain'],
                shutter_open=p['shutter_open'],
                target_temp=p['temperature'],
            )
            call_state['ixon_configured'] = True

        elif action['type'] == 'tucsen' and tucsen and not call_state.get('tucsen_configured'):
            tucsen.setup(
                exposure=p['exposure'],
                gain_mode=p['gain_mode'],
                bit_depth=p['bit_depth'],
                is_master=p['is_master'],
            )
            call_state['tucsen_configured'] = True


def main():
    name_was_changed = False  # IT LIVES HERE NOW. NO MORE GHOST ERRORS.
    args = sys.argv[1:]
    timer_mode = False
    dry_run = False

    if '--timer' in args:
        timer_mode = True
        args.remove('--timer')

    if '--dry-run' in args:
        dry_run = True
        args.remove('--dry-run')

    if len(args) < 1:
        print("Usage: pscan [--timer] [--dry-run] <path_to_config.con>")
        print("  --dry-run  validate and report what would happen, then exit")
        print("             without opening any hardware")
        sys.exit(1)

    con_filepath = args[0]
    if not os.path.exists(con_filepath):
        print(f"Error: File '{con_filepath}' not found.")
        sys.exit(1)

    original_base = os.path.splitext(os.path.basename(con_filepath))[0]
    base_filename = original_base
    
    try:
        all_blocks = parse_flat_con_file(con_filepath)

        # Everything checkable is checked here, before a single device opens.
        config_warnings = validate_blocks(all_blocks)

        stages_str, count_val, r_set, r_acq, r_tear = categorize_pipeline(all_blocks)

        a2d_global_counter = [1]
        stage_params = compile_stage_parameters(stages_str)
        setup_pipeline = compile_pipeline_list(r_set, a2d_global_counter)
        acquisition_pipeline = compile_pipeline_list(r_acq, a2d_global_counter)
        teardown_pipeline = compile_pipeline_list(r_tear, a2d_global_counter)
    except ValueError as e:
        print(f"\n[CONFIG ERROR] in '{con_filepath}': {e}")
        sys.exit(1)

    # The grid is whatever axes the config declared, nested by axis id. There
    # is no longer a fixed set of three slots that an undeclared axis has to
    # occupy with a placeholder step.
    total_a2d_blocks = sum(1 for act in acquisition_pipeline if act['type'] == 'a2d')
    global_total_traces = axis_grid_size(stage_params) * count_val


    scope_ip = next((act['params']['ip'] for act in acquisition_pipeline if act['type'] == 'scope'), None)
    ixon_in_pipeline = any(act['type'] == 'ixon' for act in acquisition_pipeline + setup_pipeline + teardown_pipeline)
    
    counts_per_mm = 34304.0

    if dry_run:
        print_dry_run(
            con_filepath, base_filename, setup_pipeline, acquisition_pipeline,
            teardown_pipeline, stage_params, count_val, global_total_traces,
            total_a2d_blocks, config_warnings,
        )
        return

    for warning in config_warnings:
        print(f"[CONFIG WARNING] {warning}")

    all_actions = setup_pipeline + acquisition_pipeline + teardown_pipeline

    while True:
        collisions = existing_outputs(
            base_filename, all_actions, total_a2d_blocks, global_total_traces
        )
        if not collisions:
            break

        print(f"\n[WARNING] {len(collisions)} output(s) for "
              f"'{base_filename}' already exist:")
        for path in collisions:
            print(f"     {path}")

        msg = " -> Overwrite files (o) or Change base filename (c)? "
        choice = input(msg).strip().lower()
        if choice == 'o':
            break
        elif choice == 'c':
            new_base = input(" -> Enter new base filename: ").strip()
            if new_base:
                base_filename = os.path.splitext(os.path.basename(new_base))[0]
                name_was_changed = True

    if name_was_changed:
        shutil.copy(con_filepath, f"{base_filename}.con")

    tucsen_in_pipeline = any(act['type'] == 'tucsen' for act in acquisition_pipeline + setup_pipeline + teardown_pipeline)

    # Every handle starts as None so the teardown below can tell what actually
    # came up. Construction and connection happen inside the try, because when
    # they sat outside it a failure part way through left earlier devices
    # running - a streaming Tucsen, or the iXon with its shutter open.
    scope = stage = ixon = tucsen = None
    axes = []
    # Shared across every APT axis so the "repeated settle timeouts" advice
    # fires once for the run rather than once per axis.
    axis_state = {'settle_timeouts': 0}
    aborted = False

    call_state = {
        'current_trace_idx': 0,
        'ixon_configured': False,
        'tucsen_configured': False,
        'webcam_configured': False,
        'webcam': None,
        'camera_frames': {},
        'dropped': [],
        'consecutive_drops': 0,
    }

    try:
        if tucsen_in_pipeline:
            print("\nConnecting to Tucsen Camera...")
            tucsen = TucsenCamera()
            tucsen.connect()
        if scope_ip:
            print(f"\nOpening session to LeCroy at {scope_ip}...")
            scope = LeCroyScope(scope_ip)
            scope.connect()
        # Opened before anything moves, so a missing card - or a config that
        # is ambiguous about which of two cards it means - stops the run
        # before the sample has been driven anywhere.
        open_a2d_sessions(all_actions)

        if any(axis in stage_params for axis in APT_AXES):
            stage = ThorlabsStage()
            stage.connect()

        # Builds the axis nest and opens any analog-output card it needs. A
        # card already opened for an a2d block is reused, not reopened: on a
        # PCIe-6351 the photodiode input and the mirror output are the same
        # board.
        if stage_params:
            print("\nResolving scan axes...")

            def open_d2a(params):
                return daq.open_device(
                    backend=params.get('backend'),
                    address=params.get('device'),
                    need_ao=True,
                    role='d2a',
                )

            axes = build_axes(
                stage_params, stage, counts_per_mm, axis_state, open_d2a
            )

        if ixon_in_pipeline and ANDOR_AVAILABLE:
            print("\nConnecting to Windows iXon Camera...")
            ixon = IXonCamera()
            ixon.connect()

        # Cameras are configured before anything moves, so a long TEC
        # cooldown does not happen with the sample sitting at coordinate 0.
        configure_cameras(all_actions, ixon, tucsen, call_state)

        if axes:
            grid = " x ".join(
                f"{len(ax.positions)} {ax.label}" for ax in axes
            )
            print(f"\nGrid: {grid} (outermost first).")
        else:
            print("\nGrid: no axis declared - one coordinate, nothing moves.")
        print(f"Total Traces Target: {global_total_traces}")

        if setup_pipeline:
            print("\nExecuting Pre-Scan Initialization Commands...")
            run_pipeline_sequence(
                setup_pipeline, scope, ixon, tucsen, base_filename,
                total_a2d_blocks, global_total_traces, call_state,
                timer_mode=timer_mode
            )

        print("\nExecuting Data Acquisition Sequence...")
        
        if scope_ip:
            scope_act = next((act for act in acquisition_pipeline if act['type'] == 'scope'), None)
            if scope_act and scope_act['params'].get('sweeps'):
                print(f" -> Scope Hardware Target: {scope_act['params']['sweeps']} Averages\n")
                
        master_trace_idx = 0
        pending = PendingSettle()

        def acquire_here():
            """Everything that happens at one coordinate.

            Called by walk_axes() at the innermost level, after every
            commanded move has arrived.
            """
            nonlocal master_trace_idx

            # The first coordinate gets extra time for everything to stabilise,
            # but only if something actually moved to get there.
            moved = any(ax.current is not None for ax in axes)
            if moved and master_trace_idx == 0:
                time.sleep(INITIAL_SETTLE_S)

            for _ in range(count_val):
                if timer_mode:
                    t_trace_start = time.perf_counter()

                call_state['current_trace_idx'] = master_trace_idx

                print(
                    f" -> Pos: {format_position_line(axes)} "
                    f"| Trace {master_trace_idx + 1}/{global_total_traces}..."
                )

                run_pipeline_sequence(
                    acquisition_pipeline, scope, ixon, tucsen, base_filename,
                    total_a2d_blocks, global_total_traces, call_state,
                    silent_acq=True, timer_mode=timer_mode
                )

                if timer_mode:
                    t_trace_end = time.perf_counter()
                    print(f"      [Timer] Total trace time: {t_trace_end - t_trace_start:.4f}s\n")

                master_trace_idx += 1

        # One nest over however many axes the config declared, innermost last.
        # Restores happen inside the walk, at the end of each axis's own loop,
        # which is where the hardcoded version put them.
        if axes:
            walk_axes(axes, 0, pending, acquire_here)
        else:
            acquire_here()

        if teardown_pipeline:
            print("\nExecuting Post-Scan Teardown Commands...")
            run_pipeline_sequence(
                teardown_pipeline, scope, ixon, tucsen, base_filename,
                total_a2d_blocks, global_total_traces, call_state,
                timer_mode=timer_mode
            )

    except KeyboardInterrupt:
        aborted = True
        print("\n\n[WARNING] Sequence Aborted by User (Ctrl+C)!")
    except AcquisitionAborted as e:
        # Covers both a desynchronised file and an axis that would not move:
        # in either case everything written from here on would carry a
        # coordinate it was not measured at.
        aborted = True
        print(f"\n\n[ABORTED] {e}")
    except Exception as e:
        aborted = True
        print(f"\n\n[ERROR] Sequence Failed: {e}")
        traceback.print_exc()

    finally:
        # Halt motion first. On a clean finish nothing is moving, so this is
        # skipped rather than waiting out a stop confirmation that will never
        # arrive.
        if aborted and stage:
            print("\nHalting stage motion...")
            safe_teardown("Stage stop", stage.stop_all)

        print("\nExecuting Hardware Disconnects & Saving Files...")

        # Correct the .m while the scope still holds the first trace's
        # metadata, before it is torn down.
        def correct_metadata():
            if scope.finalize_matlab_metadata():
                print(
                    f" -> Rewrote {base_filename}_scope0.m for the "
                    f"{scope.traces_written} trace(s) actually recorded."
                )

        if scope:
            safe_teardown("Metadata rewrite", correct_metadata)

        if scope: safe_teardown("Scope disconnect", scope.disconnect)
        if stage: safe_teardown("Stage disconnect", stage.disconnect)
        if ixon: safe_teardown("iXon shutdown", ixon.shutdown)
        if tucsen: safe_teardown("Tucsen shutdown", tucsen.shutdown)

        # Closes every DAQ session opened for an a2d block or a d2a axis,
        # flushing each one's trace writer first. Analog outputs are left
        # energized, as the Phidget piezo path always did - a scan ending
        # should not slam a mirror or a piezo back to zero.
        if daq.open_sessions():
            safe_teardown("DAQ disconnect", daq.close_all)

        if call_state.get('webcam') is not None:
            safe_teardown("Webcam release", call_state['webcam'].release)

        # Written here rather than on the first frame so n_frames reflects
        # what was actually captured, even on an aborted run.
        for kind, info in call_state['camera_frames'].items():
            safe_teardown(
                f"{kind} metadata",
                lambda k=kind, i=info: write_camera_metadata(base_filename, k, i)
            )

        print("Waiting for background disk writes to finish...")
        write_queue.put(None)
        writer_thread.join()

        # Placeholder traces are indistinguishable from real data once on
        # disk, so record which indices are padding.
        if call_state['dropped']:
            manifest = f"{base_filename}_dropped.txt"
            with open(manifest, 'w') as f:
                f.write("# trace_idx\tsource\n")
                for idx, src in call_state['dropped']:
                    f.write(f"{idx}\t{src}\n")
            print(
                f"\n[WARNING] {len(call_state['dropped'])} trace(s) are "
                f"placeholders, not measurements. Indices listed in "
                f"{manifest} - exclude them before analysis."
            )

        print("Sequence Complete!")
