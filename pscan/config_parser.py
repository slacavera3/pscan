import re

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
    # Added 'ixon' to the list of core acquisition types
    core_types = ['apt_stage', 'count', 'scope', 'a2d', 'webcam', 'delay', 'ixon']
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
            swp_match = re.search(r'sweeps\s+(\d+)', body)
            
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
            
        elif t == 'ixon':
            exp_m = re.search(r'exposure\s+([\d\.]+)', body)
            gain_m = re.search(r'em_gain\s+(\d+)', body)
            cyc_m = re.search(r'kinetic_cycle\s+([\d\.]+)', body)
            shut_m = re.search(r'shutter_open\s+(\w+)', body)

            p['exposure'] = float(exp_m.group(1)) if exp_m else 0.1
            p['em_gain'] = int(gain_m.group(1)) if gain_m else 72
            p['kinetic_cycle'] = float(cyc_m.group(1)) if cyc_m else 0.5
            p['shutter_open'] = shut_m.group(1).lower() == 'true' if shut_m else True
            
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
