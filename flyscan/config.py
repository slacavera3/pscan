"""Flat .con configuration for fly scans, in pscan's format.

Deliberately the same file syntax as pscan - 'action <type>' blocks of
key-value lines ending in 'end' - so that pscan's own actions can be dropped
into a fly scan's setup and teardown phases without a second config format
to learn. 'action script' and 'action webcam' blocks are parsed by pscan's
own compiler and mean exactly what they mean there.

FLAT, AND NESTING IS REFUSED RATHER THAN IGNORED

pscan's blocks nest, and the nesting IS the scan: where you write a block
decides whether 'count' averages a point, repeats a row or repeats the
image. None of that can be true here, because a fly scan's shape is fixed
rather than written:

  * the slow axis is the only loop there is;
  * the fast axis is NOT a loop - it is one continuous move per line;
  * the samples along it are hardware triggers, not Python iterations.
    Getting Python out of the per-pixel loop is the entire point of flying.

So there is nothing a fly config could put inside a loop, and position
carries no information: exactly one apt_fly, one apt_stage and one
scope_line are required, in any order. The reader underneath this module
would happily FLATTEN a nested file and run it, which is the problem -
someone who nests is saying "placement matters here" and it does not. It is
refused with that reason instead, because a format whose central rule is
true in one orchestrator and quietly false in the other is worse than two
formats that look different.

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
    parse_con_tree, flatten_tree, has_nesting,
    compile_pipeline_list, compile_stage_parameters,
    generate_positions, _field, _flag,
)
from flyscan.budget import (DEFAULT_REP_RATE_HZ, ROLE_AC, ROLE_DC,
                            ROLE_TRIGGER, ROLE_SPARE)
from flyscan.raster import ChannelPlan, RasterPlan

# Actions a fly scan understands, on top of the fly-specific ones below.
# Reused from pscan so the shared blocks have one definition, not two.
REUSED_ACTIONS = ('count', 'apt_stage', 'script', 'webcam', 'delay')

# Actions that exist in pscan but cannot work here, with the reason, so the
# error says why rather than just "unknown".
REJECTED_ACTIONS = {
    'scope': ("pscan's step-and-settle scope block; a fly scan uses "
              "'action scope_line' instead"),
    'picoscope': ("pscan's step-and-settle USB scope block; a fly scan uses "
                  "'action scope_line' with a 'usb' line instead, which "
                  "drives the same instrument a line at a time"),
    'a2d': "the NI card cannot sample per pixel at kHz pixel rates",
    'ixon': "no camera can expose per pixel at kHz pixel rates",
    'tucsen': "no camera can expose per pixel at kHz pixel rates",
    'd2a_phidget': ("the piezo is the focus actuator in a fly scan - "
                    "configure it with 'action autofocus'"),
    # The generic spelling of the same block. Listed explicitly so writing it
    # the new way gets this reason rather than a bare "unknown action type".
    'd2a': ("an analog output is a step-and-settle axis; in a fly scan the "
            "piezo is the focus actuator - configure it with "
            "'action autofocus'"),
}

FLY_SCHEMAS = {
    'apt_fly': {
        # No 'pixels': velocity and the delay clock decide how many copeaks
        # fit in a line, so specifying both would over-determine it.
        'required': ['axis', 'scan', 'velocity'],
        'optional': ['runup', 'limits', 'unidirectional', 'spot', 'bin',
                     'acceleration'],
    },
    # 'ip' is not required any more: a fly line can run on a USB PicoScope
    # instead, which is named by 'usb' (and optionally 'series'/'serial')
    # rather than by an address. Exactly one of the two has to identify the
    # instrument, which _scope_interface() below enforces - the schema cannot
    # express "one of these two groups".
    'scope_line': {
        'required': ['channels', 'roles', 'sample_rate', 'segment_window'],
        'optional': ['ip', 'sparsing', 'delay_rate', 'rep_rate',
                     'transfer_rate',
                     # The USB scope. It has no front panel, so what a LeCroy
                     # inherits from its knobs has to be said here.
                     'usb', 'series', 'serial', 'v_max', 'coupling',
                     'trigger', 'resolution', 'timeout'],
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
                 count, warnings, spot_um=None, scope_kind='lecroy',
                 picoscope=None):
        self.path = path
        self.plan = plan
        self.channels = channels
        self.scope_ip = scope_ip
        # 'lecroy' (VXI-11, named by scope_ip) or 'picoscope' (USB, described
        # by the picoscope dict). cli.run_scan() picks the class from this.
        self.scope_kind = scope_kind
        self.picoscope = picoscope or {}
        self.sample_rate_hz = sample_rate_hz
        self.transfer_mb_s = transfer_mb_s
        self.autofocus = autofocus
        # Kept so the runner can re-check smear against the spot right
        # before anything moves, not only at parse time.
        self.spot_um = spot_um
        self.setup_pipeline = setup_pipeline
        self.teardown_pipeline = teardown_pipeline
        self.count = count
        self.warnings = warnings

    @property
    def output_path(self):
        """Default output beside the config, as pscan names its outputs."""
        return os.path.splitext(self.path)[0] + '.h5'

    @property
    def scope_label(self):
        """How the instrument is addressed, for a log line."""
        if self.scope_kind != 'picoscope':
            return f"LeCroy {self.scope_ip}"
        where = self.picoscope.get('series') or 'auto-detect'
        if self.picoscope.get('serial'):
            where += f" {self.picoscope['serial']}"
        return f"PicoScope on USB ({where})"

    def summary(self):
        parts = [self.plan.summary(),
                 f"scope {self.scope_label}, channels "
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


def _nested_parents(blocks):
    """Every block in the tree that has blocks written inside it."""
    found = []
    for block in blocks:
        if block['children']:
            found.append(f"'action {block['type']}'")
            found.extend(_nested_parents(block['children']))
    return found


def _refuse_nesting(roots):
    """A fly config is flat. Say why, rather than flattening it silently.

    The reader underneath would flatten a nested file and run it correctly,
    ignoring the hierarchy - and that is exactly the failure worth refusing:
    in pscan the hierarchy IS the experiment, so someone who writes one here
    is relying on a rule that does not hold.
    """
    if not has_nesting(roots):
        return

    parents = _nested_parents(roots)
    if len(parents) == 1:
        culprit = f"{parents[0]} has blocks written inside it"
    else:
        culprit = (f"{', '.join(parents[:-1])} and {parents[-1]} have "
                   f"blocks written inside them")
    raise FlyConfigError(
        f"this config nests its blocks ({culprit}), and a fly scan is "
        f"flat.\n"
        f"  In pscan the nesting IS the scan - where you write a block "
        f"decides whether 'count' averages a point, repeats a row or "
        f"repeats the image. A fly scan's shape is fixed instead: the slow "
        f"axis is the only loop, the fast axis is one continuous move per "
        f"line, and the samples along it are hardware triggers rather than "
        f"Python iterations.\n"
        f"  So there is nothing to put inside a loop here, and position "
        f"carries no information: write the blocks one after another, in "
        f"any order. Anything before 'action apt_fly' is setup and anything "
        f"after it is teardown.\n"
        f"  This is refused rather than flattened for you because the "
        f"hierarchy would have meant nothing, and silently meaning nothing "
        f"is how a config comes to look right and do something else."
    )


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


def _scope_interface(body, present, label):
    """Which instrument an 'action scope_line' block describes.

    Decided by how it is ADDRESSED, because that is the one thing the two
    cannot share: a LeCroy has a network address and a PicoScope has a USB
    serial number. Stated rather than inferred from the channel names, which
    would make 'channels A, B' mean a different instrument from 'C1, C2' -
    a surprise worth avoiding.
    """
    usb_keys = sorted(present & {'usb', 'series', 'serial'})
    if 'ip' in present and usb_keys:
        raise FlyConfigError(
            f"{label} names both an 'ip' and {', '.join(usb_keys)}, so which "
            f"instrument it means is ambiguous. A fly line runs on one scope: "
            f"keep 'ip' for a LeCroy over the network, or 'usb' for a "
            f"PicoScope."
        )
    if 'ip' in present:
        return 'lecroy'
    if usb_keys:
        return 'picoscope'
    raise FlyConfigError(
        f"{label} names no instrument. Give 'ip <address>' for a LeCroy over "
        f"the network, or 'usb' for a PicoScope over USB (with an optional "
        f"'series'/'serial' when more than one is attached)."
    )


def _aligned(body, key, names, label, cast=str):
    """A per-channel list, aligned with the 'channels' line. {} when absent.

    One value applies to every channel, which is how a four-channel block
    says "all of them on the same range" without repeating itself.
    """
    values = _csv(body, key)
    if not values:
        return {}
    if len(values) == 1:
        values = values * len(names)
    if len(values) != len(names):
        raise FlyConfigError(
            f"{label} has {len(names)} channel(s) but {len(values)} "
            f"'{key}' value(s). Give one for all of them, or one per channel "
            f"in the same order as 'channels'."
        )
    try:
        return {name: cast(value) for name, value in zip(names, values)}
    except ValueError:
        raise FlyConfigError(
            f"{label} '{key} {' '.join(values)}' is not all numbers"
        ) from None


def _build_picoscope(body, present, label, names, warnings):
    """The PicoScope settings a fly line needs, validated before anything opens.

    The series table, range ladder, couplings and edges come from the driver
    rather than being restated here. Importing it opens nothing - the PicoSDK
    library is loaded in connect().
    """
    from pscan import picoscope_driver as pico

    series = None
    if 'series' in present:
        series = _field(body, 'series', r'(\S+)').group(1).lower()
        if series not in pico.SERIES:
            raise FlyConfigError(
                f"{label} series '{series}' is not one pscan drives. Use one "
                f"of: {', '.join(pico.SERIES_ORDER)}. The 2000 and 3000 "
                f"series WITHOUT an 'A' in the model number use a different "
                f"PicoSDK library."
            )

    for name in names:
        if len(name) != 1 or not ('A' <= name.upper() <= 'H'):
            raise FlyConfigError(
                f"{label} channel '{name}' is not a PicoScope input. "
                f"PicoScope channels are single letters: A, B, C, D (and "
                f"E..H on a 4000A). 'C1'-style names are LeCroy traces."
            )
        if series is not None:
            available = pico.SERIES[series]['n_channels']
            if ord(name.upper()) - ord('A') >= available:
                raise FlyConfigError(
                    f"{label} channel {name.upper()} does not exist on a "
                    f"{series} unit, which has {available} input(s)"
                )

    v_max = _aligned(body, 'v_max', names, label, float)
    widest = pico.RANGE_VOLTS[
        pico.SERIES[series]['max_range'] if series
        else len(pico.RANGE_VOLTS) - 1
    ]
    for name, value in v_max.items():
        if value <= 0:
            raise FlyConfigError(
                f"{label} v_max for {name} is the full scale in volts and "
                f"must be positive, not {value:g}"
            )
        if value > widest + 1e-12:
            raise FlyConfigError(
                f"{label} v_max {value:g} V is wider than any PicoScope "
                f"range (the widest is +/-{widest:g} V). Use an attenuating "
                f"probe."
            )
    if not v_max:
        warnings.append(
            f"{label} declares no 'v_max', so every channel is set to "
            f"+/-{pico.DEFAULT_V_MAX:g} V. A PicoScope has no front panel to "
            f"inherit a range from, so state it - too wide costs resolution "
            f"and too narrow clips"
        )

    coupling = _aligned(body, 'coupling', names, label)
    for name, value in coupling.items():
        if value.lower() not in pico.COUPLINGS:
            raise FlyConfigError(
                f"{label} coupling for {name} must be dc or ac, not '{value}'"
            )

    # Required. Every segment is one delay sweep, so without a trigger the
    # segments are not sweeps and the delay axis means nothing.
    trigger_match = _field(body, 'trigger')
    if trigger_match is None:
        raise FlyConfigError(
            f"{label} needs a 'trigger <source> <level_volts> <edge>' line - "
            f"the ASOPS delay clock, usually on the external input: "
            f"'trigger ext 1.0 rising'. A PicoScope inherits no trigger from "
            f"a front panel, and an untriggered capture is not a delay sweep."
        )
    tokens = trigger_match.group(1).replace(',', ' ').split()
    if len(tokens) != 3:
        raise FlyConfigError(
            f"{label} trigger needs three values: "
            f"'trigger <source> <level_volts> <edge>', for example "
            f"'trigger ext 1.0 rising'"
        )
    source, level, edge = tokens
    try:
        level = float(level)
    except ValueError:
        raise FlyConfigError(
            f"{label} trigger level '{level}' is not a number (volts)"
        ) from None
    if edge.lower() not in pico.EDGES:
        raise FlyConfigError(
            f"{label} trigger edge must be one of "
            f"{', '.join(sorted(pico.EDGES))}, not '{edge}'"
        )
    if source.upper() not in ('EXT', 'EXTERNAL'):
        if source.upper() not in [n.upper() for n in names]:
            raise FlyConfigError(
                f"{label} triggers on channel {source}, which it does not "
                f"record. A PicoScope can only trigger on a channel that is "
                f"switched on, or on 'ext'."
            )
        warnings.append(
            f"{label} triggers on channel {source} rather than 'ext'. That "
            f"works, but it spends an input on the delay clock that could be "
            f"recording signal"
        )

    resolution = None
    if 'resolution' in present:
        resolution = int(_numbers(body, 'resolution', 1, label)[0])
        if resolution not in pico.RESOLUTIONS:
            raise FlyConfigError(
                f"{label} resolution must be one of "
                f"{', '.join(str(b) for b in sorted(pico.RESOLUTIONS))} bits, "
                f"not {resolution}"
            )
        if series is not None and not pico.SERIES[series]['resolution_arg']:
            warnings.append(
                f"{label} sets 'resolution', but only the 5000A series has a "
                f"switchable ADC; on a {series} the line has no effect"
            )

    timeout = None
    if 'timeout' in present:
        timeout = _numbers(body, 'timeout', 1, label)[0]
        if timeout <= 0:
            raise FlyConfigError(
                f"{label} timeout must be positive, not {timeout:g}"
            )

    return {
        'series': series,
        'serial': _field(body, 'serial', r'(\S+)').group(1)
                  if 'serial' in present else None,
        'v_max': {name.upper(): value for name, value in v_max.items()},
        'coupling': {name.upper(): value.lower()
                     for name, value in coupling.items()},
        'trigger': {'source': source, 'level': level,
                    'direction': edge.lower()},
        'resolution': resolution,
        'timeout': timeout,
    }


def parse_fly_config(path, counts_per_mm):
    """Read a fly-scan .con file into a FlyConfig.

    counts_per_mm is a required argument rather than a config key because it
    is a property of the stage, not of the scan - and because a wrong value
    silently rescales every position. See HARDWARE_CHECKLIST.md item 1.
    """
    if not os.path.exists(path):
        raise FlyConfigError(f"config file '{path}' not found")

    # Read as a tree, like pscan does, so that a nested file can be
    # RECOGNISED and refused. Reading it flat would succeed and quietly
    # discard the hierarchy, which is the thing worth catching.
    roots = parse_con_tree(path)
    _refuse_nesting(roots)
    blocks = flatten_tree(roots)
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

    scope_kind = _scope_interface(scope_body, scope_present, scope_label)
    scope_ip = ''
    picoscope = None

    if scope_kind == 'lecroy':
        ip_match = _field(scope_body, 'ip', r'(\S+)')
        scope_ip = ip_match.group(1) if ip_match else ''
        octets = scope_ip.split('.')
        if len(octets) != 4 or not all(o.isdigit() and 0 <= int(o) <= 255
                                       for o in octets):
            raise FlyConfigError(
                f"{scope_label} ip '{scope_ip}' is not a dotted-quad address"
            )

    channels = _build_channels(scope_body, scope_present, scope_label)

    if scope_kind == 'picoscope':
        # Aligned with the 'channels' line rather than with the role order,
        # so a range sits beside the channel it belongs to in the file.
        picoscope = _build_picoscope(
            scope_body, scope_present, scope_label,
            _csv(scope_body, 'channels'), warnings,
        )

    sample_rate = _numbers(scope_body, 'sample_rate', 1, scope_label)[0]
    segment_window = _numbers(scope_body, 'segment_window', 1, scope_label)[0]
    delay_rate = (_numbers(scope_body, 'delay_rate', 1, scope_label)[0]
                  if 'delay_rate' in scope_present else 10e3)
    # The laser rep rate is not a scope setting, but like delay_rate it is a
    # property of the timing chain the scope is hanging off, and the two are
    # only meaningful together: their ratio is the stretch factor. Benches
    # here run 80 MHz and 100 MHz lasers, and the delay axis of every file
    # scales directly with this, so an unstated value is worth a warning
    # rather than a silent default.
    if 'rep_rate' in scope_present:
        rep_rate = _numbers(scope_body, 'rep_rate', 1, scope_label)[0]
    else:
        rep_rate = DEFAULT_REP_RATE_HZ
        warnings.append(
            f"{scope_label} has no 'rep_rate', so the delay axis will be "
            f"written assuming {DEFAULT_REP_RATE_HZ / 1e6:.6g} MHz. State it "
            f"outright - a rig running a different laser gets a delay axis "
            f"wrong by the ratio of the two"
        )
    # USB moves a line far faster than VXI-11, so the two defaults differ.
    # Either way it is only used to size timeouts and the time estimate -
    # measure yours and state it.
    if 'transfer_rate' in scope_present:
        transfer_mb_s = _numbers(scope_body, 'transfer_rate', 1, scope_label)[0]
    elif scope_kind == 'picoscope':
        from flyscan.scope import DEFAULT_USB_TRANSFER_MB_S
        transfer_mb_s = DEFAULT_USB_TRANSFER_MB_S
    else:
        transfer_mb_s = 4.0

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
            sample_rate_hz=sample_rate, delay_rate_hz=delay_rate,
            rep_rate_hz=rep_rate,
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

    # 'action count' above 1 is REFUSED, not warned about.
    #
    # Nothing in the fly runner reads this value - not cli.py, not
    # raster.py - so a config asking for 4 used to get exactly one pass and
    # a warning claiming it would "fly every line 4 times". A warning that
    # describes an effect which does not happen is worse than no warning:
    # the data comes back a quarter of the expected size and looks healthy.
    #
    # 'count 1' is still accepted in silence, because it means what it says
    # and every config written from pscan's template carries it.
    #
    # What it WOULD mean is a real and useful experiment - repeat the whole
    # image and compare the passes for drift rather than averaging them
    # blindly - which is why the message says so instead of pretending the
    # key is meaningless. It needs a loop around RasterScan.run()'s line
    # loop; see todo.txt.
    #
    # Note what it does NOT mean: averaging at a point. In a fly scan every
    # segment IS one pixel, so there is no per-point repeat to average -
    # more signal per pixel comes from 'bin', which averages neighbouring
    # copeaks into one output pixel after the fact.
    count_block = _exactly_one(fly, 'count', required=False)
    count = 1
    if count_block is not None:
        count = int(_numbers(count_block['body'], 'count', 1,
                             "'action count'")[0])
        if count != 1:
            raise FlyConfigError(
                f"'action count' is {count}, and a fly scan cannot repeat "
                f"yet: nothing in the fly runner acts on this key, so the "
                f"scan would make ONE pass and the file would be a "
                f"{count}th of the size you expected.\n"
                f"  Repeating the whole image - so the passes can be "
                f"compared for drift instead of averaged blindly - is worth "
                f"having and is not implemented. There is no per-point "
                f"repeat to average in a fly scan either: every segment IS "
                f"one pixel. For more signal per pixel use 'bin <nm>' in "
                f"the 'action apt_fly' block, which averages neighbouring "
                f"copeaks into one output pixel and whose SNR gain "
                f"--dry-run reports.\n"
                f"  Use 'count 1', or drop the block."
            )

    counter = [1]
    return FlyConfig(
        path=path, plan=plan, channels=channels, scope_ip=scope_ip,
        scope_kind=scope_kind, picoscope=picoscope,
        sample_rate_hz=sample_rate, transfer_mb_s=transfer_mb_s,
        autofocus=autofocus,
        setup_pipeline=compile_pipeline_list(setup_blocks, counter),
        teardown_pipeline=compile_pipeline_list(teardown_blocks, counter),
        count=count, warnings=warnings, spot_um=spot_um,
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
