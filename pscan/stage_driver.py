import serial
import serial.tools.list_ports
import struct
import time
import os
import sys

# Inbound APT message IDs.
MSG_GET_POSCOUNTER = 0x0412
MSG_MOVE_COMPLETED = 0x0464
MSG_MOVE_STOPPED   = 0x0466

# Outbound.
MSG_MOVE_STOP = 0x0465
MSG_SET_VELPARAMS = 0x0413
MSG_REQ_VELPARAMS = 0x0414
MSG_GET_VELPARAMS = 0x0415

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
