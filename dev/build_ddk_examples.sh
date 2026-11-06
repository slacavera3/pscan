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

# The DDK's Makefile hardcodes a Windows/VISA OS interface, which is the one
# thing overridden here.
#
# CXXFLAGS has to be given in FULL, not added to. A variable set on make's
# command line overrides the makefiles' own assignments - including their
# '+=' lines - so passing just the extra flags silently dropped
# LinuxKernel.mak's '-c -DkLittleEndian=1 -DkGNU=1' and the DDK Makefile's
# '-DkBAR0Only=1 -Dk64BitKernel=1'. Losing -c made it try to LINK each
# translation unit, and losing the defines brought back osiTypes.h's
# "#error target endianness not specified".
#
# -w -fpermissive are the additions: NI's headers have a genuine 32 << 27
# overflow in tDO.h that modern GCC rejects outright.
#
# DMA_LIBRARY_DIR is left at its default. Emptying it to skip the DMA
# examples also skipped dma.mak, which is what puts CHInCh on the include
# path - and streamHelper.h includes CHInCh/dmaProperties.h unconditionally,
# so even the non-DMA examples need it.
DDK_CXXFLAGS="-c -O2 -w -fpermissive"
DDK_CXXFLAGS="$DDK_CXXFLAGS -DkLittleEndian=1 -DkGNU=1"
DDK_CXXFLAGS="$DDK_CXXFLAGS -DkBAR0Only=1 -Dk64BitKernel=1"

echo " -> building (this takes a minute)..."

# pipefail so the status below is make's, not tail's. Without it this
# reported success after a failed build - the same mistake install.sh used
# to make with the Phidget compile.
set -o pipefail

if make -C "$DDK" \
        OSINTERFACE_DIR="$OSI" \
        OSINTERFACE_MAKEFILE=LinuxKernel.mak \
        CXXFLAGS="$DDK_CXXFLAGS" \
        2>&1 | tail -30
then
    # Claim success only for binaries that actually exist. The build can
    # report 0 while a target is missing, and a "Built." that is not true
    # wastes the next command.
    built=""
    missing=""
    for exe in boardBringup aiex1 aiex2; do
        if [ -x "$DDK/$exe" ]; then built="$built $exe"; else missing="$missing $exe"; fi
    done

    if [ -n "$built" ]; then
        echo ""
        echo "Built:$built"
        [ -n "$missing" ] && echo "NOT built:$missing"
        echo ""
        echo "The examples take the PCI bus and device in DECIMAL, so the"
        echo "card at 82:00.0 is '130 0':"
        echo ""
        echo "    cd ${DDK#$REPO_ROOT/}"
        echo "    sudo ./boardBringup 130 0   # signature, EEPROM, temperature"
        echo "    sudo ./aiex1 130 0          # simplest possible AI read"
        echo ""
        echo "boardBringup is the one that matters. A sane temperature means"
        echo "the card and our bus layer are fine and the bug is ours."
    else
        echo ""
        echo "make reported success but produced no executables:$missing"
        echo "Check the output above."
    fi
else
    echo ""
    echo "BUILD FAILED - see the errors above. No executables to run."
fi

echo ""
echo "To put NI's nirlpk version back:"
echo "    cp '$OSI_USERCODE.nirlpk' '$OSI_USERCODE'"
