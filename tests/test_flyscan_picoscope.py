"""The fly line on a USB PicoScope: one segment per copeak.

What matters here is not that the capture works - it is that a line which
cannot be trusted is refused rather than written. A fly line maps segment i
to position i, so a missed delay sweep does not shorten the line, it
misaligns every pixel in it, and the resulting map looks perfectly fine.

The other thing pinned below is the contract itself: flyscan.raster and
flyscan.stage must not be able to tell a PicoScope from a LeCroy, so
download_line() has to return the same keys with the same meanings.

Runs against tests/fake_picoscope.py. No scope, no PicoSDK.
"""

import unittest

import numpy as np

from flyscan.scope import DEFAULT_USB_TRANSFER_MB_S, FlyPicoScope, ScopeConfigError
from pscan import picoscope_driver as pico
from tests.fake_picoscope import FakePicoBackend

TRIGGER = {'source': 'ext', 'level': 1.0, 'direction': 'rising'}


class FlyPicoTestCase(unittest.TestCase):

    def open_scope(self, backend=None, **kwargs):
        kwargs.setdefault('channels', ['A', 'B'])
        kwargs.setdefault('trigger', TRIGGER)
        self.backend = backend or FakePicoBackend()
        self.scope = FlyPicoScope(backend=self.backend, **kwargs)
        self.scope.connect()
        return self.scope

    def configure(self, segments=8, segment_seconds=50e-6, sample_rate=1e6,
                  delay_rate=10e3):
        return self.scope.configure_line(
            segments=segments, segment_seconds=segment_seconds,
            sample_rate=sample_rate, delay_rate=delay_rate,
        )

    def tearDown(self):
        scope = getattr(self, 'scope', None)
        if scope is not None:
            scope.disconnect()


class TestConfiguration(FlyPicoTestCase):

    def test_configure_line_reports_what_the_lecroy_path_reports(self):
        self.open_scope()
        result = self.configure(segments=8, segment_seconds=50e-6,
                                sample_rate=1e6)

        self.assertEqual(result['segments'], 8)
        self.assertEqual(result['points_per_segment'], 50)
        self.assertEqual(result['requested_interval'], 1e-6)
        self.assertGreater(result['timeout_s'], 0)

    def test_one_segment_per_copeak_is_asked_of_the_unit(self):
        self.open_scope()
        self.configure(segments=64)
        self.assertEqual(self.backend.captures, 64)
        self.assertEqual(self.backend.segments, 64)

    def test_unused_inputs_are_switched_off(self):
        # The enabled channel count decides which timebases and how much
        # capture memory exist, so an unknown channel state makes both the
        # sample rate and the segment count unreproducible between runs.
        self.open_scope(channels=['A'])
        self.configure()
        self.assertEqual(
            {i: state[0] for i, state in self.backend.channels.items()},
            {0: True, 1: False, 2: False, 3: False},
        )

    def test_ranges_and_coupling_are_keyed_by_channel_name(self):
        self.open_scope(channels=['A', 'C'],
                        v_max={'A': 0.05, 'C': 5.0},
                        coupling={'A': 'ac', 'C': 'dc'})
        self.configure()
        self.assertEqual(self.backend.channels[0][1:],
                         (pico.COUPLINGS['ac'],
                          pico.resolve_range(0.05, 'ps5000a')))
        self.assertEqual(self.backend.channels[2][1:],
                         (pico.COUPLINGS['dc'],
                          pico.resolve_range(5.0, 'ps5000a')))

    def test_the_trigger_never_self_triggers(self):
        # A unit that gave up waiting and triggered itself would hand back a
        # segment that is not a delay sweep - the same failure TRMD AUTO
        # causes on a LeCroy, and it looks like good data.
        self.open_scope()
        self.configure()
        source, threshold, direction, auto_ms = self.backend.trigger
        self.assertEqual(source, pico.SERIES['ps5000a']['external'])
        self.assertEqual(direction, pico.EDGES['rising'])
        self.assertEqual(auto_ms, 0)
        # 1 V on the +/-5 V external input.
        self.assertEqual(threshold, round(0.2 * self.backend.max_adc()))

    def test_a_fly_line_without_a_trigger_is_refused_at_construction(self):
        with self.assertRaises(ValueError) as caught:
            FlyPicoScope(channels=['A'], trigger=None,
                         backend=FakePicoBackend())
        self.assertIn("delay axis", str(caught.exception))

    def test_a_segment_longer_than_the_memory_allows_is_refused(self):
        # Refused, not truncated: every segment is one copeak, so a shorter
        # one cannot simply be cut down.
        self.open_scope(backend=FakePicoBackend(segment_capacity=1000))
        with self.assertRaises(ScopeConfigError) as caught:
            self.configure(segments=100, segment_seconds=50e-6,
                           sample_rate=1e6)
        self.assertIn("one copeak", str(caught.exception))

    def test_a_rate_the_unit_cannot_reach_is_refused_not_halved(self):
        # The sample rate sets the delay resolution, so silently halving it
        # would silently halve what the file resolves.
        self.open_scope()
        with self.assertRaises(ScopeConfigError) as caught:
            # A short window, so this fails on the timebase rather than on
            # the capture memory.
            self.configure(segment_seconds=1e-8, sample_rate=10e9)
        self.assertIn("delay resolution", str(caught.exception))

    def test_the_timebase_is_the_slowest_one_fast_enough(self):
        self.open_scope()
        self.configure(sample_rate=1e6)
        self.assertLessEqual(self.scope._interval, 1e-6 + 1e-18)
        slower, _ = self.backend.timebase(self.scope._timebase + 1, 50)
        self.assertGreater(slower, 1e-6)

    def test_the_timeout_is_sized_from_the_delay_clock(self):
        # A line is complete when `segments` sweeps have arrived, so what
        # bounds the wait is the clock - not the bus.
        self.open_scope()
        # 100 sweeps at 10 Hz is a 10 s line, so the timeout has to be well
        # past the floor - sized by the clock, not by the record length.
        result = self.configure(segments=100, delay_rate=10.0)
        self.assertAlmostEqual(result['timeout_s'], 30.0, places=6)

    def test_an_explicit_timeout_wins(self):
        self.open_scope(timeout_s=2.5)
        self.assertEqual(self.configure()['timeout_s'], 2.5)

    def test_a_flat_line_is_one_segment(self):
        self.open_scope()
        result = self.scope.configure_flat_line(
            line_seconds=100e-6, sample_rate=1e6, delay_rate=10e3
        )
        self.assertEqual(result['segments'], 1)
        self.assertEqual(result['points_per_segment'], 100)


class TestLineContract(FlyPicoTestCase):
    """download_line() must be indistinguishable from the LeCroy's."""

    def setUp(self):
        self.open_scope(channels=['A', 'B'], v_max={'A': 1.0, 'B': 5.0})
        self.configure(segments=8, segment_seconds=50e-6, sample_rate=1e6)
        self.line = self.scope.acquire_line(['A', 'B'], {'A': 1, 'B': 10})

    def test_samples_are_segments_by_points_int16(self):
        self.assertEqual(self.line['A']['samples'].shape, (8, 50))
        self.assertEqual(self.line['A']['samples'].dtype, np.int16)

    def test_sparsing_shortens_the_segment_it_is_applied_to(self):
        # 10x on B, which is what makes a slowly-varying channel ride along
        # at a tenth of the transfer cost.
        self.assertEqual(self.line['B']['samples'].shape, (8, 5))

    def test_a_sparsed_channel_holds_every_nth_sample(self):
        # Not the first five: decimation happens in the scope, and a channel
        # that quietly returned the head of the record would have the right
        # shape and the wrong delay axis.
        full = self.backend.waveform(1, 0, 50)
        self.assertTrue(
            np.array_equal(self.line['B']['samples'][0], full[::10])
        )

    def test_the_meta_fields_raster_reads_are_all_present(self):
        meta = self.line['A']['meta']
        for key in ('vertical_gain', 'vertical_offset', 'segments',
                    'points_per_segment', 'horiz_interval',
                    'sparsing_factor', 'sweeps_per_acq', 'nominal_bits'):
            self.assertIn(key, meta)

    def test_vertical_gain_turns_counts_into_volts(self):
        meta = self.line['A']['meta']
        volts = (meta['vertical_gain']
                 * self.line['A']['samples'].astype(np.float64)
                 - meta['vertical_offset'])
        # The fake's waveform peaks at 0.4 of full scale, and A is on +/-1 V.
        # Within a sample of the peak: 50 points per period do not land on
        # the crest.
        self.assertAlmostEqual(np.abs(volts).max(), 0.4, delta=0.005)

    def test_a_sparsed_channels_interval_is_its_own(self):
        # The delay axis of a decimated channel steps 10x further per sample.
        self.assertAlmostEqual(
            self.line['B']['meta']['horiz_interval'],
            self.line['A']['meta']['horiz_interval'] * 10,
        )

    def test_achieved_carries_what_came_back(self):
        achieved = self.line['achieved']['B']
        self.assertEqual(achieved['segments'], 8)
        self.assertEqual(achieved['points_per_segment'], 5)
        self.assertEqual(achieved['sparsing_factor'], 10)
        self.assertEqual(achieved['requested_sparsing'], 10)

    def test_samples_are_a_copy_and_not_the_live_buffer(self):
        # raster hands this array to the writer, and the buffer is reused on
        # the next line.
        first = self.line['A']['samples'].copy()
        self.scope.acquire_line(['A', 'B'], {'A': 1, 'B': 10})
        self.assertTrue(np.array_equal(self.line['A']['samples'], first))

    def test_buffers_are_sized_to_the_decimated_length_and_reused(self):
        # A 100x-sparsed channel holds a hundredth of what the signal
        # channel does, and neither is re-allocated per line: at thousands
        # of segments both would be real costs.
        self.assertEqual(self.scope._buffers['A'].shape, (8, 50))
        self.assertEqual(self.scope._buffers['B'].shape, (8, 5))

        before = {name: id(buffer)
                  for name, buffer in self.scope._buffers.items()}
        self.scope.acquire_line(['A', 'B'], {'A': 1, 'B': 10})
        self.assertEqual(
            {name: id(buffer)
             for name, buffer in self.scope._buffers.items()},
            before,
        )

    def test_each_channel_is_transferred_once(self):
        # A channel left registered would be read again at the next one's
        # decimation ratio: wasted bandwidth, and wrong data in its buffer.
        channels = [channel for channel, _ratio, _mode
                    in self.backend.retrievals]
        self.assertEqual(sorted(channels), [0] * 8 + [1] * 8)


class TestSegmentTimes(FlyPicoTestCase):
    """Block mode does not timestamp captures, so where do the times come from."""

    def test_column_one_is_the_delay_clock(self):
        self.open_scope(channels=['A'])
        self.configure(segments=5, delay_rate=10e3)
        line = self.scope.acquire_line(['A'])

        trigtime = line['trigtime']
        self.assertEqual(trigtime.shape, (5, 2))
        # Segment i fires at i / delay_rate, which is exact for a fixed-rate
        # clock - and the capture count check is what rules out a missed one.
        self.assertTrue(np.allclose(
            trigtime[:, 0], np.arange(5) / 10e3
        ))

    def test_column_two_is_the_measured_sub_sample_offset(self):
        self.open_scope(channels=['A'])
        self.configure(segments=3)
        line = self.scope.acquire_line(['A'])

        self.assertTrue(np.allclose(
            line['trigtime'][:, 1], [1e-10, 2e-10, 3e-10]
        ))
        self.assertEqual(line['trigtime_source'],
                         'delay_clock+measured_offset')

    def test_a_unit_that_will_not_report_offsets_says_so(self):
        # Rather than implying a measurement that was not made.
        backend = FakePicoBackend()
        backend.offsets_unavailable = True
        self.open_scope(backend=backend, channels=['A'])
        self.configure(segments=3)
        line = self.scope.acquire_line(['A'])

        self.assertTrue(np.all(line['trigtime'][:, 1] == 0.0))
        self.assertEqual(line['trigtime_source'], 'delay_clock')

    def test_the_offsets_are_only_asked_for_once_per_scan(self):
        backend = FakePicoBackend()
        backend.offsets_unavailable = True
        self.open_scope(backend=backend, channels=['A'])
        self.configure(segments=3)

        self.scope.acquire_line(['A'])
        self.assertTrue(self.scope._offsets_unavailable)


class TestRefusals(FlyPicoTestCase):

    def test_a_short_capture_count_fails_the_line(self):
        # The one that matters: a missed delay sweep misaligns every pixel,
        # and the map would look perfectly plausible.
        backend = FakePicoBackend()
        self.open_scope(backend=backend, channels=['A'])
        self.configure(segments=8)
        backend.captured_override = 7

        with self.assertRaises(RuntimeError) as caught:
            self.scope.acquire_line(['A'])
        self.assertIn("misaligned", str(caught.exception))

    def test_a_short_segment_fails_the_line(self):
        self.open_scope(backend=FakePicoBackend(short_by=1), channels=['A'])
        self.configure(segments=4)
        with self.assertRaises(RuntimeError) as caught:
            self.scope.acquire_line(['A'])
        self.assertIn("delay axis", str(caught.exception))

    def test_a_trigger_that_never_arrives_fails_the_line(self):
        backend = FakePicoBackend(ready=False)
        self.open_scope(backend=backend, channels=['A'], timeout_s=0.05)
        self.configure(segments=2, delay_rate=10e3)

        self.scope.arm_line()
        with self.assertRaises(RuntimeError) as caught:
            self.scope.wait_for_line()
        self.assertIn("delay clock", str(caught.exception))
        # And it is left idle rather than armed.
        self.assertGreaterEqual(backend.stops, 1)

    def test_a_channel_that_was_not_configured_is_refused(self):
        self.open_scope(channels=['A'])
        self.configure()
        with self.assertRaises(ValueError):
            self.scope.download_line(['B'])

    def test_arming_before_configuring_is_refused(self):
        self.open_scope()
        with self.assertRaises(RuntimeError):
            self.scope.arm_line()

    def test_configuring_before_connecting_is_refused(self):
        scope = FlyPicoScope(channels=['A'], trigger=TRIGGER,
                             backend=FakePicoBackend())
        with self.assertRaises(RuntimeError):
            scope.configure_line(segments=4, segment_seconds=50e-6,
                                 sample_rate=1e6, delay_rate=10e3)

    def test_triggering_on_an_unrecorded_channel_is_refused(self):
        self.open_scope(
            channels=['A'],
            trigger={'source': 'B', 'level': 0.5, 'direction': 'rising'},
        )
        with self.assertRaises(ValueError) as caught:
            self.configure()
        self.assertIn("switched on", str(caught.exception))

    def test_a_level_outside_the_trigger_range_is_refused(self):
        self.open_scope(
            channels=['A'],
            trigger={'source': 'ext', 'level': 50.0, 'direction': 'rising'},
        )
        with self.assertRaises(ValueError) as caught:
            self.configure()
        self.assertIn("never fire", str(caught.exception))


class TestBudget(FlyPicoTestCase):

    def test_sparsing_reduces_what_crosses_the_bus(self):
        # Decimation happens in the scope, so this is a real saving and not
        # just a smaller file - which is what makes the number honest.
        self.open_scope(channels=['A', 'B'])
        self.configure(segments=10, segment_seconds=50e-6, sample_rate=1e6)

        budget = self.scope.line_budget(['A', 'B'], {'A': 1, 'B': 10})
        self.assertEqual(budget['per_channel_points']['A'], 500)
        self.assertEqual(budget['per_channel_points']['B'], 50)
        self.assertEqual(budget['total_points'], 550)
        self.assertAlmostEqual(budget['fill_s'], 10 / 10e3)

    def test_usb_is_assumed_faster_than_vxi11(self):
        self.open_scope(channels=['A'])
        self.assertEqual(self.scope.transfer_mb_s, DEFAULT_USB_TRANSFER_MB_S)
        self.assertGreater(DEFAULT_USB_TRANSFER_MB_S, 4.0)


FLY_CONFIG = """
    action apt_fly
    axis 0
    scan 53.500 53.540
    velocity 0.2
    limits 50.0 57.0
    spot 1.0
    end

    action apt_stage
    axis 1
    scan 37.000 37.010 0.005
    end

    action scope_line
    {scope}
    channels A, B, C
    roles ac, dc, trigger
    sparsing 1, 100, 20
    sample_rate 1e6
    segment_window 50e-6
    rep_rate 80e6
    end
"""


class TestFlyConfigBlock(unittest.TestCase):
    """'action scope_line' has to describe either instrument, unambiguously."""

    def parse(self, scope):
        import os
        import tempfile
        import textwrap

        from flyscan.config import parse_fly_config

        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.con', delete=False, newline='\n'
        )
        try:
            handle.write(textwrap.dedent(
                FLY_CONFIG.format(scope=textwrap.dedent(scope).strip())
            ))
            handle.close()
            return parse_fly_config(handle.name, counts_per_mm=20000.0)
        finally:
            os.unlink(handle.name)

    def assert_rejected(self, scope, fragment):
        from flyscan.config import FlyConfigError

        with self.assertRaises(FlyConfigError) as caught:
            self.parse(scope)
        self.assertIn(fragment, str(caught.exception))

    def test_a_usb_block_compiles(self):
        config = self.parse("""
            usb
            series ps5000a
            serial AB123/456
            v_max 0.2, 5.0, 5.0
            coupling ac, dc, dc
            trigger ext 1.0 rising
            timeout 2.5
            resolution 12
        """)
        self.assertEqual(config.scope_kind, 'picoscope')
        pico_cfg = config.picoscope
        self.assertEqual(pico_cfg['series'], 'ps5000a')
        self.assertEqual(pico_cfg['serial'], 'AB123/456')
        self.assertEqual(pico_cfg['v_max'], {'A': 0.2, 'B': 5.0, 'C': 5.0})
        self.assertEqual(pico_cfg['coupling'],
                         {'A': 'ac', 'B': 'dc', 'C': 'dc'})
        self.assertEqual(pico_cfg['trigger'],
                         {'source': 'ext', 'level': 1.0,
                          'direction': 'rising'})
        self.assertEqual(pico_cfg['timeout'], 2.5)
        self.assertEqual(pico_cfg['resolution'], 12)

    def test_usb_alone_is_enough_to_name_the_instrument(self):
        config = self.parse("""
            usb
            trigger ext 1.0 rising
        """)
        self.assertEqual(config.scope_kind, 'picoscope')
        self.assertIsNone(config.picoscope['series'])

    def test_the_lecroy_block_is_untouched(self):
        config = self.parse("""
            ip 192.168.1.100
        """)
        self.assertEqual(config.scope_kind, 'lecroy')
        self.assertEqual(config.scope_ip, '192.168.1.100')
        self.assertEqual(config.transfer_mb_s, 4.0)

    def test_usb_assumes_a_faster_transfer_than_vxi11(self):
        config = self.parse("""
            usb
            trigger ext 1.0 rising
        """)
        self.assertEqual(config.transfer_mb_s, DEFAULT_USB_TRANSFER_MB_S)

    def test_naming_both_interfaces_is_refused(self):
        self.assert_rejected("""
            ip 192.168.1.100
            usb
            trigger ext 1.0 rising
        """, "ambiguous")

    def test_pscans_picoscope_block_is_refused_with_the_reason(self):
        # Not "unknown action type": it is a real block, in the wrong
        # orchestrator, and the message says what to write instead.
        import os
        import tempfile
        import textwrap

        from flyscan.config import FlyConfigError, parse_fly_config

        body = FLY_CONFIG.format(scope="usb\ntrigger ext 1.0 rising") + """
            action picoscope
            channels A
            end
        """
        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.con', delete=False, newline='\n'
        )
        try:
            handle.write(textwrap.dedent(body))
            handle.close()
            with self.assertRaises(FlyConfigError) as caught:
                parse_fly_config(handle.name, counts_per_mm=20000.0)
            self.assertIn("'action scope_line' with a 'usb' line",
                          str(caught.exception))
        finally:
            os.unlink(handle.name)

    def test_naming_neither_is_refused(self):
        self.assert_rejected("""
            sparsing 1, 100, 20
        """, "names no instrument")

    def test_a_usb_block_without_a_trigger_is_refused(self):
        # Every segment is one delay sweep; without a trigger they are not.
        self.assert_rejected("""
            usb
        """, "not a delay sweep")

    def test_a_per_channel_list_of_the_wrong_length_is_refused(self):
        self.assert_rejected("""
            usb
            trigger ext 1.0 rising
            v_max 1.0, 2.0
        """, "one per channel")

    def test_one_v_max_applies_to_every_channel(self):
        config = self.parse("""
            usb
            trigger ext 1.0 rising
            v_max 2.0
        """)
        self.assertEqual(set(config.picoscope['v_max'].values()), {2.0})

    def test_omitting_v_max_warns_rather_than_silently_defaulting(self):
        config = self.parse("""
            usb
            trigger ext 1.0 rising
        """)
        self.assertTrue(any("no 'v_max'" in w for w in config.warnings))

    def test_triggering_on_a_recorded_channel_warns_about_the_cost(self):
        config = self.parse("""
            usb
            trigger C 1.0 rising
        """)
        self.assertTrue(any("spends an input" in w for w in config.warnings))

    def test_an_unknown_series_is_refused(self):
        self.assert_rejected("""
            usb
            series ps2000
            trigger ext 1.0 rising
        """, "not one pscan drives")

    def test_the_scope_label_says_which_instrument(self):
        self.assertIn("PicoScope on USB", self.parse("""
            usb
            series ps3000a
            trigger ext 1.0 rising
        """).scope_label)
        self.assertIn("LeCroy", self.parse("""
            ip 192.168.1.100
        """).scope_label)


class TestLecroyNamesOnUsb(unittest.TestCase):
    """A LeCroy trace name on a USB block is a config error, not a channel."""

    def test_lecroy_names_are_refused_on_usb(self):
        import os
        import tempfile
        import textwrap

        from flyscan.config import FlyConfigError, parse_fly_config

        body = FLY_CONFIG.format(
            scope="usb\ntrigger ext 1.0 rising"
        ).replace("channels A, B, C", "channels C1, C2, C3")

        handle = tempfile.NamedTemporaryFile(
            'w', suffix='.con', delete=False, newline='\n'
        )
        try:
            handle.write(textwrap.dedent(body))
            handle.close()
            with self.assertRaises(FlyConfigError) as caught:
                parse_fly_config(handle.name, counts_per_mm=20000.0)
            self.assertIn("single letters", str(caught.exception))
        finally:
            os.unlink(handle.name)


if __name__ == '__main__':
    unittest.main()
