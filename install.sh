#!/bin/bash

echo "======================================================="
echo "        PSCAN LAB - AUTOMATED INSTALL & UPDATE         "
echo "======================================================="

# This is now the ONLY installer. There is no separate experimental build, no
# second package name, no virtualenv and no install-dev.sh - that arrangement
# produced a different failure mode for every combination of build, venv and
# partial install, which cost far more than the isolation was worth.
#
# --drivers-only installs just the shared system drivers (sections 1 to 2) and
# stops before touching Python packages. Useful on its own after a kernel
# upgrade, when NI-DAQmx's modules need rebuilding but nothing in Python has
# changed.
#
# Every driver step is idempotent, so re-running this costs seconds.
DRIVERS_ONLY=0
if [ "${1:-}" = "--drivers-only" ]; then
    DRIVERS_ONLY=1
    echo " MODE: shared system drivers only (no Python packages)"
fi

# ldconfig lives in /sbin, which is NOT on a normal user's PATH on Debian and
# Ubuntu. Calling it bare makes every "is this library installed?" check come
# back false for a library that is installed - which is exactly what happened:
# libcomedi was present, apt said so, pscan loaded it fine, and this script
# reported it missing. Always go through this.
# Several steps cd into extracted source trees; this is what they come back to.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT" || exit 1

ldconfig_p() {
    if command -v ldconfig >/dev/null 2>&1; then
        ldconfig -p 2>/dev/null
    elif [ -x /sbin/ldconfig ]; then
        /sbin/ldconfig -p 2>/dev/null
    else
        return 1
    fi
}

have_lib() {
    ldconfig_p | grep -q "$1"
}

# --- 1. Install System Dependencies ---
echo -e "\n[1/6] Checking OS-level C-drivers (Comedi)..."
sudo apt update
# python3-venv is here only so anyone who wants their own venv can make one:
# pscan itself no longer uses one. Debian and Ubuntu split
# ensurepip out, so creating a virtualenv fails without it. The versioned name
# is the one that actually matters (python3.13-venv on a 3.13 machine); the
# metapackage does not always pull it.
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")

# libusb is a HARD requirement for libphidget21: its configure aborts with
# "Missing libusb!" without it. Both flavours are listed because the configure
# probes for libusb-1.0 (libusb_init) and falls back to libusb-0.1
# (usb_find_busses). This was the missing piece that made the Phidget build
# fail while the script still announced success.
#
# dkms, build-essential and the running kernel's headers are here for
# NI-DAQmx: its kernel modules are built on this machine, and DKMS cannot
# build anything without headers matching `uname -r`.
sudo apt install -y \
    curl unzip \
    libcomedi0 libcomedi-dev \
    libusb-1.0-0-dev libusb-dev \
    build-essential dkms "linux-headers-$(uname -r)" \
    python3-pip
if [ -n "$PYVER" ]; then
    sudo apt install -y "python${PYVER}-venv" python3-venv 2>/dev/null || true
fi

# --- 1.5 Setup Phidgets OS Drivers (Legacy C Library) ---
echo -e "\n[1.5/6] Setting up Phidget21 C Library from local tarball..."

echo " -> Checking for existing libphidget21 installation..."
# Safely check the file paths directly rather than relying on ldconfig in the user PATH
if [ -f "/usr/local/lib/libphidget21.so" ] || [ -f "/usr/lib/libphidget21.so" ]; then
    echo " -> libphidget21.so is already installed. Skipping compilation."
else
    echo " -> libphidget21.so not found. Beginning extraction and compilation..."
    TAR_PATH="./libs/libphidget_2.1.9.20190409.tar.gz"

    if [ -f "$TAR_PATH" ]; then
        echo " -> Extracting $TAR_PATH..."
        tar -zxf "$TAR_PATH" -C ./libs/
        
        # Navigate into the extracted directory
        cd ./libs/libphidget-2.1.9.20190409 || { echo " -> [ERROR] Failed to navigate into extracted Phidget directory."; exit 1; }
        
        # Each step is checked. Without this, a configure that aborted with
        # "Missing libusb!" was followed by "No targets specified and no
        # makefile found", then "No rule to make target 'install'", and the
        # script still printed "installed successfully" - so the real failure
        # scrolled past three screens above a success message.
        echo " -> Configuring and compiling (with JNI disabled)..."
        if ! ./configure --disable-jni; then
            echo " -> [ERROR] Phidget configure failed. The usual cause is a"
            echo " ->         missing libusb; this script installs"
            echo " ->         libusb-1.0-0-dev and libusb-dev in step 1, so if"
            echo " ->         that step was skipped or failed, fix it first."
            cd "$REPO_ROOT" || exit 1
            PHIDGET_OK=0
        elif ! make; then
            echo " -> [ERROR] Phidget compile failed. See the output above."
            cd "$REPO_ROOT" || exit 1
            PHIDGET_OK=0
        else
            echo " -> Installing to system and updating dynamic linker bindings..."
            if sudo make install; then
                PHIDGET_OK=1
            else
                echo " -> [ERROR] 'make install' failed for libphidget21."
                PHIDGET_OK=0
            fi
        fi
        # Force the absolute path to ldconfig so it correctly links the new library
        sudo /sbin/ldconfig

        # Clean up the extracted source folder to keep the repo tidy
        cd "$REPO_ROOT" || exit 1
        rm -rf ./libs/libphidget-2.1.9.20190409

        # Claim success only if the library is actually there now.
        if [ "${PHIDGET_OK:-0}" -eq 1 ] && \
           { [ -f "/usr/local/lib/libphidget21.so" ] || [ -f "/usr/lib/libphidget21.so" ]; }; then
            echo " -> libphidget21.so installed successfully."
        else
            echo " -> [WARNING] libphidget21.so was NOT installed. The piezo"
            echo " ->           D2A will not work. Everything else is fine."
        fi
    else
        echo " -> [ERROR] Tarball not found at $TAR_PATH!"
        echo " -> Please ensure libphidget_2.1.9.20190409.tar.gz is placed in the ./libs/ directory."
        exit 1
    fi
fi

# --- 1.7 Setup NI-DAQmx (the only route to NI X Series cards) ---
echo -e "\n[1.7/6] Checking NI-DAQmx..."

# comedi's ni_pcimio driver covers the NI E and M series and no X Series board,
# so a PCIe-6351 - or any 635x/636x - is unreachable through comedi and needs
# NI's own driver.
#
# 'ni-daqmx' is NOT in Ubuntu's archives, which is why 'apt install ni-daqmx'
# fails with "Unable to locate package" on a clean machine. NI ships a small
# (~31 KB) registration .deb that adds their apt feed, and the real driver
# comes from that feed. Both halves are automated here so this stays the
# one-liner it is supposed to be.
#
# Env overrides:
#   PSCAN_SKIP_DAQMX=1    never install it
#   PSCAN_FORCE_DAQMX=1   install even with no NI card currently seated
#   NI_RELEASE=2025Q1     pin the release whose zip is fetched

install_nidaqmx() {
    if have_lib libnidaqmx; then
        echo " -> NI-DAQmx is already installed. Skipping."
        return 0
    fi

    if [ -n "${PSCAN_SKIP_DAQMX:-}" ]; then
        echo " -> PSCAN_SKIP_DAQMX is set. Skipping."
        return 0
    fi

    # Don't pull a large vendor driver with kernel modules onto a machine that
    # has no NI card in it. A comedi-only rig needs none of this.
    if [ -z "${PSCAN_FORCE_DAQMX:-}" ]; then
        if ! lspci 2>/dev/null | grep -qi "National Instruments"; then
            echo " -> No NI PCI/PCIe card found by lspci, so NI-DAQmx is not"
            echo " -> being installed. comedi cards are unaffected."
            echo " -> Seat the card and re-run this script, or force it with:"
            echo " ->   PSCAN_FORCE_DAQMX=1 bash install.sh"
            return 0
        fi
        echo " -> NI card detected by lspci:"
        lspci 2>/dev/null | grep -i "National Instruments" | sed 's/^/ ->   /'
    fi

    local distro release ubu
    distro=$(lsb_release -is 2>/dev/null || echo unknown)
    release=$(lsb_release -rs 2>/dev/null || echo unknown)
    ubu=$(echo "$release" | tr -d .)

    # NI supports Ubuntu, RHEL and openSUSE. Debian is NOT supported, and
    # installing the Ubuntu feed on Debian is a real risk, not a formality:
    # the feed targets jammy, so apt can later try to satisfy Debian packages
    # from it and damage the system. That is a decision for a person, not an
    # installer, so it needs an explicit opt-in.
    if [ "$distro" != "Ubuntu" ]; then
        if [ -z "${PSCAN_FORCE_DAQMX:-}" ]; then
            echo " -> [WARNING] This machine is '$distro $release'. NI supports"
            echo " ->           Ubuntu, RHEL and openSUSE - NOT Debian - and"
            echo " ->           there is no Debian registration package."
            echo " ->"
            echo " ->           NI-DAQmx is therefore NOT installed, which"
            echo " ->           means NI X Series cards (your PCIe-6351) cannot"
            echo " ->           be reached on this OS. comedi cards are fine."
            echo " ->"
            echo " ->           The supported fix is Ubuntu 22.04 or 24.04 on"
            echo " ->           this machine. To try the Ubuntu packages here"
            echo " ->           anyway - unsupported, and it can leave apt"
            echo " ->           pulling jammy libraries over Debian ones:"
            echo " ->             PSCAN_FORCE_DAQMX=1 bash install.sh"
            echo " ->           Pin the feed to NI packages only if you do."
            return 1
        fi

        echo " -> [WARNING] '$distro $release' is not supported by NI."
        echo " ->           PSCAN_FORCE_DAQMX is set, so trying the Ubuntu"
        echo " ->           22.04 packages. If apt starts proposing to replace"
        echo " ->           system libraries, abort and remove"
        echo " ->           /etc/apt/sources.list.d/ni-*.list."
        # Closest supported Ubuntu LTS to a Debian base.
        ubu="2204"
    fi

    sudo apt install -y curl unzip

    # Every release's zip carries a '-stream' registration package pointing at
    # NI's rolling feed, so even an older zip yields current drivers. Probed
    # newest-first, so this keeps working as NI publishes new releases without
    # anyone editing this list.
    local candidates="${NI_RELEASE:-}"
    if [ -z "$candidates" ]; then
        candidates="2026Q4 2026Q3 2026Q2 2026Q1 2025Q4 2025Q3 2025Q2 2025Q1"
        candidates="$candidates 2024Q4 2024Q3 2024Q2 2024Q1 2023Q4 2023Q3 2023Q2"
    fi

    local base="https://download.ni.com/support/softlib/MasterRepository"
    local url="" found_rel="" rel="" try=""

    echo " -> Looking for the newest NI Linux driver release..."
    for rel in $candidates; do
        try="$base/LinuxDrivers$rel/NILinux${rel}DeviceDrivers.zip"
        # A one-byte range request rather than HEAD: more widely honoured,
        # and enough to prove the file is there.
        if curl -fsL --max-time 20 -r 0-0 -o /dev/null "$try" 2>/dev/null; then
            url="$try"
            found_rel="$rel"
            break
        fi
    done

    if [ -z "$url" ]; then
        echo " -> [ERROR] Could not reach any NI driver release at $base."
        echo " ->         Check network access, or download the registration"
        echo " ->         package by hand from:"
        echo " ->   https://www.ni.com/en/support/downloads/drivers/download.ni-linux-device-drivers.html"
        return 1
    fi
    echo " -> Found release $found_rel."

    # Its own directory, and nothing is executed out of it - the zip is
    # third-party content and only the one .deb we name is taken from it.
    local workdir
    workdir=$(mktemp -d)

    if ! curl -fsL --max-time 300 -o "$workdir/ni.zip" "$url"; then
        echo " -> [ERROR] Download failed: $url"
        rm -rf "$workdir"
        return 1
    fi

    local deb="ni-ubuntu${ubu}-drivers-stream.deb"
    if ! unzip -l "$workdir/ni.zip" | grep -q "$deb"; then
        echo " -> [ERROR] Release $found_rel has no registration package for"
        echo " ->         Ubuntu $release. It contains:"
        unzip -l "$workdir/ni.zip" | grep -o 'ni-ubuntu[0-9]*' | sort -u | sed 's/^/ ->           /'
        echo " ->         Pin a newer release with NI_RELEASE=<rel>, or"
        echo " ->         install by hand."
        rm -rf "$workdir"
        return 1
    fi

    echo " -> Registering NI's apt feed ($deb)..."
    unzip -j -o "$workdir/ni.zip" "$deb" -d "$workdir" >/dev/null
    sudo apt install -y "$workdir/$deb"

    echo " -> Installing the driver from NI's feed..."
    sudo apt update
    sudo apt install -y ni-daqmx

    echo " -> Building kernel modules for the running kernel..."
    sudo dkms autoinstall

    rm -rf "$workdir"

    echo " ->"
    echo " -> NI-DAQmx installed. A REBOOT IS REQUIRED before the card is"
    echo " -> usable. After rebooting, check it with:  pydaq list"
    NI_DAQMX_NEEDS_REBOOT=1
    return 0
}

install_nidaqmx || echo " -> [WARNING] NI-DAQmx setup did not complete; see above."

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

if [ "$DRIVERS_ONLY" -eq 1 ]; then
    echo -e "\n======================================================="
    echo " Shared system drivers done."
    if [ -n "${NI_DAQMX_NEEDS_REBOOT:-}" ]; then
        echo ""
        echo " *** REBOOT REQUIRED: NI-DAQmx was installed this run and its"
        echo " *** kernel modules are not loaded yet."
    fi
    echo "======================================================="
    exit 0
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

# pscan/daq used to be a package directory and is now pscan/daq.py. If the old
# directory survives - git leaves it behind when it still holds untracked files
# such as __pycache__ - find_packages() picks up 'pscan.daq' again and pip
# installs both. Python resolves a regular package ahead of a module of the
# same name, so the stale __init__.py wins and the new daq.py is invisible,
# surfacing as an AttributeError nowhere near its cause.
for stale in pscan/daq pscan/scan_axes.py; do
    if [ -e "$stale" ]; then
        echo " -> Removing stale $stale (pre-consolidation layout)."
        sudo rm -rf "$stale"
    fi
done
find . -name '__pycache__' -type d -prune -exec sudo rm -rf {} + 2>/dev/null || true

# --- 5. Exorcise Ghost Installations ---
echo -e "\n[5/6] Removing existing or broken installations..."
# Both package names: 'pscan-lab-dev' was the separate experimental build,
# which no longer exists. Leaving it installed would put a second copy of the
# 'pscan' package in site-packages, and whichever pip wrote last would own the
# files - the exact fragmentation this consolidation removes.
for pkg in pscan-lab pscan-lab-dev; do
    sudo pip uninstall -y "$pkg" $FLAG 2>/dev/null
    pip uninstall -y "$pkg" $FLAG 2>/dev/null
done

# Brute-force delete any lingering files, including the old venv and its
# -dev command symlinks.
rm -rf ~/.local/lib/python*/site-packages/pscan*
rm -rf ~/.local/share/pscan
rm -f ~/.local/bin/pscan ~/.local/bin/pscan ~/.local/bin/pystage \
      ~/.local/bin/pypiezo ~/.local/bin/pydaq ~/.local/bin/flyscan
sudo rm -rf /usr/local/lib/python*/dist-packages/pscan*
sudo rm -f /usr/local/bin/pscan

# --- 6. Clean Installation ---
echo -e "\n[6/6] Compiling and installing pscan..."

# Two passes, deliberately.
#
# The first installs the dependencies (numpy, opencv, pyserial, vxi11 ...).
# This step used to be --no-deps only, which meant those were never installed
# at all - they happened to be present because the retired venv pulled them,
# so a system install would come up and then fail on 'import cv2'.
#
# The second forces just pscan itself, so a code change always takes even when
# the version number has not moved. Forcing everything would reinstall 90 MB
# of wheels on every run for no reason.
sudo pip install --no-cache-dir . $FLAG
sudo pip install --force-reinstall --no-deps --no-cache-dir . $FLAG

# The nidaqmx Python binding, only when the driver it wraps is actually
# present - 'pydaq list' reporting the backend as available when there is no
# driver underneath it would be worse than reporting nothing.
if have_lib libnidaqmx; then
    echo " -> NI-DAQmx driver present: installing the nidaqmx Python package."
    sudo pip install --no-cache-dir 'nidaqmx>=0.9' $FLAG
fi

# --- Verify, from a directory that is NOT the repo ---
#
# `python -c` prepends the working directory to sys.path, so run from here
# `import pscan` would resolve to ./pscan/ in the source tree and this would
# validate the code it just installed from rather than the installation. That
# mistake made an earlier version of this check pass against a site-packages
# that was broken. Hence the cd.
echo -e "\nVerifying the installation..."
if ( cd / && python3 -c '
import sys
import pscan, pscan.daq as daq

print(f"    pscan      -> {pscan.__file__}")
print(f"    pscan.daq  -> {daq.__file__}")

problems = []
if not (daq.__file__ or "").endswith("daq.py"):
    problems.append("pscan.daq is a package directory, not daq.py - a stale "
                    "pscan/daq/ is shadowing it")
for attr in ("backend_names", "open_device", "close_all", "resolve_range"):
    if not hasattr(daq, attr):
        problems.append(f"pscan.daq is missing {attr}()")
for mod in ("pscan.main", "pscan.a2d_driver", "pscan.daqmx_driver",
            "pscan.control_daq", "pscan.config_parser", "flyscan.cli"):
    try:
        __import__(mod)
    except Exception as e:
        problems.append(f"{mod}: {type(e).__name__}: {e}")

for p in problems:
    print(f" -> [ERROR] {p}")
sys.exit(1 if problems else 0)
' ); then
    echo " -> Installation verified."
    INSTALL_OK=1
else
    echo " -> [ERROR] The installation is not usable (see above)."
    INSTALL_OK=0
fi

echo -e "\n======================================================="
if [ "${INSTALL_OK:-0}" -eq 1 ]; then
    echo " DONE! Run 'pscan' | 'pystage' | 'pypiezo' | 'pydaq' |"
    echo " 'flyscan' from anywhere."
    echo ""
    echo " 'pydaq list' shows which DAQ cards pscan can reach and"
    echo " under which backend - start there when bringing up a card."
else
    echo " INSTALL FAILED - see the errors above. The commands may"
    echo " exist but will not work."
fi
if [ -n "${NI_DAQMX_NEEDS_REBOOT:-}" ]; then
    echo ""
    echo " *** REBOOT REQUIRED: NI-DAQmx was installed this run and its"
    echo " *** kernel modules are not loaded yet. No NI X Series card will"
    echo " *** be visible until you reboot."
fi
echo "======================================================="
[ "${INSTALL_OK:-0}" -eq 1 ] || exit 1
