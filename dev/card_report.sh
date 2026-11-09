#!/bin/bash
#
# Everything we know how to ask an X Series card, in one run, to one file.
#
# WHY
#
# Comparing two cards is only worth anything if both were asked the same
# questions the same way. Doing that by hand is five commands across two
# tools, one of which needs an environment variable and a decimal PCI address
# that differs per slot - and a single difference in how the two runs were
# taken is enough to make the comparison worthless without anyone noticing.
#
# It is also the attachment for a fault report. The evidence that matters to
# NI comes from NI's own boardBringup and aiex1, not from pscan, so those are
# run first and in full.
#
# USAGE
#
#   sudo bash dev/card_report.sh                 -> dev/card_report.txt
#   sudo bash dev/card_report.sh serial02507F2F  -> dev/card_report_serial02507F2F.txt
#
# Naming the run after the card's serial is worth the extra typing: two files
# called "before" and "after" stop meaning anything within a week.
#
# Nothing here writes to the card beyond a single analog output value, and
# nothing here needs anything wired to the terminal block - every input test
# uses a signal internal to the chip. Measuring the analog OUTPUT does need a
# meter and a human, so this prints the DAC code it wrote and tells you the
# command to hold the voltage; it does not block waiting for you.

set -u

LABEL="${1:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/dev/card_report${LABEL:+_$LABEL}.txt"

# Section headers, so the two files diff cleanly and a human can find a step.
section() {
    echo ""
    echo "============================================================"
    echo "  $*"
    echo "============================================================"
}

# Run a command, record it and its exit status, and never abort the report on
# a failure - a failing step IS the result here. Timed out rather than trusted
# to return: a card in a bad state can hang a register read indefinitely, and
# a report that never finishes tells us nothing.
run() {
    local what="$1"; shift
    echo ""
    echo "--- $what"
    echo "\$ $*"
    timeout 120 "$@" 2>&1
    local rc=$?
    if [ $rc -eq 124 ]; then
        echo "[TIMED OUT after 120s]"
    else
        echo "[exit $rc]"
    fi
    return 0
}

if [ "$(id -u)" -ne 0 ]; then
    echo "This needs root: the card is reached through /dev/vfio." >&2
    echo "  sudo bash dev/card_report.sh ${LABEL}" >&2
    exit 1
fi

# --- find the card -------------------------------------------------------
# By PCI vendor 0x1093 (National Instruments). The device id is generic
# across the whole X Series family, so the SUBSYSTEM id is what names a
# model - the same reason nixseries_driver.py reads subsystem_device.
SLOT=""
for dev in /sys/bus/pci/devices/*; do
    [ -r "$dev/vendor" ] || continue
    if [ "$(cat "$dev/vendor")" = "0x1093" ]; then
        SLOT="$(basename "$dev")"
        break
    fi
done

if [ -z "$SLOT" ]; then
    echo "No National Instruments card found on the PCI bus (vendor 0x1093)."
    echo "If one is fitted, it is not being enumerated - which is itself the"
    echo "finding. Record 'lspci -nn' and stop here."
    exit 1
fi

# The DDK examples take bus and device in DECIMAL; sysfs names them in hex.
# 0000:82:00.0 is "130 0". Getting this wrong opens a different card, or
# nothing at all, and looks exactly like a missing card.
BUS_HEX="${SLOT#*:}"; BUS_HEX="${BUS_HEX%%:*}"
DEV_HEX="${SLOT##*:}"; DEV_HEX="${DEV_HEX%%.*}"
BUS_DEC=$((16#$BUS_HEX))
DEV_DEC=$((16#$DEV_HEX))

DDK="$(find "$REPO_ROOT/libs/vendor/build" -maxdepth 3 -type d -name ChipObjects \
       -print -quit 2>/dev/null)"
DDK="${DDK%/ChipObjects}"

{
section "WHAT AND WHEN"
echo "label          ${LABEL:-(none given)}"
echo "date           $(date -Is)"
echo "host           $(hostname)"
echo "pscan commit   $(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "pscan installed $(python3 -c 'import pscan,os;print(os.path.dirname(pscan.__file__))' 2>/dev/null || echo 'not importable')"
echo ""
echo "NOTE: pscan installs as a copy, not editable. If the path above is not"
echo "      under this repo, 'git pull' alone did NOT update what just ran."

section "MACHINE"
echo "kernel         $(uname -r)"
echo "distro         $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo -n "secure boot    "
mokutil --sb-state 2>/dev/null | head -1 || echo "(mokutil not installed)"
echo -n "lockdown       "
cat /sys/kernel/security/lockdown 2>/dev/null || echo "(not exposed)"

section "CARD IDENTITY"
echo "slot           $SLOT   (DDK examples want: $BUS_DEC $DEV_DEC)"
echo "vendor         $(cat "/sys/bus/pci/devices/$SLOT/vendor")"
echo "device         $(cat "/sys/bus/pci/devices/$SLOT/device")"
echo "subsystem      $(cat "/sys/bus/pci/devices/$SLOT/subsystem_device")   <- this names the model"
echo "driver         $(basename "$(readlink -f "/sys/bus/pci/devices/$SLOT/driver" 2>/dev/null)" 2>/dev/null || echo '(none bound)')"
echo "iommu group    $(basename "$(readlink -f "/sys/bus/pci/devices/$SLOT/iommu_group" 2>/dev/null)" 2>/dev/null || echo '(none)')"
run "lspci" lspci -nnv -s "$SLOT"

# --- NI's own tools, first and in full -----------------------------------
# These matter most because none of our code is in them. A fault they show is
# a fault NI cannot attribute to pscan.
section "NI'S OWN DDK EXAMPLES (no pscan code involved)"
if [ -n "$DDK" ] && [ -x "$DDK/boardBringup" ]; then
    echo "built from     ${DDK#$REPO_ROOT/}"
    run "boardBringup - signatures, EEPROM, calibration, temperature, self test" \
        "$DDK/boardBringup" "$BUS_DEC" "$DEV_DEC"
    [ -x "$DDK/aiex1" ] && \
        run "aiex1 - the simplest possible analog input read" \
            "$DDK/aiex1" "$BUS_DEC" "$DEV_DEC"
else
    echo "NOT BUILT. These are the most important part of this report - they"
    echo "are NI's own code, so a fault they show cannot be blamed on pscan."
    echo "Build them with:"
    echo "    bash dev/build_ddk_examples.sh"
fi

# --- pscan's view --------------------------------------------------------
section "PSCAN: WHAT IT SEES"
run "pydaq list" pydaq list --all

section "PSCAN: ANALOG INPUT"
echo "The self test reads the chip's INTERNAL temperature sensor through the"
echo "same ADC, timing engine and FIFO a real measurement uses. Nothing need"
echo "be wired up, and nothing external can affect it - so a failure here is"
echo "below channel selection and below the terminal block."
run "pydaq selftest" \
    env PSCAN_XSERIES_DEBUG=1 pydaq selftest --backend nixseries
run "pydaq read ai0 - 1000 samples at 10 kHz" \
    env PSCAN_XSERIES_DEBUG=1 pydaq read --backend nixseries --channel 0 \
        --samples 1000 --rate 10000 --out /tmp/card_report_ai0.d

section "PSCAN: ANALOG OUTPUT"
echo "The debug line below gives the DAC code written. Check it against the"
echo "DAC coefficients boardBringup printed above: code = c0 + c1 * volts."
echo "A matching code means the software is correct and anything wrong at the"
echo "connector is the card."
run "pydaq set ao0 to +2.5V" \
    env PSCAN_XSERIES_DEBUG=1 pydaq set 2.5 --backend nixseries --channel 0

section "ANALOG OUTPUT: NEEDS A METER"
cat <<'EOF'
Not done here, because it needs a human and a multimeter, and because the
output cannot outlive the process: vfio-pci resets the card when its last
file descriptor closes, which zeroes the DACs.

  sudo PSCAN_XSERIES_DEBUG=1 pydaq set 2.5 --backend nixseries --channel 0 --hold

Measure on the AO 0 BNC - centre pin against the shell, which is AO GND.
It should read a steady +2.5 V the instant the hold message appears. An X
Series DAC settles in microseconds, so anything that drifts, creeps or takes
a visible moment to arrive is not a settled output, whatever its value.

Record what the meter said at the bottom of this file.
EOF

section "END"
echo "Written $(date -Is)"
} 2>&1 | tee "$OUT"

echo ""
echo "Report written to ${OUT#"$REPO_ROOT"/}"
echo ""
echo "To compare two cards:"
echo "    diff dev/card_report_<first>.txt dev/card_report_<second>.txt"
