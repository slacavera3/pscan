# PCIe-6351 on Debian: what is known, what is ruled out

Handover for a session working **on the e-rig itself**, with no access to the
conversation this came from. Everything below is evidence from that rig unless
marked as inference.

Read `libs/xseries_shim/xseries_shim.cpp` and
`libs/xseries_shim/osiUserCode_vfio.cpp` alongside this.

---

## The one open fault

**An analog output does not hold. About 8 seconds after `ao0` is set, the DAC
data register changes on its own to ~0 V, and the output follows.**

Reproduce:

```bash
sudo PSCAN_XSERIES_DEBUG=1 pydaq set 2.5 --backend nixseries --channel 0 --hold
```

Last observed run, in full:

```
[xseries] 0000:82:00.0: runtime PM pinned: control auto -> on
[xseries] runtime status: active
[xseries] shim built Nov 10 2026 15:12:35
[xseries] device reset ok
[xseries] watchdog before: state=3, expired 0 time(s)
[xseries] watchdog after:  state=3 (paused, triggers off)
  Setting ao0 to +2.5000 V...
[xseries] AO0: +2.5000 V, range=0, gain=0 -> DAC code 0x1fb1 (8113)
[xseries] AO0: DAC shadow 0x9fb1 after 2 read(s), wrote 0x1fb1 -> OK

    [   0s] DAC 0x9fb1 (expected 0x9fb1) as written
    [   8s] DAC 0x8002 (expected 0x9fb1) CHANGED  [card active]
      CHInCh signature   0xc0107ad0
      STC3 signature     0x08050501
      AO config bank     0x80   (still configured)
      AO trigger select  0x00000000
      AO FIFO status     0x00000000
      watchdog state     3, expired 0 time(s)
```

A second, probably related fault: **analog input returns `0x0000` on every
channel**, including the chip's internal temperature sensor. The timing engine
runs and the FIFO fills on schedule; every sample is zero. NI's own
`boardBringup` and `aiex1` fail identically.

---

## The card is not faulty

Tested in a Windows 7 machine with NI MAX: self-test passed, **board
temperature 41.3 °C**, and analog output held an arbitrary voltage measured on
a multimeter.

The temperature reading matters most: MAX obtains it by converting the chip's
internal temperature sensor **through the same analog input path**. So the ADC
demonstrably converts a real internal signal correctly on Windows, and returns
`0x0000` on this rig.

Caveat worth keeping in mind: that test changed four variables at once —
machine, OS, driver stack and PCIe slot. It proves the card is good. It does
**not** on its own distinguish "our software" from "this particular PCIe slot".
**The card has never been tried in a different slot of the e-rig.** That is
cheap and still untested.

---

## Ruled out, with the evidence

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Card is faulty | **No** | Works fully under Windows + NI MAX |
| Register access is broken | **No** | CHInCh `0xc0107ad0`, STC3 `0x08050501`, EEPROM calibration readable, scratch-pad walking-ones/zeros passes |
| VFIO cannot reach the card under lockdown | **No** | `dev/check_vfio.py` maps BAR0 and reads correct signatures; lockdown is `[integrity]` |
| Wrong DAC code / bad calibration | **No** | Calibration predicts 8114, shim writes 8113, shadow decodes to +2.4759 V, meter read 2.46 V |
| DAC never latched the value | **No** | `AO_DacShadow` reads back correctly for the first ~8 s |
| STC3 watchdog driving outputs to a safe state | **No** | `state=3` (`WdtSt_Sleeping`), `expired 0 time(s)` — never fired, before or after. It is now paused at open anyway |
| PCI runtime power management suspending the card | **No** | Card reports `active` at the exact moment of the change; `power/control` pinned to `on` |
| Card reset underneath us | **No** | Both signatures intact at the moment of the change |
| AO subsystem reset | **No** | Config bank reads `0x80`, which is what a correct `programConfigBank` produces (offset 0, gain 0, `kImmediate`=0, `kBipolar`=1). Its reset value would be `0xBF` |
| Out timer pushing FIFO data into the DAC | **No** | `AO_FIFO_Status` = 0, `AO_Trigger_Select` = 0 |
| Stale build — the fix was not in what ran | **No** | Shim prints its own `__DATE__ __TIME__`; `xseries_build_id` is a required symbol, so an old `.so` is now refused by name |

---

## Traps already hit — do not re-discover these

- **`AO_Direct_Data` and `AO_DacShadow` use different encodings.** The data
  register takes the DDK scaler's two's complement (`0x0000` = 0 V); the shadow
  reports **offset binary** (`0x8000` = 0 V). A healthy channel reads back the
  written code **XOR `0x8000`**. Comparing them raw reports a mismatch on a
  perfectly good DAC.
- **The shadow lags the write.** Read immediately it returns `0x0000`; correct a
  moment later. Re-read before concluding anything.
- **`aoHelper`'s constructor resets nothing.** `aoHelper::reset()` must be called
  explicitly, once, before programming — `aoex1` does. We now do it at open.
- **`aoex1` finishes in 0.2 s** (20 updates, 10 ms apart), so it never exposes a
  hold-related fault. It is not evidence that holding works.
- **`install.sh --drivers-only` does not touch Python**, and `pscan` installs as
  a copy, not editable — so `git pull` alone never updates what runs. Use a full
  `bash install.sh`.
- **`modinfo` and `lsmod` live in `/sbin`**, not on every user's PATH. A bare
  `modinfo` check silently became "no such module".
- **vfio-pci must be loaded for `driver_override` to do anything.** Those writes
  succeed and change nothing when the module is absent.
- **vfio-pci resets the card when the last fd closes**, zeroing the DACs. An
  output cannot outlive the process; that is deliberate kernel behaviour and is
  not the fault being chased here.

---

## What has not been tried

1. **A different PCIe slot on the e-rig.** Cheapest remaining hardware
   discriminator, and the Windows test does not cover it.
2. **A second PCIe-6351** — one is still sealed in its box.
3. **Ubuntu + NI-DAQmx**, which would replace this entire reverse-engineered
   stack with NI's supported driver. `pscan/daqmx_driver.py` is already written
   against it. Note DAQmx installs out-of-tree kernel modules, so the
   interaction with Secure Boot must be checked — that is the thing to verify,
   not assume.
4. **Reading `dmesg` during the 8-second window.** Never captured.

Inference, not evidence: the MHDDK supplies register definitions and example
sequences, but NI's real driver does considerably more initialisation than the
examples show, and we have no reference for what. An 8-second timer with the
card otherwise healthy smells like a hardware default nothing has overridden.

---

## Tools in this repo

```bash
sudo bash dev/card_report.sh <label>   # everything, into one diffable file
sudo python3 dev/check_vfio.py         # module state, binding, BAR mapping
bash dev/build_ddk_examples.sh         # NI's own examples vs our bus layer
sudo ./boardBringup 130 0              # under libs/vendor/build/.../
```

`PSCAN_XSERIES_DEBUG=1` turns on the shim's instrumentation throughout.
