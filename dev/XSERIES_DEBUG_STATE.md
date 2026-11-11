# PCIe-6351 on Debian: what is known, what is ruled out

---

## RESOLVED — the card works, via NI-DAQmx

**11 Nov 2026.** The PCIe-6351 is fully working on Debian 13 under NI's own
driver. Both analog directions verified:

```
self_test_device()            PASSED
one sample, software timed    -0.000184 V
1000 samples at 10 kHz        span 0.001932 V, varying
ao0 set to +2.5 V             holds indefinitely
```

Two things were in the way, and neither was the card:

1. **NI-DAQmx was never tried.** `install_nidaqmx()` returned early on Debian
   unless `PSCAN_FORCE_DAQMX=1`, and that flag had never been pulled. "Not
   officially supported" was treated as "will not work". NI ship `.deb`s, DKMS
   builds their modules against whatever kernel is present, and the feed can
   be pinned so it cannot touch Debian packages. It works.

2. **The IOMMU was translating the card's DMA.** Debian builds with
   `CONFIG_INTEL_IOMMU_DEFAULT_ON`, so this is a stock install's behaviour,
   not something pscan enabled. NI's kernel driver does not use the kernel DMA
   API the way that expects, so every buffered acquisition failed with
   `-50152 A hardware failure has occurred` while single samples, self test
   and analog output all worked — only buffered acquisition needs DMA.
   `iommu=pt` fixes it, and `install.sh` now sets it.

**The shim below is the fallback**, for machines that cannot run NI-DAQmx. Its
faults were never explained, and the record of that investigation is kept
because it would otherwise have to be repeated. Note it was never retried with
`iommu=pt`, though VFIO uses the IOMMU deliberately so the connection is not
obvious.

---

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
| Kernel sees a PCIe error / link event / reset at the 8 s mark | **No** | `dmesg -T` across the window shows **nothing** at that time. The only two lines are at open (below) |

### What dmesg does say, at open and only at open

```
vfio-pci 0000:82:00.0: enabling device (0000 -> 0002)
pcieport 0000:80:1c.0: unlocked secondary bus reset via: __pci_reset_function_locked+0x3e/0x70
```

Two things worth knowing from that:

- `0000 -> 0002` is the PCI command register: **memory space enabled, bus
  master not**. Fine for this driver, which is programmed I/O only and
  implements no DMA.
- **`VFIO_DEVICE_RESET` is not doing a function-level reset.** This card has no
  FLR, so VFIO falls back to a **secondary bus reset**, which resets the whole
  bus segment below the port. That is far heavier than intended, it is ours,
  and NI's driver does nothing like it on open. See `PSCAN_XSERIES_NO_RESET`
  below.

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
4. **Opening the card WITHOUT the secondary bus reset.**

   ```bash
   sudo PSCAN_XSERIES_DEBUG=1 PSCAN_XSERIES_NO_RESET=1 \
        pydaq set 2.5 --backend nixseries --channel 0 --hold
   ```

   Caveat on interpreting it: vfio-pci resets the device when the last file
   descriptor closes, so the card has usually just been bus-reset by the
   *previous* run anyway. A genuinely untouched card means trying this first
   thing after a reboot.

5. **Reading NI's own `osiUserCode.cpp`** — the nirlpk version, backed up by
   `dev/build_ddk_examples.sh` to
   `libs/vendor/build/.../LinuxKernel/osiUserCode.cpp.nirlpk`, plus the
   `nirlpk/` kernel module source beside it. That is NI's real board-access
   layer for Linux and the only reference we have for what their driver does
   at open that ours does not. It has never been read. It cannot be committed
   here — the repo is public and the DDK is not redistributable.

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
