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
   R            re-read positions and zero from the controller
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

C **refuses rather than guessing** when it cannot be sure, and says which axis
and why:

* the axis is not homed, so there is no coordinate frame to be the centre of
  (press H);
* the controller did not report its travel, or did not report whether it is
  homed;
* the reported `cnts_per_unit` does not match this rig's counts-per-mm, which
  means the `0x04F2` field offsets are wrong for that controller and the
  limits cannot be believed. The field layout is transcribed from two
  third-party libraries rather than Thorlabs' manual, so this check is what
  stands between a mis-parse and a move to a wrong absolute position.

C also acts only on *enabled* axes and names any it skipped, so a one-axis
move is never reported as having centred both.

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

Blocks are split into three pipelines, and the split is positional, not
declared. A block runs:

   * at SETUP (once, before anything moves)  if NO core block precedes it
   * at TEARDOWN (once, after the scan)      if NO core block follows it
   * PER COORDINATE (once per grid point)    otherwise

Core blocks are: apt_stage, d2a_phidget, d2a, count, scope, a2d, webcam,
delay, ixon, tucsen. In practice only 'action script' is ever non-core, so
whether your script runs once or thousands of times depends on where it sits
relative to the core blocks around it.

This catches people out, so check it with `pscan --dry-run`, which prints the
three pipelines explicitly. Note that in the template below the script block
sits between 'a2d'/'scope' and 'ixon', so it runs at EVERY coordinate. To run
a script once at startup, put it above every core block.

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

### RUNNING THE TESTS

   python -m unittest discover -s tests -t .

No hardware needed. The acquisition path is exercised through tests/fake_card.py
and the axis walk through fake axes that only record what they were told to do.
The fake card deliberately lives in tests/ rather than pscan/: source holds one
driver file per real piece of hardware, and a simulated card that could be
auto-selected during a scan would produce a full set of plausible .d files
containing nothing but a synthesised sine.

Worth running after any change to the scan loop, the config parser or the
output format - those three are where a mistake is silent rather than loud.

On a machine without the full install the axis-walk module skips, because the
grid lives in main.py alongside the camera and scope imports. The other three
modules run anywhere.

### Configuration File (.con) Template

The orchestrator reads flat .con files. Below is a comprehensive template showing every possible unnested action block. Unknown action types, unknown keys, out-of-range axes and missing required lines are all rejected before any hardware opens, so a typo fails loudly instead of silently falling back to a default.

CRITICAL NOTE FOR LECROY SCOPES: The 'action scope' block should declare 'sweeps' so the driver knows exactly how many trigger pulses to wait for before timing out. 'segments' and 'averages' are accepted as aliases, but prefer 'sweeps' - until recently only 'sweeps' was actually parsed, so older config files written with 'segments' were silently falling back to whatever the scope was already set to.

## ==========================================
## pscan Configuration Template
## ==========================================

## 1. LOOP COUNT
## Defines how many times the acquisition pipeline repeats per coordinate.
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
## 'restore' sends the axis back to start_mm after the scan finishes.
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
