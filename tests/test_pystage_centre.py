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
                if msg_id == 0x0450 and (dest & 0x7F) in (0x21, 0x22):
                    _chan, counts = struct.unpack_from('<Hl', payload, 0)
                    self.targets[(dest & 0x7F) - 0x20] = counts
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
                self.position[axis] = self.targets.pop(axis)
            self.out += _long(0x0464,
                              struct.pack('<HllL', 0x01, self.position[axis],
                                          self.position[axis], 0x80000400),
                              dest)


class NoPorts:
    @staticmethod
    def comports():
        return []


def drive(fake, keys):
    """Run main() through these keystrokes, returning everything printed."""
    it = iter(list(keys) + ['q'])
    printed = []
    with mock.patch.object(cs.serial, 'Serial', return_value=fake), \
            mock.patch.object(cs.serial.tools, 'list_ports', NoPorts), \
            mock.patch.object(cs, 'get_key', lambda: next(it)), \
            mock.patch.object(cs.os, 'system', lambda *a, **k: None), \
            mock.patch('builtins.print',
                       side_effect=lambda *a, **k: printed.append(
                           ' '.join(str(x) for x in a))):
        cs.main()
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

    def test_wrong_offsets_detected_via_cnts_per_unit(self):
        """A controller whose 0x04F2 layout differs must not move the stage."""
        fake = FakeController(cnts_per_unit=2048,
                              position={1: int(SCALE * 12), 2: int(SCALE * 7)})
        out = drive(fake, ['c'])
        self.assertAlmostEqual(fake.mm(1), 12.0, places=4)
        self.assertAlmostEqual(fake.mm(2), 7.0, places=4)
        self.assertIn('field offsets are wrong', out)

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
