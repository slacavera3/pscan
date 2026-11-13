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
# --calibrate runs the counts-per-mm calibration at the end of the install.
# OFF by default, and deliberately so: calibration moves the stage, and an
# installer that drives hardware nobody asked it to touch - possibly with a
# sample mounted - is a bad trade for the convenience. A normal install only
# reports whether a calibration exists.
#
# Every driver step is idempotent, so re-running this costs seconds.
DRIVERS_ONLY=0
CALIBRATE=0
for _arg in "$@"; do
    case "$_arg" in
        --drivers-only)
            DRIVERS_ONLY=1
            echo " MODE: shared system drivers only (no Python packages)"
            ;;
        --calibrate)
            CALIBRATE=1
            echo " MODE: will calibrate counts-per-mm after installing"
            ;;
        --help|-h)
            echo "usage: bash install.sh [--drivers-only] [--calibrate]"
            echo "  --drivers-only  system drivers only, no Python packages"
            echo "  --calibrate     after installing, measure counts-per-mm."
            echo "                  MOVES THE STAGE. Clear the sample first."
            exit 0
            ;;
        *)
            echo " [WARNING] ignoring unknown argument '$_arg'"
            ;;
    esac
done

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

# Hand any NI card back from vfio-pci to whichever driver claims it.
#
# Needed because the two owners are exclusive. Once a device is bound to
# vfio-pci nothing else can see it - not NI's driver, not lspci's "Kernel
# driver in use" showing anything else - so a machine set up for the shim and
# then given NI-DAQmx ends up with a working driver and an invisible card,
# with nothing in either tool's output pointing at the cause.
#
# Removes the persistence first, so a reboot does not simply undo this.
release_card_from_vfio() {
    local changed=0

    for f in /etc/modprobe.d/pscan-vfio.conf /etc/modules-load.d/pscan-vfio.conf; do
        if [ -f "$f" ]; then
            sudo rm -f "$f"
            echo " ->   removed $f"
            changed=1
        fi
    done

    for dev in /sys/bus/pci/devices/*; do
        [ -r "$dev/vendor" ] || continue
        [ "$(cat "$dev/vendor" 2>/dev/null)" = "0x1093" ] || continue

        local slot current
        slot="$(basename "$dev")"
        current="$(basename "$(readlink "$dev/driver" 2>/dev/null)" 2>/dev/null)"

        [ "$current" = "vfio-pci" ] || continue

        echo " ->   releasing $slot from vfio-pci..."
        echo "$slot" | sudo tee "$dev/driver/unbind" >/dev/null 2>&1 || true
        # Clear the override, or drivers_probe just hands it back to vfio-pci.
        echo "" | sudo tee "$dev/driver_override" >/dev/null 2>&1 || true
        echo "$slot" | sudo tee /sys/bus/pci/drivers_probe >/dev/null 2>&1 || true

        current="$(basename "$(readlink "$dev/driver" 2>/dev/null)" 2>/dev/null)"
        if [ -n "$current" ] && [ "$current" != "vfio-pci" ]; then
            echo " ->   $slot is now driven by '$current'."
        else
            echo " ->   $slot is not bound to anything yet. NI's modules are"
            echo " ->   loaded at boot, so REBOOT and check with 'pydaq list'."
            NI_DAQMX_NEEDS_REBOOT=1
        fi
        changed=1
    done

    [ "$changed" -eq 1 ] || echo " ->   nothing to release."
    return 0
}

# Put the NI card's DMA into IOMMU passthrough, persistently.
#
# WHY THIS IS AUTOMATED RATHER THAN DOCUMENTED
#
# Debian builds with CONFIG_INTEL_IOMMU_DEFAULT_ON, so a stock install
# translates every device's DMA. NI's kernel driver does not use the kernel
# DMA API the way that expects, so buffered acquisition fails with "-50152 A
# hardware failure has occurred" while single samples and analog output work
# - only buffered acquisition needs DMA. The card looks half-broken in a way
# that takes days to attribute.
#
# The alternative to doing this here is telling a new user to hand-edit
# /etc/default/grub, which is far more dangerous than a script that backs the
# file up, makes one idempotent change, validates the result and rolls back
# if anything is wrong. Editing boot configuration badly is the one change in
# this installer that can stop a machine booting; that is an argument for
# doing it carefully, not for delegating it to someone with nano.
#
# PSCAN_NO_GRUB=1 skips it, for sites that manage boot configuration
# centrally. The warning then says what to add.
ensure_iommu_passthrough() {
    local grub=/etc/default/grub

    # Only when this card's own IOMMU group is actually translating.
    local slot="" gid="" type=""
    for g in /sys/bus/pci/devices/*/iommu_group; do
        [ -e "$g" ] || continue
        slot="$(basename "$(dirname "$g")")"
        [ "$(cat "/sys/bus/pci/devices/$slot/vendor" 2>/dev/null)" = "0x1093" ] \
            || { slot=""; continue; }
        gid="$(basename "$(readlink -f "$g")")"
        type="$(cat "/sys/kernel/iommu_groups/$gid/type" 2>/dev/null)"
        break
    done

    [ -n "$slot" ] || return 0
    case "$type" in
        DMA*) ;;
        *) echo " ->   IOMMU group $gid is '$type' - DMA is not translated."
           return 0 ;;
    esac

    echo " ->   $slot is in IOMMU group $gid, type '$type': its DMA is"
    echo " ->   translated, which breaks NI's buffered acquisition."

    if [ -n "${PSCAN_NO_GRUB:-}" ]; then
        echo " ->   PSCAN_NO_GRUB is set. Add this yourself and reboot:"
        echo " ->     GRUB_CMDLINE_LINUX_DEFAULT=\"... iommu=pt\""
        return 0
    fi

    if [ ! -f "$grub" ]; then
        echo " ->   [WARNING] No $grub, so this cannot be set automatically."
        echo " ->             Add 'iommu=pt' to the kernel command line."
        return 0
    fi

    if grep -q 'iommu=pt' "$grub"; then
        echo " ->   iommu=pt is already in $grub but not in force yet."
        echo " ->   A REBOOT is needed for it to take effect."
        IOMMU_NEEDS_REBOOT=1
        return 0
    fi

    sudo cp -a "$grub" "$grub.pscan-backup"
    echo " ->   Backed up $grub to $grub.pscan-backup"

    if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$grub"; then
        sudo sed -i -E \
            's/^(GRUB_CMDLINE_LINUX_DEFAULT=")([^"]*)"/\1\2 iommu=pt"/' "$grub"
    else
        echo 'GRUB_CMDLINE_LINUX_DEFAULT="iommu=pt"' | sudo tee -a "$grub" >/dev/null
    fi

    # Validate by SOURCING it, which is what grub-mkconfig does. A file that
    # no longer parses, or where the edit did not land, must not reach
    # update-grub.
    if ! ( . "$grub" >/dev/null 2>&1 && \
           case "${GRUB_CMDLINE_LINUX_DEFAULT:-}" in *iommu=pt*) exit 0;; esac
           exit 1 ); then
        echo " ->   [ERROR] The edit did not produce a valid $grub. Restoring."
        sudo cp -a "$grub.pscan-backup" "$grub"
        echo " ->           Add 'iommu=pt' by hand and reboot."
        return 1
    fi

    echo " ->   Set: $(grep '^GRUB_CMDLINE_LINUX_DEFAULT=' "$grub")"

    if ! sudo update-grub >/dev/null 2>&1; then
        echo " ->   [ERROR] update-grub failed. Restoring $grub."
        sudo cp -a "$grub.pscan-backup" "$grub"
        sudo update-grub >/dev/null 2>&1 || true
        return 1
    fi

    echo " ->   update-grub done."
    IOMMU_NEEDS_REBOOT=1
    IOMMU_GROUP_ID="$gid"
    return 0
}

# Did NI's kernel modules actually build, and will Secure Boot let them load?
#
# This is the step that decides whether the DAQmx route works on a locked-down
# machine, and it fails silently otherwise: dkms reports success, the modules
# sit on disk unsigned, and modprobe refuses them at boot with nothing in the
# installer's output to say why.
check_daqmx_modules() {
    echo " -> Checking NI's kernel modules..."

    local built
    built=$(sudo dkms status 2>/dev/null | grep -ci 'ni' || true)
    echo " ->   dkms knows about $built NI module(s)."

    local secureboot="unknown"
    if command -v mokutil >/dev/null 2>&1; then
        if mokutil --sb-state 2>/dev/null | grep -qi enabled; then
            secureboot="enabled"
        else
            secureboot="disabled"
        fi
    fi
    echo " ->   Secure Boot: $secureboot"

    if [ "$secureboot" != "enabled" ]; then
        return 0
    fi

    # Debian's dkms signs what it builds with a machine-owner key and leaves
    # the certificate in /var/lib/dkms. The key is useless until it is
    # enrolled, which must happen at boot with someone physically present -
    # that requirement IS Secure Boot, and no installer can work around it.
    #
    # The certificate's name has moved between Debian releases, so find it
    # rather than hardcode one. Telling a user to import a path that does not
    # exist is worse than saying nothing: this printed 'mok.der' while
    # checking for 'mok.pub', and the command failed in their hands.
    local cert=""
    local candidate
    for candidate in /var/lib/dkms/mok.pub /var/lib/dkms/mok.der \
                     /var/lib/shim-signed/mok/MOK.der; do
        [ -f "$candidate" ] && { cert="$candidate"; break; }
    done

    if [ -z "$cert" ]; then
        echo " ->"
        echo " ->   Secure Boot is on, and no DKMS signing certificate exists"
        echo " ->   yet. It is created when dkms first builds a module, so"
        echo " ->   this usually means nothing was built. Check:"
        echo " ->     sudo dkms status"
        echo " ->     sudo dmesg | grep -i 'module verification\\|key rejected'"
        return 0
    fi

    # Already enrolled? Then there is nothing to do and nothing to say.
    if command -v mokutil >/dev/null 2>&1 && \
       sudo mokutil --test-key "$cert" 2>/dev/null | grep -qi 'already enrolled'; then
        echo " ->   DKMS signing key already enrolled. Modules will load."
        return 0
    fi

    echo " ->"
    echo " ->   Secure Boot is on, so NI's modules must be signed by a key"
    echo " ->   this machine trusts. DKMS has signed them with:"
    echo " ->     $cert"
    echo " ->   That key needs enrolling ONCE on this machine, ever."
    echo " ->"
    echo " ->   You will be asked to invent a password now, and to type it"
    echo " ->   again on a blue screen at the next boot. It is not stored"
    echo " ->   and you never need it again. No BIOS change."
    echo " ->"
    printf " ->   Enrol it now? [y/N] "
    local answer=""
    read -r answer </dev/tty 2>/dev/null || answer=""

    case "$answer" in
        [yY]*)
            if sudo mokutil --import "$cert"; then
                MOK_ENROLMENT_PENDING=1
                echo " ->   Enrolment queued. It takes effect at the next boot."
            else
                echo " ->   [WARNING] mokutil --import failed. Run it by hand:"
                echo " ->     sudo mokutil --import $cert"
            fi
            ;;
        *)
            echo " ->   Skipped. NI's modules will NOT load until you run:"
            echo " ->     sudo mokutil --import $cert"
            ;;
    esac
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
            echo " ->           NI-DAQmx is therefore NOT installed. The X"
            echo " ->           Series shim (section 1.8) is the fallback."
            echo " ->           comedi cards are unaffected either way."
            echo " ->"
            echo " ->           'Not supported' is not the same as 'will not"
            echo " ->           work': NI's packages are .debs, DKMS builds"
            echo " ->           their modules against whatever kernel is here,"
            echo " ->           and the feed is pinned so it cannot replace"
            echo " ->           Debian packages. It is worth trying:"
            echo " ->             PSCAN_FORCE_DAQMX=1 bash install.sh"
            echo " ->"
            echo " ->           With Secure Boot on you will need to enrol a"
            echo " ->           DKMS signing key once; this script says how"
            echo " ->           after it builds."
            return 1
        fi

        echo " -> [WARNING] '$distro $release' is not supported by NI."
        echo " ->           PSCAN_FORCE_DAQMX is set, so trying NI's Ubuntu"
        echo " ->           packages. The apt feed is pinned below so it"
        echo " ->           cannot replace Debian system libraries."

        # If comedi currently drives an NI card here, say so before changing
        # anything. Two kernel drivers cannot bind the same PCI function:
        # ni_pcimio and NI's own driver both claim NI boards, and whichever
        # binds first owns it. The loser's card simply is not there - comedi
        # finds cards through /dev/comediN, which stops existing if NI's
        # driver won.
        #
        # Not a warning against doing it, and not predicting which wins:
        # stated because 'pydaq list' afterwards is the only way to know, and
        # a card that has quietly changed hands looks like a card that has
        # failed. The same mistake as the vfio-pci binding, which cost a day.
        if ls /dev/comedi0 >/dev/null 2>&1 && \
           grep -qls '^0x1093$' /sys/bus/pci/devices/*/vendor 2>/dev/null; then
            echo " ->"
            echo " ->           NOTE: comedi currently has a device node and an"
            echo " ->           NI card is present. Both drivers claim NI"
            echo " ->           boards and only one can own a card, so check"
            echo " ->           with 'pydaq list' afterwards which one has it."
        fi

        # Ubuntu 24.04 (noble), not 22.04 (jammy).
        #
        # The closer the libc, the fewer surprises: Debian 13 ships glibc
        # ~2.41, noble 2.39, jammy 2.35. Jammy was picked here when "closest
        # supported LTS" was a guess rather than a comparison.
        #
        # NI_UBUNTU=2204 overrides, since which releases NI publishes for
        # changes over time.
        ubu="${NI_UBUNTU:-2404}"
        echo " ->           Targeting Ubuntu $ubu packages."
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

    # Find the package by its FULL PATH inside the zip, not by its basename.
    #
    # NI nest it in a directory, so the bare name matches when grepping the
    # listing but is not a valid extraction pattern - unzip wants the entry's
    # whole path. That mismatch made this step report
    #
    #   caution: filename not matched:  ni-ubuntu2404-drivers-stream.deb
    #
    # and then hand apt a file that was never extracted:
    #
    #   Error: Unsupported file /tmp/.../ni-ubuntu2404-drivers-stream.deb
    #
    # The feed was therefore never registered, which is the whole reason
    # 'apt install ni-daqmx' then could not find the package. One bug, and
    # every later failure followed from it.
    #
    # 'unzip -Z1' lists entry paths alone, so the search and the extraction
    # now use exactly the same string and cannot disagree again.
    local deb="ni-ubuntu${ubu}-drivers-stream.deb"
    local deb_path
    deb_path=$(unzip -Z1 "$workdir/ni.zip" 2>/dev/null | grep -m1 -F "$deb")

    if [ -z "$deb_path" ]; then
        echo " -> [ERROR] Release $found_rel has no registration package for"
        echo " ->         Ubuntu $release. It contains:"
        unzip -l "$workdir/ni.zip" | grep -o 'ni-ubuntu[0-9]*' | sort -u | sed 's/^/ ->           /'
        echo " ->         Pin a newer release with NI_RELEASE=<rel>, or"
        echo " ->         install by hand."
        rm -rf "$workdir"
        return 1
    fi

    # Pin NI's feed BEFORE registering it.
    #
    # This is the part that makes putting an Ubuntu feed on Debian safe
    # rather than reckless, and it was described in the warning above long
    # before it was implemented. Priority 100 means "install from here only
    # when nothing else provides this package": NI's own ni-* packages still
    # install, because Debian has no competing version, while anything NI
    # ships that Debian also ships - libstdc++, libpython, kernel headers -
    # loses to Debian's at priority 500.
    #
    # Without this, a later 'apt upgrade' can quietly start pulling Ubuntu
    # system libraries over Debian ones, which is how an unsupported-but-
    # working machine becomes an unbootable one.
    echo " -> Pinning NI's feed so it cannot replace Debian packages..."
    sudo tee /etc/apt/preferences.d/pscan-ni-pin >/dev/null <<'PIN'
# Written by pscan's install.sh.
# NI's feed is built for Ubuntu. On Debian it must supply NI's own packages
# and nothing else, or apt will offer to replace system libraries with
# Ubuntu builds. Priority 100 = only when no other source provides it.
Package: *
Pin: origin download.ni.com
Pin-Priority: 100
PIN

    echo " -> Registering NI's apt feed ($deb_path)..."
    unzip -j -o "$workdir/ni.zip" "$deb_path" -d "$workdir" >/dev/null

    # -j junks the directory, so it lands under its basename. Check it is
    # really there: the previous version went straight on to apt with a path
    # that did not exist, and the only clue was a 'caution:' line from unzip
    # that scrolled past.
    if [ ! -s "$workdir/$deb" ]; then
        echo " -> [ERROR] Extracting $deb_path produced nothing."
        echo " ->         The zip lists it, so this is a pattern problem, not"
        echo " ->         a missing file. Inspect with:"
        echo " ->           unzip -Z1 $workdir/ni.zip | grep ni-ubuntu"
        rm -rf "$workdir"
        return 1
    fi

    if ! sudo apt install -y "$workdir/$deb"; then
        echo " -> [ERROR] Installing the registration package failed."
        echo " ->         Without it NI's apt feed is not configured, so"
        echo " ->         'apt install ni-daqmx' cannot work."
        rm -rf "$workdir"
        return 1
    fi

    echo " -> Installing the driver from NI's feed..."
    sudo apt update
    if ! sudo apt install -y ni-daqmx; then
        echo " -> [ERROR] 'apt install ni-daqmx' failed."
        echo " ->         Check what the feed actually offers:"
        echo " ->           apt-cache search '^ni-' | head -30"
        echo " ->           apt-cache policy ni-daqmx"
        echo " ->         If it is a dependency on an Ubuntu-only package,"
        echo " ->         that name is the thing to work around - try"
        echo " ->         NI_UBUNTU=2204 for the older, lighter dependency set."
        rm -rf "$workdir"
        return 1
    fi

    echo " -> Building kernel modules for the running kernel..."
    sudo dkms autoinstall || echo " -> [WARNING] dkms autoinstall reported a failure."

    check_daqmx_modules

    rm -rf "$workdir"

    echo " ->"
    echo " -> NI-DAQmx installed. A REBOOT IS REQUIRED before the card is"
    echo " -> usable. After rebooting, check it with:  pydaq list"
    NI_DAQMX_NEEDS_REBOOT=1
    return 0
}

install_nidaqmx || echo " -> [WARNING] NI-DAQmx setup did not complete; see above."

# --- 1.8 Tidy up after a pre-DAQmx install ---
echo -e "\n[1.8/6] Checking the NI card is reachable..."

# Undo what a pre-DAQmx install of pscan left behind.
#
# Migration, not setup. The X Series shim that needed these was removed once
# NI-DAQmx was shown to work on Debian, but a machine set up for the shim
# keeps its vfio-pci binding across upgrades - and a card bound there is
# invisible to NI's driver. Without this, updating pscan would leave a working
# driver and an unreachable card.
release_card_from_vfio

# The IOMMU half of the same problem - BUT ONLY WHERE NI-DAQmx DRIVES THE
# CARD.
#
# NI's own driver does not use the kernel DMA API the way address translation
# expects, so buffered acquisition fails with "-50152 A hardware failure has
# occurred" while single samples, self test and analog output all work.
# iommu=pt fixes that.
#
# comedi's ni_pcimio does not have the problem: it is an ordinary in-tree
# driver and uses the DMA API properly, so an M Series card on comedi works
# with the IOMMU translating and needs nothing changed.
#
# This was briefly unconditional, and edited /etc/default/grub on a stable
# comedi rig that had no use for it. Editing boot configuration is the most
# intrusive thing this installer does and it has to be earned, not done on
# the off-chance the card might one day be driven by something else.
if have_lib libnidaqmx; then
    for _grp in /sys/bus/pci/devices/*/iommu_group; do
        [ -e "$_grp" ] || continue
        _slot="$(basename "$(dirname "$_grp")")"
        [ "$(cat "/sys/bus/pci/devices/$_slot/vendor" 2>/dev/null)" = "0x1093" ] \
            || continue

        _gid="$(basename "$(readlink -f "$_grp")")"
        _type="$(cat "/sys/kernel/iommu_groups/$_gid/type" 2>/dev/null)"

        case "$_type" in
            DMA*) ensure_iommu_passthrough || true ;;
        esac
        break
    done
elif grep -qls '^0x1093$' /sys/bus/pci/devices/*/vendor 2>/dev/null; then
    # Say nothing is needed, rather than nothing at all. Someone who reads
    # about iommu=pt elsewhere should be able to tell it was considered.
    echo " ->   NI card present but NI-DAQmx is not installed, so comedi"
    echo " ->   drives it. comedi needs no IOMMU change; boot config"
    echo " ->   untouched."
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
#
# Both top-level packages are named explicitly. The glob used to be 'pscan*',
# which does not match 'flyscan', so flyscan/ survived every reinstall: a
# module deleted upstream stayed importable out of site-packages and the
# installed flyscan was a mix of two versions.
for _pkg in pscan flyscan pscan_lab pscan-lab; do
    rm -rf ~/.local/lib/python*/site-packages/"$_pkg"*
    sudo rm -rf /usr/local/lib/python*/dist-packages/"$_pkg"*
done
rm -rf ~/.local/share/pscan

# Every command, under both prefixes. This used to remove ~/.local/bin/pscan
# twice and /usr/local/bin/pscan once, leaving pystage, pypiezo, pydaq and
# flyscan behind in /usr/local/bin.
#
# That is not cosmetic. The .egg-info is deleted just above, so pip no longer
# knows anything is installed; if the install below then fails, those four
# files stay on PATH as executable stubs importing a package that is no longer
# there. 'pscan' says "command not found" while 'pystage' dies with
# ModuleNotFoundError - which reads as a code bug rather than a failed
# install, and sends you looking in the wrong place.
for _cmd in pscan pystage pypiezo pydaq flyscan; do
    rm -f ~/.local/bin/"$_cmd"
    sudo rm -f /usr/local/bin/"$_cmd"
done

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
sudo pip install --no-cache-dir . $FLAG || INSTALL_FAILED=1
sudo pip install --force-reinstall --no-deps --no-cache-dir . $FLAG || INSTALL_FAILED=1

# Stop here rather than carrying on to the verify step.
#
# There is no 'set -e' in this script, so without this check both commands
# above can fail and the run still reaches the summary, which reports the much
# softer "the commands may exist but will not work" - after step [5/6] has
# already deleted the installation that was working five seconds earlier. Say
# plainly that the machine has no pscan on it at all now, because that is the
# state the next command the user types will be in.
if [ -n "${INSTALL_FAILED:-}" ]; then
    echo ""
    echo " *** INSTALL FAILED: pip could not install pscan."
    echo " ***"
    echo " *** Step [5/6] removed the previous installation, so there is NO"
    echo " *** working pscan on this machine right now - pscan, pystage,"
    echo " *** pypiezo, pydaq and flyscan are all gone until this succeeds."
    echo " ***"
    echo " *** Read pip's error above; the usual causes are no network, a"
    echo " *** PEP 668 refusal, or a wheel that has to compile and cannot."
    echo " *** Fix it and re-run 'bash install.sh'. The git tree here is"
    echo " *** untouched, so nothing of yours is lost."
    exit 1
fi

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

# --- Stage calibration ------------------------------------------------------
#
# counts-per-mm belongs to the stage and its controller, not to pscan. The
# hardcoded 34304 is the Z8xx/TDC001 DC-servo figure; a BBD303 driving an
# MLS203 reports 20000. A wrong value rescales every position silently - the
# data look healthy and the microns are wrong - so say plainly whether this
# machine has settled the question.
#
# Not run by default. Calibration moves the stage, and an installer that
# drives hardware nobody asked it to touch, possibly with a sample mounted, is
# a bad trade for the convenience.
if [ "${INSTALL_OK:-0}" -eq 1 ]; then
    # Under sudo, $HOME is root's. The calibration belongs to the person who
    # ran this, so look where they would actually have written it.
    CAL_HOME="$HOME"
    if [ -n "${SUDO_USER:-}" ]; then
        CAL_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    fi
    CAL_USER_FILE="$CAL_HOME/.config/pscan/stage_calibration.json"
    CAL_SYS_FILE="/etc/pscan/stage_calibration.json"

    if [ "$CALIBRATE" -eq 1 ]; then
        echo -e "\nCalibrating counts-per-mm..."
        echo " *** This MOVES THE STAGE. Ctrl-C now if a sample is mounted."
        if [ -n "${SUDO_USER:-}" ]; then
            sudo -u "$SUDO_USER" pystage --calibrate \
                || echo " -> calibration did not complete; run it again later."
        else
            pystage --calibrate \
                || echo " -> calibration did not complete; run it again later."
        fi
    elif [ -s "$CAL_USER_FILE" ] || [ -s "$CAL_SYS_FILE" ]; then
        echo -e "\nStage calibration: present."
    else
        echo -e "\nStage calibration: NONE recorded on this machine."
        echo " -> pscan will use the controller's own cnts_per_unit where it"
        echo " -> reports one, and fall back to 34304 where it does not."
        echo " -> Run 'pystage --calibrate' to settle it against the actual"
        echo " -> stage. It moves the stage, so clear the sample first."
    fi
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
if [ -n "${IOMMU_NEEDS_REBOOT:-}" ]; then
    echo ""
    echo " *** REBOOT REQUIRED: 'iommu=pt' was added to the kernel command"
    echo " *** line so the NI card's DMA is not translated by the IOMMU."
    echo " *** Until you reboot, buffered acquisition will keep failing with"
    echo " *** '-50152 A hardware failure has occurred' while single samples"
    echo " *** and analog output appear to work."
    echo " ***"
    echo " *** After rebooting this should read 'identity':"
    echo " ***   cat /sys/kernel/iommu_groups/${IOMMU_GROUP_ID:-<N>}/type"
    echo " ***"
    echo " *** /etc/default/grub.pscan-backup is the file as it was."
fi
# Last, and in full, because it is the one step nobody can guess and the one
# that cannot be automated: enrolling a key must happen with someone
# physically at the machine, which is the entire point of Secure Boot.
if [ -n "${MOK_ENROLMENT_PENDING:-}" ]; then
    cat <<'MOK'

 *** ON THE NEXT REBOOT, READ THIS SCREEN - do not just press enter.
 ***
 *** A blue screen titled "Shim UEFI key management" or "MOK management"
 *** appears BEFORE Debian starts. It waits about 10 seconds and then
 *** carries on without enrolling, so do not walk away.
 ***
 ***   1. Press any key when it says "Press any key to perform MOK
 ***      management".
 ***   2. Choose  Enroll MOK
 ***   3. Choose  Continue
 ***   4. Choose  Yes
 ***   5. Type the password you just invented, and press enter.
 ***   6. Choose  Reboot
 ***
 *** If you miss it, the machine boots normally and the NI card simply
 *** will not appear. Re-run this installer to queue it again.
 ***
 *** This happens ONCE per machine. It is not asked again, and it is not
 *** a BIOS change.
MOK
fi
echo "======================================================="
[ "${INSTALL_OK:-0}" -eq 1 ] || exit 1
