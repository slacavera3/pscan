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
COMMANDS=(pscan-dev pystage-dev pypiezo-dev pydaq-dev flyscan-dev)

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

# --- 2. Install the shared system drivers ---
# These are shared - both builds load the same .so files through ctypes - so
# they are installed by install.sh, which this script calls with
# --drivers-only. That flag stops install.sh before its pip step, which is the
# part that must not run here: both builds ship a top-level 'pscan' package,
# so a full install.sh would drop this branch's copy over a stable one.
#
# This used to only WARN and tell you to run install.sh from a stable
# checkout. On an experimental rig with no stable checkout that was
# impossible, and the result was a machine where every DAQ backend reported
# itself unavailable.
echo -e "\n[2/5] Installing shared system drivers..."

drivers_present() {
    local missing=0
    ldconfig -p 2>/dev/null | grep -q libcomedi || missing=1
    [ -f "/usr/local/lib/libphidget21.so" ] || [ -f "/usr/lib/libphidget21.so" ] || missing=1
    [ -f "/opt/tucsen/sdk/lib/libTUCam.so" ] || missing=1
    return $missing
}

if drivers_present; then
    echo " -> All shared drivers already present. Skipping."
else
    echo " -> One or more shared drivers are missing; running install.sh"
    echo " -> --drivers-only (this needs sudo)."
    # In a subshell with the repo as CWD: install.sh resolves ./libs/ and its
    # libTUCam.so search relative to the working directory, so calling it from
    # anywhere else would silently fail to find the bundled tarballs.
    ( cd "$REPO_DIR" && bash install.sh --drivers-only ) || \
        echo " -> [WARNING] Driver install did not complete cleanly; see above."
fi

# Report what is actually usable, after the attempt above.
ldconfig -p 2>/dev/null | grep -q libcomedi \
    && echo " -> libcomedi found." \
    || echo " -> [WARNING] libcomedi not found. comedi cards will not work."

{ [ -f "/usr/local/lib/libphidget21.so" ] || [ -f "/usr/lib/libphidget21.so" ]; } \
    && echo " -> libphidget21.so found." \
    || echo " -> [WARNING] libphidget21.so not found. The piezo D2A will not work."

[ -f "/opt/tucsen/sdk/lib/libTUCam.so" ] \
    && echo " -> libTUCam.so found." \
    || echo " -> [WARNING] libTUCam.so not found. The Tucsen camera will not work."

if ldconfig -p 2>/dev/null | grep -q libnidaqmx; then
    echo " -> libnidaqmx found (NI-DAQmx driver installed)."
    NEED_DAQMX_PY=1
else
    echo " -> [INFO] libnidaqmx not found. Only needed for NI X Series cards"
    echo " ->        (PCIe-635x/636x, e.g. the PCIe-6351); comedi cards are"
    echo " ->        unaffected. install.sh skips it when lspci sees no NI"
    echo " ->        card - seat the card and re-run, or force it with:"
    echo " ->          PSCAN_FORCE_DAQMX=1 bash install.sh --drivers-only"
    echo " ->        A reboot is needed after it installs, then re-run this."
    NEED_DAQMX_PY=0
fi

# --- 3. Build a clean virtualenv ---
# --clear rather than a reuse-if-present check: a stale venv carrying a
# half-removed earlier build is the failure mode worth spending 20 seconds to
# rule out, and this script is expected to be re-run on every update.
echo -e "\n[3/5] Creating an isolated virtualenv..."
# `python3 -m venv --help` is NOT a usable check: the venv module ships with
# Python, so --help succeeds on a machine where creating a venv still fails
# with "ensurepip is not available". Debian and Ubuntu split ensurepip into a
# separate package, so test for what is actually needed.
venv_ready() {
    python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

if ! venv_ready; then
    # The package name is version-specific (python3.13-venv on a 3.13
    # machine), and the generic metapackage does not always pull the right
    # one. Install both names and let apt sort it out.
    PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    echo " -> python3-venv is missing (ensurepip unavailable). Installing"
    echo " -> python${PYVER}-venv. This is the one step here that needs sudo."
    sudo apt install -y "python${PYVER}-venv" || true
    if ! venv_ready; then
        sudo apt install -y python3-venv || true
    fi

    if ! venv_ready; then
        echo " -> [ERROR] Could not make python3-venv work. Install it by hand:"
        echo " ->   sudo apt install -y python${PYVER}-venv"
        exit 1
    fi
    echo " -> python3-venv is ready."
fi
mkdir -p "$(dirname "$VENV_DIR")"
python3 -m venv --clear "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip

# --- 4. Install this branch into it ---
echo -e "\n[4/5] Installing the experimental build (this pulls its own deps)..."
# The daqmx extra is added only when the system driver is actually present.
# The nidaqmx Python package is useless without it, and installing it anyway
# would make 'pydaq-dev list' report the backend as available right up to the
# point it fails to open anything.
if [ "${NEED_DAQMX_PY:-0}" -eq 1 ]; then
    echo " -> NI-DAQmx driver present: including the [daqmx] extra."
    "$VENV_DIR/bin/python" -m pip install --no-cache-dir "$REPO_DIR[daqmx]"
else
    "$VENV_DIR/bin/python" -m pip install --no-cache-dir "$REPO_DIR"
fi

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

# The PATH verdict decides what the closing banner is allowed to claim. An
# earlier version announced DONE and listed the commands, then warned about
# PATH underneath - so the install read as successful and the one thing left
# to do scrolled past as a footnote. Nothing here says the commands are
# runnable until they actually are.
case ":$PATH:" in
    *":$BIN_DIR:"*) PATH_OK=1 ;;
    *) PATH_OK=0 ;;
esac

if [ "$PATH_OK" -eq 1 ]; then
    echo -e "\n======================================================="
    echo " DONE! Run 'pscan-dev' | 'pystage-dev' | 'pypiezo-dev' |"
    echo "       'pydaq-dev' | 'flyscan-dev'."
    echo " The stable 'pscan' command is untouched."
    echo ""
    echo " Start with 'pydaq-dev list' to see which DAQ cards pscan can"
    echo " reach and under which backend."
    echo "======================================================="
else
    echo -e "\n======================================================="
    echo " ALMOST DONE - ONE STEP LEFT"
    echo "======================================================="
    echo " The build installed correctly, but $BIN_DIR"
    echo " is not on your PATH, so the -dev commands will not be found yet:"
    echo ""
    echo "   echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    echo "   source ~/.bashrc"
    echo ""
    # Debian and Ubuntu put ~/.local/bin on PATH from ~/.profile, but only
    # when the directory already exists as the login shell starts. If this
    # script created it a moment ago, that test failed at the last login and
    # will keep failing until the next one - which looks exactly like a
    # broken install.
    echo " (Debian/Ubuntu add this directory automatically, but only if it"
    echo "  exists at login - a newly created one needs the line above, or"
    echo "  a log out and back in.)"
    echo ""
    echo " Until then the commands still work by full path, e.g."
    echo "   $VENV_DIR/bin/pscan-dev"
    echo "======================================================="
fi
