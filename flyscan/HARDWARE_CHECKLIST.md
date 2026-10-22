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

**Why it matters:** the ASOPS sweep clock runs continuously, so the scope
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

**Assumed:** ~1 µm, giving `velocity ≤ spot × sweep_rate` = 10 mm/s.

**Why it matters:** this is the governing constraint on the whole design.
Smear per sweep is `velocity / sweep_rate`; at the stage's full 250 mm/s that
would be 25 µm per delay sweep, which is unusable.

**How to settle it:** measure the optical resolution directly (edge response
or a resolution target), then pick the smear fraction you will accept.

---

## 11. Delay window actually in use

**Confirmed:** half of the 12.5 ns window, i.e. **6.25 ns of real delay =
50 µs of lab time**. At 10 MS/s that is 500 points per segment.

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

**How to settle it:** feed the laser's 80 MHz (divided) into the 66Zi's
10 MHz reference input and confirm it locks.

---

## 13. ASOPS offset stability

**Assumed:** 10 kHz, stable.

**Why it matters:** the sweep rate sets the pixel clock. Drift changes the
delay-axis calibration and the pixel pitch together.

**How to settle it:** the TRIGTIME array from any Sequence acquisition gives
measured segment-to-segment spacing; its statistics are a direct read on the
offset lock. Worth logging per line.

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
