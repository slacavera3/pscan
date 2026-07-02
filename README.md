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

### Installation

For Linux deployments, a universal bash script is provided to handle both OS-level hardware drivers (Comedi) and Python package linking. 

-- Local Lab Installation (Server Direct) --
If you are operating directly on the lab server where the source code is hosted:
1. Navigate to the pscan source directory:
   cd path/to/psource

2. Run the installer. Note: The script will invoke sudo internally to install apt packages and link the CLI commands globally. You may be prompted for your password.
   ./install_pscan.sh

-- Remote Installation (Via GitHub) --
If you are deploying on a new machine:
1. Clone the repository and navigate to the directory:
   git clone https://github.com/slacavera3/pscan.git
   cd pscan

2. Make the installer executable:
   chmod +x install_pscan.sh

3. Run the installer:
   ./install_pscan.sh

-- Remote Installation (Via GitHub) on Windows (7)
If you are deploying on a new machine:
1. pip install git+https://github.com/slacavera3/pscan.git

2. If you're deploying on a machine which already has a copy of the repository for some reason: 'pip install -e .' in the root folder).

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
