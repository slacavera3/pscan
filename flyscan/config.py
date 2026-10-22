"""Flat .con configuration for fly scans, in pscan's format.

Deliberately the same file syntax as pscan - 'action <type>' blocks of
key-value lines ending in 'end' - so that pscan's own actions can be dropped
into a fly scan's setup and teardown phases without a second config format
to learn. 'action script' and 'action webcam' blocks are parsed by pscan's
own compiler and mean exactly what they mean there.

The fly-specific blocks are:
  apt_fly      the fast axis, swept at constant velocity
  scope_line   one Sequence-mode acquisition per line
  autofocus    pre-scan focus map, and optional periodic refocus

and the slow axis is an ordinary 'action apt_stage', because stepping and
settling once per line is exactly what that already does.

Blocks that cannot work in a fly scan are rejected rather than ignored:
pscan's step-and-settle 'action scope', and the per-coordinate detectors
(a2d, ixon, tucsen), none of which can keep up at kHz pixel rates.
"""
import os
import re

from pscan.config_parser import (
    ACTION_SCHEMAS as PSCAN_SCHEMAS,
    parse_flat_con_file, compile_pipeline_list, compile_stage_parameters,
    generate_positions, _field, _flag,
)
from flyscan.budget import ROLE_AC, ROLE_DC, ROLE_TRIGGER, ROLE_SPARE
from flyscan.raster import ChannelPlan, RasterPlan

# Actions a fly scan understands, on top of the fly-specific ones below.
# Reused from pscan so the shared blocks have one definition, not two.
REUSED_ACTIONS = ('count', 'apt_stage', 'script', 'webcam', 'delay')

# Actions that exist in pscan but cannot work here, with the reason, so the
# error says why rather than just "unknown".
REJECTED_ACTIONS = {
    'scope': ("pscan's step-and-settle scope block; a fly scan uses "
              "'action scope_line' instead"),
    'a2d': "the NI card cannot sample per pixel at kHz pixel rates",
    'ixon': "no camera can expose per pixel at kHz pixel rates",
    'tucsen': "no camera can expose per pixel at kHz pixel rates",
    'd2a_phidget': ("the piezo is the focus actuator in a fly scan - "
                    "configure it with 'action autofocus'"),
}

FLY_SCHEMAS = {
    'apt_fly': {
        # No 'pixels': velocity and the sweep clock decide how many copeaks
        # fit in a line, so specifying both would over-determine it.
        'required': ['axis', 'scan', 'velocity'],
        'optional': ['runup', 'limits', 'unidirectional', 'spot', 'bin',
                     'acceleration'],
    },
    'scope_line': {
        'required': ['ip', 'channels', 'roles', 'sample_rate',
                     'segment_window'],
        'optional': ['sparsing', 'sweep_rate', 'transfer_rate'],
    },
    'autofocus': {
        'required': ['coarse'],
        'optional': ['fine_span', 'fine_step', 'grid', 'metric',
                     'every_lines', 'limits', 'surface'],
    },
}

ROLE_NAMES = {
    'ac': ROLE_AC, 'dc': ROLE_DC,
    'trigger': ROLE_TRIGGER, 'spare': ROLE_SPARE,
}

FOCUS_METRICS = ('copeak', 'dc')
FOCUS_SURFACES = ('plane', 'quadratic')


class FlyConfigError(ValueError):
    """The configuration cannot describe a runnable fly scan."""


def _numbers(body, key, count, label):
    """Read `count` numbers from a key's line.

    Separators are spaces and tabs only. With \\s+ a key short of a value
    would match across the newline and swallow the NEXT line's key as its
    last number - 'scan 53.500' followed by 'pixels 100' came back as
    ('53.500', 'pixels') instead of failing.
    """
    pattern = r'(\S+)' + r'[ \t]+(\S+)' * (count - 1)
    match = _field(body, key, pattern)
    if match is None:
        raise FlyConfigError(
            f"{label} '{key}' needs {count} number(s)"
        )
    try:
        return [float(v) for v in match.groups()]
    except ValueError:
        raise FlyConfigError(
            f"{label} '{key} {' '.join(match.groups())}' is not all numbers"
        ) from None


def _csv(body, key):
    """Read a comma- or space-separated list from a key's line."""
    match = _field(body, key)
    if match is None:
        return []
    return [item.strip() for item in
            match.group(1).replace(',', ' ').split() if item.strip()]


class AutofocusPlan:
    """How to find focus, and how often to check it again.

    The pre-scan grid replaces the single centre autofocus that a long scan
    outgrows: focus is measured at `grid` points across the field, a surface
    is fitted, and the piezo then follows that surface once per line. See
    HARDWARE_CHECKLIST.md on depth of focus against sample tilt.
    """

    def __init__(self, coarse_start_um, coarse_stop_um, coarse_step_um,
                 fine_span_um=None, fine_step_um=None, grid=(3, 3),
                 metric='copeak', every_lines=0, limits_um=None,
                 surface='plane'):
        self.coarse_start_um = float(coarse_start_um)
        self.coarse_stop_um = float(coarse_stop_um)
        self.coarse_step_um = float(coarse_step_um)
        self.fine_span_um = (None if fine_span_um is None
                             else float(fine_span_um))
        self.fine_step_um = (None if fine_step_um is None
                             else float(fine_step_um))
        self.grid = (int(grid[0]), int(grid[1]))
        self.metric = metric
        self.every_lines = int(every_lines)
        self.limits_um = limits_um
        self.surface = surface

        self.check()

    @property
    def coarse_positions(self):
        return generate_positions(
            self.coarse_start_um, self.coarse_stop_um, self.coarse_step_um
        )

    @property
    def grid_points(self):
        return self.grid[0] * self.grid[1]

    @property
    def does_fine_pass(self):
        return self.fine_span_um is not None and self.fine_step_um is not None

    def fine_positions(self, centre_um):
        """Fine sweep around a coarse result."""
        if not self.does_fine_pass:
            return [centre_um]
        half = self.fine_span_um / 2.0
        return generate_positions(
            centre_um - half, centre_um + half, self.fine_step_um
        )

    def check(self):
        if self.metric not in FOCUS_METRICS:
            raise FlyConfigError(
                f"autofocus metric '{self.metric}' is not one of: "
                f"{', '.join(FOCUS_METRICS)}"
            )
        if self.surface not in FOCUS_SURFACES:
            raise FlyConfigError(
                f"autofocus surface '{self.surface}' is not one of: "
                f"{', '.join(FOCUS_SURFACES)}"
            )
        if min(self.grid) < 1:
            raise FlyConfigError("autofocus grid needs at least 1x1 points")
        if self.every_lines < 0:
            raise FlyConfigError("autofocus every_lines cannot be negative")

        # generate_positions raises on a zero or wrong-sign step.
        coarse = self.coarse_positions
        if len(coarse) < 3:
            raise FlyConfigError(
                f"coarse sweep gives only {len(coarse)} point(s); a focus "
                f"peak cannot be located in fewer than 3"
            )

        if self.surface == 'plane' and self.grid_points < 3:
            raise FlyConfigError(
                f"fitting a plane needs at least 3 grid points, not "
                f"{self.grid_points}"
            )
        if self.surface == 'quadratic' and self.grid_points < 6:
            raise FlyConfigError(
                f"fitting a quadratic needs at least 6 grid points, not "
                f"{self.grid_points}"
            )

        if self.limits_um is not None:
            low, high = self.limits_um
            span = min(coarse), max(coarse)
            if span[0] < low or span[1] > high:
                raise FlyConfigError(
                    f"coarse sweep {span[0]:g}..{span[1]:g} um falls outside "
                    f"piezo limits {low:g}..{high:g} um"
                )

    def summary(self):
        lines = [
            f"coarse sweep {self.coarse_start_um:g}..{self.coarse_stop_um:g} "
            f"um step {self.coarse_step_um:g} ({len(self.coarse_positions)} pts)",
            f"focus grid {self.grid[0]}x{self.grid[1]} = {self.grid_points} "
            f"points, fitted as a {self.surface}",
            f"metric: {self.metric}",
        ]
        if self.does_fine_pass:
            lines.append(
                f"fine sweep +/-{self.fine_span_um / 2:g} um "
                f"step {self.fine_step_um:g}"
            )
        lines.append(
            "refocus: never during the scan" if self.every_lines == 0
            else f"refocus every {self.every_lines} line(s)"
        )
        return "\n".join(lines)


class FlyConfig:
    """Everything one .con file says about a fly scan."""

    def __init__(self, path, plan, channels, scope_ip, sample_rate_hz,
                 transfer_mb_s, autofocus, setup_pipeline, teardown_pipeline,
                 count, warnings):
        self.path = path
        self.plan = plan
        self.channels = channels
        self.scope_ip = scope_ip
        self.sample_rate_hz = sample_rate_hz
        self.transfer_mb_s = transfer_mb_s
        self.autofocus = autofocus
        self.setup_pipeline = setup_pipeline
        self.teardown_pipeline = teardown_pipeline
        self.count = count
        self.warnings = warnings

    @property
    def output_path(self):
        """Default output beside the config, as pscan names its outputs."""
        return os.path.splitext(self.path)[0] + '.h5'

    def summary(self):
        parts = [self.plan.summary(),
                 f"scope {self.scope_ip}, channels "
                 + ", ".join(f"{c}={self.channels.roles[c]}"
                             f"/sp{self.channels.sparsing[c]}"
                             for c in self.channels.channels)]
        if self.autofocus is not None:
            parts.append(self.autofocus.summary())
        else:
            parts.append("no autofocus block - focus is not corrected")
        for label, pipeline in (("setup", self.setup_pipeline),
                                ("teardown", self.teardown_pipeline)):
            if pipeline:
                parts.append(f"{label}: "
                             + ", ".join(a['type'] for a in pipeline))
        parts.append(f"output: {self.output_path}")
        return "\n".join(parts)


def _split_blocks(blocks):
    """Sort blocks into the fly-specific ones and the reused pipeline ones.

    Unlike pscan, position in the file does not decide when a block runs:
    a fly scan has no per-coordinate pipeline to fall into, so anything
    before the apt_fly block is setup and anything after it is teardown.
    That is both simpler and far less surprising than pscan's rule.
    """
    fly_index = next((i for i, b in enumerate(blocks)
                      if b['type'] == 'apt_fly'), None)
    if fly_index is None:
        raise FlyConfigError(
            "no 'action apt_fly' block - that is what makes this a fly scan"
        )

    fly, setup, teardown = {}, [], []
    for i, block in enumerate(blocks):
        kind = block['type']
        if kind in ('apt_fly', 'scope_line', 'autofocus', 'apt_stage',
                    'count'):
            fly.setdefault(kind, []).append(block)
        elif i < fly_index:
            setup.append(block)
        else:
            teardown.append(block)
    return fly, setup, teardown


def _validate_types(blocks):
    allowed = set(FLY_SCHEMAS) | set(REUSED_ACTIONS)
    for block in blocks:
        kind = block['type']
        if kind in REJECTED_ACTIONS:
            raise FlyConfigError(
                f"'action {kind}' cannot be used in a fly scan: "
                f"{REJECTED_ACTIONS[kind]}"
            )
        if kind not in allowed:
            raise FlyConfigError(
                f"unknown action type 'action {kind}'. A fly scan "
                f"understands: {', '.join(sorted(allowed))}"
            )


def _validate_keys(block, schemas):
    kind = block['type']
    schema = schemas[kind]
    label = f"'action {kind}'"
    allowed = set(schema['required']) | set(schema['optional'])

    present = set()
    for line in block['body'].splitlines():
        line = line.strip()
        if not line or line.lower() == 'end' or line.lower().startswith('action '):
            continue
        present.add(line.split()[0].lower())

    unknown = sorted(present - allowed)
    if unknown:
        raise FlyConfigError(
            f"{label} does not understand the key '{unknown[0]}'. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )
    for key in schema['required']:
        if key not in present:
            raise FlyConfigError(f"{label} is missing its required '{key}' line")
    return present


def _exactly_one(fly, kind, required=True):
    found = fly.get(kind, [])
    if len(found) > 1:
        raise FlyConfigError(
            f"{len(found)} 'action {kind}' blocks; a fly scan takes one"
        )
    if not found:
        if required:
            raise FlyConfigError(f"no 'action {kind}' block")
        return None
    return found[0]


def _build_channels(body, present, label):
    names = _csv(body, 'channels')
    roles = _csv(body, 'roles')
    sparsing = _csv(body, 'sparsing') if 'sparsing' in present else []

    if not names:
        raise FlyConfigError(f"{label} channels line lists no channels")
    if len(roles) != len(names):
        raise FlyConfigError(
            f"{label} has {len(names)} channel(s) but {len(roles)} role(s); "
            f"they must correspond one to one"
        )
    if sparsing and len(sparsing) != len(names):
        raise FlyConfigError(
            f"{label} has {len(names)} channel(s) but {len(sparsing)} "
            f"sparsing value(s)"
        )

    for role in roles:
        if role.lower() not in ROLE_NAMES:
            raise FlyConfigError(
                f"{label} role '{role}' is not one of: "
                f"{', '.join(sorted(ROLE_NAMES))}"
            )
    if roles.count('ac') != 1:
        raise FlyConfigError(
            f"{label} needs exactly one channel with role 'ac' - it is the "
            f"signal, and the rest are referred to it"
        )

    factors = {}
    for index, name in enumerate(names):
        if name in factors:
            raise FlyConfigError(f"{label} lists channel {name} twice")
        try:
            factors[name] = int(sparsing[index]) if sparsing else 1
        except ValueError:
            raise FlyConfigError(
                f"{label} sparsing '{sparsing[index]}' is not a whole number"
            ) from None
        if factors[name] < 1:
            raise FlyConfigError(f"{label} sparsing must be at least 1")

    by_role = {role.lower(): name for name, role in zip(names, roles)}
    return ChannelPlan(
        ac=by_role.get('ac'), dc=by_role.get('dc'),
        trigger=by_role.get('trigger'), spare=by_role.get('spare'),
        dc_sparsing=factors.get(by_role.get('dc'), 1),
        trigger_sparsing=factors.get(by_role.get('trigger'), 1),
        spare_sparsing=factors.get(by_role.get('spare'), 1),
    )


def parse_fly_config(path, counts_per_mm):
    """Read a fly-scan .con file into a FlyConfig.

    counts_per_mm is a required argument rather than a config key because it
    is a property of the stage, not of the scan - and because a wrong value
    silently rescales every position. See HARDWARE_CHECKLIST.md item 1.
    """
    if not os.path.exists(path):
        raise FlyConfigError(f"config file '{path}' not found")

    blocks = parse_flat_con_file(path)
    if not blocks:
        raise FlyConfigError(
            f"'{path}' has no action blocks - empty, or fully commented out"
        )

    _validate_types(blocks)
    schemas = dict(FLY_SCHEMAS)
    schemas.update({k: PSCAN_SCHEMAS[k] for k in REUSED_ACTIONS})
    for block in blocks:
        _validate_keys(block, schemas)

    fly, setup_blocks, teardown_blocks = _split_blocks(blocks)
    warnings = []

    # ---------------------------------------------------- the fast axis
    fast = _exactly_one(fly, 'apt_fly')
    body = fast['body']
    label = "'action apt_fly'"
    present = _validate_keys(fast, schemas)

    fast_axis = int(_numbers(body, 'axis', 1, label)[0])
    fast_start, fast_stop = _numbers(body, 'scan', 2, label)
    velocity = _numbers(body, 'velocity', 1, label)[0]
    # 'bin' is the output pixel size in nm, which is post-processing
    # metadata rather than an acquisition setting: it changes how many
    # copeaks are averaged together, not how they are taken.
    bin_um = (_numbers(body, 'bin', 1, label)[0] / 1000.0
              if 'bin' in present else None)
    acceleration = (_numbers(body, 'acceleration', 1, label)[0]
                    if 'acceleration' in present else None)
    runup = (_numbers(body, 'runup', 1, label)[0]
             if 'runup' in present else 0.05)
    fast_limits = (tuple(_numbers(body, 'limits', 2, label))
                   if 'limits' in present else None)
    spot_um = (_numbers(body, 'spot', 1, label)[0]
               if 'spot' in present else None)
    bidirectional = not _flag(body, 'unidirectional')

    if fast_limits is None:
        warnings.append(
            f"{label} declares no 'limits <min_mm> <max_mm>', so the fly "
            f"travel including run-up is not bounds-checked"
        )
    if spot_um is None:
        warnings.append(
            f"{label} declares no 'spot <um>', so per-sweep smear cannot be "
            f"checked against the optical resolution"
        )

    # ---------------------------------------------------- the slow axis
    slow_block = _exactly_one(fly, 'apt_stage')
    slow_params = compile_stage_parameters([slow_block])
    slow_axes = [ax for ax in slow_params if ax != 99]
    if len(slow_axes) != 1:
        raise FlyConfigError(
            "the 'action apt_stage' block must declare exactly one axis, "
            "which becomes the slow axis"
        )
    slow_axis = slow_axes[0]
    if slow_axis == fast_axis:
        raise FlyConfigError(
            f"axis {fast_axis} is both the fly axis and the slow axis"
        )
    slow_positions = slow_params[slow_axis]['positions']

    # ---------------------------------------------------------- the scope
    scope_block = _exactly_one(fly, 'scope_line')
    scope_body = scope_block['body']
    scope_label = "'action scope_line'"
    scope_present = _validate_keys(scope_block, schemas)

    ip_match = _field(scope_body, 'ip', r'(\S+)')
    scope_ip = ip_match.group(1) if ip_match else ''
    octets = scope_ip.split('.')
    if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255
                                   for o in octets):
        raise FlyConfigError(
            f"{scope_label} ip '{scope_ip}' is not a dotted-quad address"
        )

    channels = _build_channels(scope_body, scope_present, scope_label)
    sample_rate = _numbers(scope_body, 'sample_rate', 1, scope_label)[0]
    segment_window = _numbers(scope_body, 'segment_window', 1, scope_label)[0]
    sweep_rate = (_numbers(scope_body, 'sweep_rate', 1, scope_label)[0]
                  if 'sweep_rate' in scope_present else 10e3)
    transfer_mb_s = (_numbers(scope_body, 'transfer_rate', 1, scope_label)[0]
                     if 'transfer_rate' in scope_present else 4.0)

    if channels.roles.get(channels.ac) != ROLE_AC:
        raise FlyConfigError(f"{scope_label} has no 'ac' channel")
    if ROLE_TRIGGER not in channels.roles.values():
        warnings.append(
            f"{scope_label} records no 'trigger' channel, so position has to "
            f"be inferred from time and velocity rather than measured - see "
            f"HARDWARE_CHECKLIST.md item 3"
        )

    # ------------------------------------------------------- the autofocus
    focus_block = _exactly_one(fly, 'autofocus', required=False)
    autofocus = None
    if focus_block is not None:
        autofocus = _build_autofocus(focus_block, schemas)
    else:
        warnings.append(
            "no 'action autofocus' block. Over a field wider than about "
            "100 um, sample tilt alone usually exceeds the depth of focus, "
            "and signal strength follows focus quality"
        )

    # ------------------------------------------------------------- plan
    try:
        plan = RasterPlan(
            fast_axis=_axis_name(fast_axis),
            slow_axis=_axis_name(slow_axis),
            fast_start_mm=fast_start, fast_stop_mm=fast_stop,
            slow_start_mm=slow_positions[0], slow_stop_mm=slow_positions[-1],
            lines=len(slow_positions),
            velocity_mm_s=velocity, counts_per_mm=counts_per_mm,
            sample_rate_hz=sample_rate, sweep_rate_hz=sweep_rate,
            segment_seconds=segment_window, runup_mm=runup,
            bidirectional=bidirectional, fast_limits_mm=fast_limits,
            bin_um=bin_um, acceleration_mm_s2=acceleration,
        )
        warnings.extend(plan.check(spot_um=spot_um))
    except FlyConfigError:
        raise
    except ValueError as e:
        # RasterPlan validates geometry and knows nothing about config
        # files; re-raise so the caller gets a config error naming the file
        # rather than a bare ValueError from a geometry class.
        raise FlyConfigError(f"in '{os.path.basename(path)}': {e}") from e

    count_block = _exactly_one(fly, 'count', required=False)
    count = 1
    if count_block is not None:
        count = int(_numbers(count_block['body'], 'count', 1,
                             "'action count'")[0])
        if count != 1:
            warnings.append(
                f"'action count' is {count}; a fly scan repeats whole lines, "
                f"so this will fly every line {count} times"
            )

    counter = [1]
    return FlyConfig(
        path=path, plan=plan, channels=channels, scope_ip=scope_ip,
        sample_rate_hz=sample_rate, transfer_mb_s=transfer_mb_s,
        autofocus=autofocus,
        setup_pipeline=compile_pipeline_list(setup_blocks, counter),
        teardown_pipeline=compile_pipeline_list(teardown_blocks, counter),
        count=count, warnings=warnings,
    )


def _build_autofocus(block, schemas):
    body = block['body']
    label = "'action autofocus'"
    present = _validate_keys(block, schemas)

    coarse = _numbers(body, 'coarse', 3, label)
    grid = ([int(v) for v in _numbers(body, 'grid', 2, label)]
            if 'grid' in present else (3, 3))

    metric_match = _field(body, 'metric', r'(\S+)')
    surface_match = _field(body, 'surface', r'(\S+)')

    return AutofocusPlan(
        coarse_start_um=coarse[0], coarse_stop_um=coarse[1],
        coarse_step_um=coarse[2],
        fine_span_um=(_numbers(body, 'fine_span', 1, label)[0]
                      if 'fine_span' in present else None),
        fine_step_um=(_numbers(body, 'fine_step', 1, label)[0]
                      if 'fine_step' in present else None),
        grid=grid,
        metric=(metric_match.group(1).lower() if metric_match else 'copeak'),
        every_lines=(int(_numbers(body, 'every_lines', 1, label)[0])
                     if 'every_lines' in present else 0),
        limits_um=(tuple(_numbers(body, 'limits', 2, label))
                   if 'limits' in present else None),
        surface=(surface_match.group(1).lower() if surface_match else 'plane'),
    )


def _axis_name(axis_number):
    """pscan numbers APT axes 0 and 1; the driver addresses them x and y."""
    if axis_number == 0:
        return 'x'
    if axis_number == 1:
        return 'y'
    raise FlyConfigError(
        f"axis {axis_number} is not a stage axis; use 0 (X) or 1 (Y)"
    )
