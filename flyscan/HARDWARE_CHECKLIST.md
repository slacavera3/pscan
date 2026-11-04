# Fly-scan hardware checklist

Everything the fly-scan code assumes but cannot verify without the rig. Each
item says what we assume, why a wrong value hurts, and how to settle it.

Ordered by how badly a wrong answer corrupts data.

---

## 1. `counts_per_mm` for the MLSA02 — BLOCKING

**Assumed:** nothing. `VelocityCalibration` deliberately has no default.

**Currently in pscan:** `counts_per_mm = 34304.0`, hardcoded in
`pscan/main.py`. That is the Z8xx / TDC001 **DC-servo** figure, which is a
different controller family from a BBD303 driving a direct-drive stage. It
may still be right for your setup, but it should not be assumed.

**Why it matters:** scales every commanded position *and*, through the
calibration, every velocity. A wrong value stretches or compresses the whole
map uniformly, which is easy to miss because the data still looks sensible.

**How to settle it, best first:**
1. Your own lab wiki lists `BBD302: Benchtop DC controller, hardcoded for 2
   axis MLS203` at `/home/scan/source/hardware/apt_control`. That is working
   code for very nearly this pairing — read the constant out of it. This is
   the most reliable source available and it is already on your machines.
2. Thorlabs APT Communications Protocol PDF (Rev 27+), the conversion-factors
   section, which tabulates `EncCnt` per stage.
3. Empirically: command a 1.000 mm move and measure actual travel against a
   dial indicator, or image a calibration target with known feature spacing
   and compare commanded to apparent pitch.

**Note:** web searches did not yield MLS203 counts/mm, max velocity or
acceleration. The obsolete Thorlabs catalog pages give repeatability
(0.25 µm) and accuracy (<3 µm) but not the scaling. Renishaw confirms the
MLS203 uses their TONiC encoders, but the 100 nm / 0.4 m/s figures in that
case study are for the DDS600, **not** the MLS203 — do not transfer them.

---

## 2. APT velocity and acceleration scaling factors — BLOCKING

**Assumed:** nothing. `set_velocity_params_raw()` takes raw controller units
and `calibrate_velocity_scale()` measures the mapping on hardware.

**Why it matters:** these factors differ from the position scaling factor and
differ again between controller families. Getting one wrong flies the stage
at the wrong speed, and if position is inferred from time rather than
measured, that error is **completely invisible in the data** — the map just
has the wrong pitch.

**How to settle it:** run `calibrate_velocity_scale()` over a range of raw
values and check the fit residual. A clean straight line through the origin
means the mapping is sound. Cross-check against the lab code in item 1 and
against the protocol PDF; if the measured scale disagrees with the documented
one, trust the measurement and work out why.

The general APT form is `velocity_scale = EncCnt × T × 65536` with
`acceleration_scale = EncCnt × T² × 65536`, where `T` is the controller's
sampling interval. **We did not confirm `T` for the BBD303** — a value of
102.4 µs is plausible for the brushless family but unverified, and should not
be coded in until checked.

---

## 3. Arm-to-motion latency — BLOCKING for absolute position

**Assumed:** small and repeatable, having been reduced to a single 6-byte
serial write (~0.5 ms at 115200 baud) by splitting `preload_target()` from
`trigger_move()`.

**Why it matters:** the ASOPS delay clock runs continuously, so the scope
starts filling segments the instant it is armed, whether or not the stage has
moved. Whatever delay sits between arming and motion becomes a position
offset on every line — and because it does not reverse with direction, it
shows up as a comb/interlace artifact on a bidirectional raster.

For scale: pscan's unmodified `move_absolute()` carries a 50 ms sleep between
loading the target and executing it. At 10 mm/s that is **500 µm**, an entire
line. Hence the split; but ~1 ms of residual latency is still ~10 µm at
10 mm/s, which is not nothing against a 1 µm pixel.

**How to settle it:**
- Measure the spread: fly the same line repeatedly and compare where a sharp
  edge lands. The scatter is the latency jitter.
- Remove the problem rather than correcting it, by recording position on a
  scope channel (item 4).
- Failing that, calibrate the offset per direction by scanning an edge
  forward and backward, and apply a direction-dependent shift.

---

## 4. BBD303 position-trigger output — the clean fix for item 3

**Assumed:** the capability exists (BBD30x supports trigger output at
programmed position intervals). **The APT message IDs and payload layout for
it are NOT implemented** — guessing message bytes for a motion controller is
not worth the risk.

**Why it matters:** recording the trigger pulse train on a scope channel
gives exact absolute position in the same time base as the signal, with no
calibration, no drift, no velocity model, and no latency sensitivity. It
turns items 2 and 3 from error sources into non-issues.

**How to settle it:** the trigger I/O section of the APT protocol PDF for the
BBD30x. Then:
- Set the pulse **width** wide — tens of µs — so it survives transfer
  sparsing. A 1 µs pulse sparsed to 100 kS/s is simply missed.
- Keep the pulse interval longer than the width. At 10 mm/s with 0.5 µm
  intervals, pulses are 50 µs apart, so ~20 µs is a clean duty cycle.

---

## 5. Hardware-timed analog output on the PCIe-6351 under comedi

**Assumed:** nothing yet. This decides whether intra-line focus correction
is possible at all.

**Why it matters:** sample tilt is generally multi-directional, so over a
long fast axis focus drifts *within* a line, not just between lines. A
per-line focus update (which the Phidget can manage) only fixes the slow
axis. Correcting along the fast axis needs an analog ramp played in
hardware, triggered at line start.

The PCIe-6351 is the right instrument for it — 16-bit AO, ~2.8 MS/s,
externally triggerable, on PCIe rather than USB — and it is already owned
and already driven through comedi for analog input. The open question is
whether comedi's `ni_pcimio` exposes **hardware-timed** AO on X-series, or
only software-timed single writes. Software-timed is still far better than
the Phidget, but it is not a clean synchronised ramp.

**How to settle it:**
- Check whether `ni_pcimio` reports an AO subdevice with `SDF_CMD` set
  (`comedi_get_subdevice_flags`), which is what distinguishes a
  command-capable subdevice from one that only does `comedi_data_write`.
- If it does, confirm an AO command accepts an external start trigger.
- If it does not, the fallbacks are NI-DAQmx on a supported distro, or
  splitting the field into strips short enough that per-line focus holds
  (see PERFORMANCE.md — about 156 um at 1 mrad tilt).

**Related, and worth settling at the same time:** the AO range is +/-10 V
while the piezo amplifier wants 0-5 V. Use the narrowest range the card
offers, clamp in software, and consider a passive clamp in the BNC-2110
path — a 16-bit DAC glitching to -10 V into a piezo amplifier is expensive.

---

## 6. Piezo hysteresis against the focus excursion

**Assumed:** that a fitted focus surface can be applied open-loop.

**Why it matters:** open-loop piezo drive has 10-15% hysteresis **of the
excursion**, plus creep. The current workflow is immune because it
autofocuses fresh each time and never relies on the voltage-to-position
map. A fitted surface does rely on it, and a bidirectional raster ramps
focus up and down, which is exactly where hysteresis shows.

Hysteresis scales with excursion, not full range, which is the saving
grace: a field needing +/-1 um of focus travel sees ~100 nm of error
against a ~312 nm depth of focus - marginal but probably survivable. A
field needing +/-10 um does not.

**How to settle it:** the 3x3 focus map measures the excursion your field
actually needs, before anything depends on it. If it is too large, the fix
is a closed-loop controller using the scanner's own sensor (PI E-709/E-727
class). If the PIFOC has a sensor but no controller reading it, the 6351's
AI could at least record position so focus is measured rather than assumed.

---

## 7. HRO 66Zi maximum segment count and memory

**Assumed:** enough segments for one line (500–5000) and enough memory for
`segments × points_per_segment × channels`. `configure_line()` reads the
granted segment count back and **raises** if it is short, so this fails loudly
rather than misaligning a line.

**How to settle it:** `MSIZ?` for the current record length, `*OPT?` for
installed memory options. Then push `SEQ ON,<n>` upward until it refuses, and
note the ceiling.

---

## 8. Four-channel derating

**Assumed:** irrelevant. Many LeCroy models halve sample rate or memory with
all four channels enabled, but at 10–25 MS/s there are orders of magnitude of
headroom, so it should not bind.

**Channel plan:** C1 signal (full rate), C2 probe DC (sparsed ~100×),
C3 position triggers (sparsed ~20×, bounded by pulse width), C4 spare.

**How to settle it:** enable all four, set the timebase, and read
`HORIZ_INTERVAL` back out of a waveform. `download_line()` already warns when
the achieved interval is not what was requested.

---

## 9. VXI-11 transfer rate

**Assumed:** 4 MB/s, as `FlyScope(transfer_mb_s=...)`. Used only to size
timeouts and to report whether a scan is transfer-bound.

**Why it matters:** transfer dominates flight by roughly 1.3:1 at a half
window, so it sets the scan time. If it is much slower than assumed, the
derived timeouts could be too tight.

**How to settle it:** time one `download_line()` against the byte count from
`line_budget()`. If it is poor, the levers are LeCroy's native VICP instead
of VXI-11, or saving to the scope's internal disk and pulling over SMB — the
66Zi runs Windows internally.

---

## 10. Spot size, and therefore maximum velocity

**Assumed:** ~1 µm, giving `velocity ≤ spot × delay_rate` = 10 mm/s.

**Why it matters:** this is the governing constraint on the whole design.
Smear per sweep is `velocity / delay_rate`; at the stage's full 250 mm/s that
would be 25 µm per delay sweep, which is unusable.

**How to settle it:** measure the optical resolution directly (edge response
or a resolution target), then pick the smear fraction you will accept.

---

## 11. Delay window actually in use

**Confirmed:** half of the window, i.e. **50 µs of lab time**, which at
10 MS/s is 500 points per segment. The window is one laser period, so what
that half is worth in real delay depends on the bench: 6.25 ns of a 12.5 ns
window at 80 MHz, 5 ns of a 10 ns window at 100 MHz. Declare `rep_rate` in
the config so the file records which.

**Still to confirm:** that half is genuinely enough — that nothing of interest
(a substrate echo off the diamond, say) sits in the discarded half. Capture
one **full-window** line with `configure_flat_line()` and look, before
committing a long scan to the half window.

---

## 12. Scope timebase versus the laser

**Assumed:** not locked.

**Why it matters:** over a long record the sample grid drifts relative to the
pulse train. Less critical in Sequence mode, where each segment is
independently triggered so nothing accumulates, but it matters for the flat
diagnostic captures.

**How to settle it:** feed the laser's rep rate (divided) into the 66Zi's
10 MHz reference input and confirm it locks.

---

## 12b. The trigger is never configured — only inherited

**This is a gap in the code, found chasing item 13.** The complete set of
SCPI that `scope.py` sends is `CHDR OFF`, `CFMT`, `SEQ ON,n,pts`, `TDIV`,
`MSIZ`, `CLSW`, `TRMD SINGLE`, `ARM`, `WAIT`, `WFSU`, `Cn:WF?`. Nothing sets
or reads back:

| | |
|---|---|
| `TRSE` | trigger source, type **and holdoff** |
| `TRSL` | trigger slope |
| `TRLV` | trigger level |
| `TRCP` | trigger coupling |

So a fly scan inherits whatever the front panel was last left on — possibly
by pscan, possibly by hand. Every pixel in a fly scan is one segment and one
trigger, so the source, slope, level and holdoff decide the entire
position-to-sample mapping, and they are the one thing the acquisition
neither sets nor verifies. `configure_line()` carefully checks that the
scope granted the segment count and the points per segment, then takes the
trigger entirely on trust.

**Measured on the WS4024HD (`scope_probe.py`, as found):**

| | | |
|---|---|---|
| `TRSE?` | `EDGE,SR,C1,HT,OFF` | source **is** C1, holdoff off — good |
| `TRMD?` | `AUTO` | **self-triggering** |
| `TRDL?` | `0E-6` | no delay |
| `C1:TRLV?` | `480E-3` | see below |
| `C1:TRCP?` | `DC` | trigger coupling |
| `C1:CPL?` | `A1M` | input **AC** coupled, 1 MΩ |
| `SEQ?` | `ON,500,1.25E+3` | as configured |
| `TDIV?`/`MSIZ?` | `5E-6` / `1.25E+3` | ⇒ 25.0 MS/s, 50 µs — as requested |
| `SARA?` | no reply | not supported on this model; derive it instead |

So the trigger *source* hypothesis is dead — it is C1, as intended. Two
things remain:

**`TRMD AUTO`.** In AUTO the scope self-triggers whenever a real trigger
does not arrive in time, which decouples a segment from the signal entirely
and lands it at an arbitrary phase. A sequence must fill in SINGLE or NORM.
`arm_line()` does write `TRMD SINGLE`, and `disconnect()` deliberately
restores AUTO, so "as found" may simply be the previous run's teardown —
`--after-arm` is what settles whether the arming takes.

**Trigger level against AC coupling.** `A1M` centres a symmetric square wave
on 0 V, so the natural trigger level is 0 V, not 480 mV. At 500 mV/div a
trace that fits the screen is at most ~±2 V, and 480 mV is 96% of peak for a
1 Vpp signal, 48% for 2 Vpp. If it sits near the top, triggers are missed
intermittently — and in AUTO a missed trigger becomes a self-trigger, which
is precisely the drift. Check the amplitude against the level.

---

## 12c. SOLVED — the drift was a reshape, not the instrument

**The MATLAB loader hardcoded `points_per_trace = 500`; the files hold 502.**

Reading a flat array of `N`-point segments as if they were `M` points long
shears it. A feature at offset `p` in segment `j` sits at flat index
`jN + p`, which on reshape lands in row `floor((jN+p)/M)` at column
`(jN+p) mod M`, so per *row* it advances by

    walk = (N - M) * M / N      samples

With `M = 500` that gives:

| N | walk | wrap period |
|---|---|---|
| 499 | −1.00200 | 499 rows |
| 501 | +0.99800 | 501 rows |
| **502** | **+1.99203** | **251 rows** |
| 503 | +2.98211 | 168 rows |

`measure_walk.m` measured **+1.99203 samples/segment, wrap period 251.0**.
That is `1000/502 = 1.99203187` to five significant figures, not a fit that
happens to land near 2. The instrument was never at fault.

It explains every observation at once: dead-straight linearity, the same
behaviour behind a copeak and a square wave, presence from the very first
test, and a perfectly clean trigger chain (`TRSE EDGE,SR,C1,HT,OFF`,
`TRMD SINGLE` after arming, 25.0 MS/s derived from TDIV/MSIZ).

**Withdrawn as artifacts of this bug:** the 19960 Hz beat, the ×2 divider,
the 1992 ppm loop residual, the "half the pixels have no copeak" claim, and
the lost-pixel arithmetic in item 13 below. None of it describes the lasers.

**Two-second check on any existing file:** 10000 segments × 500 points ×
2 bytes = 10,000,000 bytes; at 502 points it is 10,040,000. Just stat the
`.bin`.

**The fix:** never hardcode the shape. `manifest.json` records the real one
per channel, and `io_hdf5.py` has carried the correct idiom in its docstring
all along:

```matlab
m  = jsondecode(fileread('map.flyscan/manifest.json'));
f  = fopen('map.flyscan/line_0000_ac.bin');
ac = reshape(fread(f, Inf, 'int16=>double'), m.lines(1).channels(1).shape);
```

**Why 502 and not 500:** the scope returns what it returns, and
`configure_line()` only raises when the granted count differs from the
request — the wavedesc is then believed, correctly, by `download_line()`.
So the written file is right and the manifest is right. Worth confirming
against `SEQ?`/`MSIZ?` whether this model always adds two points per
segment, since that is the kind of thing a sanity check should name.

---

## 13. ASOPS offset stability

**Open, and the evidence is contradictory.** One 10 kHz signal generator does
two jobs: it triggers the scope, and it sets the slave laser's offset so the
slave runs at `f_rep + 10 kHz`. If that offset loop closes, the beat *is* the
generator, the trigger and the delay sweep share one oscillator, and the
copeak cannot walk at all. It walks. So the loop is not delivering its
setpoint, and that — not the trigger wiring — is the fault to chase.

**Why it matters:** the delay rate sets the pixel clock, so an error moves
the delay-axis calibration and the pixel pitch together. Worse, a beat that
differs from the trigger makes every segment start at a different point in
the delay window, which walks the feature straight through the record.

**Observed:** a slow bidirectional test (10 kHz trigger, 100 MHz master,
10 MS/s, 500 points/segment) walked the coincidence peak by **~2 samples per
segment**, dead linear. That is ~200 ns of lab time per segment against a
100 µs trigger period, a fractional mismatch of ~2e-3 — a true beat near
9.98 kHz against a 10.000 kHz setpoint, about 20 Hz of static error. A closed
phase lock has none. A frequency lock with finite DC gain, or a drive into
the rep-rate actuator with no feedback at all, has exactly this much.

**Suspects, in order:** (1) the loop is unlocked, out of capture range, or
railed — check the lock indicator and error monitor *while a line runs*;
(2) it is feed-forward rather than a servo, so nothing forces the achieved
offset to equal the setpoint.

**Measured: two faults, a factor of two and a 2e-3 residual.**
`measure_walk.m` on a 10000-segment line gives a walk of **+1.99203
samples/segment**, residual rms 1.93 samples against the 144.3 that pure
noise would give — a real fit, not a fluke. From it:

| | |
|---|---|
| copeak recurrence | 50.0996 µs |
| **true beat** | **19960.24 Hz** |
| setpoint | 10000 Hz |
| **ratio** | **1.996024** |
| beat vs *exactly* 2× trigger | 1992 ppm low |
| wrap period | 251 segments |

On its own this would read as a prescaler error delivering twice the
setpoint, plus a loop missing that doubled target by 2e-3.

**It is not the lasers. Disregard those numbers.** A flyscan acquisition of a
**1 kHz square wave that was both the signal and the trigger** — 500
segments, 50 µs window, 500 points — shows the edge marching steadily across
the window, the same linear drift as the copeak. That is impossible with
genuine per-segment triggering: if the scope triggers on an edge of a
channel, that edge is pinned at the trigger point in every segment by
construction, and a fixed `TRDL` moves it by the same amount every time. A
*moving* edge means the record is not starting at a trigger on that signal.

So the drift is in the acquisition, and the same artifact would appear
behind any input. Every laser conclusion above describes the artifact, not
the lasers: the ×2, the 19960 Hz, the divider, the loop residual. All void
until the acquisition is cleared.

**A second anomaly in the same figure.** At 1 kHz the half period is 500 µs,
so a genuine 50 µs window triggered on an edge should show a *constant
level* and no transition whatsoever. There is a transition in essentially
every segment, which puts the real window nearer 50-100% of a half period.
Either the window is not 50 µs, the sample rate is not 10 MS/s, or the
signal was not 1 kHz. `download_line()` warns when the returned
`horiz_interval` is neither the requested interval nor the sparsed one
("the scope chose its own timebase") — check whether that warning fired on
this run; it only prints, it does not raise.

**Measure the walk, do not eyeball it.** `measure_walk.m` fits the copeak
index against segment number over the longest run where the copeak is
actually in the window, and converts the slope to a beat frequency. Reading
band spacing off an `imagesc` of 10000 segments is unreliable — the display
downsamples ~12:1 and the apparent spacing can be out by an integer factor.
Fit residuals matter too: a clean straight line means a static frequency
error, while curvature or a wandering rate means the beat itself is moving,
which separates suspect (1) from suspect (2).

**Test 1, and nothing else until it is done — read TRIGTIME.**
`check_trigtime.m`. The scope records when each segment actually triggered,
against the same clock that samples the data, so it separates the two cases
outright. Trigger spacing ~100.000 µs while the copeak walks 199.2 ns per
segment means the trigger is honest and the fault is in the lasers. Trigger
spacing ~100.199 µs means the trigger is slow, *every* signal walks, and
the laser conclusions are void. It also removes the assumption that the
generator is exactly 10.000 kHz: the segment count only confirms the trigger
to ~0.1%, and TRIGTIME pins it to ppm. The file is already on disk next to
the channel data as `line_NNNN_trigtime.bin`; nothing needs re-acquiring.

**Test 2 — run `measure_walk.m` on the square-wave line.** If the pulses
walk at the same ~1.992 samples/segment as the copeak, one common cause is
driving both, and it is not the lasers. A different rate means two separate
effects. Also uses data already on disk.

**Test 3, only once the above clears the acquisition:** set the generator to
10.100 kHz and re-run `measure_walk.m`. If the loop follows, the beat goes
to ~20160 Hz and the ratio holds at 1.996 — divider plus loop gain. If the
beat barely moves, the loop is not following at all.

**No pixels are being lost — the doubled beat saved us.** At a 19960 Hz beat
the stretch factor is `f_rep/Δf` = **5010**, not 10000, so 500 samples at
10 MS/s span **9.98 ns of delay: 99.8% of the full 10 ns window**, not half
of it. That is why the copeak is present in 9997 of 10000 segments and the
sawtooth fills 499 of 500 samples. The "half window" has been capturing
essentially the whole thing all along.

So the walk is a pure circular shift of 39.76 ps of delay per segment with
nothing missing behind it, and realigning per segment in software is a
legitimate interim fix. That only holds while the beat is doubled: repair
the divider without widening `segment_window` and the window really does
become half, at which point the walk starts costing pixels for real.

**How to settle it properly:** fix the loop, so the setpoint is the beat.

**If the loop cannot be closed:** trigger from the beat *measured*, not from
the setpoint. Split a little of each laser before the sample, combine on a
fast photodiode, low-pass: that gives the same 10 kHz coincidence train from
strong, clean, sample-independent light. It is the true beat whether or not
the servo behaves, and unlike the through-sample copeak it does not care
about focus, reflectivity or topography. Do **not** trigger off the AC
measurement channel: threshold crossing moves with pulse height, so a
varying-reflectivity sample would imprint a timing error on its own delay
axis — an artifact correlated with the image, which is the hardest kind to
notice. (Lost triggers alone would be survivable: `analyse_trigtime` fits a
lattice and preserves sweep numbering across gaps, so a miss costs that pixel
and does not renumber the rest. Amplitude-dependent trigger walk is the part
no lattice fit can undo.)

**Workaround if no beat trigger is available:** capture the *whole* window,
`segment_window 101e-6` (1010 points at 10 MS/s — slightly over one trigger
period, since the delay window is `stretch/f_rep` ≈ 100.2 µs, not 100.0).
The copeak is then always somewhere in the record and the walk becomes a
realignment the copeak finder can take out. It doubles an already
transfer-dominated line: ~20 MB/line/channel, ~14 s of transfer against 1 s
of flight, and 10 Mpts/channel of scope memory. Check the scope grants the
segment count before committing a long scan.

**How to measure it:** the TRIGTIME array from any Sequence acquisition gives
measured segment-to-segment spacing, which is a direct read on the *trigger*.
Comparing that against the observed per-segment walk of the copeak separates
the two: TRIGTIME flat at 100.000 µs means the generator is doing its job and
the beat is elsewhere. Record a `trigger` channel and log it per line.

---

## 14. AC / DC channel relative delay

**Assumed:** negligible.

**Why it matters:** ΔR/R divides the AC trace by its DC value. Since DC is a
slowly-varying envelope rather than something read at the acoustic timescale,
a 1 µs mismatch is ~1% of a sweep, about 10 nm of position error — fine. Only
worth checking if the two detector paths differ by more than a few µs.

---

## 15. Sequence-mode inter-segment dead time

**Assumed:** µs-scale, against a gap of 100 µs − 50 µs = 50 µs between the
end of one captured window and the next trigger. Should be comfortable.

**How to settle it:** if segments are being dropped, TRIGTIME spacing will
show gaps at exactly the sweep period. That is the diagnostic.
