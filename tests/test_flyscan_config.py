"""A fly config is FLAT, and says so rather than quietly flattening.

Two rules, both of which exist because the alternative is silent.

NESTING. pscan's blocks nest and the nesting is the experiment: where a
'count' block sits decides whether it averages a point, repeats a row or
repeats the image. None of that can be true in a fly scan, whose shape is
fixed - the slow axis is the only loop, the fast axis is one continuous
move, and the samples along it are hardware triggers. The reader underneath
flyscan.config would FLATTEN a nested file and run it correctly, ignoring
the hierarchy, which is exactly what has to be caught: someone who nests is
relying on a rule that does not hold here.

COUNT. 'action count' above 1 is refused, because nothing in the fly runner
reads it. It used to pass with a warning claiming it would "fly every line
N times" - so the data came back a fraction of the expected size and looked
healthy. A warning describing an effect that does not happen is worse than
no warning at all.
"""

import os
import tempfile
import textwrap
import unittest

from flyscan.config import FlyConfigError, parse_fly_config

#: The three blocks a fly scan requires, each complete on its own.
FAST = """
    action apt_fly
    axis 0
    scan 53.500 53.540
    velocity 0.2
    limits 50.0 57.0
    spot 1.0
    end
"""

SLOW = """
    action apt_stage
    axis 1
    scan 37.000 37.010 0.005
    end
"""

SCOPE = """
    action scope_line
    ip 192.168.1.100
    channels C1, C2
    roles ac, dc
    sparsing 1, 100
    sample_rate 1e6
    segment_window 50e-6
    rep_rate 80e6
    end
"""

#: A minimal runnable fly config, with {extra} spliced in at the end.
BASE = FAST + SLOW + SCOPE + "{extra}\n"


def parse(text):
    handle = tempfile.NamedTemporaryFile(
        'w', suffix='.con', delete=False, newline='\n'
    )
    try:
        handle.write(textwrap.dedent(text))
        handle.close()
        return parse_fly_config(handle.name, counts_per_mm=20000.0)
    finally:
        os.unlink(handle.name)


def rejects(case, text, *expected):
    with case.assertRaises(FlyConfigError) as caught:
        parse(text)
    message = str(caught.exception).lower()
    for word in expected:
        case.assertIn(word.lower(), message)
    return message


class TestFlatIsStillAccepted(unittest.TestCase):

    def test_the_base_config_parses(self):
        config = parse(BASE.format(extra=""))
        self.assertEqual(config.plan.lines, 3)
        self.assertEqual(config.scope_kind, 'lecroy')

    def test_order_carries_no_information(self):
        # Exactly one of each fly block is required, so a different order is
        # the same scan - which is the reason nesting would add nothing to
        # say here, and the reason refusing it costs nothing.
        self.assertEqual(parse(SCOPE + SLOW + FAST).plan.lines, 3)


class TestNestingIsRefused(unittest.TestCase):

    def test_a_nested_fly_config_is_refused_with_the_reason(self):
        message = rejects(self, """
            action apt_fly
            axis 0
            scan 53.500 53.540
            velocity 0.2
            limits 50.0 57.0

                action scope_line
                ip 192.168.1.100
                channels C1
                roles ac
                sample_rate 1e6
                segment_window 50e-6
                end

            end

            action apt_stage
            axis 1
            scan 37.000 37.010 0.005
            end
        """, "nests its blocks", "a fly scan is flat")

        # It names the block that nests, and says what to do instead.
        self.assertIn("'action apt_fly'", message)
        self.assertIn("one after another", message)

    def test_the_reason_is_given_and_not_just_the_rule(self):
        # Someone who nests believes placement matters. The message has to
        # say why it does not, or the refusal looks arbitrary.
        message = rejects(self, """
            action apt_stage
            axis 1
            scan 37.000 37.010 0.005

                action apt_fly
                axis 0
                scan 53.500 53.540
                velocity 0.2
                end

            end

            action scope_line
            ip 192.168.1.100
            channels C1
            roles ac
            sample_rate 1e6
            segment_window 50e-6
            end
        """, "nests its blocks")
        self.assertIn("only loop", message)
        self.assertIn("hardware triggers", message)

    def test_two_nested_parents_are_both_named(self):
        message = rejects(self, """
            action apt_stage
            axis 1
            scan 37.000 37.010 0.005

                action apt_fly
                axis 0
                scan 53.500 53.540
                velocity 0.2

                    action scope_line
                    ip 192.168.1.100
                    channels C1
                    roles ac
                    sample_rate 1e6
                    segment_window 50e-6
                    end

                end

            end
        """, "nests its blocks")
        self.assertIn("'action apt_stage'", message)
        self.assertIn("'action apt_fly'", message)
        self.assertIn("have blocks written inside them", message)

    def test_a_nested_setup_block_is_refused_too(self):
        # The rule is about the file, not about which block: a 'count'
        # wrapped round the fly blocks is the most likely way to write one
        # by habit, and it is the case that would silently do nothing.
        rejects(self, """
            action count
            count 1

                action apt_fly
                axis 0
                scan 53.500 53.540
                velocity 0.2
                end

                action apt_stage
                axis 1
                scan 37.000 37.010 0.005
                end

                action scope_line
                ip 192.168.1.100
                channels C1
                roles ac
                sample_rate 1e6
                segment_window 50e-6
                end

            end
        """, "nests its blocks", "'action count'")


class TestCountIsHonest(unittest.TestCase):

    def test_count_above_one_is_refused(self):
        message = rejects(self, BASE.format(extra="""
            action count
            count 4
            end
        """), "'action count' is 4", "one pass")
        # It says what the data would have looked like, which is the part
        # that would otherwise go unnoticed.
        self.assertIn("4th of the size", message)

    def test_the_refusal_points_at_what_does_work(self):
        # Not "unsupported": binning is the real answer for more signal per
        # pixel, and 'averages' is NOT - every segment is one pixel here.
        message = rejects(self, BASE.format(extra="""
            action count
            count 10
            end
        """), "'action count' is 10")
        self.assertIn("bin <nm>", message)
        self.assertIn("every segment is one pixel", message)

    def test_count_one_is_accepted_in_silence(self):
        # Every config written from pscan's template carries it, and it
        # means what it says.
        config = parse(BASE.format(extra="""
            action count
            count 1
            end
        """))
        self.assertEqual(config.count, 1)
        self.assertFalse([w for w in config.warnings if 'count' in w.lower()])

    def test_no_count_block_is_accepted(self):
        config = parse(BASE.format(extra=""))
        self.assertEqual(config.count, 1)


if __name__ == '__main__':
    unittest.main()
