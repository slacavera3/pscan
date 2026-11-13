import serial
import serial.tools.list_ports
import struct
import time
import os
import sys

# Inbound APT message IDs.
MSG_GET_POSCOUNTER = 0x0412
MSG_GET_STATUSBITS = 0x042A
MSG_GET_HOMEPARAMS = 0x0442
MSG_MOVE_HOMED     = 0x0444
MSG_MOVE_COMPLETED = 0x0464
MSG_MOVE_STOPPED   = 0x0466
MSG_GET_STAGEAXISPARAMS = 0x04F2

# Outbound.
MSG_REQ_POSCOUNTER = 0x0411
MSG_MOVE_STOP = 0x0465
MSG_SET_VELPARAMS = 0x0413
MSG_REQ_VELPARAMS = 0x0414
MSG_GET_VELPARAMS = 0x0415
MSG_REQ_STATUSBITS = 0x0429
MSG_REQ_HOMEPARAMS = 0x0441
MSG_MOVE_HOME      = 0x0443
MSG_REQ_STAGEAXISPARAMS = 0x04F1

# Motor status bits. Only the two that say whether a coordinate frame exists
# yet: an absolute move means nothing until the axis has been homed.
STATUS_HOMING = 0x00000200
STATUS_HOMED  = 0x00000400

# APT velocity scaling for brushless (BBD) controllers is
# counts_per_mm * T * 65536, where T is the controller's sampling interval.
# T = 102.4 us is CONFIRMED for the BBD303: calibrating against measured
# motion gave 230531 raw per mm/s where this formula predicts 230210, a
# 0.14% agreement. A different controller family needs a different T.
BBD_SAMPLE_INTERVAL_S = 102.4e-6

# Velocity to leave on the stage for manual use. Anything that changes the
# velocity for its own purposes should put it back to this, so the next
# person to jog the stage does not find it crawling or bolting.
DEFAULT_MANUAL_VELOCITY_MM_S = 5.0


def velocity_scale(counts_per_mm, sample_interval_s=BBD_SAMPLE_INTERVAL_S):
    """Raw APT velocity units per mm/s.

    A calculated fallback for callers with no measured calibration. Prefer
    flyscan's VelocityCalibration where one exists - it is measured on the
    actual hardware rather than assuming the sampling interval.
    """
    return counts_per_mm * sample_interval_s * 65536

# Param2 of MOVE_STOP selects the stop mode.
STOP_IMMEDIATE = 0x01
STOP_PROFILED  = 0x02

# HOMEPARAMS fields. These live in the controller's own flash and nothing in
# pscan writes them - they are read here only so a tool can SAY where zero is
# rather than leaving someone to infer it from where the stage ended up.
#
# This is the whole reason 'home' and 'go to zero' look unrelated between two
# controllers: the home direction, the switch it seeks and the offset applied
# afterwards are per-controller stored state, usually set once in Kinesis or
# APT User and then forgotten.
HOME_DIR_FORWARD = 0x01
HOME_DIR_REVERSE = 0x02
LIMIT_SWITCH_REVERSE = 0x01
LIMIT_SWITCH_FORWARD = 0x04

# chan_ident, home_dir, limit_switch, home_velocity, offset_distance
HOMEPARAMS_STRUCT = '<HHHll'
HOMEPARAMS_LEN = struct.calcsize(HOMEPARAMS_STRUCT)  # 14


def decode_home_params(payload, counts_per_mm=None):
    """Unpack a GET_HOMEPARAMS payload into something readable.

    `offset_mm` is None when no counts_per_mm is supplied, because converting
    with a wrong scale would be worse than not converting: pscan's hardcoded
    34304 is the Z8xx/TDC001 figure and is very likely wrong for a brushless
    stage. The raw count is always reported alongside it.
    """
    if payload is None or len(payload) < HOMEPARAMS_LEN:
        return None

    _chan, direction, limit, velocity, offset = struct.unpack_from(
        HOMEPARAMS_STRUCT, payload, 0)

    directions = {HOME_DIR_FORWARD: 'forward', HOME_DIR_REVERSE: 'reverse'}
    switches = {LIMIT_SWITCH_REVERSE: 'reverse', LIMIT_SWITCH_FORWARD: 'forward'}

    return {
        'direction': directions.get(direction, f'unknown (0x{direction:02x})'),
        'limit_switch': switches.get(limit, f'unknown (0x{limit:02x})'),
        'velocity_counts': velocity,
        'offset_counts': offset,
        'offset_mm': (offset / counts_per_mm) if counts_per_mm else None,
    }


# --- Stage travel, for a 'go to the centre' that survives being moved ---
#
# STAGEAXISPARAMS (0x04F2, called PMDSTAGEAXISPARAMS in older revisions of the
# protocol) is the only thing on the wire that reports a stage's travel. The
# field layout below is NOT from Thorlabs' manual - it is transcribed from two
# third-party libraries, thorpy and pystage_apt, which declare it identically:
#
#   chan_ident H, stage_id H, axis_id H, part_no_axis 16s, serial_num I,
#   cnts_per_unit I, min_pos I, max_pos I, max_accn I, max_dec I, max_vel I,
#   then padding, 74 bytes in all.
#
# Third-party and therefore not to be trusted on its own, which is what
# cnts_per_unit is for. It sits immediately before min_pos, so if it decodes
# to the counts-per-mm this rig is known to use, the alignment is right and
# the two fields after it can be believed. If it decodes to anything else the
# layout is wrong for this controller and the numbers are garbage - which is
# why validate_travel() refuses rather than falling back to a guess. Nothing
# commands a move off an unvalidated read.
STAGEAXIS_LEN = 74
_STAGEAXIS_HEAD = '<HHH16sII'          # through cnts_per_unit, 30 bytes
_STAGEAXIS_LIMITS_OFFSET = 30

# Signed, deliberately, though both libraries declare these unsigned. A stage
# whose travel straddles zero reports a negative min_pos, and read unsigned
# that arrives as ~4.29e9 and gets thrown out by validation for no reason. No
# real travel approaches 2^31 counts, so reading signed is correct where
# unsigned is correct and also correct where it is not.
_STAGEAXIS_LIMITS = '<ii'


def decode_stage_axis_params(payload):
    """Unpack a GET_STAGEAXISPARAMS payload. Validate before trusting it."""
    if payload is None or len(payload) < _STAGEAXIS_LIMITS_OFFSET + 8:
        return None

    chan, stage_id, axis_id, part_no, serial, cnts = struct.unpack_from(
        _STAGEAXIS_HEAD, payload, 0)
    min_pos, max_pos = struct.unpack_from(
        _STAGEAXIS_LIMITS, payload, _STAGEAXIS_LIMITS_OFFSET)

    return {
        'chan_ident': chan,
        'stage_id': stage_id,
        'axis_id': axis_id,
        'part_no': part_no.split(b'\x00')[0].decode('ascii', 'replace').strip(),
        'serial_num': serial,
        'cnts_per_unit': cnts,
        'min_counts': min_pos,
        'max_counts': max_pos,
    }


def validate_travel(params, counts_per_mm, min_span_mm=1.0, max_span_mm=500.0,
                    tolerance=0.02):
    """Is this travel read safe to command a move from? (ok, reason).

    The cnts_per_unit check is the load-bearing one: it proves the byte
    offsets are right for this controller. The span checks only catch a
    reading that is self-consistent but absurd.
    """
    if params is None:
        return False, "controller did not report stage axis parameters"

    if not counts_per_mm:
        return False, "no counts-per-mm to check the reply against"

    cnts = params['cnts_per_unit']
    if cnts <= 0:
        return False, f"cnts_per_unit is {cnts}"
    if abs(cnts - counts_per_mm) > tolerance * counts_per_mm:
        return False, (f"cnts_per_unit is {cnts}, expected ~{counts_per_mm:.0f}"
                       f" - the 0x04F2 field offsets are wrong for this"
                       f" controller, so min/max cannot be trusted")

    span_counts = params['max_counts'] - params['min_counts']
    if span_counts <= 0:
        return False, (f"max_counts {params['max_counts']} is not above "
                       f"min_counts {params['min_counts']}")

    span_mm = span_counts / counts_per_mm
    if not (min_span_mm <= span_mm <= max_span_mm):
        return False, (f"travel of {span_mm:.3f} mm is outside the plausible "
                       f"{min_span_mm:g}-{max_span_mm:g} mm window")

    return True, f"{span_mm:.5f} mm of travel"


def travel_centre_mm(params, counts_per_mm):
    """The geometric centre of travel, in the controller's own coordinates.

    The midpoint of the reported span, NOT half the travel from zero. That is
    the whole point: min and max arrive in the same frame as the position
    counter, so their midpoint is the centre of the stage wherever homing
    happened to put zero - a stage homed to its reverse limit and one carrying
    a 55 mm home offset both give the right answer. Half-travel-from-zero only
    works for the first of those.
    """
    if params is None or not counts_per_mm:
        return None
    return ((params['min_counts'] + params['max_counts']) / 2.0) / counts_per_mm


def describe_travel(params, counts_per_mm):
    """One line of travel and centre for a UI, or why there is none."""
    ok, reason = validate_travel(params, counts_per_mm)
    if not ok:
        return f"unavailable - {reason}"

    low = params['min_counts'] / counts_per_mm
    high = params['max_counts'] / counts_per_mm
    centre = travel_centre_mm(params, counts_per_mm)
    name = params['part_no'] or 'unnamed axis'
    return (f"{low:.5f} to {high:.5f} mm, centre {centre:.5f} mm  [{name}]")


def describe_zero(params):
    """One line saying where this axis's zero sits, for a UI to show.

    Deliberately refuses to call anything 'the centre'. Whether zero is the
    middle of travel depends on the travel length, which is a property of the
    stage and is not reported over APT - so this says what it knows (distance
    from which limit switch) and leaves the rest alone.
    """
    if params is None:
        return "unknown (controller did not answer)"

    switch = f"{params['limit_switch']} limit"
    offset_mm = params['offset_mm']

    if offset_mm is None:
        return f"{params['offset_counts']} counts from the {switch}"
    if abs(offset_mm) < 1e-6:
        return f"at the {switch} - i.e. a corner of travel, not the centre"
    return f"{offset_mm:.5f} mm from the {switch}"


class ThorlabsStage:
    DEST_MOTHERBOARD = 0x50
    DEST_CH1 = 0x21  # X-Axis
    DEST_CH2 = 0x22  # Y-Axis
    SOURCE_PC = 0x01

    def __init__(self, port=None):
        self.port = port if port else self._find_stage_port()
        self.ser = None

    def _find_stage_port(self):
        for p in serial.tools.list_ports.comports():
            if p.manufacturer and ("FTDI" in p.manufacturer or "Thorlabs" in p.manufacturer):
                return p.device
            if p.description and ("APT" in p.description or "USB Serial Port" in p.description):
                return p.device
        return 'COM6' if os.name == 'nt' else '/dev/ttyUSB0'

    def connect(self):
        if self.ser is not None:
            return
            
        print(f"Connecting to Thorlabs Stage on {self.port}...")
        try:
            # Short read timeout: position polling during a move wants to come
            # back promptly rather than block for a second on a quiet port.
            self.ser = serial.Serial(self.port, 115200, timeout=0.05)
            self.ser.dtr = True
            self.ser.rts = True
            time.sleep(0.1)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            
            # Init motherboard and enable channels (Restored strictly to your master copy)
            self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, self.DEST_MOTHERBOARD, self.SOURCE_PC))
            time.sleep(0.2)
            self.ser.write(struct.pack('<HBBBB', 0x0210, 0x01, 0x01, self.DEST_CH1, self.SOURCE_PC))
            self.ser.write(struct.pack('<HBBBB', 0x0210, 0x02, 0x01, self.DEST_CH2, self.SOURCE_PC))
            time.sleep(0.5)
            print("Thorlabs stage controller initialized successfully.")
        except Exception as e:
            # Raise rather than sys.exit: exiting here skipped main()'s
            # teardown entirely, so anything already connected - a streaming
            # camera, an open iXon shutter - was left as it was.
            raise RuntimeError(f"Error binding to Thorlabs Stage: {e}") from e

    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
            self.ser = None

    def _get_dest_channel(self, axis_name):
        ax = str(axis_name).lower().strip()
        if ax == 'x': return self.DEST_CH1
        if ax == 'y': return self.DEST_CH2
        raise ValueError(f"Unknown axis identifier target: {axis_name}")

    def move_absolute(self, axis, position_mm, counts_per_mm):
        dest = self._get_dest_channel(axis)
        counts = int(position_mm * counts_per_mm)
        
        # Load trajectory
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0450, 0x06, 0x00, dest | 0x80, self.SOURCE_PC)
        self.ser.write(header + data)
        time.sleep(0.05)
        
        # CRITICAL: Execute ping!
        self.ser.write(struct.pack('<HBBBB', 0x0453, 0x01, 0x00, dest, self.SOURCE_PC))

    def move_relative(self, axis, distance_mm, counts_per_mm):
        dest = self._get_dest_channel(axis)
        counts = int(distance_mm * counts_per_mm)
        
        # Load trajectory
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0445, 0x06, 0x00, dest | 0x80, self.SOURCE_PC)
        self.ser.write(header + data)
        time.sleep(0.05)
        
        # CRITICAL: Execute ping!
        self.ser.write(struct.pack('<HBBBB', 0x0448, 0x01, 0x00, dest, self.SOURCE_PC))

    def _read_exactly(self, n, timeout_s):
        """Read exactly n bytes, or return None if they do not arrive in time."""
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while len(buf) < n:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
            elif time.monotonic() >= deadline:
                return None
        return bytes(buf)

    def _read_packet(self, timeout_s=0.5):
        """Read one framed APT packet as (msg_id, payload), or None on timeout.

        APT headers are always 6 bytes. If bit 7 of the destination byte is
        set, the header is the long form and bytes 2-3 hold the length of the
        data that follows; otherwise the packet is header-only. Framing
        properly matters because the controller emits MOVE_COMPLETED (20
        bytes) unsolicited, and a fixed-size 12-byte read would slice straight
        through it and silently fail to parse the position reply.
        """
        header = self._read_exactly(6, timeout_s)
        if header is None:
            return None

        msg_id = struct.unpack_from('<H', header, 0)[0]
        payload = b''
        if header[4] & 0x80:
            length = struct.unpack_from('<H', header, 2)[0]
            if length:
                payload = self._read_exactly(length, timeout_s)
                if payload is None:
                    return None
        return msg_id, payload

    def get_position(self, axis, counts_per_mm, timeout_s=0.5):
        dest = self._get_dest_channel(axis)
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', 0x0411, 0x01, 0x00, dest, self.SOURCE_PC))

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            packet = self._read_packet(timeout_s=remaining)
            if packet is None:
                return None

            msg_id, payload = packet
            # An unsolicited MOVE_COMPLETED can land ahead of our reply, so
            # keep reading until the packet we asked for shows up.
            if msg_id == MSG_GET_POSCOUNTER and len(payload) >= 6:
                _, counts = struct.unpack_from('<Hl', payload, 0)
                return counts / counts_per_mm

    def stop(self, axis, profiled=True, timeout_s=2.0):
        """Halt a move in progress and wait for the controller to confirm.

        A profiled stop decelerates through the configured ramp instead of
        cutting drive dead, which is kinder to a lead screw carrying a
        sample. Returns True only if MOVE_STOPPED came back.
        """
        if self.ser is None or not self.ser.is_open:
            return False

        dest = self._get_dest_channel(axis)
        stop_mode = STOP_PROFILED if profiled else STOP_IMMEDIATE
        self.ser.write(struct.pack(
            '<HBBBB', MSG_MOVE_STOP, 0x01, stop_mode, dest, self.SOURCE_PC
        ))

        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False

            packet = self._read_packet(timeout_s=remaining)
            if packet is None:
                return False
            if packet[0] == MSG_MOVE_STOPPED:
                return True

    def stop_all(self, profiled=True):
        """Stop every axis, reporting but not raising on individual failures.

        Called from main()'s teardown so an aborted run does not leave the
        stage travelling to its target with no software control. A second
        Ctrl+C still gets through, since KeyboardInterrupt is not an
        Exception.
        """
        for axis in ('x', 'y'):
            try:
                if not self.stop(axis, profiled=profiled):
                    # An axis that was already stationary sends no
                    # MOVE_STOPPED, so note it rather than alarm about it.
                    print(f"[note] Stage {axis.upper()} sent no stop "
                          f"confirmation; it may already have been still.")
            except Exception as e:
                print(f"[WARNING] Could not stop stage {axis.upper()}: {e}")

    def wait_for_move(self, axis, target_mm, counts_per_mm,
                      tolerance_mm=0.001, timeout_s=15.0, dwell_s=0.02):
        """Block until axis reaches target_mm, then dwell for mechanical ringdown.

        The encoder is the authority here rather than MOVE_COMPLETED, so this
        works on controllers that do not emit an end-of-move message. The
        timeout only costs time when a move genuinely fails to arrive, so it
        can be generous enough to cover the longest move in a scan - typically
        the fast axis travelling back to the start of the next row.

        Returns False on timeout, leaving the stage wherever it actually is.
        """
        deadline = time.monotonic() + timeout_s
        last_pos = None

        while time.monotonic() < deadline:
            pos = self.get_position(axis, counts_per_mm)
            if pos is not None:
                last_pos = pos
                if abs(pos - target_mm) <= tolerance_mm:
                    time.sleep(dwell_s)
                    return True
            time.sleep(0.002)

        seen = f"{last_pos:.5f} mm" if last_pos is not None else "no reply"
        print(
            f"\n[WARNING] Stage {str(axis).upper()} did not reach "
            f"{target_mm:.5f} mm within {timeout_s:.1f}s (last read: {seen}). "
            f"This point may have been acquired off-position."
        )
        return False
