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

# 2. Enforce Directory Structure
if [ ! -f "pscan/__init__.py" ]; then
    echo "[SYSTEM] Enforcing Python package structure..."
    mkdir -p pscan
    touch pscan/__init__.py
fi

# 3. Standard Global Installation
if [ -f "setup.py" ] && [ -d "pscan" ]; then
    echo -e "\n[PIP] Local repository detected. Installing from local source..."
    sudo pip3 install . --break-system-packages
else
    echo -e "\n[PIP] Remote installation detected. Pulling directly from GitHub..."
    sudo pip3 install git+https://github.com/slacavera3/pscan.git --break-system-packages
fi

echo -e "\n[SUCCESS] Installation complete!"
echo "You can now run 'pscan' or 'pystage' from anywhere in the terminal."
