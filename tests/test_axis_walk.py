"""The axis walk: nesting order, move overlap, and when restores happen.

This is the riskiest part of the multi-axis change, because it replaced three
hardcoded nested loops whose exact ordering was load-bearing. Each test below
pins down one property the old loop had.

The axes here are fakes that record what they were asked to do, so the whole
grid is exercised with no stage, no card and no driver.
"""

import unittest

# The grid lives in main.py, which is the orchestrator and imports the camera,
# scope and serial stacks at module scope. Those are installed on the rig, so
# this module runs there; on a bare laptop it skips with a reason rather than
# erroring out and looking like a real failure.
try:
    from pscan.main import (
        AcquisitionAborted,
        D2AAxis,
        PendingSettle,
        axis_grid_size,
        build_axes,
        format_position_line,
        walk_axes,
    )
except ImportError as e:
    raise unittest.SkipTest(
        f"pscan.main needs the instrument stack to import ({e}). These tests "
        f"run on a machine with the full install."
    ) from e


class RecordingAxis:
    """An axis that writes down what happened instead of moving anything."""

    def __init__(self, axis_id, positions, log, waits=True, dwell=0.0):
        self.axis_id = axis_id
        self.positions = positions
        self.label = f"A{axis_id}"
        self.current = None
        self.log = log
        self.waits = waits
        self.dwell = dwell
        self.params = {}

    def command(self, position, pending):
        self.current = position
        self.log.append(('command', self.axis_id, position))
        if self.waits:
            pending.add_waiter(
                lambda p=position: self.log.append(('arrive', self.axis_id, p))
            )
        if self.dwell:
            pending.add_dwell(self.dwell)

    def restore(self):
        self.log.append(('restore', self.axis_id, None))

    def format(self, position):
        return "Static" if position is None else f"{position}"


class TestNestingOrder(unittest.TestCase):

    def test_highest_axis_id_is_the_innermost_loop(self):
        log = []
        axes = [
            RecordingAxis(0, [10, 20], log, waits=False),
            RecordingAxis(99, ['a', 'b'], log, waits=False),
        ]
        coords = []
        walk_axes(axes, 0, PendingSettle(),
                  lambda: coords.append((axes[0].current, axes[1].current)))

        # Axis 0 steps once for every full sweep of axis 99.
        self.assertEqual(
            coords, [(10, 'a'), (10, 'b'), (20, 'a'), (20, 'b')]
        )

    def test_every_coordinate_is_visited_once(self):
        log = []
        axes = [
            RecordingAxis(0, [1, 2, 3], log, waits=False),
            RecordingAxis(98, [1, 2], log, waits=False),
            RecordingAxis(99, [1, 2, 3, 4], log, waits=False),
        ]
        visited = []
        walk_axes(axes, 0, PendingSettle(),
                  lambda: visited.append(tuple(a.current for a in axes)))

        self.assertEqual(len(visited), 3 * 2 * 4)
        self.assertEqual(len(set(visited)), 3 * 2 * 4)

    def test_no_axes_is_one_coordinate(self):
        calls = []
        walk_axes([], 0, PendingSettle(), lambda: calls.append(1))
        self.assertEqual(calls, [1])

    def test_static_axis_still_yields_one_coordinate(self):
        log = []
        axes = [RecordingAxis(0, [None], log, waits=False)]
        calls = []
        walk_axes(axes, 0, PendingSettle(), lambda: calls.append(1))
        self.assertEqual(calls, [1])
        self.assertEqual(log, [('command', 0, None), ('restore', 0, None)])


class TestMoveOverlap(unittest.TestCase):
    """Moves are commanded together and waited on together.

    This is what let an X step and a Y row-return travel at the same time in
    the original loop. It has to survive, or every row costs an extra full
    traverse of settling.
    """

    def test_outer_and_inner_moves_are_both_commanded_before_either_waits(self):
        log = []
        axes = [
            RecordingAxis(0, [10, 20], log),
            RecordingAxis(1, [1, 2], log),
        ]
        walk_axes(axes, 0, PendingSettle(), lambda: log.append(('acquire',)))

        # The first coordinate: both axes commanded, then both arrivals, then
        # the acquisition. Not command-arrive-command-arrive.
        self.assertEqual(log[:5], [
            ('command', 0, 10),
            ('command', 1, 1),
            ('arrive', 0, 10),
            ('arrive', 1, 1),
            ('acquire',),
        ])

    def test_inner_step_waits_only_on_itself(self):
        log = []
        axes = [
            RecordingAxis(0, [10], log),
            RecordingAxis(1, [1, 2], log),
        ]
        walk_axes(axes, 0, PendingSettle(), lambda: log.append(('acquire',)))

        # Second coordinate: only axis 1 moved, so only axis 1 is waited on.
        after_first_acquire = log[log.index(('acquire',)) + 1:]
        self.assertEqual(after_first_acquire[:3], [
            ('command', 1, 2),
            ('arrive', 1, 2),
            ('acquire',),
        ])

    def test_nothing_is_acquired_before_everything_has_arrived(self):
        log = []
        axes = [
            RecordingAxis(0, [1, 2], log),
            RecordingAxis(98, [1, 2], log),
            RecordingAxis(99, [1, 2], log),
        ]
        walk_axes(axes, 0, PendingSettle(), lambda: log.append(('acquire',)))

        pending = 0
        for event in log:
            if event[0] == 'command':
                pending += 1
            elif event[0] == 'arrive':
                pending -= 1
            elif event[0] == 'acquire':
                self.assertEqual(
                    pending, 0,
                    "acquired with a move still in flight"
                )


class TestDwell(unittest.TestCase):

    def test_concurrent_dwells_take_the_longest_not_the_sum(self):
        # Two outputs commanded in the same step settle simultaneously.
        pending = PendingSettle()
        pending.add_dwell(0.01)
        pending.add_dwell(0.03)
        pending.add_dwell(0.02)
        self.assertEqual(pending._dwell, 0.03)

    def test_drain_resets_the_dwell(self):
        pending = PendingSettle()
        pending.add_dwell(0.0)  # zero so the test does not actually sleep
        pending.drain()
        self.assertEqual(pending._dwell, 0.0)
        self.assertEqual(pending._waiters, [])


class TestRestoreTiming(unittest.TestCase):
    """A restore runs at the end of its own axis's loop.

    The hardcoded loop restored Y after each row, inside the X loop, and X
    once at the very end. Getting this wrong means either restoring far too
    often or never.
    """

    def test_inner_axis_restores_once_per_outer_step(self):
        log = []
        axes = [
            RecordingAxis(0, [10, 20, 30], log, waits=False),
            RecordingAxis(1, [1, 2], log, waits=False),
        ]
        walk_axes(axes, 0, PendingSettle(), lambda: None)

        restores = [e for e in log if e[0] == 'restore']
        self.assertEqual(
            [r[1] for r in restores], [1, 1, 1, 0],
            "axis 1 should restore after each of the 3 rows, axis 0 once"
        )

    def test_inner_restore_precedes_the_next_outer_command(self):
        log = []
        axes = [
            RecordingAxis(0, [10, 20], log, waits=False),
            RecordingAxis(1, [1], log, waits=False),
        ]
        walk_axes(axes, 0, PendingSettle(), lambda: None)

        self.assertEqual(log, [
            ('command', 0, 10),
            ('command', 1, 1),
            ('restore', 1, None),
            ('command', 0, 20),
            ('command', 1, 1),
            ('restore', 1, None),
            ('restore', 0, None),
        ])


class FakeSession:
    """A d2a backend that records writes and can be told to fail."""

    name = 'fake'

    def __init__(self, fail_on=None):
        self.writes = []
        self.fail_on = fail_on

    def write_d2a(self, channel, volts):
        self.writes.append((channel, volts))
        return not (self.fail_on is not None
                    and abs(volts - self.fail_on) < 1e-9)

    def describe(self):
        raise AssertionError("describe() should not be needed here")


class TestD2AAxis(unittest.TestCase):

    def _axis(self, **overrides):
        params = {
            'positions': [0.0, 1.0, 2.0],
            'channel': 1,
            'units': 'volts',
            'um_per_volt': None,
            'settle': 0.0,
            'start_pos': 0.0,
            'unit_label': 'V',
        }
        params.update(overrides)
        session = FakeSession(fail_on=overrides.pop('_fail_on', None))
        return D2AAxis(99, params, session), session

    def test_volts_are_passed_through_unscaled(self):
        axis, session = self._axis()
        pending = PendingSettle()
        axis.command(1.5, pending)
        self.assertEqual(session.writes, [(1, 1.5)])

    def test_microns_are_converted_by_um_per_volt(self):
        # The piezo's 20 um/V: 50 um is 2.5 V.
        axis, session = self._axis(units='um', um_per_volt=20.0,
                                   unit_label='um')
        axis.command(50.0, PendingSettle())
        self.assertEqual(session.writes, [(1, 2.5)])

    def test_static_position_writes_nothing(self):
        axis, session = self._axis(positions=[None])
        axis.command(None, PendingSettle())
        self.assertEqual(session.writes, [])
        self.assertIsNone(axis.current)

    def test_a_failed_write_aborts_rather_than_continuing(self):
        # A trace can be padded; a coordinate cannot. Acquiring after a failed
        # move would label data with a position it was never measured at.
        params = {
            'positions': [0.0, 1.0], 'channel': 0, 'units': 'volts',
            'settle': 0.0, 'start_pos': 0.0, 'unit_label': 'V',
        }
        axis = D2AAxis(99, params, FakeSession(fail_on=1.0))
        axis.command(0.0, PendingSettle())  # fine
        with self.assertRaises(AcquisitionAborted):
            axis.command(1.0, PendingSettle())

    def test_settle_is_registered_as_a_dwell(self):
        axis, _session = self._axis(settle=0.05)
        pending = PendingSettle()
        axis.command(1.0, pending)
        self.assertEqual(pending._dwell, 0.05)

    def test_restore_is_a_no_op_without_the_flag(self):
        axis, session = self._axis()
        axis.restore()
        self.assertEqual(session.writes, [])

    def test_restore_returns_to_start_pos(self):
        axis, session = self._axis(restore=True, start_pos=0.0)
        axis.restore()
        self.assertEqual(session.writes, [(1, 0.0)])

    def test_piezo_axis_still_prints_as_z(self):
        axis, _ = self._axis()
        self.assertEqual(axis.label, 'Z')

    def test_other_output_axes_are_named_by_id(self):
        params = {'positions': [0.0], 'channel': 0, 'settle': 0.0,
                  'start_pos': 0.0, 'unit_label': 'V'}
        self.assertEqual(D2AAxis(98, params, FakeSession()).label, 'D98')


class TestGridHelpers(unittest.TestCase):

    def test_grid_size_multiplies_every_axis(self):
        stage_params = {
            0: {'positions': [1, 2, 3]},
            1: {'positions': [1, 2]},
            99: {'positions': [1, 2, 3, 4]},
        }
        self.assertEqual(axis_grid_size(stage_params), 24)

    def test_grid_size_of_no_axes_is_one(self):
        # One coordinate, nothing moves - which is a legitimate config.
        self.assertEqual(axis_grid_size({}), 1)

    def test_build_axes_orders_by_axis_id(self):
        stage_params = {
            99: {'type': 'd2a', 'positions': [0.0], 'channel': 0,
                 'settle': 0.0, 'start_pos': 0.0, 'unit_label': 'V'},
            0: {'type': 'apt_stage', 'positions': [1.0], 'start_pos': 1.0},
            98: {'type': 'd2a', 'positions': [0.0], 'channel': 1,
                 'settle': 0.0, 'start_pos': 0.0, 'unit_label': 'V'},
        }
        axes = build_axes(
            stage_params, stage=None, counts_per_mm=1.0,
            state={'settle_timeouts': 0}, open_d2a=lambda p: FakeSession(),
        )
        self.assertEqual([a.axis_id for a in axes], [0, 98, 99])

    def test_position_line_with_no_axes_says_so(self):
        self.assertIn("no axis declared", format_position_line([]))


if __name__ == '__main__':
    unittest.main()
