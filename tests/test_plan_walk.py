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

from pscan.config_parser import build_plan, parse_con_tree, plan_total_traces
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


class TestPredictedTraceCountMatchesReality(unittest.TestCase):
    """The number reported before a scan must be the number it takes.

    plan_total_traces() mirrors walk_plan() branch for branch, so these check
    the two against each other rather than trusting the mirror. A progress
    count that drifts from reality is how a short run looks finished - and
    it is also what every output file's expected length is computed from.
    """

    def check(self, text, sizes):
        from tempfile import TemporaryDirectory
        import re

        log = []
        axes = {i: RecordingAxis(i, list(range(n)), log)
                for i, n in sizes.items()}

        with TemporaryDirectory() as tmp:
            plan = build_plan(parse_con_tree(con(tmp, text)))

        def axis_id(block):
            return int(re.search(r'^axis\s+(\d+)', block['body'],
                                 re.M).group(1))

        predicted = plan_total_traces(plan,
                                      lambda b: len(axes[axis_id(b)].positions))

        walk_plan(plan, PendingSettle(),
                  lambda b: axes[axis_id(b)],
                  lambda blocks: log.append("TRACE"))

        actual = log.count("TRACE")
        self.assertEqual(predicted, actual,
                         f"predicted {predicted}, walked {actual}")
        return actual

    def test_flat_one_axis(self):
        self.assertEqual(self.check("""
            action d2a
            axis 98
            scan 0 4 1
            end
            action a2d
            channel 0
            end
        """, {98: 5}), 5)

    def test_flat_two_axes_with_count(self):
        self.assertEqual(self.check("""
            action count
            count 3
            end
            action d2a
            axis 98
            scan 0 4 1
            end
            action d2a
            axis 99
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """, {98: 5, 99: 2}), 30)

    def test_nested_count_outermost(self):
        self.assertEqual(self.check("""
            action count
            count 3
              action d2a
              axis 98
              scan 0 4 1
                action a2d
                channel 0
                end
              end
            end
        """, {98: 5}), 15)

    def test_nested_count_innermost(self):
        self.assertEqual(self.check("""
            action d2a
            axis 98
            scan 0 4 1
              action count
              count 3
                action a2d
                channel 0
                end
              end
            end
        """, {98: 5}), 15)

    def test_nested_three_deep(self):
        self.assertEqual(self.check("""
            action d2a
            axis 97
            scan 0 2 1
              action d2a
              axis 98
              scan 0 1 1
                action count
                count 4
                  action a2d
                  channel 0
                  end
                end
              end
            end
        """, {97: 3, 98: 2}), 24)

    def test_it_agrees_with_the_formula_main_uses_today(self):
        """main() computes axis_grid_size(stage_params) * count_val.

        The switch to plans replaces that formula, so for a flat config the
        two must give the same answer - it is what every output file's
        expected length and the progress display are built from.
        """
        from tempfile import TemporaryDirectory
        import re
        from pscan.config_parser import (categorize_pipeline,
                                         compile_stage_parameters,
                                         parse_flat_con_file)
        from pscan.main import axis_grid_size

        configs = [
            ("""
                action d2a
                axis 98
                scan 0 4 1
                end
                action a2d
                channel 0
                end
            """, 1),
            ("""
                action count
                count 3
                end
                action d2a
                axis 98
                scan 0 4 1
                end
                action d2a
                axis 99
                scan 0 1 1
                end
                action a2d
                channel 0
                end
            """, 3),
        ]

        for text, _ in configs:
            with TemporaryDirectory() as tmp:
                path = con(tmp, text)
                blocks = parse_flat_con_file(path)
                stages, count_val, _s, _a, _t = categorize_pipeline(blocks)
                stage_params = compile_stage_parameters(stages)
                old = axis_grid_size(stage_params) * count_val

                plan = build_plan(parse_con_tree(path))
                sizes = {axis_id: len(p['positions'])
                         for axis_id, p in stage_params.items()}
                new = plan_total_traces(
                    plan,
                    lambda b: sizes[int(re.search(r'^axis\s+(\d+)',
                                                  b['body'], re.M).group(1))],
                )

            with self.subTest(expected=old):
                self.assertEqual(new, old)

    def test_no_axis_at_all_is_one_trace(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            plan = build_plan(parse_con_tree(con(tmp, """
                action a2d
                channel 0
                end
            """)))
        self.assertEqual(plan_total_traces(plan, lambda b: 0), 1)


if __name__ == "__main__":
    unittest.main()
