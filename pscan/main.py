import sys
import os
import time
import subprocess
import cv2
import shutil
import numpy as np

# Cross-platform module imports
from pscan.config_parser import (
    parse_flat_con_file, categorize_pipeline, 
    compile_pipeline_list, compile_stage_parameters
)
from pscan.lecroy_driver import LeCroyScope
from pscan.a2d_driver import NIDriver
from pscan.stage_driver import ThorlabsStage

# OS-Specific Andor Import
try:
    if os.name == 'nt':
        from pscan.ixon_driver import IXonCamera
        ANDOR_AVAILABLE = True
    else:
        ANDOR_AVAILABLE = False
except ImportError:
    ANDOR_AVAILABLE = False

def run_pipeline_sequence(pipeline, scope, ni_daq, ixon, base_filename, 
                          total_a2d_blocks, global_total_traces, 
                          call_state, silent_acq=False, timer_mode=False):
    is_first = (call_state['current_trace_idx'] == 0)
    
    for step_idx, action in enumerate(pipeline):
        act_type = action['type']
        p = action['params']
        
        if timer_mode: 
            t_mod_start = time.perf_counter()
        
        if act_type == 'script':
            if not silent_acq: 
                print(f" -> Executing system command: '{p['cmd']}'...")
            subprocess.run(
                p['cmd'], shell=True, check=True, 
                capture_output=True, text=True
            )
            
        elif act_type == 'delay':
            time.sleep(p['seconds'])
            
        elif act_type == 'webcam':
            idx = call_state['current_trace_idx']
            img_filename = (
                f"{base_filename}_"
                f"webcam_trace{idx}.jpg"
            )
            if not silent_acq:
                print(f" -> Grabbing USB frame -> {img_filename}...")
            
            cam = cv2.VideoCapture(p['device'])
            if cam.isOpened():
                time.sleep(0.1)
                ret, frame = cam.read()
                if ret: 
                    cv2.imwrite(img_filename, frame)
                cam.release()
                
        elif act_type == 'scope' and scope:
            output_name = f"{base_filename}_scope0"
            scope.acquire_multi_channel(
                channels=p['channels'], output_base_name=output_name,
                save_for_matlab=True, is_first_trace=is_first, 
                total_loops=global_total_traces,
                sweeps=p.get('sweeps')  
            )
            
        elif act_type == 'a2d':
            idx_num = p['legacy_index']
            output_name = (
                f"{base_filename}_"
                f"a2d{idx_num}_{total_a2d_blocks}f.d"
            )
            ni_daq.acquire_a2d(
                channel=p['channel'], range_val=p['range'], 
                n_samples=p['n_samples'], sample_rate=p['sample_rate'], 
                output_filename=output_name, is_first_trace=is_first
            )
            
        elif act_type == 'ixon' and ixon:
            if not call_state.get('ixon_configured'):
                ixon.setup(
                    exposure=p['exposure'],
                    em_gain=p['em_gain'],
                    kinetic_cycle=p['kinetic_cycle'],
                    shutter_open=p['shutter_open']
                )
                call_state['ixon_configured'] = True

            idx = call_state['current_trace_idx']
            img_filename = (
                f"{base_filename}_"
                f"ixon_trace{idx}.npy"
            )
            
            if not silent_acq:
                print(f" -> Acquiring iXon frame -> {img_filename}...")
                
            frame_data = ixon.acquire()
            if frame_data is not None:
                np.save(img_filename, frame_data)
                
        if timer_mode and silent_acq:
            t_mod_end = time.perf_counter()
            print(f"      [Timer] {act_type.upper()} module : {t_mod_end - t_mod_start:.4f}s")

def main():
    name_was_changed = False  # IT LIVES HERE NOW. NO MORE GHOST ERRORS.
    args = sys.argv[1:]
    timer_mode = False
    
    if '--timer' in args:
        timer_mode = True
        args.remove('--timer')

    if len(args) < 1:
        print("Usage: pscan [--timer] <path_to_config.con>")
        sys.exit(1)

    con_filepath = args[0]
    if not os.path.exists(con_filepath):
        print(f"Error: File '{con_filepath}' not found.")
        sys.exit(1)

    original_base = os.path.splitext(os.path.basename(con_filepath))[0]
    base_filename = original_base
    
    all_blocks = parse_flat_con_file(con_filepath)
    stages_str, count_val, r_set, r_acq, r_tear = categorize_pipeline(all_blocks)
    
    a2d_global_counter = [1]
    stage_params = compile_stage_parameters(stages_str)
    setup_pipeline = compile_pipeline_list(r_set, a2d_global_counter)
    acquisition_pipeline = compile_pipeline_list(r_acq, a2d_global_counter)
    teardown_pipeline = compile_pipeline_list(r_tear, a2d_global_counter)
    
    axis_0_pos = stage_params.get(0, {}).get('positions', [None]) 
    axis_1_pos = stage_params.get(1, {}).get('positions', [None]) 
    
    total_a2d_blocks = sum(1 for act in acquisition_pipeline if act['type'] == 'a2d')
    global_total_traces = len(axis_0_pos) * len(axis_1_pos) * count_val
    
    scope_ip = next((act['params']['ip'] for act in acquisition_pipeline if act['type'] == 'scope'), None)
    ixon_in_pipeline = any(act['type'] == 'ixon' for act in acquisition_pipeline + setup_pipeline + teardown_pipeline)
    
    counts_per_mm = 34304.0 

    while True:
        scope_target = f"{base_filename}_scope0.dat"
        a2d_target = f"{base_filename}_a2d1_{total_a2d_blocks}f.d"
        
        scope_exists = scope_ip and os.path.exists(scope_target)
        a2d_exists = total_a2d_blocks > 0 and os.path.exists(a2d_target)
        has_collision = scope_exists or a2d_exists
        
        if has_collision:
            print(f"\n[WARNING] Outputs for '{base_filename}' exist!")
            msg = " -> Overwrite files (o) or Change base filename (c)? "
            choice = input(msg).strip().lower()
            if choice == 'o': 
                break
            elif choice == 'c':
                new_base = input(" -> Enter new base filename: ").strip()
                if new_base: 
                    base_filename = os.path.splitext(os.path.basename(new_base))[0]
                    name_was_changed = True
                continue
        else:
            break

    if name_was_changed:
        shutil.copy(con_filepath, f"{base_filename}.con")

    scope = LeCroyScope(scope_ip) if scope_ip else None
    ni_daq = NIDriver()
    stage = ThorlabsStage() if stage_params else None
    ixon = None

    if scope:
        print(f"\nOpening session to LeCroy at {scope_ip}...")
        scope.connect()
    if total_a2d_blocks > 0:
        print("Connecting to NI A2D card...")
        ni_daq.connect()
    if stage:
        stage.connect()
    if ixon_in_pipeline and ANDOR_AVAILABLE:
        print("\nConnecting to Windows iXon Camera...")
        ixon = IXonCamera()
        ixon.connect()

    print(f"\nGrid: {len(axis_0_pos)} X steps x {len(axis_1_pos)} Y steps.")
    print(f"Total Traces Target: {global_total_traces}")
    
    call_state = {'current_trace_idx': 0, 'ixon_configured': False}
    
    try:
        if setup_pipeline:
            print("\nExecuting Pre-Scan Initialization Commands...")
            run_pipeline_sequence(
                setup_pipeline, scope, ni_daq, ixon, base_filename, 
                total_a2d_blocks, global_total_traces, call_state, 
                timer_mode=timer_mode
            )

        print("\nExecuting Data Acquisition Sequence...")
        
        if scope_ip:
            scope_act = next((act for act in acquisition_pipeline if act['type'] == 'scope'), None)
            if scope_act and scope_act['params'].get('sweeps'):
                print(f" -> Scope Hardware Target: {scope_act['params']['sweeps']} Averages\n")
                
        master_trace_idx = 0
        
        for x_val in axis_0_pos:
            if x_val is not None and stage_params.get(0):
                stage.move_absolute('x', x_val, counts_per_mm)
                
            for y_val in axis_1_pos:
                if y_val is not None and stage_params.get(1):
                    stage.move_absolute('y', y_val, counts_per_mm)
                    
                if x_val is not None or y_val is not None:
                    settle_time = 1.5 if master_trace_idx == 0 else 0.2
                    time.sleep(settle_time)
                    
                for c_val in range(count_val):
                    if timer_mode:
                        t_trace_start = time.perf_counter()
                    
                    call_state['current_trace_idx'] = master_trace_idx
                    current_num = master_trace_idx + 1
                    
                    x_str = f"{x_val:7.4f} mm" if x_val is not None else "Static"
                    y_str = f"{y_val:7.4f} mm" if y_val is not None else "Static"
                    
                    print(
                        f" -> Position: X = {x_str}, Y = {y_str} "
                        f"| Trace {current_num}/{global_total_traces}..."
                    )
                    
                    run_pipeline_sequence(
                        acquisition_pipeline, scope, ni_daq, ixon, base_filename, 
                        total_a2d_blocks, global_total_traces, call_state, 
                        silent_acq=True, timer_mode=timer_mode
                    )
                    
                    if timer_mode:
                        t_trace_end = time.perf_counter()
                        print(f"      [Timer] Total trace time: {t_trace_end - t_trace_start:.4f}s\n")
                    
                    master_trace_idx += 1
                
            if 1 in stage_params and stage_params[1]['restore']:
                y_start = stage_params[1]['start_pos']
                stage.move_absolute('y', y_start, counts_per_mm)

        if 0 in stage_params and stage_params[0]['restore']:
            x_start = stage_params[0]['start_pos']
            stage.move_absolute('x', x_start, counts_per_mm)

        if teardown_pipeline:
            print("\nExecuting Post-Scan Teardown Commands...")
            run_pipeline_sequence(
                teardown_pipeline, scope, ni_daq, ixon, base_filename, 
                total_a2d_blocks, global_total_traces, call_state, 
                timer_mode=timer_mode
            )

    except KeyboardInterrupt:
        print("\n\n[WARNING] Sequence Aborted by User (Ctrl+C)!")
    except Exception as e:
        print(f"\n\n[ERROR] Sequence Failed: {e}")
        
    finally:
        print("\nExecuting Hardware Disconnects & Saving Files...")
        if scope: scope.disconnect()
        if stage: stage.disconnect()
        if ixon: ixon.shutdown()
        if total_a2d_blocks > 0: ni_daq.disconnect()
        
        print("Sequence Complete!")

if __name__ == "__main__":
    main()
