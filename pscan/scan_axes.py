"""The coordinate grid: what moves, in what order, and how arrival is known.

This used to be three hardcoded nested loops in main() over X, Y and the
piezo. That was fine while there was exactly one analog output in the system.
It stopped being fine once an 'action d2a' block could target any card's AO
channel, because a galvo pair is two output axes and there was no third slot
to put them in.

So the grid is now a list of axes, nested by axis id ascending: axis 0 is the
outermost loop and the highest-numbered axis the innermost. Three properties
of the old loop are preserved deliberately, because each of them was a fix for
a real failure:

  * Moves overlap. An X step and a Y row-return are both commanded before
    either is waited on, so they travel at the same time. PendingSettle is
    what generalises that to any number of axes.

  * The encoder is the authority for a stage axis. A blind sleep was too
    short for the fast axis traversing a whole row back to its start, so the
    first point of every row after the first was acquired mid-flight.

  * Restores happen at the end of their own axis's loop, which means the
    inner axis restores once per outer step, not once per run.

Nothing in this module imports a camera, a scope or a serial port, so the
whole grid is testable without any of them attached.
"""

import time

from pscan.config_parser import APT_AXES, PIEZO_AXIS

# Stage settling. wait_for_move() polls the position counter until the axis is
# inside SETTLE_TOLERANCE_MM, then naps SETTLE_DWELL_S for mechanical
# ringdown.
SETTLE_TOLERANCE_MM = 0.001
SETTLE_DWELL_S = 0.02
SETTLE_TIMEOUT_S = 15.0


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
            raise AxisCommandFailed(
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


class AxisCommandFailed(RuntimeError):
    """An axis could not be put where the scan needs it.

    Raised rather than reported, because there is no way to record a trace
    honestly once the coordinate is unknown. main() turns this into an
    AcquisitionAborted.
    """


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
