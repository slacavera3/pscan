#!/bin/bash

echo "--- 1. Bypassing OS Locks to Uninstall pscan-lab ---"
sudo pip uninstall -y pscan-lab --break-system-packages
pip uninstall -y pscan-lab --break-system-packages

echo "--- 2. Brute-Force Deleting Ghost Directories ---"
# Nuke local user ghosts
rm -rf ~/.local/lib/python*/site-packages/pscan*
rm -f ~/.local/bin/pscan

# Nuke system-level ghosts
sudo rm -rf /usr/local/lib/python*/dist-packages/pscan*
sudo rm -f /usr/local/bin/pscan

echo "--- 3. Cleaning Build Artifacts ---"
sudo rm -rf build/ pscan_lab.egg-info/ dist/

echo "--- 4. Installing Fresh ---"
sudo pip install --force-reinstall --no-deps --no-cache-dir . --break-system-packages

echo "--- Update Complete! ---"

