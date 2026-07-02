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
echo -e "\n[PIP] Installing Python package globally..."

# Notice the '-e' is completely gone. This copies the files to the system 
# directory safely without polluting the local user folder with root permissions.
sudo pip install . --break-system-packages

echo -e "\n[SUCCESS] Installation complete!"
echo "You can now run 'pscan' or 'pystage' from anywhere in the terminal."
