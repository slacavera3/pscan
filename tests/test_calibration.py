"""Establishing counts-per-mm, and refusing to pretend it has been measured.

The bug behind all of this: pscan hardcoded 34304, the Z8xx/TDC001 figure,
and ran it on a BBD303 driving an MLS203 that reports 20000. Nothing ever
said where the number came from, so it survived.
"""
import json
import os
import tempfile
import unittest

from pscan.calibrate import (
    KNOWN_COUNTS_PER_MM, LEGACY_COUNTS_PER_MM, SOURCE_CATALOGUE,
    SOURCE_CONTROLLER, SOURCE_LEGACY, SOURCE_MANUAL, SOURCE_SPAN,
    CalibrationUnknown, catalogue_counts_per_mm, compare, counts_per_mm_for,
    default_travel_mm, describe_source, from_controller, from_manual,
    from_span, load_calibration, make_record, measure_manual, measure_span,
    require_counts_per_mm, resolve_counts_per_mm, rig_key, run_calibration,
    save_calibration, wait_until_still,
)

MLS203 = {'part_no': 'MLS203', 'serial_num': 94000123,
          'cnts_per_unit': 20000, 'min_counts': -2200000, 'max_counts': 0}


class TestRigKey(unittest.TestCase):

    def test_identifies_stage_controller_and_axis(self):
        self.assertEqual(rig_key(MLS203, 1), '94000123:MLS203:1')
        self.assertEqual(rig_key(MLS203, 2), '94000123:MLS203:2')

    def test_axes_do_not_collide(self):
        self.assertNotEqual(rig_key(MLS203, 1), rig_key(MLS203, 2))

    def test_different_controllers_do_not_collide(self):
        other = dict(MLS203, serial_num=94000999)
        self.assertNotEqual(rig_key(MLS203, 1), rig_key(other, 1))

    def test_unknown_params_degrade_rather_than_crash(self):
        self.assertEqual(rig_key(None, 1), 'unknown:unknown:1')


class TestSources(unittest.TestCase):

    def test_controller_figure(self):
        rec = from_controller(MLS203)
        self.assertEqual(rec['counts_per_mm'], 20000)
        self.assertEqual(rec['source'], SOURCE_CONTROLLER)

    def test_controller_rejects_implausible(self):
        self.assertIsNone(from_controller(dict(MLS203,
                                               cnts_per_unit=1634692198)))
        self.assertIsNone(from_controller(dict(MLS203, cnts_per_unit=0)))
        self.assertIsNone(from_controller(None))

    def test_span_divides_counts_by_datasheet_travel(self):
        rec = from_span(2200000, 110.0)
        self.assertAlmostEqual(rec['counts_per_mm'], 20000.0)
        self.assertEqual(rec['source'], SOURCE_SPAN)

    def test_span_is_sign_agnostic(self):
        """An axis homed to its forward limit travels negative."""
        self.assertAlmostEqual(from_span(-2200000, 110.0)['counts_per_mm'],
                               20000.0)

    def test_span_needs_a_travel(self):
        self.assertIsNone(from_span(2200000, None))
        self.assertIsNone(from_span(2200000, 0))

    def test_manual_divides_counts_by_measured_distance(self):
        rec = from_manual(200000, 10.0)
        self.assertAlmostEqual(rec['counts_per_mm'], 20000.0)
        self.assertEqual(rec['source'], SOURCE_MANUAL)

    def test_manual_catches_the_34304_error(self):
        """Commanded 10 mm at 34304; the stage really moved 17.152 mm."""
        rec = from_manual(343040, 17.152)
        self.assertAlmostEqual(rec['counts_per_mm'], 20000.0, places=1)

    def test_manual_needs_a_measurement(self):
        self.assertIsNone(from_manual(200000, None))
        self.assertIsNone(from_manual(200000, 0))
        self.assertIsNone(from_manual(0, 10.0))


class TestDefaultTravel(unittest.TestCase):

    def test_known_part(self):
        self.assertEqual(default_travel_mm(MLS203, 1), 110.0)
        self.assertEqual(default_travel_mm(MLS203, 2), 75.0)

    def test_unknown_part_is_not_guessed(self):
        self.assertIsNone(default_travel_mm(dict(MLS203, part_no='XYZ99'), 1))
        self.assertIsNone(default_travel_mm(None, 1))


class TestCompare(unittest.TestCase):

    def test_agreement(self):
        lines = compare([from_controller(MLS203), from_manual(200000, 10.0)])
        self.assertEqual(len(lines), 1)
        self.assertIn('agree', lines[0])

    def test_disagreement_reports_the_factor(self):
        lines = compare([make_record(34304, SOURCE_CONTROLLER),
                         make_record(20000, SOURCE_MANUAL)])
        self.assertIn('1.7152', lines[0])

    def test_one_source_says_nothing(self):
        self.assertEqual(compare([from_controller(MLS203)]), [])
        self.assertEqual(compare([None, None]), [])


class TestStore(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'cal.json')

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        rec = from_manual(200000, 10.0)
        save_calibration('94000123:MLS203:1', rec, self.path)
        loaded = load_calibration([self.path])
        self.assertAlmostEqual(
            loaded['94000123:MLS203:1']['counts_per_mm'], 20000.0)

    def test_second_write_preserves_the_first(self):
        save_calibration('a:b:1', make_record(20000, SOURCE_MANUAL),
                         self.path)
        save_calibration('a:b:2', make_record(20001, SOURCE_MANUAL),
                         self.path)
        loaded = load_calibration([self.path])
        self.assertIn('a:b:1', loaded)
        self.assertIn('a:b:2', loaded)

    def test_user_store_overrides_system_store(self):
        sys_path = os.path.join(self.dir.name, 'system.json')
        save_calibration('a:b:1', make_record(34304, SOURCE_CONTROLLER),
                         sys_path)
        save_calibration('a:b:1', make_record(20000, SOURCE_MANUAL),
                         self.path)
        loaded = load_calibration([sys_path, self.path])
        self.assertAlmostEqual(loaded['a:b:1']['counts_per_mm'], 20000.0)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(load_calibration([self.path + '.nope']), {})

    def test_corrupt_file_is_empty_not_an_error(self):
        with open(self.path, 'w', encoding='utf-8') as fh:
            fh.write('{not json')
        self.assertEqual(load_calibration([self.path]), {})

    def test_written_file_is_readable_json(self):
        save_calibration('a:b:1', make_record(20000, SOURCE_MANUAL),
                         self.path)
        with open(self.path, 'r', encoding='utf-8') as fh:
            self.assertIn('entries', json.load(fh))


class TestResolution(unittest.TestCase):
    """Stored beats declared beats the legacy constant - and says which."""

    def test_stored_wins(self):
        cal = {'94000123:MLS203:1': make_record(19999, SOURCE_MANUAL)}
        value, source = counts_per_mm_for(MLS203, 1, calibration=cal)
        self.assertEqual(value, 19999)
        self.assertEqual(source, SOURCE_MANUAL)

    def test_controller_used_when_nothing_stored(self):
        value, source = counts_per_mm_for(MLS203, 1, calibration={})
        self.assertEqual(value, 20000)
        self.assertEqual(source, SOURCE_CONTROLLER)

    def test_legacy_only_as_a_last_resort(self):
        value, source = counts_per_mm_for(None, 1, calibration={})
        self.assertEqual(value, LEGACY_COUNTS_PER_MM)
        self.assertEqual(source, SOURCE_LEGACY)

    def test_stored_for_another_axis_is_not_reused(self):
        cal = {'94000123:MLS203:1': make_record(19999, SOURCE_MANUAL)}
        value, source = counts_per_mm_for(MLS203, 2, calibration=cal)
        self.assertEqual(source, SOURCE_CONTROLLER)

    def test_stored_for_another_rig_is_not_reused(self):
        cal = {'94000123:MLS203:1': make_record(19999, SOURCE_MANUAL)}
        other = dict(MLS203, serial_num=94000999)
        value, source = counts_per_mm_for(other, 1, calibration=cal)
        self.assertEqual(source, SOURCE_CONTROLLER)

    def test_uncalibrated_says_so_out_loud(self):
        line = describe_source(LEGACY_COUNTS_PER_MM, SOURCE_LEGACY)
        self.assertIn('UNCALIBRATED', line)
        self.assertIn('--calibrate', line)

    def test_measured_says_so(self):
        self.assertIn('measured', describe_source(20000, SOURCE_MANUAL))


class FakeAxis:
    """A stage that moves instantly, for the measurement procedures."""

    def __init__(self, counts=0, cnts_per_mm=20000, limits=None):
        self.counts = counts
        self.cnts_per_mm = cnts_per_mm
        self.limits = limits
        self.commands = []

    def _clamp(self, target):
        if self.limits:
            lo, hi = self.limits
            return max(lo, min(hi, target))
        return target

    def get_position_counts(self, axis_id):
        return self.counts

    def move_relative_counts(self, axis_id, counts):
        self.commands.append(('rel', counts))
        self.counts = self._clamp(self.counts + counts)

    def move_absolute_counts(self, axis_id, counts):
        self.commands.append(('abs', counts))
        self.counts = self._clamp(counts)

    def get_stage_axis_params(self, axis_id):
        return MLS203


class TestMeasurementProcedures(unittest.TestCase):

    def test_wait_until_still_returns_the_resting_count(self):
        axis = FakeAxis(counts=1234)
        self.assertEqual(
            wait_until_still(axis, 1, still_for_s=0.0, poll_s=0.0,
                             sleep=lambda _s: None),
            1234)

    def test_manual_reports_counts_actually_moved(self):
        axis = FakeAxis(counts=0)
        delta, start, end = measure_manual(axis, 1, 200000,
                                           sleep=lambda _s: None)
        self.assertEqual((delta, start, end), (200000, 0, 200000))

    def test_manual_commands_counts_not_mm(self):
        """The calibration must not be set up through the thing it calibrates."""
        axis = FakeAxis(counts=0)
        measure_manual(axis, 1, 200000, sleep=lambda _s: None)
        self.assertEqual(axis.commands, [('rel', 200000)])

    def test_manual_shortfall_is_visible_when_a_limit_intervenes(self):
        axis = FakeAxis(counts=0, limits=(0, 50000))
        delta, _start, _end = measure_manual(axis, 1, 200000,
                                             sleep=lambda _s: None)
        self.assertEqual(delta, 50000)
        self.assertNotEqual(delta, 200000)

    def test_span_travels_between_the_reported_limits(self):
        axis = FakeAxis(counts=-1000000, limits=(-2200000, 0))
        span, low, high = measure_span(axis, 1, MLS203,
                                       sleep=lambda _s: None)
        self.assertEqual((low, high), (-2200000, 0))
        self.assertEqual(span, 2200000)
        self.assertEqual(axis.commands,
                         [('abs', -2200000), ('abs', 0)])

    def test_span_corroborates_the_controller(self):
        axis = FakeAxis(counts=0, limits=(-2200000, 0))
        span, _lo, _hi = measure_span(axis, 1, MLS203, sleep=lambda _s: None)
        rec = from_span(span, 110.0)
        self.assertAlmostEqual(rec['counts_per_mm'], 20000.0)
        self.assertEqual(compare([from_controller(MLS203), rec]),
                         ['controller and span agree '
                          '(20000.0 vs 20000.0 counts/mm)'])

    def test_span_needs_usable_limits(self):
        self.assertIsNone(measure_span(FakeAxis(), 1, None))
        self.assertIsNone(measure_span(FakeAxis(), 1,
                                       dict(MLS203, min_counts=0,
                                            max_counts=0)))


class TestCatalogue(unittest.TestCase):
    """counts-per-mm is a property of the stage model, so it belongs in a table."""

    def test_mls203(self):
        self.assertEqual(catalogue_counts_per_mm(MLS203), 20000.0)

    def test_case_and_whitespace_tolerated(self):
        self.assertEqual(
            catalogue_counts_per_mm(dict(MLS203, part_no=' mls203 ')), 20000.0)

    def test_unlisted_part_is_not_guessed(self):
        self.assertIsNone(
            catalogue_counts_per_mm(dict(MLS203, part_no='DDS600')))
        self.assertIsNone(catalogue_counts_per_mm(None))

    def test_catalogue_is_not_34304(self):
        """The whole point. 34304 is a different controller family."""
        for part, value in KNOWN_COUNTS_PER_MM.items():
            self.assertNotEqual(value, 34304.0, part)


class TestResolve(unittest.TestCase):

    def test_controller_preferred_and_corroborated(self):
        value, source, notes = resolve_counts_per_mm(MLS203, 1,
                                                     calibration={})
        self.assertEqual(value, 20000.0)
        self.assertEqual(source, SOURCE_CONTROLLER)
        self.assertTrue(any('agree' in n for n in notes))

    def test_catalogue_used_when_controller_is_silent(self):
        quiet = dict(MLS203, cnts_per_unit=0)
        value, source, notes = resolve_counts_per_mm(quiet, 1, calibration={})
        self.assertEqual(value, 20000.0)
        self.assertEqual(source, SOURCE_CATALOGUE)

    def test_disagreement_is_reported_loudly(self):
        """A controller configured for the wrong stage must not pass quietly."""
        odd = dict(MLS203, cnts_per_unit=34304)
        value, source, notes = resolve_counts_per_mm(odd, 1, calibration={})
        self.assertEqual(value, 34304)
        self.assertTrue(any('DISAGREEMENT' in n for n in notes))

    def test_stored_measurement_overrides_and_says_so(self):
        cal = {'94000123:MLS203:1': make_record(19500, SOURCE_MANUAL)}
        value, source, notes = resolve_counts_per_mm(MLS203, 1,
                                                     calibration=cal)
        self.assertEqual(value, 19500)
        self.assertEqual(source, SOURCE_MANUAL)
        self.assertTrue(any('overrides' in n for n in notes))

    def test_nothing_known_resolves_to_nothing(self):
        value, source, _notes = resolve_counts_per_mm(None, 1, calibration={})
        self.assertIsNone(value)
        self.assertIsNone(source)

    def test_unlisted_silent_controller_resolves_to_nothing(self):
        unknown = {'part_no': 'DDS600', 'serial_num': 1, 'cnts_per_unit': 0}
        value, _source, _notes = resolve_counts_per_mm(unknown, 1,
                                                       calibration={})
        self.assertIsNone(value)


class TestRequireRefusesRatherThanGuessing(unittest.TestCase):

    def test_raises_when_unknown(self):
        with self.assertRaises(CalibrationUnknown) as ctx:
            require_counts_per_mm(None, 1, calibration={}, what='this scan')
        self.assertIn('silently wrong', str(ctx.exception))
        self.assertIn('--calibrate', str(ctx.exception))

    def test_never_falls_back_to_34304(self):
        """The regression that corrupted every map on this rig."""
        with self.assertRaises(CalibrationUnknown):
            require_counts_per_mm({'part_no': 'DDS600', 'serial_num': 1,
                                   'cnts_per_unit': 0}, 1, calibration={})

    def test_returns_the_figure_when_known(self):
        value, source, _notes = require_counts_per_mm(MLS203, 1,
                                                      calibration={})
        self.assertEqual(value, 20000.0)
        self.assertEqual(source, SOURCE_CONTROLLER)

    def test_an_internally_inconsistent_controller_is_refused(self):
        """part_no and cnts_per_unit come from one record; both cannot be right."""
        odd = dict(MLS203, cnts_per_unit=34304)
        with self.assertRaises(CalibrationUnknown) as ctx:
            require_counts_per_mm(odd, 1, calibration={})
        self.assertIn('ambiguous', str(ctx.exception))
        self.assertIn('disagrees with itself', str(ctx.exception))

    def test_a_stored_measurement_settles_the_ambiguity(self):
        odd = dict(MLS203, cnts_per_unit=34304)
        cal = {'94000123:MLS203:1': make_record(20000, SOURCE_MANUAL)}
        value, source, _notes = require_counts_per_mm(odd, 1, calibration=cal)
        self.assertEqual(value, 20000.0)
        self.assertEqual(source, SOURCE_MANUAL)

    def test_an_unlisted_part_cannot_conflict(self):
        """No catalogue entry means nothing to contradict; the controller stands."""
        unlisted = dict(MLS203, part_no='DDS600', cnts_per_unit=10000)
        value, source, _notes = require_counts_per_mm(unlisted, 1,
                                                      calibration={})
        self.assertEqual(value, 10000)
        self.assertEqual(source, SOURCE_CONTROLLER)


class TestJogToolStillHasALabelledFallback(unittest.TestCase):
    """pystage may jog on a guess; it records nothing. Scans may not."""

    def test_fallback_is_labelled_not_silent(self):
        value, source = counts_per_mm_for(None, 1, calibration={})
        self.assertEqual(value, LEGACY_COUNTS_PER_MM)
        self.assertEqual(source, SOURCE_LEGACY)
        self.assertIn('UNCALIBRATED', describe_source(value, source))

    def test_catalogue_reaches_the_jog_tool_too(self):
        quiet = dict(MLS203, cnts_per_unit=0)
        value, source = counts_per_mm_for(quiet, 1, calibration={})
        self.assertEqual(value, 20000.0)
        self.assertEqual(source, SOURCE_CATALOGUE)


class FakeRig:
    """Two axes, for the interactive driver."""

    def __init__(self, cnts_per_mm=20000, truth=None, params=MLS203):
        # `truth` is what the stage ACTUALLY does per count, letting a test
        # simulate a rig whose real resolution differs from what it claims.
        self.truth = truth or cnts_per_mm
        self.params = params
        self.axes = {1: FakeAxis(0, cnts_per_mm), 2: FakeAxis(0, cnts_per_mm)}

    def get_stage_axis_params(self, axis_id):
        return self.params

    def get_position_counts(self, axis_id):
        return self.axes[axis_id].get_position_counts(axis_id)

    def move_relative_counts(self, axis_id, counts):
        self.axes[axis_id].move_relative_counts(axis_id, counts)

    def move_absolute_counts(self, axis_id, counts):
        self.axes[axis_id].move_absolute_counts(axis_id, counts)


def scripted(answers):
    it = iter(answers)
    return lambda _prompt='': next(it)


class TestRunCalibration(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'cal.json')
        self.said = []

    def say(self, *a):
        self.said.append(' '.join(str(x) for x in a))

    def out(self):
        return '\n'.join(self.said)

    def tearDown(self):
        self.dir.cleanup()

    def test_declining_everything_still_records_the_controller(self):
        chosen = run_calibration(
            FakeRig(), ask=scripted(['n', 'n']), say=self.say,
            save_path=self.path, sleep=lambda _s: None)
        self.assertEqual(chosen[1]['source'], SOURCE_CONTROLLER)
        self.assertEqual(chosen[2]['source'], SOURCE_CONTROLLER)

    def test_manual_measurement_wins_over_the_controller(self):
        chosen = run_calibration(
            FakeRig(), axis_ids=(1,),
            ask=scripted(['y', '10.0']), say=self.say,
            save_path=self.path, sleep=lambda _s: None)
        self.assertEqual(chosen[1]['source'], SOURCE_MANUAL)
        self.assertAlmostEqual(chosen[1]['counts_per_mm'], 20000.0)

    def test_nothing_moves_without_consent(self):
        rig = FakeRig()
        run_calibration(rig, axis_ids=(1,), ask=scripted(['n']),
                        say=self.say, save_path=self.path,
                        sleep=lambda _s: None)
        self.assertEqual(rig.axes[1].commands, [])

    def test_span_is_not_run_unless_allowed(self):
        rig = FakeRig()
        run_calibration(rig, axis_ids=(1,), ask=scripted(['n']),
                        say=self.say, save_path=self.path,
                        sleep=lambda _s: None)
        self.assertNotIn('[span]', self.out())

    def test_span_is_still_confirmed_when_allowed(self):
        rig = FakeRig()
        run_calibration(rig, axis_ids=(1,), allow_span=True,
                        ask=scripted(['n', 'n']), say=self.say,
                        save_path=self.path, sleep=lambda _s: None)
        self.assertEqual(rig.axes[1].commands, [])

    def test_span_runs_when_confirmed(self):
        rig = FakeRig()
        run_calibration(rig, axis_ids=(1,), allow_span=True,
                        ask=scripted(['y', 'n']), say=self.say,
                        save_path=self.path, sleep=lambda _s: None)
        self.assertIn('[span]', self.out())
        self.assertEqual(rig.axes[1].commands,
                         [('abs', -2200000), ('abs', 0)])

    def test_disagreement_is_surfaced_not_hidden(self):
        """Controller claims 20000; the stage really moves 34304 per mm."""
        rig = FakeRig()
        chosen = run_calibration(
            rig, axis_ids=(1,), ask=scripted(['y', '11.6598']),
            say=self.say, save_path=self.path, sleep=lambda _s: None)
        self.assertIn('but', self.out())
        self.assertIn('counts/mm', self.out())
        # The measurement wins, and it is the measured figure that is stored.
        self.assertEqual(chosen[1]['source'], SOURCE_MANUAL)
        self.assertAlmostEqual(chosen[1]['counts_per_mm'], 17153.0, delta=2)

    def test_result_is_persisted_per_axis(self):
        run_calibration(FakeRig(), ask=scripted(['n', 'n']), say=self.say,
                        save_path=self.path, sleep=lambda _s: None)
        stored = load_calibration([self.path])
        self.assertIn('94000123:MLS203:1', stored)
        self.assertIn('94000123:MLS203:2', stored)

    def test_save_can_be_suppressed(self):
        run_calibration(FakeRig(), ask=scripted(['n', 'n']), say=self.say,
                        save_path=self.path, save=False,
                        sleep=lambda _s: None)
        self.assertEqual(load_calibration([self.path]), {})

    def test_a_silent_controller_yields_nothing_rather_than_a_guess(self):
        rig = FakeRig(params=None)
        chosen = run_calibration(rig, axis_ids=(1,), ask=scripted(['n']),
                                 say=self.say, save_path=self.path,
                                 sleep=lambda _s: None)
        self.assertEqual(chosen, {})
        self.assertIn('Nothing determined', self.out())


if __name__ == '__main__':
    unittest.main()
