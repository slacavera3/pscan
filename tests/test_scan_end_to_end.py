"""Run main() over a fake card and check what lands on disk.

WHY THIS EXISTS

Everything else tests a piece: the reader, the plan, the walker, the output
format. main() is where they meet, and switching it from walk_axes() to
walk_plan() changed the one function that unit tests do not cover - the
bookkeeping between the walk and the file. Trace numbering, the per-trace
print, when setup and teardown run, how many traces actually reach the .d.

So these run a whole scan, with no hardware, and look at the result.

The flat cases matter most: they must produce byte-identical files to the
ones they produced before plans existed, because real .con files describe
experiments whose .d files are already analysed.
"""

import os
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from pscan import daq
from tests import fake_card  # noqa: F401  - registers the backend


def write_con(directory, name, text):
    path = Path(directory) / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return str(path)


def run_scan(con_path):
    """Run main() on a config, from the directory holding it."""
    from pscan.main import main

    previous_argv, previous_cwd = sys.argv, os.getcwd()
    os.chdir(os.path.dirname(con_path))
    sys.argv = ["pscan", os.path.basename(con_path)]
    try:
        main()
    except SystemExit:
        pass
    finally:
        sys.argv, _ = previous_argv, os.chdir(previous_cwd)
        daq.close_all()


def traces_in(path, n_samples):
    data = np.fromfile(path, dtype=np.float32)
    assert data.size % n_samples == 0, (
        f"{path} holds {data.size} floats, not a whole number of "
        f"{n_samples}-sample traces")
    return data.reshape(-1, n_samples)


SWEEP = """
    action d2a
    axis 98
    backend fake
    device fake0
    channel 0
    units volts
    scan 0 4 1
    limits -10 10
    settle 0
    end

    action a2d
    channel 0
    backend fake
    device fake0
    v_min -10
    v_max 10
    sample_rate 10000
    n_samples 64
    end
"""


class TestFlatConfigsStillRun(unittest.TestCase):

    def test_one_axis_writes_one_trace_per_position(self):
        with TemporaryDirectory() as tmp:
            path = write_con(tmp, "sweep.con", SWEEP)
            run_scan(path)

            out = Path(tmp) / "sweep_a2d1_1f.d"
            self.assertTrue(out.exists(), f"{out} was not written")
            self.assertEqual(traces_in(str(out), 64).shape, (5, 64))

    def test_count_multiplies_the_traces(self):
        with TemporaryDirectory() as tmp:
            path = write_con(tmp, "avg.con", """
                action count
                count 3
                end
            """ + SWEEP)
            run_scan(path)

            out = Path(tmp) / "avg_a2d1_1f.d"
            self.assertEqual(traces_in(str(out), 64).shape, (15, 64))

    def test_two_axes_multiply(self):
        with TemporaryDirectory() as tmp:
            path = write_con(tmp, "grid.con", """
                action d2a
                axis 98
                backend fake
                device fake0
                channel 0
                units volts
                scan 0 2 1
                limits -10 10
                settle 0
                end

                action d2a
                axis 99
                backend fake
                device fake0
                channel 1
                units volts
                scan 0 1 1
                limits -10 10
                settle 0
                end

                action a2d
                channel 0
                backend fake
                device fake0
                v_min -10
                v_max 10
                sample_rate 10000
                n_samples 64
                end
            """)
            run_scan(path)

            out = Path(tmp) / "grid_a2d1_1f.d"
            self.assertEqual(traces_in(str(out), 64).shape, (6, 64))

    def test_no_axis_is_a_single_trace(self):
        with TemporaryDirectory() as tmp:
            path = write_con(tmp, "single.con", """
                action a2d
                channel 0
                backend fake
                device fake0
                v_min -10
                v_max 10
                sample_rate 10000
                n_samples 64
                end
            """)
            run_scan(path)

            out = Path(tmp) / "single_a2d1_1f.d"
            self.assertEqual(traces_in(str(out), 64).shape, (1, 64))

    def test_two_a2d_blocks_write_two_files_of_equal_length(self):
        with TemporaryDirectory() as tmp:
            path = write_con(tmp, "two.con", """
                action d2a
                axis 98
                backend fake
                device fake0
                channel 0
                units volts
                scan 0 2 1
                limits -10 10
                settle 0
                end

                action a2d
                channel 0
                backend fake
                device fake0
                v_min -10
                v_max 10
                sample_rate 10000
                n_samples 64
                end

                action a2d
                channel 1
                backend fake
                device fake0
                v_min -10
                v_max 10
                sample_rate 10000
                n_samples 64
                end
            """)
            run_scan(path)

            # The suffix counts the a2d blocks in the config, not the trace
            # index: two blocks means '_2f', so a one-block scan's '_1f' is
            # not the pattern here.
            first = Path(tmp) / "two_a2d1_2f.d"
            second = Path(tmp) / "two_a2d2_2f.d"
            self.assertTrue(
                first.exists() and second.exists(),
                f"wrote: {sorted(p.name for p in Path(tmp).glob('*.d'))}")
            self.assertEqual(traces_in(str(first), 64).shape, (3, 64))
            self.assertEqual(traces_in(str(second), 64).shape, (3, 64))


class TestNestedConfigsRun(unittest.TestCase):
    """The capability the whole change is for, exercised end to end."""

    OUTER = """
        action count
        count 3
          action d2a
          axis 98
          backend fake
          device fake0
          channel 0
          units volts
          scan 0 4 1
          limits -10 10
          settle 0

            action a2d
            channel 0
            backend fake
            device fake0
            v_min -10
            v_max 10
            sample_rate 10000
            n_samples 64
            end
          end
        end
    """

    INNER = """
        action d2a
        axis 98
        backend fake
        device fake0
        channel 0
        units volts
        scan 0 4 1
        limits -10 10
        settle 0

          action count
          count 3
            action a2d
            channel 0
            backend fake
            device fake0
            v_min -10
            v_max 10
            sample_rate 10000
            n_samples 64
            end
          end
        end
    """

    def test_count_outside_the_sweep(self):
        with TemporaryDirectory() as tmp:
            run_scan(write_con(tmp, "outer.con", self.OUTER))
            out = Path(tmp) / "outer_a2d1_1f.d"
            self.assertTrue(out.exists(), f"{out} was not written")
            self.assertEqual(traces_in(str(out), 64).shape, (15, 64))

    def test_count_inside_the_sweep(self):
        with TemporaryDirectory() as tmp:
            run_scan(write_con(tmp, "inner.con", self.INNER))
            out = Path(tmp) / "inner_a2d1_1f.d"
            self.assertTrue(out.exists(), f"{out} was not written")
            self.assertEqual(traces_in(str(out), 64).shape, (15, 64))

    def test_both_write_the_same_amount_of_data(self):
        """Same experiment size, different order - the file length matches."""
        with TemporaryDirectory() as tmp:
            run_scan(write_con(tmp, "a.con", self.OUTER))
            run_scan(write_con(tmp, "b.con", self.INNER))
            a = (Path(tmp) / "a_a2d1_1f.d").stat().st_size
            b = (Path(tmp) / "b_a2d1_1f.d").stat().st_size
            self.assertEqual(a, b)
            self.assertEqual(a, 15 * 64 * 4)


if __name__ == "__main__":
    unittest.main()
