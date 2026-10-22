import re

# Keys each action block understands. Anything else is a typo - which used to
# fall straight through to a silent default, so a misspelled 'sweep' left the
# scope on whatever it happened to be set to and nothing said a word.
#
# 'one_of' means at least one of those keys must be present.
ACTION_SCHEMAS = {
    'count': {
        'required': ['count'],
        'optional': [],
    },
    'apt_stage': {
        'required': ['axis', 'scan'],
        'optional': ['restore', 'save', 'limits'],
    },
    'd2a_phidget': {
        'required': [],
        'optional': ['scan', 'position', 'limits'],
        'one_of': ['scan', 'position'],
    },
    'scope': {
        'required': ['ip', 'channels'],
        'optional': ['sweeps', 'segments', 'averages'],
    },
    'a2d': {
        'required': ['channel'],
        'optional': ['range', 'sample_rate', 'n_samples'],
    },
    'webcam': {
        'required': [],
        'optional': ['device'],
    },
    'delay': {
        'required': [],
        'optional': ['time', 'duration', 'delay'],
        'one_of': ['time', 'duration', 'delay'],
    },
    'ixon': {
        'required': [],
        'optional': ['exposure', 'gain', 'em_gain', 'kinetic_cycle',
                     'shutter_open', 'temperature'],
    },
    'tucsen': {
        'required': [],
        'optional': ['exposure', 'gain_mode', 'bit_depth', 'is_master'],
    },
    'script': {
        'required': ['script'],
        'optional': [],
    },
}

# Blocks that anchor the setup / acquisition / teardown split. See
# categorize_pipeline().
CORE_TYPES = ['apt_stage', 'd2a_phidget', 'count', 'scope', 'a2d', 'webcam',
              'delay', 'ixon', 'tucsen']

# Waverunner trace names: input channels, maths functions, memories and the
# four zoom traces.
CHANNEL_RE = re.compile(r'^(?:C\d+|F\d+|M\d+|Z\d+|T[A-D])$', re.IGNORECASE)

# The APT axes main() actually iterates. The piezo is a d2a_phidget block,
# which lands on virtual axis 99.
APT_AXES = (0, 1)
PIEZO_AXIS = 99


def generate_positions(start, stop, step):
    """Build the coordinate list for a 'scan <start> <stop> <step>' line.

    Positions are computed as start + i*step rather than by repeatedly adding
    step, so the error does not accumulate over a long axis. A step of zero,
    or one whose sign points away from stop, raises instead of looping
    forever.
    """
    if step == 0:
        raise ValueError(
            f"scan step cannot be zero (start={start}, stop={stop})"
        )

    span = stop - start
    if span != 0 and (span > 0) != (step > 0):
        raise ValueError(
            f"scan step {step} points away from stop "
            f"(start={start}, stop={stop}); flip the sign of the step"
        )

    # Tolerate a step that divides the span to within 1%, so a range written
    # as 53.555 53.565 0.005 yields 3 points and not 2.
    eps = abs(step) * 0.01
    n_steps = int((abs(span) + eps) // abs(step))
    return [round(start + i * step, 6) for i in range(n_steps + 1)]


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


def _field(body, key, value_pattern=r'([^\n]+)'):
    """Match 'key <value>' as a whole line.

    Every field lookup goes through here so a key cannot match inside a
    longer one - the old unanchored searches meant 'gain' matched inside
    'em_gain', which happened to be wanted, while 'exposure' would equally
    have matched inside anything ending in that word.
    """
    # The separator is spaces and tabs only, never \s - \s matches newlines,
    # so a key left without a value would swallow the following line as its
    # value ('ip' on its own would come back as 'channels C1').
    return re.search(
        rf'^[ \t]*(?:{key})[ \t]+{value_pattern}',
        body, re.MULTILINE | re.IGNORECASE
    )


def _flag(body, key):
    """True when `key` appears on a line of its own, as a bare flag."""
    return re.search(
        rf'^[ \t]*{key}[ \t]*$', body, re.MULTILINE | re.IGNORECASE
    ) is not None


def _block_keys(body):
    """The set of keys a block declares, taken from each line's first token."""
    keys = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.lower() == 'end' or line.lower().startswith('action '):
            continue
        keys.add(line.split()[0].lower())
    return keys


def _require_number(body, key, label, cast=float):
    match = _field(body, key, r'(\S+)')
    if match is None:
        raise ValueError(f"{label} has no readable '{key}' value")
    try:
        return cast(match.group(1))
    except ValueError:
        raise ValueError(
            f"{label} '{key} {match.group(1)}' is not a number"
        ) from None


def _parse_limits(body, label, unit):
    """Read an optional 'limits <min> <max>' line."""
    match = _field(body, 'limits', r'(\S+)\s+(\S+)')
    if match is None:
        return None

    try:
        low, high = float(match.group(1)), float(match.group(2))
    except ValueError:
        raise ValueError(
            f"{label} 'limits' needs two numbers: limits <min> <max>"
        ) from None

    if low >= high:
        raise ValueError(
            f"{label} 'limits {low} {high}' is not a range - "
            f"min must be below max ({unit})"
        )
    return low, high


def _check_within_limits(positions, limits, label, unit):
    if limits is None:
        return
    low, high = limits
    outside = [p for p in positions if p < low or p > high]
    if outside:
        raise ValueError(
            f"{label}: {len(outside)} of {len(positions)} position(s) fall "
            f"outside limits {low}..{high} {unit} (first is {outside[0]}). "
            f"Widen 'limits' or narrow 'scan'."
        )


def validate_blocks(all_blocks):
    """Check parsed blocks before any hardware is opened.

    Raises ValueError naming the offending block and key. Returns a list of
    warning strings for things that are suspicious but not worth refusing to
    run over.
    """
    if not all_blocks:
        raise ValueError(
            "no 'action' blocks found - the file is empty, fully commented "
            "out, or uses a syntax this parser does not recognise"
        )

    warnings = []
    seen_axes = {}
    seen_piezo = False

    for block in all_blocks:
        t = block['type']
        body = block['body']

        if t not in ACTION_SCHEMAS:
            raise ValueError(
                f"unknown action type 'action {t}'. Known types: "
                f"{', '.join(sorted(ACTION_SCHEMAS))}"
            )

        label = f"'action {t}'"
        schema = ACTION_SCHEMAS[t]
        allowed = set(schema['required']) | set(schema['optional'])
        present = _block_keys(body)

        # A friendlier message than the generic unknown-key one, because
        # apt_stage silently dropped a block written this way.
        if t == 'apt_stage' and 'position' in present:
            raise ValueError(
                f"{label} has no 'position' key - use "
                f"'scan <start> <stop> <step>'. ('position' belongs to "
                f"'action d2a_phidget'.)"
            )

        unknown = sorted(present - allowed)
        if unknown:
            raise ValueError(
                f"{label} does not understand the key '{unknown[0]}'. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )

        for key in schema['required']:
            if key not in present:
                raise ValueError(f"{label} is missing its required '{key}' line")

        one_of = schema.get('one_of')
        if one_of and not (present & set(one_of)):
            raise ValueError(
                f"{label} needs one of: {', '.join(one_of)}"
            )

        if t == 'count':
            count = _require_number(body, 'count', label, int)
            if count < 1:
                raise ValueError(f"{label} count must be at least 1, not {count}")

        elif t == 'apt_stage':
            axis = _require_number(body, 'axis', label, int)
            if axis not in APT_AXES:
                raise ValueError(
                    f"{label} axis {axis} is not a stage axis. Use axis 0 (X) "
                    f"or 1 (Y); the piezo is 'action d2a_phidget' and lands on "
                    f"axis {PIEZO_AXIS}."
                )
            if axis in seen_axes:
                raise ValueError(
                    f"axis {axis} is declared by more than one {label} block"
                )
            seen_axes[axis] = True

            scan = _field(body, 'scan', r'(\S+)\s+(\S+)\s+(\S+)')
            if scan is None:
                raise ValueError(
                    f"{label} scan line needs three numbers: "
                    f"scan <start_mm> <stop_mm> <step_mm>"
                )
            try:
                start, stop, step = (float(v) for v in scan.groups())
            except ValueError:
                raise ValueError(
                    f"{label} scan values are not all numbers: "
                    f"{' '.join(scan.groups())}"
                ) from None

            # Surfaces a zero or wrong-sign step here, before hardware opens.
            positions = generate_positions(start, stop, step)

            limits = _parse_limits(body, label, 'mm')
            if limits is None:
                warnings.append(
                    f"axis {axis} declares no 'limits <min_mm> <max_mm>', so "
                    f"its {len(positions)} position(s) are not bounds-checked"
                )
            _check_within_limits(positions, limits, f"{label} axis {axis}", 'mm')

        elif t == 'd2a_phidget':
            if seen_piezo:
                raise ValueError(
                    "more than one 'action d2a_phidget' block - "
                    f"they would both land on axis {PIEZO_AXIS}"
                )
            seen_piezo = True

            scan = _field(body, 'scan', r'(\S+)\s+(\S+)\s+(\S+)')
            if scan is not None:
                try:
                    start, stop, step = (float(v) for v in scan.groups())
                except ValueError:
                    raise ValueError(
                        f"{label} scan values are not all numbers: "
                        f"{' '.join(scan.groups())}"
                    ) from None
                positions = generate_positions(start, stop, step)
            else:
                positions = [_require_number(body, 'position', label)]

            _check_within_limits(
                positions, _parse_limits(body, label, 'um'), label, 'um'
            )

        elif t == 'scope':
            ip_match = _field(body, 'ip', r'(\S+)')
            ip = ip_match.group(1) if ip_match else ''
            octets = ip.split('.')
            if len(octets) != 4 or not all(
                o.isdigit() and 0 <= int(o) <= 255 for o in octets
            ):
                raise ValueError(
                    f"{label} ip '{ip}' is not a dotted-quad address"
                )

            ch_match = _field(body, 'channels')
            names = [
                c.strip() for c in ch_match.group(1).replace(',', ' ').split()
                if c.strip()
            ] if ch_match else []
            if not names:
                raise ValueError(f"{label} channels line lists no channels")
            for name in names:
                if not CHANNEL_RE.match(name):
                    raise ValueError(
                        f"{label} channel '{name}' is not a Waverunner trace "
                        f"name. Expected C1-style inputs, F1-style maths, "
                        f"M1-style memories, Z1-style zooms or TA..TD."
                    )

            # Mixing a maths average with a raw channel fails at download time
            # in Sequence mode, because the average collapses the segments.
            has_math = any(n.upper().startswith('F') for n in names)
            has_raw = any(n.upper().startswith('C') for n in names)
            if has_math and has_raw:
                warnings.append(
                    f"{label} mixes maths ({', '.join(n for n in names if n.upper().startswith('F'))}) "
                    f"with raw channels. In Sequence mode a maths average "
                    f"collapses all segments into one, so the channel shapes "
                    f"will not match and the trace will be refused. Use "
                    f"separate scope blocks if Sequence mode is on."
                )

            for alias in ('segments', 'averages'):
                if alias in present:
                    warnings.append(
                        f"{label} key '{alias}' is read as 'sweeps'. Until now "
                        f"only 'sweeps' was ever parsed, so a '{alias}' line "
                        f"had no effect at all - check the value is what you "
                        f"actually want before running."
                    )
            if 'sweeps' in present and (present & {'segments', 'averages'}):
                warnings.append(
                    f"{label} declares both 'sweeps' and an alias; 'sweeps' wins."
                )

        elif t == 'a2d':
            channel = _require_number(body, 'channel', label, int)
            if channel < 0:
                raise ValueError(f"{label} channel cannot be negative")

            if 'range' in present:
                rng = _require_number(body, 'range', label, int)
                if rng < 0:
                    raise ValueError(f"{label} range cannot be negative")
                # The driver asks the card for its own range list, so a high
                # index may well be valid - validation runs before any
                # hardware opens and cannot know. Warn rather than refuse,
                # since the built-in fallback table only covers 0-3.
                if rng > 3:
                    warnings.append(
                        f"{label} range {rng} is outside the built-in "
                        f"fallback table (0=10V, 1=5V, 2=1V, 3=0.2V). It will "
                        f"work if the card reports that range, but if the "
                        f"query fails the conversion falls back to 10V."
                    )
            for key in ('sample_rate', 'n_samples'):
                if key in present:
                    value = _require_number(body, key, label, int)
                    if value < 1:
                        raise ValueError(
                            f"{label} {key} must be positive, not {value}"
                        )

        elif t == 'delay':
            # one_of above guarantees exactly which of these is present.
            key = next(k for k in ('time', 'duration', 'delay') if k in present)
            if _require_number(body, key, label) < 0:
                raise ValueError(f"{label} {key} cannot be negative")

        elif t == 'webcam':
            if 'device' in present:
                _require_number(body, 'device', label, int)

        elif t == 'ixon':
            if 'exposure' in present:
                _require_number(body, 'exposure', label)
            if present & {'gain', 'em_gain'}:
                _require_number(
                    body, 'em_gain' if 'em_gain' in present else 'gain',
                    label, int
                )
            if 'temperature' in present:
                _require_number(body, 'temperature', label, int)
            if 'shutter_open' in present:
                value = _field(body, 'shutter_open', r'(\S+)').group(1).lower()
                if value not in ('true', 'false'):
                    raise ValueError(
                        f"{label} shutter_open must be True or False, "
                        f"not '{value}'"
                    )

        elif t == 'tucsen':
            if 'exposure' in present:
                _require_number(body, 'exposure', label)
            if 'bit_depth' in present:
                depth = _require_number(body, 'bit_depth', label, int)
                if depth not in (8, 16):
                    raise ValueError(
                        f"{label} bit_depth must be 8 or 16, not {depth}"
                    )
            if 'gain_mode' in present:
                mode = _field(body, 'gain_mode', r'(\S+)').group(1).lower()
                if mode not in ('hdr', 'high', 'low'):
                    raise ValueError(
                        f"{label} gain_mode must be hdr, high or low, "
                        f"not '{mode}'"
                    )
            if 'is_master' in present:
                value = _field(body, 'is_master', r'(\S+)').group(1).lower()
                if value not in ('true', 'false'):
                    raise ValueError(
                        f"{label} is_master must be True or False, "
                        f"not '{value}'"
                    )

        elif t == 'script':
            match = _field(body, 'script')
            if match is None or not match.group(1).strip():
                raise ValueError(
                    f"{label} needs a command: 'script <command to run>'"
                )

    return warnings


def categorize_pipeline(all_blocks):
    core_indices = [
        i for i, b in enumerate(all_blocks) if b['type'] in CORE_TYPES
    ]

    first_core = core_indices[0] if core_indices else len(all_blocks)
    last_core = core_indices[-1] if core_indices else -1

    stages, setup_pipe, acq_pipe, tear_pipe = [], [], [], []
    count_val = 1

    for i, block in enumerate(all_blocks):
        if block['type'] in ['apt_stage', 'd2a_phidget']:
            stages.append(block)
            continue
        if block['type'] == 'count':
            m = _field(block['body'], 'count', r'(\d+)')
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
            ip_match = _field(body, 'ip', r'([\d\.]+)')
            ch_match = _field(body, 'channels')

            # 'sweeps' is the real key; the README has long documented
            # 'segments' and 'averages' as alternatives, which were never
            # actually parsed. Accept them, preferring 'sweeps' when both
            # appear. validate_blocks() warns when an alias is used.
            swp_match = (
                _field(body, 'sweeps')
                or _field(body, 'segments|averages')
            )

            p['ip'] = ip_match.group(1) if ip_match else None
            # Keeps it as a string so the scope driver can split it
            p['sweeps'] = swp_match.group(1).strip() if swp_match else None

            if ch_match:
                p['channels'] = [
                    ch.strip() for ch in
                    ch_match.group(1).replace(',', ' ').split()
                    if ch.strip()
                ]
            else:
                p['channels'] = []

        elif t == 'a2d':
            ch_m = _field(body, 'channel', r'(\d+)')
            rg_m = _field(body, 'range', r'(\d+)')
            sr_m = _field(body, 'sample_rate', r'(\d+)')
            ns_m = _field(body, 'n_samples', r'(\d+)')

            p['channel'] = int(ch_m.group(1)) if ch_m else 0
            p['range'] = int(rg_m.group(1)) if rg_m else 0
            p['sample_rate'] = int(sr_m.group(1)) if sr_m else 10000
            p['n_samples'] = int(ns_m.group(1)) if ns_m else 100
            p['legacy_index'] = a2d_counter[0]
            a2d_counter[0] += 1

        elif t == 'script':
            match = _field(body, 'script')
            p['cmd'] = match.group(1).strip() if match else ""

        elif t == 'webcam':
            dev_m = _field(body, 'device', r'(\d+)')
            p['device'] = int(dev_m.group(1)) if dev_m else 0

        elif t == 'delay':
            del_m = _field(body, 'duration|delay|time', r'([\d\.]+)')
            p['seconds'] = float(del_m.group(1)) if del_m else 1.0

        elif t == 'ixon':
            exp_m = _field(body, 'exposure', r'([\d\.]+)')
            # Matches both 'gain' and the README's 'em_gain'.
            gain_m = _field(body, '(?:em_)?gain', r'(\d+)')
            shut_m = _field(body, 'shutter_open', r'(\w+)')
            temp_m = _field(body, 'temperature', r'([\-\d\.]+)')

            p['exposure'] = float(exp_m.group(1)) if exp_m else 0.1
            p['em_gain'] = int(gain_m.group(1)) if gain_m else 1
            p['shutter_open'] = shut_m.group(1).lower() == 'true' if shut_m else True
            p['temperature'] = int(temp_m.group(1)) if temp_m else None

        elif t == 'tucsen':
            exp_m = _field(body, 'exposure', r'([\d\.]+)')
            gain_m = _field(body, 'gain_mode', r'(\w+)')
            depth_m = _field(body, 'bit_depth', r'(\d+)')
            master_m = _field(body, 'is_master', r'(\w+)')

            p['exposure'] = float(exp_m.group(1)) if exp_m else 0.1
            p['gain_mode'] = gain_m.group(1).lower() if gain_m else "high"
            p['bit_depth'] = int(depth_m.group(1)) if depth_m else 16
            p['is_master'] = master_m.group(1).lower() == 'true' if master_m else True

        compiled.append({'type': t, 'params': p})
    return compiled


def compile_stage_parameters(stages):
    stage_params = {}
    for block in stages:
        body = block['body']

        if block['type'] == 'apt_stage':
            axis_match = _field(body, 'axis', r'(\d+)')
            scan_match = _field(
                body, 'scan', r'([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)'
            )

            if axis_match and scan_match:
                ax = int(axis_match.group(1))
                start = float(scan_match.group(1))
                stop = float(scan_match.group(2))
                step = float(scan_match.group(3))

                stage_params[ax] = {
                    'type': 'apt_stage', # Tagged for main.py routing
                    'positions': generate_positions(start, stop, step),
                    # Whole-line flags, so a word appearing inside a value
                    # cannot switch these on.
                    'restore': _flag(body, 'restore'),
                    'save': _flag(body, 'save'),
                    'limits': _parse_limits(body, "'action apt_stage'", 'mm'),
                    'start_pos': start
                }

        elif block['type'] == 'd2a_phidget':
            scan_match = _field(
                body, 'scan', r'([\d\.\-]+)\s+([\d\.\-]+)\s+([\d\.\-]+)'
            )
            pos_match = _field(body, 'position', r'([\d\.\-]+)')
            limits = _parse_limits(body, "'action d2a_phidget'", 'um')

            if scan_match:
                start = float(scan_match.group(1))
                stop = float(scan_match.group(2))
                step = float(scan_match.group(3))

                # Using 99 as a virtual axis ID for the piezo
                stage_params[PIEZO_AXIS] = {
                    'type': 'piezo', # Tagged for main.py routing
                    'positions': generate_positions(start, stop, step),
                    'limits': limits,
                    'start_pos': start
                }
            elif pos_match:
                target = float(pos_match.group(1))
                stage_params[PIEZO_AXIS] = {
                    'type': 'piezo',
                    'positions': [target],
                    'limits': limits,
                    'start_pos': target
                }

    return stage_params
