"""The USB PicoScope path: what reaches the file, and what refuses to.

Two properties matter more than anything else here.

The first is that a PicoScope run and a LeCroy run are the SAME FILE. Both go
through pscan.scope_io, and the .m key set is pinned below against a literal,
because every MATLAB script in the lab reads it and a quietly renamed field
breaks all of them at once.

The second is that a trace which cannot be trusted never reaches the .dat. A
missed trigger, a short read or a geometry the unit cannot do must come back
as a refusal, so the orchestrator pads deliberately and records the index -
writing a short or stale block instead would shift every later trace onto the
wrong coordinate, and nothing downstream could tell.

Everything runs against tests/fake_picoscope.py. No scope, no PicoSDK.
"""

import os
import re
import tempfile
import unittest

import numpy as np

from pscan import picoscope_driver as pico
from pscan.picoscope_driver import PicoError, PicoScope
from pscan.scope_io import ScopeTraceWriter
from tests.fake_picoscope import FakePicoBackend, factory


def read_m(path):
    """A .m sidecar as {field: raw text}."""
    with open(path) as handle:
        text = handle.read()
    return dict(re.findall(r'^scp\.(\w+)\s*=\s*(.+?);\s*$', text, re.M))


def m_numbers(meta, field):
    """One .m field's numbers, whether it is a scalar or a MATLAB array."""
    return [float(v) for v in meta[field].strip('[]').split(',') if v]


class PicoTestCase(unittest.TestCase):
    """A connected PicoScope writing into a scratch directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.base = os.path.join(self.dir, "run")
        self.scope = None

    def tearDown(self):
        if self.scope is not None:
            self.scope.disconnect()
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def open_scope(self, backend=None, **kwargs):
        kwargs.setdefault('channels', ['A'])
        kwargs.setdefault('n_samples', 64)
        kwargs.setdefault('sample_rate', 1e6)
        self.backend = backend or FakePicoBackend()
        self.scope = PicoScope(backend=self.backend, **kwargs)
        self.scope.connect()
        return self.scope

    def acquire(self, index=0, **kwargs):
        return self.scope.acquire_multi_channel(
            channels=self.scope.channels, output_base_name=self.base,
            is_first_trace=(index == 0), **kwargs
        )

    def dat(self, points=None, dtype=np.int16):
        """The .dat as a (rows, points) array, after flushing the writer."""
        self.scope.disconnect()
        self.scope = None
        data = np.fromfile(f"{self.base}.dat", dtype=dtype)
        if points is None:
            return data
        return data.reshape(-1, points)


class TestRangeResolution(unittest.TestCase):
    """A range index means different things per family; volts do not."""

    def test_the_narrowest_range_that_contains_the_request_wins(self):
        # 0.3 V does not fit in +/-0.2, so +/-0.5 - not +/-1 or wider.
        self.assertEqual(pico.resolve_range(0.3, 'ps5000a'), 5)
        self.assertEqual(pico.RANGE_VOLTS[5], 0.5)

    def test_an_exact_range_is_not_rounded_up(self):
        self.assertEqual(pico.RANGE_VOLTS[pico.resolve_range(1.0, 'ps5000a')],
                         1.0)

    def test_more_volts_than_the_hardware_has_is_refused_not_widened(self):
        with self.assertRaises(PicoError) as caught:
            pico.resolve_range(120.0, 'ps5000a')
        self.assertIn("50", str(caught.exception))

    def test_the_4000a_reaches_further_than_the_others(self):
        # Its range ladder runs to 200 V, which the 5000A's does not.
        self.assertEqual(pico.RANGE_VOLTS[pico.resolve_range(120.0, 'ps4000a')],
                         200.0)


class TestChannelNames(unittest.TestCase):

    def test_letters_become_indices(self):
        self.assertEqual(pico.channel_index('A'), 0)
        self.assertEqual(pico.channel_index('d'), 3)

    def test_a_lecroy_style_name_is_refused(self):
        with self.assertRaises(PicoError):
            pico.channel_index('C1')

    def test_a_channel_the_series_does_not_have_is_refused(self):
        # E exists on an 8-input 4000A and not on a 5000A, and recording a
        # channel that is not there would otherwise be a file of zeros.
        self.assertEqual(pico.channel_index('E', 'ps4000a'), 4)
        with self.assertRaises(PicoError):
            pico.channel_index('E', 'ps5000a')


class TestTimebaseSelection(PicoTestCase):
    """The rate asked for is a request; the ladder decides what is possible."""

    def chosen(self, sample_rate, **kwargs):
        scope = self.open_scope(sample_rate=sample_rate, **kwargs)
        self.acquire()
        return scope._timebase, scope._interval

    def test_it_samples_at_least_as_fast_as_asked(self):
        timebase, interval = self.chosen(1e6)
        self.assertLessEqual(interval, 1e-6 + 1e-18)

        # And it is the SLOWEST timebase that still satisfies the request:
        # one step slower must be too slow. Oversampling by 1000x would eat
        # capture memory for nothing.
        slower, _ = self.backend.timebase(timebase + 1, 64)
        self.assertGreater(slower, 1e-6)

    def test_a_slow_rate_does_not_need_a_scan_through_the_whole_ladder(self):
        # 1 kHz is timebase ~125002 on this ladder. Bracket-and-bisect finds
        # it in a few dozen probes; the linear scan this replaced would have
        # taken 125000.
        timebase, interval = self.chosen(1e3)
        self.assertGreater(timebase, 100000)
        self.assertLessEqual(interval, 1e-3 + 1e-15)

    def test_the_fastest_available_is_used_when_the_request_is_impossible(self):
        timebase, interval = self.chosen(10e9)  # 10 GS/s: not on this unit
        self.assertEqual(timebase, 0)
        self.assertEqual(interval, 1e-9)

    def test_the_m_file_records_the_rate_actually_used(self):
        # Not the rate that was asked for. A config asking for 3 MS/s on a
        # ladder with no 3 MS/s rung gets a real number in scp.hint.
        scope = self.open_scope(sample_rate=3e6)
        self.acquire()
        interval = scope._interval
        self.scope.disconnect()
        self.scope = None
        self.assertAlmostEqual(
            float(read_m(f"{self.base}.m")['hint']), interval, places=15
        )


class TestDiscovery(unittest.TestCase):
    """Exactly one unit, or a refusal that says what to do."""

    def test_one_unit_is_found_past_three_missing_libraries(self):
        backend, serial = pico.discover(
            backend_factory=factory({'ps3000a': ['XY001/002']})
        )
        self.assertEqual(backend.series, 'ps3000a')
        self.assertEqual(serial, 'XY001/002')

    def test_two_units_stop_the_run_and_print_the_lines_to_paste(self):
        with self.assertRaises(PicoError) as caught:
            pico.discover(backend_factory=factory({
                'ps2000a': ['AAA/1'], 'ps5000a': ['BBB/2'],
            }))
        message = str(caught.exception)
        self.assertIn("series ps2000a", message)
        self.assertIn("serial BBB/2", message)

    def test_a_serial_settles_which_of_two_units_is_meant(self):
        backend, serial = pico.discover(
            serial='BBB/2',
            backend_factory=factory({
                'ps2000a': ['AAA/1'], 'ps5000a': ['BBB/2'],
            }),
        )
        self.assertEqual(backend.series, 'ps5000a')
        self.assertEqual(serial, 'BBB/2')

    def test_an_unknown_serial_names_what_is_actually_attached(self):
        with self.assertRaises(PicoError) as caught:
            pico.discover(
                serial='NOPE',
                backend_factory=factory({'ps5000a': ['AAA/1']}),
            )
        self.assertIn("AAA/1", str(caught.exception))

    def test_no_unit_at_all_says_the_non_a_libraries_are_not_driven(self):
        with self.assertRaises(PicoError) as caught:
            pico.discover(backend_factory=factory({'ps5000a': []}))
        self.assertIn("without an 'a'", str(caught.exception).lower())


class TestTraceFiles(PicoTestCase):

    def test_a_trace_is_int16_counts_one_row_per_channel(self):
        self.open_scope(channels=['A', 'B'], n_samples=64)
        self.assertTrue(self.acquire())

        rows = self.dat(points=64)
        self.assertEqual(rows.shape, (2, 64))
        self.assertEqual(rows.dtype, np.int16)

    def test_captures_become_rows_in_segment_then_channel_order(self):
        # The layout the format fixes: for each segment, one row per channel.
        self.open_scope(channels=['A', 'B'], n_samples=32, captures=3)
        self.acquire()

        rows = self.dat(points=32)
        self.assertEqual(rows.shape, (6, 32))
        # The fake offsets each segment by its index, so row pairs differ by
        # exactly one count between consecutive segments.
        self.assertTrue(np.array_equal(rows[2] - rows[0], np.ones(32)))
        self.assertTrue(np.array_equal(rows[3] - rows[1], np.ones(32)))

    def test_vgain_maps_the_counts_back_to_volts(self):
        self.open_scope(channels=['A'], v_max=[1.0], n_samples=64)
        self.acquire()
        rows = self.dat(points=64)

        meta = read_m(f"{self.base}.m")
        vgain = m_numbers(meta, 'vgain')[0]
        voff = m_numbers(meta, 'voff')[0]
        volts = vgain * rows[0].astype(np.float64) - voff

        # The fake's waveform peaks at 0.4 of full scale, so a +/-1 V range
        # puts it at 0.4 V. Within one count.
        self.assertAlmostEqual(np.abs(volts).max(), 0.4, places=3)
        self.assertLessEqual(np.abs(volts).max(), 1.0)

    def test_the_first_trace_truncates_and_later_traces_append(self):
        self.open_scope(n_samples=16)
        for index in range(4):
            self.acquire(index)
        self.assertEqual(self.dat().size, 4 * 16)

    def test_rerunning_the_same_config_starts_a_fresh_file(self):
        self.open_scope(n_samples=16)
        self.acquire(0)
        self.acquire(1)
        self.acquire(0)  # a second run of the config
        self.assertEqual(self.dat().size, 16)

    def test_averaging_collapses_segments_into_one_row(self):
        self.open_scope(channels=['A'], n_samples=32, averages=4)
        self.acquire()

        rows = self.dat(points=32)
        self.assertEqual(rows.shape, (1, 32))

        # The fake offsets segment s by +s, so averaging 0..3 adds 1.5, which
        # rounds to 2 on a trace whose samples are even. What matters is that
        # the average is of the whole group, not the first segment.
        self.assertEqual(self.backend.captures, 4)

    def test_averaging_is_recorded_in_n_averages_not_in_the_trace_count(self):
        self.open_scope(channels=['A'], n_samples=32, averages=100)
        self.acquire()
        self.scope.disconnect()
        self.scope = None

        meta = read_m(f"{self.base}.m")
        self.assertEqual(meta['n_averages'], '[100]')
        self.assertEqual(meta['multitraces'], '1')

    def test_a_pretrigger_record_puts_the_trigger_inside_the_trace(self):
        self.open_scope(
            channels=['A'], n_samples=100, pretrigger=20,
            trigger={'source': 'A', 'level': 0.1, 'direction': 'rising'},
        )
        self.acquire()
        interval = self.scope._interval
        self.scope.disconnect()
        self.scope = None

        # hoff is the time of the first sample relative to the trigger, so a
        # 20% pre-trigger record starts 20 samples BEFORE it.
        self.assertAlmostEqual(
            float(read_m(f"{self.base}.m")['hoff']), -20 * interval, places=15
        )


class TestMetadataContract(PicoTestCase):
    """The .m is read by every analysis script in the lab."""

    def test_the_field_set_is_exactly_what_the_lecroy_path_writes(self):
        self.open_scope(channels=['A', 'B'], n_samples=8)
        self.acquire()
        self.scope.disconnect()
        self.scope = None

        self.assertEqual(sorted(read_m(f"{self.base}.m")), sorted([
            'version', 'hint', 'hoff', 'vgain', 'voff', 'points_per_trace',
            'n_traces', 'n_channels', 'format', 'dataname', 'n_averages',
            'multitraces', 'channels', 'trigtimename',
            'trigtime_rows_per_trace', 'trigtime_cols',
        ]))

    def test_no_trigtime_sidecar_is_claimed_or_written(self):
        # Block mode reports a segment's sub-sample trigger offset but not
        # the time between triggers. Half a pair would be read as a zero.
        self.open_scope(channels=['A'], n_samples=8, captures=4)
        self.acquire()
        self.scope.disconnect()
        self.scope = None

        meta = read_m(f"{self.base}.m")
        self.assertEqual(meta['trigtimename'], "''")
        self.assertEqual(meta['trigtime_rows_per_trace'], '0')
        self.assertFalse(os.path.exists(f"{self.base}_trigtime.dat"))

    def test_n_traces_is_corrected_at_teardown_for_a_short_run(self):
        # The .m is written on the first trace from the predicted grid size.
        # An aborted run leaves it claiming traces the .dat does not hold.
        self.open_scope(channels=['A'], n_samples=8)
        self.scope.acquire_multi_channel(
            channels=['A'], output_base_name=self.base,
            is_first_trace=True, total_loops=50,
        )
        self.assertEqual(read_m(f"{self.base}.m")['n_traces'], '50')

        self.assertTrue(self.scope.finalize_matlab_metadata())
        self.assertEqual(read_m(f"{self.base}.m")['n_traces'], '1')
        # Idempotent: nothing left to correct the second time.
        self.assertFalse(self.scope.finalize_matlab_metadata())


class TestPlaceholderPadding(PicoTestCase):

    def test_a_placeholder_matches_the_geometry_and_counts_as_a_trace(self):
        self.open_scope(channels=['A', 'B'], n_samples=16)
        self.acquire(0)
        self.assertTrue(self.scope.write_placeholder_trace(self.base))
        self.acquire(2)

        rows = self.dat(points=16)
        self.assertEqual(rows.shape, (6, 16))
        # Trace 1 is the padding: all zeros, and the real trace 2 landed in
        # the third slot rather than the second.
        self.assertTrue(np.all(rows[2:4] == 0))
        self.assertFalse(np.all(rows[4:6] == 0))

    def test_nothing_to_pad_from_is_reported_rather_than_guessed(self):
        self.open_scope()
        self.assertFalse(self.scope.write_placeholder_trace(self.base))


class TestRefusals(PicoTestCase):
    """What must not reach the .dat."""

    def test_a_trigger_that_never_fires_is_a_dropped_trace_not_a_hang(self):
        backend = FakePicoBackend(ready=False)
        self.open_scope(
            backend=backend, n_samples=16, timeout=0.05,
            trigger={'source': 'A', 'level': 0.5, 'direction': 'rising'},
        )
        self.assertFalse(self.acquire())
        # And the unit is left idle rather than armed.
        self.assertGreaterEqual(backend.stops, 1)

        self.scope.disconnect()
        self.scope = None
        self.assertFalse(os.path.exists(f"{self.base}.dat"))

    def test_a_short_read_is_discarded_rather_than_written(self):
        self.open_scope(backend=FakePicoBackend(short_by=1), n_samples=16)
        self.assertFalse(self.acquire())

        self.scope.disconnect()
        self.scope = None
        self.assertFalse(os.path.exists(f"{self.base}.dat"))

    def test_more_samples_than_the_segment_holds_is_refused_up_front(self):
        # Refused, not truncated: a shorter record than the config asked for
        # would be read back with the wrong time axis.
        self.open_scope(
            backend=FakePicoBackend(segment_capacity=1000),
            n_samples=500, captures=10,
        )
        with self.assertRaises(PicoError) as caught:
            self.acquire()
        self.assertIn("500", str(caught.exception))

    def test_triggering_on_a_channel_that_is_not_recorded_is_refused(self):
        with self.assertRaises(PicoError) as caught:
            self.open_scope(
                channels=['A'], n_samples=16,
                trigger={'source': 'B', 'level': 0.5, 'direction': 'rising'},
            )
            self.acquire()
        self.assertIn("B", str(caught.exception))

    def test_a_channel_list_that_differs_from_the_block_is_refused(self):
        # v_max and coupling are positional, so a different channel list
        # would apply the wrong range to the wrong input.
        self.open_scope(channels=['A'], v_max=[0.1], n_samples=16)
        with self.assertRaises(PicoError):
            self.scope.acquire_multi_channel(
                channels=['B'], output_base_name=self.base,
                is_first_trace=True,
            )

    def test_acquiring_before_connecting_is_refused(self):
        scope = PicoScope(channels=['A'], backend=FakePicoBackend())
        with self.assertRaises(PicoError):
            scope.acquire_multi_channel(
                channels=['A'], output_base_name=self.base
            )
        scope.disconnect()

    def test_a_per_channel_list_of_the_wrong_length_is_refused(self):
        with self.assertRaises(PicoError):
            PicoScope(channels=['A', 'B', 'C'], v_max=[1.0, 2.0])

    def test_nonsense_settings_are_refused_before_any_call(self):
        for kwargs in (
            {'sample_rate': 0},
            {'n_samples': 0},
            {'averages': 0},
            {'timeout': -1},
            {'pretrigger': 150},
            {'coupling': 'sideways'},
            {'series': 'ps2000'},
            {'trigger': {'source': 'A', 'level': 1.0, 'direction': 'sideways'}},
        ):
            with self.assertRaises(PicoError, msg=f"accepted {kwargs}"):
                PicoScope(channels=['A'], **kwargs)


class TestConfiguration(PicoTestCase):
    """What the driver puts on the unit, which nothing else can see."""

    def test_unused_inputs_are_switched_off_explicitly(self):
        # The enabled channel count decides which timebases exist, so a
        # channel left in an unknown state makes the sample rate
        # unreproducible between runs.
        self.open_scope(channels=['B'], n_samples=16)
        self.acquire()
        enabled = {i: state[0] for i, state in self.backend.channels.items()}
        self.assertEqual(enabled, {0: False, 1: True, 2: False, 3: False})

    def test_coupling_and_range_follow_the_channel_order(self):
        self.open_scope(
            channels=['A', 'B'], v_max=[0.05, 5.0], coupling=['ac', 'dc'],
            n_samples=16,
        )
        self.acquire()
        self.assertEqual(self.backend.channels[0][1:],
                         (pico.COUPLINGS['ac'], pico.resolve_range(0.05, 'ps5000a')))
        self.assertEqual(self.backend.channels[1][1:],
                         (pico.COUPLINGS['dc'], pico.resolve_range(5.0, 'ps5000a')))

    def test_a_single_value_applies_to_every_channel(self):
        self.open_scope(channels=['A', 'B'], v_max=2.0, coupling='ac',
                        n_samples=16)
        self.acquire()
        self.assertEqual(self.backend.channels[0][1:],
                         self.backend.channels[1][1:])

    def test_the_trigger_level_is_converted_to_counts_on_its_own_range(self):
        self.open_scope(
            channels=['A'], v_max=[1.0], n_samples=16,
            trigger={'source': 'A', 'level': 0.5, 'direction': 'falling'},
        )
        self.acquire()

        source, threshold, direction, auto_ms = self.backend.trigger
        self.assertEqual(source, 0)
        self.assertEqual(direction, pico.EDGES['falling'])
        # Half of a +/-1 V range is half of full-scale counts.
        self.assertEqual(threshold, round(0.5 * self.backend.max_adc()))
        # 0 means wait indefinitely: the block's own timeout ends the wait,
        # so a missed trigger is a reported drop rather than a trace of
        # whatever the input happened to be doing.
        self.assertEqual(auto_ms, 0)

    def test_no_trigger_line_means_capture_immediately(self):
        self.open_scope(channels=['A'], n_samples=16)
        self.acquire()
        self.assertIsNone(self.backend.trigger)

    def test_the_external_input_can_be_the_trigger_source(self):
        self.open_scope(
            channels=['A'], n_samples=16,
            trigger={'source': 'ext', 'level': 2.5, 'direction': 'rising'},
        )
        self.acquire()
        source, threshold, _direction, _auto = self.backend.trigger
        self.assertEqual(source, pico.SERIES['ps5000a']['external'])
        # The external input is fixed at +/-5 V, so 2.5 V is half scale.
        self.assertEqual(threshold, round(0.5 * self.backend.max_adc()))

    def test_the_unit_is_configured_once_and_not_at_every_coordinate(self):
        self.open_scope(channels=['A'], n_samples=16)
        for index in range(5):
            self.acquire(index)

        # Five captures, one configuration: re-deriving the timebase per
        # coordinate would put driver round trips in the per-trace path.
        self.assertEqual(self.backend.runs, 5)
        self.assertEqual(self.backend.captures, 1)
        self.assertEqual(len(self.backend.channels), 4)

    def test_buffers_are_re_registered_before_every_retrieval(self):
        # A registration the driver has forgotten returns PICO_OK and leaves
        # the buffer holding the PREVIOUS trace - the one failure that would
        # reach the data file silently.
        self.open_scope(channels=['A', 'B'], n_samples=16, captures=2)
        self.acquire(0)
        self.acquire(1)
        self.assertEqual(len(self.backend.buffers), 4)  # 2 channels x 2 segments


class TestLeCroyFormatUnchanged(unittest.TestCase):
    """The writer was lifted out of lecroy_driver; the bytes did not move.

    Pinned against a literal rather than against the other driver, so this
    still fails if both are changed together.
    """

    def test_the_m_file_is_byte_for_byte_what_it_always_was(self):
        directory = tempfile.mkdtemp()
        base = os.path.join(directory, "golden")
        writer = ScopeTraceWriter()
        try:
            writer.record_trace(
                base, ['C1', 'C2'],
                [{'v_gain': 1.5e-4, 'v_offset': -0.5},
                 {'v_gain': 3.0e-4, 'v_offset': 0.25}],
                np.zeros((4, 8), dtype=np.int16),
                is_first_trace=True, total_loops=7, h_interval=2e-9,
                h_offset=-1e-7, points=8, segments=2, sweeps=[1000, 100],
                trigtime=np.zeros((2, 2), dtype=np.float64),
            )
            writer.flush()
            with open(f"{base}.m") as handle:
                written = handle.read()
        finally:
            for name in os.listdir(directory):
                os.unlink(os.path.join(directory, name))
            os.rmdir(directory)

        self.assertEqual(written, """% scope metadata file version 1.0 June 2026
function scp=golden()
scp.version=1.0;
scp.hint=2.000000e-09;
scp.hoff=-1.000000e-07;
scp.vgain=[1.500000e-04,3.000000e-04];
scp.voff=[-5.000000e-01,2.500000e-01];
scp.points_per_trace=8;
scp.n_traces=28;
scp.n_channels=2;
scp.format=2;
scp.dataname='""" + f"{base}.dat" + """';
scp.n_averages=[1000,100];
scp.multitraces=2;
scp.channels={'C1','C2'};
scp.trigtimename='""" + f"{base}_trigtime.dat" + """';
scp.trigtime_rows_per_trace=2;
scp.trigtime_cols=2;
""")


if __name__ == '__main__':
    unittest.main()
