import vxi11
import os
import re
import time

def test_set_scope_sweeps(ip_address="192.168.74.2", test_target=2500):
    print(f"--- 1. Connecting to LeCroy at {ip_address} ---")
    try:
        instr = vxi11.Instrument(ip_address)
        instr.timeout = 5
        # Ensure headers are ON for the DEF? query so we get the full string
        instr.write("CHDR SHORT") 
    except Exception as e:
        print(f"FATAL: Connection failed: {e}")
        return

    print("\n--- 2. Querying Current Math Sweeps ---")
    instr.write("F1:DEF?")
    scpi_resp = instr.read().strip()
    print(f"Raw SCPI Response: '{scpi_resp}'")

    # Extract current sweeps
    match = re.search(r'SWEEPS\s*,\s*(\d+)', scpi_resp, re.IGNORECASE)
    if match:
        current_sweeps = int(match.group(1))
        print(f"-> Currently set to: {current_sweeps} sweeps")
    else:
        print("-> FAILED: Could not find 'SWEEPS,[number]'. Exiting.")
        instr.close()
        return

    print(f"\n--- 3. Attempting to Set Sweeps to {test_target} ---")
    # Surgically replace the old number with the new target using regex
    new_scpi_cmd = re.sub(r'(SWEEPS\s*,\s*)\d+', f'\\g<1>{test_target}', scpi_resp, flags=re.IGNORECASE)
    
    # CHDR SHORT prepends 'F1:DEF ' to the read response, which makes it a perfectly valid write command!
    if not new_scpi_cmd.upper().startswith("F1:DEF"):
        new_scpi_cmd = f"F1:DEF {new_scpi_cmd}"
        
    print(f"Sending Command: '{new_scpi_cmd}'")
    instr.write(new_scpi_cmd)
    
    # Brief pause to let the scope's processor apply the change
    time.sleep(0.5)

    print("\n--- 4. Verifying the Change ---")
    instr.write("F1:DEF?")
    verify_resp = instr.read().strip()
    print(f"New Raw Response: '{verify_resp}'")
    
    verify_match = re.search(r'SWEEPS\s*,\s*(\d+)', verify_resp, re.IGNORECASE)
    if verify_match:
        verified_sweeps = int(verify_match.group(1))
        if verified_sweeps == test_target:
            print(f"-> SUCCESS! Scope physically updated to {verified_sweeps} sweeps.")
        else:
            print(f"-> FAILED. Scope refused the change. Still stuck at {verified_sweeps}.")
    
    # Return the scope to standard headerless comms
    instr.write("CHDR OFF")
    instr.close()

if __name__ == "__main__":
    test_set_scope_sweeps()
