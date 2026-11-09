"""No byte-order marks, and no CRLF where it breaks things.

WHY THIS EXISTS

install.sh picked up a UTF-8 BOM from a Windows editor, and Linux refused its
shebang:

    install.sh: line 1: '#!/bin/bash: No such file or directory

It still ran under 'bash install.sh', so for weeks the only symptom was one
odd line at the top of otherwise healthy output - easy to read past while
hunting something else. The same three bytes on a .con file make it fail on
its first directive, naming a keyword that looks exactly right on screen,
because the character is invisible.

This is a file-format problem, not a code problem, so no amount of reading the
code finds it. Hence a test.

The vendor drops under libs/ are excluded: they are not ours to reformat.
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

BOM = b"\xef\xbb\xbf"

#: Where a BOM or a CRLF is not merely untidy but stops the file working.
EXECUTABLE_SUFFIXES = {".sh", ".con", ".bash"}
EXECUTABLE_NAMES = {"Makefile", "makefile", "GNUmakefile"}


def tracked_files():
    """Every file git knows about, so untracked scratch is not policed."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise unittest.SkipTest(f"git unavailable: {e}")
    if out.returncode != 0:
        raise unittest.SkipTest("not a git checkout")

    for name in out.stdout.split(b"\0"):
        if not name:
            continue
        path = REPO / name.decode("utf-8", "replace")
        # Vendor drops are not ours, and libs/vendor is build output.
        parts = path.relative_to(REPO).parts
        if parts[0] == "libs" and len(parts) > 1 and parts[1] != "xseries_shim":
            continue
        if path.is_file():
            yield path


def head(path, n=4096):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


class TestNoByteOrderMarks(unittest.TestCase):

    def test_nothing_tracked_starts_with_a_bom(self):
        offenders = [
            str(p.relative_to(REPO))
            for p in tracked_files()
            if head(p, 3).startswith(BOM)
        ]
        self.assertEqual(
            offenders, [],
            "These files start with a UTF-8 BOM. On a shell script that "
            "breaks the shebang; on a .con file it corrupts the first "
            "directive invisibly. Strip it:\n  "
            + "\n  ".join(offenders)
        )


class TestNoCRLFWhereItBreaks(unittest.TestCase):
    """Python tolerates CRLF. Shell scripts, makefiles and .con files do not.

    A shell script with CRLF fails with 'bad interpreter: No such file or
    directory' - the \\r becomes part of the interpreter path.
    """

    def test_scripts_and_configs_are_lf_only(self):
        offenders = []
        for path in tracked_files():
            if (path.suffix not in EXECUTABLE_SUFFIXES
                    and path.name not in EXECUTABLE_NAMES):
                continue
            if b"\r\n" in head(path):
                offenders.append(str(path.relative_to(REPO)))

        self.assertEqual(
            offenders, [],
            "These scripts/configs have CRLF line endings, which Linux "
            "cannot run:\n  " + "\n  ".join(offenders)
        )


class TestShebangsAreIntact(unittest.TestCase):

    def test_every_shell_script_starts_with_its_shebang(self):
        offenders = []
        for path in tracked_files():
            if path.suffix != ".sh":
                continue
            first = head(path, 2)
            if first != b"#!":
                offenders.append(
                    f"{path.relative_to(REPO)} starts with {first!r}")

        self.assertEqual(
            offenders, [],
            "A shell script must begin with '#!' at byte 0 - anything "
            "before it, a BOM included, makes the kernel refuse to run "
            "it:\n  " + "\n  ".join(offenders)
        )


if __name__ == "__main__":
    unittest.main()
