import argparse
import sys
import os
import struct
import time
import serial
import serial.tools.list_ports

from pscan.stage_driver import (
    MSG_SET_VELPARAMS, MSG_REQ_VELPARAMS, MSG_GET_VELPARAMS,
    MSG_REQ_POSCOUNTER, MSG_GET_POSCOUNTER,
    MSG_MOVE_HOME, MSG_MOVE_HOMED,
    MSG_REQ_HOMEPARAMS, MSG_GET_HOMEPARAMS,
    MSG_REQ_STATUSBITS, MSG_GET_STATUSBITS,
    MSG_REQ_STAGEAXISPARAMS, MSG_GET_STAGEAXISPARAMS,
    STATUS_HOMED, STATUS_HOMING,
    DEFAULT_MANUAL_VELOCITY_MM_S, velocity_scale,
    decode_home_params, describe_zero,
    decode_stage_axis_params, validate_travel, travel_centre_mm,
    travel_centre_counts, describe_travel, describe_axis_raw,
    scale_disagreement,
)
from pscan.calibrate import (
    LEGACY_COUNTS_PER_MM, SOURCE_LEGACY,
    counts_per_mm_for, describe_source, run_calibration,
)

# How long to wait for a homing seek to report back. Generous on purpose: the
# seek length is not knowable here - it depends on where the stage happens to
# be, the homing velocity stored in the controller's flash, and which limit
# switch the home direction points at. This is a timeout, not a delay; a
# controller that confirms in two seconds returns in two seconds.
HOME_TIMEOUT_S = 60.0

# ==========================================
# 1. Simple Blocking Keystroke Reader
# ==========================================
if os.name == 'nt':
    import msvcrt
    def get_key():
        key = msvcrt.getch() # Blocking wait
        if key in (b'\xe0', b'\x00'): 
            return key + msvcrt.getch()
        return key.decode('utf-8', 'ignore')
else:
    import tty
    import termios
    def get_key():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1) # Blocking wait, suppresses SSH echo natively
            if ch == '\x1b':
                ch += sys.stdin.read(2)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

# ==========================================
# 2. The Fixed Stage Driver
# ==========================================
class SimpleStage:
    def __init__(self, scale=None):
        self.ser = None
        # The legacy single scale, kept as the fallback and as what callers
        # that predate per-axis calibration still read.
        self.scale = float(scale) if scale else LEGACY_COUNTS_PER_MM
        # Per axis, because two axes can carry different encoders and because
        # the calibration store is keyed per axis. Populated by
        # apply_calibration() once the controller has identified itself.
        self.scales = {1: self.scale, 2: self.scale}
        self.scale_sources = {1: SOURCE_LEGACY, 2: SOURCE_LEGACY}
        self.velocity_mm_s = None

    def scale_for(self, axis_id):
        return self.scales.get(axis_id, self.scale)

    def apply_calibration(self, calibration=None):
        """Resolve each axis's counts-per-mm: stored, else controller, else default.

        Called after connect(), because the store is keyed by the controller's
        serial and the stage's part number and neither is known until the
        controller has been asked.
        """
        for axis_id in (1, 2):
            params = self.get_stage_axis_params(axis_id)
            value, source = counts_per_mm_for(params, axis_id,
                                              calibration=calibration,
                                              fallback=self.scale)
            self.scales[axis_id] = value
            self.scale_sources[axis_id] = source
        return self.scales

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
        """Read one framed APT packet as (msg_id, payload, source).

        APT headers are always 6 bytes. If bit 7 of the destination byte is
        set the header is the long form and bytes 2-3 hold the length of the
        data that follows; otherwise the packet is header-only.

        Framing properly is not a tidiness exercise. The controller emits
        MOVE_COMPLETED (20 bytes) and MOVE_HOMED unsolicited, so the old
        "wait for 12 bytes then read 12" approach would slice straight
        through one of those and then fail to recognise the position reply -
        which is why a perfectly deterministic home looked random and the
        position on screen was sometimes stale. stage_driver.py has had this
        fix for a while; pystage never received it.

        `source` is the sixth header byte, which for a reply is the bay that
        sent it (0x21 for X, 0x22 for Y). That is what lets a two-axis home
        tell which axis has finished.
        """
        header = self._read_exactly(6, timeout_s)
        if header is None:
            return None

        msg_id = struct.unpack_from('<H', header, 0)[0]
        source = header[5]
        payload = b''
        if header[4] & 0x80:
            length = struct.unpack_from('<H', header, 2)[0]
            if length:
                payload = self._read_exactly(length, timeout_s)
                if payload is None:
                    return None
        return msg_id, payload, source

    def _await_reply(self, msg_id, dest, timeout_s=0.5):
        """Return the payload of the next `msg_id`, skipping anything else.

        Anything unsolicited that arrives first is discarded rather than
        mistaken for the answer.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None

            packet = self._read_packet(timeout_s=remaining)
            if packet is None:
                return None

            got_id, payload, source = packet
            if got_id == msg_id and (dest is None or source == dest):
                return payload

    def get_velocity_params(self, axis_id):
        """Read this axis's velocity parameters, or None on no reply.

        GET_VELPARAMS carries chan_ident, min velocity, acceleration and max
        velocity in a 14-byte payload.
        """
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_VELPARAMS, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_VELPARAMS, dest)
        if payload is None or len(payload) < 14:
            return None
        _chan, min_v, accel, max_v = struct.unpack_from('<Hlll', payload, 0)
        return {'min': min_v, 'accel': accel, 'max': max_v}

    def get_status_bits(self, axis_id):
        """Read this axis's status word, or None on no reply."""
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_STATUSBITS, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_STATUSBITS, dest)
        if payload is None or len(payload) < 6:
            return None
        _chan, bits = struct.unpack_from('<HL', payload, 0)
        return bits

    def is_homed(self, axis_id):
        """True / False / None, where None means the controller did not say.

        Asked because an absolute move to 'the centre of travel' is only
        meaningful once homing has fixed the coordinate frame. An axis homed
        in a previous session is still homed, so this is read from the
        controller rather than assumed false at startup.
        """
        bits = self.get_status_bits(axis_id)
        if bits is None:
            return None
        if bits & STATUS_HOMING:
            return False
        return bool(bits & STATUS_HOMED)

    def get_stage_axis_params(self, axis_id):
        """Read the stage's travel limits, or None on no reply."""
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_STAGEAXISPARAMS, 0x01,
                                   0x00, dest, 0x01))

        payload = self._await_reply(MSG_GET_STAGEAXISPARAMS, dest)
        return decode_stage_axis_params(payload)

    def travel_centre(self, axis_id, params):
        """The centre of travel in COUNTS, or (None, why it is unavailable).

        Reads the position counter itself, because the check that matters is
        whether the reported limits bracket where the stage actually is.
        """
        position = self.get_position_counts(axis_id)
        ok, reason = validate_travel(params, position)
        if not ok:
            return None, reason
        return travel_centre_counts(params), reason

    def get_home_params(self, axis_id):
        """Read where this axis's zero will land, or None on no reply."""
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_HOMEPARAMS, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_HOMEPARAMS, dest)
        return decode_home_params(payload, self.scale_for(axis_id))

    def set_velocity_mm_s(self, axis_id, mm_per_s):
        """Set max velocity, leaving acceleration as the controller has it.

        Acceleration has its own scaling constant, which is not calibrated
        here, so it is read back and written through unchanged rather than
        guessed at.
        """
        current = self.get_velocity_params(axis_id)
        if current is None:
            return False

        raw = int(round(mm_per_s * velocity_scale(self.scale_for(axis_id))))
        dest = 0x20 + axis_id
        payload = struct.pack('<Hlll', 0x01, current['min'],
                              current['accel'], raw)
        header = struct.pack('<HHBB', MSG_SET_VELPARAMS, len(payload),
                             dest | 0x80, 0x01)
        self.ser.write(header + payload)
        time.sleep(0.05)
        return True

    def connect(self):
        for p in serial.tools.list_ports.comports():
            if p.manufacturer and ("FTDI" in p.manufacturer or "Thorlabs" in p.manufacturer):
                port = p.device
                break
        else:
            port = 'COM6' if os.name == 'nt' else '/dev/ttyUSB0'
            
        # Short read timeout so the framed reader stays responsive: it polls
        # read() against its own deadline, and a 1.0 s port timeout made the
        # granularity of every wait a full second.
        self.ser = serial.Serial(port, 115200, timeout=0.05)
        self.ser.dtr = True; self.ser.rts = True
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        
        self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, 0x50, 0x01))
        time.sleep(0.1)
        self.set_enable(1, True)
        self.set_enable(2, True)
        time.sleep(0.2)

        # Other tools change the velocity for their own purposes - a fly
        # scan can leave an axis at 0.02 mm/s - so start from a known,
        # jog-friendly speed rather than inheriting whatever was left behind.
        if all(self.set_velocity_mm_s(axis, DEFAULT_MANUAL_VELOCITY_MM_S)
               for axis in (1, 2)):
            self.velocity_mm_s = DEFAULT_MANUAL_VELOCITY_MM_S

        return port

    def set_enable(self, axis_id, enable=True):
        try:
            dest = 0x20 + axis_id
            cmd = 0x01 if enable else 0x02
            param1 = axis_id if os.name == 'nt' else 0x01
            self.ser.write(struct.pack('<HBBBB', 0x0210, param1, cmd, dest, 0x01))
        except (serial.SerialException, OSError):
            print("\n\n[HARDWARE FAULT] USB connection lost!")
            print(" -> This is usually caused by a motor power surge (Back-EMF) when re-enabling.")
            print(" -> Please power-cycle the Thorlabs controller, replug the USB, and restart pystage.")
            sys.exit(1)

    def home_axis(self, axis_id):
        """Start a homing seek. Does not wait - see home_axes()."""
        dest = 0x20 + axis_id
        self.ser.write(struct.pack('<HBBBB', MSG_MOVE_HOME, 0x01, 0x00,
                                   dest, 0x01))

    def wait_for_homed(self, axis_ids, timeout_s=HOME_TIMEOUT_S):
        """Block until MOVE_HOMED arrives from each axis. Returns those seen.

        The controller reports completion per bay, so an axis that never
        confirms is named rather than averaged into a general success.
        """
        pending = {0x20 + axis_id: axis_id for axis_id in axis_ids}
        confirmed = set()
        deadline = time.monotonic() + timeout_s

        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            packet = self._read_packet(timeout_s=remaining)
            if packet is None:
                break

            msg_id, _payload, source = packet
            if msg_id == MSG_MOVE_HOMED and source in pending:
                confirmed.add(pending.pop(source))

        return confirmed

    def home_axes(self, axis_ids=(1, 2), timeout_s=HOME_TIMEOUT_S):
        """Home these axes and wait for each to confirm.

        Returns (confirmed, missing) as sets of axis ids.

        Waiting on MOVE_HOMED replaced a flat time.sleep(10.0). The sleep was
        wrong in both directions: a long seek was still running when the UI
        announced success - and the position read that followed therefore
        landed mid-move - while a short seek wasted the rest of the ten
        seconds.
        """
        self.ser.reset_input_buffer()
        for axis_id in axis_ids:
            self.home_axis(axis_id)
            time.sleep(0.05)

        confirmed = self.wait_for_homed(axis_ids, timeout_s=timeout_s)
        return confirmed, set(axis_ids) - confirmed

    def get_position_counts(self, axis_id):
        """Raw encoder counts, unscaled. The authority for anything that moves.

        Separate from get_position() because counts need no counts_per_mm to
        be correct, and comparing a position against the controller's own
        travel limits has to happen in the controller's own units.
        """
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_POSCOUNTER, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_POSCOUNTER, dest)
        if payload is None or len(payload) < 6:
            return None
        _chan, counts = struct.unpack_from('<Hl', payload, 0)
        return counts

    def get_position(self, axis_id):
        counts = self.get_position_counts(axis_id)
        if counts is None:
            return None
        return counts / self.scale_for(axis_id)

    def move_relative(self, axis_id, distance_mm):
        self.move_relative_counts(
            axis_id, int(distance_mm * self.scale_for(axis_id)))

    def move_relative_counts(self, axis_id, counts):
        """Jog by an exact number of counts, with no scale in the way.

        What the calibration uses: the quantity being calibrated is the
        counts-to-mm conversion, so setting up the measurement through it
        would make the result depend on the answer.
        """
        dest = 0x20 + axis_id
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0445, 0x06, 0x00, dest | 0x80, 0x01)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0448, 0x01, 0x00, dest, 0x01))

    def move_absolute(self, axis_id, position_mm):
        self.move_absolute_counts(
            axis_id, int(position_mm * self.scale_for(axis_id)))

    def move_absolute_counts(self, axis_id, counts):
        """Move to an absolute encoder count, with no scale in the way.

        What C uses. The centre of travel is a midpoint in counts, and going
        through millimetres to get there would reintroduce exactly the scale
        error the midpoint does not otherwise care about.
        """
        dest = 0x20 + axis_id
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0450, 0x06, 0x00, dest | 0x80, 0x01)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0453, 0x01, 0x00, dest, 0x01))

# ==========================================
# 3. Main CLI Application
# ==========================================
def counts_to_mm(counts, scale):
    """Encoder counts to mm, preserving 'unknown' rather than inventing zero."""
    if counts is None or not scale:
        return None
    return counts / scale


def format_position(pos):
    """Render a position, or say it is unknown. A failed read is not zero."""
    if pos is None:
        return " unknown"
    return f"{pos:8.5f}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='pystage',
        description='Jog the Thorlabs stages, and calibrate counts-per-mm.')
    parser.add_argument(
        '--calibrate', action='store_true',
        help='establish counts-per-mm for this stage and controller, then '
             'store it, instead of opening the jog UI')
    parser.add_argument(
        '--span', action='store_true',
        help='with --calibrate, also travel the full reported range to '
             'cross-check against the datasheet travel. Moves the stage '
             'across its whole travel, so clear the sample first.')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    stage = SimpleStage()
    try:
        port = stage.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    # Before anything is displayed or commanded in millimetres, settle what a
    # millimetre is on this rig: a stored measurement if one exists, else the
    # controller's own figure, else the legacy 34304 that started all this.
    stage.apply_calibration()

    if args.calibrate:
        run_calibration(stage, allow_span=args.span)
        stage.apply_calibration()
        print("\nCalibration finished. Re-run 'pystage' to use it.")
        return

    current_step = 0.5
    msg = "Connected. Use Arrow Keys to jog."

    # Not 'or 0.0'. A position the controller did not report is unknown, and
    # showing it as 0.00000 mm claimed the stage was sitting exactly on zero -
    # the one reading most likely to be acted on, and the easiest to believe.
    # Counts are read first and millimetres derived from them, rather than the
    # other way round: counts are what the travel limits are compared against
    # and what a move is commanded in, so they are the thing that has to be
    # right even when the mm scale is not.
    pos_counts = {axis: stage.get_position_counts(axis) for axis in (1, 2)}
    x_pos = counts_to_mm(pos_counts[1], stage.scale_for(1))
    y_pos = counts_to_mm(pos_counts[2], stage.scale_for(2))

    x_enabled = True
    y_enabled = True
    needs_refresh = False

    # Read once up front and again on R/H. Shown rather than kept internal
    # because C is otherwise unexplainable: both where zero sits and how far
    # the stage travels are stored in the controller, differ between a BBD20x
    # and a BBD30x that were set up separately, and nothing in pscan writes
    # them. The panel showing the centre it will aim for is also the preview
    # of what C is about to do.
    zero = {axis: stage.get_home_params(axis) for axis in (1, 2)}
    travel = {axis: stage.get_stage_axis_params(axis) for axis in (1, 2)}

    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        x_status = "[ON]" if x_enabled else "[OFF]"
        y_status = "[ON]" if y_enabled else "[OFF]"
        
        print("==================================================")
        print(f" THORLABS PRECISION CONTROL CLI ({port})")
        print("==================================================")
        print(f" X-Axis Position: {format_position(x_pos)} mm  {x_status}")
        print(f" Y-Axis Position: {format_position(y_pos)} mm  {y_status}")
        speed = (f"{stage.velocity_mm_s:.2f} mm/s"
                 if stage.velocity_mm_s is not None else "as found")
        print(f"\n Current Step Size: {current_step:.5f} mm   "
              f"Velocity: {speed}")
        print("--------------------------------------------------")
        # 'Zero' rather than 'centre', because that is all this is: the
        # encoder origin that H last defined. Whether it is the middle of
        # travel depends on the offset below, which lives in the controller.
        print(f" Zero   X: {describe_zero(zero[1])}")
        print(f" Zero   Y: {describe_zero(zero[2])}")
        print(f" Travel X: {describe_travel(travel[1], stage.scale_for(1), pos_counts[1])}")
        print(f" Travel Y: {describe_travel(travel[2], stage.scale_for(2), pos_counts[2])}")
        # Said out loud, because a counts-per-mm nobody can see the
        # provenance of is exactly how 34304 survived on a rig that reports
        # 20000.
        print(f" Scale  X: {describe_source(stage.scale_for(1), stage.scale_sources[1])}")
        print(f" Scale  Y: {describe_source(stage.scale_for(2), stage.scale_sources[2])}")
        print("==================================================")
        print("   Up/Down/L/R : Jog Axes   |  J : Set Step Size")
        print("   G           : Go To Abs  |  C : Centre of Travel")
        print("   1 / 2       : Toggle X/Y |  R : Refresh Pos")
        print("   H           : Home Stage |  D : Dump Raw Axis")
        print("   K           : Calibrate  |  Q : Quit")
        print("==================================================")
        print(f" [MSG] {msg}")
        
        # Script naturally halts here. No flickering.
        key = get_key()
        
        if key:
            key_str = key.lower() if isinstance(key, str) else ''
            
            if key_str == 'q':
                break
                
            elif key_str == '1':
                x_enabled = not x_enabled
                stage.set_enable(1, x_enabled)
                msg = f"X-Axis power {'ENABLED' if x_enabled else 'DISABLED'}."
                
            elif key_str == '2':
                y_enabled = not y_enabled
                stage.set_enable(2, y_enabled)
                msg = f"Y-Axis power {'ENABLED' if y_enabled else 'DISABLED'}."
                
            elif key_str == 'j':
                try:
                    val = input("\n -> Enter new step size (mm): ")
                    current_step = abs(float(val))
                    msg = f"Step size updated to {current_step} mm."
                except ValueError:
                    msg = "Invalid input. Step size unchanged."
                    
            elif key_str == 'g':
                try:
                    tgt_x = input("\n -> Enter absolute X target (mm): ")
                    tgt_y = input(" -> Enter absolute Y target (mm): ")
                    if x_enabled: stage.move_absolute(1, float(tgt_x))
                    if y_enabled: stage.move_absolute(2, float(tgt_y))
                    msg = f"Commanded Absolute Move to ({tgt_x}, {tgt_y})."
                    needs_refresh = True
                except ValueError:
                    msg = "Invalid coordinates entered. Move cancelled."

            elif key_str == 'k':
                # The same procedure as 'pystage --calibrate', from inside
                # the UI. --span is not offered here: driving the full travel
                # is worth a deliberate command line, not a keystroke next to
                # the jog keys.
                print("\n --- CALIBRATE counts-per-mm ---")
                try:
                    run_calibration(stage)
                except Exception as e:
                    print(f" calibration aborted: {e}")
                stage.apply_calibration()
                zero = {axis: stage.get_home_params(axis) for axis in (1, 2)}
                travel = {axis: stage.get_stage_axis_params(axis)
                          for axis in (1, 2)}
                print(" --- press any key to continue ---")
                get_key()
                msg = "Calibration run; scale reloaded."
                needs_refresh = True

            elif key_str == 'd':
                # Every decoded field, raw. The 0x04F2 layout is transcribed
                # from third-party libraries rather than Thorlabs' manual, so
                # when C refuses this is what says whether the read is
                # garbage or merely disagrees with pscan's mm scale.
                print("\n --- REQ_STAGEAXISPARAMS (0x04F1) raw ---")
                for axis, label in ((1, 'X'), (2, 'Y')):
                    params = stage.get_stage_axis_params(axis)
                    print(f" {label}: {describe_axis_raw(params)}")
                    print(f"    position counter: "
                          f"{stage.get_position_counts(axis)} counts")
                    if params:
                        centre = travel_centre_counts(params)
                        print(f"    midpoint would be: {centre} counts")
                        ok, why = validate_travel(
                            params, stage.get_position_counts(axis))
                        print(f"    usable for C: {ok} - {why}")
                        warn = scale_disagreement(params, stage.scale_for(axis))
                        if warn:
                            print(f"    scale: {warn}")
                print(" --- press any key to continue ---")
                get_key()
                msg = "Raw axis parameters dumped above."

            elif key_str == 'r':
                zero = {axis: stage.get_home_params(axis) for axis in (1, 2)}
                travel = {axis: stage.get_stage_axis_params(axis)
                          for axis in (1, 2)}
                msg = ("Positions, zero and travel refreshed directly from "
                       "hardware.")
                needs_refresh = True

            elif key_str == 'c':
                # The geometric centre of travel, computed as the MIDPOINT of
                # the span the controller reports - not as half the travel
                # from zero. The midpoint is invariant to where homing put
                # zero, so a stage homed to its reverse limit and one
                # carrying a home offset both land in the same physical
                # place. That invariance is the requirement.
                #
                # This used to move to absolute (0,0) and call it "Center",
                # which on a stage whose zero sits on a limit switch is a
                # corner.
                moved, skipped, refused = [], [], []
                for axis_id, enabled, label in ((1, x_enabled, 'X'),
                                                (2, y_enabled, 'Y')):
                    if not enabled:
                        skipped.append(label)
                        continue

                    centre, reason = stage.travel_centre(axis_id,
                                                         travel[axis_id])
                    if centre is None:
                        refused.append(f"{label}: {reason}")
                        continue

                    # An absolute move means nothing in a frame homing has
                    # not fixed yet, and could drive into a limit. Refusing
                    # is the safe answer and H is one keystroke away.
                    homed = stage.is_homed(axis_id)
                    if homed is None:
                        refused.append(f"{label}: controller did not say "
                                       f"whether it is homed")
                        continue
                    if not homed:
                        refused.append(f"{label}: not homed - press H first")
                        continue

                    stage.move_absolute_counts(axis_id, centre)
                    centre_mm = travel_centre_mm(travel[axis_id])
                    moved.append(f"{label} to {centre_mm:.5f} mm "
                                 f"({centre} counts)")
                    time.sleep(0.1)

                parts = []
                if moved:
                    parts.append(f"Centring {', '.join(moved)}.")
                if skipped:
                    parts.append(f"{'/'.join(skipped)} disabled, left alone.")
                if refused:
                    parts.append(f"REFUSED - {'; '.join(refused)}.")
                msg = ' '.join(parts) if parts else "Nothing to do."
                needs_refresh = True

            elif key_str == 'h':
                stage.set_enable(1, True)
                time.sleep(0.1)
                stage.set_enable(2, True)
                time.sleep(0.1)
                x_enabled, y_enabled = True, True

                # Printed before blocking: the panel above was drawn with the
                # previous message, and a silent terminal during a seek that
                # can take a minute reads as a hang.
                print("\n -> HOMING. Waiting for the controller to confirm"
                      f" (up to {HOME_TIMEOUT_S:.0f}s)...")
                confirmed, missing = stage.home_axes((1, 2))

                names = {1: 'X', 2: 'Y'}
                if not missing:
                    msg = "Both axes homed and confirmed. Zero is now redefined."
                elif confirmed:
                    msg = (f"Homed {'+'.join(names[a] for a in sorted(confirmed))}"
                           f", but {'/'.join(names[a] for a in sorted(missing))}"
                           f" never confirmed - it may still be moving or have"
                           f" tripped out.")
                else:
                    msg = ("No axis confirmed homing. Check the controller is"
                           " enabled and not already at its limit.")

                # Zero has just been redefined, so both the stored offset and
                # the travel limits expressed against it are worth re-reading
                # rather than showing what they were before.
                zero = {axis: stage.get_home_params(axis) for axis in (1, 2)}
                travel = {axis: stage.get_stage_axis_params(axis)
                          for axis in (1, 2)}
                needs_refresh = True
                
            elif key == '\x1b[C' or key == b'\xe0M':
                if x_enabled:
                    stage.move_relative(1, current_step)
                    msg = f"Jogged X +{current_step} mm"
                    needs_refresh = True
                else: msg = "X-Axis is disabled! Press '1' to enable."
                
            elif key == '\x1b[D' or key == b'\xe0K':
                if x_enabled:
                    stage.move_relative(1, -current_step)
                    msg = f"Jogged X -{current_step} mm"
                    needs_refresh = True
                else: msg = "X-Axis is disabled! Press '1' to enable."
                
            elif key == '\x1b[A' or key == b'\xe0H':
                if y_enabled:
                    stage.move_relative(2, current_step)
                    msg = f"Jogged Y +{current_step} mm"
                    needs_refresh = True
                else: msg = "Y-Axis is disabled! Press '2' to enable."
                    
            elif key == '\x1b[B' or key == b'\xe0P':
                if y_enabled:
                    stage.move_relative(2, -current_step)
                    msg = f"Jogged Y -{current_step} mm"
                    needs_refresh = True
                else: msg = "Y-Axis is disabled! Press '2' to enable."
                
            if needs_refresh:
                time.sleep(0.2)
                for axis in (1, 2):
                    counts = stage.get_position_counts(axis)
                    if counts is not None:
                        pos_counts[axis] = counts
                x_pos = counts_to_mm(pos_counts[1], stage.scale_for(1))
                y_pos = counts_to_mm(pos_counts[2], stage.scale_for(2))
                needs_refresh = False

    os.system('cls' if os.name == 'nt' else 'clear')
    print("Stage CLI closed.")

if __name__ == "__main__":
    main()
