"""The .d file format, which must not depend on which backend wrote it.

A trace acquired through DAQmx and a trace acquired through comedi land in the
same file, and the MATLAB side downstream has no idea which produced it. If
these ever diverge - dtype, byte order, append-vs-truncate, placeholder
padding - every analysis script in the lab silently reads garbage.

Exercised through the simulated backend, so this runs anywhere.
"""

import os
import tempfile
import unittest

import numpy as np

from tests.fake_card import FakeCard


class TraceFileTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "trace.d")
        self.session = FakeCard()
        self.session.connect()

    def tearDown(self):
        # disconnect() flushes the writer thread, which is the only way the
        # bytes are guaranteed to be on disk.
        self.session.disconnect()
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def read_back(self):
        self.session.disconnect()
        return np.fromfile(self.path, dtype=np.float32)


class TestTraceFormat(TraceFileTestCase):

    def test_a_trace_is_float32_volts(self):
        ok = self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=128, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.assertTrue(ok)

        data = self.read_back()
        self.assertEqual(data.dtype, np.float32)
        self.assertEqual(data.size, 128)
        self.assertEqual(os.path.getsize(self.path), 128 * 4)

    def test_first_trace_truncates_and_later_traces_append(self):
        for index in range(4):
            self.session.acquire_a2d(
                channel=0, range_val=0, n_samples=32, sample_rate=10000,
                output_filename=self.path, is_first_trace=(index == 0),
            )

        # Four appended blocks, not one overwritten one.
        self.assertEqual(self.read_back().size, 4 * 32)

    def test_rerunning_with_is_first_trace_starts_a_fresh_file(self):
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=32, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=32, sample_rate=10000,
            output_filename=self.path, is_first_trace=False,
        )
        # A second run of the same config truncates rather than growing the
        # file, which is what stops a re-run doubling up on an old one.
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=32, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.assertEqual(self.read_back().size, 32)

    def test_samples_respect_the_resolved_range(self):
        self.session.acquire_a2d(
            channel=0, n_samples=256, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
            v_min=-1.0, v_max=1.0,
        )
        data = self.read_back()
        self.assertLessEqual(np.abs(data).max(), 1.0 + 1e-6)

    def test_the_same_config_produces_the_same_bytes(self):
        # The simulator is seeded so a test can diff two runs. A real card
        # obviously cannot promise this; the point is that nothing in the
        # write path introduces variation of its own.
        second = os.path.join(self.dir, "trace2.d")
        for path in (self.path, second):
            self.session.acquire_a2d(
                channel=2, range_val=1, n_samples=64, sample_rate=10000,
                output_filename=path, is_first_trace=True,
            )
        self.session.disconnect()

        with open(self.path, 'rb') as a, open(second, 'rb') as b:
            self.assertEqual(a.read(), b.read())


class TestPlaceholderBlock(TraceFileTestCase):
    """A failed trace is padded, not skipped.

    Skipping would leave the file one block short and misalign every later
    trace against its coordinate - a silently wrong map with no error
    anywhere. NaN rather than zero so the padding is obvious in MATLAB.
    """

    def test_placeholder_is_the_right_length(self):
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=64, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.session.write_placeholder_trace(self.path, 64)

        data = self.read_back()
        self.assertEqual(data.size, 128)

    def test_placeholder_is_all_nan(self):
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=16, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.session.write_placeholder_trace(self.path, 16)

        data = self.read_back()
        self.assertFalse(np.isnan(data[:16]).any(), "real trace was padded")
        self.assertTrue(np.isnan(data[16:]).all(), "padding is not NaN")

    def test_padding_keeps_later_traces_on_their_coordinate(self):
        # Trace 0 real, trace 1 failed, trace 2 real. Trace 2 must land in the
        # third block, not the second.
        n = 8
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=n, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.session.write_placeholder_trace(self.path, n)
        self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=n, sample_rate=10000,
            output_filename=self.path, is_first_trace=False,
        )

        data = self.read_back().reshape(3, n)
        self.assertFalse(np.isnan(data[0]).any())
        self.assertTrue(np.isnan(data[1]).all())
        self.assertFalse(np.isnan(data[2]).any())


class TestAcquisitionGuards(TraceFileTestCase):

    def test_a_short_trace_is_discarded_rather_than_written(self):
        # A backend that returns fewer samples than asked for must not reach
        # the file: a short block shifts everything after it.
        self.session._acquire_volts = (
            lambda config: np.zeros(config.n_samples - 1, dtype=np.float64)
        )
        ok = self.session.acquire_a2d(
            channel=0, range_val=0, n_samples=32, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.assertFalse(ok)
        self.session.disconnect()
        self.assertFalse(os.path.exists(self.path))

    def test_an_out_of_bounds_range_fails_the_trace(self):
        ok = self.session.acquire_a2d(
            channel=0, range_val=99, n_samples=32, sample_rate=10000,
            output_filename=self.path, is_first_trace=True,
        )
        self.assertFalse(ok)

    def test_an_unknown_terminal_is_refused(self):
        with self.assertRaises(ValueError):
            self.session.acquire_a2d(
                channel=0, range_val=0, n_samples=32, sample_rate=10000,
                output_filename=self.path, is_first_trace=True,
                terminal='sideways',
            )

    def test_acquiring_without_connecting_returns_false(self):
        session = FakeCard()
        try:
            self.assertFalse(session.acquire_a2d(
                channel=0, range_val=0, n_samples=8, sample_rate=1000,
                output_filename=self.path, is_first_trace=True,
            ))
        finally:
            session.disconnect()


class TestOutputChannels(unittest.TestCase):

    def setUp(self):
        self.session = FakeCard()
        self.session.connect()

    def tearDown(self):
        self.session.disconnect()

    def test_a_write_is_recalled_by_read_d2a(self):
        self.assertTrue(self.session.write_d2a(0, 1.25))
        self.assertEqual(self.session.read_d2a(0), 1.25)

    def test_an_unwritten_channel_reads_as_unknown(self):
        # None means "nobody knows", which callers must handle rather than
        # treating as 0V - a piezo parked at 2V reported as 0V is how you
        # drive it into a hard stop.
        self.assertIsNone(self.session.read_d2a(1))

    def test_out_of_range_requests_are_clamped(self):
        self.session.write_d2a(0, 50.0)
        self.assertEqual(self.session.read_d2a(0), 10.0)

    def test_a_nonexistent_channel_fails_rather_than_silently_passing(self):
        self.assertFalse(self.session.write_d2a(7, 1.0))


if __name__ == '__main__':
    unittest.main()
