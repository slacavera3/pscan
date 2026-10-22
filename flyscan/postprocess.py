"""Post-processing for an ASOPS fly-scan line.

The acquisition is triggered by the 10 kHz ASOPS clock, so the scope hands
back one segment per delay sweep, already aligned in delay, with a TRIGTIME
array giving the exact trigger time of each segment. The chain is therefore:

    segments + TRIGTIME + DC + position-trigger channel
      -> TRIGTIME diagnostics: sweep spacing, missed and extra triggers
      -> copeak validation: is the coincidence where the window expects it,
         and is it walking (trigger timing drift)?
      -> align each segment on its own copeak and cut a common window
      -> one scalar probe DC per copeak; divide to get dR/R
      -> an absolute position per segment
      -> a regular spatial grid, several sweeps averaged per pixel

Peak finding has not gone away, it has changed job. It no longer has to carve
a flat record into sweeps - the scope does that now - so what is left is the
two checks that matter: that the copeak lands where the capture window
expects it in every segment, and that the triggers themselves are regular.
The first catches a mis-positioned or drifting window, the second catches a
missed or spurious trigger. A flat record can still be processed
(`process_flat`), because capturing one whole sweep is how the copeak's
position inside the 12.5 ns window gets found in the first place; that path
finds peaks the hard way and then joins the same chain.

Everything a line learned - including what went wrong - comes back in a
`LineResult`, and `LineResult.report()` prints it. The report is meant to be
read per line while a map runs: TRIGTIME spacing is a live diagnostic of the
offset lock and the scope timebase, copeak drift is a live diagnostic of the
trigger path, and sweeps-per-pixel says whether the map being built is the
one that was asked for.
"""

from bisect import bisect_left, insort
from dataclasses import dataclass, field, asdict
import math

import numpy as np

from .budget import AsopsTiming

MAD_TO_SIGMA = 1.4826


# ===========================================================================
# A lattice fit, used three times over
# ===========================================================================

@dataclass
class LatticeFit:
    """A fit of `position = offset + spacing * index` to irregular events.

    Three things in this pipeline are regular events with occasional missing
    and occasional spurious members: coincidence peaks in a flat record,
    triggers in TRIGTIME, and position pulses from the BBD303. All three want
    the same treatment - fit the lattice, judge each event by its residual,
    report sites with nothing on them as missing rather than renumbering
    everything after them. One missing event that silently shifted the
    numbering would misplace every pixel after it.
    """

    offset: float
    spacing: float
    index: np.ndarray            # lattice index per input event
    residual: np.ndarray         # event position minus its lattice site
    outlier: np.ndarray          # bool: too far off the lattice to belong
    missing_index: tuple         # lattice sites with no event
    n_inliers: int
    spacing_mean: float = float("nan")
    spacing_median: float = float("nan")
    spacing_std: float = float("nan")
    spacing_min: float = float("nan")
    spacing_max: float = float("nan")
    residual_rms: float = float("nan")
    warnings: list = field(default_factory=list)


def fit_lattice(positions, nominal_spacing, tolerance=0.25,
                nominal_window=0.3, iterations=3, exclude=None,
                reseed=True) -> LatticeFit:
    """Fit a regular lattice to `positions` and classify every entry.

    `nominal_spacing` is used as a prior, to pick which observed gaps are
    plausibly one step rather than two: entries within `nominal_window` of it
    seed the fit. Nothing downstream assumes the nominal value is right - the
    fitted spacing is what gets reported, and the difference between them is
    a measurement.

    `reseed` decides what happens when no gap looks like one nominal step.
    With True, the median observed gap becomes the step, which is right when
    every event should have been seen and the nominal value is simply wrong.
    With False, the nominal step is kept and the observed gaps are read as
    integer multiples of it - which is what the BBD303 position pulses need,
    because in Sequence mode only a fraction of them are ever digitised and
    the ones that are still sit on the original interval lattice. Reseeding
    there would silently halve (or third) the distance between anchors, and
    so the whole map's scale.

    `exclude` marks entries that should not influence the fit (a clipped peak
    at a record edge, say) but should still be indexed.
    """
    positions = np.asarray(positions, dtype=np.float64)
    n = positions.size
    empty = np.zeros(0, dtype=np.int64)
    if n == 0:
        return LatticeFit(0.0, float(nominal_spacing), empty,
                          np.zeros(0), np.zeros(0, dtype=bool), (), 0)

    exclude = (np.zeros(n, dtype=bool) if exclude is None
               else np.asarray(exclude, dtype=bool))
    order = np.argsort(positions)
    warnings = []

    usable = positions[order][~exclude[order]]
    if usable.size < 3:
        warnings.append(
            f"Only {usable.size} usable events; no lattice could be fitted, "
            f"so spacing statistics and missing-event detection are "
            f"unavailable.")
        index = np.arange(n, dtype=np.int64)
        return LatticeFit(
            float(positions[order][0]), float(nominal_spacing), index,
            np.zeros(n), np.zeros(n, dtype=bool), (), int(n),
            warnings=warnings)

    # Seed: the median gap that looks like a single step.
    gaps = np.diff(usable)
    near = gaps[(gaps > (1.0 - nominal_window) * nominal_spacing)
                & (gaps < (1.0 + nominal_window) * nominal_spacing)]
    if near.size:
        spacing = float(np.median(near))
    elif reseed:
        spacing = float(np.median(gaps))
        warnings.append(
            f"No gap within {nominal_window * 100:.0f}% of the nominal "
            f"{nominal_spacing:.6g}; the lattice was seeded from the median "
            f"observed gap ({spacing:.6g}) instead. Check the nominal value.")
    else:
        spacing = float(nominal_spacing)
        steps = np.median(gaps) / spacing
        warnings.append(
            f"No gap within {nominal_window * 100:.0f}% of the nominal "
            f"{nominal_spacing:.6g}: the median gap is {steps:.2f} steps, so "
            f"roughly one event in {max(round(steps), 1):.0f} was seen. The "
            f"nominal step was kept and the gaps read as integer multiples "
            f"of it.")
    offset = float(usable[0])

    for _ in range(max(int(iterations), 1)):
        k = np.rint((usable - offset) / spacing)
        if np.unique(k).size < 2:
            break
        slope, intercept = np.polyfit(k, usable, 1)
        if not (math.isfinite(slope) and math.isfinite(intercept)) or slope <= 0:
            break
        spacing, offset = float(slope), float(intercept)

    index = np.rint((positions - offset) / spacing).astype(np.int64)
    residual = positions - (offset + spacing * index)
    outlier = (np.abs(residual) > tolerance * spacing) & ~exclude

    # Two events on one site: keep whichever sits closer to it.
    seen = {}
    for i in np.argsort(positions):
        if outlier[i] or exclude[i]:
            continue
        k = int(index[i])
        if k in seen:
            loser = i if abs(residual[i]) > abs(residual[seen[k]]) else seen[k]
            winner = seen[k] if loser == i else i
            outlier[loser] = True
            seen[k] = winner
        else:
            seen[k] = i

    inlier = ~outlier & ~exclude
    fit = LatticeFit(offset=offset, spacing=spacing, index=index,
                     residual=residual, outlier=outlier, missing_index=(),
                     n_inliers=int(np.count_nonzero(inlier)),
                     warnings=warnings)

    if fit.n_inliers >= 2:
        k_in = np.sort(index[inlier])
        # Number from the first inlier, so index 0 is the first sweep of this
        # line and a reported missing index means something to a reader.
        shift = int(k_in[0])
        fit.index = index - shift
        fit.offset = offset + spacing * shift
        k_in = k_in - shift
        expected = np.arange(k_in[0], k_in[-1] + 1)
        missing = np.setdiff1d(expected, k_in)
        fit.missing_index = tuple(int(k) for k in missing)

        pos_in = np.sort(positions[inlier])
        consecutive = np.diff(k_in) == 1
        step = np.diff(pos_in)
        good = step[consecutive] if np.any(consecutive) else step
        fit.spacing_mean = float(np.mean(good))
        fit.spacing_median = float(np.median(good))
        fit.spacing_std = float(np.std(good, ddof=1)) if good.size > 1 else 0.0
        fit.spacing_min = float(np.min(good))
        fit.spacing_max = float(np.max(good))
        res_in = positions[inlier] - (fit.offset + fit.spacing * fit.index[inlier])
        fit.residual = positions - (fit.offset + fit.spacing * fit.index)
        fit.residual_rms = (float(np.std(res_in, ddof=1))
                            if res_in.size > 1 else 0.0)
    return fit


# ===========================================================================
# TRIGTIME diagnostics
# ===========================================================================

@dataclass
class TrigtimeStats:
    """What the trigger times say about the offset lock and the timebase."""

    n_segments: int = 0
    duration_s: float = float("nan")
    spacing_mean_s: float = float("nan")
    spacing_median_s: float = float("nan")
    spacing_std_s: float = float("nan")
    spacing_min_s: float = float("nan")
    spacing_max_s: float = float("nan")
    fitted_spacing_s: float = float("nan")
    implied_sweep_rate_hz: float = float("nan")
    nominal_sweep_rate_hz: float = float("nan")
    sweep_rate_error_hz: float = float("nan")
    sweep_rate_error_ppm: float = float("nan")
    jitter_rms_s: float = float("nan")
    jitter_rms_delay_s: float = float("nan")
    jitter_frac_of_sweep: float = float("nan")
    n_missed_triggers: int = 0
    missed_sweep_indices: tuple = ()
    n_extra_triggers: int = 0
    extra_segment_indices: tuple = ()
    first_sweep_index: int = 0
    last_sweep_index: int = 0
    offset_mean_s: float = float("nan")
    offset_spread_s: float = float("nan")
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def report(self) -> str:
        return format_trigtime_stats(self)


def analyse_trigtime(trigtime, timing: AsopsTiming = AsopsTiming(),
                     tolerance: float = 0.25):
    """Index the segments by sweep number and report on the trigger train.

    Returns (stats, LatticeFit). The fit's `index` is the sweep number of
    each segment, counted from the first good trigger of this line, and is
    what keeps a missed trigger from renumbering everything after it.
    """
    trigtime = np.atleast_2d(np.asarray(trigtime, dtype=np.float64))
    if trigtime.shape[1] < 1:
        raise ValueError("trigtime must have at least one column")
    times = trigtime[:, 0]
    offsets = trigtime[:, 1] if trigtime.shape[1] > 1 else np.zeros(times.size)

    stats = TrigtimeStats()
    stats.n_segments = int(times.size)
    stats.nominal_sweep_rate_hz = timing.sweep_rate_hz
    stats.offset_mean_s = float(np.mean(offsets)) if offsets.size else float("nan")
    stats.offset_spread_s = (float(np.max(offsets) - np.min(offsets))
                             if offsets.size else float("nan"))
    if times.size:
        stats.duration_s = float(times[-1] - times[0])

    fit = fit_lattice(times, timing.sweep_period_s, tolerance=tolerance)
    stats.warnings.extend(fit.warnings)

    stats.fitted_spacing_s = fit.spacing
    stats.spacing_mean_s = fit.spacing_mean
    stats.spacing_median_s = fit.spacing_median
    stats.spacing_std_s = fit.spacing_std
    stats.spacing_min_s = fit.spacing_min
    stats.spacing_max_s = fit.spacing_max
    stats.n_missed_triggers = len(fit.missing_index)
    stats.missed_sweep_indices = fit.missing_index
    stats.n_extra_triggers = int(np.count_nonzero(fit.outlier))
    stats.extra_segment_indices = tuple(
        int(i) for i in np.flatnonzero(fit.outlier))
    inlier = ~fit.outlier
    if np.any(inlier):
        stats.first_sweep_index = int(np.min(fit.index[inlier]))
        stats.last_sweep_index = int(np.max(fit.index[inlier]))

    if math.isfinite(stats.spacing_mean_s) and stats.spacing_mean_s > 0:
        stats.implied_sweep_rate_hz = 1.0 / stats.spacing_mean_s
        stats.sweep_rate_error_hz = (
            stats.implied_sweep_rate_hz - timing.sweep_rate_hz)
        stats.sweep_rate_error_ppm = (
            1e6 * stats.sweep_rate_error_hz / timing.sweep_rate_hz)
    if math.isfinite(fit.residual_rms):
        stats.jitter_rms_s = fit.residual_rms
        stats.jitter_rms_delay_s = fit.residual_rms / timing.stretch_factor
        stats.jitter_frac_of_sweep = fit.residual_rms / fit.spacing

    if stats.n_missed_triggers:
        stats.warnings.append(
            f"{stats.n_missed_triggers} missed trigger(s) at sweep index "
            f"{stats.missed_sweep_indices}. Sweep numbering is preserved "
            f"across the gap, so later segments are NOT shifted; those sweeps "
            f"simply contribute nothing. A missed trigger means the scope was "
            f"not ready - check the re-arm time against the 100 us between "
            f"ASOPS triggers.")
    if stats.n_extra_triggers:
        stats.warnings.append(
            f"{stats.n_extra_triggers} segment(s) triggered off the ASOPS "
            f"lattice, at segment index {stats.extra_segment_indices}. Their "
            f"capture window does not sit on a coincidence, so they are "
            f"dropped rather than averaged in.")
    if abs(stats.sweep_rate_error_ppm) > 1000.0:
        stats.warnings.append(
            f"Measured trigger rate {stats.implied_sweep_rate_hz:,.4f} Hz is "
            f"{stats.sweep_rate_error_ppm:,.0f} ppm from the nominal "
            f"{timing.sweep_rate_hz:,.1f} Hz. Either the offset lock has "
            f"moved or the scope timebase is not what it claims; both "
            f"rescale the delay axis, since delay = lab time / stretch.")
    if (math.isfinite(stats.offset_spread_s)
            and stats.offset_spread_s > 0.5 / max(timing.sweep_rate_hz, 1.0)):
        stats.warnings.append(
            "TRIGTIME offsets vary by more than half a sweep period, which is "
            "not a sample-clock phase. Check the TRIGTIME column convention "
            "before trusting the segment time base.")
    return stats, fit


# ===========================================================================
# Copeak location and validation
# ===========================================================================

@dataclass(frozen=True)
class CopeakConfig:
    """How the coincidence peak is located inside a segment."""

    # Detection threshold in robust noise sigmas above the segment baseline.
    threshold_sigma: float = 8.0
    # Search only this fraction of the segment, from the start. 1.0 searches
    # all of it. Narrowing it helps when a spurious peak sits later in the
    # window, at the cost of refusing to find a badly drifted copeak.
    search_fraction: float = 1.0
    refine_subsample: bool = True
    # Warn if the copeak sits within this many samples of either window edge:
    # at that point the window is mis-positioned and the trace is clipped.
    edge_margin_samples: float = 4.0
    # Warn below this many samples of FWHM: narrower and the copeak position
    # is limited by sampling phase, not by the instrument.
    min_fwhm_samples: float = 3.0
    # Warn if the copeak walks more than this fraction of the window over the
    # line.
    max_drift_fraction: float = 0.1


@dataclass
class CopeakStats:
    """Per-line validation of where the coincidence landed."""

    n_segments: int = 0
    n_found: int = 0
    n_missing: int = 0
    missing_segment_indices: tuple = ()
    expected_sample: float = float("nan")
    median_sample: float = float("nan")
    offset_samples: float = float("nan")
    offset_delay_s: float = float("nan")
    scatter_samples: float = float("nan")
    scatter_delay_s: float = float("nan")
    drift_samples_per_segment: float = float("nan")
    drift_samples_total: float = float("nan")
    drift_delay_total_s: float = float("nan")
    drift_fraction_of_window: float = float("nan")
    fwhm_samples_median: float = float("nan")
    amplitude_median_v: float = float("nan")
    amplitude_min_v: float = float("nan")
    amplitude_max_v: float = float("nan")
    noise_sigma_v: float = float("nan")
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def report(self) -> str:
        return format_copeak_stats(self)


def _parabolic_offset(y_prev, y, y_next) -> float:
    """Sub-sample apex offset from a 3-point parabola, clipped to +-1."""
    denom = y_prev - 2.0 * y + y_next
    if denom == 0.0:
        return 0.0
    return float(np.clip(0.5 * (y_prev - y_next) / denom, -1.0, 1.0))


def _fwhm_samples(trace, apex, amplitude, limit) -> float:
    if amplitude <= 0:
        return float("nan")
    half = 0.5 * amplitude
    left = apex
    while left > max(apex - limit, 0) and trace[left] > half:
        left -= 1
    right = apex
    while right < min(apex + limit, trace.size - 1) and trace[right] > half:
        right += 1
    return float(right - left)


def find_copeaks(segments, expected_sample=float("nan"),
                 config: CopeakConfig = CopeakConfig(),
                 stretch_factor: float = float("nan")):
    """Locate the copeak in every segment and judge the result.

    `segments` is (N, P). Returns (samples, amplitudes, stats), with NaN in
    `samples` for a segment where no copeak was found.
    """
    data = np.atleast_2d(np.asarray(segments, dtype=np.float64))
    n, points = data.shape
    stats = CopeakStats(n_segments=int(n))
    stats.expected_sample = float(expected_sample)

    # One noise estimate for the whole line: a single segment is short, and
    # its own MAD is dominated by the signal in it.
    flat = data.reshape(-1)
    baseline = np.median(flat)
    noise = float(np.median(np.abs(flat - baseline))) * MAD_TO_SIGMA
    if noise <= 0:
        noise = max(float(np.std(flat)) * 1e-3, 1e-15)
    stats.noise_sigma_v = noise
    threshold = baseline + config.threshold_sigma * noise

    stop = max(int(round(config.search_fraction * points)), 3)
    samples = np.full(n, np.nan)
    amplitudes = np.full(n, np.nan)
    fwhm = np.full(n, np.nan)

    for i in range(n):
        trace = data[i]
        local = trace[:stop] - np.median(trace)
        apex = int(np.argmax(local))
        peak = float(local[apex])
        if peak < threshold - baseline:
            continue
        position = float(apex)
        if config.refine_subsample and 0 < apex < local.size - 1:
            position += _parabolic_offset(local[apex - 1], local[apex],
                                          local[apex + 1])
        samples[i] = position
        amplitudes[i] = peak
        fwhm[i] = _fwhm_samples(local, apex, peak, max(points // 4, 4))

    found = np.isfinite(samples)
    stats.n_found = int(np.count_nonzero(found))
    stats.n_missing = int(n - stats.n_found)
    stats.missing_segment_indices = tuple(
        int(i) for i in np.flatnonzero(~found))

    if stats.n_found:
        stats.median_sample = float(np.median(samples[found]))
        stats.amplitude_median_v = float(np.median(amplitudes[found]))
        stats.amplitude_min_v = float(np.min(amplitudes[found]))
        stats.amplitude_max_v = float(np.max(amplitudes[found]))
        stats.fwhm_samples_median = float(np.nanmedian(fwhm[found]))
        if math.isfinite(stats.expected_sample):
            stats.offset_samples = stats.median_sample - stats.expected_sample

    if stats.n_found >= 3:
        idx = np.flatnonzero(found).astype(float)
        slope, intercept = np.polyfit(idx, samples[found], 1)
        stats.drift_samples_per_segment = float(slope)
        stats.drift_samples_total = float(slope * (n - 1))
        stats.scatter_samples = float(
            np.std(samples[found] - (slope * idx + intercept), ddof=1))
        stats.drift_fraction_of_window = abs(stats.drift_samples_total) / points
    elif stats.n_found == 2:
        stats.scatter_samples = 0.0

    if math.isfinite(stretch_factor) and stretch_factor > 0:
        # Samples here are lab-time samples; the delay equivalent is what
        # matters for whether the delay axis has moved.
        pass  # filled in by the caller, which knows the sample rate

    if stats.n_missing:
        stats.warnings.append(
            f"No copeak found in {stats.n_missing} of {n} segments "
            f"(index {stats.missing_segment_indices[:8]}"
            f"{'...' if stats.n_missing > 8 else ''}). Either those segments "
            f"were triggered off the ASOPS phase, or the capture window has "
            f"drifted off the coincidence.")
    if (math.isfinite(stats.median_sample)
            and (stats.median_sample < config.edge_margin_samples
                 or stats.median_sample > points - 1
                 - config.edge_margin_samples)):
        stats.warnings.append(
            f"The copeak sits {stats.median_sample:.1f} samples into a "
            f"{points}-sample window, within {config.edge_margin_samples:g} "
            f"samples of an edge. The window is mis-positioned: move the "
            f"scope's horizontal offset, or capture one whole sweep flat to "
            f"see where the coincidence actually is.")
    if (math.isfinite(stats.drift_fraction_of_window)
            and stats.drift_fraction_of_window > config.max_drift_fraction):
        stats.warnings.append(
            f"The copeak walks {stats.drift_samples_total:+.1f} samples "
            f"across the line, {stats.drift_fraction_of_window * 100:.1f}% of "
            f"the capture window. That is trigger timing drifting against the "
            f"offset lock; left alone it will walk out of the window during a "
            f"map. Segments are realigned on their own copeaks here, so the "
            f"data are still usable - but the drift is real and worth fixing.")
    if (math.isfinite(stats.fwhm_samples_median)
            and stats.fwhm_samples_median < config.min_fwhm_samples):
        stats.warnings.append(
            f"The copeak is only {stats.fwhm_samples_median:.1f} samples wide "
            f"at half maximum. Below about {config.min_fwhm_samples:g} "
            f"samples its position - and so the drift and scatter figures "
            f"above - is limited by sampling phase rather than by the "
            f"instrument. Slow the envelope filter or raise the sample rate.")
    return samples, amplitudes, stats


# ===========================================================================
# Flat-record peak finding (secondary path)
# ===========================================================================

@dataclass(frozen=True)
class FlatPeakConfig:
    """Finding coincidence peaks in one long continuous record."""

    threshold_sigma: float = 8.0
    min_amplitude_frac: float = 0.25
    local_amplitude_window: int = 9
    min_separation_frac: float = 0.5
    max_residual_frac: float = 0.25
    nominal_window: float = 0.3
    edge_guard_frac: float = 0.02
    baseline_blocks_per_sweep: float = 1.0
    refine_subsample: bool = True


@dataclass
class FlatPeaks:
    index: np.ndarray            # sub-sample refined sample index
    amplitude: np.ndarray
    sweep_index: np.ndarray
    is_edge: np.ndarray          # possibly clipped by a record edge
    rejected_index: np.ndarray
    fit: LatticeFit
    sample_rate_hz: float
    n_candidates: int = 0
    n_merged: int = 0
    warnings: list = field(default_factory=list)

    def times_s(self) -> np.ndarray:
        return self.index / self.sample_rate_hz


def _block_baseline(x: np.ndarray, block: int) -> np.ndarray:
    """Slow baseline as a per-block median, linearly interpolated.

    A true rolling median over a million samples is not worth the time; one
    median per sweep tracks any drift well below the sweep rate, which is the
    only drift that matters. The copeak occupies a handful of samples in a
    block of hundreds, so it cannot pull the median.
    """
    n = x.size
    block = max(int(block), 8)
    n_blocks = max(n // block, 1)
    usable = n_blocks * block
    med = np.median(x[:usable].reshape(n_blocks, block), axis=1)
    if n_blocks == 1:
        return np.full(n, med[0])
    centres = (np.arange(n_blocks) + 0.5) * block
    return np.interp(np.arange(n), centres, med)


def _local_median(values: np.ndarray, window: int) -> np.ndarray:
    n = values.size
    if n == 0:
        return values
    half = max(int(window) // 2, 1)
    out = np.empty(n)
    for i in range(n):
        out[i] = np.median(values[max(i - half, 0):min(i + half + 1, n)])
    return out


def find_flat_peaks(signal, sample_rate_hz: float,
                    timing: AsopsTiming = AsopsTiming(),
                    config: FlatPeakConfig = FlatPeakConfig()) -> FlatPeaks:
    """Find the coincidence peaks in a flat record and fit the sweep lattice.

    Used by the flat diagnostic path, and only there: in Sequence mode the
    scope has already segmented the record. The rule is deliberately not "one
    peak every 100 us" - the offset lock jitters, sweeps can go missing, and
    spurious peaks happen - so candidates are found generously and then
    judged against a fitted lattice.
    """
    signal = np.asarray(signal, dtype=np.float64)
    if signal.ndim != 1:
        raise ValueError("signal must be one-dimensional")
    if signal.size < 32:
        raise ValueError("record too short to contain a sweep")

    warnings = []
    expected = sample_rate_hz / timing.sweep_rate_hz
    block = int(round(expected / max(config.baseline_blocks_per_sweep, 1e-9)))
    detrended = signal - _block_baseline(signal, block)

    level = float(np.median(detrended))
    noise = float(np.median(np.abs(detrended - level))) * MAD_TO_SIGMA
    if noise <= 0:
        noise = max(float(np.std(detrended)) * 1e-3, 1e-15)
        warnings.append("Noise estimate was zero; threshold fell back to a "
                        "fraction of the record standard deviation.")
    threshold = level + config.threshold_sigma * noise

    core = detrended[1:-1]
    mask = (core >= detrended[:-2]) & (core > detrended[2:]) & (core > threshold)
    cand = (np.flatnonzero(mask) + 1).astype(np.int64)
    # A clipped peak sits exactly at a record edge, where no local maximum
    # test can reach it.
    edges = []
    if detrended[0] > threshold and detrended[0] >= detrended[1]:
        edges.append(0)
    last = detrended.size - 1
    if detrended[last] > threshold and detrended[last] >= detrended[last - 1]:
        edges.append(last)
    if edges:
        cand = np.sort(np.concatenate([cand, np.array(edges, dtype=np.int64)]))

    n_candidates = int(cand.size)
    if cand.size == 0:
        warnings.append(
            f"No candidate peaks above {threshold:.4g} V "
            f"({config.threshold_sigma:g} sigma): either the threshold is too "
            f"high or this record holds no coincidences.")
        return FlatPeaks(
            np.zeros(0), np.zeros(0), np.zeros(0, dtype=np.int64),
            np.zeros(0, dtype=bool), np.zeros(0),
            fit_lattice(np.zeros(0), expected), sample_rate_hz,
            n_candidates=0, warnings=warnings)

    amp = detrended[cand]
    if cand.size >= 5:
        # Local, so that amplitude drifting along the line does not quietly
        # kill the dim end of it.
        bright = amp >= config.min_amplitude_frac * _local_median(
            amp, config.local_amplitude_window)
        cand, amp = cand[bright], amp[bright]

    # Minimum separation, brightest first: when a doubled peak appears the
    # real one survives and its shoulder is discarded.
    min_sep = max(int(round(config.min_separation_frac * expected)), 1)
    kept_positions, kept_mask = [], np.zeros(cand.size, dtype=bool)
    for i in np.argsort(-amp, kind="stable"):
        pos = int(cand[i])
        j = bisect_left(kept_positions, pos)
        close = ((j < len(kept_positions) and kept_positions[j] - pos < min_sep)
                 or (j > 0 and pos - kept_positions[j - 1] < min_sep))
        if not close:
            insort(kept_positions, pos)
            kept_mask[i] = True
    n_merged = int(np.count_nonzero(~kept_mask))
    cand, amp = cand[kept_mask], amp[kept_mask]

    refined = cand.astype(np.float64)
    if config.refine_subsample:
        for n, i in enumerate(cand):
            if 0 < i < detrended.size - 1:
                refined[n] = i + _parabolic_offset(
                    detrended[i - 1], detrended[i], detrended[i + 1])

    guard = max(config.edge_guard_frac * expected, 2.0)
    is_edge = (refined < guard) | (refined > detrended.size - 1 - guard)

    fit = fit_lattice(refined, expected, tolerance=config.max_residual_frac,
                      nominal_window=config.nominal_window, exclude=is_edge)
    warnings.extend(fit.warnings)

    keep = ~fit.outlier
    rejected = refined[fit.outlier]
    if rejected.size:
        warnings.append(
            f"{rejected.size} candidate peak(s) rejected for sitting off the "
            f"sweep lattice by more than {config.max_residual_frac:g} of a "
            f"spacing.")
    if fit.missing_index:
        warnings.append(
            f"{len(fit.missing_index)} dropped sweep(s) at lattice index "
            f"{fit.missing_index}. Sweep numbering is preserved across the "
            f"gap, so later pixels are NOT shifted.")

    return FlatPeaks(
        index=refined[keep], amplitude=amp[keep], sweep_index=fit.index[keep],
        is_edge=is_edge[keep], rejected_index=rejected, fit=fit,
        sample_rate_hz=sample_rate_hz, n_candidates=n_candidates,
        n_merged=n_merged, warnings=warnings)


# ===========================================================================
# Segment extraction and alignment
# ===========================================================================

@dataclass(frozen=True)
class SegmentConfig:
    """The common delay window cut from every segment, and its alignment."""

    # Window in REAL delay, relative to the copeak. The defaults sit inside a
    # 2 ns capture window with 0.2 ns before the coincidence, leaving about
    # 28 samples of slack at 10 MS/s. That slack is not spare: every segment
    # is realigned on its own copeak, so the window has to absorb trigger
    # jitter and any drift of the copeak through the capture window. A
    # processing window as large as the captured one discards every segment,
    # and one only slightly smaller discards the end of the line as soon as
    # the copeak walks. `SegmentSet.slack_samples` reports what is left and
    # the chain warns when the drift is eating into it.
    pre_delay_s: float = 0.15e-9
    post_delay_s: float = 1.5e-9
    # 'copeak' resamples each segment onto a grid centred on its own copeak,
    # which removes trigger jitter and the sample-clock phase in one step.
    # 'trigtime' corrects only the recorded sub-sample offset. 'none' trusts
    # the segments as they arrive.
    align: str = "copeak"
    # Subtract the mean of the pre-copeak window from each trace. That window
    # is the only baseline a segment contains.
    baseline_subtract: bool = True


@dataclass
class SegmentSet:
    """Per-sweep delay traces on a common delay axis."""

    traces: np.ndarray            # (N, W)
    pre_samples: int
    post_samples: int
    sample_rate_hz: float
    stretch_factor: float
    sweep_period_s: float
    trigger_time_s: np.ndarray    # (N,) lab time of each trigger
    copeak_time_s: np.ndarray     # (N,) lab time of each copeak
    copeak_sample: np.ndarray     # (N,) copeak position in the raw segment
    sweep_index: np.ndarray       # (N,) sweep number within the line
    amplitude: np.ndarray         # (N,) copeak height
    source_index: np.ndarray      # (N,) row in the input array
    capture_points: int = 0       # points in a raw captured segment
    slack_samples: int = 0        # capture_points - window width
    n_discarded: int = 0
    discarded_reasons: dict = field(default_factory=dict)
    align: str = "copeak"

    @property
    def n_segments(self) -> int:
        return int(self.traces.shape[0])

    @property
    def width(self) -> int:
        return int(self.traces.shape[1])

    def lab_axis_s(self) -> np.ndarray:
        """Lab time of each sample in a trace, zero at the coincidence."""
        return (np.arange(self.width) - self.pre_samples) / self.sample_rate_hz

    def delay_axis_s(self) -> np.ndarray:
        """Pump-probe delay of each sample in a trace, zero at coincidence."""
        return self.lab_axis_s() / self.stretch_factor


def _resample_row(row, centre, pre, width):
    """One trace, linearly interpolated onto a grid centred on `centre`."""
    base = int(math.floor(centre)) - pre
    frac = centre - math.floor(centre)
    need = base + width + (1 if frac > 0 else 0)
    if base < 0 or need > row.size:
        return None
    if frac > 0:
        return ((1.0 - frac) * row[base:base + width]
                + frac * row[base + 1:base + width + 1])
    return row[base:base + width].copy()


def build_segments(
    segments, sample_rate_hz: float, copeak_samples, trigger_time_s,
    sweep_index, amplitude=None, timing: AsopsTiming = AsopsTiming(),
    config: SegmentConfig = SegmentConfig(), trigger_offset_s=None,
    exclude=None,
) -> SegmentSet:
    """Cut a common, copeak-aligned delay window out of every segment.

    Segments whose window runs off either end of their capture window are
    discarded rather than zero-padded: a half-length trace averaged into a
    pixel biases that pixel, which is worse than leaving it one sweep lighter.
    """
    data = np.atleast_2d(np.asarray(segments, dtype=np.float64))
    n, points = data.shape
    copeak_samples = np.asarray(copeak_samples, dtype=np.float64)
    trigger_time_s = np.asarray(trigger_time_s, dtype=np.float64)
    sweep_index = np.asarray(sweep_index, dtype=np.int64)
    amplitude = (np.full(n, np.nan) if amplitude is None
                 else np.asarray(amplitude, dtype=np.float64))
    exclude = (np.zeros(n, dtype=bool) if exclude is None
               else np.asarray(exclude, dtype=bool))
    if trigger_offset_s is None:
        trigger_offset_s = np.zeros(n)
    trigger_offset_s = np.asarray(trigger_offset_s, dtype=np.float64)

    if config.align not in ("copeak", "trigtime", "none"):
        raise ValueError("align must be 'copeak', 'trigtime' or 'none'")

    pre = int(round(timing.lab_time(config.pre_delay_s) * sample_rate_hz))
    post = int(round(timing.lab_time(config.post_delay_s) * sample_rate_hz))
    if pre + post < 4:
        raise ValueError("segment window is shorter than 4 samples")
    width = pre + post

    traces, kept = [], []
    reasons = {"excluded": 0, "no_copeak": 0, "window_off_segment": 0}
    for i in range(n):
        if exclude[i]:
            reasons["excluded"] += 1
            continue
        centre = copeak_samples[i]
        if not math.isfinite(centre):
            reasons["no_copeak"] += 1
            continue
        if config.align == "copeak":
            target = centre
        elif config.align == "trigtime":
            # Keep the nominal copeak position but take out the recorded
            # sub-sample phase of this segment's first sample.
            frac = (trigger_offset_s[i] * sample_rate_hz) % 1.0
            target = round(centre) + frac
        else:
            target = float(round(centre))
        row = _resample_row(data[i], target, pre, width)
        if row is None:
            reasons["window_off_segment"] += 1
            continue
        if config.baseline_subtract and pre >= 2:
            row = row - np.mean(row[:pre])
        traces.append(row)
        kept.append(i)

    kept = np.asarray(kept, dtype=np.int64)
    stacked = (np.vstack(traces) if kept.size
               else np.zeros((0, width), dtype=np.float64))
    copeak_time = (trigger_time_s[kept] + trigger_offset_s[kept]
                   + copeak_samples[kept] / sample_rate_hz
                   if kept.size else np.zeros(0))

    return SegmentSet(
        traces=stacked, pre_samples=pre, post_samples=post,
        sample_rate_hz=sample_rate_hz, stretch_factor=timing.stretch_factor,
        sweep_period_s=timing.sweep_period_s,
        trigger_time_s=trigger_time_s[kept] if kept.size else np.zeros(0),
        copeak_time_s=copeak_time,
        copeak_sample=copeak_samples[kept] if kept.size else np.zeros(0),
        sweep_index=sweep_index[kept] if kept.size
        else np.zeros(0, dtype=np.int64),
        amplitude=amplitude[kept] if kept.size else np.zeros(0),
        source_index=kept, capture_points=int(points),
        slack_samples=int(points - width), n_discarded=int(n - kept.size),
        discarded_reasons={k: v for k, v in reasons.items() if v},
        align=config.align)


# ===========================================================================
# Probe DC and dR/R
# ===========================================================================

@dataclass(frozen=True)
class DcConfig:
    """How one scalar probe-DC value is formed per copeak.

    One value per sweep is the physically right answer, not an
    interpolation along the delay axis: in dR/R the R is the unperturbed
    reflectivity at that point of the sample, and it does not depend on the
    pump-probe delay. The default is the mean over the whole of that
    segment's DC slice - which at a 2 ns capture window is about a sixth of a
    micron of travel, so it really is the local reflectivity. `window` can
    narrow it to part of the slice, in sparsed DC samples.
    """

    method: str = "mean"          # 'mean' or 'median'
    window: tuple = ()            # (start, stop) in sparsed DC samples
    offset_v: float = 0.0         # amplifier offset removed before dividing
    min_level_v: float = 1.0e-3   # below this, dR/R is NaN, not enormous


@dataclass
class DcResult:
    level_v: np.ndarray           # (N,) one scalar per segment
    traces: np.ndarray            # (N, W) dR/R
    n_bad: int = 0
    mean_v: float = float("nan")
    min_v: float = float("nan")
    max_v: float = float("nan")
    variation_fraction: float = float("nan")
    intra_segment_fraction: float = float("nan")
    samples_per_segment: int = 0
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dc_mean_v": self.mean_v, "dc_min_v": self.min_v,
            "dc_max_v": self.max_v,
            "dc_variation_fraction": self.variation_fraction,
            "dc_intra_segment_fraction": self.intra_segment_fraction,
            "dc_samples_per_segment": self.samples_per_segment,
            "dc_n_bad": self.n_bad,
        }

    def report(self) -> str:
        return format_dc_result(self)


def dc_scalar_per_segment(dc, config: DcConfig = DcConfig()):
    """Reduce each segment's DC slice to one number. Returns (N,) volts."""
    data = np.atleast_2d(np.asarray(dc, dtype=np.float64)) - config.offset_v
    if config.window:
        start, stop = int(config.window[0]), int(config.window[1])
        data = data[:, start:stop]
    if data.shape[1] == 0:
        raise ValueError("DC window selects no samples")
    if config.method == "median":
        return np.median(data, axis=1)
    if config.method != "mean":
        raise ValueError("DcConfig.method must be 'mean' or 'median'")
    return np.mean(data, axis=1)


def normalise_by_dc(segments: SegmentSet, dc, config: DcConfig = DcConfig(),
                    source_index=None) -> DcResult:
    """Divide each segment's trace by its own scalar probe DC.

    What survives the division is the modulation depth: laser power, focus,
    detector gain and sample reflectivity all multiply AC and DC equally, so
    all of them cancel.
    """
    data = np.atleast_2d(np.asarray(dc, dtype=np.float64))
    rows = segments.source_index if source_index is None else source_index
    level_all = dc_scalar_per_segment(data, config)
    if rows is None:
        level = level_all
    else:
        rows = np.asarray(rows, dtype=np.int64)
        level = level_all[rows]

    result = DcResult(level_v=level,
                      traces=np.zeros_like(segments.traces),
                      samples_per_segment=int(data.shape[1]))
    if level.size:
        result.mean_v = float(np.mean(level))
        result.min_v = float(np.min(level))
        result.max_v = float(np.max(level))
        if result.mean_v != 0:
            result.variation_fraction = (
                (result.max_v - result.min_v) / abs(result.mean_v))
    if data.shape[1] >= 2 and result.mean_v not in (0.0, float("nan")):
        swing = np.ptp(data - config.offset_v, axis=1)
        result.intra_segment_fraction = float(
            np.median(swing) / abs(result.mean_v))

    bad = ~np.isfinite(level) | (np.abs(level) < config.min_level_v)
    safe = np.where(bad, np.nan, level)
    with np.errstate(invalid="ignore", divide="ignore"):
        result.traces = segments.traces / safe[:, None]
    result.n_bad = int(np.count_nonzero(bad))

    if result.n_bad:
        result.warnings.append(
            f"{result.n_bad} segment(s) had probe DC below "
            f"{config.min_level_v:g} V; their dR/R is NaN rather than "
            f"enormous, so the pixel stays visibly empty.")
    if result.samples_per_segment < 4:
        result.warnings.append(
            f"Only {result.samples_per_segment} DC sample(s) per segment to "
            f"average into one value. The denominator is noisier than it "
            f"needs to be: sparse the DC channel less.")
    return result


# ===========================================================================
# Position
# ===========================================================================

@dataclass(frozen=True)
class PositionConfig:
    """How absolute position is assigned to each segment.

    'anchors' is the primary path: the BBD303 emits a TTL pulse every
    programmed interval of travel, and any pulse edge that happens to fall
    inside a capture window is an exact position at an exact time. Those
    anchors are interpolated, so velocity ripple between them survives
    instead of being fitted away.

    'velocity' is the fallback when no trigger channel was recorded: position
    from TRIGTIME and the commanded velocity. It is exact in time and only as
    good as the velocity in space - any velocity error becomes a linear
    scaling of the map, and ripple becomes invisible.

    'edges' is for a flat record, where the whole pulse train is digitised
    and every edge can be found.
    """

    source: str = "anchors"
    velocity_mm_s: float = float("nan")
    # Stage position at the origin of the segment time base, i.e. at the
    # first trigger of the acquisition (TRIGTIME column 0 == 0), not at
    # whatever moment the stage started moving.
    x_start_um: float = 0.0
    direction: int = 1
    trigger_interval_um: float = 0.5
    # NaN: identify the first anchor's absolute position from x_start and the
    # commanded velocity, which is safe because the first anchor arrives
    # early in the line.
    first_pulse_position_um: float = float("nan")
    threshold_v: float = float("nan")
    latency_s: float = 0.0
    allow_extrapolation: bool = False
    max_lattice_residual: float = 0.25


@dataclass
class PositionResult:
    positions_um: np.ndarray
    source: str = ""
    n_anchors: int = 0
    n_missing_anchors: int = 0
    anchor_interval_um: float = float("nan")
    anchor_uncertainty_um: float = float("nan")
    anchor_rate_hz: float = float("nan")
    ripple_bandwidth_hz: float = float("nan")
    measured_velocity_mm_s: float = float("nan")
    commanded_velocity_mm_s: float = float("nan")
    velocity_std_mm_s: float = float("nan")
    velocity_min_mm_s: float = float("nan")
    velocity_max_mm_s: float = float("nan")
    ripple_rms_um: float = float("nan")
    ripple_pp_um: float = float("nan")
    start_um: float = float("nan")
    end_um: float = float("nan")
    span_um: float = float("nan")
    direction: int = 0
    n_out_of_range: int = 0
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("positions_um")
        out.pop("warnings")
        return out

    def report(self) -> str:
        return format_position_result(self)


def _rising_edges_in_rows(rows, dt_s, row_start_times, threshold):
    """Threshold crossings inside each row. Returns (times, row indices)."""
    data = np.atleast_2d(np.asarray(rows, dtype=np.float64))
    times, which = [], []
    for i in range(data.shape[0]):
        trace = data[i]
        above = trace > threshold
        for j in np.flatnonzero(~above[:-1] & above[1:]):
            v0, v1 = trace[j], trace[j + 1]
            frac = 0.5 if v1 == v0 else (threshold - v0) / (v1 - v0)
            frac = float(np.clip(frac, 0.0, 1.0))
            times.append(row_start_times[i] + (j + frac) * dt_s)
            which.append(i)
    return np.asarray(times), np.asarray(which, dtype=np.int64)


def _auto_threshold(values, minimum_span=0.0):
    """Halfway between the low and high levels, or None if there is no step.

    Returns None rather than raising on an empty input: numpy's percentile
    fails with an IndexError from deep inside itself, which tells a caller
    nothing about trigger channels or sparsing.
    """
    values = np.asarray(values)
    if values.size == 0:
        return None
    low = float(np.percentile(values, 5))
    high = float(np.percentile(values, 95))
    if high - low <= minimum_span:
        return None
    return 0.5 * (low + high)


# Samples a sparsed trigger channel needs before its edges can be used. Two
# per segment is the structural minimum - an edge is a transition between two
# samples, so one sample per segment cannot hold one. Three on a pulse is
# what `budget.AcquisitionConfig.min_trigger_samples` asks for, because an
# edge located to one sparsed sample is also timed to one.
MIN_TRIGGER_SAMPLES_PER_SEGMENT = 2
MIN_TRIGGER_SAMPLES_PER_PULSE = 3


def _check_trigger_shape(rows, n_expected_rows, trigger_dt_s, where):
    """Fail early and legibly on a trigger channel that cannot be decoded.

    This is the hazard the hardware checklist warns about from the other
    end: the BBD303's TTL pulse has to be wide enough to survive the
    sparsing on its channel. Sparse too hard and the pulses are not merely
    noisy, they are structurally unrepresentable - so say so, with the
    numbers, rather than letting an IndexError surface from inside numpy.
    """
    rows = np.atleast_2d(np.asarray(rows, dtype=np.float64))
    n_rows, per_row = rows.shape

    if rows.size == 0 or per_row < MIN_TRIGGER_SAMPLES_PER_SEGMENT:
        raise ValueError(
            f"{where}: the trigger channel has {per_row} sample(s) per "
            f"segment, too few to locate pulse edges - an edge is a "
            f"transition between two samples, so at least "
            f"{MIN_TRIGGER_SAMPLES_PER_SEGMENT} are needed to hold one and "
            f"about {MIN_TRIGGER_SAMPLES_PER_PULSE} to time one. Reduce the "
            f"sparsing factor on that channel, or widen the programmed TTL "
            f"pulse: at this channel's "
            f"{1.0 / trigger_dt_s / 1e3:.4g} kS/s a pulse needs to be at "
            f"least {MIN_TRIGGER_SAMPLES_PER_PULSE * trigger_dt_s * 1e6:.3g} "
            f"us wide. Alternatively set PositionConfig.source='velocity' to "
            f"accept position from the commanded velocity instead."
            if trigger_dt_s and math.isfinite(trigger_dt_s) else
            f"{where}: the trigger channel has {per_row} sample(s) per "
            f"segment, too few to locate pulse edges (at least "
            f"{MIN_TRIGGER_SAMPLES_PER_SEGMENT} are needed). Reduce the "
            f"sparsing factor on that channel, or widen the programmed TTL "
            f"pulse.")

    if n_expected_rows is not None and n_rows != n_expected_rows:
        raise ValueError(
            f"{where}: the trigger channel has {n_rows} row(s) but there are "
            f"{n_expected_rows} segments. Sequence-mode channels are stored "
            f"as (n_segments, points_per_segment); a one-dimensional trigger "
            f"channel reaching here means it was stored flat, or the segment "
            f"count was lost on the way in.")
    return rows


def index_by_gaps(times, nominal_gap, tolerance=0.3):
    """Number events whose spacing is a whole multiple of `nominal_gap`.

    This is not the same problem as `fit_lattice`, and using that here would
    be wrong. The ASOPS triggers are regular in TIME, so a global straight
    line through them is the right model. The BBD303 position pulses are
    regular in SPACE: their times carry whatever the velocity did, including
    ripple, and a global fit would treat that ripple as error and start
    rejecting anchors - which is exactly the information the channel exists
    to provide.

    So each gap is read on its own as an integer number of intervals. That
    only needs the velocity to be steady between two consecutive anchors, not
    across the line, and it is how a missing pulse is kept from renumbering
    everything after it.

    Returns (index, max_residual, n_missing, warnings).
    """
    times = np.asarray(times, dtype=np.float64)
    if times.size == 0:
        return (np.zeros(0, dtype=np.int64), 0.0, 0, [])
    if times.size == 1 or nominal_gap <= 0:
        return (np.zeros(times.size, dtype=np.int64), 0.0, 0, [])

    ratio = np.diff(times) / nominal_gap
    steps = np.maximum(np.rint(ratio), 1.0)
    residual = float(np.max(np.abs(ratio - steps)))
    index = np.concatenate([[0], np.cumsum(steps)]).astype(np.int64)
    n_missing = int(index[-1] + 1 - index.size)

    warnings = []
    if residual > tolerance:
        warnings.append(
            f"A gap between position pulses sits {residual:.2f} intervals "
            f"from a whole number of them. Either the velocity changed "
            f"sharply between two anchors, or an edge was mis-detected; the "
            f"pulse numbering past that point may be wrong, and with it the "
            f"position scale.")
    return index, residual, n_missing, warnings


def positions_from_velocity(trigger_times_s, config: PositionConfig):
    """Position per segment from TRIGTIME and the commanded velocity."""
    t = np.asarray(trigger_times_s, dtype=np.float64)
    if not math.isfinite(config.velocity_mm_s):
        raise ValueError(
            "position source 'velocity' needs PositionConfig.velocity_mm_s")
    v_um_s = config.velocity_mm_s * 1.0e3
    t0 = t[0] if t.size else 0.0
    positions = config.x_start_um + config.direction * v_um_s * (t - t0)
    result = PositionResult(positions_um=positions, source="velocity",
                            commanded_velocity_mm_s=config.velocity_mm_s,
                            measured_velocity_mm_s=config.velocity_mm_s,
                            direction=config.direction)
    result.warnings.append(
        "Position came from the commanded velocity and TRIGTIME, with no "
        "position measurement. Timing is exact; distance is only as good as "
        "the velocity, so a velocity error scales the whole map, and velocity "
        "ripple is invisible. Record the BBD303 trigger channel to fix both.")
    _fill_position_stats(result, trigger_times_s)
    return result


def positions_from_anchors(trigger_rows, trigger_dt_s, row_start_times_s,
                           copeak_times_s, config: PositionConfig,
                           sweep_period_s=float("nan")):
    """Position per segment from BBD303 pulse edges seen inside the windows.

    Each capture window only looks at the trigger channel for a small part of
    each sweep, so most pulses are never digitised - but the ones that are
    give an exact position at an exact time. Those are the anchors; position
    between them is interpolated, which keeps ripple instead of fitting it
    away.
    """
    row_start_times_s = np.asarray(row_start_times_s, dtype=np.float64)
    copeak_times_s = np.asarray(copeak_times_s, dtype=np.float64)
    rows = _check_trigger_shape(
        trigger_rows, row_start_times_s.size, trigger_dt_s,
        "position from pulse anchors")

    result = PositionResult(positions_um=np.full(copeak_times_s.size, np.nan),
                            source="anchors",
                            anchor_interval_um=config.trigger_interval_um,
                            commanded_velocity_mm_s=config.velocity_mm_s,
                            direction=config.direction)

    threshold = (config.threshold_v if math.isfinite(config.threshold_v)
                 else _auto_threshold(rows.reshape(-1)))
    if threshold is None:
        # Not necessarily a dead channel: if the pulse interval divided by the
        # velocity is a whole number of sweep periods, every pulse arrives at
        # the same phase of the sweep, and if that phase is outside the
        # capture window then no pulse is ever digitised however many are
        # emitted. Changing the programmed interval by a non-integer number
        # of sweeps' worth of travel fixes it.
        result.warnings.append(
            "The trigger channel shows no level change at all. Either the "
            "BBD303 emitted nothing, or the pulse interval is a whole number "
            "of sweeps' worth of travel and every pulse lands in the dead "
            "time between capture windows. Falling back to the commanded "
            "velocity.")
        fallback = positions_from_velocity(copeak_times_s, config)
        fallback.warnings = result.warnings + fallback.warnings
        fallback.source = "velocity (no pulses seen)"
        return fallback

    edge_times, _ = _rising_edges_in_rows(
        rows, trigger_dt_s, row_start_times_s, threshold)
    edge_times = np.sort(edge_times) - config.latency_s
    result.n_anchors = int(edge_times.size)

    if edge_times.size < 2:
        result.warnings.append(
            f"Only {edge_times.size} position-pulse edge(s) landed inside a "
            f"capture window, which is not enough to interpolate. Falling "
            f"back to the commanded velocity. Program a wider BBD303 pulse, "
            f"or a coarser interval, so pulses overlap the windows more often.")
        fallback = positions_from_velocity(copeak_times_s, config)
        fallback.warnings = result.warnings + fallback.warnings
        fallback.source = "velocity (anchors unavailable)"
        fallback.n_anchors = result.n_anchors
        return fallback

    if not math.isfinite(config.velocity_mm_s):
        raise ValueError(
            "position source 'anchors' needs PositionConfig.velocity_mm_s as "
            "a prior, to index the pulses that were seen")
    v_um_s = config.velocity_mm_s * 1.0e3
    nominal_gap = config.trigger_interval_um / v_um_s

    index, residual, n_missing, gap_warnings = index_by_gaps(
        edge_times, nominal_gap, tolerance=config.max_lattice_residual)
    result.warnings.extend(gap_warnings)
    result.n_missing_anchors = n_missing
    if residual > 0.5 * config.max_lattice_residual:
        result.warnings.append(
            f"Gaps between position pulses are only just resolvable as whole "
            f"intervals: the worst is {residual:.2f} intervals off. That "
            f"happens when the stage's own position ripple is comparable to "
            f"the trigger interval, and it makes the pulse numbering - and "
            f"therefore the position scale - ambiguous at the level of one "
            f"{config.trigger_interval_um:.3g} um interval. Use a coarser "
            f"trigger interval, or pass first_pulse_position_um and treat "
            f"the ripple as unresolved.")
    if index.size > 1:
        steps = np.diff(index)
        median_step = float(np.median(steps)) if steps.size else 1.0
        # How often an anchor actually arrives bounds what ripple can be
        # seen: anything faster than half this rate is not resolved.
        span = float(edge_times[-1] - edge_times[0])
        if span > 0:
            rate = (edge_times.size - 1) / span
            result.anchor_rate_hz = rate
            result.ripple_bandwidth_hz = 0.5 * rate
        if median_step > 1.5:
            result.warnings.append(
                f"Only about one position pulse in {median_step:.0f} landed "
                f"inside a capture window, which is expected in Sequence "
                f"mode: the windows see "
                f"{100 * trigger_dt_s * rows.shape[1] / max(sweep_period_s, 1e-12):.0f}%"
                f" of each sweep. Anchors are therefore "
                f"{median_step * config.trigger_interval_um:.3g} um apart "
                f"rather than {config.trigger_interval_um:.3g} um.")

    # Absolute identification: the first anchor seen arrives early, so the
    # commanded velocity places it to well under one interval even if the
    # velocity is a few percent off.
    if math.isfinite(config.first_pulse_position_um):
        first_position = config.first_pulse_position_um
    else:
        # Times are relative to the first trigger, and x_start_um is the
        # position at that instant, so the elapsed time is the edge time
        # itself. The first anchor arrives early, so even a few percent of
        # velocity error places it well inside one interval.
        elapsed = float(edge_times[0])
        estimate = config.x_start_um + config.direction * v_um_s * elapsed
        first_position = (round(estimate / config.trigger_interval_um)
                          * config.trigger_interval_um)
        drift = abs(v_um_s * 0.02 * elapsed)
        if drift > 0.5 * config.trigger_interval_um:
            result.warnings.append(
                f"The first position pulse was seen {elapsed * 1e3:.2f} ms "
                f"into the line, by which point a 2% velocity error is "
                f"{drift:.3g} um - more than half the "
                f"{config.trigger_interval_um:.3g} um interval. Its absolute "
                f"pulse number is therefore ambiguous: pass "
                f"first_pulse_position_um explicitly.")

    anchor_times = edge_times
    anchor_positions = (first_position
                        + config.direction * config.trigger_interval_um * index)

    outside = ((copeak_times_s < anchor_times[0])
               | (copeak_times_s > anchor_times[-1]))
    positions = np.interp(copeak_times_s, anchor_times, anchor_positions)
    result.n_out_of_range = int(np.count_nonzero(outside))
    if result.n_out_of_range and not config.allow_extrapolation:
        # np.interp clamps rather than extrapolating; NaN keeps a clamped
        # position from masquerading as a measured one.
        positions = np.where(outside, np.nan, positions)
    result.positions_um = positions

    # An anchor's time is exact; its own uncertainty is the sparsed sample
    # interval on the trigger channel.
    result.anchor_uncertainty_um = v_um_s * trigger_dt_s
    if anchor_times.size > 2:
        slope, intercept = np.polyfit(anchor_times, anchor_positions, 1)
        result.measured_velocity_mm_s = abs(slope) / 1.0e3
        residual = anchor_positions - (slope * anchor_times + intercept)
        result.ripple_rms_um = float(np.std(residual, ddof=1))
        result.ripple_pp_um = float(np.max(residual) - np.min(residual))
        # Velocity between consecutive anchors: a known distance over a
        # measured time, with no differentiation of a noisy signal involved.
        # This is the ripple, measured rather than modelled.
        with np.errstate(invalid="ignore", divide="ignore"):
            v = (np.diff(anchor_positions) / np.diff(anchor_times)) / 1.0e3
        v = np.abs(v[np.isfinite(v)])
        if v.size:
            result.velocity_std_mm_s = (float(np.std(v, ddof=1))
                                        if v.size > 1 else 0.0)
            result.velocity_min_mm_s = float(np.min(v))
            result.velocity_max_mm_s = float(np.max(v))

    if (math.isfinite(result.measured_velocity_mm_s)
            and math.isfinite(config.velocity_mm_s)
            and config.velocity_mm_s > 0):
        err = abs(result.measured_velocity_mm_s - config.velocity_mm_s)
        if err > 0.05 * config.velocity_mm_s:
            result.warnings.append(
                f"Measured velocity {result.measured_velocity_mm_s:.4g} mm/s "
                f"is {100 * err / config.velocity_mm_s:.1f}% from the "
                f"commanded {config.velocity_mm_s:.4g} mm/s. The pulses are "
                f"the authority for position, so the map is still right - but "
                f"a discrepancy this large points at counts_per_mm or the APT "
                f"velocity scaling being wrong.")
    if result.n_missing_anchors:
        result.warnings.append(
            f"{result.n_missing_anchors} position-pulse site(s) had no edge "
            f"inside a window. In Sequence mode that is expected - the "
            f"windows only see a fraction of each sweep - and the pulse "
            f"numbering is preserved across the gap, so positions after it "
            f"are not shifted.")
    if (math.isfinite(sweep_period_s)
            and trigger_dt_s > 0.5 * sweep_period_s):
        result.warnings.append(
            "The trigger channel is sparsed so hard that it has fewer than "
            "two samples per sweep; edge times are correspondingly coarse.")
    _fill_position_stats(result, copeak_times_s)
    return result


def positions_from_edges(trigger_flat, dt_s, t0_s, copeak_times_s,
                         config: PositionConfig):
    """Position from a fully digitised pulse train (flat records only)."""
    volts = np.asarray(trigger_flat, dtype=np.float64).reshape(-1)
    if volts.size < MIN_TRIGGER_SAMPLES_PER_SEGMENT:
        raise ValueError(
            f"position from pulse edges: the trigger channel holds "
            f"{volts.size} sample(s), too few to contain an edge. Reduce the "
            f"sparsing on that channel, or set "
            f"PositionConfig.source='velocity'.")
    result = PositionResult(positions_um=np.full(len(copeak_times_s), np.nan),
                            source="edges",
                            anchor_interval_um=config.trigger_interval_um,
                            commanded_velocity_mm_s=config.velocity_mm_s,
                            direction=config.direction)
    threshold = (config.threshold_v if math.isfinite(config.threshold_v)
                 else _auto_threshold(volts))
    if threshold is None:
        result.warnings.append(
            "The trigger channel shows no level change; falling back to the "
            "commanded velocity.")
        fallback = positions_from_velocity(copeak_times_s, config)
        fallback.warnings = result.warnings + fallback.warnings
        fallback.source = "velocity (no pulses seen)"
        return fallback
    above = volts > threshold
    rising = np.flatnonzero(~above[:-1] & above[1:])
    if rising.size < 2:
        result.warnings.append(
            "Fewer than two pulse edges in the trigger channel; falling back "
            "to the commanded velocity.")
        fallback = positions_from_velocity(copeak_times_s, config)
        fallback.warnings = result.warnings + fallback.warnings
        fallback.source = "velocity (no edges)"
        return fallback

    times = []
    for i in rising:
        v0, v1 = volts[i], volts[i + 1]
        frac = 0.5 if v1 == v0 else (threshold - v0) / (v1 - v0)
        times.append(t0_s + (i + float(np.clip(frac, 0.0, 1.0))) * dt_s)
    edge_times = np.asarray(times) - config.latency_s
    result.n_anchors = int(edge_times.size)
    if math.isfinite(config.velocity_mm_s):
        result.anchor_uncertainty_um = config.velocity_mm_s * 1e3 * dt_s

    # Every pulse is digitised in a flat record, so the observed median gap
    # really is one interval.
    nominal_gap = float(np.median(np.diff(edge_times)))
    index, residual, n_missing, gap_warnings = index_by_gaps(
        edge_times, nominal_gap, tolerance=config.max_lattice_residual)
    result.warnings.extend(gap_warnings)
    result.n_missing_anchors = n_missing
    first_position = (config.first_pulse_position_um
                      if math.isfinite(config.first_pulse_position_um)
                      else round(config.x_start_um / config.trigger_interval_um)
                      * config.trigger_interval_um)
    anchor_times = edge_times
    anchor_positions = (first_position
                        + config.direction * config.trigger_interval_um * index)

    copeak_times_s = np.asarray(copeak_times_s, dtype=np.float64)
    outside = ((copeak_times_s < anchor_times[0])
               | (copeak_times_s > anchor_times[-1]))
    out = np.interp(copeak_times_s, anchor_times, anchor_positions)
    result.n_out_of_range = int(np.count_nonzero(outside))
    if result.n_out_of_range and not config.allow_extrapolation:
        out = np.where(outside, np.nan, out)
    result.positions_um = out

    if anchor_times.size > 2:
        slope, intercept = np.polyfit(anchor_times, anchor_positions, 1)
        result.measured_velocity_mm_s = abs(slope) / 1.0e3
        residual = anchor_positions - (slope * anchor_times + intercept)
        result.ripple_rms_um = float(np.std(residual, ddof=1))
        result.ripple_pp_um = float(np.max(residual) - np.min(residual))
        with np.errstate(invalid="ignore", divide="ignore"):
            v = (np.diff(anchor_positions) / np.diff(anchor_times)) / 1.0e3
        v = np.abs(v[np.isfinite(v)])
        if v.size:
            result.velocity_std_mm_s = (float(np.std(v, ddof=1))
                                        if v.size > 1 else 0.0)
            result.velocity_min_mm_s = float(np.min(v))
            result.velocity_max_mm_s = float(np.max(v))
    if result.n_missing_anchors:
        result.warnings.append(
            f"{result.n_missing_anchors} position pulse(s) missing from the "
            f"train. Numbering is preserved across each gap, so later "
            f"positions are not shifted.")
    _fill_position_stats(result, copeak_times_s)
    return result


def _fill_position_stats(result: PositionResult, times_s):
    finite = result.positions_um[np.isfinite(result.positions_um)]
    if finite.size:
        result.start_um = float(finite[0])
        result.end_um = float(finite[-1])
        result.span_um = result.end_um - result.start_um
        if result.direction == 0:
            result.direction = 1 if result.span_um >= 0 else -1
    if result.n_out_of_range:
        result.warnings.append(
            f"{result.n_out_of_range} segment(s) fell outside the span the "
            f"position references cover and have no position; they are "
            f"dropped rather than clamped to the end.")
    return result


# ===========================================================================
# Spatial grid
# ===========================================================================

@dataclass(frozen=True)
class SpatialGrid:
    """A regular 1-D pixel grid, shared by every line in a map."""

    origin_um: float
    pitch_um: float
    n_pixels: int

    def __post_init__(self):
        if self.pitch_um <= 0:
            raise ValueError("pitch_um must be positive")
        if self.n_pixels < 1:
            raise ValueError("n_pixels must be at least 1")

    @classmethod
    def covering(cls, positions_um, pitch_um, origin_um=None, margin=0):
        """Smallest grid at `pitch_um` that covers `positions_um`.

        Worth pinning `origin_um` by hand for a real map, so that every line -
        and every repeat of the map - uses literally the same pixel edges.
        """
        x = np.asarray(positions_um, dtype=np.float64)
        x = x[np.isfinite(x)]
        if x.size == 0:
            raise ValueError("no finite positions to cover")
        lo = float(np.min(x)) if origin_um is None else float(origin_um)
        hi = float(np.max(x))
        n = int(math.floor((hi - lo) / pitch_um)) + 1 + 2 * int(margin)
        return cls(origin_um=lo - int(margin) * pitch_um,
                   pitch_um=float(pitch_um), n_pixels=max(n, 1))

    def centers_um(self) -> np.ndarray:
        return self.origin_um + (np.arange(self.n_pixels) + 0.5) * self.pitch_um

    def edges_um(self) -> np.ndarray:
        return self.origin_um + np.arange(self.n_pixels + 1) * self.pitch_um

    def index_of(self, positions_um) -> np.ndarray:
        """Pixel index for each position; -1 where it falls off the grid."""
        x = np.asarray(positions_um, dtype=np.float64)
        with np.errstate(invalid="ignore"):
            idx = np.floor((x - self.origin_um) / self.pitch_um)
        idx = np.where(np.isfinite(idx), idx, -1.0).astype(np.int64)
        return np.where((idx >= 0) & (idx < self.n_pixels), idx, -1)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GriddedLine:
    """One line resampled onto the grid."""

    grid: SpatialGrid
    traces: np.ndarray        # (n_pixels, width), NaN where empty
    counts: np.ndarray        # sweeps averaged into each pixel
    x_um: np.ndarray
    method: str
    direction: int
    n_outside: int
    n_no_position: int
    sweeps_per_pixel_mean: float
    sweeps_per_pixel_min: int
    sweeps_per_pixel_max: int
    n_empty_pixels: int
    warnings: list = field(default_factory=list)

    def summary(self) -> str:
        return format_gridded_line(self)


def bin_to_grid(traces, positions_um, grid: SpatialGrid, method="bin",
                direction=0) -> GriddedLine:
    """Average (or interpolate) per-sweep traces onto `grid`.

    The grid is defined by an origin and a pitch, not by the data, and that
    is what makes a bidirectional raster work. A reverse line's sweeps arrive
    in decreasing position order, but binning is order-free: a sweep at
    7.3 um lands in the pixel containing 7.3 um whichever way the stage was
    moving. So forward and reverse lines land on the same grid automatically,
    provided positions come from the position references rather than from an
    assumed start plus a velocity, and provided the output is indexed by grid
    position rather than by acquisition order. Both hold here.

    method 'bin'    - every sweep averaged into the pixel it falls in; the
                      honest choice when several sweeps land per pixel.
    method 'interp' - traces interpolated in position onto the pixel centres;
                      right when fewer than one sweep lands per pixel, and a
                      quiet lie if used to invent detail finer than the smear.

    Empty pixels are NaN with a count of zero, never zero-filled: a zero
    trace averaged into a map looks like a real measurement of nothing.
    """
    traces = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    positions_um = np.asarray(positions_um, dtype=np.float64)
    if traces.shape[0] != positions_um.size:
        raise ValueError(
            f"{traces.shape[0]} traces but {positions_um.size} positions")
    if method not in ("bin", "interp"):
        raise ValueError("method must be 'bin' or 'interp'")

    width = traces.shape[1]
    warnings = []
    finite = np.isfinite(positions_um)
    n_no_position = int(np.count_nonzero(~finite))
    traces, positions_um = traces[finite], positions_um[finite]

    if direction == 0 and positions_um.size >= 2:
        direction = 1 if positions_um[-1] >= positions_um[0] else -1

    idx = grid.index_of(positions_um)
    inside = idx >= 0
    n_outside = int(np.count_nonzero(~inside))
    used_idx, used = idx[inside], traces[inside]
    counts = np.bincount(used_idx, minlength=grid.n_pixels).astype(np.int64)

    if method == "bin":
        sums = np.zeros((grid.n_pixels, width), dtype=np.float64)
        if used_idx.size:
            np.add.at(sums, used_idx, np.nan_to_num(used, nan=0.0))
            valid = np.isfinite(used).astype(np.float64)
            weights = np.zeros((grid.n_pixels, width), dtype=np.float64)
            np.add.at(weights, used_idx, valid)
        else:
            weights = np.zeros((grid.n_pixels, width), dtype=np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = sums / weights
        out[weights == 0] = np.nan
    else:
        out = np.full((grid.n_pixels, width), np.nan)
        xs_all = positions_um[inside]
        if xs_all.size >= 2:
            order = np.argsort(xs_all)
            xs, ys = xs_all[order], used[order]
            centres = grid.centers_um()
            within = (centres >= xs[0]) & (centres <= xs[-1])
            if np.any(within):
                target = centres[within]
                interpolated = np.empty((target.size, width))
                for j in range(width):
                    interpolated[:, j] = np.interp(target, xs, ys[:, j])
                out[within] = interpolated
        counts = np.where(np.isfinite(out[:, 0]), 1, 0).astype(np.int64)

    occupied = counts > 0
    n_empty = int(np.count_nonzero(~occupied))
    if np.any(occupied):
        mean_spp = float(np.mean(counts[occupied]))
        min_spp, max_spp = int(np.min(counts[occupied])), int(np.max(counts[occupied]))
    else:
        mean_spp, min_spp, max_spp = 0.0, 0, 0

    if n_empty:
        warnings.append(
            f"{n_empty} of {grid.n_pixels} pixels had no sweep land in them "
            f"and are NaN. Either the pitch is finer than the smear per "
            f"sweep, or the line did not cover the whole grid.")
    if n_outside:
        warnings.append(
            f"{n_outside} sweep(s) fell outside the grid and were discarded.")
    if n_no_position:
        warnings.append(
            f"{n_no_position} segment(s) had no position and were discarded.")
    if mean_spp and mean_spp < 2.0 and method == "bin":
        warnings.append(
            f"Only {mean_spp:.2f} sweeps per pixel on average; pixel-to-pixel "
            f"noise is dominated by how many sweeps happened to land, not by "
            f"the signal.")

    return GriddedLine(
        grid=grid, traces=out, counts=counts, x_um=grid.centers_um(),
        method=method, direction=int(direction), n_outside=n_outside,
        n_no_position=n_no_position, sweeps_per_pixel_mean=mean_spp,
        sweeps_per_pixel_min=min_spp, sweeps_per_pixel_max=max_spp,
        n_empty_pixels=n_empty, warnings=warnings)


# ===========================================================================
# Collapsing a delay trace to map values
# ===========================================================================

def delay_spectrum(traces, delay_axis_s, skip_coincidence_s=0.0,
                   max_delay_s=0.0, window="none"):
    """Amplitude spectrum of each trace against REAL frequency.

    The copeak is a huge broadband spike, so `skip_coincidence_s` of delay
    can be dropped from the start before transforming. `max_delay_s`
    truncates the far end, which is worth doing when the capture window is
    much longer than the decay.

    The default window is rectangular, not Hann, and that is deliberate: the
    oscillation starts at delay zero and decays, so it sits at the very start
    of the transformed region, where a Hann window is near zero. Tapering
    would attenuate exactly the part of the trace that carries the signal.
    """
    traces = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    delay = np.asarray(delay_axis_s, dtype=np.float64)
    if traces.shape[1] != delay.size:
        raise ValueError("delay_axis_s must match the trace width")

    start = max(int(np.searchsorted(delay, 0.0)),
                int(np.searchsorted(delay, skip_coincidence_s))
                if skip_coincidence_s > 0 else 0)
    stop = delay.size
    if max_delay_s > 0:
        stop = min(stop, int(np.searchsorted(delay, max_delay_s)) + 1)
    cut, delay_cut = traces[:, start:stop], delay[start:stop]
    if delay_cut.size < 8:
        raise ValueError("not enough delay samples left to transform")

    step = float(np.mean(np.diff(delay_cut)))
    # Mean-remove without np.nanmean, which warns on an all-NaN row - and a
    # pixel no sweep landed in is exactly that.
    finite = np.isfinite(cut)
    counts = np.sum(finite, axis=1, keepdims=True)
    sums = np.where(finite, cut, 0.0).sum(axis=1, keepdims=True)
    data = cut - np.where(counts > 0, sums / np.maximum(counts, 1), 0.0)
    data = np.nan_to_num(data, nan=0.0)
    if window == "hann":
        data = data * np.hanning(data.shape[1])[None, :]
    spectrum = np.fft.rfft(data, axis=1)
    freqs = np.fft.rfftfreq(data.shape[1], d=step)
    return freqs, np.abs(spectrum) * (2.0 / data.shape[1])


def brillouin_metric(traces, delay_axis_s, search_hz=(1e9, 20e9),
                     skip_coincidence_s=0.0, max_delay_s=0.0, window="none"):
    """Per-trace (amplitude, frequency, bin width) from the spectrum peak.

    A windowed-DFT peak with parabolic interpolation of the three bins around
    the maximum, searched over a range rather than fitted. Deliberately so:
    it needs no starting guess, cannot fail to converge, and is a sound way
    to produce a map that then says where a proper damped-sinusoid fit is
    worth running. It is not a substitute for one.
    """
    traces = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    empty = ~np.any(np.isfinite(traces), axis=1)
    freqs, amp = delay_spectrum(
        traces, delay_axis_s, skip_coincidence_s=skip_coincidence_s,
        max_delay_s=max_delay_s, window=window)

    lo, hi = search_hz
    band = np.flatnonzero((freqs >= lo) & (freqs <= hi))
    if band.size == 0:
        raise ValueError(
            f"search range {lo:g}-{hi:g} Hz contains no DFT bin; the window "
            f"gives {freqs[1] - freqs[0]:.3g} Hz bins")
    sub = amp[:, band]
    best = np.argmax(sub, axis=1)
    amplitudes = sub[np.arange(sub.shape[0]), best]
    frequencies = freqs[band[best]].astype(np.float64)

    bin_width = float(freqs[1] - freqs[0])
    for n, b in enumerate(best):
        j = band[b]
        if 0 < j < amp.shape[1] - 1:
            y0, y1, y2 = amp[n, j - 1], amp[n, j], amp[n, j + 1]
            denom = y0 - 2.0 * y1 + y2
            if denom != 0:
                delta = 0.5 * (y0 - y2) / denom
                if abs(delta) <= 1.0:
                    frequencies[n] = freqs[j] + delta * bin_width

    return (np.where(empty, np.nan, amplitudes),
            np.where(empty, np.nan, frequencies), bin_width)


def peak_to_peak(traces, delay_axis_s, window_s=None):
    """Crude but robust per-trace contrast, over an optional delay window."""
    traces = np.atleast_2d(np.asarray(traces, dtype=np.float64))
    delay = np.asarray(delay_axis_s, dtype=np.float64)
    sel = (delay > 0 if window_s is None
           else (delay >= window_s[0]) & (delay <= window_s[1]))
    cut = traces[:, sel]
    with np.errstate(invalid="ignore"):
        return np.nanmax(cut, axis=1) - np.nanmin(cut, axis=1)


def feature_centroid_um(x_um, values, floor_frac=0.5):
    """Centroid of the brightest part of a line profile, in microns.

    Used to ask the one question a bidirectional raster must answer: does a
    known feature land in the same place scanning left-to-right as it does
    right-to-left? A centroid above a half-maximum floor is robust to a
    coarse pixel grid and a few empty pixels, which a plain argmax is not.
    """
    x = np.asarray(x_um, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    good = np.isfinite(v)
    if not np.any(good):
        return float("nan")
    x, v = x[good], v[good]
    base, span = np.min(v), np.max(v) - np.min(v)
    if span <= 0:
        return float("nan")
    weight = np.maximum(v - base - floor_frac * span, 0.0)
    total = np.sum(weight)
    return float(np.sum(x * weight) / total) if total > 0 else float("nan")


# ===========================================================================
# The chain
# ===========================================================================

@dataclass(frozen=True)
class ProcessingConfig:
    """Every knob of the chain, in one object that can be stored and reused."""

    timing: AsopsTiming = field(default_factory=AsopsTiming)
    copeaks: CopeakConfig = field(default_factory=CopeakConfig)
    segments: SegmentConfig = field(default_factory=SegmentConfig)
    dc: DcConfig = field(default_factory=DcConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    flat_peaks: FlatPeakConfig = field(default_factory=FlatPeakConfig)

    # Where the copeak is supposed to sit inside a raw captured segment, in
    # REAL delay from the segment's first sample - i.e. the pre-coincidence
    # delay the acquisition was set up with. NaN means "do not judge the
    # absolute position", and only drift and scatter are reported. This is
    # not the same quantity as `segments.pre_delay_s`, which is the window
    # the post-processing cuts afterwards and is deliberately smaller.
    acquisition_pre_delay_s: float = float("nan")

    normalise_by_dc: bool = True
    grid_source: str = "dr_over_r"      # or 'ac'
    pixel_pitch_um: float = 1.0
    # Pin the grid for a real map so every line uses the same pixel edges.
    grid_origin_um: float = float("nan")
    grid_n_pixels: int = 0
    grid_method: str = "bin"
    trigtime_tolerance: float = 0.25


@dataclass
class LineResult:
    """What one line produced."""

    segments: SegmentSet
    trigtime_stats: TrigtimeStats
    copeak_stats: CopeakStats
    dc: DcResult
    position: PositionResult
    gridded: GriddedLine
    gridded_from: str
    sample_rate_hz: float
    direction: int
    shape: str
    config: ProcessingConfig
    flat_peaks: FlatPeaks = None
    warnings: list = field(default_factory=list)

    @property
    def grid(self) -> SpatialGrid:
        return self.gridded.grid

    @property
    def positions_um(self) -> np.ndarray:
        return self.position.positions_um

    def delay_axis_s(self) -> np.ndarray:
        return self.segments.delay_axis_s()

    def traces(self) -> np.ndarray:
        if self.gridded_from == "dr_over_r":
            return self.dc.traces
        return self.segments.traces

    def report(self) -> str:
        return format_line_report(self)


def process_segmented(record_or_ac, sample_rate_hz=None, trigtime=None,
                      dc=None, dc_sparsing=1, trigger=None,
                      trigger_sparsing=1,
                      config: ProcessingConfig = ProcessingConfig(),
                      velocity_mm_s=float("nan"), x_start_um=0.0,
                      direction=None, grid: SpatialGrid = None) -> LineResult:
    """Run the chain on Sequence-mode data.

    Either pass a `flyscan.synth.SegmentedRecord` as the first argument, or
    pass the arrays: `ac` as (N, P), `trigtime` as (N, 2) in the convention
    documented in `flyscan.synth` - sample j of segment i at lab time
    trigtime[i,0] + trigtime[i,1] + j/sample_rate.
    """
    if hasattr(record_or_ac, "ac") and hasattr(record_or_ac, "trigtime"):
        record = record_or_ac
        ac = record.ac
        sample_rate_hz = record.sample_rate_hz
        trigtime = record.trigtime
        dc = record.dc if dc is None else dc
        dc_sparsing = record.dc_sparsing
        trigger = record.trigger if trigger is None else trigger
        trigger_sparsing = record.trigger_sparsing
        if direction is None:
            direction = record.direction
        if not math.isfinite(velocity_mm_s):
            velocity_mm_s = record.velocity_mm_s
    else:
        ac = record_or_ac
    if sample_rate_hz is None or trigtime is None:
        raise ValueError("segmented processing needs sample_rate_hz and trigtime")
    if direction is None:
        direction = config.position.direction

    ac = np.atleast_2d(np.asarray(ac, dtype=np.float64))
    trigtime = np.atleast_2d(np.asarray(trigtime, dtype=np.float64))
    if trigtime.shape[0] != ac.shape[0]:
        raise ValueError(
            f"{ac.shape[0]} segments but {trigtime.shape[0]} trigtime rows")
    warnings = []
    timing = config.timing

    trig_stats, trig_fit = analyse_trigtime(
        trigtime, timing=timing, tolerance=config.trigtime_tolerance)

    expected = (timing.lab_time(config.acquisition_pre_delay_s)
                * sample_rate_hz
                if math.isfinite(config.acquisition_pre_delay_s)
                else float("nan"))
    copeak_samples, amplitudes, copeak_stats = find_copeaks(
        ac, expected_sample=expected, config=config.copeaks)
    # Delay equivalents of the lab-time figures, now that the rate is known.
    copeak_stats.offset_delay_s = timing.delay_time(
        copeak_stats.offset_samples / sample_rate_hz)
    copeak_stats.scatter_delay_s = timing.delay_time(
        copeak_stats.scatter_samples / sample_rate_hz)
    copeak_stats.drift_delay_total_s = timing.delay_time(
        copeak_stats.drift_samples_total / sample_rate_hz)

    segments = build_segments(
        ac, sample_rate_hz, copeak_samples, trigtime[:, 0],
        trig_fit.index, amplitude=amplitudes, timing=timing,
        config=config.segments, trigger_offset_s=trigtime[:, 1],
        exclude=trig_fit.outlier)

    return _finish_line(
        segments, trig_stats, copeak_stats, dc, dc_sparsing, trigger,
        trigger_sparsing, config, sample_rate_hz, velocity_mm_s, x_start_um,
        direction, grid, "segmented", warnings, None)


def process_flat(record_or_ac, sample_rate_hz=None, dc=None, dc_sparsing=1,
                 trigger=None, trigger_sparsing=1, t0_s=0.0,
                 config: ProcessingConfig = ProcessingConfig(),
                 velocity_mm_s=float("nan"), x_start_um=0.0, direction=None,
                 grid: SpatialGrid = None) -> LineResult:
    """Run the chain on one long continuous record.

    The diagnostic path: this is how the coincidence's position inside the
    12.5 ns window gets found in the first place. Peaks are located the hard
    way, then the record is cut into per-sweep segments and the rest of the
    chain is identical to the Sequence-mode one.
    """
    if hasattr(record_or_ac, "ac") and hasattr(record_or_ac, "t0_s"):
        record = record_or_ac
        ac = record.ac
        sample_rate_hz = record.sample_rate_hz
        dc = record.dc if dc is None else dc
        dc_sparsing = record.dc_sparsing
        trigger = record.trigger if trigger is None else trigger
        trigger_sparsing = record.trigger_sparsing
        t0_s = record.t0_s
        if direction is None:
            direction = record.direction
        if not math.isfinite(velocity_mm_s):
            velocity_mm_s = record.velocity_mm_s
    else:
        ac = record_or_ac
    if sample_rate_hz is None:
        raise ValueError("flat processing needs sample_rate_hz")
    if direction is None:
        direction = config.position.direction

    ac = np.asarray(ac, dtype=np.float64).reshape(-1)
    timing = config.timing
    warnings = []

    peaks = find_flat_peaks(ac, sample_rate_hz, timing=timing,
                            config=config.flat_peaks)
    warnings.extend(peaks.warnings)

    # Dress the flat record up as segments: each peak becomes a segment whose
    # copeak sits `pre` samples in, so everything downstream is shared.
    pre = int(round(timing.lab_time(config.segments.pre_delay_s)
                    * sample_rate_hz))
    post = int(round(timing.lab_time(config.segments.post_delay_s)
                     * sample_rate_hz))
    width = pre + post
    rows, kept = [], []
    for n, apex in enumerate(peaks.index):
        base = int(math.floor(apex)) - pre
        if base < 0 or base + width + 1 > ac.size:
            continue
        rows.append(ac[base:base + width + 1])
        kept.append(n)
    kept = np.asarray(kept, dtype=np.int64)
    if kept.size == 0:
        raise ValueError("no peak in this record had a full window around it")
    stacked = np.vstack(rows)
    # Copeak position inside each extracted row, keeping the sub-sample part.
    local_copeak = peaks.index[kept] - (
        np.floor(peaks.index[kept]) - pre)
    row_start_time = (np.floor(peaks.index[kept]) - pre) / sample_rate_hz + t0_s

    trig_stats, _ = analyse_trigtime(
        np.column_stack([peaks.times_s() + t0_s,
                         np.zeros(peaks.index.size)]),
        timing=timing, tolerance=config.trigtime_tolerance)

    _, amplitudes, copeak_stats = find_copeaks(
        stacked, expected_sample=float(pre), config=config.copeaks)
    copeak_stats.offset_delay_s = timing.delay_time(
        copeak_stats.offset_samples / sample_rate_hz)
    copeak_stats.scatter_delay_s = timing.delay_time(
        copeak_stats.scatter_samples / sample_rate_hz)
    copeak_stats.drift_delay_total_s = timing.delay_time(
        copeak_stats.drift_samples_total / sample_rate_hz)

    segments = build_segments(
        stacked, sample_rate_hz, local_copeak, row_start_time,
        peaks.sweep_index[kept], amplitude=peaks.amplitude[kept],
        timing=timing, config=config.segments,
        exclude=peaks.is_edge[kept])

    return _finish_line(
        segments, trig_stats, copeak_stats, dc, dc_sparsing, trigger,
        trigger_sparsing, config, sample_rate_hz, velocity_mm_s, x_start_um,
        direction, grid, "flat", warnings, peaks, flat_t0_s=t0_s)


def process_synth(record, config: ProcessingConfig = None,
                  grid: SpatialGrid = None) -> LineResult:
    """Run the chain over a `flyscan.synth` record, segmented or flat.

    A convenience for the verification scripts, so that they drive exactly
    the code the live acquisition will call and only the source of the
    samples differs. It fills in the two things the chain cannot know from
    the arrays alone: where the stage was at the time origin, and what
    pre-coincidence delay the acquisition was set up with.
    """
    from dataclasses import replace
    cfg = record.config
    config = config if config is not None else ProcessingConfig()
    config = replace(
        config, timing=cfg.timing,
        acquisition_pre_delay_s=(
            config.acquisition_pre_delay_s
            if math.isfinite(config.acquisition_pre_delay_s)
            else cfg.segment_pre_delay_s),
        position=replace(
            config.position,
            trigger_interval_um=cfg.trigger_interval_um,
            direction=record.direction,
            velocity_mm_s=(config.position.velocity_mm_s
                           if math.isfinite(config.position.velocity_mm_s)
                           else record.velocity_mm_s)))
    x_zero = float(record.truth.get("x_at_t_zero_um", cfg.x_start_um))
    if record.truth.get("shape") == "flat":
        return process_flat(record, config=config, x_start_um=x_zero,
                            grid=grid)
    return process_segmented(record, config=config, x_start_um=x_zero,
                             grid=grid)


def _finish_line(segments, trig_stats, copeak_stats, dc, dc_sparsing, trigger,
                 trigger_sparsing, config, sample_rate_hz, velocity_mm_s,
                 x_start_um, direction, grid, shape, warnings, peaks,
                 flat_t0_s=0.0):
    """The part of the chain both acquisition shapes share."""
    from dataclasses import replace

    # --- dR/R -------------------------------------------------------------
    gridded_from = "ac"
    if config.normalise_by_dc and dc is not None:
        dc_array = np.asarray(dc, dtype=np.float64)
        if shape == "flat":
            # A flat DC record has no segments of its own: take each
            # segment's slice around its copeak.
            dc_dt = dc_sparsing / sample_rate_hz
            width = max(int(round(segments.width / dc_sparsing)), 1)
            starts = np.rint(
                (segments.copeak_time_s - flat_t0_s
                 - segments.pre_samples / sample_rate_hz) / dc_dt
            ).astype(np.int64)
            rows = []
            for s in starts:
                lo = int(np.clip(s, 0, max(dc_array.size - width, 0)))
                rows.append(dc_array[lo:lo + width])
            dc_rows = np.vstack(rows) if rows else np.zeros((0, width))
            dc_result = normalise_by_dc(segments, dc_rows, config.dc,
                                        source_index=np.arange(dc_rows.shape[0]))
        else:
            dc_result = normalise_by_dc(segments, dc_array, config.dc)
        if config.grid_source == "dr_over_r":
            gridded_from = "dr_over_r"
    else:
        if config.normalise_by_dc:
            warnings.append(
                "No DC channel supplied, so the map is raw AC volts, not "
                "dR/R: laser power drift and sample reflectivity are still "
                "in it.")
        dc_result = DcResult(level_v=np.full(segments.n_segments, np.nan),
                             traces=segments.traces.copy())
    warnings.extend(dc_result.warnings)

    # --- position ---------------------------------------------------------
    pos_config = replace(config.position, direction=int(direction),
                         x_start_um=x_start_um)
    if not math.isfinite(pos_config.velocity_mm_s):
        pos_config = replace(pos_config, velocity_mm_s=velocity_mm_s)

    source = pos_config.source
    if trigger is None and source in ("anchors", "edges"):
        source = "velocity"
        warnings.append(
            f"No trigger channel supplied, so position fell back to the "
            f"commanded velocity instead of '{pos_config.source}'.")
    if segments.n_segments == 0:
        # Nothing to place. Returning an empty result keeps the failure
        # reported above as the explanation, instead of burying it under a
        # second one from the position code.
        source = "none"
        position = PositionResult(positions_um=np.zeros(0),
                                  source="none (no segments)",
                                  direction=int(direction))

    if source == "anchors":
        where = "position from pulse anchors"
        trig_dt = trigger_sparsing / sample_rate_hz
        trig_rows = _check_trigger_shape(trigger, None, trig_dt, where)
        needed = int(segments.source_index.max()) + 1
        if trig_rows.shape[0] < needed:
            raise ValueError(
                f"{where}: the trigger channel has {trig_rows.shape[0]} "
                f"row(s) but segment {needed - 1} was kept from the AC "
                f"channel. Every channel of one acquisition must have one row "
                f"per segment; a trigger channel stored flat, or stored for a "
                f"different number of segments, cannot be aligned to the "
                f"segments.")
        position = positions_from_anchors(
            trig_rows[segments.source_index], trig_dt,
            segments.trigger_time_s, segments.copeak_time_s, pos_config,
            sweep_period_s=segments.sweep_period_s)
    elif source == "edges":
        position = positions_from_edges(
            np.asarray(trigger, dtype=np.float64).reshape(-1),
            trigger_sparsing / sample_rate_hz, flat_t0_s,
            segments.copeak_time_s, pos_config)
    elif source == "velocity":
        position = positions_from_velocity(segments.copeak_time_s, pos_config)
    elif source != "none":
        raise ValueError(
            "PositionConfig.source must be 'anchors', 'edges' or 'velocity'")

    # --- grid -------------------------------------------------------------
    traces = (dc_result.traces if gridded_from == "dr_over_r"
              else segments.traces)
    if grid is None:
        if config.grid_n_pixels > 0 and math.isfinite(config.grid_origin_um):
            grid = SpatialGrid(origin_um=config.grid_origin_um,
                               pitch_um=config.pixel_pitch_um,
                               n_pixels=config.grid_n_pixels)
        else:
            finite = position.positions_um[np.isfinite(position.positions_um)]
            if finite.size == 0:
                raise ValueError(
                    f"no segment had a position, so no grid can be derived "
                    f"from this line ({segments.n_segments} segment(s) "
                    f"survived of {segments.n_segments + segments.n_discarded};"
                    f" position source '{position.source}'). The warnings on "
                    f"the line say why; pass an explicit grid if the rest of "
                    f"the map should still be built.")
            origin = (config.grid_origin_um
                      if math.isfinite(config.grid_origin_um) else None)
            grid = SpatialGrid.covering(finite, config.pixel_pitch_um,
                                        origin_um=origin)
            warnings.append(
                "Grid derived from this line's own extent. For a raster, pin "
                "grid_origin_um and grid_n_pixels so every line shares the "
                "same pixel edges.")

    gridded = bin_to_grid(traces, position.positions_um, grid,
                          method=config.grid_method,
                          direction=position.direction)

    if segments.n_discarded:
        detail = ", ".join(f"{k} {v}"
                           for k, v in segments.discarded_reasons.items())
        warnings.append(
            f"{segments.n_discarded} segment(s) discarded ({detail}).")
    needed = (abs(copeak_stats.drift_samples_total)
              + 3.0 * (copeak_stats.scatter_samples
                       if math.isfinite(copeak_stats.scatter_samples) else 0.0))
    if (segments.capture_points and math.isfinite(needed)
            and needed > segments.slack_samples):
        warnings.append(
            f"The copeak moves by about {needed:.1f} samples across this line "
            f"(drift plus three times the scatter) but the processing window "
            f"leaves only {segments.slack_samples} samples of slack inside "
            f"the {segments.capture_points}-sample captured segment. Segments "
            f"at one end of the line will not fit the window and are being "
            f"dropped. Narrow pre_delay_s / post_delay_s, or capture a wider "
            f"window.")
    if segments.n_segments == 0:
        warnings.append(
            f"NO SEGMENTS SURVIVED. The processing window is "
            f"{segments.pre_samples} + {segments.post_samples} samples; if "
            f"the reason above is window_off_segment, that window does not "
            f"fit inside the captured one once each segment is realigned on "
            f"its own copeak. Narrow pre_delay_s / post_delay_s, or capture "
            f"a wider window.")

    return LineResult(
        segments=segments, trigtime_stats=trig_stats,
        copeak_stats=copeak_stats, dc=dc_result, position=position,
        gridded=gridded, gridded_from=gridded_from,
        sample_rate_hz=sample_rate_hz, direction=position.direction,
        shape=shape, config=config, flat_peaks=peaks, warnings=warnings)


# ===========================================================================
# Reports
# ===========================================================================

def format_trigtime_stats(s: TrigtimeStats) -> str:
    lines = [
        "TRIGTIME (offset-lock and timebase diagnostic)",
        f"  segments                 {s.n_segments:,d} over "
        f"{s.duration_s * 1e3:.3f} ms",
        f"  sweep index range        {s.first_sweep_index} .. "
        f"{s.last_sweep_index}",
        f"  mean spacing             {s.spacing_mean_s * 1e6:.6f} us "
        f"(median {s.spacing_median_s * 1e6:.6f}, "
        f"std {s.spacing_std_s * 1e9:.4g} ns)",
        f"  min / max spacing        {s.spacing_min_s * 1e6:.6f} / "
        f"{s.spacing_max_s * 1e6:.6f} us",
        f"  implied sweep rate       {s.implied_sweep_rate_hz:,.4f} Hz "
        f"(nominal {s.nominal_sweep_rate_hz:,.1f}, "
        f"error {s.sweep_rate_error_ppm:,.1f} ppm)",
        f"  trigger jitter           {s.jitter_rms_s * 1e9:.3f} ns rms "
        f"= {s.jitter_rms_delay_s * 1e15:.2f} fs of delay",
        f"  missed / extra triggers  {s.n_missed_triggers} / "
        f"{s.n_extra_triggers}",
        f"  TRIGTIME offsets         mean "
        f"{s.offset_mean_s * 1e6:.4f} us, spread "
        f"{s.offset_spread_s * 1e9:.4g} ns (sample-clock phase)",
    ]
    if s.warnings:
        lines.append("  warnings:")
        lines.extend(f"    ! {w}" for w in s.warnings)
    return "\n".join(lines)


def format_copeak_stats(s: CopeakStats) -> str:
    lines = [
        "Copeak validation (trigger-path diagnostic)",
        f"  found                    {s.n_found} of {s.n_segments} segments",
        f"  position in window       {s.median_sample:.2f} samples "
        f"(expected {s.expected_sample:.2f}, "
        f"offset {s.offset_samples:+.2f} = "
        f"{s.offset_delay_s * 1e12:+.2f} ps of delay)",
        f"  scatter                  {s.scatter_samples:.3f} samples "
        f"= {s.scatter_delay_s * 1e15:.1f} fs of delay",
        f"  drift across the line    {s.drift_samples_total:+.2f} samples "
        f"= {s.drift_delay_total_s * 1e12:+.2f} ps of delay "
        f"({s.drift_fraction_of_window * 100:.2f}% of the window)",
        f"  copeak FWHM              {s.fwhm_samples_median:.1f} samples",
        f"  amplitude                median {s.amplitude_median_v:.4g} V, "
        f"range {s.amplitude_min_v:.4g} .. {s.amplitude_max_v:.4g} V",
        f"  noise                    {s.noise_sigma_v:.4g} V rms",
    ]
    if s.warnings:
        lines.append("  warnings:")
        lines.extend(f"    ! {w}" for w in s.warnings)
    return "\n".join(lines)


def format_dc_result(r: DcResult) -> str:
    lines = [
        "Probe DC (one scalar per copeak)",
        f"  samples per segment      {r.samples_per_segment} "
        f"-> averaged to one value",
        f"  level                    mean {r.mean_v:.5g} V, "
        f"range {r.min_v:.5g} .. {r.max_v:.5g} V "
        f"({r.variation_fraction * 100:.1f}% along the line)",
        f"  variation within a segment "
        f"{r.intra_segment_fraction * 100:.2f}%",
        f"  unusable segments        {r.n_bad}",
    ]
    if r.warnings:
        lines.append("  warnings:")
        lines.extend(f"    ! {w}" for w in r.warnings)
    return "\n".join(lines)


def format_position_result(r: PositionResult) -> str:
    arrow = "+x" if r.direction >= 0 else "-x"
    lines = [f"Position ({r.source})"]
    if r.n_anchors:
        lines.append(
            f"  anchors                  {r.n_anchors} pulse edges at "
            f"{r.anchor_interval_um:.4g} um, {r.n_missing_anchors} sites "
            f"unseen, +-{r.anchor_uncertainty_um:.4g} um each")
        lines.append(
            f"  anchor rate              {r.anchor_rate_hz:,.0f} Hz "
            f"-> ripple resolved up to about "
            f"{r.ripple_bandwidth_hz:,.0f} Hz")
    lines += [
        f"  travel                   {r.start_um:.4f} -> {r.end_um:.4f} um "
        f"({r.span_um:+.4f} um, {arrow})",
        f"  velocity                 measured "
        f"{r.measured_velocity_mm_s:.5g} mm/s, commanded "
        f"{r.commanded_velocity_mm_s:.5g} mm/s",
        f"  velocity spread          std {r.velocity_std_mm_s:.4g}, "
        f"range {r.velocity_min_mm_s:.5g} .. {r.velocity_max_mm_s:.5g} mm/s",
        f"  ripple vs straight line  {r.ripple_rms_um:.4g} um rms, "
        f"{r.ripple_pp_um:.4g} um peak-to-peak",
    ]
    if r.warnings:
        lines.append("  warnings:")
        lines.extend(f"    ! {w}" for w in r.warnings)
    return "\n".join(lines)


def format_gridded_line(line: GriddedLine) -> str:
    arrow = "forward (+x)" if line.direction >= 0 else "reverse (-x)"
    lines = [
        "Spatial grid",
        f"  direction                {arrow}",
        f"  grid                     {line.grid.n_pixels} px of "
        f"{line.grid.pitch_um:.4g} um from {line.grid.origin_um:.4f} um "
        f"({line.method})",
        f"  sweeps per pixel         mean {line.sweeps_per_pixel_mean:.2f}, "
        f"min {line.sweeps_per_pixel_min}, max {line.sweeps_per_pixel_max}",
        f"  empty pixels             {line.n_empty_pixels}",
    ]
    if line.warnings:
        lines.append("  warnings:")
        lines.extend(f"    ! {w}" for w in line.warnings)
    return "\n".join(lines)


def format_line_report(result: LineResult) -> str:
    seg = result.segments
    delay = seg.delay_axis_s()
    lines = [
        "-" * 72,
        f"FLY-SCAN LINE  [{result.shape}]",
        "-" * 72,
        f"  sample rate              {result.sample_rate_hz / 1e6:.6g} MS/s",
        f"  segments kept            {seg.n_segments} "
        f"({seg.n_discarded} discarded)",
        f"  window slack             {seg.slack_samples} samples spare in a "
        f"{seg.capture_points}-sample captured segment",
        f"  delay window per trace   {delay[-1] * 1e12:,.1f} ps after "
        f"coincidence, {-delay[0] * 1e12:,.1f} ps before, {seg.width} "
        f"samples ({seg.align} aligned)",
        f"  delay per sample         {(delay[1] - delay[0]) * 1e12:.4f} ps",
        f"  gridded quantity         {result.gridded_from}",
        "",
        result.trigtime_stats.report(),
        "",
        result.copeak_stats.report(),
        "",
        result.dc.report(),
        "",
        result.position.report(),
        "",
        result.gridded.summary(),
    ]
    if result.warnings:
        lines.append("")
        lines.append("Line warnings:")
        lines.extend(f"  ! {w}" for w in result.warnings)
    lines.append("-" * 72)
    return "\n".join(lines)
