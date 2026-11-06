#!/bin/bash
#
# Build NI's OWN DDK examples against our VFIO bus layer.
#
# WHY
#
# Our shim's acquisitions return 0x0000 for every sample - including the
# card's internal temperature sensor, so it is nothing to do with wiring -
# while the timing engine, FIFO, register writes and EEPROM calibration all
# demonstrably work. At that point the question is whether the fault is in
# our register sequence or below it, and no amount of reading our own code
# settles that.
#
# NI's examples are the ground truth. They use the same chip objects over the
# same bus layer; only the acquisition logic differs. So:
#
#   boardBringup works -> the card and bus layer are fine, and the bug is in
#                         libs/xseries_shim/xseries_shim.cpp, which can then
#                         be diffed against a working reference
#   boardBringup fails -> the fault is in the bus layer or the card itself,
#                         and our acquisition code is not the place to look
#
# WHAT IT DOES
#
# Copies libs/xseries_shim/osiUserCode_vfio.cpp over NI's nirlpk-based
# osiUserCode.cpp in the extracted OS interface (keeping a backup), then runs
# the DDK's own Makefile. Only files under libs/vendor/build/ are touched,
# and that whole tree is rebuilt by install.sh anyway.
#
# DMA examples are skipped: our bus layer does not implement allocDMA, and
# the examples that need it are not the ones in question.

set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR="$REPO_ROOT/libs/vendor/build"

DDK="$(find "$VENDOR" -maxdepth 3 -type d -name ChipObjects -print -quit 2>/dev/null)"
DDK="${DDK%/ChipObjects}"
OSI="$(find "$VENDOR" -maxdepth 4 -name osiBus.h -print -quit 2>/dev/null)"
OSI="${OSI%/osiBus.h}"

if [ -z "$DDK" ] || [ -z "$OSI" ]; then
    echo "ERROR: could not find the extracted DDK and OS interface under"
    echo "       $VENDOR"
    echo "       Run 'bash install.sh' first - it extracts both."
    exit 1
fi

echo "DDK: ${DDK#$REPO_ROOT/}"
echo "OSI: ${OSI#$REPO_ROOT/}"

OSI_USERCODE="$OSI/LinuxKernel/osiUserCode.cpp"
if [ ! -f "$OSI_USERCODE" ]; then
    echo "ERROR: $OSI_USERCODE not found"
    exit 1
fi

if [ ! -f "$OSI_USERCODE.nirlpk" ]; then
    cp "$OSI_USERCODE" "$OSI_USERCODE.nirlpk"
    echo " -> backed up NI's nirlpk version to osiUserCode.cpp.nirlpk"
fi

cp "$REPO_ROOT/libs/xseries_shim/osiUserCode_vfio.cpp" "$OSI_USERCODE"
echo " -> swapped in our VFIO bus layer"

# The DDK's Makefile hardcodes a Windows/VISA OS interface and the CHInCh DMA
# library; both are overridden here. Its own build rules then apply.
echo " -> building (this takes a minute)..."
if make -C "$DDK" \
        OSINTERFACE_DIR="$OSI" \
        OSINTERFACE_MAKEFILE=LinuxKernel.mak \
        DMA_LIBRARY_DIR= \
        CXXFLAGS="-O2 -w -fpermissive -DkBAR0Only=1 -Dk64BitKernel=1" \
        2>&1 | tail -25
then
    echo ""
    echo "Built. The examples take the PCI bus and device in DECIMAL, so the"
    echo "card at 82:00.0 is '130 0':"
    echo ""
    echo "    cd ${DDK#$REPO_ROOT/}"
    echo "    sudo ./boardBringup 130 0      # self test: signature, EEPROM, temperature"
    echo "    sudo ./aiex1 130 0             # simplest possible AI read"
    echo ""
    echo "boardBringup is the one that matters. If its temperature is sane,"
    echo "the card and our bus layer are fine and the bug is ours."
else
    echo ""
    echo "Build failed - see above. The examples' own Makefile may need"
    echo "different flags than our shim does."
fi

echo ""
echo "To put NI's nirlpk version back:"
echo "    cp '$OSI_USERCODE.nirlpk' '$OSI_USERCODE'"
