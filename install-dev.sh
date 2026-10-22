#!/bin/bash
#
# Installs this branch as pscan-dev, alongside an existing stable pscan.
#
# The two builds cannot share a site-packages directory: both ship a
# top-level 'pscan' package, so whichever was installed last would own the
# files and uninstalling either would break the other. This installer sidesteps
# that by giving the experimental build its own virtualenv and exposing only
# the -dev commands on PATH. Nothing it touches is shared with the stable
# install, so install.sh can still be run afterwards without disturbing this.

set -euo pipefail

VENV_DIR="${PSCAN_DEV_HOME:-$HOME/.local/share/pscan-dev}/venv"
BIN_DIR="$HOME/.local/bin"
COMMANDS=(pscan-dev pystage-dev pypiezo-dev flyscan-dev)

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================================="
echo "      PSCAN-DEV - EXPERIMENTAL BUILD INSTALLER         "
echo "======================================================="
echo " Source: $REPO_DIR"
echo " Venv:   $VENV_DIR"

# --- 1. Sanity-check the source tree ---
echo -e "\n[1/5] Checking the source tree..."
if [ ! -f "$REPO_DIR/setup.py" ]; then
    echo " -> [ERROR] No setup.py beside this script. Run it from inside the repo."
    exit 1
fi

# Installing the stable branch under the dev name would produce a pscan-dev
# that is not actually the experimental build, which is worse than failing.
if [ ! -d "$REPO_DIR/flyscan" ]; then
    echo " -> [ERROR] No flyscan/ directory found."
    echo " -> This looks like the stable branch. Check out the experimental"
    echo " -> branch first:  git checkout test/working-snapshot"
    exit 1
fi

if command -v git >/dev/null 2>&1 && git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    echo " -> Branch: $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD) ($(git -C "$REPO_DIR" rev-parse --short HEAD))"
fi

# --- 2. Check the shared system drivers ---
# These are installed system-wide by install.sh and are not duplicated here:
# both builds load the same .so files through ctypes. Missing ones are only
# a problem for the hardware that needs them, so warn rather than exit.
echo -e "\n[2/5] Checking shared system drivers..."
if [ -f "/usr/local/lib/libphidget21.so" ] || [ -f "/usr/lib/libphidget21.so" ]; then
    echo " -> libphidget21.so found."
else
    echo " -> [WARNING] libphidget21.so not found. The piezo D2A will not work."
    echo " -> Run 'bash install.sh' once to install the shared system drivers."
fi

if [ -f "/opt/tucsen/sdk/lib/libTUCam.so" ]; then
    echo " -> libTUCam.so found."
else
    echo " -> [WARNING] libTUCam.so not found at /opt/tucsen/sdk/lib."
    echo " -> The Tucsen camera will not work until install.sh has been run."
fi

# --- 3. Build a clean virtualenv ---
# --clear rather than a reuse-if-present check: a stale venv carrying a
# half-removed earlier build is the failure mode worth spending 20 seconds to
# rule out, and this script is expected to be re-run on every update.
echo -e "\n[3/5] Creating an isolated virtualenv..."
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo " -> [ERROR] python3-venv is missing. Install it with:"
    echo " ->   sudo apt install -y python3-venv"
    exit 1
fi
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv --clear "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip

# --- 4. Install this branch into it ---
echo -e "\n[4/5] Installing the experimental build (this pulls its own deps)..."
"$VENV_DIR/bin/python" -m pip install --no-cache-dir "$REPO_DIR"

# --- 5. Expose the -dev commands on PATH ---
# Symlinks, not copies, so a re-run of step 4 is picked up without reinstalling
# the shims. They point into the venv, whose console scripts already hard-code
# its interpreter, so the commands work without activating anything.
echo -e "\n[5/5] Linking commands into $BIN_DIR..."
mkdir -p "$BIN_DIR"
for cmd in "${COMMANDS[@]}"; do
    if [ ! -x "$VENV_DIR/bin/$cmd" ]; then
        echo " -> [ERROR] $cmd was not installed into the venv. Aborting."
        exit 1
    fi
    ln -sfn "$VENV_DIR/bin/$cmd" "$BIN_DIR/$cmd"
    echo " -> $cmd"
done

echo -e "\n======================================================="
echo " DONE! Run 'pscan-dev' | 'pystage-dev' | 'pypiezo-dev' | 'flyscan-dev'."
echo " The stable 'pscan' command is untouched."
echo "======================================================="

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo -e "\n[WARNING] $BIN_DIR is not on your PATH, so the commands above"
        echo "will not be found yet. Add this to ~/.bashrc and open a new shell:"
        echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
        ;;
esac
