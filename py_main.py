import sys
import os
import re
import time
import subprocess
import cv2
import shutil

from lecroy_driver import LeCroyScope
from a2d_driver import NIDriver
from stage_driver import ThorlabsStage

def generate_positions(start, stop, step):
    pos = []
    curr = start
    eps = abs(step) * 0.01
    while curr <= stop + eps:
        pos.append(round(curr, 6))
        curr += step
    return pos

def parse_flat_con_file(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
        
    all_blocks = []
    current_block = None
    block_body = []
    
    for line in lines:
        cleaned = line.strip()
        if not cleaned or cleaned.startswith('#'):
            continue
            
        if cleaned.startswith('action '):
            if current_block:
                all_blocks.append({
                    'type': current_block, 
                    'body': "\n".join(block_body)
                })
            current_block = cleaned.split()[1].lower()
            block_body = [cleaned]
            
        elif cleaned == 'end':
            if current_block:
                block_body.append(cleaned)
                all_blocks.append({
                    'type': current_block, 
                    'body': "\n".join(block_body)
                })
                current_block = None
                block_body = []
        else:
            if current_block:
                block_body.append(cleaned)
                
    if current_block:
        all_blocks.append({
            'type': current_block, 
            'body': "\n".join(block_body)
        })
        
    return all_blocks

def categorize_pipeline(all_blocks):
    core_types = ['apt_stage', 'count', 'scope', 'a2d', 'webcam', 'delay']
    core_indices = [
        i for i, b in enumerate(all_blocks) if b['type'] in core_types
    ]
    
    first_core = core_indices[0] if core_indices else len(all_blocks)
    last_core = core_indices[-1] if core_indices else -1
    
    stages, setup_pipe, acq_pipe, tear_pipe = [], [], [], []
    count_val = 1
    
    for i, block in enumerate(all_blocks):
        if block['type'] == 'apt_stage':
            stages.append(block['body'])
            continue
        if block['type'] == 'count':
            m = re.search(r'count\s+(\d+)', block['body'])
            if m: 
                count_val = int(m.group(1))
            continue
            
        if i < first_core:
            setup_pipe.append(block)
        elif i <= last_core:
            acq_pipe.append(block)
        else:
            tear_pipe.append(block)
            
    return stages, count_val, setup_pipe, acq_pipe, tear_pipe

def compile_pipeline_list(raw_pipeline, a2d_counter):
    compiled = []
    for act in raw_pipeline:
        body = act['body']
        t = act['type']
        p = {}
        
        if t == 'scope':
            ip_match = re.search(r'ip\s+([\d\.]+)', body)
            ch_match = re.search(r'channels\s+([^\n]+)', body)
            swp_match = re.search(r'(?:sweeps|segments|averages)\s+(\d+)', body)
            
            p['ip'] = ip_match.group(1) if ip_match else None
            p['sweeps'] = int(swp_match.group(1)) if swp_match else None
            
            if ch_match:
                p['channels'] = [
                    ch.strip() for ch in 
                    ch_match.group(1).replace(',', ' ').split() 
                    if ch.strip()
                ]
            else:
                p['channels'] = []
            
        elif t == 'a2d':
            ch_m = re.search(r'channel\s+(\d+)', body)
            rg_m = re.search(r'range\s+(\d+)', body)
            sr_m = re.search(r'sample_rate\s+(\d+)', body)
            ns_m = re.search(r'n_samples\s+(\d+)', body)
            
            p['channel'] = int(ch_m.group(1)) if ch_m else 0
            p['range'] = int(rg_m.group(1)) if rg_m else 0
            p['sample_rate'] = int(sr_m.group(1)) if sr_m else 10000
            p['n_samples'] = int(ns_m.group(1)) if ns_m else 100
            p['legacy_index'] = a2d_counter[0]
            a2d_counter[0] += 1
            
        elif t == 'script':
            match = re.search(r'^\s*script\s+([^\n]+)', body, re.MULTILINE)
            p['cmd'] = match.group(1).strip() if match else ""
            
        elif t == 'webcam':
            dev_m = re.search(r'device\s+(\d+)', body)
            p['device'] = int(dev_m.group(1)) if dev_m else 0
            
        elif t == 'delay':
            del_m = re.search(r'(?:duration|delay|time)\s+([\d\.]+)', body)
            p['seconds'] = float(del_m.group(1)) if del_m else 1.0
            
        compiled.append({'type': t, 'params': p})
    return compiled

def compile_stage_parameters(stage_strings):
    stage_params = {}
    for body in stage_strings:
        axis_match = re.search(r'axis\s+(\d+)', body)
        scan_match = re.search(
            r'scan\s+([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)', body
        )
        
        if axis_match and scan_match:
            ax = int(axis_match.group(1))
            start = float(scan_match.group(1))
            stop = float(scan_match.group(2))
            step = float(scan_match.group(3))
            
            stage_params[ax] = {
                'positions': generate_positions(start, stop, step),
                'restore': 'restore' in body,
                'save': 'save' in body,
                'start_pos': start
            }
    return stage_params

def run_pipeline_sequence(pipeline, scope, ni_daq, base_filename, 
                          total_a2d_blocks, global_total_traces, 
                          call_state, silent_acq=False):
    is_first = (call_state['current_trace_idx'] == 0)
    
    for step_idx, action in enumerate(pipeline):
        act_type = action['type']
        p = action['params']
        
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
                f"{base_filename}_count{global_total_traces}_"
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
            output_name = f"{base_filename}_count{global_total_traces}_scope0"
            scope.acquire_multi_channel(
                channels=p['channels'], output_base_name=output_name,
                save_for_matlab=True, is_first_trace=is_first, 
                total_loops=global_total_traces,
                sweeps=p.get('sweeps')  # Dynamically pass sweep count to driver
            )
            
        elif act_type == 'a2d':
            idx_num = p['legacy_index']
            output_name = (
                f"{base_filename}_count{global_total_traces}_"
                f"a2d{idx_num}_{total_a2d_blocks}f.d"
            )
            ni_daq.acquire_a2d(
                channel=p['channel'], range_val=p['range'], 
                n_samples=p['n_samples'], sample_rate=p['sample_rate'], 
                output_filename=output_name, is_first_trace=is_first
            )
            
            if (step_idx == len(pipeline) - 1 or 
                pipeline[step_idx+1]['type'] != 'scope'):
                call_state['current_trace_idx'] += 1

def main():
    name_was_changed = False

    if len(sys.argv) < 2:
        print("Usage: python3 py_main.py <path_to_config.con>")
        sys.exit(1)

    con_filepath = sys.argv[1]
    if not os.path.exists(con_filepath):
        print(f"Error: File '{con_filepath}' not found.")
        sys.exit(1)

    original_base = os.path.splitext(os.path.basename(con_filepath))[0]
    base_filename = original_base
    
    all_blocks = parse_flat_con_file(con_filepath)
    stages_str, count_val, r_set, r_acq, r_tear = categorize_pipeline(
        all_blocks
    )
    
    a2d_global_counter = [1]
    stage_params = compile_stage_parameters(stages_str)
    setup_pipeline = compile_pipeline_list(r_set, a2d_global_counter)
    acquisition_pipeline = compile_pipeline_list(r_acq, a2d_global_counter)
    teardown_pipeline = compile_pipeline_list(r_tear, a2d_global_counter)
    
    axis_0_pos = stage_params.get(0, {}).get('positions', [None]) 
    axis_1_pos = stage_params.get(1, {}).get('positions', [None]) 
    
    total_a2d_blocks = sum(
        1 for act in acquisition_pipeline if act['type'] == 'a2d'
    )
    global_total_traces = len(axis_0_pos) * len(axis_1_pos) * count_val
    
    scope_ip = next(
        (act['params']['ip'] for act in acquisition_pipeline 
         if act['type'] == 'scope'), None
    )
    counts_per_mm = 34304.0 

    while True:
        scope_target = (
            f"{base_filename}_count{global_total_traces}_scope0.dat"
        )
        a2d_target = (
            f"{base_filename}_count{global_total_traces}_"
            f"a2d1_{total_a2d_blocks}f.d"
        )
        
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
                    base_filename = os.path.splitext(
                        os.path.basename(new_base)
                    )[0]
                    name_was_changed = True
                continue
        else:
            break

    if name_was_changed:
        shutil.copy(con_filepath, f"{base_filename}.con")

    scope = LeCroyScope(scope_ip) if scope_ip else None
    ni_daq = NIDriver()
    stage = ThorlabsStage() if stage_params else None

    if scope:
        print(f"\nOpening session to LeCroy at {scope_ip}...")
        scope.connect()
    if stage:
        stage.connect()

    print(f"\nGrid: {len(axis_0_pos)} X steps x {len(axis_1_pos)} Y steps.")
    print(f"Total Traces Target: {global_total_traces}")
    
    if setup_pipeline:
        print("\nExecuting Pre-Scan Initialization Commands...")
        run_pipeline_sequence(
            setup_pipeline, scope, ni_daq, base_filename, 
            total_a2d_blocks, global_total_traces, {'current_trace_idx': 0}
        )

    print("\nExecuting Data Acquisition Sequence...")
    call_state = {'current_trace_idx': 0}
    
    for x_val in axis_0_pos:
        if x_val is not None and stage_params.get(0):
            stage.move_absolute('x', x_val, counts_per_mm)
            
        for y_val in axis_1_pos:
            if y_val is not None and stage_params.get(1):
                stage.move_absolute('y', y_val, counts_per_mm)
                
            if x_val is not None or y_val is not None:
                settle_time = 1.5 if call_state['current_trace_idx'] == 0 else 0.2
                time.sleep(settle_time)
                
            for c_val in range(count_val):
                current_num = call_state['current_trace_idx'] + 1
                x_str = f"{x_val:7.4f} mm" if x_val is not None else "Static"
                y_str = f"{y_val:7.4f} mm" if y_val is not None else "Static"
                
                print(
                    f" -> Position: X = {x_str}, Y = {y_str} "
                    f"| Loop {c_val+1}/{count_val} "
                    f"| Trace {current_num}/{global_total_traces}..."
                )
                
                run_pipeline_sequence(
                    acquisition_pipeline, scope, ni_daq, base_filename, 
                    total_a2d_blocks, global_total_traces, call_state, 
                    silent_acq=True
                )
            
        if 1 in stage_params and stage_params[1]['restore']:
            y_start = stage_params[1]['start_pos']
            stage.move_absolute('y', y_start, counts_per_mm)

    if 0 in stage_params and stage_params[0]['restore']:
        x_start = stage_params[0]['start_pos']
        stage.move_absolute('x', x_start, counts_per_mm)

    if teardown_pipeline:
        print("\nExecuting Post-Scan Teardown Commands...")
        run_pipeline_sequence(
            teardown_pipeline, scope, ni_daq, base_filename, 
            total_a2d_blocks, global_total_traces, call_state
        )

    if scope: scope.disconnect()
    if stage: stage.disconnect()
    print("\nSequence Complete!")

if __name__ == "__main__":
    main()
