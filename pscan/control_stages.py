import sys
import os
import struct
import time
import serial
import serial.tools.list_ports

# ==========================================
# 1. Simple Blocking Keystroke Reader
# ==========================================
if os.name == 'nt':
    import msvcrt
    def get_key():
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key in (b'\xe0', b'\x00'): 
                return key + msvcrt.getch()
            return key.decode('utf-8', 'ignore')
        return None
else:
    import select
    import tty
    import termios
    def get_key():
        if select.select([sys.stdin], [], [], 0)[0]:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    ch += sys.stdin.read(2)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch
        return None

# ==========================================
# 2. The Fixed Stage Driver
# ==========================================
class SimpleStage:
    def __init__(self):
        self.ser = None
        self.scale = 34304.0 

    def connect(self):
        for p in serial.tools.list_ports.comports():
            if p.manufacturer and ("FTDI" in p.manufacturer or "Thorlabs" in p.manufacturer):
                port = p.device
                break
        else:
            port = 'COM6' if os.name == 'nt' else '/dev/ttyUSB0'
            
        self.ser = serial.Serial(port, 115200, timeout=1.0)
        self.ser.dtr = True; self.ser.rts = True
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        
        self.ser.write(struct.pack('<HBBBB', 0x0018, 0x00, 0x00, 0x50, 0x01))
        time.sleep(0.1)
        self.set_enable(1, True)
        self.set_enable(2, True)
        time.sleep(0.2)
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
        dest = 0x20 + axis_id
        self.ser.write(struct.pack('<HBBBB', 0x0443, 0x01, 0x00, dest, 0x01))

    def get_position(self, axis_id):
        dest = 0x20 + axis_id
        self.ser.reset_input_buffer()
        self.ser.write(struct.pack('<HBBBB', 0x0411, 0x01, 0x00, dest, 0x01))
        
        timeout = time.time() + 0.5
        while time.time() < timeout:
            if self.ser.in_waiting >= 12:
                data = self.ser.read(12)
                if data[0:2] == b'\x12\x04':
                    _, _, _, _, _, _, pos = struct.unpack('<HBBBBHl', data)
                    return pos / self.scale
        return None

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
def main():
    stage = SimpleStage()
    try:
        port = stage.connect()
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    current_step = 0.5 
    msg = "Connected. Use Arrow Keys to jog."

    x_pos = stage.get_position(1) or 0.0
    y_pos = stage.get_position(2) or 0.0
    
    x_enabled = True
    y_enabled = True
    needs_refresh = False
    needs_redraw = True  # <--- New UI flag

    while True:
        # Only clear and redraw the screen if something changed!
        if needs_redraw:
            os.system('cls' if os.name == 'nt' else 'clear')
            x_status = "[ON]" if x_enabled else "[OFF]"
            y_status = "[ON]" if y_enabled else "[OFF]"
            
            print("==================================================")
            print(f" THORLABS PRECISION CONTROL CLI ({port})")
            print("==================================================")
            print(f" X-Axis Position: {x_pos:8.5f} mm  {x_status}")
            print(f" Y-Axis Position: {y_pos:8.5f} mm  {y_status}")
            print(f"\n Current Step Size: {current_step:.5f} mm")
            print("==================================================")
            print("   Up/Down/L/R : Jog Axes   |  J : Set Step Size")
            print("   G           : Go To Abs  |  C : Center (0.0, 0.0)")
            print("   1 / 2       : Toggle X/Y |  R : Refresh Pos")
            print("   H           : Home Stage |  Q : Quit")
            print("==================================================")
            print(f" [MSG] {msg}")
            
            needs_redraw = False  # Reset flag after drawing

        key = get_key()
        
        if key:
            key_str = key.lower() if isinstance(key, str) else ''
            needs_redraw = True  # A key was pressed, we will need to redraw
            
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
                    time.sleep(1.0)
                    needs_refresh = True
                except ValueError:
                    msg = "Invalid coordinates entered. Move cancelled."

            elif key_str == 'r':
                msg = "Positions refreshed directly from hardware."
                needs_refresh = True
                
            elif key_str == 'c':
                if x_enabled: 
                    stage.move_absolute(1, 0.0)
                    time.sleep(0.1) 
                if y_enabled: 
                    stage.move_absolute(2, 0.0)
                msg = "Commanded Center (0.0, 0.0). Moving..."
                time.sleep(2.0)
                needs_refresh = True
                
            elif key_str == 'h':
                msg = "HOMING... Please wait 10 seconds!"
                stage.set_enable(1, True)
                time.sleep(0.1)
                stage.set_enable(2, True)
                time.sleep(0.1)
                x_enabled, y_enabled = True, True
                stage.home_axis(1)
                time.sleep(0.1) 
                stage.home_axis(2)
                time.sleep(10.0)
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

        time.sleep(0.01)

    os.system('cls' if os.name == 'nt' else 'clear')
    print("Stage CLI closed.")

if __name__ == "__main__":
    main()
