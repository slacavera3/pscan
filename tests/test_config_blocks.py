"""Config parsing for the new a2d keys and the d2a block.

The property that matters most here is the last class: an existing .con file
written against the Phidget piezo has to compile to exactly the same scan it
always did, through the same board, with the same total settle time. Anything
else means every config in the lab needs revisiting.
"""

import os
import tempfile
import textwrap
import unittest

from pscan.config_parser import (
    DEFAULT_D2A_AXIS,
    DEFAULT_D2A_SETTLE_S,
    PHIDGET_SETTLE_S,
    PHIDGET_UM_PER_VOLT,
    categorize_pipeline,
    compile_pipeline_list,
    compile_stage_parameters,
    parse_flat_con_file,
    validate_blocks,
)


def compile_con(text):
    """Parse, validate and compile a .con body. Returns (axes, actions, warns)."""
    handle = tempfile.NamedTemporaryFile(
        'w', suffix='.con', delete=False, newline='\n'
    )
    try:
        handle.write(textwrap.dedent(text))
        handle.close()
        blocks = parse_flat_con_file(handle.name)
        warnings = validate_blocks(blocks)
        stages, _count, _setup, acq, _tear = categorize_pipeline(blocks)
        return (compile_stage_parameters(stages),
                compile_pipeline_list(acq, [1]),
                warnings)
    finally:
        os.unlink(handle.name)


A2D_BLOCK = """
    action a2d
    channel 0
    end
"""


class TestD2ABlock(unittest.TestCase):

    def test_defaults(self):
        axes, _, _ = compile_con("""
            action d2a
            position 1.0
            limits -5 5
            end
        """)
        params = axes[DEFAULT_D2A_AXIS]
        self.assertEqual(params['type'], 'd2a')
        self.assertEqual(params['positions'], [1.0])
        self.assertEqual(params['channel'], 0)
        self.assertEqual(params['units'], 'volts')
        self.assertEqual(params['settle'], DEFAULT_D2A_SETTLE_S)
        self.assertFalse(params['restore'])
        # No backend line means auto-detect, which pscan.daq reads as None.
        self.assertIsNone(params['backend'])

    def test_explicit_axis_channel_and_backend(self):
        axes, _, _ = compile_con("""
            action d2a
            axis 98
            backend sim
            device sim0
            channel 1
            scan -2 2 1
            limits -5 5
            settle 0.002
            restore
            end
        """)
        params = axes[98]
        self.assertEqual(params['positions'], [-2.0, -1.0, 0.0, 1.0, 2.0])
        self.assertEqual(params['backend'], 'sim')
        self.assertEqual(params['device'], 'sim0')
        self.assertEqual(params['channel'], 1)
        self.assertEqual(params['settle'], 0.002)
        self.assertTrue(params['restore'])

    def test_backend_auto_normalises_to_none(self):
        axes, _, _ = compile_con("""
            action d2a
            backend auto
            position 0
            limits -1 1
            end
        """)
        self.assertIsNone(axes[DEFAULT_D2A_AXIS]['backend'])

    def test_micron_units_carry_their_scaling(self):
        axes, _, _ = compile_con("""
            action d2a
            units um
            um_per_volt 12.5
            scan 0 50 25
            limits 0 100
            end
        """)
        params = axes[DEFAULT_D2A_AXIS]
        self.assertEqual(params['units'], 'um')
        self.assertEqual(params['um_per_volt'], 12.5)
        self.assertEqual(params['unit_label'], 'um')

    def test_two_output_axes_coexist(self):
        axes, _, _ = compile_con("""
            action d2a
            axis 98
            scan -1 1 1
            limits -5 5
            end

            action d2a
            axis 99
            scan -1 1 1
            limits -5 5
            end
        """ + A2D_BLOCK)
        self.assertEqual(sorted(axes), [98, 99])

    def test_missing_limits_only_warns(self):
        _, _, warnings = compile_con("""
            action d2a
            position 1.0
            end
        """)
        self.assertTrue(any("limits" in w for w in warnings))


class TestD2AValidation(unittest.TestCase):

    def assert_rejected(self, text, fragment):
        with self.assertRaises(ValueError) as caught:
            compile_con(text)
        self.assertIn(fragment, str(caught.exception))

    def test_axis_colliding_with_the_stage_is_refused(self):
        # Axis 0 is the X stage. An output landing there would be iterated as
        # a stage move.
        self.assert_rejected("""
            action d2a
            axis 0
            position 1
            end
        """, "out of range")

    def test_axis_above_99_is_refused(self):
        self.assert_rejected("""
            action d2a
            axis 120
            position 1
            end
        """, "out of range")

    def test_two_blocks_on_one_axis_are_refused(self):
        self.assert_rejected("""
            action d2a
            axis 99
            position 1
            end

            action d2a
            axis 99
            position 2
            end
        """, "claimed by both")

    def test_d2a_colliding_with_the_legacy_piezo_is_refused(self):
        # Both default to axis 99.
        self.assert_rejected("""
            action d2a_phidget
            position 50
            end

            action d2a
            position 1
            end
        """, "claimed by both")

    def test_unknown_units_are_refused(self):
        self.assert_rejected("""
            action d2a
            units nanometres
            position 1
            end
        """, "units must be one of")

    def test_unknown_backend_is_refused(self):
        self.assert_rejected("""
            action d2a
            backend magic
            position 1
            end
        """, "is not known")

    def test_negative_settle_is_refused(self):
        self.assert_rejected("""
            action d2a
            settle -1
            position 1
            end
        """, "settle cannot be negative")

    def test_positions_outside_limits_are_refused(self):
        self.assert_rejected("""
            action d2a
            scan -10 10 5
            limits -5 5
            end
        """, "outside limits")

    def test_scan_or_position_is_required(self):
        self.assert_rejected("""
            action d2a
            channel 0
            end
        """, "needs one of")


class TestA2DKeys(unittest.TestCase):

    def test_new_keys_compile(self):
        _, actions, _ = compile_con("""
            action a2d
            channel 3
            backend sim
            device sim0
            terminal diff
            v_min -1.5
            v_max 1.5
            sample_rate 20000
            n_samples 64
            end
        """)
        p = actions[0]['params']
        self.assertEqual(p['channel'], 3)
        self.assertEqual(p['backend'], 'sim')
        self.assertEqual(p['device'], 'sim0')
        self.assertEqual(p['terminal'], 'diff')
        self.assertEqual(p['v_min'], -1.5)
        self.assertEqual(p['v_max'], 1.5)

    def test_omitted_keys_keep_the_original_defaults(self):
        _, actions, _ = compile_con(A2D_BLOCK)
        p = actions[0]['params']
        self.assertEqual(p['range'], 0)
        self.assertEqual(p['sample_rate'], 10000)
        self.assertEqual(p['n_samples'], 100)
        self.assertEqual(p['terminal'], 'rse')
        self.assertIsNone(p['backend'])
        self.assertIsNone(p['v_min'])
        self.assertIsNone(p['v_max'])

    def test_unknown_terminal_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            compile_con("""
                action a2d
                channel 0
                terminal sideways
                end
            """)
        self.assertIn("terminal 'sideways'", str(caught.exception))

    def test_phidget_backend_is_refused_for_input(self):
        # The Phidget analog board has outputs only.
        with self.assertRaises(ValueError) as caught:
            compile_con("""
                action a2d
                channel 0
                backend phidget
                end
            """)
        self.assertIn("outputs only", str(caught.exception))

    def test_inverted_volts_are_refused(self):
        with self.assertRaises(ValueError):
            compile_con("""
                action a2d
                channel 0
                v_min 2
                v_max 1
                end
            """)

    def test_range_plus_volts_warns(self):
        _, _, warnings = compile_con("""
            action a2d
            channel 0
            range 1
            v_min -1
            v_max 1
            end
        """)
        self.assertTrue(any("explicit volts win" in w for w in warnings))


class TestLegacyPiezoUnchanged(unittest.TestCase):
    """An existing .con file must scan exactly what it always scanned."""

    LEGACY = """
        action d2a_phidget
        scan 0 100 25
        limits 0 100
        end
    """

    def setUp(self):
        self.axes, _, self.warnings = compile_con(self.LEGACY + A2D_BLOCK)
        self.params = self.axes[DEFAULT_D2A_AXIS]

    def test_still_lands_on_axis_99(self):
        self.assertEqual(sorted(self.axes), [99])

    def test_positions_are_identical(self):
        self.assertEqual(self.params['positions'],
                         [0.0, 25.0, 50.0, 75.0, 100.0])

    def test_phidget_backend_is_pinned_not_auto_detected(self):
        # Auto-detection never considers the Phidget board, so the legacy
        # block has to name it explicitly or it would resolve to a DAQ card.
        self.assertEqual(self.params['backend'], 'phidget')

    def test_units_and_scaling_match_the_old_driver(self):
        self.assertEqual(self.params['units'], 'um')
        self.assertEqual(self.params['um_per_volt'], PHIDGET_UM_PER_VOLT)
        self.assertEqual(self.params['um_per_volt'], 20.0)

    def test_total_settle_time_is_preserved(self):
        # main() slept 0.20s after the move and the driver slept 0.05s inside
        # set_position_um(). The backend no longer sleeps, so the axis has to
        # carry the whole 0.25s.
        self.assertEqual(self.params['settle'], PHIDGET_SETTLE_S)
        self.assertAlmostEqual(self.params['settle'], 0.25)

    def test_does_not_restore_by_default(self):
        # The old piezo path had no restore at all.
        self.assertFalse(self.params['restore'])

    def test_a_single_position_still_works(self):
        axes, _, _ = compile_con("""
            action d2a_phidget
            position 42.5
            limits 0 100
            end
        """ + A2D_BLOCK)
        self.assertEqual(axes[DEFAULT_D2A_AXIS]['positions'], [42.5])

    def test_legacy_block_rejects_unknown_keys(self):
        # 'axis' belongs to 'action d2a'. Accepting it silently on the legacy
        # block would look like it worked.
        with self.assertRaises(ValueError):
            compile_con("""
                action d2a_phidget
                axis 98
                position 50
                end
            """)


if __name__ == '__main__':
    unittest.main()
