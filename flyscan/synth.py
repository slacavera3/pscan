"""Synthetic ASOPS records: what the scope would hand back, without a laser.

Primary output is segmented, because that is how the acquisition works. The
scope is triggered by the 10 kHz ASOPS clock, once per delay sweep, so in
Sequence mode one acquisition holds N segments - one per sweep - each
capturing the useful part of the 12.5 ns delay window rather than all of it.
`synth_segmented()` produces exactly that shape:

    ac        (N, P)      volts, one row per sweep
    dc        (N, P_dc)   probe DC, sparsed
    trigger   (N, P_tr)   BBD303 position-trigger train, sparsed
    trigtime  (N, 2)      [trigger_time_s, trigger_offset_s] per segment

matching WAVEDESC's TRIGTIME_ARRAY. The convention used here, and the one the
post-processing assumes: sample j of segment i sits at lab time

    trigtime[i, 0] + trigtime[i, 1] + j / sample_rate

where column 0 is the trigger time relative to the first trigger of the
acquisition and column 1 is the offset from that trigger to the segment's
first sample. Column 1 is not constant: the digitiser's sample clock is not
locked to the trigger, so each segment starts at a different sub-sample
phase. That is the whole reason the column exists, and ignoring it leaves a
sub-sample misalignment between segments that blurs any average over them.

`synth_flat()` produces the secondary form - one long continuous record per
line, no scope-side segmentation. That is how the signal gets found in the
first place: capture a whole sweep once and look at where the coincidence
actually sits inside it. The post-processing takes either.

The pathologies, all switchable, all checkable against truth:

  dropped_sweeps     a missed trigger: the segment is absent, and TRIGTIME
                     has a double gap where it should have been
  extra_triggers     a spurious trigger off the ASOPS phase: an extra
                     segment whose copeak is not where it should be
  spurious_peaks     an extra peak inside the waveform (flat mode) or inside
                     a segment, with no Brillouin tail
  copeak_drift       the copeak walking through the capture window, i.e.
                     trigger timing drift against the offset lock
  trigger_missing    a lost BBD303 position pulse
  ripple             velocity ripple written into the motion itself, so it
                     reaches TRIGTIME, the trigger channel and the DC channel
                     alike - and must be recovered, not smoothed away

Two modelling choices worth knowing about, because they are physics rather
than convenience.

Amplitudes multiply in the right order. A common gain (laser power, focus)
scales AC and DC alike; sample reflectivity R(x) scales both as well; only
the Brillouin modulation depth is AC-only. So dR/R = AC/DC removes power
drift and reflectivity and leaves the modulation depth - which is what the
normalisation is for, and what the verification checks.

The sample moves during a sweep. Delay d is measured at lab time
t_copeak + d * stretch, so a trace is spread over its capture window in lab
time and therefore over a slice of the smear in space. Amplitudes are
evaluated per sample for that reason, not once per trace.

A note on the coincidence peak width, the one default that is a judgement
rather than a convenience. The detector sees an 80 MHz pulse train; the
envelope carrying the pump-probe signal is recovered by low-pass filtering
well below 80 MHz, and that filter - not the ~1 ps optical cross-correlation
- sets how wide the copeak looks in lab time. The default `peak_sigma_s` of
200 ns is about 4.7 samples of FWHM at 10 MS/s. If the real filter is much
faster the copeak lands inside a single sample, its timing becomes
sampling-phase limited, and the drift statistic stops meaning anything.
"""

from dataclasses import dataclass, field, replace
import math

import numpy as np

# The role names live in budget.py, which owns them; they are re-exported
# here so `synth.ROLE_AC` keeps working for anything that reaches for it.
from .budget import (AsopsTiming, ROLE_AC, ROLE_DC,  # noqa: F401
                     ROLE_POSITION_ANALOG, ROLE_TRIGGER, ROLE_TRIGTIME)


@dataclass(frozen=True)
class SynthConfig:
    """Everything about a synthetic line. All times are lab time."""

    # --- geometry and motion ---------------------------------------------
    line_length_um: float = 100.0
    velocity_mm_s: float = 10.0
    direction: int = 1          # +1 or -1
    x_start_um: float = 0.0     # position where the record begins

    # --- digitiser --------------------------------------------------------
    sample_rate_hz: float = 10.0e6
    timing: AsopsTiming = field(default_factory=AsopsTiming)

    # --- segment capture window (Sequence mode) --------------------------
    # Delay captured per segment, and how much of it is before the copeak.
    segment_window_real_s: float = 2.0e-9
    segment_pre_delay_s: float = 0.2e-9
    # The scope triggers on the ASOPS clock, not on the coincidence, so the
    # copeak sits at a fixed offset inside the window - plus whatever the
    # trigger timing does over the line.
    copeak_jitter_s: float = 0.0          # rms, independent per segment
    copeak_drift_s_per_s: float = 0.0     # lab seconds of walk per lab second
    # Sample clock not locked to the trigger: each segment starts at its own
    # sub-sample phase, recorded in TRIGTIME column 1.
    sample_clock_async: bool = True

    # --- the AC signal ----------------------------------------------------
    peak_amplitude_v: float = 0.5
    peak_sigma_s: float = 2.0e-7
    brillouin_real_hz: float = 5.0e9
    brillouin_amplitude_v: float = 0.02
    brillouin_decay_real_s: float = 1.0e-9   # decay time in DELAY, not lab
    brillouin_phase_rad: float = 0.0

    # --- structure along the line ----------------------------------------
    # A localised feature in the Brillouin modulation depth (AC only), used
    # to check that forward and reverse lines put the same thing in the same
    # place. NaN centre means a featureless line.
    feature_center_um: float = float("nan")
    feature_width_um: float = 5.0
    feature_amplitude_contrast: float = 2.0  # modulation multiplier at centre
    feature_freq_shift_hz: float = 0.0       # real-frequency shift at centre
    # Laser power / focus drift. Scales AC and DC together, so dR/R should
    # remove it entirely.
    amplitude_tilt_per_um: float = 0.0
    # Sample reflectivity. Scales AC and DC together as well.
    dc_reflectivity_contrast: float = 0.0
    dc_reflectivity_center_um: float = float("nan")
    dc_reflectivity_width_um: float = 5.0
    dc_tilt_per_um: float = 0.0

    # --- non-idealities ---------------------------------------------------
    noise_v: float = 0.002
    baseline_drift_v: float = 0.0
    baseline_drift_hz: float = 50.0
    baseline_tilt_v_per_s: float = 0.0
    trigger_clock_jitter_s: float = 0.0   # rms jitter of the 10 kHz trigger

    # --- pathologies ------------------------------------------------------
    dropped_sweeps: tuple = ()      # sweep indices with no trigger at all
    extra_triggers: tuple = ()      # fractional sweep positions, e.g. 7.4
    spurious_peaks: tuple = ()      # fractional sweep positions, e.g. 7.5
    spurious_amplitude_frac: float = 0.6
    # Flat mode only: start/end the record mid-sweep so an edge copeak is cut.
    clip_start_peak: bool = False
    clip_end_peak: bool = False
    first_peak_offset_s: float = float("nan")

    # --- probe DC channel -------------------------------------------------
    dc_enabled: bool = True
    dc_gain_v: float = 0.2       # DC volts at unit reflectivity and gain
    dc_offset_v: float = 0.0
    dc_sparsing: int = 20
    dc_noise_v: float = 0.0005

    # --- BBD303 position-trigger channel ---------------------------------
    trigger_enabled: bool = True
    # 0.6 um at 10 mm/s is 0.6 sweeps per pulse: not a simple fraction of the
    # sweep period, so pulses walk through the sweep phase rather than always
    # arriving at the same one or two phases, where they would either never be
    # captured or be captured only when the ripple allowed it.
    trigger_interval_um: float = 0.6
    trigger_pulse_width_s: float = 20.0e-6
    trigger_amplitude_v: float = 3.3
    trigger_sparsing: int = 20
    trigger_edge_jitter_s: float = 0.0
    trigger_missing: tuple = ()   # indices (0-based, pulse order) to drop
    trigger_noise_v: float = 0.0
    # Delay of the trigger channel behind true stage position (cable,
    # filter). The classic cause of a forward/reverse offset.
    trigger_latency_s: float = 0.0

    # --- velocity ripple --------------------------------------------------
    position_ripple_um: float = 0.0
    position_ripple_hz: float = 300.0
    position_ripple_phase_rad: float = 0.0

    # --- digitisation ------------------------------------------------------
    quantise_bits: int = 0       # 0 = keep float volts; 12 = emulate the ADC
    full_scale_v: float = 1.0
    seed: int = 0

    def window_lab_s(self) -> float:
        return self.timing.lab_time(self.segment_window_real_s)

    def pre_lab_s(self) -> float:
        return self.timing.lab_time(self.segment_pre_delay_s)

    def points_per_segment(self) -> int:
        return max(int(round(self.window_lab_s() * self.sample_rate_hz)), 8)


@dataclass
class SegmentedRecord:
    """One line as the scope returns it in Sequence mode."""

    ac: np.ndarray                  # (N, P) volts
    trigtime: np.ndarray            # (N, 2) [trigger_time_s, offset_s]
    sample_rate_hz: float
    dc: np.ndarray = None           # (N, P_dc) volts
    dc_sparsing: int = 1
    trigger: np.ndarray = None      # (N, P_tr) volts
    trigger_sparsing: int = 1
    direction: int = 1
    velocity_mm_s: float = float("nan")
    config: SynthConfig = None
    truth: dict = field(default_factory=dict)

    @property
    def n_segments(self) -> int:
        return int(self.ac.shape[0])

    @property
    def points_per_segment(self) -> int:
        return int(self.ac.shape[1])

    def segment_times_s(self) -> np.ndarray:
        """(N, P) lab time of every sample, by the TRIGTIME convention."""
        j = np.arange(self.points_per_segment) / self.sample_rate_hz
        start = self.trigtime[:, 0] + self.trigtime[:, 1]
        return start[:, None] + j[None, :]


@dataclass
class FlatRecord:
    """One line as one continuous record - the diagnostic form."""

    ac: np.ndarray                  # (n,) volts
    sample_rate_hz: float
    dc: np.ndarray = None
    dc_sparsing: int = 1
    trigger: np.ndarray = None
    trigger_sparsing: int = 1
    t0_s: float = 0.0
    direction: int = 1
    velocity_mm_s: float = float("nan")
    config: SynthConfig = None
    truth: dict = field(default_factory=dict)

    @property
    def dc_dt_s(self) -> float:
        return self.dc_sparsing / self.sample_rate_hz

    @property
    def trigger_dt_s(self) -> float:
        return self.trigger_sparsing / self.sample_rate_hz


# ---------------------------------------------------------------------------
# Motion and sample model
# ---------------------------------------------------------------------------

def true_position_um(cfg: SynthConfig, t_s) -> np.ndarray:
    """Where the stage actually is at lab time `t_s`, ripple included."""
    t = np.asarray(t_s, dtype=float)
    velocity_um_s = cfg.velocity_mm_s * 1.0e3
    ripple = 0.0
    if cfg.position_ripple_um:
        ripple = cfg.position_ripple_um * np.sin(
            2.0 * math.pi * cfg.position_ripple_hz * t
            + cfg.position_ripple_phase_rad)
    return cfg.x_start_um + cfg.direction * (velocity_um_s * t + ripple)


def _gauss(x_um, centre_um, width_um):
    x = np.asarray(x_um, dtype=float)
    if not math.isfinite(centre_um):
        return np.zeros_like(x)
    dx = (x - centre_um) / width_um
    return np.exp(-0.5 * dx * dx)


def common_gain(cfg: SynthConfig, x_um):
    """Laser power / focus drift. Affects AC and DC equally."""
    return 1.0 + cfg.amplitude_tilt_per_um * (
        np.asarray(x_um, dtype=float) - cfg.x_start_um)


def reflectivity(cfg: SynthConfig, x_um):
    """Sample reflectivity along the line. Affects AC and DC equally."""
    x = np.asarray(x_um, dtype=float)
    return (1.0 + cfg.dc_tilt_per_um * (x - cfg.x_start_um)
            + cfg.dc_reflectivity_contrast * _gauss(
                x, cfg.dc_reflectivity_center_um, cfg.dc_reflectivity_width_um))


def modulation_depth(cfg: SynthConfig, x_um):
    """Brillouin modulation depth along the line. AC only - the real signal."""
    return 1.0 + (cfg.feature_amplitude_contrast - 1.0) * _gauss(
        x_um, cfg.feature_center_um, cfg.feature_width_um)


def dc_volts_at(cfg: SynthConfig, t_s):
    """Probe DC the detector would report at these lab times."""
    x = true_position_um(cfg, t_s)
    return cfg.dc_gain_v * common_gain(cfg, x) * reflectivity(cfg, x) \
        + cfg.dc_offset_v


def _ac_volts_at(cfg: SynthConfig, times, copeaks, peak_scale, osc_scale,
                 lab_hz):
    """AC signal at arbitrary lab times.

    One routine for both acquisition shapes: the flat record and every
    segment are just different sets of sample times. Only the nearest few
    coincidences can contribute to a sample - the copeak is a few hundred
    nanoseconds wide and the oscillation decays within a few microseconds -
    so three candidates either side of the insertion point is generous.
    """
    times = np.asarray(times, dtype=float)
    out = np.zeros(times.shape, dtype=float)
    if copeaks.size == 0:
        return out

    x = true_position_um(cfg, times)
    gain = common_gain(cfg, x) * reflectivity(cfg, x)
    mod = modulation_depth(cfg, x)

    sigma = cfg.peak_sigma_s
    decay_lab = cfg.brillouin_decay_real_s * cfg.timing.stretch_factor
    insert = np.searchsorted(copeaks, times)

    peak_sum = np.zeros(times.shape, dtype=float)
    osc_sum = np.zeros(times.shape, dtype=float)
    for shift in (-2, -1, 0, 1):
        k = insert + shift
        valid = (k >= 0) & (k < copeaks.size)
        kc = np.clip(k, 0, copeaks.size - 1)
        tau = times - copeaks[kc]
        peak_sum += np.where(
            valid, peak_scale[kc] * np.exp(-0.5 * (tau / sigma) ** 2), 0.0)
        osc_sum += np.where(
            valid & (tau > 0.0),
            osc_scale[kc] * np.exp(-np.abs(tau) / decay_lab)
            * np.sin(2.0 * math.pi * lab_hz[kc] * tau
                     + cfg.brillouin_phase_rad),
            0.0)

    out = gain * (cfg.peak_amplitude_v * peak_sum
                  + cfg.brillouin_amplitude_v * mod * osc_sum)
    return out


def _baseline(cfg: SynthConfig, times):
    times = np.asarray(times, dtype=float)
    out = np.zeros(times.shape, dtype=float)
    if cfg.baseline_drift_v:
        out += cfg.baseline_drift_v * np.sin(
            2.0 * math.pi * cfg.baseline_drift_hz * times)
    if cfg.baseline_tilt_v_per_s:
        out += cfg.baseline_tilt_v_per_s * times
    return out


def _quantise(values: np.ndarray, cfg: SynthConfig) -> np.ndarray:
    if not cfg.quantise_bits:
        return values
    full = 2 ** (cfg.quantise_bits - 1) - 1
    lsb = cfg.full_scale_v / full
    return np.clip(np.rint(values / lsb), -full - 1, full) * lsb


def reversed_config(cfg: SynthConfig, **overrides) -> SynthConfig:
    """The same line driven the other way, covering the same ground.

    A forward line starting at x_start and running `line_length_um` in +x is
    reversed by starting at the far end and running back, so the two cover
    the same extent and a feature should land in the same place in both. Any
    further differences - another ripple phase, another noise seed - are
    passed as overrides, because the return pass is not a mirror image of the
    outward one in reality.
    """
    return replace(cfg, direction=-cfg.direction,
                   x_start_um=cfg.x_start_um + cfg.direction * cfg.line_length_um,
                   **overrides)


# ---------------------------------------------------------------------------
# Coincidence lattice shared by both acquisition shapes
# ---------------------------------------------------------------------------

def _coincidence_lattice(cfg: SynthConfig, duration_s, rng, first_offset_s):
    """Lab times of the coincidences, plus the spurious peaks among them.

    Returns (times, peak_scale, osc_scale, lab_hz, sweep_index, is_spurious),
    sorted by time. A spurious peak carries no Brillouin tail: it is an
    electrical artefact, not a coincidence. That is itself a clue the
    post-processing could use one day, and deliberately does not rely on now.
    """
    sweep_period = cfg.timing.sweep_period_s
    n = max(int(math.floor((duration_s - first_offset_s) / sweep_period)) + 1, 1)
    index = np.arange(n)
    times = first_offset_s + index * sweep_period
    if cfg.copeak_jitter_s:
        times = times + rng.normal(0.0, cfg.copeak_jitter_s, n)
    if cfg.copeak_drift_s_per_s:
        times = times + cfg.copeak_drift_s_per_s * (times - times[0])

    dropped = set(int(k) for k in cfg.dropped_sweeps)
    keep = np.array([k not in dropped for k in index], dtype=bool)
    times, index = times[keep], index[keep]

    x = true_position_um(cfg, times)
    real_hz = cfg.brillouin_real_hz + cfg.feature_freq_shift_hz * _gauss(
        x, cfg.feature_center_um, cfg.feature_width_um)
    lab_hz = real_hz / cfg.timing.stretch_factor

    peak_scale = np.ones(times.size)
    osc_scale = np.ones(times.size)
    spurious = np.zeros(times.size, dtype=bool)

    for frac in cfg.spurious_peaks:
        t_sp = first_offset_s + float(frac) * sweep_period
        if not (0.0 <= t_sp < duration_s):
            continue
        times = np.append(times, t_sp)
        index = np.append(index, -1)
        lab_hz = np.append(lab_hz, cfg.brillouin_real_hz
                           / cfg.timing.stretch_factor)
        peak_scale = np.append(peak_scale, cfg.spurious_amplitude_frac)
        osc_scale = np.append(osc_scale, 0.0)
        spurious = np.append(spurious, True)

    order = np.argsort(times)
    return (times[order], peak_scale[order], osc_scale[order], lab_hz[order],
            index[order], spurious[order])


def _trigger_crossings(cfg: SynthConfig, t_axis, x_axis):
    """Times at which the stage crosses each programmed trigger position.

    Found from the motion itself rather than from velocity * time, so
    velocity ripple moves the pulses in time exactly as it would on the
    bench - which is the whole reason the channel is recorded.
    """
    interval = cfg.trigger_interval_um
    if interval <= 0:
        raise ValueError("trigger_interval_um must be positive")
    lo, hi = float(np.min(x_axis)), float(np.max(x_axis))
    first, last = math.ceil(lo / interval), math.floor(hi / interval)
    if last < first:
        return np.array([], dtype=float), np.array([], dtype=float)

    targets = np.arange(first, last + 1) * interval
    positions, times = [], []
    for p in targets:
        delta = x_axis - p
        change = np.flatnonzero(np.sign(delta[:-1]) != np.sign(delta[1:]))
        if change.size == 0:
            continue
        i = int(change[0])
        d0, d1 = delta[i], delta[i + 1]
        frac = 0.0 if d1 == d0 else -d0 / (d1 - d0)
        positions.append(float(p))
        times.append(float(t_axis[i] + frac * (t_axis[i + 1] - t_axis[i])))
    order = np.argsort(times)
    return np.asarray(positions)[order], np.asarray(times)[order]


def _trigger_pulse_train(cfg: SynthConfig, times, edge_times):
    """TTL level at arbitrary lab times, given the pulse edge times."""
    times = np.asarray(times, dtype=float)
    level = np.zeros(times.shape, dtype=float)
    if edge_times.size == 0:
        return level
    insert = np.searchsorted(edge_times, times) - 1
    valid = insert >= 0
    k = np.clip(insert, 0, edge_times.size - 1)
    since = times - edge_times[k]
    high = valid & (since >= 0.0) & (since < cfg.trigger_pulse_width_s)
    level[high] = cfg.trigger_amplitude_v
    return level


def _emitted_edges(cfg: SynthConfig, duration_s, rng):
    """(positions, times) of the pulses the controller actually emits."""
    if not cfg.trigger_enabled:
        return (np.array([], dtype=float), np.array([], dtype=float),
                np.array([], dtype=float))
    # A dense enough grid to find the crossings; the ripple is slow.
    n = max(int(duration_s * 2.0e5), 64)
    t_grid = np.linspace(0.0, duration_s, n)
    x_grid = true_position_um(cfg, t_grid)
    positions, times = _trigger_crossings(cfg, t_grid, x_grid)
    if cfg.trigger_edge_jitter_s and times.size:
        times = times + rng.normal(0.0, cfg.trigger_edge_jitter_s, times.size)
    missing = set(int(i) for i in cfg.trigger_missing)
    emit = np.array([i not in missing for i in range(times.size)], dtype=bool)
    # The channel reports the crossing one latency late.
    return positions, times, times[emit] + cfg.trigger_latency_s


# ---------------------------------------------------------------------------
# Segmented generation (primary)
# ---------------------------------------------------------------------------

def synth_segmented(cfg: SynthConfig = SynthConfig()) -> SegmentedRecord:
    """Build one synthetic line as the scope returns it in Sequence mode."""

    _validate(cfg)
    rng = np.random.default_rng(cfg.seed)
    timing = cfg.timing
    fs = cfg.sample_rate_hz
    dt = 1.0 / fs
    sweep_period = timing.sweep_period_s
    duration = cfg.line_length_um / (cfg.velocity_mm_s * 1.0e3)

    # Coincidences, including any spurious peaks.
    first_offset = (cfg.first_peak_offset_s
                    if math.isfinite(cfg.first_peak_offset_s)
                    else 0.5 * sweep_period)
    (copeaks, peak_scale, osc_scale, lab_hz, sweep_index,
     spurious) = _coincidence_lattice(cfg, duration, rng, first_offset)

    # Triggers: the 10 kHz ASOPS clock, one per sweep, minus any that were
    # missed and plus any spurious ones. A trigger exists independently of
    # whether its coincidence does.
    sweep_period_index = np.arange(
        max(int(math.floor((duration - first_offset) / sweep_period)) + 1, 1))
    dropped = set(int(k) for k in cfg.dropped_sweeps)
    trig_keep = np.array([k not in dropped for k in sweep_period_index],
                         dtype=bool)
    trigger_sweep = sweep_period_index[trig_keep]
    trigger_times = first_offset + trigger_sweep * sweep_period
    if cfg.trigger_clock_jitter_s:
        trigger_times = trigger_times + rng.normal(
            0.0, cfg.trigger_clock_jitter_s, trigger_times.size)

    extra_flags = np.zeros(trigger_times.size, dtype=bool)
    for frac in cfg.extra_triggers:
        t_extra = first_offset + float(frac) * sweep_period
        if not (0.0 <= t_extra < duration):
            continue
        trigger_times = np.append(trigger_times, t_extra)
        trigger_sweep = np.append(trigger_sweep, -1)
        extra_flags = np.append(extra_flags, True)
    order = np.argsort(trigger_times)
    trigger_times = trigger_times[order]
    trigger_sweep = trigger_sweep[order]
    extra_flags = extra_flags[order]

    # The trigger fires at the ASOPS clock edge; the copeak for that sweep
    # arrives `pre_lab` later, so the segment starts `pre_lab` before it.
    pre_lab = cfg.pre_lab_s()
    points = cfg.points_per_segment()
    if cfg.sample_clock_async:
        sub = rng.random(trigger_times.size) * dt
    else:
        sub = np.zeros(trigger_times.size)
    # Column 1 of TRIGTIME: offset from the trigger to the first sample.
    offsets = -pre_lab + sub
    t_first = trigger_times + offsets

    j = np.arange(points) * dt
    times = t_first[:, None] + j[None, :]

    ac = _ac_volts_at(cfg, times, copeaks, peak_scale, osc_scale, lab_hz)
    ac = ac + _baseline(cfg, times)
    if cfg.noise_v:
        ac = ac + rng.normal(0.0, cfg.noise_v, ac.shape)
    ac = _quantise(ac, cfg)

    dc = None
    if cfg.dc_enabled:
        dc_idx = np.arange(0, points, cfg.dc_sparsing)
        dc_times = t_first[:, None] + (dc_idx * dt)[None, :]
        dc = dc_volts_at(cfg, dc_times)
        if cfg.dc_noise_v:
            dc = dc + rng.normal(0.0, cfg.dc_noise_v, dc.shape)
        dc = _quantise(dc, cfg)

    trigger = None
    trig_positions, trig_crossing_times, emitted = _emitted_edges(
        cfg, duration, rng)
    if cfg.trigger_enabled:
        tr_idx = np.arange(0, points, cfg.trigger_sparsing)
        tr_times = t_first[:, None] + (tr_idx * dt)[None, :]
        trigger = _trigger_pulse_train(cfg, tr_times, emitted)
        if cfg.trigger_noise_v:
            trigger = trigger + rng.normal(
                0.0, cfg.trigger_noise_v, trigger.shape)

    # TRIGTIME column 0 is relative to the first trigger of the acquisition.
    t_zero = float(trigger_times[0])
    trigtime = np.column_stack([trigger_times - t_zero, offsets])

    # Truth, per segment.
    copeak_time = np.full(trigger_times.size, np.nan)
    copeak_sample = np.full(trigger_times.size, np.nan)
    for n, k in enumerate(trigger_sweep):
        if k < 0:
            continue
        match = np.flatnonzero((sweep_index == k) & ~spurious)
        if match.size:
            copeak_time[n] = copeaks[match[0]]
            copeak_sample[n] = (copeaks[match[0]] - t_first[n]) * fs
    segment_x = true_position_um(cfg, np.nan_to_num(copeak_time,
                                                    nan=t_first[0]))
    segment_x = np.where(np.isfinite(copeak_time), segment_x, np.nan)

    truth = {
        "shape": "segmented",
        "n_segments": int(trigger_times.size),
        "points_per_segment": points,
        "trigger_times_s": trigger_times - t_zero,
        "trigger_offsets_s": offsets,
        "trigger_sweep_index": trigger_sweep,
        "extra_trigger": extra_flags,
        "n_extra_triggers": int(np.count_nonzero(extra_flags)),
        "dropped_sweeps": tuple(sorted(dropped)),
        "copeak_times_s": copeak_time - t_zero,
        "copeak_sample": copeak_sample,
        "segment_positions_um": segment_x,
        "modulation_depth": modulation_depth(cfg, segment_x),
        "reflectivity": reflectivity(cfg, segment_x),
        "common_gain": common_gain(cfg, segment_x),
        "spurious_times_s": copeaks[spurious] - t_zero,
        "sweep_period_s": sweep_period,
        "duration_s": duration,
        "t_zero_s": t_zero,
        "first_peak_offset_s": first_offset,
        "pre_lab_s": pre_lab,
        "nominal_copeak_sample": pre_lab * fs,
        "feature_center_um": cfg.feature_center_um,
        "x_start_um": cfg.x_start_um,
        # Position at the origin of the TRIGTIME time base - the first
        # trigger, not the start of the motion. This is what
        # PositionConfig.x_start_um wants.
        "x_at_t_zero_um": float(true_position_um(cfg, t_zero)),
        "x_end_um": float(true_position_um(cfg, duration)),
        "smear_per_sweep_um": timing.travel_per_sweep_um(cfg.velocity_mm_s),
        "trigger_positions_um": trig_positions,
        "trigger_crossing_times_s": trig_crossing_times - t_zero,
        "n_trigger_pulses": int(trig_positions.size),
        "trigger_missing": tuple(sorted(int(i) for i in cfg.trigger_missing)),
        "trigger_latency_s": cfg.trigger_latency_s,
        "position_ripple_um": cfg.position_ripple_um,
        "position_ripple_hz": cfg.position_ripple_hz,
    }

    return SegmentedRecord(
        ac=ac, trigtime=trigtime, sample_rate_hz=fs, dc=dc,
        dc_sparsing=cfg.dc_sparsing, trigger=trigger,
        trigger_sparsing=cfg.trigger_sparsing, direction=cfg.direction,
        velocity_mm_s=cfg.velocity_mm_s, config=cfg, truth=truth)


# ---------------------------------------------------------------------------
# Flat generation (secondary, for diagnostics)
# ---------------------------------------------------------------------------

def synth_flat(cfg: SynthConfig = SynthConfig()) -> FlatRecord:
    """Build one synthetic line as a single continuous record.

    This is the form used to find out where the coincidence sits inside the
    12.5 ns window in the first place, so it captures whole sweeps and does
    not use the segment window at all.
    """
    _validate(cfg)
    rng = np.random.default_rng(cfg.seed)
    timing = cfg.timing
    fs = cfg.sample_rate_hz
    dt = 1.0 / fs
    sweep_period = timing.sweep_period_s
    nominal_duration = cfg.line_length_um / (cfg.velocity_mm_s * 1.0e3)

    if math.isfinite(cfg.first_peak_offset_s):
        first_offset = cfg.first_peak_offset_s
    elif cfg.clip_start_peak:
        first_offset = -2.0 * cfg.peak_sigma_s   # apex before the record
    else:
        first_offset = 0.5 * sweep_period

    (copeaks, peak_scale, osc_scale, lab_hz, sweep_index,
     spurious) = _coincidence_lattice(cfg, nominal_duration, rng, first_offset)

    duration = nominal_duration
    if cfg.clip_end_peak and copeaks.size:
        real = copeaks[~spurious]
        if real.size:
            duration = float(real[-1] + 1.5 * cfg.peak_sigma_s)

    n_samples = int(round(duration * fs))
    if n_samples < 64:
        raise ValueError(
            f"record would be {n_samples} samples long; raise line_length_um "
            f"or the sample rate")
    t_axis = np.arange(n_samples) * dt

    ac = _ac_volts_at(cfg, t_axis, copeaks, peak_scale, osc_scale, lab_hz)
    ac = ac + _baseline(cfg, t_axis)
    if cfg.noise_v:
        ac = ac + rng.normal(0.0, cfg.noise_v, n_samples)
    ac = _quantise(ac, cfg)

    dc = None
    if cfg.dc_enabled:
        idx = np.arange(0, n_samples, cfg.dc_sparsing)
        dc = dc_volts_at(cfg, idx * dt)
        if cfg.dc_noise_v:
            dc = dc + rng.normal(0.0, cfg.dc_noise_v, dc.size)
        dc = _quantise(dc, cfg)

    trigger = None
    trig_positions, trig_times, emitted = _emitted_edges(cfg, duration, rng)
    if cfg.trigger_enabled:
        idx = np.arange(0, n_samples, cfg.trigger_sparsing)
        trigger = _trigger_pulse_train(cfg, idx * dt, emitted)
        if cfg.trigger_noise_v:
            trigger = trigger + rng.normal(0.0, cfg.trigger_noise_v,
                                           trigger.size)

    real_mask = ~spurious
    apex_in_record = (copeaks >= 0.0) & (copeaks < duration)
    truth = {
        "shape": "flat",
        "n_samples": n_samples,
        "duration_s": duration,
        "copeak_times_s": copeaks[real_mask],
        "copeak_samples": copeaks[real_mask] * fs,
        "sweep_index": sweep_index[real_mask],
        "apex_in_record": apex_in_record[real_mask],
        "n_peaks": int(np.count_nonzero(real_mask)),
        "n_peaks_in_record": int(np.count_nonzero(real_mask & apex_in_record)),
        "copeak_positions_um": true_position_um(cfg, copeaks[real_mask]),
        "spurious_times_s": copeaks[spurious],
        "dropped_sweeps": tuple(sorted(int(k) for k in cfg.dropped_sweeps)),
        "sweep_period_s": sweep_period,
        "first_peak_offset_s": first_offset,
        "feature_center_um": cfg.feature_center_um,
        "x_start_um": cfg.x_start_um,
        # A flat record's time base starts at the record itself, so the two
        # agree here.
        "x_at_t_zero_um": cfg.x_start_um,
        "x_end_um": float(true_position_um(cfg, duration)),
        "smear_per_sweep_um": timing.travel_per_sweep_um(cfg.velocity_mm_s),
        "trigger_positions_um": trig_positions,
        "trigger_crossing_times_s": trig_times,
        "n_trigger_pulses": int(trig_positions.size),
        "trigger_missing": tuple(sorted(int(i) for i in cfg.trigger_missing)),
        "trigger_latency_s": cfg.trigger_latency_s,
        "position_ripple_um": cfg.position_ripple_um,
        "position_ripple_hz": cfg.position_ripple_hz,
    }

    return FlatRecord(
        ac=ac, sample_rate_hz=fs, dc=dc, dc_sparsing=cfg.dc_sparsing,
        trigger=trigger, trigger_sparsing=cfg.trigger_sparsing, t0_s=0.0,
        direction=cfg.direction, velocity_mm_s=cfg.velocity_mm_s, config=cfg,
        truth=truth)


def _validate(cfg: SynthConfig):
    if cfg.direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    if cfg.velocity_mm_s <= 0:
        raise ValueError("velocity_mm_s must be positive")
    if cfg.segment_window_real_s <= 0:
        raise ValueError("segment_window_real_s must be positive")
    for name, value in (("dc_sparsing", cfg.dc_sparsing),
                        ("trigger_sparsing", cfg.trigger_sparsing)):
        if value < 1:
            raise ValueError(f"{name} must be >= 1")
