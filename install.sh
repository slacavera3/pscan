#!/bin/bash

echo "======================================================="
echo "        PSCAN LAB - AUTOMATED INSTALL & UPDATE         "
echo "======================================================="

# --- 1. Install System Dependencies ---
echo -e "\n[1/5] Checking OS-level C-drivers (Comedi)..."
sudo apt update
sudo apt install -y libcomedi0 libcomedi-dev

# --- 2. Automated PEP 668 Flag Detection ---
echo -e "\n[2/5] Analyzing Python environment..."
FLAG=""
# Check if the current pip version even knows what --break-system-packages is
if python3 -m pip install --help | grep -q "\-\-break-system-packages"; then
    FLAG="--break-system-packages"
    echo " -> Modern PEP 668 environment detected. Using $FLAG"
else
    echo " -> Legacy pip environment detected. No override needed."
fi

# --- 3. Clean Build Artifacts ---
echo -e "\n[3/5] Scrubbing old build artifacts..."
sudo rm -rf build/ pscan_lab.egg-info/ dist/

# --- 4. Exorcise Ghost Installations ---
echo -e "\n[4/5] Removing existing or broken installations..."
# Ask pip nicely first
sudo pip uninstall -y pscan-lab $FLAG 2>/dev/null
pip uninstall -y pscan-lab $FLAG 2>/dev/null

# Brute-force delete any lingering files
rm -rf ~/.local/lib/python*/site-packages/pscan*
rm -f ~/.local/bin/pscan
sudo rm -rf /usr/local/lib/python*/dist-packages/pscan*
sudo rm -f /usr/local/bin/pscan

# --- 5. Clean Installation ---
echo -e "\n[5/5] Compiling and installing pscan..."
sudo pip install --force-reinstall --no-deps --no-cache-dir . $FLAG

echo -e "\n======================================================="
echo " DONE! You can now run 'pscan' or 'pystage' from anywhere."
echo "======================================================="
