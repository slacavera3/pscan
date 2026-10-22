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
1. GLOBAL INSTALLATION & UPDATING (LINUX - RECOMMENDED)
--------------------------------------------------------------------------------
For Debian, Ubuntu, and most Linux distributions, the provided `install.sh` 
script completely automates the entire setup. It automatically installs required 
`apt` C-drivers, detects your OS's PEP 668 restrictions, and securely scrubs 
old "ghost" dependencies before building the new package.

# Step 1: Download the source code
git clone https://github.com/slacavera3/pscan.git
cd pscan

# Step 2: Run the automated installer
bash install.sh

# TO UPDATE LATER:
# Simply navigate back to the folder, pull the latest code, and re-run:
cd /path/to/pscan
git pull
bash install.sh

--------------------------------------------------------------------------------
1b. EXPERIMENTAL BUILD, ALONGSIDE THE STABLE ONE (LINUX)
--------------------------------------------------------------------------------
This branch is the experimental build. It installs as `pscan-dev` and can sit
on the same machine as a normal `pscan` install without either disturbing the
other.

Why it needs its own installer: both builds ship a top-level `pscan` Python
package, so they cannot share a site-packages directory - whichever was
installed second would own the files, and uninstalling either would break the
other. `install-dev.sh` therefore puts this build in its own virtualenv under
~/.local/share/pscan-dev and links only the -dev commands into ~/.local/bin.

# Step 1: Clone somewhere SEPARATE from your stable pscan checkout
git clone https://github.com/slacavera3/pscan.git pscan-dev
cd pscan-dev
git checkout test/working-snapshot

# Step 2: Run the dev installer (no sudo, nothing installed system-wide)
bash install-dev.sh

# TO UPDATE LATER:
cd /path/to/pscan-dev
git pull
bash install-dev.sh

This gives you four commands that shadow nothing:
  pscan-dev | pystage-dev | pypiezo-dev | flyscan-dev

The shared system drivers (Comedi, libphidget21, libTUCam) are NOT installed
by install-dev.sh. Both builds load the same .so files, so run the normal
`bash install.sh` once from your stable checkout to put them in place;
install-dev.sh only checks for them and warns.

# TO REMOVE THE EXPERIMENTAL BUILD:
rm -f ~/.local/bin/{pscan-dev,pystage-dev,pypiezo-dev,flyscan-dev}
rm -rf ~/.local/share/pscan-dev

WARNING: do not run `pip install .` on this branch outside the venv. That
would drop its `pscan` package on top of your stable install's and leave you
with a mixture of the two.

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

Controls: Use Arrow Keys to jog, 'J' to adjust step size (mm), '1' or '2' to toggle motor power, and 'H' to home the stages and clear safety lockouts.

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

Core blocks are: apt_stage, d2a_phidget, count, scope, a2d, webcam, delay,
ixon, tucsen. In practice only 'action script' is ever non-core, so whether
your script runs once or thousands of times depends on where it sits
relative to the core blocks around it.

This catches people out, so check it with `pscan --dry-run`, which prints the
three pipelines explicitly. Note that in the template below the script block
sits between 'a2d'/'scope' and 'ixon', so it runs at EVERY coordinate. To run
a script once at startup, put it above every core block.

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

## 6. NI A2D ACQUISITION (Comedi)
action a2d
channel 1
range 0
sample_rate 10000
n_samples 100
end

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
