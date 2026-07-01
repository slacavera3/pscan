import serial
import serial.tools.list_ports
import struct
import time
import os
import sys

def get_key():
    """Cross-platform non-blocking keypress capture (Arrows & WASD)."""
    if os.name == 'nt':
        import msvcrt
        key = msvcrt.getch()
        if key in (b'\xe0', b'\x00'):
            key = msvcrt.getch()
            if key == b'H': return 'up'
            if key == b'P': return 'down'
            if key == b'K': return 'left'
            if key == b'M': return 'right'
        return key.decode('utf-8', 'ignore').lower()
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                sys.stdin.read(1)
                opt = sys.stdin.read(1)
                if opt == 'A': return 'up'
                if opt == 'B': return 'down'
                if opt == 'D': return 'left'
                if opt == 'C': return 'right'
            return ch.lower()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

class SimpleStage:
    def __init__(self):
        self.ser = None

    def connect(self):
        ports = serial.tools.list_ports.comports()
        port = None
        
        # Priority 1: Genuine Thorlabs hardware
        for p in ports:
            if (p.manufacturer and ("FTDI" in p.manufacturer or "Thorlabs" in p.manufacturer)) or \
               (p.description and ("APT" in p.description or "USB Serial Port" in p.description)):
                port = p.device
                break
                
        # Priority 2: Prolific adapters (if someone hooks it up wrong again)
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
        
        # Init Motherboard & Enable Both Axes
        self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, 0x50, 0x01))
        time.sleep(0.1)
        self.set_enable(1, True)
        self.set_enable(2, True)
        time.sleep(0.2)
        return port

    def set_enable(self, axis, enable):
        dest = 0x21 if axis == 1 else 0x22
        state = 0x01 if enable else 0x02
        self.ser.write(struct.pack('<HBBBB', 0x0210, 0x01, state, dest, 0x01))

    def jog(self, axis, direction):
        # 1 = Forward, 2 = Reverse
        dest = 0x21 if axis == 1 else 0x22
        self.ser.write(struct.pack('<HBBBB', 0x0429, 0x01, direction, dest, 0x01))

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
        
    print("\nControls:")
    print("  [W] or [UP]    : Jog Y Forward")
    print("  [S] or [DOWN]  : Jog Y Reverse")
    print("  [D] or [RIGHT] : Jog X Forward")
    print("  [A] or [LEFT]  : Jog X Reverse")
    print("  [Q]            : Quit")
    
    while True:
        key = get_key()
        
        if key == 'q':
            print("\nExiting pystage...")
            break
        elif key in ('w', 'up'):
            print("Jogging Y Forward", end='\r')
            stage.jog(axis=2, direction=1)
        elif key in ('s', 'down'):
            print("Jogging Y Reverse", end='\r')
            stage.jog(axis=2, direction=2)
        elif key in ('d', 'right'):
            print("Jogging X Forward", end='\r')
            stage.jog(axis=1, direction=1)
        elif key in ('a', 'left'):
            print("Jogging X Reverse", end='\r')
            stage.jog(axis=1, direction=2)

if __name__ == "__main__":
    main()
