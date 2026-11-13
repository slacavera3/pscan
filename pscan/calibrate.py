"""Establish counts-per-mm for a particular stage on a particular controller.

Why this exists: pscan hardcoded 34304, which is the Z8xx/TDC001 DC-servo
figure. A BBD303 driving an MLS203 reports 20000 (50 nm). A wrong value
rescales every position silently - the data look healthy and the microns are
wrong - so the number has to come from the rig rather than from a constant.

Three sources, because software alone cannot measure a length:

  controller  Read cnts_per_unit out of STAGEAXISPARAMS (0x04F2). Costs
              nothing and moves nothing, but it is the controller's own
              declaration, not a measurement. Usually right.

  span        Home to one limit, drive to the other, and divide the counts
              travelled by the stage's datasheet travel. This does not
              measure anything either - it trades the counts-per-mm constant
              for a travel constant - but the travel of a catalogue stage is
              far better known than a DC-servo figure copied from another
              product. Its real value is as an independent check: if
              span_counts / cnts_per_unit comes out at the datasheet travel,
              the controller's figure is corroborated.

  manual      Command a known move, measure how far the stage actually went
              with an instrument outside this software, and divide. The only
              one of the three that is a measurement.

Records are keyed by controller serial, stage part number and axis, so a
value measured on one rig is never silently reused on another - which is the
failure that produced the 34304 problem in the first place.
"""
import json
import os
import time
from pathlib import Path

# Last-resort fallback, and the source of the original bug. Kept only so a
# rig with no calibration behaves as it always did rather than refusing to
# run; anything that uses it should say so.
LEGACY_COUNTS_PER_MM = 34304.0

# Catalogue travel for stages we have seen, in mm, used by the 'span' method
# when the caller does not supply one. Keyed by the part number the
# controller reports in STAGEAXISPARAMS. Deliberately short: a guess here is
# worse than asking.
KNOWN_TRAVEL_MM = {
    'MLS203': (110.0, 75.0),   # (x, y)
}

SOURCE_CONTROLLER = 'controller'
SOURCE_SPAN = 'span'
SOURCE_MANUAL = 'manual'
SOURCE_LEGACY = 'legacy-default'

# Agreement within this fraction counts as corroboration rather than conflict.
AGREEMENT_TOLERANCE = 0.02


def user_path():
    """Per-user store. Written by default."""
    base = os.environ.get('XDG_CONFIG_HOME') or (Path.home() / '.config')
    return Path(base) / 'pscan' / 'stage_calibration.json'


def system_path():
    """Machine-wide store, for a rig calibrated once for everyone on it."""
    return Path('/etc/pscan/stage_calibration.json')


def rig_key(params, axis_id):
    """Identify this stage on this controller.

    Serial number and part number both come from STAGEAXISPARAMS. Without
    them the key degrades to the axis alone, which is better than nothing but
    will collide across rigs, so callers should say when identity is unknown.
    """
    if not params:
        return f"unknown:unknown:{axis_id}"
    serial = params.get('serial_num') or 'unknown'
    part = params.get('part_no') or 'unknown'
    return f"{serial}:{part}:{axis_id}"


def _read_one(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = data.get('entries')
    return entries if isinstance(entries, dict) else {}


def load_calibration(paths=None):
    """Merge the system store then the user store, user winning."""
    if paths is None:
        paths = (system_path(), user_path())
    merged = {}
    for path in paths:
        merged.update(_read_one(path))
    return merged


def save_calibration(key, record, path=None):
    """Write one record, preserving everything else already in the file."""
    # Coerced rather than assumed: user_path() is the default, but callers
    # and tests pass plain strings and a str has no .parent.
    path = Path(path) if path else Path(user_path())
    path.parent.mkdir(parents=True, exist_ok=True)

    entries = _read_one(path)
    entries[key] = record
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'entries': entries}, fh, indent=2, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, path)
    return path


def make_record(counts_per_mm, source, **detail):
    record = {
        'counts_per_mm': float(counts_per_mm),
        'source': source,
        'recorded_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    record.update(detail)
    return record


def from_controller(params):
    """The controller's own cnts_per_unit, if it is usable."""
    if not params:
        return None
    cnts = params.get('cnts_per_unit')
    if not cnts or not (100 <= cnts <= 1_000_000):
        return None
    return make_record(cnts, SOURCE_CONTROLLER,
                       part_no=params.get('part_no'),
                       serial_num=params.get('serial_num'))


def from_span(span_counts, travel_mm):
    """Counts travelled between the limits, over the datasheet travel."""
    if not span_counts or not travel_mm or travel_mm <= 0:
        return None
    return make_record(abs(span_counts) / float(travel_mm), SOURCE_SPAN,
                       span_counts=int(span_counts),
                       travel_mm=float(travel_mm))


def from_manual(delta_counts, measured_mm):
    """Counts the encoder reported, over the distance you actually measured."""
    if not delta_counts or not measured_mm or measured_mm == 0:
        return None
    return make_record(abs(delta_counts) / abs(float(measured_mm)),
                       SOURCE_MANUAL,
                       delta_counts=int(delta_counts),
                       measured_mm=float(measured_mm))


def default_travel_mm(params, axis_id):
    """Datasheet travel for a part number we recognise, else None."""
    if not params:
        return None
    pair = KNOWN_TRAVEL_MM.get((params.get('part_no') or '').upper())
    if not pair:
        return None
    return pair[0] if axis_id == 1 else pair[1]


def compare(records):
    """Lines describing how the available figures relate. [] if they agree.

    Reported rather than resolved: two methods disagreeing is information
    about the rig, and picking a winner silently is how a wrong constant
    survives for years.
    """
    present = [r for r in records if r]
    if len(present) < 2:
        return []

    lines = []
    base = present[0]
    for other in present[1:]:
        a, b = base['counts_per_mm'], other['counts_per_mm']
        if not a:
            continue
        if abs(a - b) <= AGREEMENT_TOLERANCE * a:
            lines.append(f"{base['source']} and {other['source']} agree "
                         f"({a:.1f} vs {b:.1f} counts/mm)")
        else:
            lines.append(f"{base['source']} says {a:.1f} counts/mm but "
                         f"{other['source']} says {b:.1f} - a factor of "
                         f"{a / b:.4f}")
    return lines


def counts_per_mm_for(params, axis_id, calibration=None,
                      fallback=LEGACY_COUNTS_PER_MM):
    """Resolve the figure to use: stored, else controller, else the default.

    Returns (counts_per_mm, source). The source is returned, not discarded,
    so a UI can say where the number came from - the whole problem with
    34304 was that nothing ever said.
    """
    if calibration is None:
        calibration = load_calibration()

    key = rig_key(params, axis_id)
    stored = calibration.get(key)
    if stored and stored.get('counts_per_mm'):
        return float(stored['counts_per_mm']), stored.get('source', 'stored')

    declared = from_controller(params)
    if declared:
        return declared['counts_per_mm'], SOURCE_CONTROLLER

    return float(fallback), SOURCE_LEGACY


# --- Procedures that move the stage ---------------------------------------
#
# All of these take the stage as an argument and do their own I/O through
# injected callables, so the logic can be exercised without hardware. None of
# them drives deliberately into a limit switch: the 'span' procedure travels
# between the limits the controller reports, which the caller has already
# validated against the position counter, rather than seeking the ends by
# stalling the motor against them.

def wait_until_still(stage, axis_id, timeout_s=60.0, still_for_s=0.4,
                     poll_s=0.05, sleep=None):
    """Poll the encoder until it stops changing. Returns the final count.

    The encoder is the authority rather than MOVE_COMPLETED, so this works on
    a controller that does not emit one, and it cannot be satisfied early by
    an unsolicited packet.
    """
    sleep = sleep or time.sleep
    deadline = time.monotonic() + timeout_s
    last = stage.get_position_counts(axis_id)
    unchanged_since = time.monotonic()

    while time.monotonic() < deadline:
        sleep(poll_s)
        now = stage.get_position_counts(axis_id)
        if now is None:
            continue
        if last is not None and now == last:
            if time.monotonic() - unchanged_since >= still_for_s:
                return now
        else:
            unchanged_since = time.monotonic()
        last = now

    return last


def measure_manual(stage, axis_id, step_counts, sleep=None):
    """Move a known number of COUNTS and report how many it actually moved.

    Commanded in counts on purpose: the thing being calibrated is the
    counts-to-mm conversion, so using it to set up the measurement would make
    the result depend on the answer.

    Returns (delta_counts, start_counts, end_counts), or None if the position
    could not be read.
    """
    start = stage.get_position_counts(axis_id)
    if start is None:
        return None

    stage.move_relative_counts(axis_id, step_counts)
    end = wait_until_still(stage, axis_id, sleep=sleep)
    if end is None:
        return None

    return end - start, start, end


def measure_span(stage, axis_id, params, sleep=None):
    """Travel between the controller's reported limits, counting as it goes.

    Not a limit seek. It moves to min_pos, then to max_pos, and reports the
    counts actually covered. If the stage stops short of either end the
    controller's limits are wrong, and the shortfall says by how much.

    Returns (span_counts, low_counts, high_counts), or None.
    """
    if not params:
        return None
    lo, hi = params.get('min_counts'), params.get('max_counts')
    if lo is None or hi is None or hi <= lo:
        return None

    stage.move_absolute_counts(axis_id, lo)
    low = wait_until_still(stage, axis_id, sleep=sleep)

    stage.move_absolute_counts(axis_id, hi)
    high = wait_until_still(stage, axis_id, sleep=sleep)

    if low is None or high is None:
        return None
    return high - low, low, high


def _confirm(ask, prompt):
    return str(ask(prompt)).strip().lower() in ('y', 'yes')


def _float_or_none(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def run_calibration(stage, axis_ids=(1, 2), ask=None, say=None,
                    allow_span=False, save_path=None, step_counts=None,
                    sleep=None, save=True):
    """Walk one rig through the available methods and store the outcome.

    Every step that moves the stage is confirmed first. Returns a dict of
    axis -> chosen record, containing only the axes that produced one.

    ask and say default to input and print, but are resolved here rather than
    in the signature: a default argument binds at import, which would capture
    the builtins as they were then and make this impossible to drive from a
    test or from a UI that wants its own prompt.
    """
    ask = ask or input
    say = say or print
    chosen = {}

    for axis_id in axis_ids:
        label = {1: 'X', 2: 'Y'}.get(axis_id, str(axis_id))
        say(f"\n=== Axis {label} ===")

        params = stage.get_stage_axis_params(axis_id)
        if params:
            say(f"  controller reports: part '{params.get('part_no')}' "
                f"serial {params.get('serial_num')} "
                f"cnts_per_unit {params.get('cnts_per_unit')}")
        else:
            say("  controller did not answer REQ_STAGEAXISPARAMS (0x04F1).")

        records = []
        declared = from_controller(params)
        if declared:
            records.append(declared)
            say(f"  [controller] {declared['counts_per_mm']:.1f} counts/mm")
        else:
            say("  [controller] no usable cnts_per_unit")

        # --- span ---
        if allow_span and params:
            travel = default_travel_mm(params, axis_id)
            if travel is None:
                answer = _float_or_none(
                    ask(f"  Datasheet travel for axis {label} in mm "
                        f"(blank to skip): "))
                travel = answer
            if travel:
                say(f"  This drives axis {label} across its full travel.")
                if _confirm(ask, "  Clear of the sample? Proceed? [y/N] "):
                    result = measure_span(stage, axis_id, params, sleep=sleep)
                    if result:
                        span_counts, low, high = result
                        rec = from_span(span_counts, travel)
                        if rec:
                            records.append(rec)
                            say(f"  [span] travelled {span_counts} counts "
                                f"({low} -> {high}) over {travel} mm "
                                f"= {rec['counts_per_mm']:.1f} counts/mm")
                    else:
                        say("  [span] could not read the position counter")

        # --- manual ---
        step = step_counts
        if step is None:
            cnts = (params or {}).get('cnts_per_unit') or LEGACY_COUNTS_PER_MM
            step = int(cnts * 10)      # about 10 mm, on current belief

        say(f"  Manual check moves axis {label} by {step} counts; you then "
            f"measure how far it actually went.")
        if _confirm(ask, "  Do the manual measurement? [y/N] "):
            result = measure_manual(stage, axis_id, step, sleep=sleep)
            if result is None:
                say("  [manual] could not read the position counter")
            else:
                delta, start, end = result
                say(f"  moved {delta} counts ({start} -> {end})")
                if delta != step:
                    say(f"  NOTE: asked for {step}, got {delta} - it may have "
                        f"reached a limit, which would make this invalid.")
                measured = _float_or_none(
                    ask("  Measured distance in mm (blank to skip): "))
                rec = from_manual(delta, measured) if measured else None
                if rec:
                    records.append(rec)
                    say(f"  [manual] {rec['counts_per_mm']:.1f} counts/mm")

        for line in compare(records):
            say(f"  * {line}")

        if not records:
            say(f"  Nothing determined for axis {label}.")
            continue

        # Best evidence wins: a measurement over a derivation over a claim.
        rank = {SOURCE_MANUAL: 0, SOURCE_SPAN: 1, SOURCE_CONTROLLER: 2}
        best = sorted(records, key=lambda r: rank.get(r['source'], 9))[0]
        say(f"  -> using {best['counts_per_mm']:.1f} counts/mm "
            f"({best['source']})")

        chosen[axis_id] = best
        if save:
            path = save_calibration(rig_key(params, axis_id), best, save_path)
            say(f"  saved to {path}")

    return chosen


def describe_source(counts_per_mm, source):
    """One line for a UI, honest about how much the number is worth."""
    note = {
        SOURCE_MANUAL: 'measured on this rig',
        SOURCE_SPAN: 'from limit-to-limit travel',
        SOURCE_CONTROLLER: 'declared by the controller',
        SOURCE_LEGACY: 'UNCALIBRATED fallback - run pystage --calibrate',
    }.get(source, source)
    return f"{counts_per_mm:.1f} counts/mm ({note})"
