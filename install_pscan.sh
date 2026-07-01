#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "=================================================="
echo " pscan Universal Installer"
echo "=================================================="

# 1. Handle OS-Level Hardware Drivers (Linux Only)
if [ "$(uname)" = "Linux" ]; then
    if ! dpkg -l | grep -q libcomedi0; then
        echo "[APT] libcomedi0 not found. Installing hardware drivers..."
        sudo apt update
        sudo apt install -y libcomedi0 libcomedi-dev python3-pip
    else
        echo "[APT] Linux Comedi drivers are already installed system-wide."
    fi
fi

# 2. The Anti-Bandaid: Enforce Directory Structure
# If the labmate somehow cloned an incomplete repo, this fixes it silently.
if [ ! -f "pscan/__init__.py" ]; then
    echo "[SYSTEM] Enforcing Python package structure..."
    mkdir -p pscan
    touch pscan/__init__.py
fi

# 3. Synchronize Python Environment
echo -e "\n[PIP] Synchronizing Python environment globally..."
sudo pip install -e . --break-system-packages

# 4. The Anti-Bandaid: Fix Root Ownership
# Because sudo pip creates root-owned metadata, we forcibly return ownership 
# to the labmate so they never see a 'Permission Denied' traceback.
if [ "$(uname)" = "Linux" ]; then
    echo "[SYSTEM] Cleaning up permissions..."
    sudo chown -R $USER:$USER .
fi

echo -e "\n[SUCCESS] Installation complete!"
echo "You can now run 'pscan' or 'pystage' from anywhere in the terminal."
