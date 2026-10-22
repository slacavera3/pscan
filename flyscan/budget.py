"""ASOPS timing arithmetic and the acquisition budget for a fly-scan line.

Two directions, and the second one is the point:

  forward   given a line length, a stage velocity, a sample rate and a
            segment capture window, say what that costs in smear, memory,
            time and delay resolution, and whether it fits.

  inverse   given a spot size, a pixel pitch and a memory or download
            budget, say which sample rate and velocity satisfy them.

Choosing parameters by hand on this instrument is unpleasant because the
constraints pull against each other, and the binding one is not the obvious
one. The stage is an MLS203-family direct-drive linear motor on a BBD303
servo: it will do about 250 mm/s. But one ASOPS sweep takes 100 us, so at
250 mm/s a single delay trace is smeared over 25 um of sample - far wider
than a ~1 um optical spot, and useless. The governing limit is

    smear per sweep = velocity / sweep_rate  <=  spot size

which caps the velocity at ~10 mm/s for a 1 um spot, or ~5 mm/s for half-spot
smear: a few percent of what the stage can do. Smear against spot size is
reported first for that reason, and the solver treats it as a primary
constraint alongside memory and download time.

Acquisition model
-----------------
The scope is triggered by the 10 kHz ASOPS clock, once per delay sweep, so
Sequence mode is the natural model: N segments per line, one segment per
sweep, each capturing only the useful part of the 12.5 ns delay window. The
capture window is therefore a first-class budget input - it sets points per
segment and so the whole data volume. A 2 ns window out of 12.5 ns is a
6.25x reduction over capturing the whole sweep, which is enough to flip the
balance between flight time and transfer time.

`mode="flat"` costs the alternative: one long continuous record per line, no
segmentation by the scope. That is how the signal gets found in the first
place - capture a whole sweep once and look at where things are - so the
budget reports both and the reduction factor between them.

Units, stated once and used throughout: lengths in microns unless the name
says mm, velocities in mm/s (the controller's unit), rates in Hz or S/s,
delays and times in seconds, and 1 MB = 1e6 bytes.

Numbers in here that are assumptions rather than derived facts are collected
by `assumption_notes()` and printed with every report, because several of
them cannot be settled without the instrument.
"""

from dataclasses import dataclass, field, asdict
import math

BYTES_PER_MB = 1.0e6

# Conventional defaults for this instrument. Configurable because the offset
# lock is a knob on the bench and the rep rate depends on the laser.
DEFAULT_REP_RATE_HZ = 80.0e6
DEFAULT_SWEEP_RATE_HZ = 10.0e3

# Points per cycle used when asking "what is the highest Brillouin frequency
# this sample rate can carry". Two is Nyquist and is honest only in the sense
# that the frequency is not aliased; fitting a damped sinusoid to two points
# per cycle recovers nothing useful once noise and a finite decay are in play.
# Ten leaves amplitude and decay identifiable as well as frequency.
DEFAULT_POINTS_PER_CYCLE = 10.0

# Placeholder until MSIZ? is run on the instrument with the intended channels
# enabled. Deliberately a round, obviously-placeholder number, reported as an
# assumption so it cannot quietly become a fact. In Sequence mode with a
# narrow capture window there is large headroom, so being wrong here is cheap.
ASSUMED_RECORD_LENGTH = 16_000_000

# Sustained waveform transfer over LAN for a scope of this vintage. Measured
# values for VXI-11 / LXI block transfers land anywhere between ~2 MB/s and
# ~40 MB/s depending on the interface and the host, so this is a guess with a
# wide error bar. Measure it with one long waveform and a stopwatch.
ASSUMED_TRANSFER_RATE_MB_S = 10.0

# WaveRunner HRO 66Zi front-panel limits.
SCOPE_MAX_SAMPLE_RATE_HZ = 2.0e9

# Segments the scope will accept in one Sequence acquisition, and the time it
# needs between them. Both ASSUMED - the segment count depends on installed
# options, and the re-arm time has to be below the 100 us between ASOPS
# triggers or segments will simply be missed.
ASSUMED_MAX_SEGMENTS = 10_000
ASSUMED_SEGMENT_REARM_S = 2.0e-6

# Roles a scope channel can play in this experiment. This module owns these
# names: `synth` and `io_hdf5` import them from here rather than keeping their
# own copies, so there is one spelling of each and nothing to drift.
ROLE_AC = "ac"            # Brillouin / pump-probe signal, full rate
ROLE_DC = "dc"            # probe DC, denominator of dR/R
ROLE_TRIGGER = "trigger"  # BBD303 position-trigger pulse train
ROLE_SPARE = "spare"
# Not a channel: the per-segment trigger times that come with a Sequence-mode
# acquisition. Named here so the file format and the generator agree on it.
ROLE_TRIGTIME = "trigtime"
# An analog position ramp, the secondary position path.
ROLE_POSITION_ANALOG = "position_analog"

CHANNEL_ROLES = (ROLE_AC, ROLE_DC, ROLE_TRIGGER, ROLE_SPARE,
                 ROLE_POSITION_ANALOG)


# ---------------------------------------------------------------------------
# ASOPS timing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AsopsTiming:
    """The ASOPS timing relations for one pair of lasers.

    Every number here follows from two quantities, the repetition rate and
    the offset between the lasers:

      stretch factor      S  = f_rep / df          (80 MHz / 10 kHz = 8000)
      delay window        Td = 1 / f_rep           (12.5 ns of delay)
      sweep period        Ts = 1 / df              (100 us of lab time)
      probe pulses/sweep       = S                 (8000 delay points)
      intrinsic delay step     = Td / S            (1.5625 ps)
      real <-> lab frequency   f_lab = f_real / S  (5 GHz -> 625 kHz)

    The last line is what makes the scheme work: a 5 GHz Brillouin
    oscillation arrives at the digitiser as 625 kHz. It is also what makes
    the sample-rate ceiling useful - above one sample per probe pulse
    (f_rep, i.e. 80 MS/s) there is nothing new to collect, because between
    two probe pulses nothing has been measured.
    """

    rep_rate_hz: float = DEFAULT_REP_RATE_HZ
    sweep_rate_hz: float = DEFAULT_SWEEP_RATE_HZ

    def __post_init__(self):
        if self.rep_rate_hz <= 0:
            raise ValueError("rep_rate_hz must be positive")
        if self.sweep_rate_hz <= 0:
            raise ValueError("sweep_rate_hz must be positive")
        if self.sweep_rate_hz >= self.rep_rate_hz:
            raise ValueError(
                "sweep_rate_hz must be far below rep_rate_hz for ASOPS "
                f"(got {self.sweep_rate_hz:g} Hz offset on "
                f"{self.rep_rate_hz:g} Hz rep rate)")

    @property
    def stretch_factor(self) -> float:
        """Lab time per unit delay time. 8000 for 80 MHz / 10 kHz."""
        return self.rep_rate_hz / self.sweep_rate_hz

    @property
    def delay_window_s(self) -> float:
        """Pump-probe delay swept out once per sweep: one laser period."""
        return 1.0 / self.rep_rate_hz

    @property
    def sweep_period_s(self) -> float:
        """Lab time taken to sweep the delay window once."""
        return 1.0 / self.sweep_rate_hz

    @property
    def pulses_per_sweep(self) -> float:
        """Probe pulses per sweep, i.e. physically distinct delay points."""
        return self.stretch_factor

    @property
    def intrinsic_delay_resolution_s(self) -> float:
        """Delay step between consecutive probe pulses - the hard floor.

        No sample rate beats this: between two probe pulses the sample has
        not been interrogated at all.
        """
        return self.delay_window_s / self.stretch_factor

    def samples_per_sweep(self, sample_rate_hz: float) -> float:
        """Digitiser samples in one whole ASOPS sweep."""
        return sample_rate_hz / self.sweep_rate_hz

    def delay_resolution_s(self, sample_rate_hz: float) -> float:
        """Delay step per digitiser sample, floored at the intrinsic step."""
        sampled = 1.0 / (sample_rate_hz * self.stretch_factor)
        return max(sampled, self.intrinsic_delay_resolution_s)

    def lab_frequency(self, real_hz: float) -> float:
        """Frequency as the digitiser sees it."""
        return real_hz / self.stretch_factor

    def real_frequency(self, lab_hz: float) -> float:
        """Frequency in the sample, given what the digitiser saw."""
        return lab_hz * self.stretch_factor

    def lab_time(self, delay_s: float) -> float:
        """Lab time corresponding to an interval of pump-probe delay."""
        return delay_s * self.stretch_factor

    def delay_time(self, lab_s: float) -> float:
        """Pump-probe delay corresponding to an interval of lab time."""
        return lab_s / self.stretch_factor

    def max_real_frequency(
            self, sample_rate_hz: float,
            points_per_cycle: float = DEFAULT_POINTS_PER_CYCLE) -> float:
        """Highest real Brillouin frequency adequately sampled.

        The criterion is explicit and configurable: `points_per_cycle`
        digitiser samples per cycle of the lab-frame oscillation. Set it to 2
        for the bare Nyquist number, which is much larger and much less
        useful. The rate is clamped at the rep rate first, since oversampling
        one probe pulse adds no delay points.
        """
        effective = min(sample_rate_hz, self.rep_rate_hz)
        return self.stretch_factor * effective / points_per_cycle

    def required_sample_rate(
            self, real_hz: float,
            points_per_cycle: float = DEFAULT_POINTS_PER_CYCLE) -> float:
        """Sample rate needed to carry `real_hz` at the stated criterion."""
        return points_per_cycle * real_hz / self.stretch_factor

    def travel_per_sweep_um(self, velocity_mm_s: float) -> float:
        """Distance covered during one ASOPS sweep, in microns.

        The hard spatial quantum of a fly-scan: one delay trace is smeared
        over this much sample, so no pixel pitch finer than this means
        anything, however finely the data are binned.
        """
        return velocity_mm_s * 1.0e3 * self.sweep_period_s

    def describe(self) -> str:
        return "\n".join([
            f"  laser rep rate      {self.rep_rate_hz / 1e6:.6g} MHz",
            f"  ASOPS sweep rate    {self.sweep_rate_hz / 1e3:.6g} kHz",
            f"  stretch factor      {self.stretch_factor:.6g}",
            f"  delay window        {self.delay_window_s * 1e9:.6g} ns",
            f"  sweep period        {self.sweep_period_s * 1e6:.6g} us",
            f"  probe pulses/sweep  {self.pulses_per_sweep:.6g}",
            f"  intrinsic delay res "
            f"{self.intrinsic_delay_resolution_s * 1e12:.6g} ps",
        ])


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelPlan:
    """One scope channel: what it carries and how hard it is sparsed."""

    name: str
    role: str
    sparsing: int = 1

    def __post_init__(self):
        if self.sparsing < 1:
            raise ValueError(f"{self.name}: sparsing must be >= 1")
        if self.role not in CHANNEL_ROLES:
            raise ValueError(f"{self.name}: unknown role {self.role!r}")


# The intended allocation on the four-channel HRO 66Zi. In Sequence mode the
# sparsing on C2 and C3 is much gentler than it would be for a flat record,
# because each segment is short: sparsing 1:20 of a 160-point segment leaves
# eight DC samples to average, while 1:100 would leave one and a half.
DEFAULT_CHANNELS = (
    ChannelPlan("C1", ROLE_AC, 1),
    ChannelPlan("C2", ROLE_DC, 20),
    ChannelPlan("C3", ROLE_TRIGGER, 20),
)


def default_channel_plan(ac_channels=1, dc_sparsing=20, trigger_sparsing=20,
                         spare_channels=0):
    """Build a channel plan; a sparsing of 0 omits that channel entirely."""
    plan, n = [], 1
    for _ in range(max(int(ac_channels), 0)):
        plan.append(ChannelPlan(f"C{n}", ROLE_AC, 1))
        n += 1
    if dc_sparsing:
        plan.append(ChannelPlan(f"C{n}", ROLE_DC, int(dc_sparsing)))
        n += 1
    if trigger_sparsing:
        plan.append(ChannelPlan(f"C{n}", ROLE_TRIGGER, int(trigger_sparsing)))
        n += 1
    for _ in range(max(int(spare_channels), 0)):
        plan.append(ChannelPlan(f"C{n}", ROLE_SPARE, 1))
        n += 1
    if not plan:
        raise ValueError("a channel plan needs at least one channel")
    return tuple(plan)


@dataclass(frozen=True)
class ScopeConfig:
    """What the digitiser can do, and what transferring from it costs."""

    record_length: int = ASSUMED_RECORD_LENGTH
    max_sample_rate_hz: float = SCOPE_MAX_SAMPLE_RATE_HZ
    bytes_per_sample: int = 2
    transfer_rate_mb_s: float = ASSUMED_TRANSFER_RATE_MB_S
    max_segments: int = ASSUMED_MAX_SEGMENTS
    segment_rearm_s: float = ASSUMED_SEGMENT_REARM_S
    # LeCroy interleaves acquisition memory, so per-channel record length
    # drops once more than `interleave_channels` are enabled. The factor is
    # an assumption; set it to 1.0 to switch the model off.
    interleave_channels: int = 2
    interleave_factor: float = 2.0
    record_length_confirmed: bool = False
    transfer_rate_measured: bool = False

    def effective_record_length(self, n_channels: int) -> float:
        if n_channels > self.interleave_channels and self.interleave_factor > 0:
            return self.record_length / self.interleave_factor
        return float(self.record_length)


@dataclass(frozen=True)
class StageConfig:
    """The stage and its controller.

    `counts_per_mm` has no default on purpose. pscan hardcodes 34304, which
    is the Z8xx / TDC001 DC-servo figure; an MLS203-family stage on a BBD303
    is very likely a different number, and a wrong value silently rescales
    every position in the map - the data look perfectly healthy and the
    microns are wrong. It has to be measured: command a known move, read the
    encoder, and divide.
    """

    counts_per_mm: float
    max_velocity_mm_s: float = 250.0
    max_acceleration_mm_s2: float = 1000.0
    # APT SET_VELPARAMS scaling. For the benchtop brushless controllers the
    # documented form is
    #   velocity counts     = v[mm/s]  * counts_per_mm * t * 2^bits
    #   acceleration counts = a[mm/s2] * counts_per_mm * t^2 * 2^bits
    # with t the controller's position-loop interval. Both the interval and
    # the exponent are UNVERIFIED for this controller: treat the numbers this
    # produces as a starting point for calibration, never as correct.
    apt_sample_interval_s: float = 102.4e-6
    apt_scaling_bits: int = 16
    scaling_verified: bool = False

    def __post_init__(self):
        if self.counts_per_mm <= 0:
            raise ValueError("counts_per_mm must be positive")

    def velocity_counts(self, velocity_mm_s: float) -> float:
        """The number APT SET_VELPARAMS wants for this velocity."""
        return (velocity_mm_s * self.counts_per_mm
                * self.apt_sample_interval_s * (2 ** self.apt_scaling_bits))

    def acceleration_counts(self, acceleration_mm_s2: float) -> float:
        """The number APT SET_VELPARAMS wants for this acceleration."""
        return (acceleration_mm_s2 * self.counts_per_mm
                * self.apt_sample_interval_s ** 2
                * (2 ** self.apt_scaling_bits))

    def counts(self, millimetres: float) -> float:
        return millimetres * self.counts_per_mm


@dataclass(frozen=True)
class AcquisitionConfig:
    """How the record is taken and which part of it comes back."""

    sample_rate_hz: float = 10.0e6
    channels: tuple = DEFAULT_CHANNELS
    # 'sequence': one segment per ASOPS sweep, triggered by the 10 kHz clock.
    # 'flat': one continuous record per line, segmented in software.
    mode: str = "sequence"
    points_per_cycle: float = DEFAULT_POINTS_PER_CYCLE
    # The Brillouin frequency that has to be resolved. ~5 GHz for this
    # sample; 0 disables the checks that depend on it.
    target_real_hz: float = 5.0e9

    # --- segment capture window ------------------------------------------
    # How much of the 12.5 ns delay window each segment keeps, in REAL delay.
    # 0 falls back to `segment_window_fraction` of the whole window.
    segment_window_real_s: float = 2.0e-9
    segment_window_fraction: float = 0.16
    # Delay captured before the coincidence peak. Worth having: it is the
    # only baseline a segment contains, and the trigger does not arrive at
    # the coincidence, so some margin protects against trigger drift.
    segment_pre_delay_s: float = 0.2e-9
    # Fewest acoustic cycles the window must contain to be worth fitting.
    min_cycles_in_window: float = 3.0

    # --- auxiliary channels ----------------------------------------------
    # BBD303 position-trigger programming. 0.6 um at 10 mm/s is 0.6 sweeps
    # per pulse - deliberately not a simple fraction of the sweep period, so
    # the pulses walk through the sweep phase instead of arriving at a couple
    # of fixed phases. See the STROBOSCOPIC TRIGGER warning.
    trigger_interval_um: float = 0.6
    trigger_pulse_width_s: float = 20.0e-6
    min_trigger_samples: float = 3.0
    # DC samples per segment to average into one scalar per copeak.
    min_dc_samples_per_segment: float = 4.0

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    def by_role(self, role):
        return tuple(c for c in self.channels if c.role == role)

    def window_real_s(self, timing: AsopsTiming) -> float:
        if self.segment_window_real_s > 0:
            return self.segment_window_real_s
        return self.segment_window_fraction * timing.delay_window_s


@dataclass(frozen=True)
class ScanConfig:
    """The geometry being scanned."""

    line_length_um: float = 500.0
    velocity_mm_s: float = 10.0
    pixel_pitch_um: float = 1.0
    n_lines: int = 1
    # Optical spot size (the diameter that matters for smear). The constraint
    # most likely to bite.
    spot_size_um: float = 1.0
    # Smear this fraction of the spot is accepted. 1.0 = smear equal to the
    # spot, the hard cap; 0.5 is the conservative working value.
    max_smear_fraction: float = 1.0


# ---------------------------------------------------------------------------
# Forward budget
# ---------------------------------------------------------------------------

@dataclass
class Budget:
    """Everything the forward calculation produces, in flat fields."""

    # Echo of the inputs, so a Budget is self-describing in a log or a dump.
    mode: str
    rep_rate_hz: float
    sweep_rate_hz: float
    sample_rate_hz: float
    line_length_um: float
    velocity_mm_s: float
    pixel_pitch_um: float
    spot_size_um: float
    max_smear_fraction: float
    n_lines: int
    n_channels: int
    channel_summary: str
    bytes_per_sample: int
    record_length: float
    record_length_nominal: int
    transfer_rate_mb_s: float
    points_per_cycle: float

    # Timing
    stretch_factor: float
    delay_window_s: float
    intrinsic_delay_resolution_s: float
    delay_resolution_s: float
    samples_per_sweep: float
    max_real_hz: float
    nyquist_real_hz: float
    target_real_hz: float
    points_per_cycle_at_target: float

    # Segment capture window
    segment_window_real_s: float
    segment_window_lab_s: float
    segment_window_fraction: float
    points_per_segment: float
    cycles_in_window: float
    segment_duty: float

    # Smear and space
    smear_per_sweep_um: float
    smear_fraction_of_spot: float
    smear_ok: bool
    velocity_for_full_spot_mm_s: float
    velocity_for_half_spot_mm_s: float
    sweeps_per_pixel: float
    pixel_pitch_floor_um: float
    pixels_per_line: float
    line_duration_s: float
    sweeps_per_line: float
    n_segments: float
    segments_ok: bool

    # Memory
    points_per_line_per_channel: float
    flat_points_per_line_per_channel: float
    reduction_vs_flat: float
    mb_per_line_on_scope: float
    mb_per_line_transferred: float
    mb_map_transferred: float
    fits_record_length: bool
    headroom_points: float
    headroom_fraction: float
    max_line_length_um: float

    # Transfer
    download_s_per_line: float
    duty_cycle: float
    dominant: str
    transfer_to_flight_ratio: float
    map_wall_time_s: float

    # Auxiliary channels
    trigger_interval_um: float
    trigger_pulse_width_s: float
    trigger_pulse_interval_s: float
    trigger_samples_per_pulse: float
    trigger_pulses_per_segment: float
    sweeps_per_trigger_pulse: float
    dc_samples_per_segment: float

    # Stage / APT
    counts_per_mm: float
    apt_velocity_counts: float
    apt_acceleration_counts: float
    apt_acceleration_mm_s2: float
    apt_scaling_verified: bool
    counts_per_pixel: float

    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def report(self, include_assumptions: bool = True) -> str:
        return format_budget(self, include_assumptions=include_assumptions)


def velocity_for_smear(timing: AsopsTiming, spot_size_um: float,
                       smear_fraction: float) -> float:
    """Velocity in mm/s whose per-sweep smear is `smear_fraction` of the spot."""
    return smear_fraction * spot_size_um / timing.sweep_period_s / 1.0e3


def _channel_summary(channels) -> str:
    return ", ".join(
        f"{c.name}:{c.role}" + (f"/{c.sparsing}" if c.sparsing > 1 else "")
        for c in channels)


def compute_budget(
    timing: AsopsTiming = AsopsTiming(),
    scan: ScanConfig = ScanConfig(),
    acq: AcquisitionConfig = AcquisitionConfig(),
    scope: ScopeConfig = ScopeConfig(),
    stage: StageConfig = None,
) -> Budget:
    """Forward budget: what the configured scan costs and whether it fits.

    `stage` carries counts_per_mm and so the APT numbers. It has no default
    because counts_per_mm has none; leave it out and the APT section is
    reported as unavailable rather than guessed.
    """
    if scan.velocity_mm_s <= 0:
        raise ValueError("velocity_mm_s must be positive")
    if scan.line_length_um <= 0:
        raise ValueError("line_length_um must be positive")
    if scan.pixel_pitch_um <= 0:
        raise ValueError("pixel_pitch_um must be positive")
    if scan.spot_size_um <= 0:
        raise ValueError("spot_size_um must be positive")
    if acq.sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if acq.mode not in ("sequence", "flat"):
        raise ValueError("mode must be 'sequence' or 'flat'")

    warnings = []
    fs = acq.sample_rate_hz
    velocity_um_s = scan.velocity_mm_s * 1.0e3

    samples_per_sweep = timing.samples_per_sweep(fs)
    smear_um = timing.travel_per_sweep_um(scan.velocity_mm_s)
    smear_fraction = smear_um / scan.spot_size_um
    sweeps_per_pixel = scan.pixel_pitch_um / smear_um

    line_duration_s = scan.line_length_um / velocity_um_s
    sweeps_per_line = line_duration_s * timing.sweep_rate_hz

    window_real = acq.window_real_s(timing)
    window_lab = timing.lab_time(window_real)
    # The scope captures a whole number of samples in a whole number of
    # segments, so the data volume is quantised even though the window and
    # the line length are not.
    points_per_segment = float(round(window_lab * fs))
    n_segments = float(math.floor(sweeps_per_line))

    flat_points = line_duration_s * fs
    if acq.mode == "sequence":
        points_per_line = n_segments * points_per_segment
    else:
        points_per_line = flat_points
    reduction = flat_points / points_per_line if points_per_line > 0 else 1.0

    # Scope memory is consumed on every enabled channel; sparsing is a
    # transfer-side setting and buys no memory.
    n_channels = acq.n_channels
    record_length = scope.effective_record_length(n_channels)
    bytes_on_scope = points_per_line * n_channels * scope.bytes_per_sample
    transferred_points = sum(
        math.ceil(points_per_line / c.sparsing) for c in acq.channels)
    bytes_transferred = transferred_points * scope.bytes_per_sample

    fits = points_per_line <= record_length
    headroom_points = record_length - points_per_line
    headroom_fraction = headroom_points / record_length
    # Longest line that still fits, at this velocity, rate and window.
    if acq.mode == "sequence":
        per_um = (timing.sweep_rate_hz * points_per_segment) / velocity_um_s
    else:
        per_um = fs / velocity_um_s
    max_line_length_um = record_length / per_um if per_um > 0 else math.inf

    download_s = bytes_transferred / BYTES_PER_MB / scope.transfer_rate_mb_s
    duty_cycle = line_duration_s / (line_duration_s + download_s)
    dominant = "flight" if line_duration_s >= download_s else "transfer"
    ratio = download_s / line_duration_s if line_duration_s > 0 else math.inf

    # Auxiliary channels.
    trigger_channels = acq.by_role(ROLE_TRIGGER)
    trigger_sparsing = trigger_channels[0].sparsing if trigger_channels else 1
    pulse_interval_s = (acq.trigger_interval_um / velocity_um_s
                        if acq.trigger_interval_um > 0 else float("nan"))
    trigger_samples_per_pulse = (
        acq.trigger_pulse_width_s * fs / trigger_sparsing
        if trigger_channels else float("nan"))
    # In Sequence mode a pulse is only seen if it overlaps a capture window.
    pulses_per_segment = (
        window_lab / pulse_interval_s
        if trigger_channels and math.isfinite(pulse_interval_s)
        else float("nan"))
    sweeps_per_pulse = (
        pulse_interval_s / timing.sweep_period_s
        if math.isfinite(pulse_interval_s) else float("nan"))

    dc_channels = acq.by_role(ROLE_DC)
    if dc_channels:
        per_segment = (points_per_segment if acq.mode == "sequence"
                       else samples_per_sweep)
        dc_samples_per_segment = per_segment / dc_channels[0].sparsing
    else:
        dc_samples_per_segment = float("nan")

    target_ppc = float("nan")
    cycles = float("nan")
    if acq.target_real_hz > 0:
        target_ppc = fs / timing.lab_frequency(acq.target_real_hz)
        cycles = window_real * acq.target_real_hz

    if stage is not None:
        accel = stage.max_acceleration_mm_s2
        counts_per_mm = stage.counts_per_mm
        apt_vel = stage.velocity_counts(scan.velocity_mm_s)
        apt_acc = stage.acceleration_counts(accel)
        verified = stage.scaling_verified
        counts_per_pixel = stage.counts(scan.pixel_pitch_um * 1e-3)
    else:
        accel = float("nan")
        counts_per_mm = float("nan")
        apt_vel = float("nan")
        apt_acc = float("nan")
        verified = False
        counts_per_pixel = float("nan")

    budget = Budget(
        mode=acq.mode,
        rep_rate_hz=timing.rep_rate_hz,
        sweep_rate_hz=timing.sweep_rate_hz,
        sample_rate_hz=fs,
        line_length_um=scan.line_length_um,
        velocity_mm_s=scan.velocity_mm_s,
        pixel_pitch_um=scan.pixel_pitch_um,
        spot_size_um=scan.spot_size_um,
        max_smear_fraction=scan.max_smear_fraction,
        n_lines=scan.n_lines,
        n_channels=n_channels,
        channel_summary=_channel_summary(acq.channels),
        bytes_per_sample=scope.bytes_per_sample,
        record_length=record_length,
        record_length_nominal=scope.record_length,
        transfer_rate_mb_s=scope.transfer_rate_mb_s,
        points_per_cycle=acq.points_per_cycle,

        stretch_factor=timing.stretch_factor,
        delay_window_s=timing.delay_window_s,
        intrinsic_delay_resolution_s=timing.intrinsic_delay_resolution_s,
        delay_resolution_s=timing.delay_resolution_s(fs),
        samples_per_sweep=samples_per_sweep,
        max_real_hz=timing.max_real_frequency(fs, acq.points_per_cycle),
        nyquist_real_hz=timing.max_real_frequency(fs, 2.0),
        target_real_hz=acq.target_real_hz,
        points_per_cycle_at_target=target_ppc,

        segment_window_real_s=window_real,
        segment_window_lab_s=window_lab,
        segment_window_fraction=window_real / timing.delay_window_s,
        points_per_segment=points_per_segment,
        cycles_in_window=cycles,
        segment_duty=window_lab / timing.sweep_period_s,

        smear_per_sweep_um=smear_um,
        smear_fraction_of_spot=smear_fraction,
        smear_ok=smear_fraction <= scan.max_smear_fraction,
        velocity_for_full_spot_mm_s=velocity_for_smear(
            timing, scan.spot_size_um, 1.0),
        velocity_for_half_spot_mm_s=velocity_for_smear(
            timing, scan.spot_size_um, 0.5),
        sweeps_per_pixel=sweeps_per_pixel,
        pixel_pitch_floor_um=smear_um,
        pixels_per_line=scan.line_length_um / scan.pixel_pitch_um,
        line_duration_s=line_duration_s,
        sweeps_per_line=sweeps_per_line,
        n_segments=n_segments if acq.mode == "sequence" else 1.0,
        segments_ok=(acq.mode != "sequence"
                     or n_segments <= scope.max_segments),

        points_per_line_per_channel=points_per_line,
        flat_points_per_line_per_channel=flat_points,
        reduction_vs_flat=reduction,
        mb_per_line_on_scope=bytes_on_scope / BYTES_PER_MB,
        mb_per_line_transferred=bytes_transferred / BYTES_PER_MB,
        mb_map_transferred=bytes_transferred * scan.n_lines / BYTES_PER_MB,
        fits_record_length=fits,
        headroom_points=headroom_points,
        headroom_fraction=headroom_fraction,
        max_line_length_um=max_line_length_um,

        download_s_per_line=download_s,
        duty_cycle=duty_cycle,
        dominant=dominant,
        transfer_to_flight_ratio=ratio,
        map_wall_time_s=(line_duration_s + download_s) * scan.n_lines,

        trigger_interval_um=acq.trigger_interval_um,
        trigger_pulse_width_s=acq.trigger_pulse_width_s,
        trigger_pulse_interval_s=pulse_interval_s,
        trigger_samples_per_pulse=trigger_samples_per_pulse,
        trigger_pulses_per_segment=pulses_per_segment,
        sweeps_per_trigger_pulse=sweeps_per_pulse,
        dc_samples_per_segment=dc_samples_per_segment,

        counts_per_mm=counts_per_mm,
        apt_velocity_counts=apt_vel,
        apt_acceleration_counts=apt_acc,
        apt_acceleration_mm_s2=accel,
        apt_scaling_verified=verified,
        counts_per_pixel=counts_per_pixel,
        warnings=warnings,
    )

    _add_warnings(budget, timing, scan, acq, scope, stage)
    return budget


def _add_warnings(budget, timing, scan, acq, scope, stage):
    """Attach the conclusions that need stating in words, not numbers."""
    w = budget.warnings

    if not budget.smear_ok:
        w.append(
            f"SMEAR: one sweep covers {budget.smear_per_sweep_um:.4g} um, "
            f"{budget.smear_fraction_of_spot:.2f} of the "
            f"{budget.spot_size_um:.4g} um spot - above the "
            f"{budget.max_smear_fraction:.2f} allowed. Every delay trace is "
            f"an average over that much sample. Drop to "
            f"{velocity_for_smear(timing, budget.spot_size_um, budget.max_smear_fraction):.4g}"
            f" mm/s, or accept the smear as the real spatial resolution."
        )

    if not budget.fits_record_length:
        w.append(
            f"DOES NOT FIT: needs "
            f"{budget.points_per_line_per_channel:,.0f} points/channel but "
            f"the record length is {budget.record_length:,.0f} - short by "
            f"{-budget.headroom_points:,.0f} points "
            f"({-budget.headroom_fraction * 100:.1f}%). Longest line that "
            f"fits here is {budget.max_line_length_um:,.1f} um."
        )

    if not budget.segments_ok:
        w.append(
            f"TOO MANY SEGMENTS: {budget.n_segments:,.0f} sweeps in this line "
            f"but the scope is configured for at most {scope.max_segments:,d} "
            f"segments. Split the line, or confirm the real limit with *OPT? "
            f"and SEQ? - the segment ceiling depends on installed options."
        )

    if acq.mode == "sequence" and scope.segment_rearm_s > 0:
        headroom = timing.sweep_period_s - scope.segment_rearm_s
        if headroom <= 0:
            w.append(
                f"SEGMENT RE-ARM: the assumed {scope.segment_rearm_s * 1e6:.3g}"
                f" us re-arm time is not shorter than the "
                f"{timing.sweep_period_s * 1e6:.3g} us between ASOPS "
                f"triggers, so segments will be missed. Measure it before "
                f"trusting any of this."
            )

    if budget.sample_rate_hz > timing.rep_rate_hz:
        w.append(
            f"Sample rate {budget.sample_rate_hz / 1e6:.3g} MS/s is above the "
            f"laser rep rate {timing.rep_rate_hz / 1e6:.3g} MHz, which is the "
            f"information-theoretic ceiling: one sample per probe pulse. "
            f"Extra samples interpolate the detector response to a single "
            f"pulse and add no delay points, so delay resolution is pinned at "
            f"the intrinsic "
            f"{budget.intrinsic_delay_resolution_s * 1e12:.4g} ps."
        )

    if budget.sample_rate_hz > scope.max_sample_rate_hz:
        w.append(f"Sample rate exceeds the scope maximum "
                 f"{scope.max_sample_rate_hz / 1e9:.3g} GS/s.")

    if (budget.n_channels > scope.interleave_channels
            and scope.interleave_factor > 1):
        w.append(
            f"{budget.n_channels} channels enabled, so per-channel memory is "
            f"modelled as {budget.record_length_nominal:,d} / "
            f"{scope.interleave_factor:g} = {budget.record_length:,.0f} "
            f"points. ASSUMED: confirm with MSIZ? while all of them are on, "
            f"and check whether the maximum sample rate also drops."
        )

    if budget.sweeps_per_pixel < 1.0:
        w.append(
            f"Pixel pitch {budget.pixel_pitch_um:.4g} um is finer than the "
            f"{budget.smear_per_sweep_um:.4g} um the stage covers in one "
            f"sweep, so {budget.sweeps_per_pixel:.3g} sweeps land per pixel: "
            f"pixels will be empty or interpolated, not averaged."
        )

    if acq.target_real_hz > 0:
        needed = timing.required_sample_rate(
            acq.target_real_hz, acq.points_per_cycle)
        if needed > budget.sample_rate_hz * 1.000001:
            w.append(
                f"Target {acq.target_real_hz / 1e9:.4g} GHz needs "
                f"{needed / 1e6:.4g} MS/s at {acq.points_per_cycle:g} "
                f"points/cycle; configured rate carries only "
                f"{budget.max_real_hz / 1e9:.4g} GHz."
            )
        if budget.cycles_in_window < acq.min_cycles_in_window:
            w.append(
                f"CAPTURE WINDOW TOO SHORT: "
                f"{budget.segment_window_real_s * 1e9:.3g} ns holds only "
                f"{budget.cycles_in_window:.2f} cycles at "
                f"{acq.target_real_hz / 1e9:.4g} GHz, below the "
                f"{acq.min_cycles_in_window:g} wanted. Frequency resolution "
                f"from a window that short is about "
                f"{1.0 / budget.segment_window_real_s / 1e9:.2f} GHz, which "
                f"is the whole quantity of interest."
            )

    if budget.points_per_segment < 32:
        w.append(
            f"Only {budget.points_per_segment:.0f} points per segment. Below "
            f"a few tens the copeak cannot be located within the segment, so "
            f"the trigger-drift check in the post-processing stops working."
        )

    trigger_plan = acq.by_role(ROLE_TRIGGER)
    if trigger_plan:
        sparsing = trigger_plan[0].sparsing
        needed_width_s = (
            acq.min_trigger_samples * sparsing / budget.sample_rate_hz)
        if budget.trigger_samples_per_pulse < acq.min_trigger_samples:
            w.append(
                f"TRIGGER PULSE TOO NARROW FOR SPARSING: a "
                f"{budget.trigger_pulse_width_s * 1e6:.3g} us pulse lands on "
                f"{budget.trigger_samples_per_pulse:.2f} samples of the "
                f"trigger channel sparsed 1:{sparsing}. Below "
                f"{acq.min_trigger_samples:g} samples pulses are missed "
                f"outright, and a missed pulse is a lost position reference. "
                f"Widen the BBD303 pulse to at least "
                f"{needed_width_s * 1e6:.3g} us, or sparse that channel less."
            )
        if (math.isfinite(budget.trigger_pulse_interval_s)
                and budget.trigger_pulse_width_s
                >= budget.trigger_pulse_interval_s):
            w.append(
                f"TRIGGER PULSES OVERLAP: {budget.trigger_interval_um:.4g} um "
                f"at {budget.velocity_mm_s:.4g} mm/s is one pulse every "
                f"{budget.trigger_pulse_interval_s * 1e6:.3g} us, no longer "
                f"than the {budget.trigger_pulse_width_s * 1e6:.3g} us pulse "
                f"width. The train becomes a continuous level and carries no "
                f"position."
            )
        if (acq.mode == "sequence"
                and math.isfinite(budget.sweeps_per_trigger_pulse)):
            # A pulse interval that is a whole number of sweeps' worth of
            # travel puts every pulse at the same phase of the sweep. If that
            # phase is outside the capture window, no pulse is ever digitised
            # however many the controller emits - the train is
            # stroboscopically invisible.
            sweeps = budget.sweeps_per_trigger_pulse
            commensurate = None
            for q in (1, 2, 3, 4):
                if abs(sweeps * q - round(sweeps * q)) < 0.05 * q:
                    commensurate = q
                    break
            if commensurate:
                w.append(
                    f"STROBOSCOPIC TRIGGER: {budget.trigger_interval_um:.4g} "
                    f"um at {budget.velocity_mm_s:.4g} mm/s is "
                    f"{sweeps:.3f} sweeps per pulse, which is within 5% of a "
                    f"multiple of 1/{commensurate}. The pulses then arrive at "
                    f"only {commensurate} distinct phases of the sweep. If "
                    f"none of those phases falls inside the "
                    f"{budget.segment_window_lab_s * 1e6:.3g} us capture "
                    f"window, not one pulse is ever digitised however many "
                    f"the controller emits; if one does, the pulses that are "
                    f"seen are selected by stage position ripple rather than "
                    f"sampling it, and the ripple becomes invisible. Choose "
                    f"an interval whose travel time is not a simple fraction "
                    f"of {timing.sweep_period_s * 1e6:.3g} us - "
                    f"{budget.trigger_interval_um * 1.3:.4g} um would give "
                    f"{sweeps * 1.3:.3f} sweeps per pulse.")

        if acq.mode == "sequence":
            w.append(
                f"POSITION IN SEQUENCE MODE: each segment only looks at the "
                f"trigger channel for {budget.segment_window_lab_s * 1e6:.3g} "
                f"us out of every {timing.sweep_period_s * 1e6:.3g} us "
                f"({budget.segment_duty * 100:.1f}% duty), so most pulse "
                f"edges fall in the dead time and are never digitised. The "
                f"ones that do land in a window are timed to that channel's "
                f"sparsed sample interval, so each is an exact position - "
                f"what is lost is how often they arrive, roughly "
                f"{budget.segment_duty / budget.trigger_pulse_interval_s:,.0f}"
                f" per second here. Position between anchors is interpolated, "
                f"so stage ripple faster than about "
                f"{0.5 * budget.segment_duty / budget.trigger_pulse_interval_s:,.0f}"
                f" Hz is not resolved; the post-processing reports the anchor "
                f"rate it actually achieved."
            )

    if acq.by_role(ROLE_DC):
        if budget.dc_samples_per_segment < acq.min_dc_samples_per_segment:
            per_segment = (budget.points_per_segment if acq.mode == "sequence"
                           else budget.samples_per_sweep)
            w.append(
                f"DC channel gives {budget.dc_samples_per_segment:.2f} "
                f"samples per segment, below the "
                f"{acq.min_dc_samples_per_segment:g} wanted. One scalar DC "
                f"per copeak is formed by averaging that segment's DC slice, "
                f"so too few samples makes the denominator noisy. Sparse the "
                f"DC channel at about "
                f"{per_segment / acq.min_dc_samples_per_segment:,.0f} or less."
            )
    else:
        w.append(
            "No DC channel in the plan, so dR/R cannot be formed; only raw "
            "AC amplitudes will be available.")

    if budget.sweeps_per_line < 10:
        w.append(
            f"Only {budget.sweeps_per_line:.3g} sweeps in the whole line - "
            f"TRIGTIME spacing statistics will be weak.")

    if stage is None:
        w.append(
            "No StageConfig supplied, so counts_per_mm is unknown and the APT "
            "velocity and acceleration numbers are not reported. "
            "counts_per_mm has no default on purpose: pscan's 34304 is the "
            "Z8xx/TDC001 figure and is very unlikely to be right for an "
            "MLS203 on a BBD303. A wrong value silently rescales every "
            "position in the map.")
    elif not stage.scaling_verified:
        w.append(
            f"APT SET_VELPARAMS numbers below use an UNVERIFIED scaling: "
            f"t = {stage.apt_sample_interval_s * 1e6:.4g} us and 2^"
            f"{stage.apt_scaling_bits} with counts_per_mm = "
            f"{stage.counts_per_mm:,.1f}. Calibrate before trusting them: "
            f"command a slow move, time a known distance, and compare. Both "
            f"the loop interval and counts_per_mm are controller- and "
            f"stage-specific.")


def assumption_notes(scope: ScopeConfig, acq: AcquisitionConfig,
                     stage: StageConfig = None) -> list:
    """The numbers in a report that are assumptions, not derived facts."""
    notes = [
        f"ASSUMED transfer rate {scope.transfer_rate_mb_s:g} MB/s"
        + ("" if scope.transfer_rate_measured else " - NOT measured")
        + ". Every download time, duty cycle and flight-vs-transfer verdict "
        "scales directly with it. Measure it once with a long waveform.",
        f"ASSUMED record length {scope.record_length:,d} points/channel at "
        f"<= {scope.interleave_channels} channels"
        + ("" if scope.record_length_confirmed else " - NOT confirmed")
        + f", divided by {scope.interleave_factor:g} above that. Query MSIZ? "
        "(and *OPT? for memory options) with the intended channels enabled. "
        "In Sequence mode with a narrow window there is large headroom, so an "
        "error here is unlikely to change a decision.",
        f"ASSUMED at most {scope.max_segments:,d} segments per Sequence "
        f"acquisition and {scope.segment_rearm_s * 1e6:g} us of re-arm time "
        f"between them. The segment ceiling depends on installed options; the "
        f"re-arm time must be below the 100 us between ASOPS triggers or "
        f"segments are missed. Confirm both with SEQ? and a test acquisition.",
        f"ASSUMED {scope.bytes_per_sample} bytes/sample, i.e. a WORD-format "
        "transfer of the 12-bit ADC. A BYTE transfer halves every size here "
        "and throws away 4 bits.",
        f"CRITERION {acq.points_per_cycle:g} digitiser points per cycle for "
        "'adequately sampled'. Nyquist (2 points/cycle) is also reported and "
        "is 5x more optimistic; it is not enough to fit an amplitude and a "
        "decay constant.",
        "ASSUMED the capture window is positioned correctly on the "
        "coincidence. Where the copeak sits inside the 12.5 ns delay window "
        "is not known until a whole sweep has been captured once, flat - "
        "which is what mode='flat' is for. The post-processing checks the "
        "copeak's position in every segment and reports drift.",
        "ASSUMED the optical spot size given is the diameter over which "
        "smear matters. Smear and spot are compared as plain lengths, with no "
        "convolution model: a sweep covering a tenth of a spot is negligible, "
        "one covering a whole spot halves the resolution, and between those "
        "the honest statement is the ratio itself.",
    ]
    if stage is None:
        notes.append(
            "counts_per_mm NOT SUPPLIED, so no APT numbers were produced. It "
            "has no default by design: pscan's 34304 is the Z8xx/TDC001 "
            "figure, an MLS203 on a BBD303 is very likely different, and a "
            "wrong value silently rescales every position.")
    else:
        notes.append(
            f"counts_per_mm = {stage.counts_per_mm:,.1f} as supplied"
            + ("" if stage.scaling_verified else " - NOT verified")
            + f", and the APT velocity/acceleration scaling (t = "
            f"{stage.apt_sample_interval_s * 1e6:.4g} us, 2^"
            f"{stage.apt_scaling_bits}) is the documented form for the "
            f"benchtop brushless controllers but is UNVERIFIED for this one. "
            f"Velocity must be set programmatically over APT - there is no "
            f"Kinesis on this system - so these numbers go into "
            f"SET_VELPARAMS and a calibration error moves the stage at the "
            f"wrong speed, which changes the smear.")
    return notes


# ---------------------------------------------------------------------------
# Inversion: choose parameters rather than check them
# ---------------------------------------------------------------------------

_LADDER_MANTISSAS = (1.0, 2.0, 5.0)


def snap_sample_rate_down(rate_hz: float) -> float:
    """Largest 1-2-5 ladder rate not exceeding `rate_hz`."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be positive")
    decade = math.floor(math.log10(rate_hz))
    best = None
    for d in (decade - 1, decade, decade + 1):
        for m in _LADDER_MANTISSAS:
            candidate = m * 10.0 ** d
            if candidate <= rate_hz * (1.0 + 1e-12):
                if best is None or candidate > best:
                    best = candidate
    if best is None:
        raise ValueError(f"no ladder rate at or below {rate_hz:g} S/s")
    return best


def snap_sample_rate_up(rate_hz: float) -> float:
    """Smallest 1-2-5 ladder rate at or above `rate_hz`."""
    if rate_hz <= 0:
        raise ValueError("rate_hz must be positive")
    decade = math.floor(math.log10(rate_hz))
    best = None
    for d in (decade - 1, decade, decade + 1, decade + 2):
        for m in _LADDER_MANTISSAS:
            candidate = m * 10.0 ** d
            if candidate >= rate_hz * (1.0 - 1e-12):
                if best is None or candidate < best:
                    best = candidate
    if best is None:
        raise ValueError(f"no ladder rate at or above {rate_hz:g} S/s")
    return best


def next_ladder_rate_up(rate_hz: float) -> float:
    """The ladder step strictly above `rate_hz`. Used for tightness checks."""
    return snap_sample_rate_up(rate_hz * 1.0000001 + 1.0)


@dataclass(frozen=True)
class SolveRequest:
    """What the user wants; the solver finds velocity and sample rate."""

    line_length_um: float = 500.0
    pixel_pitch_um: float = 1.0
    n_lines: int = 1
    # Smear constraint - the one that binds on this hardware.
    spot_size_um: float = 1.0
    max_smear_fraction: float = 1.0
    # At least this many ASOPS sweeps must land in each pixel.
    min_sweeps_per_pixel: float = 1.0
    # Stage ceiling. MLS203 on a BBD303 is about 250 mm/s; 0 means do not cap.
    max_velocity_mm_s: float = 250.0
    # Brillouin frequency that must survive; 0 means take all the bandwidth
    # the budget allows.
    target_real_hz: float = 5.0e9
    points_per_cycle: float = DEFAULT_POINTS_PER_CYCLE
    # Budgets. 0 means unconstrained; the record length always applies.
    max_mb_per_line: float = 0.0
    max_download_s_per_line: float = 0.0
    snap_to_ladder: bool = True


@dataclass
class Solution:
    velocity_mm_s: float
    velocity_limited_by: str
    velocity_limits: dict
    sample_rate_hz: float
    sample_rate_ceiling_hz: float
    sample_rate_limited_by: str
    required_sample_rate_hz: float
    target_feasible: bool
    budget: Budget
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "velocity_mm_s": self.velocity_mm_s,
            "velocity_limited_by": self.velocity_limited_by,
            "velocity_limits_mm_s": dict(self.velocity_limits),
            "sample_rate_hz": self.sample_rate_hz,
            "sample_rate_ceiling_hz": self.sample_rate_ceiling_hz,
            "sample_rate_limited_by": self.sample_rate_limited_by,
            "required_sample_rate_hz": self.required_sample_rate_hz,
            "target_feasible": self.target_feasible,
            "notes": list(self.notes),
            "budget": self.budget.to_dict(),
        }

    def report(self, include_assumptions: bool = True) -> str:
        return format_solution(self, include_assumptions=include_assumptions)


def solve_for_pitch(
    request: SolveRequest = SolveRequest(),
    timing: AsopsTiming = AsopsTiming(),
    acq: AcquisitionConfig = AcquisitionConfig(),
    scope: ScopeConfig = ScopeConfig(),
    stage: StageConfig = None,
) -> Solution:
    """Pick a velocity and sample rate that meet the constraints.

    The chain, in order, because each step feeds the next:

      1. Three things cap the velocity: smear against the optical spot, the
         averaging requirement (sweeps per pixel), and the stage itself. The
         smallest wins, and the solver says which.
      2. Velocity fixes the line duration and so the number of segments.
      3. Segment count times points per segment turns every memory or time
         budget into a sample-rate ceiling. The rep rate caps it as well:
         one sample per probe pulse is all the information there is.
      4. If a target Brillouin frequency was given, the points-per-cycle
         criterion sets a sample-rate floor. If the floor is under the
         ceiling the target is feasible and the solver takes the cheapest
         ladder rate that meets it; otherwise it reports the shortfall and
         the ways out.

    `acq` supplies the channel plan, the capture window and the trigger
    programming; its sample_rate_hz is ignored, since that is what is being
    solved for.
    """
    notes = []
    sweep_period = timing.sweep_period_s

    if request.min_sweeps_per_pixel <= 0:
        raise ValueError("min_sweeps_per_pixel must be positive")
    if request.spot_size_um <= 0:
        raise ValueError("spot_size_um must be positive")

    # 1. Velocity caps.
    limits = {
        "smear_vs_spot": velocity_for_smear(
            timing, request.spot_size_um, request.max_smear_fraction),
        "sweeps_per_pixel": (
            request.pixel_pitch_um
            / (request.min_sweeps_per_pixel * sweep_period) / 1.0e3),
    }
    if request.max_velocity_mm_s > 0:
        limits["stage_max"] = float(request.max_velocity_mm_s)

    limited_by = min(limits, key=limits.get)
    velocity = limits[limited_by]
    notes.append(
        "Velocity caps: "
        + ", ".join(f"{k} {v:.4g} mm/s"
                    for k, v in sorted(limits.items(), key=lambda kv: kv[1]))
        + f" -> {velocity:.4g} mm/s, set by {limited_by}.")
    if limited_by == "smear_vs_spot" and "stage_max" in limits:
        notes.append(
            f"Smear is the binding constraint, as expected on this stage: one "
            f"{sweep_period * 1e6:.0f} us sweep must not cover more than "
            f"{request.max_smear_fraction:.2f} of the "
            f"{request.spot_size_um:.4g} um spot. The stage could do "
            f"{limits['stage_max']:.4g} mm/s, so it runs at "
            f"{100 * velocity / limits['stage_max']:.1f}% of capability - "
            f"comfortable for velocity stability, and far from where a servo "
            f"misbehaves.")

    # 2. Line duration and segment count.
    velocity_um_s = velocity * 1.0e3
    line_duration_s = request.line_length_um / velocity_um_s
    n_segments = line_duration_s * timing.sweep_rate_hz

    # 3. Sample-rate ceilings. Points per segment scales with the rate, so a
    # memory budget divided by (segments * window_lab) is a rate ceiling.
    window_lab = timing.lab_time(acq.window_real_s(timing))
    record_length = scope.effective_record_length(acq.n_channels)
    if acq.mode == "sequence":
        points_per_rate = n_segments * window_lab        # points per S/s
    else:
        points_per_rate = line_duration_s
    ceilings = [
        ("record_length", record_length / points_per_rate),
        ("scope_max_rate", scope.max_sample_rate_hz),
        ("laser_rep_rate", timing.rep_rate_hz),
    ]

    channel_weight = sum(1.0 / c.sparsing for c in acq.channels)
    bytes_per_point = channel_weight * scope.bytes_per_sample
    if request.max_mb_per_line > 0:
        ceilings.append((
            "mb_per_line",
            request.max_mb_per_line * BYTES_PER_MB
            / (bytes_per_point * points_per_rate)))
    if request.max_download_s_per_line > 0:
        allowed_mb = request.max_download_s_per_line * scope.transfer_rate_mb_s
        ceilings.append((
            "download_time",
            allowed_mb * BYTES_PER_MB / (bytes_per_point * points_per_rate)))

    sample_rate_limited_by, ceiling = min(ceilings, key=lambda kv: kv[1])
    if ceiling <= 0:
        raise ValueError("budgets leave no sample rate at all")

    # 4. Floor from the target frequency.
    required = 0.0
    feasible = True
    if request.target_real_hz > 0:
        required = timing.required_sample_rate(
            request.target_real_hz, request.points_per_cycle)
        feasible = required <= ceiling

    if request.target_real_hz > 0 and feasible:
        chosen = (snap_sample_rate_up(required)
                  if request.snap_to_ladder else required)
        if chosen > ceiling:
            chosen = (snap_sample_rate_down(ceiling)
                      if request.snap_to_ladder else ceiling)
            notes.append(
                f"The ladder step above the {required / 1e6:.4g} MS/s "
                f"requirement overshoots the ceiling {ceiling / 1e6:.4g} "
                f"MS/s, so the ceiling was taken instead; the target is met "
                f"only off-ladder.")
            feasible = chosen >= required
        else:
            notes.append(
                f"Target {request.target_real_hz / 1e9:.4g} GHz needs "
                f"{required / 1e6:.4g} MS/s at "
                f"{request.points_per_cycle:g} points/cycle; taking the "
                f"cheapest ladder rate that covers it keeps the record short "
                f"(a line of up to "
                f"{record_length / (timing.sweep_rate_hz * window_lab * chosen / velocity_um_s):,.0f}"
                f" um would still fit).")
    else:
        chosen = (snap_sample_rate_down(ceiling)
                  if request.snap_to_ladder else ceiling)
        if request.target_real_hz > 0:
            notes.append(
                f"TARGET NOT MET: {request.target_real_hz / 1e9:.4g} GHz "
                f"needs {required / 1e6:.4g} MS/s but {ceiling / 1e6:.4g} "
                f"MS/s is all the {sample_rate_limited_by} budget allows.")
            notes.extend(_fix_suggestions(
                request, timing, acq, scope, velocity, required,
                points_per_rate, record_length, window_lab))

    # Recompute the budget forward with the chosen pair. Nothing is reported
    # from the solver's own intermediate numbers, so the solution and the
    # forward calculation cannot disagree.
    from dataclasses import replace
    solved_acq = replace(acq, sample_rate_hz=chosen,
                         points_per_cycle=request.points_per_cycle,
                         target_real_hz=request.target_real_hz)
    scan = ScanConfig(
        line_length_um=request.line_length_um, velocity_mm_s=velocity,
        pixel_pitch_um=request.pixel_pitch_um, n_lines=request.n_lines,
        spot_size_um=request.spot_size_um,
        max_smear_fraction=request.max_smear_fraction)
    budget = compute_budget(timing, scan, solved_acq, scope, stage)

    if request.snap_to_ladder:
        step_up = next_ladder_rate_up(chosen)
        if step_up <= ceiling:
            notes.append(
                f"Headroom left on the ladder: {step_up / 1e6:.4g} MS/s would "
                f"also fit the {sample_rate_limited_by} budget.")

    return Solution(
        velocity_mm_s=velocity, velocity_limited_by=limited_by,
        velocity_limits=limits, sample_rate_hz=chosen,
        sample_rate_ceiling_hz=ceiling,
        sample_rate_limited_by=sample_rate_limited_by,
        required_sample_rate_hz=required, target_feasible=feasible,
        budget=budget, notes=notes)


def _fix_suggestions(request, timing, acq, scope, velocity, required,
                     points_per_rate, record_length, window_lab):
    """Concrete ways out when the target frequency does not fit."""
    out = []
    velocity_um_s = velocity * 1.0e3
    per_um = timing.sweep_rate_hz * window_lab * required / velocity_um_s
    out.append(
        f"  fix: shorten the line to {record_length / per_um:,.1f} um (from "
        f"{request.line_length_um:,.1f} um) and the target fits in the "
        f"present record length.")
    out.append(
        f"  fix: a record length of {required * points_per_rate:,.0f} "
        f"points/channel (have {record_length:,.0f}) holds the whole line at "
        f"{required / 1e6:.4g} MS/s.")
    out.append(
        f"  fix: narrow the segment capture window. It is "
        f"{acq.window_real_s(timing) * 1e9:.3g} ns of delay now; every "
        f"halving halves the data. The cost is frequency resolution, "
        f"currently {1.0 / acq.window_real_s(timing) / 1e9:.2f} GHz.")
    relaxed_ppc = (request.points_per_cycle
                   * (record_length / points_per_rate) / required)
    out.append(
        f"  fix: accept {relaxed_ppc:.2f} points/cycle instead of "
        f"{request.points_per_cycle:g} at the target frequency - cheapest "
        f"option, and the one that quietly degrades the fit.")
    return out


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_time(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "n/a"
    if seconds >= 1.0:
        return f"{seconds:.3f} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.3f} ms"
    if seconds >= 1e-6:
        return f"{seconds * 1e6:.3f} us"
    if seconds >= 1e-9:
        return f"{seconds * 1e9:.3f} ns"
    return f"{seconds * 1e12:.4f} ps"


def format_budget(budget: Budget, include_assumptions: bool = True) -> str:
    b = budget
    lines = []
    add = lines.append

    add("=" * 72)
    add(f"ASOPS FLY-SCAN ACQUISITION BUDGET  [{b.mode} mode]")
    add("=" * 72)
    add("")
    add("Spatial smear (the binding constraint on this stage)")
    add(f"  velocity                 {b.velocity_mm_s:.4g} mm/s")
    add(f"  smear per sweep          {b.smear_per_sweep_um:.4g} um "
        f"= velocity / sweep rate")
    add(f"  optical spot             {b.spot_size_um:.4g} um")
    add(f"  smear / spot             {b.smear_fraction_of_spot:.3f} "
        f"-> {'OK' if b.smear_ok else 'TOO FAST'} "
        f"(limit {b.max_smear_fraction:.2f})")
    add(f"  velocity for full spot   {b.velocity_for_full_spot_mm_s:.4g} mm/s")
    add(f"  velocity for half spot   {b.velocity_for_half_spot_mm_s:.4g} mm/s")
    add("")
    add("ASOPS timing")
    add(f"  rep rate                 {b.rep_rate_hz / 1e6:.6g} MHz")
    add(f"  sweep rate (offset)      {b.sweep_rate_hz / 1e3:.6g} kHz "
        f"- also the scope trigger")
    add(f"  stretch factor           {b.stretch_factor:.6g}")
    add(f"  delay window             {_fmt_time(b.delay_window_s)} of delay "
        f"per sweep")
    add(f"  intrinsic delay step     "
        f"{b.intrinsic_delay_resolution_s * 1e12:.4f} ps "
        f"(one probe pulse to the next)")
    add("")
    add("Digitiser")
    add(f"  sample rate              {b.sample_rate_hz / 1e6:.6g} MS/s")
    add(f"  samples per whole sweep  {b.samples_per_sweep:,.1f}")
    add(f"  delay resolution         "
        f"{b.delay_resolution_s * 1e12:.4f} ps per sample")
    add(f"  highest real frequency   {b.max_real_hz / 1e9:.4g} GHz "
        f"at {b.points_per_cycle:g} points/cycle")
    add(f"  (bare Nyquist)           {b.nyquist_real_hz / 1e9:.4g} GHz "
        f"at 2 points/cycle - not enough to fit amplitude and decay")
    if b.target_real_hz > 0:
        add(f"  at the {b.target_real_hz / 1e9:.4g} GHz target".ljust(27)
            + f"{b.points_per_cycle_at_target:.2f} points per cycle "
              f"({1e12 / b.target_real_hz:.1f} ps period / "
              f"{b.delay_resolution_s * 1e12:.4f} ps per sample)")
    add(f"  rep-rate ceiling         {b.rep_rate_hz / 1e6:.4g} MS/s "
        f"= one sample per probe pulse; a ceiling, not an operating point")
    add("")
    add("Segment capture window")
    add(f"  window                   "
        f"{b.segment_window_real_s * 1e9:.4g} ns of delay "
        f"({b.segment_window_fraction * 100:.1f}% of the 12.5 ns window)")
    add(f"  in lab time              {_fmt_time(b.segment_window_lab_s)} "
        f"({b.segment_duty * 100:.1f}% of the sweep period)")
    add(f"  points per segment       {b.points_per_segment:,.1f}")
    if b.target_real_hz > 0:
        add(f"  acoustic cycles held     {b.cycles_in_window:.2f} at "
            f"{b.target_real_hz / 1e9:.4g} GHz "
            f"-> frequency resolution about "
            f"{1.0 / b.segment_window_real_s / 1e9:.2f} GHz")
    add("")
    add("Sampling in space")
    add(f"  pixel pitch              {b.pixel_pitch_um:.4g} um")
    add(f"  sweeps per pixel         {b.sweeps_per_pixel:.3g}")
    add(f"  pixels per line          {b.pixels_per_line:,.1f}")
    add(f"  line length              {b.line_length_um:.4g} um")
    add(f"  line duration            {_fmt_time(b.line_duration_s)}")
    add(f"  sweeps per line          {b.sweeps_per_line:,.1f}")
    if b.mode == "sequence":
        add(f"  segments per line        {b.n_segments:,.0f} "
            f"-> {'OK' if b.segments_ok else 'TOO MANY'}")
    add("")
    add("Memory")
    add(f"  channels                 {b.n_channels} "
        f"[{b.channel_summary}] at {b.bytes_per_sample} bytes/sample")
    add(f"  points/line/channel      {b.points_per_line_per_channel:,.0f}")
    add(f"  same line captured flat  "
        f"{b.flat_points_per_line_per_channel:,.0f} points "
        f"-> windowing saves {b.reduction_vs_flat:.2f}x")
    add(f"  on scope per line        {b.mb_per_line_on_scope:,.3f} MB "
        f"(all channels)")
    add(f"  transferred per line     {b.mb_per_line_transferred:,.3f} MB "
        f"(sparsing applied)")
    add(f"  whole map ({b.n_lines} lines)".ljust(27)
        + f"{b.mb_map_transferred:,.2f} MB transferred")
    verdict = "FITS" if b.fits_record_length else "DOES NOT FIT"
    add(f"  record length            {b.record_length:,.0f} points/channel "
        f"-> {verdict}")
    if b.fits_record_length:
        add(f"  headroom                 {b.headroom_points:,.0f} points "
            f"({b.headroom_fraction * 100:.1f}%); longest line that fits "
            f"{b.max_line_length_um:,.1f} um")
    else:
        add(f"  shortfall                {-b.headroom_points:,.0f} points "
            f"({-b.headroom_fraction * 100:.1f}%); longest line that fits "
            f"{b.max_line_length_um:,.1f} um")
    add("")
    add("Transfer (see ASSUMPTIONS)")
    add(f"  assumed transfer rate    {b.transfer_rate_mb_s:g} MB/s")
    add(f"  download per line        {_fmt_time(b.download_s_per_line)}")
    add(f"  flight per line          {_fmt_time(b.line_duration_s)}")
    add(f"  dominated by             {b.dominant.upper()} "
        f"(transfer:flight = {b.transfer_to_flight_ratio:.2f}:1, "
        f"duty cycle {b.duty_cycle * 100:.1f}% flying)")
    add(f"  wall time, whole map     {_fmt_time(b.map_wall_time_s)}")
    add("")
    add("Position-trigger channel (BBD303 trigger out)")
    add(f"  trigger interval         {b.trigger_interval_um:.4g} um "
        f"= one pulse every {_fmt_time(b.trigger_pulse_interval_s)} "
        f"= {b.sweeps_per_trigger_pulse:.1f} sweeps")
    add(f"  programmed pulse width   {_fmt_time(b.trigger_pulse_width_s)}")
    add(f"  samples on a pulse       {b.trigger_samples_per_pulse:.2f} "
        f"after sparsing")
    if b.mode == "sequence":
        add(f"  pulses seen per segment  "
            f"{b.trigger_pulses_per_segment:.3f} - see the warning on how "
            f"position is recovered")
    add("")
    add("Probe DC channel (one scalar per copeak)")
    add(f"  samples per segment      {b.dc_samples_per_segment:.2f} "
        f"-> averaged to one DC value per sweep")
    add("")
    add("Stage / APT (velocity must be set programmatically)")
    if math.isfinite(b.counts_per_mm):
        add(f"  counts per mm            {b.counts_per_mm:,.1f} "
            f"({'verified' if b.apt_scaling_verified else 'UNVERIFIED'})")
        add(f"  SET_VELPARAMS velocity   {b.apt_velocity_counts:,.0f} counts "
            f"for {b.velocity_mm_s:.4g} mm/s")
        add(f"  SET_VELPARAMS accel      {b.apt_acceleration_counts:,.0f} "
            f"counts for {b.apt_acceleration_mm_s2:.4g} mm/s^2")
        add(f"  counts per pixel         {b.counts_per_pixel:,.2f} "
            f"at {b.pixel_pitch_um:.4g} um")
    else:
        add("  counts per mm            NOT SUPPLIED - no APT numbers; "
            "see the warning")

    if b.warnings:
        add("")
        add("WARNINGS")
        for item in b.warnings:
            add(_wrap(f"  ! {item}"))

    if include_assumptions:
        add("")
        add("ASSUMPTIONS (not derived facts)")
        for note in assumption_notes(
            ScopeConfig(record_length=b.record_length_nominal,
                        bytes_per_sample=b.bytes_per_sample,
                        transfer_rate_mb_s=b.transfer_rate_mb_s),
            AcquisitionConfig(points_per_cycle=b.points_per_cycle),
            None if not math.isfinite(b.counts_per_mm)
            else StageConfig(counts_per_mm=b.counts_per_mm,
                             scaling_verified=b.apt_scaling_verified),
        ):
            add(_wrap(f"  - {note}"))
    add("=" * 72)
    return "\n".join(lines)


def format_solution(solution: Solution, include_assumptions: bool = True) -> str:
    s = solution
    lines = []
    add = lines.append
    add("=" * 72)
    add("SOLVED PARAMETERS")
    add("=" * 72)
    add(f"  velocity                 {s.velocity_mm_s:.6g} mm/s "
        f"(limited by {s.velocity_limited_by})")
    add(f"  sample rate              {s.sample_rate_hz / 1e6:.6g} MS/s")
    add(f"  sample-rate ceiling      {s.sample_rate_ceiling_hz / 1e6:.6g} "
        f"MS/s (limited by {s.sample_rate_limited_by})")
    if s.required_sample_rate_hz > 0:
        add(f"  rate needed for target   "
            f"{s.required_sample_rate_hz / 1e6:.6g} MS/s"
            f"  -> target {'MET' if s.target_feasible else 'NOT MET'}")
    add("")
    for note in s.notes:
        add(note if note.startswith("  fix:") else _wrap(f"  * {note}"))
    add("")
    add(format_budget(s.budget, include_assumptions=include_assumptions))
    return "\n".join(lines)


def _wrap(text: str, width: int = 72) -> str:
    """Wrap keeping the leading indent, without textwrap's quirks."""
    indent = len(text) - len(text.lstrip())
    pad = " " * (indent + 4)
    out, current, first = [], " " * indent, True
    for word in text.split():
        candidate = current + ("" if first else " ") + word
        if len(candidate) > width and not first:
            out.append(current)
            current = pad + word
        else:
            current, first = candidate, False
    out.append(current)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_common_args(parser):
    parser.add_argument("--counts-per-mm", type=float, required=True,
                        help="REQUIRED, no default: encoder counts per mm "
                             "for this stage and controller. pscan's 34304 "
                             "is the Z8xx/TDC001 figure and is probably "
                             "wrong for an MLS203 on a BBD303; a wrong value "
                             "silently rescales every position")
    parser.add_argument("--counts-per-mm-verified", action="store_true",
                        help="mark counts_per_mm and the APT scaling as "
                             "calibrated rather than assumed")
    parser.add_argument("--mode", choices=("sequence", "flat"),
                        default="sequence",
                        help="sequence: one segment per ASOPS sweep "
                             "(default); flat: one continuous record")
    parser.add_argument("--rep-rate", type=float, default=DEFAULT_REP_RATE_HZ,
                        help="laser repetition rate in Hz (default 80e6)")
    parser.add_argument("--sweep-rate", type=float,
                        default=DEFAULT_SWEEP_RATE_HZ,
                        help="ASOPS sweep rate / laser offset / scope trigger "
                             "rate in Hz (default 10e3)")
    parser.add_argument("--line", type=float, default=500.0,
                        dest="line_length_um",
                        help="line length in um (default 500)")
    parser.add_argument("--pitch", type=float, default=1.0,
                        dest="pixel_pitch_um",
                        help="pixel pitch in um (default 1.0)")
    parser.add_argument("--spot", type=float, default=1.0,
                        dest="spot_size_um",
                        help="optical spot size in um (default 1.0)")
    parser.add_argument("--smear-fraction", type=float, default=1.0,
                        help="per-sweep smear allowed as a fraction of the "
                             "spot (default 1.0; 0.5 is conservative)")
    parser.add_argument("--lines", type=int, default=1, dest="n_lines",
                        help="number of lines in the map (default 1)")
    parser.add_argument("--window-ns", type=float, default=2.0,
                        help="segment capture window in ns of REAL delay "
                             "(default 2.0 of the 12.5 ns window)")
    parser.add_argument("--window-fraction", type=float, default=0.0,
                        help="capture window as a fraction of the delay "
                             "window instead of --window-ns")
    parser.add_argument("--ac-channels", type=int, default=1,
                        help="full-rate AC signal channels (default 1)")
    parser.add_argument("--dc-sparsing", type=int, default=20,
                        help="sparsing on the probe DC channel, 0 to omit it "
                             "(default 20)")
    parser.add_argument("--trigger-sparsing", type=int, default=20,
                        help="sparsing on the position-trigger channel, 0 to "
                             "omit it (default 20)")
    parser.add_argument("--spare-channels", type=int, default=0,
                        help="extra full-rate channels (default 0)")
    parser.add_argument("--trigger-interval", type=float, default=0.6,
                        help="BBD303 position-trigger interval in um "
                             "(default 0.6; avoid intervals whose travel "
                             "time is a simple fraction of the sweep period)")
    parser.add_argument("--trigger-width", type=float, default=20.0e-6,
                        help="programmed trigger pulse width in s "
                             "(default 20e-6)")
    parser.add_argument("--acceleration", type=float, default=1000.0,
                        help="stage acceleration in mm/s^2 for the APT "
                             "numbers (default 1000)")
    parser.add_argument("--bytes-per-sample", type=int, default=2,
                        help="2 for WORD transfer, 1 for BYTE (default 2)")
    parser.add_argument("--record-length", type=int,
                        default=ASSUMED_RECORD_LENGTH,
                        help=f"points per channel at or below the interleave "
                             f"channel count (default "
                             f"{ASSUMED_RECORD_LENGTH}, ASSUMED - query "
                             f"MSIZ?)")
    parser.add_argument("--max-segments", type=int,
                        default=ASSUMED_MAX_SEGMENTS,
                        help=f"segments the scope accepts in one Sequence "
                             f"acquisition (default {ASSUMED_MAX_SEGMENTS}, "
                             f"ASSUMED)")
    parser.add_argument("--interleave-factor", type=float, default=2.0,
                        help="per-channel memory divisor above 2 channels; "
                             "1.0 disables the model (default 2.0)")
    parser.add_argument("--record-length-confirmed", action="store_true",
                        help="mark the record length as read off the "
                             "instrument")
    parser.add_argument("--transfer-rate", type=float,
                        default=ASSUMED_TRANSFER_RATE_MB_S,
                        help=f"assumed sustained transfer rate in MB/s "
                             f"(default {ASSUMED_TRANSFER_RATE_MB_S})")
    parser.add_argument("--transfer-rate-measured", action="store_true",
                        help="mark the transfer rate as measured")
    parser.add_argument("--points-per-cycle", type=float,
                        default=DEFAULT_POINTS_PER_CYCLE,
                        help="points per Brillouin cycle required for "
                             "'adequately sampled' (default 10)")
    parser.add_argument("--max-sample-rate", type=float,
                        default=SCOPE_MAX_SAMPLE_RATE_HZ,
                        help="scope maximum sample rate in S/s (default 2e9)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of a report")
    parser.add_argument("--terse", action="store_true",
                        help="emit one line of key=value pairs, for sweeping "
                             "parameters from a shell loop")
    parser.add_argument("--no-assumptions", action="store_true",
                        help="omit the assumptions block from the report")


def _configs_from_args(args):
    timing = AsopsTiming(rep_rate_hz=args.rep_rate,
                         sweep_rate_hz=args.sweep_rate)
    scope = ScopeConfig(
        record_length=args.record_length,
        max_sample_rate_hz=args.max_sample_rate,
        bytes_per_sample=args.bytes_per_sample,
        transfer_rate_mb_s=args.transfer_rate,
        max_segments=args.max_segments,
        interleave_factor=args.interleave_factor,
        record_length_confirmed=args.record_length_confirmed,
        transfer_rate_measured=args.transfer_rate_measured)
    channels = default_channel_plan(
        ac_channels=args.ac_channels, dc_sparsing=args.dc_sparsing,
        trigger_sparsing=args.trigger_sparsing,
        spare_channels=args.spare_channels)
    stage = StageConfig(
        counts_per_mm=args.counts_per_mm,
        max_acceleration_mm_s2=args.acceleration,
        scaling_verified=args.counts_per_mm_verified)
    window_real = (0.0 if args.window_fraction > 0
                   else args.window_ns * 1e-9)
    return timing, scope, channels, stage, window_real


_TERSE_FIELDS = (
    "mode", "sample_rate_hz", "velocity_mm_s", "line_length_um",
    "pixel_pitch_um", "spot_size_um", "smear_per_sweep_um",
    "smear_fraction_of_spot", "smear_ok", "sweeps_per_pixel",
    "segment_window_real_s", "points_per_segment", "n_segments",
    "delay_resolution_s", "max_real_hz", "points_per_line_per_channel",
    "reduction_vs_flat", "mb_per_line_transferred", "fits_record_length",
    "headroom_fraction", "line_duration_s", "download_s_per_line",
    "dominant", "transfer_to_flight_ratio", "apt_velocity_counts",
)


def _terse(budget: Budget) -> str:
    d = budget.to_dict()
    parts = []
    for key in _TERSE_FIELDS:
        value = d[key]
        parts.append(f"{key}={value:.6g}" if isinstance(value, float)
                     else f"{key}={value}")
    return " ".join(parts)


def main(argv=None):
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="flyscan-budget",
        description="ASOPS fly-scan acquisition budget: check a "
                    "configuration, or solve for one.")
    sub = parser.add_subparsers(dest="command", required=True)

    fwd = sub.add_parser("forward",
                         help="cost a configuration you already have in mind")
    _add_common_args(fwd)
    fwd.add_argument("--velocity", type=float, default=10.0,
                     dest="velocity_mm_s",
                     help="stage velocity in mm/s (default 10)")
    fwd.add_argument("--rate", type=float, default=10.0e6,
                     dest="sample_rate_hz",
                     help="sample rate in S/s (default 10e6)")
    fwd.add_argument("--target-freq", type=float, default=5.0e9,
                     help="Brillouin frequency in Hz that must be resolved "
                          "(default 5e9; 0 disables the check)")

    slv = sub.add_parser(
        "solve",
        help="given a spot size, a pitch and a budget, report the velocity "
             "and sample rate that satisfy them")
    _add_common_args(slv)
    slv.add_argument("--min-sweeps-per-pixel", type=float, default=1.0,
                     help="sweeps that must land in each pixel (default 1)")
    slv.add_argument("--max-velocity", type=float, default=250.0,
                     help="stage velocity ceiling in mm/s; 0 = do not cap "
                          "(default 250, the MLS203/BBD303 figure)")
    slv.add_argument("--target-freq", type=float, default=5.0e9,
                     help="Brillouin frequency in Hz that must be resolved "
                          "(default 5e9; 0 = take all available bandwidth)")
    slv.add_argument("--max-mb-per-line", type=float, default=0.0,
                     help="transfer budget in MB per line; 0 = unconstrained")
    slv.add_argument("--max-download-s", type=float, default=0.0,
                     help="download-time budget in s per line; "
                          "0 = unconstrained")
    slv.add_argument("--no-snap", action="store_true",
                     help="do not snap the sample rate to the 1-2-5 ladder")

    args = parser.parse_args(argv)
    timing, scope, channels, stage, window_real = _configs_from_args(args)

    if args.command == "forward":
        acq = AcquisitionConfig(
            sample_rate_hz=args.sample_rate_hz, channels=channels,
            mode=args.mode, points_per_cycle=args.points_per_cycle,
            target_real_hz=args.target_freq,
            segment_window_real_s=window_real,
            segment_window_fraction=args.window_fraction or 0.16,
            trigger_interval_um=args.trigger_interval,
            trigger_pulse_width_s=args.trigger_width)
        scan = ScanConfig(
            line_length_um=args.line_length_um,
            velocity_mm_s=args.velocity_mm_s,
            pixel_pitch_um=args.pixel_pitch_um, n_lines=args.n_lines,
            spot_size_um=args.spot_size_um,
            max_smear_fraction=args.smear_fraction)
        budget = compute_budget(timing, scan, acq, scope, stage)
        if args.json:
            print(json.dumps(budget.to_dict(), indent=2))
        elif args.terse:
            print(_terse(budget))
        else:
            print(budget.report(include_assumptions=not args.no_assumptions))
        return 0 if (budget.fits_record_length and budget.smear_ok
                     and budget.segments_ok) else 1

    acq = AcquisitionConfig(
        channels=channels, mode=args.mode,
        points_per_cycle=args.points_per_cycle,
        segment_window_real_s=window_real,
        segment_window_fraction=args.window_fraction or 0.16,
        trigger_interval_um=args.trigger_interval,
        trigger_pulse_width_s=args.trigger_width)
    request = SolveRequest(
        line_length_um=args.line_length_um,
        pixel_pitch_um=args.pixel_pitch_um, n_lines=args.n_lines,
        spot_size_um=args.spot_size_um,
        max_smear_fraction=args.smear_fraction,
        min_sweeps_per_pixel=args.min_sweeps_per_pixel,
        max_velocity_mm_s=args.max_velocity, target_real_hz=args.target_freq,
        points_per_cycle=args.points_per_cycle,
        max_mb_per_line=args.max_mb_per_line,
        max_download_s_per_line=args.max_download_s,
        snap_to_ladder=not args.no_snap)
    solution = solve_for_pitch(request, timing, acq, scope, stage)
    if args.json:
        print(json.dumps(solution.to_dict(), indent=2))
    elif args.terse:
        print(_terse(solution.budget))
    else:
        print(solution.report(include_assumptions=not args.no_assumptions))
    ok = (solution.budget.fits_record_length and solution.target_feasible
          and solution.budget.smear_ok and solution.budget.segments_ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
