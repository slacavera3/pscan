"""Ask the scope what it is actually set to, without changing anything.

Checklist item 12b: flyscan sets SEQ, TDIV and MSIZ and verifies them, but
never sets or reads the trigger - source, slope, level, coupling, holdoff
are all inherited from whatever the front panel was last left on. Every
pixel of a fly scan is one segment and one trigger, so those settings decide
the whole position-to-sample mapping.

Run it before a scan to record the inherited state, and with --after-arm to
see what survives flyscan's own `TRMD SINGLE; ARM`.

    python -m flyscan.scope_probe 128.243.74.139
    python -m flyscan.scope_probe 128.243.74.139 --after-arm

Read-only by default: the only writes are CHDR OFF, so replies parse
predictably, and the arming sequence under --after-arm.
"""

import argparse
import sys

import vxi11

# (query, what it tells you). TRSE is the one that matters most: it carries
# the source AND the holdoff, either of which would explain a record that
# does not start where you think it does.
QUERIES = [
    ("*IDN?",      "model and firmware"),
    ("TRSE?",      "trigger type, SOURCE, holdoff type and value"),
    ("TRMD?",      "trigger mode - AUTO self-triggers when no trigger comes"),
    ("TRDL?",      "trigger delay: where the trigger sits in the record"),
    ("C1:TRLV?",   "trigger level on C1"),
    ("C1:TRSL?",   "trigger slope on C1"),
    ("C1:TRCP?",   "trigger coupling on C1"),
    ("SEQ?",       "sequence mode, segment count, points per segment"),
    ("TDIV?",      "timebase per division"),
    ("MSIZ?",      "memory size"),
    ("SARA?",      "ACTUAL sample rate - compare with the config"),
    ("C1:VDIV?",   "vertical scale on C1"),
    ("C1:OFST?",   "vertical offset on C1"),
    ("C1:CPL?",    "input coupling on C1"),
]


DIVISIONS = 10          # LeCroy timebases are per-division


def _number(reply):
    """First float in a reply like '5E-6' or '1.25E+3', or None."""
    import re
    match = re.search(r'[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?', reply or "")
    return float(match.group(0)) if match else None


def probe(instr, label):
    print(f"\n=== {label} " + "=" * max(0, 60 - len(label)))
    width = max(len(q) for q, _ in QUERIES)
    replies = {}
    for query, meaning in QUERIES:
        try:
            instr.write(query)
            reply = instr.read().strip()
        except Exception as e:                      # noqa: BLE001
            reply = f"<no reply: {type(e).__name__}>"
        replies[query] = reply
        print(f"  {query:<{width}}  {reply}")
        print(f"  {'':<{width}}  ({meaning})")

    # Not every model answers SARA. TDIV and MSIZ are universal and give the
    # same number, so derive it rather than leaving the question open. The
    # authoritative value is the wavedesc's horiz_interval on the returned
    # waveform, which download_line() already checks against the request.
    tdiv = _number(replies.get("TDIV?"))
    msiz = _number(replies.get("MSIZ?"))
    if tdiv and msiz:
        print(f"\n  derived sample rate   {msiz / (tdiv * DIVISIONS) / 1e6:.3f}"
              f" MS/s   (MSIZ / (TDIV x {DIVISIONS}))")
        print(f"  derived window        {tdiv * DIVISIONS * 1e6:.2f} us")

    trmd = replies.get("TRMD?", "")
    if "AUTO" in trmd.upper():
        print("\n  *** TRMD is AUTO. The scope self-triggers when no real "
              "trigger\n      arrives, which decouples segments from the "
              "signal. A sequence\n      must fill in SINGLE or NORM. ***")
    return replies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ip", help="scope IP address")
    parser.add_argument("--after-arm", action="store_true",
                        help="also arm as flyscan does and re-read, to see "
                             "what the arming sequence actually leaves set")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    instr = vxi11.Instrument(args.ip)
    instr.timeout = args.timeout
    try:
        instr.write("CHDR OFF")
        probe(instr, "as found")

        if args.after_arm:
            # Exactly what arm_line() sends, so the readback reflects the
            # state a real line is acquired in. SEQ is left as found.
            instr.write("CLSW")
            instr.write("TRMD SINGLE")
            instr.write("ARM")
            after = probe(instr, "after CLSW; TRMD SINGLE; ARM")

            trmd = after.get("TRMD?", "").upper()
            print()
            if "SINGLE" in trmd or "STOP" in trmd:
                print("  TRMD stuck as SINGLE: arming works, and segments "
                      "really are\n  waiting on triggers. Look elsewhere for "
                      "the drift.")
            else:
                print(f"  *** TRMD came back {trmd!r}, not SINGLE. The arming "
                      f"sequence is\n      not taking, so every segment the "
                      f"scope cannot trigger is\n      self-triggered at its "
                      f"own pace. That is the drift. ***")
            print("\n  NOTE: the scope is now armed and waiting. Send "
                  "'TRMD AUTO' to free it.")
    finally:
        instr.close()

    print("""
What to look for
----------------
  TRSE source   If this is not C1, the record does not start on the signal
                you are watching, and a 'drifting' edge is simply the
                scope triggering off something else.
  TRSE holdoff  A holdoff in time or events changes WHICH edge triggers.
                It cannot move the edge within the record, but it does
                change how many triggers per second you get.
  TRMD          Must be SINGLE (or STOP) while a sequence fills. AUTO means
                the scope self-triggers when no trigger arrives, which
                decouples segments from the signal entirely - exactly the
                failure that makes a self-triggered square wave drift.
  SARA          If this is not the sample_rate in the config, every time
                axis computed so far is wrong by that ratio.
  TRDL          A non-zero trigger delay is fine and fixed; it shifts every
                segment by the same amount, so it cannot cause drift.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
