# First fly-scan: bench protocol

Five stages, each one testable on its own, each one answering a question
the next stage depends on. Do not skip ahead — every stage here exists
because something downstream breaks silently if it is wrong.

Allow half a day for stages 1–3, and expect stage 4 to be the one that
teaches you the most.

---

## Stage 0 — before touching anything

Settle these, because everything downstream scales by them:

- [ ] **`counts_per_mm` for the MLSA02.** Read it out of
      `/home/scan/source/hardware/apt_control` (your wiki says that code is
      hardcoded for BBD302 + MLS203). Do **not** assume pscan's 34304.0 —
      that is the Z8xx DC-servo figure.
- [ ] **Optical spot size**, by edge response or a resolution target. This
      sets your velocity ceiling at `v = spot · f_sweep / 2`.
- [ ] **Maximum segment count.** On the scope, push `SEQ ON,<n>` upward
      until it refuses. Note the ceiling.

---

## Stage 1 — single-shot SNR, no motion at all

**Question: is single-shot good enough, and how does SNR grow with averaging?**

Scope, by hand:
- `SEQ ON,1000`
- Trigger: the 10 kHz ASOPS sync, as you already use
- Channels: C1 only, raw. **No maths averaging.**
- Full 100 µs window for now — you need to see where the signal sits
- Sample rate 10 MS/s

Config (`snr.con`), run with plain `pscan`:

```
action count
count 10
end

action scope
ip 192.168.74.2
channels C1
end
```

That gives 10 acquisitions × 1000 segments = **10,000 single shots**, in
about a minute. Each segment is one shot; Sequence mode does not combine
them.

**Analysis:** average groups of N offline, N = 1, 2, 4 … 1000, and plot SNR
against N. One dataset gives the whole curve.

**What good looks like:**
- SNR vs N follows √N. If it flattens early, you have correlated noise
  (laser drift, pickup), not shot noise — worth chasing before scanning.
- Read off the N you need for usable contrast. That is your `bin` setting.
- Check `_scope0_trigtime.dat`: spacings should be 100 µs with little
  scatter. Gaps at exactly 100 µs mean dropped sweeps.

**Also record:** where in the 100 µs window the signal actually lives. This
decides the capture window for stage 3, and whether a diamond-substrate
echo sits in the half you were planning to throw away.

---

## Stage 2 — velocity calibration, no acquisition

**Question: what raw APT value gives what actual velocity?**

No scope involved. With `FlyStage`:

```python
from flyscan.stage import FlyStage
stage = FlyStage()          # port auto-detected
stage.connect()

cal, points = stage.calibrate_velocity_scale(
    axis='x',
    counts_per_mm=<from stage 0>,
    raw_values=[...],       # a spread spanning your intended range
    start_mm=<safe start>,
    distance_mm=5.0,        # long enough for a clean constant-velocity fit
)
cal.to_json('velocity_cal.json')
```

**What good looks like:**
- `measured_mm_s` rises linearly with `raw`, through the origin.
- `cal.residual_rms` small against the fitted scale. A poor fit means the
  constant-velocity section was too short — lengthen `distance_mm`.
- Per-point `residual_rms_mm` is your **velocity ripple**. Compare it to
  your spot: if ripple is well under a spot over a line length, inferring
  position from time is viable and you may not need position triggers at
  all.

**Keep this number.** It decides whether stage 4 needs the trigger channel.

---

## Stage 3 — one fly line, flat sample

**Question: does the chain hold together and come back aligned?**

Use a featureless region. Pick a conservative velocity — half your ceiling.

Scope, by hand:
- `SEQ ON,<segments>` to match what `--dry-run` reports
- Trigger delay set so the window starts just before the copeak and runs
  through the decay. Confirm via `HORIZ_OFFSET` in the returned WAVEDESC.
- Capture window from what stage 1 told you

```bash
flyscan --dry-run --counts-per-mm <yours> oneline.con
```

Check the reported segment count, time split and data size look sane, then
drive one line directly rather than a whole raster:

```python
plan = stage.prepare_line('x', start, stop, runup_mm, counts_per_mm)
line = stage.fly_line('x', plan, scope, ['C1'], after_capture=None)
```

**What good looks like:**
- Segment count returned equals segment count requested. A short grant
  raises rather than misaligning, so if it runs, this held.
- `trigtime` spacings still 100 µs — motion has not disturbed the trigger.
- Signal amplitude roughly constant along the line. A systematic droop is
  **defocus**, which is your tilt showing up. Note how much.
- `achieved_horiz_interval_s` matches what you asked for.

**If amplitude droops badly over one line, stop here** and sort focus before
going 2D — it will only get worse across the slow axis.

---

## Stage 4 — the sharp edge, both directions

**Question: where are the pixels, really?**

The most informative single test in this protocol. Fly a line across a
sharp, high-contrast edge — a lithographic feature, a cleaved edge, a TEM
grid bar. Fly it **forward and backward**, several times each.

This one test gives you three numbers:

1. **Effective resolution** — the edge-response width, which is your spot
   convolved with the per-sweep smear. Compare it to the spot you measured
   in stage 0. If it is much wider, you are flying too fast.
2. **Position offset** — the edge will land in different places forward
   versus backward. Half that difference is your arm-to-motion latency
   expressed as distance. At 5 mm/s, 1 ms of latency is 5 µm.
3. **Repeatability** — scatter in edge position across repeats of the same
   direction. That is velocity jitter plus trigger jitter.

**What good looks like:**
- Edge width close to the spot. If smear dominates, slow down.
- Forward/backward offset stable run to run. A *stable* offset can be
  calibrated out; a *scattering* one cannot, and means you need the
  position-trigger channel rather than inferred position.
- Repeatability well under a spot.

**This is the test that decides whether you need BBD303 position triggers.**
If offset and scatter are both well inside a spot, inferring position from
`trigtime` and a measured velocity is fine, and you can defer the trigger
channel entirely.

---

## Stage 5 — small raster

**Question: does it work end to end, and does the file read back?**

A deliberately small field — 20 × 20 µm, say — so that focus cannot drift
much and a failure is cheap.

```bash
flyscan --dry-run --counts-per-mm <yours> small.con
```

Then run it. Afterwards:

```python
from flyscan.io_hdf5 import read_map
result = read_map('small.h5')
print(result.summary())
```

**What good looks like:**
- Lines written equals lines requested; `_dropped` empty.
- Forward and reverse lines reconstruct the same features at the same
  positions. Comb or zip artifacts mean the stage-4 offset is not being
  corrected.
- Map looks like the sample.

**Then, and only then**, widen the field until focus droop appears. That
field size is what decides whether you need per-line focus updates, strips,
or the 6351's hardware-timed ramp.

---

## What each stage feeds forward

| Stage | Produces | Decides |
|---|---|---|
| 0 | counts_per_mm, spot, max segments | everything |
| 1 | SNR vs N, signal location in window | `bin`, capture window |
| 2 | velocity scale, ripple | whether position can be inferred |
| 3 | chain works, tilt visible? | whether to sort focus first |
| 4 | resolution, offset, repeatability | whether position triggers are needed |
| 5 | working map, droop field size | focus strategy |

Nothing here needs the autofocus implementation, the BBD303 trigger
configuration, or the PCIe-6351. Those are all decided *by* these results
rather than needed for them.
