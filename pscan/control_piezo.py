import ctypes
import sys
import argparse

# --- Piezo Hardware Constants ---
MIN_VOLTS = 0.0
MAX_VOLTS = 5.0
UM_PER_VOLT = 20.0

MAX_UM = MAX_VOLTS * UM_PER_VOLT
MIN_UM = MIN_VOLTS * UM_PER_VOLT

def get_phidget_api():
    """Loads libphidget21 and configures ctypes signatures."""
    try:
        phidget21 = ctypes.CDLL("libphidget21.so")
    except OSError:
        print("Error: Could not find libphidget21.so. Ensure it is installed and ldconfig is updated.")
        sys.exit(1)

    phidget21.CPhidgetAnalog_create.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    phidget21.CPhidgetAnalog_create.restype = ctypes.c_int
    
    phidget21.CPhidget_open.argtypes = [ctypes.c_void_p, ctypes.c_int]
    phidget21.CPhidget_open.restype = ctypes.c_int
    
    phidget21.CPhidget_waitForAttachment.argtypes = [ctypes.c_void_p, ctypes.c_int]
    phidget21.CPhidget_waitForAttachment.restype = ctypes.c_int
    
    phidget21.CPhidgetAnalog_setVoltage.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_double]
    phidget21.CPhidgetAnalog_setVoltage.restype = ctypes.c_int

    phidget21.CPhidgetAnalog_getVoltage.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_double)]
    phidget21.CPhidgetAnalog_getVoltage.restype = ctypes.c_int

    return phidget21

def connect_to_board(phidget21):
    """Creates and opens a connection to the Phidget Analog board."""
    handle = ctypes.c_void_p()
    if phidget21.CPhidgetAnalog_create(ctypes.byref(handle)) != 0:
        raise RuntimeError("Failed to create Phidget handle.")
        
    if phidget21.CPhidget_open(handle, -1) != 0:
        raise RuntimeError("Failed to open Phidget connection.")
        
    if phidget21.CPhidget_waitForAttachment(handle, 5000) != 0:
        raise RuntimeError("Hardware failed to attach within 5 seconds.")
        
    return handle

def get_current_position_um(phidget21, handle, channel: int) -> float:
    """Queries the board for the current voltage and converts to um."""
    voltage = ctypes.c_double()
    if phidget21.CPhidgetAnalog_getVoltage(handle, channel, ctypes.byref(voltage)) != 0:
        print(f"Warning: Could not read voltage for channel {channel}. Defaulting to 0.0 um.")
        return 0.0
    return voltage.value * UM_PER_VOLT

def set_position_um(phidget21, handle, channel: int, target_um: float):
    """Converts micrometers to volts, clamps to bounds, and updates the board."""
    target_um = max(MIN_UM, min(MAX_UM, target_um))
    target_volts = target_um / UM_PER_VOLT
    
    if phidget21.CPhidgetAnalog_setVoltage(handle, channel, target_volts) != 0:
        raise RuntimeError(f"Failed to set voltage {target_volts}V on channel {channel}.")
    print(f" -> Position set to {target_um:.2f} um")

def main():
    parser = argparse.ArgumentParser(description="Directly control the Phidget Analog Piezo stage.")
    parser.add_argument("position", type=str, nargs='?', help="Target position in um (or use +X/-X to step)")
    parser.add_argument("--channel", type=int, default=0, help="Phidget Analog channel (default: 0)")
    args = parser.parse_args()
    
    phidget21 = get_phidget_api()
    handle = connect_to_board(phidget21)

    if args.position is not None:
        # One-shot command line execution
        current_um = get_current_position_um(phidget21, handle, args.channel)
        
        if args.position.startswith('+') or args.position.startswith('-'):
            target = current_um + float(args.position)
        else:
            target = float(args.position)
            
        set_position_um(phidget21, handle, args.channel, target)
    else:
        # Interactive CLI mode
        print(f"--- Pscan Lab Interactive Piezo Control ---")
        print(f"Hardware bounds: {MIN_UM:.1f} to {MAX_UM:.1f} um")
        print("Commands:")
        print("  [number]    : Set absolute position (e.g., 50)")
        print("  +[number]   : Move forward by step (e.g., +5)")
        print("  -[number]   : Move backward by step (e.g., -5)")
        print("  q or exit   : Quit")
        
        while True:
            current_um = get_current_position_um(phidget21, handle, args.channel)
            try:
                user_input = input(f"\nPiezo [CH {args.channel} | Curr: {current_um:.2f} um] > ").strip().lower()
                
                if user_input in ['q', 'quit', 'exit']:
                    print("Exiting...")
                    break
                if not user_input:
                    continue
                
                if user_input.startswith('+') or user_input.startswith('-'):
                    target = current_um + float(user_input)
                else:
                    target = float(user_input)
                
                set_position_um(phidget21, handle, args.channel, target)
                
            except ValueError:
                print("Invalid input. Please enter a numerical value (e.g., 45, +10, -5).")
            except KeyboardInterrupt:
                print("\nExiting...")
                break

    # We do NOT close the handle here so the voltage persists on the board.

if __name__ == "__main__":
    main()
