"""pystage's APT reply handling, and what H and C actually report.

The bug these pin down: SimpleStage used to read a fixed number of bytes once
enough had arrived ("if in_waiting >= 12: read(12)"). The controller emits
MOVE_COMPLETED (20 bytes) and MOVE_HOMED unsolicited, so an unsolicited packet
sitting ahead of the reply was sliced through - the position reply was then
unrecognisable and get_position returned None, or worse, returned a number
unpacked from the middle of a different message.

That is what made a deterministic homing sequence look random: H slept a flat
10 seconds, the seek was sometimes still running, and the position read that
followed landed mid-move and mis-parsed. stage_driver.py had the framed reader
already; control_stages.py did not.

No hardware here: a fake serial port queues the exact bytes a BBD controller
would send.
"""
import struct
import unittest

from pscan.control_stages import SimpleStage, format_position
from pscan.stage_driver import (
    MSG_GET_POSCOUNTER, MSG_GET_VELPARAMS, MSG_GET_HOMEPARAMS,
    MSG_GET_STATUSBITS, MSG_GET_STAGEAXISPARAMS,
    MSG_MOVE_HOMED, MSG_MOVE_COMPLETED,
    STATUS_HOMED, STATUS_HOMING,
    decode_home_params, describe_zero,
    decode_stage_axis_params, validate_travel, travel_centre_mm,
    describe_travel,
)

SCALE = 34304.0  # what SimpleStage uses, and what the rig is calibrated to

HOST = 0x01
BAY_X = 0x21
BAY_Y = 0x22


def long_msg(msg_id, payload, source):
    """A long-form APT reply: 6-byte header with bit 7 of dest set."""
    return struct.pack('<HHBB', msg_id, len(payload),
                       HOST | 0x80, source) + payload


def short_msg(msg_id, source, param1=0x01, param2=0x00):
    """A header-only APT reply."""
    return struct.pack('<HBBBB', msg_id, param1, param2, HOST, source)


def poscounter(counts, source=BAY_X):
    return long_msg(MSG_GET_POSCOUNTER, struct.pack('<Hl', 0x01, counts),
                    source)


def move_completed(source=BAY_X):
    """20 bytes total - the packet the old fixed-size read sliced through.

    chan_ident, position, encoder count, then the status word, which is an
    unsigned DWORD: the homed/enabled bits live in its high half.
    """
    payload = struct.pack('<HllL', 0x01, 123456, 123456, 0x80000400)
    return long_msg(MSG_MOVE_COMPLETED, payload, source)


def velparams(min_v, accel, max_v, source=BAY_X):
    payload = struct.pack('<Hlll', 0x01, min_v, accel, max_v)
    return long_msg(MSG_GET_VELPARAMS, payload, source)


def homeparams(direction, limit, velocity, offset, source=BAY_X):
    payload = struct.pack('<HHHll', 0x01, direction, limit, velocity, offset)
    return long_msg(MSG_GET_HOMEPARAMS, payload, source)


def statusbits(bits, source=BAY_X):
    return long_msg(MSG_GET_STATUSBITS, struct.pack('<HL', 0x01, bits), source)


def stageaxis(min_counts, max_counts, cnts_per_unit=int(SCALE),
              part_no=b'MLS203', source=BAY_X):
    """A 74-byte STAGEAXISPARAMS payload, laid out as thorpy declares it.

    chan H, stage_id H, axis_id H, part_no 16s, serial I, cnts_per_unit I,
    min_pos i, max_pos i, max_accn I, max_dec I, max_vel I, then padding.
    """
    payload = struct.pack(
        '<HHH16sIIiiIII', 0x01, 0x20, 0x01, part_no, 123456, cnts_per_unit,
        min_counts, max_counts, 1000, 1000, 230531)
    payload += b'\x00' * (74 - len(payload))
    return long_msg(MSG_GET_STAGEAXISPARAMS, payload, source)


class FakeSerial:
    """Just enough pyserial to drive SimpleStage's reader."""

    def __init__(self, inbound=b''):
        self.inbound = bytearray(inbound)
        self.written = bytearray()
        self.is_open = True

    def read(self, n=1):
        chunk = bytes(self.inbound[:n])
        del self.inbound[:n]
        return chunk

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def reset_input_buffer(self):
        # A no-op on purpose. On hardware this drops whatever the kernel has
        # already buffered; here the reply is queued before the request is
        # sent, and clearing it would delete the thing under test. What these
        # tests care about is framing, not buffer lifetime.
        pass

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.inbound)

    def close(self):
        self.is_open = False


def stage_with(inbound):
    stage = SimpleStage()
    stage.ser = FakeSerial(inbound)
    return stage


class TestPositionFraming(unittest.TestCase):

    def test_clean_position_reply(self):
        stage = stage_with(poscounter(34304))
        self.assertAlmostEqual(stage.get_position(1), 1.0, places=6)

    def test_position_survives_a_leading_move_completed(self):
        """The regression: 20 unsolicited bytes ahead of the reply.

        A fixed 12-byte read would consume the MOVE_COMPLETED header plus the
        first 6 bytes of its payload, leaving the stream desynchronised.
        """
        stage = stage_with(move_completed() + poscounter(68608))
        self.assertAlmostEqual(stage.get_position(1), 2.0, places=6)

    def test_position_survives_several_unsolicited_packets(self):
        inbound = (move_completed()
                   + short_msg(MSG_MOVE_HOMED, BAY_X)
                   + move_completed(BAY_Y)
                   + poscounter(-34304))
        stage = stage_with(inbound)
        self.assertAlmostEqual(stage.get_position(1), -1.0, places=6)

    def test_reply_from_the_wrong_bay_is_not_accepted(self):
        """A Y reply must not satisfy a question asked of X."""
        stage = stage_with(poscounter(34304, source=BAY_Y))
        self.assertIsNone(stage.get_position(1))

    def test_no_reply_returns_none(self):
        stage = stage_with(b'')
        self.assertIsNone(stage._await_reply(MSG_GET_POSCOUNTER, BAY_X,
                                             timeout_s=0.02))

    def test_truncated_payload_returns_none(self):
        """A packet that promises 6 bytes and delivers 2 is not half-parsed."""
        truncated = struct.pack('<HHBB', MSG_GET_POSCOUNTER, 6,
                                HOST | 0x80, BAY_X) + b'\x01\x00'
        stage = stage_with(truncated)
        self.assertIsNone(stage._await_reply(MSG_GET_POSCOUNTER, BAY_X,
                                             timeout_s=0.02))


class TestVelocityParams(unittest.TestCase):

    def test_velparams_behind_an_unsolicited_packet(self):
        stage = stage_with(move_completed() + velparams(0, 4000, 230531))
        params = stage.get_velocity_params(1)
        self.assertEqual(params, {'min': 0, 'accel': 4000, 'max': 230531})


class TestHoming(unittest.TestCase):

    def test_both_axes_confirm(self):
        inbound = (short_msg(MSG_MOVE_HOMED, BAY_X)
                   + short_msg(MSG_MOVE_HOMED, BAY_Y))
        stage = stage_with(inbound)
        confirmed, missing = stage.home_axes((1, 2), timeout_s=0.5)
        self.assertEqual(confirmed, {1, 2})
        self.assertEqual(missing, set())

    def test_one_axis_never_confirms_and_is_named(self):
        """The point of waiting at all: a stalled axis is reported, not averaged."""
        stage = stage_with(short_msg(MSG_MOVE_HOMED, BAY_X))
        confirmed, missing = stage.home_axes((1, 2), timeout_s=0.05)
        self.assertEqual(confirmed, {1})
        self.assertEqual(missing, {2})

    def test_homed_ignores_unrelated_traffic(self):
        inbound = (move_completed()
                   + poscounter(999)
                   + short_msg(MSG_MOVE_HOMED, BAY_Y)
                   + short_msg(MSG_MOVE_HOMED, BAY_X))
        stage = stage_with(inbound)
        confirmed, missing = stage.home_axes((1, 2), timeout_s=0.5)
        self.assertEqual(confirmed, {1, 2})
        self.assertEqual(missing, set())

    def test_home_is_sent_to_both_bays(self):
        stage = stage_with(b'')
        stage.home_axes((1, 2), timeout_s=0.02)
        # MOVE_HOME is 0x0443, short form, dest in byte 4.
        sent = bytes(stage.ser.written)
        self.assertIn(struct.pack('<HBBBB', 0x0443, 0x01, 0x00, BAY_X, 0x01),
                      sent)
        self.assertIn(struct.pack('<HBBBB', 0x0443, 0x01, 0x00, BAY_Y, 0x01),
                      sent)


class TestHomeParams(unittest.TestCase):

    def test_zero_on_the_limit_switch_is_called_a_corner(self):
        stage = stage_with(homeparams(direction=2, limit=1,
                                      velocity=10000, offset=0))
        params = stage.get_home_params(1)
        self.assertEqual(params['direction'], 'reverse')
        self.assertEqual(params['limit_switch'], 'reverse')
        self.assertEqual(params['offset_counts'], 0)
        self.assertIn('corner of travel', describe_zero(params))

    def test_offset_zero_is_reported_as_a_distance(self):
        stage = stage_with(homeparams(direction=1, limit=4,
                                      velocity=10000, offset=34304 * 55))
        params = stage.get_home_params(1)
        self.assertEqual(params['direction'], 'forward')
        self.assertEqual(params['limit_switch'], 'forward')
        self.assertAlmostEqual(params['offset_mm'], 55.0, places=4)
        desc = describe_zero(params)
        self.assertIn('55.00000 mm', desc)
        self.assertIn('forward limit', desc)
        self.assertNotIn('corner', desc)

    def test_unknown_codes_are_surfaced_not_guessed(self):
        params = decode_home_params(
            struct.pack('<HHHll', 0x01, 0x09, 0x07, 1, 0), counts_per_mm=1000)
        self.assertIn('unknown', params['direction'])
        self.assertIn('unknown', params['limit_switch'])

    def test_no_counts_per_mm_leaves_mm_unconverted(self):
        """Converting with a scale you do not trust is worse than not converting."""
        params = decode_home_params(
            struct.pack('<HHHll', 0x01, 0x02, 0x01, 1, 5000),
            counts_per_mm=None)
        self.assertIsNone(params['offset_mm'])
        self.assertEqual(params['offset_counts'], 5000)
        self.assertIn('5000 counts', describe_zero(params))

    def test_no_reply_describes_itself_as_unknown(self):
        self.assertIsNone(decode_home_params(None))
        self.assertIn('did not answer', describe_zero(None))

    def test_short_payload_is_rejected(self):
        self.assertIsNone(decode_home_params(b'\x01\x00\x02\x00'))


class TestStatusBits(unittest.TestCase):

    def test_homed(self):
        stage = stage_with(statusbits(STATUS_HOMED | 0x80000000))
        self.assertIs(stage.is_homed(1), True)

    def test_not_homed(self):
        stage = stage_with(statusbits(0x80000000))
        self.assertIs(stage.is_homed(1), False)

    def test_mid_home_is_not_yet_homed(self):
        """A stale HOMED bit while HOMING is set must not read as homed."""
        stage = stage_with(statusbits(STATUS_HOMING | STATUS_HOMED))
        self.assertIs(stage.is_homed(1), False)

    def test_no_reply_is_unknown_not_false(self):
        """None is distinct from False: C refuses on either, but says why."""
        stage = stage_with(b'')
        self.assertIsNone(stage.get_status_bits(1))


class TestTravelDecoding(unittest.TestCase):

    def test_round_trip(self):
        stage = stage_with(stageaxis(0, int(SCALE * 110)))
        params = stage.get_stage_axis_params(1)
        self.assertEqual(params['cnts_per_unit'], int(SCALE))
        self.assertEqual(params['min_counts'], 0)
        self.assertEqual(params['max_counts'], int(SCALE * 110))
        self.assertEqual(params['part_no'], 'MLS203')

    def test_negative_min_pos_survives(self):
        """Read signed. Unsigned would make this ~4.29e9 and fail validation."""
        params = decode_stage_axis_params(
            stageaxis(int(-SCALE * 50), int(SCALE * 50))[6:])
        self.assertEqual(params['min_counts'], int(-SCALE * 50))
        ok, _ = validate_travel(params, SCALE)
        self.assertTrue(ok)

    def test_short_payload_rejected(self):
        self.assertIsNone(decode_stage_axis_params(b'\x01\x00' * 4))
        self.assertIsNone(decode_stage_axis_params(None))


class TestTravelValidation(unittest.TestCase):
    """cnts_per_unit is the load-bearing check: it proves the byte offsets."""

    def test_wrong_cnts_per_unit_is_refused(self):
        params = decode_stage_axis_params(
            stageaxis(0, int(SCALE * 110), cnts_per_unit=2048)[6:])
        ok, reason = validate_travel(params, SCALE)
        self.assertFalse(ok)
        self.assertIn('field offsets are wrong', reason)

    def test_good_read_is_accepted(self):
        params = decode_stage_axis_params(stageaxis(0, int(SCALE * 110))[6:])
        ok, reason = validate_travel(params, SCALE)
        self.assertTrue(ok)
        self.assertIn('110', reason)

    def test_inverted_limits_refused(self):
        params = decode_stage_axis_params(
            stageaxis(int(SCALE * 110), 0)[6:])
        ok, reason = validate_travel(params, SCALE)
        self.assertFalse(ok)
        self.assertIn('not above', reason)

    def test_absurd_span_refused(self):
        params = decode_stage_axis_params(
            stageaxis(0, int(SCALE * 9000))[6:])
        ok, reason = validate_travel(params, SCALE)
        self.assertFalse(ok)
        self.assertIn('plausible', reason)

    def test_missing_params_refused(self):
        ok, reason = validate_travel(None, SCALE)
        self.assertFalse(ok)
        self.assertIn('did not report', reason)

    def test_describe_travel_says_why_when_unavailable(self):
        self.assertIn('unavailable', describe_travel(None, SCALE))


class TestCentreIsTranslationInvariant(unittest.TestCase):
    """The requirement: the same physical point whatever the coordinate frame.

    A stage homed to its reverse limit reports 0..110. The same stage with a
    55 mm home offset reports -55..55. Both must centre at the middle of the
    glass, which in each frame is the midpoint of the reported span - never
    half the travel from zero, and never 0,0.
    """

    def test_zero_at_the_limit(self):
        params = decode_stage_axis_params(stageaxis(0, int(SCALE * 110))[6:])
        self.assertAlmostEqual(travel_centre_mm(params, SCALE), 55.0, places=4)

    def test_zero_offset_to_mid_travel(self):
        params = decode_stage_axis_params(
            stageaxis(int(-SCALE * 55), int(SCALE * 55))[6:])
        self.assertAlmostEqual(travel_centre_mm(params, SCALE), 0.0, places=4)

    def test_arbitrary_translation(self):
        """Shift the frame by a silly amount; the centre shifts with it."""
        shift = 17.3
        params = decode_stage_axis_params(
            stageaxis(int(SCALE * shift), int(SCALE * (shift + 110)))[6:])
        self.assertAlmostEqual(travel_centre_mm(params, SCALE),
                               shift + 55.0, places=4)

    def test_centre_is_always_inside_the_span(self):
        for low, high in ((0, 110), (-55, 55), (17.3, 127.3), (-200, -90)):
            params = decode_stage_axis_params(
                stageaxis(int(SCALE * low), int(SCALE * high))[6:])
            centre = travel_centre_mm(params, SCALE)
            self.assertGreaterEqual(centre, low)
            self.assertLessEqual(centre, high)

    def test_not_half_travel_from_zero(self):
        """Guards the specific bug: half-travel-from-zero is frame-dependent."""
        params = decode_stage_axis_params(
            stageaxis(int(-SCALE * 55), int(SCALE * 55))[6:])
        centre = travel_centre_mm(params, SCALE)
        half_travel = 110.0 / 2
        self.assertNotAlmostEqual(centre, half_travel, places=2)
        self.assertAlmostEqual(centre, 0.0, places=4)

    def test_no_scale_gives_no_centre(self):
        params = decode_stage_axis_params(stageaxis(0, int(SCALE * 110))[6:])
        self.assertIsNone(travel_centre_mm(params, None))
        self.assertIsNone(travel_centre_mm(None, SCALE))


class TestTravelCentreViaStage(unittest.TestCase):

    def test_stage_reports_centre_and_reason(self):
        stage = stage_with(b'')
        params = decode_stage_axis_params(stageaxis(0, int(SCALE * 110))[6:])
        centre, reason = stage.travel_centre(params)
        self.assertAlmostEqual(centre, 55.0, places=4)
        self.assertIn('110', reason)

    def test_stage_refuses_a_bad_read(self):
        stage = stage_with(b'')
        params = decode_stage_axis_params(
            stageaxis(0, int(SCALE * 110), cnts_per_unit=99)[6:])
        centre, reason = stage.travel_centre(params)
        self.assertIsNone(centre)
        self.assertIn('field offsets are wrong', reason)


class TestPositionDisplay(unittest.TestCase):
    """main() used to do 'get_position(1) or 0.0'."""

    def test_unknown_is_not_rendered_as_zero(self):
        shown = format_position(None)
        self.assertIn('unknown', shown)
        self.assertNotIn('0.00000', shown)

    def test_a_real_zero_still_reads_as_zero(self):
        self.assertIn('0.00000', format_position(0.0))

    def test_ordinary_value(self):
        self.assertIn('2.00000', format_position(2.0))

    def test_negative_value(self):
        self.assertIn('-1.50000', format_position(-1.5))


if __name__ == '__main__':
    unittest.main()
