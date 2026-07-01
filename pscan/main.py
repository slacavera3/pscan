import sys
import time
import os
import numpy as np

# Safe cross-platform imports
from pscan import stage_driver
from pscan import lecroy_driver

# Linux-only drivers (Windows will safely ignore these)
try:
    from pscan import a2d_driver
except ImportError:
    a2d_driver = None
    print("[SYSTEM] Running on Windows. A2D Comedi drivers disabled.")

# Windows-only drivers (Linux will safely ignore these)
try:
    from pscan import driver_ixon
except ImportError:
    driver_ixon = None
    print("[SYSTEM] pyAndorSDK2 not found. iXon driver disabled on this OS.")

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

    # Initialize iXon if running on the Windows Machine
    ixon = None
    ixon_configured = False
    if driver_ixon and os.name == 'nt':
        try:
            ixon = driver_ixon.IXonCamera()
            ixon.connect()
        except Exception as e:
            print(f"[WARNING] iXon hardware not found: {e}")

    print("Starting automated acquisition pipeline...")
    
    # Core Orchestration Loop
    trace_idx = 0
    for action in actions:
        
        # Dynamic settling logic replacing the hardcoded sleep
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
            
        elif action['type'] == 'ixon':
            if ixon:
                # Only run the heavy setup function on the very first pixel
                if not ixon_configured:
                    ixon.setup(
                        exposure=float(action.get('exposure', 0.1)),
                        em_gain=int(action.get('em_gain', 72)),
                        kinetic_cycle=float(action.get('kinetic_cycle', 0.5)),
                        shutter_open=action.get('shutter_open', 'True').lower() == 'true'
                    )
                    ixon_configured = True
                
                print(f"Acquiring iXon Frame {trace_idx}...")
                frame_data = ixon.acquire()
                
                if frame_data is not None:
                    # Save array to disk using a generic naming convention
                    np.save(f"ixon_data_{trace_idx:05d}.npy", frame_data)
                
                trace_idx += 1

    # Safe Shutdown
    if ixon:
        ixon.shutdown()
        
    print("Pipeline complete.")

if __name__ == "__main__":
    main()
