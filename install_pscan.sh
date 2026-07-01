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

# 2. Let pip handle the Python dependencies and CLI linking
echo -e "\n[PIP] Synchronizing Python environment..."

# The '-e .' flag tells pip to look at the setup.py in the current folder.
# --break-system-packages is included for global Debian 13 compatibility.
sudo pip install -e . --break-system-packages

echo -e "\n[SUCCESS] Installation complete!"
echo "You can now run 'pscan' or 'pystage' from anywhere in the terminal."
