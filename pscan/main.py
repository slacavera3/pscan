import sys
import time
import os

# Safe cross-platform imports
from pscan import stage_driver
from pscan import lecroy_driver

# Linux-only drivers (Windows will safely ignore these)
try:
    from pscan import a2d_driver
except ImportError:
    a2d_driver = None
    print("[SYSTEM] Running on Windows. A2D Comedi drivers disabled.")

def parse_config(filepath):
    """Parses the .con file into a dictionary of actions."""
    print(f"Loading configuration: {filepath}")
    # (Your existing .con parsing logic goes here, preserving 'segments' for the scope)
    # Returning a dummy structure for demonstration of the main loop
    return [{'type': 'count', 'val': 1}]

def main():
    if len(sys.argv) < 2:
        print("Usage: pscan <config.con>")
        sys.exit(1)

    config_path = sys.argv[1]
    actions = parse_config(config_path)
    
    # Initialize hardware connections
    stage = stage_driver.ThorlabsStage()
    stage.connect()
    counts_per_mm = 34304.0

    print("Starting automated acquisition pipeline...")
    
    # Core Orchestration Loop
    for action in actions:
        # Example of the dynamic settling logic replacing the hardcoded sleep
        if action['type'] == 'apt_stage':
            x_val = action.get('x_target')
            y_val = action.get('y_target')
            
            if x_val is not None or y_val is not None:
                if stage:
                    # Dynamically wait for axes to hit their exact targets
                    if x_val is not None:
                        stage.wait_to_settle('x', x_val, counts_per_mm)
                    if y_val is not None:
                        stage.wait_to_settle('y', y_val, counts_per_mm)
                else:
                    # Fallback if running without hardware
                    time.sleep(0.2)
                    
        elif action['type'] == 'a2d':
            if a2d_driver is None:
                print("[WARNING] Skipping A2D action (Not supported on Windows)")
            else:
                print("Triggering Comedi A2D...")
                # a2d_driver.acquire(...)

        elif action['type'] == 'scope':
            print(f"Triggering LeCroy (Segments: {action.get('segments', 1)})...")
            # lecroy_driver.acquire(...)

    print("Pipeline complete.")

if __name__ == "__main__":
    main()
