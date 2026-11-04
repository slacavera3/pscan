"""Range resolution: the one piece of arithmetic both backends depend on.

comedi takes a range *index* into the card's own list; DAQmx takes volts and
has no concept of an index. Everything that makes one .con file mean the same
thing on both cards goes through resolve_range(), so it is worth testing
properly - a wrong answer here is not a crash, it is a silently wrong voltage
scale on every sample of every trace.
"""

import unittest

from pscan.daq import clamp_volts, resolve_range

# Shaped after a PCIe-6351's AI range list.
RANGES = ((-10.0, 10.0), (-5.0, 5.0), (-1.0, 1.0), (-0.2, 0.2))


class TestRangeByIndex(unittest.TestCase):

    def test_index_selects_that_entry(self):
        self.assertEqual(resolve_range(RANGES, range_index=2), (2, -1.0, 1.0))

    def test_no_request_defaults_to_first(self):
        # What a .con file with no 'range' line has always got.
        self.assertEqual(resolve_range(RANGES), (0, -10.0, 10.0))

    def test_index_past_the_end_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            resolve_range(RANGES, range_index=9)
        # The message has to say what the card does offer, because the whole
        # problem is that the user cannot see the card's range list.
        self.assertIn("4 range(s)", str(caught.exception))
        self.assertIn("0..3", str(caught.exception))

    def test_empty_range_list_is_refused(self):
        with self.assertRaises(ValueError):
            resolve_range((), range_index=0)


class TestRangeByVolts(unittest.TestCase):

    def test_picks_the_narrowest_range_that_fits(self):
        # Not the first that fits, and not the widest. A tighter range is more
        # resolution, which is the point of asking in volts.
        self.assertEqual(
            resolve_range(RANGES, v_min=-0.9, v_max=0.9), (2, -1.0, 1.0)
        )

    def test_exact_fit_is_allowed(self):
        self.assertEqual(
            resolve_range(RANGES, v_min=-0.2, v_max=0.2), (3, -0.2, 0.2)
        )

    def test_one_sided_request_is_mirrored(self):
        # 'v_max 2' means -2..2, so it lands on +/-5 rather than +/-1.
        self.assertEqual(resolve_range(RANGES, v_max=2.0), (1, -5.0, 5.0))
        self.assertEqual(resolve_range(RANGES, v_min=-2.0), (1, -5.0, 5.0))

    def test_volts_override_an_index(self):
        # Both given: the explicit volts win. validate_blocks() warns about
        # this combination rather than refusing it.
        self.assertEqual(
            resolve_range(RANGES, range_index=0, v_min=-0.5, v_max=0.5),
            (2, -1.0, 1.0),
        )

    def test_request_wider_than_the_card_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            resolve_range(RANGES, v_min=-50.0, v_max=50.0)
        self.assertIn("widest", str(caught.exception))

    def test_inverted_request_is_refused(self):
        with self.assertRaises(ValueError):
            resolve_range(RANGES, v_min=1.0, v_max=-1.0)

    def test_unipolar_ranges_resolve_correctly(self):
        # A card whose ranges are not symmetric about zero. The old hardcoded
        # conversion assumed every range was +/-v_max and got these wrong.
        unipolar = ((0.0, 10.0), (0.0, 5.0), (-10.0, 10.0))
        self.assertEqual(resolve_range(unipolar, v_min=0.0, v_max=4.0),
                         (1, 0.0, 5.0))
        # A negative request cannot use either unipolar range.
        self.assertEqual(resolve_range(unipolar, v_min=-1.0, v_max=1.0),
                         (2, -10.0, 10.0))


class TestClampVolts(unittest.TestCase):

    AO = ((-10.0, 10.0), (-5.0, 5.0))

    def test_inside_range_is_untouched(self):
        self.assertEqual(clamp_volts(3.0, self.AO), (3.0, False))

    def test_above_range_is_clamped_and_flagged(self):
        self.assertEqual(clamp_volts(12.0, self.AO), (10.0, True))

    def test_below_range_is_clamped_and_flagged(self):
        self.assertEqual(clamp_volts(-12.0, self.AO), (-10.0, True))

    def test_clamps_to_the_widest_available_range(self):
        # +/-10 is available, so 7V is fine even though +/-5 is also listed.
        self.assertEqual(clamp_volts(7.0, self.AO), (7.0, False))

    def test_phidget_window_is_respected(self):
        # The Phidget declares 0..5V to protect the piezo, not the board's
        # real -10..+10. A negative request must be clamped to 0.
        self.assertEqual(clamp_volts(-1.0, ((0.0, 5.0),)), (0.0, True))

    def test_unknown_range_list_does_not_clamp(self):
        self.assertEqual(clamp_volts(99.0, ()), (99.0, False))


if __name__ == '__main__':
    unittest.main()
