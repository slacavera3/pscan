"""The parts of the X Series driver that need no card.

The shim itself cannot be exercised without a PCIe-6351 and root, but the
Python side has real logic in it - a PCI address conversion and two range
tables - and getting any of it wrong is the quiet kind of bug. A wrong
resource string points the driver at a different card; a wrong range table
scales every sample incorrectly.

Importing the module does NOT load libpscan_xseries.so (that happens lazily in
_load()), so these run anywhere.
"""

import unittest

from pscan.daq import resolve_range
from pscan.nixseries_driver import (
    AI_RANGES,
    AO_RANGES,
    MAX_RATE_HZ,
    TERMINAL_CODES,
    NIXSeriesDriver,
    _resource_string,
)


class TestResourceString(unittest.TestCase):
    """sysfs names PCI slots in hex; the DDK wants decimal.

    main.cpp builds "PXI<bus>::<device>::INSTR" from decimal numbers, so
    bus 0x82 is 130, not 82. Getting this wrong opens the wrong device - or
    opens nothing and looks like a missing card.
    """

    def test_hex_bus_becomes_decimal(self):
        # The user's card sits at 82:00.0.
        self.assertEqual(
            _resource_string("0000:82:00.0"), "PXI130::0::INSTR"
        )

    def test_bus_below_ten_is_unchanged_by_the_conversion(self):
        self.assertEqual(
            _resource_string("0000:03:00.0"), "PXI3::0::INSTR"
        )

    def test_hex_device_number_also_converts(self):
        self.assertEqual(
            _resource_string("0000:0a:1f.0"), "PXI10::31::INSTR"
        )

    def test_function_number_is_ignored(self):
        # The DDK addresses bus and device only.
        self.assertEqual(
            _resource_string("0000:82:00.1"), "PXI130::0::INSTR"
        )

    def test_nonzero_domain_is_ignored(self):
        self.assertEqual(
            _resource_string("0001:82:00.0"), "PXI130::0::INSTR"
        )

    def test_a_decimal_looking_bus_is_still_read_as_hex(self):
        # 0x20 is 32. Reading "20" as decimal would give PXI20 and open the
        # wrong slot, which is the whole hazard.
        self.assertEqual(
            _resource_string("0000:20:00.0"), "PXI32::0::INSTR"
        )


class TestRangeTables(unittest.TestCase):
    """The tables must match nNISTC3::tInputRange / tOutputRange order.

    The index is what the shim passes straight to the DDK, so an entry in the
    wrong place scales every sample of every trace by the wrong factor without
    anything failing.
    """

    def test_ai_table_is_the_ddk_order(self):
        self.assertEqual(AI_RANGES, (
            (-10.0, 10.0),
            (-5.0, 5.0),
            (-2.0, 2.0),
            (-1.0, 1.0),
            (-0.5, 0.5),
            (-0.2, 0.2),
            (-0.1, 0.1),
        ))

    def test_ai_table_is_monotonically_narrowing(self):
        spans = [hi - lo for lo, hi in AI_RANGES]
        self.assertEqual(spans, sorted(spans, reverse=True))

    def test_ao_table_is_the_ddk_order(self):
        self.assertEqual(AO_RANGES, ((-10.0, 10.0), (-5.0, 5.0)))

    def test_range_zero_is_widest_on_both(self):
        # So a .con file's legacy 'range 0' means full scale here, as it does
        # on comedi.
        self.assertEqual(resolve_range(AI_RANGES, range_index=0),
                         (0, -10.0, 10.0))
        self.assertEqual(resolve_range(AO_RANGES, range_index=0),
                         (0, -10.0, 10.0))

    def test_explicit_volts_pick_the_narrowest_fit(self):
        # +/-100mV exists on X Series but not on the old comedi card, so this
        # is a real gain in resolution for a small photodiode signal.
        self.assertEqual(resolve_range(AI_RANGES, v_min=-0.08, v_max=0.08),
                         (6, -0.1, 0.1))
        self.assertEqual(resolve_range(AI_RANGES, v_min=-0.15, v_max=0.15),
                         (5, -0.2, 0.2))

    def test_a_request_beyond_full_scale_is_refused(self):
        with self.assertRaises(ValueError):
            resolve_range(AI_RANGES, v_min=-20.0, v_max=20.0)


class TestTerminalCodes(unittest.TestCase):

    def test_codes_match_the_shim(self):
        # 0/1/2 as mapped in xseries_shim.cpp's map_terminal().
        self.assertEqual(TERMINAL_CODES,
                         {'rse': 0, 'nrse': 1, 'diff': 2})

    def test_pseudodiff_is_absent(self):
        # nAI::tAI_Config_Channel_Type_t has Differential, NRSE and RSE and no
        # pseudodifferential entry, so the request is refused rather than
        # quietly turned into something else.
        self.assertNotIn('pseudodiff', TERMINAL_CODES)


class TestBackendRegistration(unittest.TestCase):

    def test_backend_name(self):
        self.assertEqual(NIXSeriesDriver.name, 'nixseries')

    def test_it_is_auto_discoverable(self):
        # Unlike the Phidget, enumerating this one only reads sysfs - it does
        # not open or disturb the card - so auto-detection may consider it.
        self.assertTrue(NIXSeriesDriver.discoverable())

    def test_unavailable_reason_names_the_installer(self):
        # Without the shim built, the message has to say what to do.
        if not NIXSeriesDriver.available():
            self.assertIn('install.sh', NIXSeriesDriver.unavailable_reason())

    def test_rate_ceiling_matches_the_convert_clock(self):
        # 100 MHz timebase / 400 ticks per convert.
        self.assertAlmostEqual(MAX_RATE_HZ, 100e6 / 400)

    def test_registered_in_the_daq_registry(self):
        from pscan import daq
        self.assertIn('nixseries', daq.backend_names())
        self.assertIs(daq.backend_class('nixseries'), NIXSeriesDriver)


if __name__ == '__main__':
    unittest.main()
