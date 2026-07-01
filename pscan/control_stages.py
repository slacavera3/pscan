import serial
import serial.tools.list_ports
import struct
import time
import os
import sys

# Cross-platform non-blocking keypress imports
if os.name == 'nt':
    import msvcrt
else:
    import select, tty, termios

def get_key():
    """Cross-platform non-blocking keypress capture."""
    if os.name == 'nt':
        if msvcrt.kbhit():
            key = msvcrt.getch()
            # Intercept Windows Arrow Keys
            if key in (b'\xe0', b'\x00'):
                key = msvcrt.getch()
                if key == b'H': return 'up'
                if key == b'P': return 'down'
                if key == b'K': return 'left'
                if key == b'M': return 'right'
                return None
            return key.decode('utf-8', 'ignore').lower()
        return None
    else:
        if select.select([sys.stdin], [], [], 0)[0]:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
                # Intercept Linux ANSI Arrow Keys
                if ch == '\x1b':
                    sys.stdin.read(1) # Skip '['
                    opt = sys.stdin.read(1)
                    if opt == 'A': return 'up'
                    if opt == 'B': return 'down'
                    if opt == 'D': return 'left'
                    if opt == 'C': return 'right'
                    return None
                return ch.lower()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return None

class SimpleStage:
    def __init__(self):
        self.ser = None
        self.SOURCE_PC = 0x01
        self.DEST_CH1 = 0x21
        self.DEST_CH2 = 0x22

    def connect(self):
        ports = serial.tools.list_ports.comports()
        port = None
        
        # Priority 1: Genuine Thorlabs hardware
        for p in ports:
            if (p.manufacturer and ("FTDI" in p.manufacturer or "Thorlabs" in p.manufacturer)) or \
               (p.description and ("APT" in p.description or "USB Serial Port" in p.description)):
                port = p.device
                break
                
        # Priority 2: Prolific adapters
        if not port:
            for p in ports:
                if (p.manufacturer and "Prolific" in p.manufacturer) or \
                   (p.description and "Prolific" in p.description):
                    port = p.device
                    break
                    
        # Priority 3: Blind fallback
        if not port:
            port = 'COM6' if os.name == 'nt' else '/dev/ttyUSB0'
            
        print(f"Connecting to stage on: {port}")
        self.ser = serial.Serial(port, 115200, timeout=1.0)
        self.ser.dtr = True; self.ser.rts = True
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        
        # Init Motherboard & Enable Both Axes
        self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, 0x50, self.SOURCE_PC))
        time.sleep(0.2)
        self.set_enable(1, True)
        self.set_enable(2, True)
        time.sleep(0.5)
        return port

    def set_enable(self, axis, enable):
        dest = self.DEST_CH1 if axis == 1 else self.DEST_CH2
        param1 = 0x01 if axis == 1 else 0x02
        state = 0x01 if enable else 0x02
        self.ser.write(struct.pack('<HBBBB', 0x0210, param1, state, dest, self.SOURCE_PC))

    def move_rel(self, axis, counts):
        dest = self.DEST_CH1 if axis == 1 else self.DEST_CH2
        data = struct.pack('<Hl', 0x01, int(counts))
        header = struct.pack('<HBBBB', 0x0445, 0x06, 0x00, dest | 0x80, self.SOURCE_PC)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0448, 0x01, 0x00, dest, self.SOURCE_PC))

    def move_abs(self, axis, counts):
        dest = self.DEST_CH1 if axis == 1 else self.DEST_CH2
        data = struct.pack('<Hl', 0x01, int(counts))
        header = struct.pack('<HBBBB', 0x0450, 0x06, 0x00, dest | 0x80, self.SOURCE_PC)
        self.ser.write(header + data)
        time.sleep(0.05)
        self.ser.write(struct.pack('<HBBBB', 0x0453, 0x01, 0x00, dest, self.SOURCE_PC))

    def home_axis(self, axis):
        dest = self.DEST_CH1 if axis == 1 else self.DEST_CH2
        self.ser.write(struct.pack('<HBBBB', 0x0443, 0x01, 0x00, dest, self.SOURCE_PC))

def main():
    print("========================================")
    print(" pscan - Precision Stage Controller")
    print("========================================")
    
    stage = SimpleStage()
    try:
        stage.connect()
    except Exception as e:
        print(f"[ERROR] Could not open serial port: {e}")
        sys.exit(1)

    counts_per_mm = 34304.0
    step_mm = 0.5 
    ch1_enabled = True
    ch2_enabled = True
        
    print("\nControls:")
    print("  [UP]    : Move Y Forward")
    print("  [DOWN]  : Move Y Reverse")
    print("  [RIGHT] : Move X Forward")
    print("  [LEFT]  : Move X Reverse")
    print("  [J]     : Change Step Size")
    print("  [C]     : Centre (Move to 0,0)")
    print("  [H]     : Home Both Axes")
    print("  [1/2]   : Toggle X/Y Enable")
    print("  [Q]     : Quit\n")
    print(f"Current Step Size: {step_mm} mm\n")
    
    while True:
        key = get_key()
        
        if key:
            if key == 'q':
                print("\nExiting pystage...")
                break
            
            # Movement
            elif key == 'up':
                print(f"Moving Y Forward {step_mm}mm   ", end='\r')
                stage.move_rel(axis=2, counts=step_mm * counts_per_mm)
            elif key == 'down':
                print(f"Moving Y Reverse {step_mm}mm   ", end='\r')
                stage.move_rel(axis=2, counts=-step_mm * counts_per_mm)
            elif key == 'right':
                print(f"Moving X Forward {step_mm}mm   ", end='\r')
                stage.move_rel(axis=1, counts=step_mm * counts_per_mm)
            elif key == 'left':
                print(f"Moving X Reverse {step_mm}mm   ", end='\r')
                stage.move_rel(axis=1, counts=-step_mm * counts_per_mm)
                
            # Changing Step Size
            elif key == 'j':
                try:
                    new_step = input(f"\n[Enter new step size in mm (Current: {step_mm})]: ")
                    step_mm = float(new_step)
                    print(f"Step size updated to: {step_mm} mm\n")
                except ValueError:
                    print(f"Invalid input. Keeping step size at {step_mm} mm\n")
                    
            # Centre Absolute (0,0)
            elif key == 'c':
                print("\nCentring to Absolute (0,0)...")
                stage.move_abs(axis=1, counts=0)
                stage.move_abs(axis=2, counts=0)
                
            # Home
            elif key == 'h':
                print("\nHoming Both Axes...")
                stage.home_axis(axis=1)
                stage.home_axis(axis=2)
                
            # Enable/Disable Toggles
            elif key == '1':
                ch1_enabled = not ch1_enabled
                stage.set_enable(1, ch1_enabled)
                print(f"\nChannel 1 (X) is now {'ENABLED' if ch1_enabled else 'DISABLED'}")
            elif key == '2':
                ch2_enabled = not ch2_enabled
                stage.set_enable(2, ch2_enabled)
                print(f"\nChannel 2 (Y) is now {'ENABLED' if ch2_enabled else 'DISABLED'}")

        # Tiny sleep to prevent CPU spike during while True loop
        time.sleep(0.01)

if __name__ == "__main__":
    main()
