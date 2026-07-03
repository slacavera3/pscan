# pscan-lab

Automated Multi-Sensor Acquisition & Thorlabs Stage Control

pscan-lab is a cross-platform Python orchestration package designed for hardware-synchronized data acquisition. 

### Supported Hardware
* Thorlabs Controllers: BBD202 and BBD302 Brushless DC Motor Controllers.
* Thorlabs Stages: MLS203 Fast XY Scanning Stages (driver utilizes 34304.0 encoder counts/mm scaling).
* Oscilloscopes: LeCroy Oscilloscopes (via ethernet/VXI-11).
* Data Acquisition: National Instruments A2D cards (via Linux Comedi).
* Imaging: Standard USB Webcams (via OpenCV/V4L2/DirectShow).

### System Requirements
* Primary OS: Debian 13 (Linux). This package is optimized and tested for Debian 13. If you require deployment on other Linux distributions, please reach out to Sal.
* Legacy Support: Nominally supports Windows 7 (requires Python 3.8.x maximum).
* Python Dependencies: numpy, opencv-python, pyserial, python-vxi11, and standard-xdrlib.


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
1. GLOBAL INSTALLATION (RECOMMENDED)
--------------------------------------------------------------------------------
This is the absolute fastest way to install pscan globally. You do not need to 
clone the repository first.

# LINUX (Ubuntu/Debian): 
# Step 1: Install the required hardware drivers system-wide via apt
sudo apt update && sudo apt install -y libcomedi0 libcomedi-dev

# Step 2: Install the Python package directly from GitHub
# Note: Use sudo to install for all users. The override flag is required for Debian 13.
sudo pip install git+https://github.com/slacavera3/pscan.git --break-system-packages

# WINDOWS POWERSHELL (Run as Administrator):
# (Windows does not require the Comedi drivers)
pip install --force-reinstall git+https://github.com/slacavera3/pscan.git

--------------------------------------------------------------------------------
2. INSTALLING FROM A LOCAL SOURCE (FOR DEVELOPERS)
--------------------------------------------------------------------------------
If you are developing or testing local changes, navigate to your source directory 
and run the standard pip installer.

# LINUX (Debian 13 Global Override):
cd /path/to/your/psource
pip install . --break-system-packages

# WINDOWS POWERSHELL:
cd C:\path\to\your\psource
pip install .

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
4. UPDATING OR REINSTALLING PSCAN
--------------------------------------------------------------------------------
If a new version of pscan is released, run the appropriate command to upgrade.
Using --no-deps prevents Debian dependency uninstall errors.

# Global Update (Linux & Windows):
sudo pip install --force-reinstall --no-deps git+https://github.com/slacavera3/pscan.git --break-system-packages

# Local Source Update:
cd /path/to/your/psource
git pull
sudo pip install --force-reinstall --no-deps . --break-system-packages

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

### Configuration File (.con) Template

The orchestrator reads flat .con files. Below is a comprehensive template showing every possible unnested action block. 

CRITICAL NOTE FOR LECROY SCOPES: The 'action scope' block must explicitly declare 'segments' (or 'sweeps' / 'averages') so the driver knows exactly how many trigger pulses to wait for before timing out.

## ==========================================
## pscan Configuration Template
## ==========================================

## 1. LOOP COUNT
## Defines how many times the acquisition pipeline repeats per coordinate.
action count
count 1
end

## 2. X-AXIS HARDWARE 
## scan: <start_mm> <stop_mm> <step_mm>
action apt_stage
axis 0
scan 53.555 53.565 0.005
end

## 3. Y-AXIS HARDWARE
## 'restore' sends the axis back to start_mm after the scan finishes.
action apt_stage
axis 1
scan 37.000 37.010 0.005
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
## 'segments' is REQUIRED for dynamic trace allocation.
action scope
ip 192.168.1.100
channels F1, F2
segments 1000
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
action ixon
exposure 0.1
em_gain 72
kinetic_cycle 0.5
shutter_open True
end

### Disclaimer & License
Author: Sal
Year: 2026

Disclaimer: This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. In no event shall the author be liable for any claim, damages, hardware collisions, or other liability, whether in an action of contract, tort or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software. Always verify coordinate boundaries manually before initiating automated stage movements.
