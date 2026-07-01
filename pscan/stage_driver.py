import serial
import serial.tools.list_ports
import struct
import time
import os
import sys

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
            self.ser = serial.Serial(self.port, 115200, timeout=1)
            self.ser.dtr = True
            self.ser.rts = True
            time.sleep(0.1)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            
            # Init motherboard and enable channels
            self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, self.DEST_MOTHERBOARD, self.SOURCE_PC))
            time.sleep(0.2)
            self.ser.write(struct.pack('<HBBBB', 0x0210, 0x01, 0x01, self.DEST_CH1, self.SOURCE_PC))
            self.ser.write(struct.pack('<HBBBB', 0x0210, 0x02, 0x01, self.DEST_CH2, self.SOURCE_PC))
            time.sleep(0.5)
            print("Thorlabs stage controller initialized successfully.")
        except Exception as e:
            print(f"Error binding to Thorlabs Stage: {e}")
            sys.exit(1)

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
