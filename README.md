================================================================================
                      PSCAN GITHUB INSTALLATION CHEATSHEET
================================================================================
Target Repository: https://github.com/slacavera3/pscan.git

Requirements: 
- Git installed (`git --version`)
- Python 3 installed (`python --version` or `python3 --version`)
- Pip installed. If missing:
    * Linux (Ubuntu/Debian): `sudo apt update && sudo apt install python3-pip`
    * Windows PowerShell:    `python -m ensurepip --upgrade`

--------------------------------------------------------------------------------
1. INSTALLATION & UPDATING (LINUX)
--------------------------------------------------------------------------------
One build, one installer, one set of commands, no virtualenv. `install.sh`
does everything: apt C-drivers (Comedi, libusb), libphidget21 from the bundled
tarball, the Tucsen SDK, NI-DAQmx where the OS supports it, the Python
dependencies, and pscan itself. It then verifies the install and refuses to
claim success if the result is unusable.

# Step 1: Download the source code
git clone https://github.com/slacavera3/pscan.git
cd pscan

# Step 2: Run the installer
bash install.sh

# TO UPDATE LATER: pull and re-run. Every step is idempotent.
cd /path/to/pscan
git pull
bash install.sh

That gives you five commands:
  pscan | pystage | pypiezo | pydaq | flyscan

If they come back "command not found", ~/.local/bin may not be on your PATH:
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
  source ~/.bashrc

USEFUL FLAGS

  bash install.sh --drivers-only
      Just the system drivers, stopping before anything Python. This is the
      half worth re-running after a kernel upgrade, when NI-DAQmx's kernel
      modules need rebuilding but nothing in Python has changed.

  PSCAN_SKIP_DAQMX=1 bash install.sh
      Never install NI-DAQmx.

  PSCAN_FORCE_DAQMX=1 bash install.sh
      Install NI-DAQmx even when no NI card is seated, or when the OS is not
      one NI supports. See the Debian warning under DAQ CARDS AND BACKENDS.

PROPRIETARY SDKs: THE libs/vendor/ DROP-BOX

One driver pscan uses cannot be committed here: Tucsen's camera SDK is free
to download but licensed in a way that does not permit this public repository
to redistribute it.

So install.sh asks. When one is missing it finishes everything that does not
depend on it, then prints a single block naming every outstanding download,
its URL, and the exact filename to save:

  =======================================================
   MANUAL STEP: 2 PROPRIETARY DOWNLOAD(S) NEEDED
  =======================================================
    [1] Tucsen Dhyana camera SDK
        ...
Drop the files in libs/vendor/ and re-run `bash install.sh`. That directory is
gitignored, so nothing you put there can be committed.

They are reported together, at the end, on purpose: one at a time would mean
download, re-run, next request, download again - a drip-feed that makes a
setup feel endless. Everything not blocked by a missing SDK installs anyway;
only the hardware that SDK drives is unavailable.

Note: an older checkout may still carry the Tucsen SDK committed under
libs/TUCamSample_ubuntu22.04_*/. install.sh still finds and uses it, so
nothing breaks, but it should come out of version control:
  git rm -r --cached libs/TUCamSample_ubuntu22.04_*
and then move it to libs/vendor/ locally.

WHY THERE IS NO LONGER A SEPARATE DEV BUILD

There used to be a `pscan-lab-dev` package, a `pscan-dev` command set and an
`install-dev.sh` that put it in its own virtualenv, so an experimental build
could sit beside a stable one. In practice that produced a different failure
mode for every combination of build, environment and partially-completed
install - stale venvs holding old code, shadowed modules, drivers installed
for one build and not the other - and diagnosing which combination you were
in cost more than the isolation ever saved. There is now one of everything.

install.sh removes the old arrangement when it finds it: the `pscan-lab-dev`
package, ~/.local/share/pscan-dev, and the -dev command symlinks.
--------------------------------------------------------------------------------
2. GLOBAL INSTALLATION & UPDATING (WINDOWS)
--------------------------------------------------------------------------------
Windows does not require the Linux Comedi C-drivers and cannot run the bash 
script natively. Execute these commands from an Administrator PowerShell.

# Fresh Install directly from GitHub:
pip install git+https://github.com/slacavera3/pscan.git

# Local Developer Install (if you cloned the code):
cd C:\path\to\psource
pip install .

# TO UPDATE LATER:
# Using --no-cache-dir is critical to ensure changes are actually applied!
pip install --force-reinstall --no-deps --no-cache-dir git+https://github.com/slacavera3/pscan.git

--------------------------------------------------------------------------------
3. VIRTUAL ENVIRONMENTS (VENV)
--------------------------------------------------------------------------------
Use this to isolate pscan from your global Python packages. 

# LINUX
# Ensure C-drivers exist globally first: sudo apt install -y libcomedi0 libcomedi-dev
mkdir my-pscan-workspace
cd my-pscan-workspace
python3 -m venv venv
source venv/bin/activate
pip install git+https://github.com/slacavera3/pscan.git
# To exit: deactivate

# WINDOWS POWERSHELL
mkdir my-pscan-workspace
cd my-pscan-workspace
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install git+https://github.com/slacavera3/pscan.git
# To exit: deactivate

--------------------------------------------------------------------------------
APPENDIX: SYSTEM-WIDE VS. VIRTUAL ENVIRONMENT
--------------------------------------------------------------------------------
SYSTEM-WIDE INSTALLATIONS:
* What it does: Installs the software globally across your OS. The tool is 
  accessible from anywhere in your terminal without needing setup commands.
* When to use: Use this for standalone CLI tools that you want to access 
  universally, just like native commands (e.g., `ls` or `ping`). 
* The downside: Dependency conflicts. If pscan relies on Version 1.0 of a 
  library, and another tool requires Version 2.0, one will break.

VENV (VIRTUAL ENVIRONMENT) INSTALLATIONS:
* What it does: Creates an isolated sandbox directory. 
* When to use: Use this for actively developing Python code or when a tool 
  has highly specific dependencies that would break your global setup. 
* The downside: You have to run the `activate` command every single time 
  you open a new terminal window before using the tool.
================================================================================


### Usage

Once installed, the package exposes two global terminal commands that can be run from any directory.

1. Manual Stage Control (pystage)
To manually jog the Thorlabs stage, enable/disable axes, or define your absolute focus coordinates, launch the Precision Control CLI:
   pystage

Controls:

   Arrow Keys   jog the axes by the current step size
   J            set the step size (mm)
   G            go to an absolute X/Y target
   C            go to the geometric centre of travel
   1 / 2        toggle X / Y motor power
   R            re-read positions, zero and travel from the controller
   D            dump the raw stage-axis parameters (diagnostics)
   K            calibrate counts-per-mm for this stage and controller
   H            home the stages and clear safety lockouts
   Q            quit

H and C do two different jobs, in that order:

* **H** trips the limit switches. The controller drives each axis until it
  reaches its switch and then *defines that point as encoder zero*. pystage
  waits for the per-axis `MOVE_HOMED` confirmation and names any axis that
  did not answer, rather than sleeping a fixed time and assuming.
* **C** drives to the **geometric centre of travel** - the midpoint of the
  span the controller reports, not half the travel from zero, and not (0,0).

The distinction matters because it is what makes C **invariant to the
coordinate frame**. Where zero lands is stored in the controller's own flash
(home direction, limit switch, offset distance; APT `SET_HOMEPARAMS`), nothing
in pscan writes it, and it is normally set once in Kinesis or APT User. **That
is why a BBD20x and a BBD30x on the same bench can disagree: stored controller
state, not a model pscan knows about.** A stage homed to its reverse limit
reports its travel as `0..110 mm`; the same stage with a 55 mm home offset
reports `-55..55 mm`. Same glass, two frames. The midpoint is the middle of
the glass in both; half-travel-from-zero is only right in the first.

The travel span comes from APT `STAGEAXISPARAMS` (`0x04F1`/`0x04F2`), read
only - pystage never writes controller parameters. The panel prints the span
and the centre it will aim for on the `Travel X:` / `Travel Y:` lines, so what
C is about to do is visible before you press it.

**C commands encoder counts, not millimetres.** A midpoint needs no scale:
`(min + max) / 2` is exact whatever counts-per-mm is, or is not. This matters
on a real rig - a BBD303 driving an MLS203 reports `cnts_per_unit = 20000`
(50 nm), while pscan hardcodes 34304 (the Z8xx/TDC001 figure). Going through
millimetres with the wrong scale would land the stage 1.7x off; going through
counts lands it on the centre. The millimetre figures on screen are still
wrong in that case, and the panel says so with a `(!)` warning rather than
refusing to work.

C **refuses rather than guessing** when it cannot be sure, and says which axis
and why:

* the axis is not homed, so there is no coordinate frame to be the centre of
  (press H);
* the controller did not report its travel, or did not report whether it is
  homed;
* `cnts_per_unit` is not a plausible encoder resolution, which means the
  `0x04F2` field offsets are wrong for that controller. The layout is
  transcribed from two third-party libraries rather than Thorlabs' manual, so
  this is the check that stands between a mis-parse and a bad absolute move;
* **the current position lies outside the reported limits.** This is the
  important one. It tests whether those limits are expressed in the same
  frame as the position counter - if they are stage descriptors in some other
  frame, the stage sits outside them and their midpoint is not the centre but
  a command into a limit.

A `cnts_per_unit` that is plausible but merely *disagrees* with pscan's
constant is not a refusal. That is a display bug, not a travel bug, and the
two are reported separately.

C also acts only on *enabled* axes and names any it skipped, so a one-axis
move is never reported as having centred both.

**D** dumps every decoded `0x04F2` field raw - resolution, limits, the
position counter, the computed midpoint and whether C would accept it. That is
the first thing to run when C refuses on a controller you have not used before.

#### Counts per mm, and calibrating it

counts-per-mm belongs to the stage and its controller, not to pscan. pscan
hardcoded **34304**, which is the Z8xx/TDC001 DC-servo figure; a BBD303
driving an MLS203 reports **20000** (50 nm). A wrong value rescales every
position silently - the data look perfectly healthy and the microns are wrong.

`pscan/calibrate.py` holds the **one resolver the whole codebase goes
through** — the scan path, the jog tool and flyscan — so they cannot drift
apart, which they had: `main.py` carried its own literal while flyscan refused
to default at all. The order is:

1. a stored measurement for this controller serial + stage part + axis;
2. the controller's own `cnts_per_unit` from `0x04F2`;
3. `KNOWN_COUNTS_PER_MM`, pscan's per-part-number catalogue (`MLS203` →
   20000). This belongs in a table because it is a property of the stage
   model's encoder, not of an individual unit — every MLS203 has the same one,
   so a new rig inherits it with nothing to do.

**There is no fallback for anything that records data.** `require_counts_per_mm()`
raises rather than guessing, and `pscan` refuses to start a scan it cannot
scale. A run that refuses is recoverable; a drawer of maps with the wrong
pitch is not. A controller whose `part_no` and `cnts_per_unit` contradict each
other is refused too — both come from the same record, so it disagrees with
itself, and picking a side silently is exactly how 34304 survived. A stored
measurement settles it, because that is a deliberate human answer.

Only pystage's jog UI keeps the old 34304 as a last resort, announced as
`UNCALIBRATED fallback`, because jogging records nothing. It prints which
source it used on the `Scale X:` / `Scale Y:` lines either way — a number
whose provenance nobody can see is how this lasted.

Calibrate with `pystage --calibrate`, or **K** from inside the UI:

```
pystage --calibrate              # controller figure, plus an optional
                                 # human-referenced measurement
pystage --calibrate --span       # also travel the full reported range
```

Three sources, because software alone cannot measure a length - there is no
reference inside the loop:

| method | needs | moves the stage | a measurement? |
|---|---|---|---|
| `controller` | nothing; reads `cnts_per_unit` | no | no, a declaration |
| `span` | the stage's datasheet travel | across full travel | no, trades one constant for another |
| `manual` | your own micrometer or target reading | one known jog | **yes** |

They are worth having together because they cross-check: if
`span_counts / cnts_per_unit` comes out at the datasheet travel, the
controller's figure is independently corroborated. Disagreements are reported
rather than resolved silently, and the best evidence wins - a measurement over
a derivation over a claim.

The manual step commands a move in **counts**, not millimetres, so setting up
the measurement does not depend on the answer being measured. Records are
keyed by controller serial, stage part number and axis, so a figure measured
on one rig is never silently reused on another - which is the failure that
produced the 34304 problem in the first place.

Stored in `~/.config/pscan/stage_calibration.json`, with
`/etc/pscan/stage_calibration.json` as a machine-wide default the user file
overrides.

`install.sh` does **not** calibrate by default - it only reports whether a
calibration exists. An installer that drives hardware to its limits unasked,
possibly with a sample mounted, is a bad trade for the convenience. Opt in
with `bash install.sh --calibrate`.

1b. DAQ Card Inspection (pydaq)
This is the bring-up tool for a new card. Before writing a .con file against
one, use it to confirm pscan sees the card, under which backend, with which
channels and ranges - and to check there is actually a signal on the input.

   pydaq list                              what pscan can see, and what it can't
   pydaq list --all                        also the Phidget, which auto-detect skips
   pydaq read --channel 0                  acquire a trace, print its statistics
   pydaq read --channel 0 --v-min -1 --v-max 1
   pydaq set 1.5 --channel 0               hold an output at 1.5 V
   pydaq set +0.1 --channel 0              step it relative to where it is

`list` prints the exact `backend` and `device` lines to paste into a .con
block, so pinning a config to a specific card is copy-and-paste. Without
`--all` it never opens the Phidget board, for the reason given under DAQ CARDS
AND BACKENDS below.

`read` writes its trace in the same float32-volts format a scan's .d file
uses, through the same acquisition path, so it tests the real code and not a
shortcut around it.

2. Automated Scanning (pscan)
To run a fully automated acquisition grid, pass your configuration .con file to the orchestrator:
   pscan path/to/your_config.con

Flags:
   --dry-run   Validate the config and print exactly what would happen - when
               each action runs, the full coordinate grid, the total trace
               count and every output file (flagging any that already exist) -
               then exit WITHOUT opening any hardware. Run this before any
               long scan.
   --timer     Print per-module and per-trace timings during the run.

### WHEN EACH ACTION RUNS (read this before writing a .con file)

**An `action ... end` block is a for-loop, and what you write inside it runs
once per iteration. So the nesting in the file IS the nesting of the scan.**

```
action apt_stage          <- for each X position
...
    action apt_stage      <-     for each Y position
    ...
        action count      <-         take 4 traces
        count 4
            action a2d    <-             record one
            ...
            end
        end
    end
end
```

Write new files this way. `examples/nested_loops.con` is the worked example
and states the rules; the short version is that only the axis blocks and
`count` are loops, a loop must contain something, and every recording block
must sit at the same depth so that every file from one run has the same trace
count.

Where you put `count` is a different EXPERIMENT, not a different layout:
inside both axes it averages a point, around the inner axis it repeats a row,
around both it repeats the whole image so the passes can be compared for
drift. That last one cannot be said at all with a single global number.

WHAT A FLAT FILE DOES, FOR THE ONES ALREADY WRITTEN

A config that nests nothing still runs exactly as it always has, and there is
nothing to migrate. Its shape is inferred instead of written: axes nest by
axis id, lowest outermost, `count` innermost, and the three pipelines are
split positionally. A block runs

   * at SETUP (once, before anything moves)  if NO core block precedes it
   * at TEARDOWN (once, after the scan)      if NO core block follows it
   * PER COORDINATE (once per grid point)    otherwise

where the core blocks are: apt_stage, d2a_phidget, d2a, count, scope,
picoscope, a2d, webcam, delay, ixon, tucsen. In practice only `action script`
is ever non-core, so in a flat file whether your script runs once or
thousands of times depends on where it sits relative to the core blocks
around it - which is the part that catches people out, and the main reason to
nest instead and say what you mean.

Either way, check it with `pscan --dry-run`: it prints the pipelines, the
grid and every output file, and says explicitly whether the config nested or
was read positionally.

### DAQ CARDS AND BACKENDS

`action a2d` grabs DC photovoltage off a detector; `action d2a` applies an
analog voltage to something. Both reach hardware through one of four
backends, one driver file each, and pscan picks for you unless you say
otherwise:

  comedi    pscan/a2d_driver.py        (NIDriver)
            The incumbent. libcomedi, on a /dev/comedi* node. Covers the NI E
            and M series (PCI-625x/628x, PCIe-6251/6259) and the cards already
            in service. Analog in and out.

  daqmx     pscan/daqmx_driver.py      (DaqmxDriver)
            NI-DAQmx, NI's own supported driver. The way to reach an NI
            X Series card: comedi's ni_pcimio has no X Series support at
            all, so a PCIe-6351 is invisible to it and always will be. Also
            drives the M Series cards comedi handles, so a PCIe-6251 is
            reachable either way. Analog in and out.

            WORKS ON DEBIAN, despite NI supporting only Ubuntu, RHEL and
            openSUSE. Their packages are .debs and DKMS builds the kernel
            modules against whatever kernel is present, so install.sh
            installs them with PSCAN_FORCE_DAQMX=1, pinning NI's apt feed so
            it cannot replace Debian packages. Verified on Debian 13 with a
            PCIe-6351: self test passes, analog output holds, and 1000
            samples at 10 kHz return a proper noise floor.

            Two things it needs, both handled by install.sh:

              iommu=pt   Debian enables the IOMMU by default and NI's driver
                         does not use the kernel DMA API the way address
                         translation expects. Without passthrough, buffered
                         acquisition fails with "-50152 A hardware failure
                         has occurred" while single samples, self test and
                         analog output all work - only buffered acquisition
                         uses DMA. install.sh adds it to the kernel command
                         line, backing up /etc/default/grub first.

              MOK        With Secure Boot on, NI's out-of-tree modules must
                         be signed. DKMS signs them; the key is enrolled
                         once, at the next boot, on a blue screen. That step
                         cannot be automated - enrolling a key has to
                         involve someone physically present, which is what
                         Secure Boot is for.

  phidget   pscan/d2aphidget_driver.py (D2APhidgetDriver)
            The Phidget analog board that has always driven the piezo.
            Outputs only, channel 0 only; never auto-detected (see below).

pscan/daq.py holds what none of them should repeat: the acquisition contract,
the output format, range resolution, and the choice of which card a block
means. A driver never touches the output file - it returns volts, and daq.py
packs them. That is the only place the .d format is decided, which is what
keeps a DAQmx trace and a comedi trace byte-identical in layout.

HOW A CARD IS CHOSEN

Omit `backend` and `device` and pscan enumerates what is present:

  * exactly one card can serve the block  -> it is used, and the card and
    driver are printed in the run's log
  * more than one could               -> the run STOPS, lists the candidates,
                                         and prints the lines to paste in
  * none                              -> the run stops and says which backends
                                         are unusable, and why

The middle case is deliberate. Quietly choosing the wrong card does not raise
an error on an instrument - it produces a complete, plausible-looking set of
.d files containing the wrong signal, and nothing downstream can tell. One
extra line in a config is a much better outcome than a scan you have to
repeat. Pin a block with:

  backend daqmx
  device Dev1

Device names differ by backend: comedi uses device nodes (/dev/comedi0),
DAQmx uses the names NI assigns (Dev1). Giving `device` alone is enough -
the backend is inferred from the form of the address.

The Phidget board is never auto-detected: opening it claims the board, and it
may be holding a piezo at a position right now, which is not something to
disturb while merely answering "what hardware is here?". Name it explicitly if
you mean it - which `action d2a_phidget` does for you.

RANGES: INDEX VS VOLTS

comedi takes a range *index* into the card's own list. DAQmx has no concept of
an index and wants the volts. So `range 0` means whatever the first entry in
that particular card's list happens to be, which is not portable.

Prefer saying the volts you need:

  v_min -1.0
  v_max 1.0

pscan then picks the NARROWEST range the card offers that contains your
request - a tighter range is more resolution - and fails loudly if nothing
fits rather than quietly widening it. One `v_min`/`v_max` pair means the same
thing on every card and every backend. Giving only one side mirrors it, so
`v_max 2` means -2..2. `range <index>` is still accepted and unchanged.

INSTALLING NI-DAQMX (Linux)

READ THIS FIRST IF YOU ARE ON DEBIAN. NI supports Ubuntu, RHEL and openSUSE.
It does NOT support Debian, and ships no Debian registration package - there
is nothing for an installer to install. On Debian, install.sh reports that and
skips NI-DAQmx, which means an NI X Series card (PCIe-635x/636x) cannot be
reached on that machine at all. comedi cards are unaffected.

The supported fix is Ubuntu 22.04 or 24.04 on the machine holding the card.
`PSCAN_FORCE_DAQMX=1` will try the Ubuntu 22.04 packages on Debian anyway, but
understand what that does: it adds an apt feed targeting Ubuntu jammy to a
Debian system, and apt can later try to satisfy Debian packages from it. Pin
the feed to NI packages only if you go that way, and be ready to remove
/etc/apt/sources.list.d/ni-*.list. There is also no guarantee NI's DKMS
modules build against Debian's kernel.

On a supported OS, `bash install.sh` does it. There is nothing to download by
hand.

That is worth spelling out, because the obvious command does not work:
`ni-daqmx` is NOT in Ubuntu's archives, so `sudo apt install ni-daqmx` fails
with "Unable to locate package" on a clean machine. NI ships a small (~31 KB)
registration .deb that adds their apt feed, and the driver itself comes from
that feed. install.sh does both: it finds the newest NI release, takes the
registration package for your Ubuntu version out of it, installs the driver,
and builds the kernel modules.

It will not pull a large vendor driver onto a machine with no NI card in it.
If `lspci` shows no National Instruments device it says so and skips, so a
comedi-only rig is unaffected. Three env vars steer it:

  PSCAN_SKIP_DAQMX=1    never install it
  PSCAN_FORCE_DAQMX=1   install even with no card currently seated
  NI_RELEASE=2025Q1     pin which release's registration package is used

A REBOOT IS REQUIRED afterwards - the kernel modules are not loaded until
then, and no card is visible however well-seated it is. install.sh says so at
the end when it installed the driver that run.

After rebooting, `sudo dkms status` should list ni-daqmx against your running
kernel. If it does not, the modules did not build and that is the thing to fix
before looking at anything else.

WHAT IS SUPPORTED ON LINUX

PCIe and PXI DAQ cards are the supported category - a PCIe-6351 is in scope.
USB DAQ devices largely are NOT supported on Linux Desktop (USB cDAQ chassis
excepted), so do not assume a USB device will work just because a PCIe one
does. Check the specific model against NI's compatibility page.

NI also published a security notice affecting driver software 2026 Q2 and
earlier - see ni.com/ni-pal-update before settling on a release.

Two further things that bite on Linux:

  * The kernel modules must be rebuilt after every kernel upgrade. A card that
    worked yesterday and is invisible today is almost always this - check
    `sudo dkms status`.
  * X Series PCIe boards have been reported throwing DMA faults on kernels
    newer than an LTS release. The fix is the `iommu=pt` kernel parameter. If
    `pydaq list` sees the card but `pydaq read` fails with a hardware
    error, try that before anything else.

### OSCILLOSCOPES: THE LECROY AND THE PICOSCOPE

Two instruments, two blocks, one output format.

  scope       pscan/lecroy_driver.py     (LeCroyScope)
              A Waverunner on the lab network, over VXI-11. It is a benchtop
              instrument with a front panel, so `action scope` names an IP
              and a few channels and otherwise reads the scope's own settings
              back out of the waveform it returns.

  picoscope   pscan/picoscope_driver.py  (PicoScope)
              A USB PicoScope, over Pico's own C libraries with ctypes - the
              same way the piezo is driven through libphidget21, so there is
              no Python package to install and nothing to keep in version
              step. It has no screen and no knobs, so everything the LeCroy
              is asked to report has to be SAID in the block instead: range,
              coupling, sample rate, record length, trigger. That is the
              whole difference between the two blocks.

  pscan/scope_io.py holds what neither should repeat: the .dat layout, the
  .m sidecar and the background writer. A driver produces ADC counts and
  says what they mean; it never opens the output file. That is what keeps a
  PicoScope trace and a LeCroy trace byte-compatible, and it is the only
  place the scope format is decided.

The two write separate files, so a run may use both:

  <base>_scope0.dat / .m     the LeCroy
  <base>_pico0.dat  / .m     the PicoScope

Both are int16 ADC counts, one row per (segment, channel), with the volts
scaling in the .m: `volts = scp.vgain(ch) * counts - scp.voff(ch)`. A MATLAB
script that reads one reads the other unchanged.

WHICH SERIES ARE SUPPORTED

The 2000A, 3000A, 4000A and 5000A families - libps2000a, libps3000a,
libps4000a, libps5000a. Their block-mode APIs differ only in a handful of
places, which the SERIES table in the driver names.

The 2000 and 3000 series WITHOUT an 'A' in the model number are NOT
supported: libps2000 and libps3000 are a different API, not a variation on
this one. So a 2206A works and a 2205 does not. The driver says so by name
when it cannot find a unit, rather than reporting a missing cable.

HOW A UNIT IS CHOSEN

The same rule pscan.daq applies to DAQ cards, for the same reason:

  * exactly one unit attached     -> it is used, and named in the run's log
  * more than one                 -> the run STOPS, lists them, and prints
                                     the lines to paste in
  * none                          -> the run stops and says which libraries
                                     were missing

Pin a unit with either or both of:

  series ps5000a
  serial AB123/456

INSTALLING THE DRIVERS (Linux)

`bash install.sh` does it: it adds Pico's apt repository with their current
signing key, installs the four libps*a packages, and registers
/opt/picoscope/lib with the dynamic linker if nothing else has - without that
last step ctypes cannot find a library that is sitting right there.

It will not pull a vendor driver onto a machine with no PicoScope attached.
If no USB device with Pico's vendor id (0ce9) is present it says so and
skips. Two env vars steer it:

  PSCAN_SKIP_PICO=1     never install them
  PSCAN_FORCE_PICO=1    install even with nothing plugged in

Pico officially support Ubuntu and openSUSE Leap on x86-64. On Debian the
feed may simply have no matching packages, in which case `action picoscope`
is unavailable and nothing else is affected.

If the package creates a `pico` group, install.sh adds you to it and says to
log out and back in. Until you do, the scope is only reachable as root.

On Windows, install PicoSDK or the PicoScope application from picotech.com;
the driver finds the DLLs under C:\Program Files\Pico Technology.

CAPTURES vs AVERAGES

Both take more than one capture per trace; what differs is what reaches the
file.

  captures 10     ten rapid-block segments, all ten WRITTEN. scp.multitraces
                  becomes 10, exactly as LeCroy Sequence mode does.
  averages 100    one hundred segments averaged in software into each row
                  written. scp.n_averages records 100.

They compose: `captures 10` with `averages 100` captures 1000 segments and
writes 10 rows, each the mean of 100. Averaging happens in ADC counts, so
the single scaling the .m declares still maps the file to volts. 'sweeps' is
accepted as a synonym for 'averages', because that is what the `action
scope` block calls the same idea.

The ceiling is the unit's capture memory split that many ways, which the
driver asks about and reports as a refusal naming the numbers - it will not
quietly shorten the record.

THINGS WORTH KNOWING BEFORE A LONG RUN

* THERE IS NO TRIGTIME SIDECAR. Block mode reports each segment's sub-sample
  trigger offset but not the time between triggers, and half of a
  (trigger_time, trigger_offset) pair is worse than none - a loader would
  read the missing half as zero. So `scp.trigtimename` is empty and
  `scp.trigtime_rows_per_trace` is 0, as it is for a LeCroy single-shot.

* `sample_rate` is a REQUEST. PicoScope timebases are a discrete ladder whose
  rungs depend on how many channels are on, so the driver asks the unit and
  takes the slowest rung that is still at least as fast as asked. The rate
  actually used is printed when the scope is armed and recorded in
  `scp.hint`. Asking for more than the unit can do warns and uses its
  fastest.

* `v_max` is the full scale in volts, and pscan picks the narrowest Pico
  range that contains it - a tighter range is more resolution. Asking for
  more than the hardware has fails loudly rather than being quietly widened.
  Clipping is reported as a warning naming the channels, because a clipped
  trace is a complete, plausible-looking file with flat peaks.

* WITH NO `trigger` LINE every capture starts the instant pscan asks for it.
  That is right for a DC measurement and wrong for anything pulsed, so the
  config parser warns when the line is missing. Write `trigger none` to say
  you meant it.

* A trigger that never fires is a DROPPED TRACE, not a hang: after `timeout`
  seconds the trace is padded, the index is recorded in `<base>_dropped.txt`,
  and the scan carries on. Ten in a row aborts the run.

* ONE picoscope block per config. A second one would write the same
  <base>_pico0.dat and interleave its traces into the first one's, so it is
  refused. Put every channel in one block - `channels A, B, C` - with its
  own `v_max` and `coupling` in the same order.

FLY SCANS USE IT TOO

`flyscan` takes a PicoScope as well, through the same `action scope_line`
block that names a LeCroy - and through the same hardware binding, since
flyscan/scope.py's FlyPicoScope is built on pscan/picoscope_driver.py's
ctypes layer rather than on a second copy of it. One binding per instrument,
two acquisition policies on top.

Which instrument a block means is decided by how it is ADDRESSED, because
that is the one thing the two cannot share:

  action scope_line
  ip 192.168.1.100          <- a LeCroy, over the network
  ...

  action scope_line
  usb                       <- a PicoScope, over USB
  series ps5000a            <- optional, as is 'serial'
  channels A, B, C
  roles ac, dc, trigger
  sparsing 1, 100, 20
  v_max 0.2, 5.0, 5.0
  coupling ac, dc, dc
  trigger ext 1.0 rising    <- REQUIRED: the ASOPS delay clock
  sample_rate 1e6
  segment_window 50e-6
  rep_rate 80e6
  end

Naming both is refused rather than resolved. Four things differ from the
LeCroy fly path, and all four are deliberate:

* `trigger` is REQUIRED. Every segment is one delay sweep, so without a
  trigger the segments are not sweeps and the delay axis means nothing. The
  unit never self-triggers either (there is no equivalent of `TRMD AUTO` to
  leave switched on), so a missing delay clock fails the line instead of
  filling it with something that is not a sweep. This is what makes
  HARDWARE_CHECKLIST item 12b - "the trigger is never configured, only
  inherited" - not apply to a PicoScope at all.

* SEGMENT TIMES ARE DERIVED, NOT MEASURED. A LeCroy timestamps every segment
  in its TRIGTIME array, which is what places each copeak along the line.
  Block mode has no equivalent: it reports each segment's sub-sample trigger
  OFFSET and not the time between triggers. The triggers come from the delay
  clock at a known fixed rate, so segment i is placed at i / delay_rate -
  exact for a fixed-rate clock, and only wrong if a sweep was MISSED. So the
  capture count is read back on every line and a short count fails it, which
  is the same discipline the LeCroy path applies to its segment grant. Every
  line records `trigtime_source` (`delay_clock+measured_offset`, or
  `delay_clock` where the unit will not report offsets) so nothing
  downstream reads the first column as a measurement. The practical cost:
  a PicoScope fly scan cannot measure trigger jitter, only rule out missed
  sweeps.

* `sample_rate` is REFUSED rather than substituted if the unit cannot reach
  it. In pscan a slower rate is a warning; here the sample rate sets the
  delay resolution, so quietly halving it would quietly halve what the file
  resolves.

* SPARSING STILL SHRINKS THE TRANSFER. WFSU's SP factor becomes the Pico
  driver's decimation mode, so the discarded samples never cross the bus,
  exactly as on the LeCroy. Channels are therefore read back one at a time,
  each at its own ratio.

`transfer_rate` defaults to 30 MB/s on USB rather than the LeCroy's 4 MB/s.
Both are only used to size timeouts and the time estimate - measure yours.

A FLY CONFIG IS FLAT, AND NESTING IT IS REFUSED

pscan's blocks nest, and the nesting IS the scan: where you write a `count`
block decides whether it averages a point, repeats a row or repeats the
image. None of that can be true in a fly scan, because its shape is fixed
rather than written:

  * the slow axis is the only loop there is;
  * the fast axis is NOT a loop - it is one continuous move per line;
  * the samples along it are hardware triggers, not Python iterations.
    Getting Python out of the per-pixel loop is the entire point of flying.

So there is nothing a fly config could put inside a loop, and position
carries no information: exactly one `apt_fly`, one `apt_stage` and one
`scope_line` are required, in any order.

Writing one hierarchically is REFUSED with that reason, rather than being
flattened and run. The reader underneath would flatten it quite happily and
produce the right answer - which is the problem. Someone who nests is saying
"placement matters here", and a format whose central rule is true in one
orchestrator and quietly false in the other is worse than two formats that
look different.

`action count` above 1 is refused for the same kind of reason: nothing in the
fly runner reads it, so a config asking for 4 made one pass and wrote a
quarter of the expected data. It used to warn that it would "fly every line 4
times", which is an effect that never happened. `count 1` is still accepted
in silence. For more signal per pixel use `bin <nm>` in `action apt_fly`,
which averages neighbouring copeaks into one output pixel and whose SNR gain
`--dry-run` reports; repeating the whole image to compare passes for drift is
on the to-do list, not implemented.

### RUNNING THE TESTS

   python -m unittest discover -s tests -t .

No hardware needed. The acquisition path is exercised through tests/fake_card.py,
the USB scope through tests/fake_picoscope.py, and the axis walk through fake
axes that only record what they were told to do.
Both fakes deliberately live in tests/ rather than pscan/: source holds one
driver file per real piece of hardware, and a simulated card or scope that could
be auto-selected during a scan would produce a full set of plausible .d files
containing nothing but a synthesised sine.

tests/fake_picoscope.py stands in for the PicoSDK at the ctypes seam, so every
decision worth testing - which range, which timebase, what reaches the file,
what happens when a trigger never fires - is covered without a scope on the
bench. tests/test_picoscope.py also pins the .m text against a literal, because
that format is read by every analysis script in the lab.

Worth running after any change to the scan loop, the config parser or the
output format - those three are where a mistake is silent rather than loud.

On a machine without the full install the axis-walk module skips, because the
grid lives in main.py alongside the camera and scope imports. The other three
modules run anywhere.

### Configuration File (.con) Template

Two things follow, and they are not the same kind of thing.

**THE TEMPLATE is the skeleton immediately below: copy that.** It nests,
because that is how a scan is written.

**THE LONG LISTING AFTER IT IS A KEY REFERENCE, NOT A SCAN.** It is every
block pscan understands with every key it takes, one after another at the top
level so each can be read on its own. Do not copy its LAYOUT - a file with
all of those blocks side by side at one level is a flat config, and the shape
it would run is inferred rather than stated. Take the keys you need from it
and put the blocks inside each other, as the skeleton does.

(That distinction is not pedantry. `examples/` used to carry a flat example
too, and it got used as the template for new files purely by being there, so
it was deleted. A reference that reads like a template does the same thing.)

Unknown action types, unknown keys, out-of-range axes and missing required
lines are all rejected before any hardware opens, so a typo fails loudly
instead of silently falling back to a default.

CRITICAL NOTE FOR LECROY SCOPES: The 'action scope' block should declare 'sweeps' so the driver knows exactly how many trigger pulses to wait for before timing out. 'segments' and 'averages' are accepted as aliases, but prefer 'sweeps' - until recently only 'sweeps' was actually parsed, so older config files written with 'segments' were silently falling back to whatever the scope was already set to.

## ==========================================
## THE TEMPLATE - copy this, then add keys from the reference below
## ==========================================
##
##   for each of 3 X positions
##       for each of 3 Y positions
##           take 4 traces, recording one a2d each
##
## 3 x 3 x 4 = 36 traces. Move the 'count' block out one level and it
## repeats a ROW instead of averaging a POINT; out two and it repeats the
## whole IMAGE. See WHEN EACH ACTION RUNS above.
action apt_stage
axis 0
scan 53.555 53.565 0.005
limits 50.000 57.000

    action apt_stage
    axis 1
    scan 37.000 37.010 0.005
    limits 35.000 40.000
    restore

        action count
        count 4

            action a2d
            channel 1
            v_min -1.0
            v_max 1.0
            terminal rse
            sample_rate 10000
            n_samples 100
            end

        end
    end
end

## ==========================================
## KEY REFERENCE - every block and every key it takes.
## NOT a scan. Take the keys; nest the blocks as above.
## ==========================================

## 1. LOOP COUNT
## How many times to repeat whatever is inside it. In a flat file it has no
## inside, and applies at each coordinate.
action count
count 1
end

## 2. X-AXIS HARDWARE 
## scan: <start_mm> <stop_mm> <step_mm>   (axis must be 0 for X or 1 for Y)
## 'limits <min_mm> <max_mm>' is optional but strongly recommended: every
## generated position is checked against it before the stage is even opened,
## so a mistyped coordinate is caught instead of driven into. Omitting it
## only warns.
action apt_stage
axis 0
scan 53.555 53.565 0.005
limits 50.000 57.000
end

## 3. Y-AXIS HARDWARE
## 'restore' sends the axis back to start_mm when ITS LOOP ends - so on a
## nested inner axis that is once per iteration of the axis outside it
## (after every row), and on the outermost axis it is at the end of the run.
action apt_stage
axis 1
scan 37.000 37.010 0.005
limits 35.000 40.000
restore
end

## 4. SYSTEM DELAY
action delay
time 1.5
end

## 5. WEBCAM ACQUISITION
action webcam
device 0
end

## 6. A2D ACQUISITION (DC voltage off the photodiode)
## Reaches the card through whichever backend can serve it - see "DAQ CARDS
## AND BACKENDS" above. With no 'backend'/'device' line pscan auto-detects,
## and stops rather than guessing if two cards could both serve this block.
##
## Prefer 'v_min'/'v_max' over 'range': a range INDEX means different things
## on different cards, while explicit volts resolve to the narrowest range
## that contains them on comedi and DAQmx alike.
##
## 'terminal' is how the input is wired: rse (default, and what this block
## always did), nrse, diff, or pseudodiff.
action a2d
channel 1
v_min -1.0
v_max 1.0
terminal rse
sample_rate 10000
n_samples 100
end

## 6b. THE SAME BLOCK, PINNED TO ONE CARD AND USING A LEGACY RANGE INDEX
## action a2d
## channel 1
## backend daqmx
## device Dev1
## range 0
## sample_rate 10000
## n_samples 100
## end

## 6c. D2A OUTPUT (apply a voltage - scanning mirror, piezo, anything DC)
## Each d2a block owns one output channel and one scan axis, so a galvo pair
## is two blocks on two axes. Axis ids set the nesting order: LOWER is the
## OUTER loop, so 98 is the slow axis and 99 the fast one. Axes 0 and 1
## belong to the APT stage; a d2a axis must be 2..99 and defaults to 99.
##
## 'units volts' is the default. 'units um' scales through 'um_per_volt',
## which is how the piezo is driven in microns.
##
## 'settle' is how long to wait after setting the voltage. An output is
## open-loop - there is no position to poll back - so this is the only thing
## standing between the move and the acquisition. A galvo settles in about a
## millisecond; a piezo takes tens. There is no safe default, so check it.
##
## 'restore' returns the axis to the start of its scan when its loop ends.
## Without it the output is left where the scan finished - as is the case at
## the end of a run, deliberately, so nothing slams back to zero.
action d2a
axis 98
backend daqmx
device Dev1
channel 0
units volts
scan -2.0 2.0 0.1
limits -5.0 5.0
settle 0.002
restore
end

## 6d. THE PIEZO, IN MICRONS, ON THE SECOND OUTPUT OF THE SAME CARD
## action d2a
## axis 99
## channel 1
## units um
## um_per_volt 20.0
## scan 0 100 25
## limits 0 100
## settle 0.25
## end

## 6e. THE ORIGINAL PIEZO BLOCK - still works, unchanged
## 'action d2a_phidget' is now shorthand for a d2a axis with the Phidget
## backend pinned, um as its unit, 20 um/V and the settle it always had.
## Existing config files need no edits.
##
## It is on its way out, though: everything it hardcodes is now sayable in a
## confile. Prefer the longhand below for new configs - it hides nothing. Once
## no .con file in the lab uses the short form, the block can be deleted.
## action d2a_phidget
## scan 0 100 25
## limits 0 100
## end
##
## 6f. THE SAME THING, SPELLED OUT - identical behaviour to 6e
## 'settle 0.20' rather than 0.25 because d2aphidget_driver.set_position_um()
## sleeps a further 0.05s inside itself, and that driver is still the one
## doing the write.
## action d2a
## axis 99
## backend phidget
## channel 0
## units um
## um_per_volt 20.0
## scan 0 100 25
## limits 0 100
## settle 0.20
## end

## 7. LECROY OSCILLOSCOPE ACQUISITION
## 'sweeps' sets the maths averaging (optional) (accepts multiple values for multi-ch)
## if no 'sweeps' line in the confile then default to hardware
## Channels are Waverunner trace names: C1-style inputs, F1-style maths,
## M1-style memories, Z1-style zooms, or TA..TD.
## Do not mix maths (F*) with raw (C*) channels while Sequence mode is on: an
## average collapses every segment into one, so the channel shapes disagree
## and the trace is refused. Use separate scope blocks instead.
action scope
ip 192.168.1.100
channels F1, F2
sweeps 1000, 100
end

## 7b. PICOSCOPE (USB) ACQUISITION
## A USB PicoScope has no front panel, so unlike 'action scope' this block
## carries every setting. Only 'channels' is required; everything below shows
## its default in the comment. See "OSCILLOSCOPES" above.
##
## Writes <base>_pico0.dat and <base>_pico0.m, in the same format the LeCroy
## path writes - so a run may use both instruments.
action picoscope
channels A, B
## Which unit, when the bench has more than one. Omit both and pscan uses
## the single attached unit, or stops and lists them if there are two.
## series ps5000a
## serial AB123/456
## resolution 12          ## 5000A only: 8, 12, 14, 15 or 16 bits (default 8)
##
## Full scale per channel, in volts; the range is +/-v_max. One value applies
## to every channel. pscan picks the narrowest Pico range that contains it.
v_max 0.2, 5.0
coupling ac, dc           ## default dc
##
## A request, not a promise: the unit's timebase ladder decides, and the rate
## actually used goes in scp.hint. Default 1e6.
sample_rate 5e6
n_samples 4096            ## default 1000
pretrigger 10             ## percent of the record BEFORE the trigger (default 0)
##
## trigger <source> <level_volts> <edge>, where source is a channel letter
## this block records or 'ext' for the external input, and edge is rising,
## falling, above, below or rising_or_falling. 'trigger none' captures
## immediately; omitting the line does the same and warns, because that is
## right for DC and wrong for anything pulsed.
trigger B 1.5 rising
timeout 2.0               ## seconds to wait for a capture before padding the trace
##
## captures: rapid-block segments WRITTEN per trace (scp.multitraces)
## averages: segments averaged into each written row (scp.n_averages).
## 'sweeps' is accepted as a synonym for 'averages'.
captures 10
averages 4
end

## 8. EXTERNAL SYSTEM SCRIPT
## Executes a raw terminal command on the host OS
action script
script echo "Acquisition step complete!"
end

## 9. ACQUIRE ANDOR IXON FRAMES
## The camera block executes at every coordinate.
## If shutter_open is True, the shutter stays open for the entire run.
## NOT TTL DRIVEN YET, WILL BE MUCH FASTER
##
## Camera setup (including the TEC cooldown, which can take minutes) happens
## BEFORE the stage moves, so the sample is not parked at the first
## coordinate while the sensor cools.
##
## Frames are written one raw .bin file per trace, which carries no header.
## The geometry needed to reload them goes into <base>_ixon.m (or
## <base>_tucsen.m) at the end of the run: height, width, dtype,
## bytes_per_pixel and the number of frames actually captured.
action ixon
exposure 0.1
em_gain 72
shutter_open True
temperature -80
end

### Disclaimer & License
Author: Sal
Year: 2026

Disclaimer: This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. In no event shall the author be liable for any claim, damages, hardware collisions, or other liability, whether in an action of contract, tort or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software. Always verify coordinate boundaries manually before initiating automated stage movements.
