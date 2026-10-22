# Fly-scan performance: limits and bottlenecks

What sets scan time, what sets resolution, and which knob actually moves
each. Worked for the rig as specified: 80 MHz rep rate, 10 kHz ASOPS offset,
~5 GHz Brillouin signal, MLSA02 on a BBD303, WaveRunner HRO 66Zi.

`flyscan/budget.py` computes all of this for arbitrary parameters, including
solving backwards from a target. This document is the reasoning behind the
numbers, not a replacement for running it.

---

## The one number that matters

**Time per pixel is ~350 µs, and it does not depend on field size, velocity
or pixel pitch.** Those all cancel. It splits as:

| Term | Expression | Value |
|---|---|---|
| Flight | `1 / f_sweep` | **100 µs** |
| Transfer | `2 · points_per_segment / transfer_rate` | **250 µs** |

So a 1-megapixel map takes about **350 s, under six minutes**. The same map
step-and-settle at ~250 ms/pixel is about **69 hours**. That is the ~700×
the whole exercise buys.

Scan time for any field is just `pixels × 350 µs`. A 500 × 500 µm field at
0.5 µm pitch is 10⁶ pixels; at 1 µm pitch it is 2.5 × 10⁵ pixels and about
90 s.

---

## Why field size and velocity cancel

Pixels per line is `L / pitch`. Flight time per line is `L / v`. With one
ASOPS sweep per pixel, `pitch = v / f_sweep`, so:

```
flight per line   = L / v
pixels per line   = L · f_sweep / v
flight per pixel  = (L/v) / (L·f_sweep/v) = 1 / f_sweep
```

The line length and the velocity divide out. Flying faster gets you a
coarser pitch, not more pixels per second. **The ASOPS sweep rate is the
pixel clock, and nothing else is.**

---

## The invariant that makes transfer stubborn

Points per segment looks like it should depend on the sweep rate, since the
delay window is `1/Δf` long. It does not:

```
stretch            = f_rep / Δf
lab frequency      = F_brillouin / stretch
sample rate needed = n · F_brillouin / stretch        (n points per cycle)
window             = fraction / Δf
points per segment = window · sample_rate
                   = (fraction/Δf) · n · F_brillouin · Δf / f_rep
                   = fraction · n · F_brillouin / f_rep
```

**Δf cancels.** Raising the ASOPS offset shrinks the window but raises the
lab-frame frequency in exact proportion, so the sample rate has to rise to
match and the point count is unchanged.

For this rig: `0.5 × 16 × (5 GHz / 80 MHz) = 0.5 × 16 × 62.5 = 500 points`.

**Consequence: raising Δf cuts flight time but not transfer time.** Since
transfer currently dominates 2.5:1, doubling Δf to 20 kHz would take the
pixel from 350 µs to only 300 µs — a 14% gain for a significant change to
the optical setup. Not worth doing until transfer is fixed.

---

## Bottleneck ranking

### 1. VXI-11 transfer — currently binding, 250 µs/pixel

The only term with real headroom, and three independent multiplicative
levers:

| Lever | From | To | Transfer/pixel |
|---|---|---|---|
| (baseline) | | | 250 µs |
| Window fraction | 1/2 | 1/4 | 125 µs |
| Oversampling `n` | 16 | 8 pts/cycle | 125 µs |
| Transfer rate | 4 MB/s | 8 MB/s | 125 µs |
| All three together | | | **31 µs** |

With all three, transfer drops below flight and the scan becomes
physics-limited at ~130 µs/pixel.

- **Window fraction** is free if the signal has decayed. Capture one
  full-window line with `configure_flat_line()` and look before cutting it —
  a substrate echo off the diamond could easily sit in the discarded half.
- **Oversampling** at 8 points per acoustic cycle is still 4× Nyquist. 16 is
  comfortable rather than necessary.
- **Transfer rate** is the least certain number here. 4 MB/s is an
  assumption, not a measurement. Measure it before optimising against it.
  If it is poor, LeCroy's native VICP beats VXI-11, and the 66Zi runs
  Windows internally so saving locally and pulling over SMB is an option.

### 2. ASOPS sweep rate — the floor, 100 µs/pixel

Once transfer is fixed this is what remains: **10 kpixel/s**, a megapixel in
100 s. Raising Δf is the only way past it, and per the invariant above it
only helps once transfer is already below it.

### 3. Optical resolution — a ceiling on velocity, nothing more

The clean way to think about this: **copeaks are the native samples, and
the output pixel grid is a binning choice made in post.**

```
native sample spacing = v / f_sweep          (also the smear per sweep)
copeaks per pixel     = desired_pitch / spacing
SNR                   ∝ √(copeaks per pixel)
```

So velocity sets how finely the line is sampled, and everything after that
is binning. Slower stage, more copeaks per unit distance, more averaging
available — paid for in line time. That is one trade-off curve, not three.

The only hard limit is where a single copeak smears further than the optics
resolve. Past `v = spot · f_sweep / 2` each copeak integrates over more than
half a resolution element, and no amount of binning recovers what that
destroys:

| Spot | Ceiling @ 10 kHz |
|---|---|
| 0.5 µm | **2.5 mm/s** |
| 1 µm | **5 mm/s** |
| 2 µm | **10 mm/s** |

Below the ceiling it is purely SNR against time. `scan_performance.m` plots
all of this, including the unusable region.

### 4. Stage — never binding

The MLSA02 does ~250 mm/s. At 5 mm/s you are using **2%** of it. Two
consequences worth having: velocity stability should be excellent that far
from the limits, and the run-up is negligible — at 5 mm/s and ~1 g,
`v²/2a ≈ 1.3 µm`.

### 5. Scope memory — never binding

A 500 µm line at 0.5 µm pitch is 1000 segments × 500 points = 500 kpts per
channel, ~1 MB. Even modest Zi-series memory swallows that. The thing to
confirm is the **maximum segment count**, not the memory: 1000+ segments per
line needs checking against your model. `configure_line()` reads the granted
count back and refuses a short grant rather than misaligning the line.

### 6. Laser rep rate — sets resolution, not rate

80 MHz gives 8000 pulses per sweep and 1.5625 ps of delay resolution. For a
5 GHz signal (200 ps period) that is 128 points per acoustic cycle — far
more than needed, which is why the sample rate can sit well below the 80 MS/s
information ceiling.

---

## Averaging

Pixel time scales linearly with sweeps per pixel, in both terms: M sweeps is
M segments, so M× the flight *and* M× the data. SNR improves as √M, so
**doubling SNR costs 4× the scan time**. There is no free averaging here —
the diamond substrate is what buys single-sweep SNR, and that is the point
of it.

Binning in post is equivalent: flying slower so that M sweeps land in one
pixel costs exactly the same time as flying fast and averaging M pixels.

---

## Focus, as a rate limit

Depth of focus is about `λ / 2·NA²` — roughly **310 nm** at 400 nm, NA 0.8.
Sample tilt of 1 mrad over 500 µm is 500 nm, so over a field this size focus
*must* track or signal strength falls with it.

- Per-line focus updates (Phidget-speed) handle the slow-axis component.
- The fast-axis component needs either intra-line ramping — which the
  PCIe-6351's hardware-timed AO does properly — or splitting the field into
  strips short enough that `tilt × strip_length < DOF/2`.
- Strips cost retrace time but no extra data. At 1 mrad and a 312 nm DOF,
  keeping within DOF/2 gives a strip width of about 156 µm.
- Open-loop piezo hysteresis is 10–15% **of the excursion**. Measure the
  focus excursion your field actually needs before assuming a fitted surface
  can be applied open-loop.

---

## Worked example

500 × 500 µm, 1 µm spot, properly sampled:

```
spot                1 µm       ->  d = 0.5 µm, v = 5 mm/s
pitch               0.5 µm     ->  1000 px/line, 1000 lines, 10^6 px
window              50 µs      ->  500 points/segment at 10 MS/s
line flight         100 ms
line data           1000 x 500 x 2 = 1 MB  (+1% for DC and trigger)
line transfer       ~250 ms at 4 MB/s
time per line       ~350 ms
total               ~350 s, under 6 minutes
```

Same field, after fixing transfer (quarter window, 8 pts/cycle, 8 MB/s):

```
time per pixel      ~131 µs    ->  total ~131 s
```

Same field, step-and-settle at 250 ms/pixel: **~69 hours**.

---

## Assumptions to retire

| Assumption | Current value | How to settle it |
|---|---|---|
| VXI-11 transfer rate | 4 MB/s | time one `download_line()` against `line_budget()` |
| Optical spot | 1 µm | edge response or resolution target |
| Sample tilt | 1 mrad | falls out of the 3×3 focus map |
| Depth of focus | 310 nm | from measured NA and wavelength |
| Max segments | assumed ≥1000 | push `SEQ ON,<n>` until refused |
| Signal decay | fits in half the window | one full-window flat capture |
| Hardware-timed AO on Linux | unknown | comedi `ni_pcimio` on the 6351 |

The first and last are the ones that would change decisions.
