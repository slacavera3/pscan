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

# ---------------------------------------------------------------------------
# PROPRIETARY SDK DROP-BOX
#
# Some drivers pscan needs cannot live in this repository: NI's X Series DDK
# and Tucsen's camera SDK are both distributed under vendor licences that do
# not permit redistribution. The repo stays public, so instead of vendoring
# them, this script asks for them.
#
# Every missing download is COLLECTED and reported together at the end, with
# its URL and the exact filename to save. Reporting them one at a time would
# mean a download, a re-run, another request, another download - which is the
# kind of drip-feed that makes a setup feel endless.
#
# Drop files in libs/vendor/ (gitignored) and re-run this script.
# ---------------------------------------------------------------------------
VENDOR_DIR="$REPO_ROOT/libs/vendor"
mkdir -p "$VENDOR_DIR" 2>/dev/null || true
VENDOR_MISSING=0
VENDOR_REQUESTS=""

# First file the USER dropped under libs/vendor/ matching a glob, or empty.
#
# maxdepth 6 because a vendor SDK extracted in place is several levels deep -
# the Tucsen tarball lands its library at
# TUCamSample_ubuntu22.04_.../sdk/lib/libTUCam.so.1.0.0, which is four levels
# down. A shallower limit silently fails to find a file that is sitting right
# there, which is worse than not looking.
#
# libs/vendor/build/ is excluded because it is OUR extraction output, not a
# user drop, and it gets wiped at the start of every build. Without this
# exclusion the search finds last run's unpacked copy, the wipe deletes it,
# and the build then reports that it cannot find what it just found.
vendor_find() {
    find "$VENDOR_DIR" -maxdepth 6 -path "$VENDOR_DIR/build" -prune -o \
         -iname "$1" -print 2>/dev/null | head -n 1
}

# request_vendor <label> <expected-filename> <url> <what-it-is-for>
request_vendor() {
    VENDOR_MISSING=$((VENDOR_MISSING + 1))
    VENDOR_REQUESTS="${VENDOR_REQUESTS}
  [$VENDOR_MISSING] $1
      Needed for : $4
      Download   : $3
      Save as    : libs/vendor/$2
"
}

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
    build-essential dkms \
    python3-pip
if [ -n "$PYVER" ]; then
    sudo apt install -y "python${PYVER}-venv" python3-venv 2>/dev/null || true
fi

# Kernel headers, in their OWN apt call. Bundled with the line above, a
# package name that does not exist on this kernel makes apt refuse the whole
# transaction - so a missing headers package would silently take libcomedi,
# libusb and build-essential down with it, and the failure would surface
# somewhere unrelated much later.
KVER="$(uname -r)"
if [ -d "/lib/modules/$KVER/build" ]; then
    echo " -> Kernel build tree present for $KVER."
else
    echo " -> Installing kernel headers for $KVER..."
    sudo apt install -y "linux-headers-$KVER" 2>/dev/null \
        || sudo apt install -y linux-headers-amd64 2>/dev/null \
        || true
    if [ ! -d "/lib/modules/$KVER/build" ]; then
        echo " -> [WARNING] No kernel build tree at /lib/modules/$KVER/build."
        echo " ->           Out-of-tree modules cannot be built, which means"
        echo " ->           nirlpk.ko - and so the NI X Series card - will be"
        echo " ->           unavailable. Install the headers matching your"
        echo " ->           RUNNING kernel:"
        echo " ->             sudo apt install linux-headers-$KVER"
        echo " ->           If apt has no such package, you are running a"
        echo " ->           kernel the archive no longer ships headers for;"
        echo " ->           reboot into the current one first."
    fi
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

# Bind NI X Series cards to vfio-pci, persistently, and make /dev/vfio
# reachable without root.
#
# Two pieces, both needed:
#
#   modprobe.d  binds any 1093:c4c4 card to vfio-pci at boot. 0xc4c4 is the
#               generic X Series PCI device id - every 632x/634x/635x/636x/
#               637x reports it - so this claims X Series cards and nothing
#               else. comedi cards have different ids and are untouched,
#               which matters because comedi has no X Series support anyway.
#
#   udev        gives the 'pscan' group access to /dev/vfio/*. Without it a
#               scan needs root, which is a poor thing to require of
#               everyday use.
setup_vfio_binding() {
    echo " -> Setting up vfio-pci binding..."

    if ! modinfo vfio-pci >/dev/null 2>&1; then
        echo " -> [WARNING] No vfio-pci module on this kernel. The X Series"
        echo " ->           card cannot be reached without it."
        return 1
    fi

    # Does this kernel's vfio-pci actually take an 'ids' parameter?
    #
    # This used to be assumed. If it is wrong, the options file written below
    # makes EVERY later modprobe fail with "unknown parameter" - including the
    # one at boot - so the config we wrote to make binding work is what stops
    # it. The first install still succeeds, because the first modprobe runs
    # before the file exists, which makes it look like a reboot broke it.
    local ids_supported=0
    if modinfo -p vfio-pci 2>/dev/null | grep -q '^ids:'; then
        ids_supported=1
    else
        echo " -> This kernel's vfio-pci has no 'ids' parameter."
        echo " -> Binding by driver_override instead."
        sudo rm -f /etc/modprobe.d/pscan-vfio.conf
    fi

    # NOT silenced. A failure here is the whole story - an unloaded vfio-pci
    # means driver_override and drivers_probe bind the card to nothing, with
    # no symptom except a missing /dev/vfio/<group> much later.
    local modprobe_out
    if ! modprobe_out="$(sudo modprobe vfio-pci 2>&1)"; then
        echo " -> [ERROR] 'modprobe vfio-pci' FAILED:"
        printf ' ->           %s\n' "$modprobe_out"
        echo " ->"
        echo " ->           pscan cannot reach an X Series card without it."
        return 1
    fi

    # Confirm the driver registered, rather than trusting modprobe's status.
    if [ ! -d /sys/bus/pci/drivers/vfio-pci ]; then
        echo " -> [ERROR] vfio-pci loaded but did not register as a PCI"
        echo " ->         driver (/sys/bus/pci/drivers/vfio-pci is absent)."
        echo " ->         Nothing can be bound to it."
        return 1
    fi

    # An IOMMU is required: it is what makes lockdown willing to allow
    # userspace device access at all.
    if [ ! -d /sys/kernel/iommu_groups ] || \
       [ -z "$(ls -A /sys/kernel/iommu_groups 2>/dev/null)" ]; then
        echo " -> [WARNING] No IOMMU groups, so vfio-pci cannot be used."
        echo " ->           Add 'intel_iommu=on' to GRUB_CMDLINE_LINUX_DEFAULT"
        echo " ->           in /etc/default/grub, run 'sudo update-grub' and"
        echo " ->           reboot. (VT-d must also be on in firmware, which"
        echo " ->           it usually is by default.)"
        return 1
    fi

    if [ "$ids_supported" -eq 1 ]; then
        echo 'options vfio-pci ids=1093:c4c4' \
            | sudo tee /etc/modprobe.d/pscan-vfio.conf >/dev/null
        echo " -> /etc/modprobe.d/pscan-vfio.conf written."
    fi

    # modprobe.d only says HOW vfio-pci behaves when something loads it. It
    # does not load it. Without this second file the binding works until the
    # first reboot and then silently stops, which is a miserable thing to
    # debug months later - /dev/vfio/<group> simply is not there, and the
    # card looks unbound for no visible reason.
    #
    # Loading it is enough to bind: the ids= option above goes through
    # pci_add_dynid(), which attaches the driver to matching devices that are
    # already present and unbound. No native driver claims an X Series card on
    # Debian, so it will be unbound and waiting.
    echo 'vfio-pci' | sudo tee /etc/modules-load.d/pscan-vfio.conf >/dev/null
    echo " -> /etc/modules-load.d/pscan-vfio.conf written (loads it at boot)."

    sudo groupadd -f pscan 2>/dev/null || true
    sudo tee /etc/udev/rules.d/99-pscan-vfio.rules >/dev/null <<'UDEV'
# pscan: let the 'pscan' group reach VFIO devices, so a scan does not need
# root. Written by install.sh.
SUBSYSTEM=="vfio", GROUP="pscan", MODE="0660"
UDEV
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger --subsystem-match=vfio 2>/dev/null || true
    echo " -> udev rule written; /dev/vfio is group 'pscan'."

    # Bind any card that is present now, so a reboot is not required.
    for dev in /sys/bus/pci/devices/*; do
        [ -f "$dev/vendor" ] || continue
        [ "$(cat "$dev/vendor" 2>/dev/null)" = "0x1093" ] || continue

        slot="$(basename "$dev")"
        current="$(basename "$(readlink "$dev/driver" 2>/dev/null)" 2>/dev/null)"

        if [ "$current" = "vfio-pci" ]; then
            echo " -> $slot already bound to vfio-pci."
            continue
        fi
        if [ -n "$current" ]; then
            echo " -> $slot is bound to '$current'; leaving it alone."
            continue
        fi

        echo vfio-pci | sudo tee "$dev/driver_override" >/dev/null
        echo "$slot" | sudo tee /sys/bus/pci/drivers_probe >/dev/null
        now="$(basename "$(readlink "$dev/driver" 2>/dev/null)" 2>/dev/null)"
        if [ "$now" = "vfio-pci" ]; then
            echo " -> $slot bound to vfio-pci."
        else
            # Said loudly: an unbound card produces no /dev/vfio/<group>, so
            # the only symptom later is "No such file or directory" on a path
            # nobody expects to have to create.
            echo " -> [WARNING] $slot did NOT bind (driver is now"
            echo " ->           '${now:-none}'). pscan cannot reach the card."
            echo " ->           Check: /sbin/lsmod | grep vfio_pci"
        fi
    done

    # Group membership only takes effect at next login, which is a confusing
    # thing to discover later rather than now.
    target_user="${SUDO_USER:-$USER}"
    if id -nG "$target_user" 2>/dev/null | tr ' ' '\n' | grep -qx pscan; then
        echo " -> $target_user is already in the 'pscan' group."
    else
        sudo usermod -aG pscan "$target_user" 2>/dev/null || true
        echo " -> Added $target_user to the 'pscan' group."
        echo " -> [NOTE] LOG OUT AND BACK IN for that to take effect. Until"
        echo " ->        then pydaq and pscan need sudo for this card."
        VFIO_NEEDS_RELOGIN=1
    fi
    return 0
}

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

# --- 1.8 Setup NI X Series DDK (the route to X Series cards on any distro) ---
echo -e "\n[1.8/6] Checking NI X Series DDK..."

# This is how an NI X Series card (PCIe-635x, e.g. the PCIe-6351) is reached
# on a distro NI-DAQmx does not support - which is every distro but Ubuntu,
# RHEL and openSUSE.
#
# NI's MHDDK documents the DAQ-STC3 registers and ships source for them, and
# NI describes it as the only Linux support they offer for custom drivers. We
# build its X Series chip objects into a small shared library and bind that
# with ctypes, exactly as this repo already does for libcomedi, libphidget21
# and libTUCam.
#
# Crucially it needs NO kernel module: the DDK's "LinuxDevMem" OS interface
# mmaps the card's PCI BARs from userspace, and the aiex2 example shows
# hardware-timed finite acquisition read straight out of the AI FIFO. DMA is
# unavailable that way, which does not matter at pscan's data rate - 1000
# samples at 10 kHz is 20 KB/s.
#
# Two downloads, both free but licence-restricted, so neither is vendored.
# The OS interface arrives under several names depending on which flavour was
# downloaded. An ARCHIVE is looked for first and a pre-extracted directory
# only as a fallback, because the two are handled differently below: an
# archive is unpacked into build/, a directory is used where it sits.
# Conflating them is what broke this before.
XSERIES_TARBALL="$(vendor_find 'nixseries*.tar.gz')"

XSERIES_OSI_ARCHIVE="$(vendor_find '*kernelmodule*.tar.gz')"
[ -n "$XSERIES_OSI_ARCHIVE" ] || XSERIES_OSI_ARCHIVE="$(vendor_find '*osinterface*.tar.gz')"
[ -n "$XSERIES_OSI_ARCHIVE" ] || XSERIES_OSI_ARCHIVE="$(vendor_find '*nimhddk*.tar.gz')"
[ -n "$XSERIES_OSI_ARCHIVE" ] || XSERIES_OSI_ARCHIVE="$(vendor_find '*Linux*Kernel*.tar.gz')"

# Only if no archive: a tree the user unpacked themselves, identified by the
# one header every flavour has.
XSERIES_OSI_DIR=""
if [ -z "$XSERIES_OSI_ARCHIVE" ]; then
    _osi_header="$(vendor_find 'osiBus.h')"
    [ -n "$_osi_header" ] && XSERIES_OSI_DIR="${_osi_header%/osiBus.h}"
fi

XSERIES_OSI="${XSERIES_OSI_ARCHIVE:-$XSERIES_OSI_DIR}"

if [ -z "$XSERIES_TARBALL" ]; then
    echo " -> X Series DDK source not found; will request it below."
    request_vendor "NI X Series DDK (MHDDK)" "nixseries.tar.gz" \
        "https://www.ni.com/en/support/downloads/drivers/download.ni-measurement-hardware-ddk.html" \
        "reading the PCIe-6351 without NI-DAQmx"
else
    echo " -> X Series DDK source found: ${XSERIES_TARBALL#$REPO_ROOT/}"
fi

if [ -z "$XSERIES_OSI" ]; then
    echo " -> X Series OS interface not found; will request it below."
    request_vendor "MHDDK Linux OS interface - 'Linux (via kernel module)' VERSION 2.0" \
        "osinterface-linux-kernelmodule-v2.tar.gz" \
        "https://www.ni.com/en/support/downloads/drivers/download.ni-measurement-hardware-ddk.html" \
        "the DDK's PCI access layer. Take the 64-bit-capable kernel-module flavour: the /dev/mem one is 32-bit only"
else
    echo " -> X Series OS interface found: ${XSERIES_OSI#$REPO_ROOT/}"
fi

# Build the shim when both halves are present. Extracted into libs/vendor/ so
# the vendor source and our one file stay visibly separate, and so a re-run
# rebuilds against whatever was dropped there rather than a stale tree.
if [ -n "$XSERIES_TARBALL" ] && [ -n "$XSERIES_OSI" ]; then
    XS_BUILD="$VENDOR_DIR/build"
    rm -rf "$XS_BUILD"
    mkdir -p "$XS_BUILD"

    echo " -> Extracting the DDK..."
    tar -xzf "$XSERIES_TARBALL" -C "$XS_BUILD" || true

    # The tarball may unpack to nixseries/ or straight into the directory.
    XS_DDK="$(find "$XS_BUILD" -maxdepth 3 -type d -name ChipObjects -print -quit 2>/dev/null)"
    XS_DDK="${XS_DDK%/ChipObjects}"

    # An archive is unpacked into build/; a directory the user extracted
    # themselves is used in place and never copied, so nothing here can
    # delete it.
    if [ -n "$XSERIES_OSI_ARCHIVE" ]; then
        echo " -> Extracting the OS interface..."
        case "$XSERIES_OSI_ARCHIVE" in
            *.tar.gz|*.tgz) tar -xzf "$XSERIES_OSI_ARCHIVE" -C "$XS_BUILD" || true ;;
            *.zip)          unzip -q -o "$XSERIES_OSI_ARCHIVE" -d "$XS_BUILD" || true ;;
        esac
        XS_OSI="$(find "$XS_BUILD" -maxdepth 4 -name osiBus.h -print -quit 2>/dev/null)"
        XS_OSI="${XS_OSI%/osiBus.h}"
    else
        XS_OSI="$XSERIES_OSI_DIR"
    fi

    if [ -z "$XS_DDK" ] || [ -z "$XS_OSI" ]; then
        echo " -> [WARNING] Could not locate the DDK and OS interface after"
        echo " ->           extraction (looked for ChipObjects/ and osiBus.h)."
        echo " ->           The X Series backend will be unavailable."
    else
        # The kernel-module OS interface needs nirlpk.ko built for the
        # running kernel and loaded, or acquireBoard() finds no device however
        # well the shim compiled. NI ships it as BSD-licensed source, so it
        # builds here rather than coming from a vendor package.
        XS_NIRLPK="$(find "$XS_BUILD" "$VENDOR_DIR" -maxdepth 5 -type d -name nirlpk -print -quit 2>/dev/null)"
        # Only when explicitly asked for. The shim reaches the card through
        # vfio-pci, an in-tree signed driver, so building NI's 2011 nirlpk.ko
        # against a modern kernel is effort spent on something nothing loads -
        # and under kernel lockdown it could not be loaded anyway, being
        # unsigned. PSCAN_XSERIES_USE_NIRLPK=1 builds it and switches the shim
        # over, which is the route to DMA if that is ever wanted.
        if [ -z "${PSCAN_XSERIES_USE_NIRLPK:-}" ]; then
            XS_NIRLPK=""
            echo " -> Using vfio-pci (no kernel module; works under lockdown)."
            setup_vfio_binding
        fi

        if [ -n "$XS_NIRLPK" ]; then
            if /sbin/lsmod 2>/dev/null | grep -q '^nirlpk'; then
                echo " -> nirlpk.ko already loaded."
            elif [ ! -d "/lib/modules/$(uname -r)/build" ]; then
                echo " -> [WARNING] Skipping nirlpk.ko: no kernel build tree at"
                echo " ->           /lib/modules/$(uname -r)/build. Its configure"
                echo " ->           reports this as 'Unable to find kernel"
                echo " ->           sources'. See the headers warning in step 1."
            else
                echo " -> Building nirlpk.ko for kernel $(uname -r)..."
                # KERNELDIR is exported as well as passed: the DDK's configure
                # is 2011-era and reads it from the environment, while its
                # Makefile may take it as a variable.
                if KERNELDIR="/lib/modules/$(uname -r)/build" \
                   make -C "$XS_NIRLPK" KERNELDIR="/lib/modules/$(uname -r)/build" \
                   && sudo make -C "$XS_NIRLPK" install; then
                    echo " -> nirlpk.ko installed and loaded."
                else
                    echo " -> [WARNING] nirlpk.ko did not build. It is 2011-era"
                    echo " ->           source against a $(uname -r) kernel, so"
                    echo " ->           this is the step most likely to need"
                    echo " ->           patching. It is BSD-licensed, so it can"
                    echo " ->           be fixed rather than worked around."
                fi
            fi
        fi

        echo " -> Building libpscan_xseries.so..."
        echo " ->   DDK: ${XS_DDK#$REPO_ROOT/}"
        echo " ->   OSI: ${XS_OSI#$REPO_ROOT/}"
        XS_USE_VFIO=1
        [ -z "${PSCAN_XSERIES_USE_NIRLPK:-}" ] || XS_USE_VFIO=0

        # The SAME variables go to both invocations. Passing them only to the
        # build left `make install` resolving OSI_DIR to its default, which
        # pointed at a directory that does not exist - and the define harvest
        # then ran grep with no file, which reads stdin and hangs make before
        # it prints anything.
        XS_MAKEVARS="NIXSERIES_DIR=$XS_DDK OSI_DIR=$XS_OSI USE_VFIO=$XS_USE_VFIO"

        if make -C "$REPO_ROOT/libs/xseries_shim" $XS_MAKEVARS && \
           sudo make -C "$REPO_ROOT/libs/xseries_shim" $XS_MAKEVARS install; then
            echo " -> libpscan_xseries.so installed."
        else
            echo " -> [WARNING] The X Series shim did not build. The card will"
            echo " ->           be unreachable; everything else is unaffected."
            echo " ->           This shim has not yet been compiled anywhere,"
            echo " ->           so a first-time failure here is expected to"
            echo " ->           need fixing rather than re-running."
        fi
    fi
fi

# --- 2. Setup Tucsen SDK Drivers ---
echo -e "\n[2/6] Setting up Tucsen Dhyana SDK..."
TUCSEN_DIR="/opt/tucsen/sdk/lib"
sudo mkdir -p $TUCSEN_DIR

if [ -f "$TUCSEN_DIR/libTUCam.so" ]; then
    echo " -> Tucsen driver already installed at $TUCSEN_DIR."
else
    # libs/vendor/ first, then anywhere in the tree - the latter so an older
    # checkout that still carries the committed SDK keeps working.
    LOCAL_LIB="$(vendor_find 'libTUCam.so*')"
    [ -n "$LOCAL_LIB" ] || LOCAL_LIB=$(find "$REPO_ROOT" -name "libTUCam.so" 2>/dev/null | head -n 1)

    if [ -n "$LOCAL_LIB" ]; then
        echo " -> Found Tucsen driver at ${LOCAL_LIB#$REPO_ROOT/}"
        echo " -> Copying to $TUCSEN_DIR..."
        sudo cp "$LOCAL_LIB" $TUCSEN_DIR/libTUCam.so
        sudo chmod 755 $TUCSEN_DIR/libTUCam.so
    else
        echo " -> Tucsen SDK not found; will request it below."
        request_vendor "Tucsen Dhyana camera SDK" \
            "TUCamSample_ubuntu*.tar.gz (or libTUCam.so on its own)" \
            "https://www.tucsen.com/download/" \
            "the Tucsen camera ('action tucsen')"
    fi
fi

# Printed once, at the end, listing every proprietary download still needed.
# Called from both exit paths.
report_vendor_requests() {
    [ "$VENDOR_MISSING" -eq 0 ] && return 0

    echo ""
    echo "======================================================="
    echo " MANUAL STEP: $VENDOR_MISSING PROPRIETARY DOWNLOAD(S) NEEDED"
    echo "======================================================="
    echo " These are free but their licences do not allow this"
    echo " repository to redistribute them, so they cannot be"
    echo " committed. Download each one, save it under"
    echo " libs/vendor/ with the filename shown, and re-run:"
    echo ""
    echo "     bash install.sh"
    echo "${VENDOR_REQUESTS}"
    echo " libs/vendor/ is gitignored, so nothing you put there"
    echo " will be committed."
    echo ""
    echo " Everything NOT dependent on these is already installed."
    echo " Only the hardware each one drives is unavailable."
    echo "======================================================="
    return 0
}

if [ "$DRIVERS_ONLY" -eq 1 ]; then
    report_vendor_requests
    echo -e "\n======================================================="
    echo " Shared system drivers done."
    echo ""
    # Said loudly because the failure it prevents is silent and expensive.
    # pscan is installed as a COPY (pip install .), not in editable mode, so
    # the pscan that runs is the installed one, never the working tree. After
    # a git pull, --drivers-only rebuilds the C shim but leaves the Python
    # exactly as it was - and the resulting half-new system misreports which
    # code is under test, which is worse than not having updated at all.
    echo " *** The PYTHON side was NOT touched. 'pscan' and 'pydaq' still run"
    echo " *** the code from the last full install, whatever git says."
    echo " ***"
    echo " *** Pulled a change to a .py file? Run 'bash install.sh' instead."
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

report_vendor_requests

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
if [ -n "${VFIO_NEEDS_RELOGIN:-}" ]; then
    echo ""
    echo " *** LOG OUT AND BACK IN: you were added to the 'pscan' group so"
    echo " *** the NI X Series card can be used without sudo. Group changes"
    echo " *** only apply to new logins."
fi
echo "======================================================="
[ "${INSTALL_OK:-0}" -eq 1 ] || exit 1
