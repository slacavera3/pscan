#!/bin/bash

echo "======================================================="
echo "        PSCAN LAB - AUTOMATED INSTALL & UPDATE         "
echo "======================================================="

# --- 1. Install System Dependencies ---
echo -e "\n[1/6] Checking OS-level C-drivers (Comedi)..."
sudo apt update
sudo apt install -y libcomedi0 libcomedi-dev

# --- 2. Setup Tucsen SDK Drivers ---
echo -e "\n[2/6] Setting up Tucsen Dhyana SDK..."
TUCSEN_DIR="/opt/tucsen/sdk/lib"
sudo mkdir -p $TUCSEN_DIR

if [ -f "$TUCSEN_DIR/libTUCam.so" ]; then
    echo " -> Tucsen driver already installed at $TUCSEN_DIR."
else
    # Search the current directory (and subdirectories) for the driver file
    LOCAL_LIB=$(find . -name "libTUCam.so" | head -n 1)
    if [ -n "$LOCAL_LIB" ]; then
        echo " -> Found local Tucsen driver at $LOCAL_LIB."
        echo " -> Copying to $TUCSEN_DIR..."
        sudo cp "$LOCAL_LIB" $TUCSEN_DIR/
        sudo chmod 755 $TUCSEN_DIR/libTUCam.so
    else
        echo " -> [WARNING] libTUCam.so not found!"
        echo " -> Please extract your Tucsen driver archive inside this folder, or manually copy libTUCam.so to $TUCSEN_DIR."
    fi
fi

# --- 3. Automated PEP 668 Flag Detection ---
echo -e "\n[3/6] Analyzing Python environment..."
FLAG=""
# Check if the current pip version even knows what --break-system-packages is
if python3 -m pip install --help | grep -q "\-\-break-system-packages"; then
    FLAG="--break-system-packages"
    echo " -> Modern PEP 668 environment detected. Using $FLAG"
else
    echo " -> Legacy pip environment detected. No override needed."
fi

# --- 4. Clean Build Artifacts ---
echo -e "\n[4/6] Scrubbing old build artifacts..."
sudo rm -rf build/ pscan_lab.egg-info/ dist/

# --- 5. Exorcise Ghost Installations ---
echo -e "\n[5/6] Removing existing or broken installations..."
# Ask pip nicely first
sudo pip uninstall -y pscan-lab $FLAG 2>/dev/null
pip uninstall -y pscan-lab $FLAG 2>/dev/null

# Brute-force delete any lingering files
rm -rf ~/.local/lib/python*/site-packages/pscan*
rm -f ~/.local/bin/pscan
sudo rm -rf /usr/local/lib/python*/dist-packages/pscan*
sudo rm -f /usr/local/bin/pscan

# --- 6. Clean Installation ---
echo -e "\n[6/6] Compiling and installing pscan..."
sudo pip install --force-reinstall --no-deps --no-cache-dir . $FLAG

echo -e "\n======================================================="
echo " DONE! You can now run 'pscan' or 'pystage' from anywhere."
echo "======================================================="
