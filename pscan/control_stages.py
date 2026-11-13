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
    DEFAULT_MANUAL_VELOCITY_MM_S, velocity_scale,
    decode_home_params, describe_zero,
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
    def __init__(self):
        self.ser = None
        self.scale = 34304.0
        self.velocity_mm_s = None

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

    def get_home_params(self, axis_id):
        """Read where this axis's zero will land, or None on no reply."""
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_HOMEPARAMS, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_HOMEPARAMS, dest)
        return decode_home_params(payload, self.scale)

    def set_velocity_mm_s(self, axis_id, mm_per_s):
        """Set max velocity, leaving acceleration as the controller has it.

        Acceleration has its own scaling constant, which is not calibrated
        here, so it is read back and written through unchanged rather than
        guessed at.
        """
        current = self.get_velocity_params(axis_id)
        if current is None:
            return False

        raw = int(round(mm_per_s * velocity_scale(self.scale)))
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

    def get_position(self, axis_id):
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', MSG_REQ_POSCOUNTER, 0x01, 0x00,
                                   dest, 0x01))

        payload = self._await_reply(MSG_GET_POSCOUNTER, dest)
        if payload is None or len(payload) < 6:
            return None
        _chan, counts = struct.unpack_from('<Hl', payload, 0)
        return counts / self.scale

    def move_relative(self, axis_id, distance_mm):
        dest = 0x20 + axis_id
        counts = int(distance_mm * self.scale)
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0445, 0x06, 0x00, dest | 0x80, 0x01)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0448, 0x01, 0x00, dest, 0x01))

    def move_absolute(self, axis_id, position_mm):
        dest = 0x20 + axis_id
        counts = int(position_mm * self.scale)
        data = struct.pack('<Hl', 0x01, counts)
        header = struct.pack('<HBBBB', 0x0450, 0x06, 0x00, dest | 0x80, 0x01)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0453, 0x01, 0x00, dest, 0x01))

# ==========================================
# 3. Main CLI Application
# ==========================================
def format_position(pos):
    """Render a position, or say it is unknown. A failed read is not zero."""
    if pos is None:
        return " unknown"
    return f"{pos:8.5f}"


def main():
    stage = SimpleStage()
    try:
        port = stage.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    current_step = 0.5 
    msg = "Connected. Use Arrow Keys to jog."

    # Not 'or 0.0'. A position the controller did not report is unknown, and
    # showing it as 0.00000 mm claimed the stage was sitting exactly on zero -
    # the one reading most likely to be acted on, and the easiest to believe.
    x_pos = stage.get_position(1)
    y_pos = stage.get_position(2)

    x_enabled = True
    y_enabled = True
    needs_refresh = False

    # Read once up front and again on R/H. Shown because 'go to zero' is
    # otherwise unexplainable: where zero sits is stored in the controller,
    # differs between a BBD20x and a BBD30x that were set up separately, and
    # nothing in pscan writes it.
    zero_x = stage.get_home_params(1)
    zero_y = stage.get_home_params(2)

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
        print(f" Zero X: {describe_zero(zero_x)}")
        print(f" Zero Y: {describe_zero(zero_y)}")
        print("==================================================")
        print("   Up/Down/L/R : Jog Axes   |  J : Set Step Size")
        print("   G           : Go To Abs  |  C : Go To Zero (0,0)")
        print("   1 / 2       : Toggle X/Y |  R : Refresh Pos")
        print("   H           : Home Stage |  Q : Quit")
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

            elif key_str == 'r':
                zero_x = stage.get_home_params(1)
                zero_y = stage.get_home_params(2)
                msg = "Positions and zero refreshed directly from hardware."
                needs_refresh = True

            elif key_str == 'c':
                # Report per axis what was actually commanded. This used to
                # skip a disabled axis and then announce "Commanded Center
                # (0.0, 0.0)" regardless, which is how a one-axis move came
                # to look like a two-axis centre that half worked.
                moved, skipped = [], []
                for axis_id, enabled, label in ((1, x_enabled, 'X'),
                                                (2, y_enabled, 'Y')):
                    if enabled:
                        stage.move_absolute(axis_id, 0.0)
                        moved.append(label)
                        time.sleep(0.1)
                    else:
                        skipped.append(label)

                if not moved:
                    msg = ("Nothing commanded: both axes are disabled. "
                           "Press '1'/'2' to enable.")
                else:
                    msg = f"Commanded {'+'.join(moved)} to zero."
                    if skipped:
                        msg += (f" {'/'.join(skipped)} SKIPPED - disabled, so"
                                f" this is NOT both axes.")
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

                # Zero has just been redefined, so the stored offset is worth
                # re-reading rather than showing what it was before.
                zero_x = stage.get_home_params(1)
                zero_y = stage.get_home_params(2)
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
                real_x = stage.get_position(1)
                real_y = stage.get_position(2)
                if real_x is not None: x_pos = real_x
                if real_y is not None: y_pos = real_y
                needs_refresh = False

    os.system('cls' if os.name == 'nt' else 'clear')
    print("Stage CLI closed.")

if __name__ == "__main__":
    main()
