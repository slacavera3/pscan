"""C must reach the geometric centre of the stage, whatever the coordinate frame.

The requirement: homing trips the limit switches and defines zero; C goes to
the geometric centre of travel; and the physical point C reaches must not
depend on where zero happens to have landed. A stage homed to its reverse
limit reports its travel as 0..110 mm and a stage carrying a 55 mm home offset
reports it as -55..55 mm - the same piece of glass, two coordinate frames. C
must drive to the middle of the glass in both.

This drives main() through real keystrokes against a fake controller that
answers whatever is asked, so it exercises the key handler rather than the
helpers underneath it.
"""
import struct
import unittest
from unittest import mock

import pscan.control_stages as cs

SCALE = 34304.0
HOST = 0x01


def _long(msg_id, payload, source):
    return struct.pack('<HHBB', msg_id, len(payload), HOST | 0x80,
                       source) + payload


def _short(msg_id, source, p1=0x01, p2=0x00):
    return struct.pack('<HBBBB', msg_id, p1, p2, HOST, source)


class FakeController:
    """Two bays, 0x21 and 0x22, with configurable travel, zero and homed state."""

    def __init__(self, travel=None, zero_offset=None, homed=None,
                 position=None, cnts_per_unit=int(SCALE),
                 answer_stageaxis=True, answer_status=True):
        self.travel = travel or {1: (0, int(SCALE * 110)),
                                 2: (0, int(SCALE * 75))}
        self.zero_offset = zero_offset or {1: 0, 2: 0}
        self.homed = {1: True, 2: True} if homed is None else dict(homed)
        self.position = position or {1: int(SCALE * 12), 2: int(SCALE * 7)}
        self.cnts_per_unit = cnts_per_unit
        self.answer_stageaxis = answer_stageaxis
        self.answer_status = answer_status

        self.out = bytearray()
        self._in = bytearray()
        self.is_open = True
        self.targets = {}
        self.rel_targets = {}
        # Travel is enforced: a relative move that would run past a limit
        # stops at it, as the hardware does. Without this a calibration step
        # could "succeed" past the end of the stage.
        self.enforce_limits = True

    # --- pyserial surface ---
    def read(self, n=1):
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def write(self, data):
        self._in.extend(data)
        self._drain()
        return len(data)

    def reset_input_buffer(self):
        # Real hardware drops buffered bytes; this fake only holds replies it
        # just generated, so clearing would delete the answer being waited on.
        pass

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.out)

    def close(self):
        self.is_open = False

    def mm(self, axis):
        return self.position[axis] / SCALE

    # --- request handling ---
    def _drain(self):
        while len(self._in) >= 6:
            msg_id = struct.unpack_from('<H', self._in, 0)[0]
            dest = self._in[4]
            if dest & 0x80:
                length = struct.unpack_from('<H', self._in, 2)[0]
                if len(self._in) < 6 + length:
                    return
                payload = bytes(self._in[6:6 + length])
                del self._in[:6 + length]
                if msg_id in (0x0450, 0x0445) and (dest & 0x7F) in (0x21, 0x22):
                    # 0x0450 SET_MOVEABSPARAMS / 0x0445 SET_MOVERELPARAMS.
                    # Both stash a target; which one is applied depends on
                    # the trigger that follows.
                    _chan, counts = struct.unpack_from('<Hl', payload, 0)
                    axis = (dest & 0x7F) - 0x20
                    if msg_id == 0x0450:
                        self.targets[axis] = counts
                    else:
                        self.rel_targets[axis] = counts
            else:
                del self._in[:6]
                self._reply(msg_id, dest)

    def _reply(self, msg_id, dest):
        if dest not in (0x21, 0x22):
            return
        axis = dest - 0x20

        if msg_id == 0x0411:                                  # REQ_POSCOUNTER
            self.out += _long(0x0412,
                              struct.pack('<Hl', 0x01, self.position[axis]),
                              dest)
        elif msg_id == 0x0414:                                # REQ_VELPARAMS
            self.out += _long(0x0415,
                              struct.pack('<Hlll', 0x01, 0, 4000, 230531),
                              dest)
        elif msg_id == 0x0429 and self.answer_status:         # REQ_STATUSBITS
            bits = 0x80000000 | (0x400 if self.homed[axis] else 0)
            self.out += _long(0x042A, struct.pack('<HL', 0x01, bits), dest)
        elif msg_id == 0x0441:                                # REQ_HOMEPARAMS
            self.out += _long(0x0442,
                              struct.pack('<HHHll', 0x01, 2, 1, 10000,
                                          self.zero_offset[axis]), dest)
        elif msg_id == 0x04F1 and self.answer_stageaxis:      # REQ_STAGEAXIS
            lo, hi = self.travel[axis]
            payload = struct.pack('<HHH16sIIiiIII', 0x01, 0x20, axis,
                                  b'MLS203', 123456, self.cnts_per_unit,
                                  lo, hi, 1000, 1000, 230531)
            payload += b'\x00' * (74 - len(payload))
            self.out += _long(0x04F2, payload, dest)
        elif msg_id == 0x0443:                                # MOVE_HOME
            self.position[axis] = self.zero_offset[axis]
            self.homed[axis] = True
            self.out += _short(0x0444, dest)
        elif msg_id == 0x0453:                                # MOVE_ABSOLUTE
            if axis in self.targets:
                self.position[axis] = self._clamp(
                    axis, self.targets.pop(axis))
            self._completed(axis, dest)
        elif msg_id == 0x0448:                                # MOVE_RELATIVE
            if axis in self.rel_targets:
                self.position[axis] = self._clamp(
                    axis, self.position[axis] + self.rel_targets.pop(axis))
            self._completed(axis, dest)

    def _clamp(self, axis, target):
        if not self.enforce_limits:
            return target
        lo, hi = self.travel[axis]
        return max(lo, min(hi, target))

    def _completed(self, axis, dest):
        self.out += _long(0x0464,
                          struct.pack('<HllL', 0x01, self.position[axis],
                                      self.position[axis], 0x80000400),
                          dest)


class NoPorts:
    @staticmethod
    def comports():
        return []


def drive(fake, keys, calibration=None):
    """Run main() through these keystrokes, returning everything printed.

    main([]) rather than main(): with no argv it would parse pytest's own
    command line and exit.

    The calibration store is stubbed empty by default so these tests read the
    controller rather than whatever happens to be saved on the machine
    running them.
    """
    it = iter(list(keys) + ['q'])
    printed = []
    with mock.patch.object(cs.serial, 'Serial', return_value=fake), \
            mock.patch.object(cs.serial.tools, 'list_ports', NoPorts), \
            mock.patch.object(cs, 'get_key', lambda: next(it)), \
            mock.patch.object(cs.os, 'system', lambda *a, **k: None), \
            mock.patch('pscan.calibrate.load_calibration',
                       return_value=dict(calibration or {})), \
            mock.patch('builtins.print',
                       side_effect=lambda *a, **k: printed.append(
                           ' '.join(str(x) for x in a))):
        cs.main([])
    return '\n'.join(printed)


class TestCentreIsTheSamePhysicalPoint(unittest.TestCase):

    def test_zero_at_the_limit(self):
        fake = FakeController()
        drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 55.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 37.5, places=4)

    def test_zero_offset_to_mid_travel_reaches_the_same_place(self):
        """Different frame, same glass: the centre is now 0,0 not 55,37.5."""
        fake = FakeController(
            travel={1: (int(-SCALE * 55), int(SCALE * 55)),
                    2: (int(-SCALE * 37.5), int(SCALE * 37.5))},
            zero_offset={1: int(SCALE * 55), 2: int(SCALE * 37.5)},
            position={1: int(SCALE * 20), 2: int(SCALE * -10)})
        drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 0.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 0.0, places=4)

    def test_arbitrarily_translated_frame(self):
        shift = 17.3
        fake = FakeController(
            travel={1: (int(SCALE * shift), int(SCALE * (shift + 110))),
                    2: (int(SCALE * shift), int(SCALE * (shift + 75)))},
            position={1: int(SCALE * 20), 2: int(SCALE * 20)})
        drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), shift + 55.0, places=4)
        self.assertAlmostEqual(fake.mm(2), shift + 37.5, places=4)

    def test_c_is_idempotent(self):
        """'every time': pressing C twice must not drift."""
        fake = FakeController()
        drive(fake, ['c', 'c', 'c'])
        self.assertAlmostEqual(fake.mm(1), 55.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 37.5, places=4)

    def test_c_after_h_lands_on_the_centre(self):
        fake = FakeController(homed={1: False, 2: False})
        drive(fake, ['h', 'c'])
        self.assertAlmostEqual(fake.mm(1), 55.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 37.5, places=4)

    def test_c_never_goes_to_zero_when_zero_is_a_corner(self):
        """The old behaviour, guarded against: (0,0) is a corner here."""
        fake = FakeController()
        drive(fake, ['c'])
        self.assertNotAlmostEqual(fake.mm(1), 0.0, places=2)
        self.assertNotAlmostEqual(fake.mm(2), 0.0, places=2)


class TestCentreRefusesRatherThanGuessing(unittest.TestCase):

    def test_unhomed_axis_is_not_moved(self):
        fake = FakeController(homed={1: False, 2: False},
                              position={1: int(SCALE * 12), 2: int(SCALE * 7)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 12.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('not homed', out)

    def test_no_travel_report_is_not_moved(self):
        fake = FakeController(answer_stageaxis=False,
                              position={1: int(SCALE * 12), 2: int(SCALE * 7)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 12.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('did not report stage axis parameters', out)

    def test_garbage_layout_detected_and_stage_not_moved(self):
        """A controller whose 0x04F2 layout differs must not move the stage."""
        fake = FakeController(cnts_per_unit=1634692198,
                              position={1: int(SCALE * 12), 2: int(SCALE * 7)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 12.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('not a plausible', out)

    def test_limits_in_a_different_frame_are_not_trusted(self):
        """Limits that do not bracket the stage are not this stage's limits.

        The failure this prevents: an axis homed to its forward limit, whose
        counter therefore runs negative, against limits reported as a
        positive 0..travel span. The midpoint of those would be a command
        straight into the far limit.
        """
        fake = FakeController(
            travel={1: (0, int(SCALE * 110)), 2: (0, int(SCALE * 75))},
            position={1: int(-SCALE * 40), 2: int(-SCALE * 30)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), -40.0, places=4)
        self.assertAlmostEqual(fake.mm(2), -30.0, places=4)
        self.assertIn('not in the same frame', out)

    def test_unknown_homed_state_is_not_moved(self):
        fake = FakeController(answer_status=False,
                              position={1: int(SCALE * 12), 2: int(SCALE * 7)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 12.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('did not say', out)

    def test_disabled_axis_is_left_alone_and_named(self):
        fake = FakeController()
        out = drive(fake, ['2', 'c'])   # '2' toggles Y off
        self.assertAlmostEqual(fake.mm(1), 55.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('disabled, left alone', out)


class TestRealBBD303(unittest.TestCase):
    """The rig as it actually reports itself.

    A BBD303 driving an MLS203 reports cnts_per_unit = 20000 (50 nm), not
    pscan's hardcoded 34304, and both axes home to the FORWARD limit - so the
    position counter runs negative and the travel span is -110..0 mm.

    C must still work. The centre is a midpoint in counts and a midpoint needs
    no scale, so a wrong counts-per-mm makes the millimetres on screen wrong
    without making the move wrong.
    """

    CNTS = 20000

    def _rig(self, x_mm=-42.0, y_mm=-25.0):
        return FakeController(
            travel={1: (-110 * self.CNTS, 0), 2: (-75 * self.CNTS, 0)},
            zero_offset={1: 0, 2: 0},
            position={1: int(x_mm * self.CNTS), 2: int(y_mm * self.CNTS)},
            cnts_per_unit=self.CNTS)

    def test_c_reaches_the_true_centre_in_counts(self):
        fake = self._rig()
        drive(fake, ['c'])
        self.assertEqual(fake.position[1], -55 * self.CNTS)
        self.assertEqual(fake.position[2], int(-37.5 * self.CNTS))

    def test_c_is_not_derailed_by_the_wrong_mm_scale(self):
        """Going via mm with 34304 would land ~1.7x off. Counts do not."""
        fake = self._rig()
        drive(fake, ['c'])
        wrong = int(-55.0 * SCALE)          # what an mm round-trip would give
        self.assertNotEqual(fake.position[1], wrong)
        self.assertEqual(fake.position[1], -55 * self.CNTS)

    def test_c_is_idempotent_on_the_real_rig(self):
        fake = self._rig()
        drive(fake, ['c', 'c'])
        self.assertEqual(fake.position[1], -55 * self.CNTS)

    def test_scale_now_comes_from_the_controller_not_34304(self):
        """The fix for the original bug: 20000 is adopted, and said out loud."""
        out = drive(self._rig(), [])
        self.assertIn('20000.0 counts/mm (declared by the controller)', out)
        self.assertNotIn('34304.0 counts/mm', out)

    def test_no_disagreement_warning_once_the_scale_is_right(self):
        out = drive(self._rig(), [])
        self.assertNotIn('(!)', out)

    def test_positions_are_now_in_real_millimetres(self):
        """-42.06623 mm was the 34304 reading of -840000 counts."""
        fake = self._rig(x_mm=-42.0)
        out = drive(fake, [])
        self.assertIn('-42.00000', out)

    def test_a_stored_value_that_disagrees_is_flagged(self):
        """A measurement contradicting the controller is worth seeing."""
        stored = {f'123456:MLS203:1': {'counts_per_mm': 18000.0,
                                       'source': 'manual'},
                  f'123456:MLS203:2': {'counts_per_mm': 18000.0,
                                       'source': 'manual'}}
        out = drive(self._rig(), [], calibration=stored)
        self.assertIn('(!)', out)
        self.assertIn('18000', out)
        self.assertIn('measured on this rig', out)

    def test_panel_shows_the_negative_span(self):
        out = drive(self._rig(), [])
        self.assertIn('-110.00000 to 0.00000 mm, centre -55.00000 mm', out)

    def test_dump_key_reports_the_raw_fields(self):
        out = drive(self._rig(), ['d', 'x'])
        self.assertIn('cnts_per_unit=20000', out)
        self.assertIn(f'min={-110 * self.CNTS}', out)
        self.assertIn('usable for C: True', out)


class TestPanelShowsTheTarget(unittest.TestCase):

    def test_travel_and_centre_are_displayed(self):
        out = drive(FakeController(), [])
        self.assertIn('0.00000 to 110.00000 mm, centre 55.00000 mm', out)
        self.assertIn('0.00000 to 75.00000 mm, centre 37.50000 mm', out)

    def test_unavailable_travel_is_displayed_as_such(self):
        out = drive(FakeController(answer_stageaxis=False), [])
        self.assertIn('Travel X: unavailable', out)

    def test_menu_no_longer_claims_zero_is_the_centre(self):
        out = drive(FakeController(), [])
        self.assertIn('Centre of Travel', out)
        self.assertNotIn('Center (0.0, 0.0)', out)


if __name__ == '__main__':
    unittest.main()
