"""walk_plan() runs a flat config exactly as walk_axes() does.

WHY THIS IS THE TEST THAT MATTERS

walk_axes() is the loop real experiments run through, and its behaviour is
pinned by tests/test_axis_walk.py. walk_plan() generalises it so a nested
config can put `count` somewhere other than innermost.

Generalising it is only safe if the general version does the identical thing
in the special case. So these drive BOTH walkers over the same axes and
compare the event logs: every command, every restore, every trace, in order.
Equivalence is asserted, not assumed.

The axes are fakes that write down what they were asked to do, so the whole
grid runs with no stage, no card and no driver.
"""

import unittest

from pscan.config_parser import build_plan, parse_con_tree
from pscan.main import PendingSettle, walk_axes, walk_plan


class RecordingAxis:
    """Writes down what happened instead of moving anything."""

    def __init__(self, axis_id, positions, log, dwell=0.0):
        self.axis_id = axis_id
        self.positions = positions
        self.label = f"A{axis_id}"
        self.current = None
        self._log = log
        self._dwell = dwell

    def command(self, position, pending):
        self.current = position
        self._log.append(f"{self.label}:={position}")
        if self._dwell:
            pending.add_dwell(self._dwell)

    def restore(self):
        self._log.append(f"{self.label}:restore")

    def format(self, value):
        return str(value)


def run_old(axes, log, count):
    """walk_axes(), with count applied the way main() applies it."""
    pending = PendingSettle()

    def body():
        for _ in range(count):
            log.append("TRACE")

    walk_axes(axes, 0, pending, body)
    return log


def run_new(plan, axes_by_id, log):
    """walk_plan() over the compiled plan."""
    pending = PendingSettle()

    def axis_for(block):
        import re
        match = re.search(r'^axis\s+(\d+)', block['body'], re.M)
        return axes_by_id[int(match.group(1))]

    def trace(blocks):
        log.append("TRACE")

    walk_plan(plan, pending, axis_for, trace)
    return log


def con(tmpdir, text):
    import textwrap
    from pathlib import Path
    path = Path(tmpdir) / "t.con"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return str(path)


class TestFlatConfigsRunIdentically(unittest.TestCase):

    def compare(self, text, axis_specs, count):
        """Drive both walkers over the same grid and demand the same log."""
        from tempfile import TemporaryDirectory

        old_log, new_log = [], []
        old_axes = [RecordingAxis(i, p, old_log) for i, p in axis_specs]
        new_axes = {i: RecordingAxis(i, p, new_log) for i, p in axis_specs}

        run_old(old_axes, old_log, count)

        with TemporaryDirectory() as tmp:
            plan = build_plan(parse_con_tree(con(tmp, text)))
        run_new(plan, new_axes, new_log)

        self.assertEqual(old_log, new_log)
        return new_log

    def test_one_axis_no_count(self):
        self.compare("""
            action d2a
            axis 98
            scan 0 2 1
            end
            action a2d
            channel 0
            end
        """, [(98, [0.0, 1.0, 2.0])], count=1)

    def test_one_axis_with_count(self):
        log = self.compare("""
            action count
            count 3
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """, [(98, [0.0, 1.0])], count=3)
        self.assertEqual(log.count("TRACE"), 6)

    def test_two_axes_count_innermost(self):
        log = self.compare("""
            action count
            count 2
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action d2a
            axis 99
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """, [(98, [0.0, 1.0]), (99, [0.0, 1.0])], count=2)
        self.assertEqual(log.count("TRACE"), 8)

    def test_axis_ids_out_of_order_in_the_file(self):
        """99 written first must still nest INSIDE 98."""
        self.compare("""
            action d2a
            axis 99
            scan 0 1 1
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """, [(98, [0.0, 1.0]), (99, [0.0, 1.0])], count=1)

    def test_three_axes(self):
        self.compare("""
            action count
            count 2
            end
            action d2a
            axis 97
            scan 0 1 1
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action d2a
            axis 99
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """, [(97, [0.0, 1.0]), (98, [0.0, 1.0]), (99, [0.0, 1.0])], count=2)


class TestMoveOverlapIsPreserved(unittest.TestCase):
    """The property that made the multi-axis change risky in the first place.

    Several axes commanded in one step must settle TOGETHER - the longest
    dwell, not the sum - and nothing may wait before everything in flight has
    been commanded.
    """

    def test_dwells_are_taken_once_as_the_maximum(self):
        from tempfile import TemporaryDirectory

        log = []
        slow = RecordingAxis(98, [0.0, 1.0], log, dwell=0.30)
        fast = RecordingAxis(99, [0.0, 1.0], log, dwell=0.05)

        pending = PendingSettle()
        drained = []

        class Spy(PendingSettle):
            def drain(self):
                drained.append(self._dwell)
                super().drain()

        pending = Spy()

        with TemporaryDirectory() as tmp:
            plan = build_plan(parse_con_tree(con(tmp, """
                action d2a
                axis 98
                scan 0 1 1
                end
                action d2a
                axis 99
                scan 0 1 1
                end
                action a2d
                channel 0
                end
            """)))

        walk_plan(plan, pending,
                  lambda b: {98: slow, 99: fast}[
                      int(__import__('re').search(r'^axis\s+(\d+)',
                                                  b['body'],
                                                  __import__('re').M).group(1))],
                  lambda blocks: log.append("TRACE"))

        # The grid is 2 outer x 2 inner, so four traces and four drains.
        #
        # The max is over what is IN FLIGHT at that moment, not over every
        # axis. When the outer axis has just stepped, both are moving and the
        # wait is the slower one, 0.30. When only the inner has stepped, 0.05
        # is right and waiting 0.30 would be needlessly slow. Asserting the
        # whole sequence says both halves of that.
        self.assertEqual(drained, [0.30, 0.05, 0.30, 0.05])

        # And never the sum, which is what waiting each axis out in turn
        # would cost.
        self.assertNotIn(0.35, drained)


class TestNestedConfigsReachTheirLeaves(unittest.TestCase):

    def walk(self, text, axes_by_id):
        from tempfile import TemporaryDirectory
        import re

        log = []
        axes = {i: RecordingAxis(i, p, log) for i, p in axes_by_id}
        with TemporaryDirectory() as tmp:
            plan = build_plan(parse_con_tree(con(tmp, text)))

        walk_plan(
            plan, PendingSettle(),
            lambda b: axes[int(re.search(r'^axis\s+(\d+)', b['body'],
                                         re.M).group(1))],
            lambda blocks: log.append("TRACE"),
        )
        return log

    def test_count_outside_the_sweep_repeats_the_whole_sweep(self):
        log = self.walk("""
            action count
            count 2
              action d2a
              axis 98
              scan 0 1 1
                action a2d
                channel 0
                end
              end
            end
        """, [(98, [0.0, 1.0])])

        # Two full sweeps: the axis is restored twice, once per count.
        self.assertEqual(log.count("A98:restore"), 2)
        self.assertEqual(log.count("TRACE"), 4)

    def test_count_inside_averages_each_point(self):
        log = self.walk("""
            action d2a
            axis 98
            scan 0 1 1
              action count
              count 2
                action a2d
                channel 0
                end
              end
            end
        """, [(98, [0.0, 1.0])])

        # One sweep, so one restore - but still four traces.
        self.assertEqual(log.count("A98:restore"), 1)
        self.assertEqual(log.count("TRACE"), 4)

    def test_the_two_differ_in_order_not_in_trace_count(self):
        outer = self.walk("""
            action count
            count 2
              action d2a
              axis 98
              scan 0 1 1
                action a2d
                channel 0
                end
              end
            end
        """, [(98, [0.0, 1.0])])
        inner = self.walk("""
            action d2a
            axis 98
            scan 0 1 1
              action count
              count 2
                action a2d
                channel 0
                end
              end
            end
        """, [(98, [0.0, 1.0])])

        self.assertEqual(outer.count("TRACE"), inner.count("TRACE"))
        self.assertNotEqual(outer, inner)


if __name__ == "__main__":
    unittest.main()
