"""The .con reader understands nesting, and flat files are unaffected by it.

WHY THE FLAT INVARIANT IS THE IMPORTANT PART

Every .con file in use is flat, and several describe real experiments whose
.d files are already analysed. Nesting is a new capability; it must not be a
change in meaning for anything written before it existed. So the first test
here is not about nesting at all - it is that a flat file produces exactly
what the old reader produced, block for block and byte for byte.

The old reader could not see nesting. A nested 'action' silently closed its
parent: the outer block lost every directive written after the nested one,
the matching 'end' was discarded as unopened, and nothing complained. Those
cases are pinned here too, because "used to be wrong" is the reason to be
confident the new behaviour is right.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pscan.config_parser import (
    DATA_TYPES,
    LOOP_TYPES,
    build_plan,
    flatten_tree,
    has_nesting,
    parse_con_tree,
    parse_flat_con_file,
    plan_order,
    validate_nesting,
)

REPO = Path(__file__).resolve().parent.parent


def write(tmpdir, text):
    path = Path(tmpdir) / "t.con"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return str(path)


def old_reader(path):
    """The reader exactly as it was before nesting, for the comparison.

    Kept verbatim rather than described, so the invariant is checked against
    the real previous behaviour and not against a paraphrase of it.
    """
    with open(path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    all_blocks, current_block, block_body = [], None, []
    for line in lines:
        cleaned = line.strip()
        if not cleaned or cleaned.startswith('#'):
            continue
        if cleaned.startswith('action '):
            if current_block:
                all_blocks.append({'type': current_block,
                                   'body': "\n".join(block_body)})
            current_block = cleaned.split()[1].lower()
            block_body = [cleaned]
        elif cleaned == 'end':
            if current_block:
                block_body.append(cleaned)
                all_blocks.append({'type': current_block,
                                   'body': "\n".join(block_body)})
                current_block, block_body = None, []
        else:
            if current_block:
                block_body.append(cleaned)
    if current_block:
        all_blocks.append({'type': current_block,
                           'body': "\n".join(block_body)})
    return all_blocks


class TestFlatFilesAreUnchanged(unittest.TestCase):

    def assert_same_as_old(self, path):
        new = parse_flat_con_file(path)
        old = old_reader(path)
        self.assertEqual(len(new), len(old), f"block count differs for {path}")
        for got, want in zip(new, old):
            self.assertEqual(got['type'], want['type'])
            self.assertEqual(got['body'], want['body'],
                             f"body differs in a '{want['type']}' block")

    def test_every_con_file_in_the_repo_parses_identically(self):
        files = sorted(REPO.glob("**/*.con"))
        self.assertTrue(files, "no .con files found to check")
        for path in files:
            with self.subTest(con=str(path.relative_to(REPO))):
                self.assert_same_as_old(str(path))

    def test_a_typical_flat_config(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, """
                ## a comment
                action count
                count 3
                end

                action d2a
                axis 98
                scan 0.0 10.0 1.0
                end

                action a2d
                channel 0
                end
            """)
            self.assert_same_as_old(path)
            blocks = parse_flat_con_file(path)
            self.assertEqual([b['type'] for b in blocks],
                             ['count', 'd2a', 'a2d'])

    def test_a_flat_file_reports_no_nesting(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, """
                action d2a
                scan 0 1 1
                end
                action a2d
                channel 0
                end
            """)
            self.assertFalse(has_nesting(parse_con_tree(path)))


class TestNestingIsRead(unittest.TestCase):

    def nested(self, tmp):
        return write(tmp, """
            action d2a
            axis 97
            channel 0
            scan 0.0 2.0 1.0
            settle 0.01

            action d2a
            axis 98
            channel 1
            scan 0.0 1.0 1.0

            action a2d
            channel 0
            end

            end

            restore
            end
        """)

    def test_children_are_nested_not_siblings(self):
        with TemporaryDirectory() as tmp:
            roots = parse_con_tree(self.nested(tmp))

            self.assertEqual(len(roots), 1, "outer d2a should be the only root")
            outer = roots[0]
            self.assertEqual(outer['type'], 'd2a')

            self.assertEqual(len(outer['children']), 1)
            inner = outer['children'][0]
            self.assertEqual(inner['type'], 'd2a')

            self.assertEqual(len(inner['children']), 1)
            self.assertEqual(inner['children'][0]['type'], 'a2d')

    def test_a_parents_own_directives_survive_its_children(self):
        """The old reader lost these, which is the bug nesting fixes.

        'restore' is written AFTER the nested block. Flat parsing dropped it,
        so the axis silently never restored.
        """
        with TemporaryDirectory() as tmp:
            path = self.nested(tmp)
            outer = parse_con_tree(path)[0]

            self.assertIn('axis 97', outer['body'])
            self.assertIn('settle 0.01', outer['body'])
            self.assertIn('restore', outer['body'],
                          "a directive after a nested block was lost")

            # And the child's lines are the child's, not the parent's.
            self.assertNotIn('axis 98', outer['body'])
            self.assertNotIn('channel 1', outer['body'])

    def test_the_old_reader_really_did_get_this_wrong(self):
        """Pins the bug, so the fix cannot be mistaken for a no-op."""
        with TemporaryDirectory() as tmp:
            path = self.nested(tmp)
            old = old_reader(path)
            outer_old = old[0]

            self.assertNotIn('restore', outer_old['body'])
            self.assertEqual(len(old), 3,
                             "the old reader saw three sibling blocks")

    def test_nesting_is_detected(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(has_nesting(parse_con_tree(self.nested(tmp))))

    def test_flatten_gives_document_order_parents_first(self):
        with TemporaryDirectory() as tmp:
            flat = flatten_tree(parse_con_tree(self.nested(tmp)))
            self.assertEqual([b['type'] for b in flat], ['d2a', 'd2a', 'a2d'])


class TestUnbalancedBlocksAreErrors(unittest.TestCase):
    """Silently tolerated before; a stray 'end' just vanished."""

    def test_end_without_action(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, """
                action a2d
                channel 0
                end
                end
            """)
            with self.assertRaises(ValueError) as caught:
                parse_con_tree(path)
            self.assertIn("no open", str(caught.exception))
            self.assertIn(":4", str(caught.exception), "should name the line")

    def test_action_never_closed(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, """
                action d2a
                scan 0 1 1

                action a2d
                channel 0
                end
            """)
            with self.assertRaises(ValueError) as caught:
                parse_con_tree(path)
            self.assertIn("never closed", str(caught.exception))

    def test_action_with_no_type(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, "action\nend\n")
            with self.assertRaises(ValueError):
                parse_con_tree(path)

    def test_directive_outside_any_block(self):
        with TemporaryDirectory() as tmp:
            path = write(tmp, """
                channel 0
                action a2d
                end
            """)
            with self.assertRaises(ValueError) as caught:
                parse_con_tree(path)
            self.assertIn("outside", str(caught.exception))


class TestNestingValidation(unittest.TestCase):
    """What a nested config must say to describe a runnable scan."""

    def check(self, text):
        with TemporaryDirectory() as tmp:
            validate_nesting(parse_con_tree(write(tmp, text)))

    def rejects(self, text, *expected_words):
        with TemporaryDirectory() as tmp:
            roots = parse_con_tree(write(tmp, text))
            with self.assertRaises(ValueError) as caught:
                validate_nesting(roots)
            message = str(caught.exception).lower()
            for word in expected_words:
                self.assertIn(word.lower(), message)

    def test_axis_then_count_then_acquisition_is_valid(self):
        self.check("""
            action d2a
            axis 97
            channel 0
            scan 0 10 1
              action count
              count 5
                action a2d
                channel 0
                end
              end
            end
        """)

    def test_two_nested_axes_are_valid(self):
        self.check("""
            action d2a
            axis 97
            scan 0 10 1
              action d2a
              axis 98
              scan 0 5 1
                action a2d
                channel 0
                end
              end
            end
        """)

    def test_a_flat_config_is_not_subjected_to_these_rules(self):
        """The guard that matters.

        Flat axis blocks have no children, so "a loop must contain something"
        would reject every config written before nesting existed. This is
        called unconditionally by design, so it must no-op on flat input.
        """
        self.check("""
            action d2a
            axis 97
            scan 0 10 1
            end
            action a2d
            channel 0
            end
        """)

    def test_a_loop_with_nothing_inside_it(self):
        self.rejects("""
            action d2a
            axis 97
            scan 0 10 1
              action count
              count 5
              end
              action a2d
              channel 0
              end
            end
        """, "nothing inside")

    def test_blocks_nested_inside_a_non_loop(self):
        self.rejects("""
            action d2a
            scan 0 1 1
              action a2d
              channel 0
                action delay
                seconds 1
                end
              end
            end
        """, "only", "loops")

    def test_recording_blocks_at_different_depths(self):
        self.rejects("""
            action d2a
            axis 97
            scan 0 10 1
              action a2d
              channel 0
              end
              action d2a
              axis 98
              scan 0 5 1
                action a2d
                channel 1
                end
              end
            end
        """, "different depths")

    def test_a_config_that_records_nothing(self):
        self.rejects("""
            action d2a
            axis 97
            scan 0 10 1
              action count
              count 5
                action delay
                seconds 1
                end
              end
            end
        """, "records nothing")

    def test_count_is_a_loop_and_a2d_is_not(self):
        """The two answers that shaped this design, pinned as facts."""
        self.assertIn('count', LOOP_TYPES)
        self.assertIn('d2a', LOOP_TYPES)
        self.assertNotIn('a2d', LOOP_TYPES)
        self.assertIn('a2d', DATA_TYPES)


class TestFlatPlansKeepTodaysOrder(unittest.TestCase):
    """A flat config must run exactly as it runs now.

    build_plan() synthesises the same nest a flat file has always implied -
    axes lowest id outermost, count innermost, acquisition at the leaf - so
    one executor can serve both shapes. These assert the ORDER directly,
    because a nest that is structurally right and runs in the wrong sequence
    is the failure this change could most easily introduce.
    """

    def order(self, text, iterations=2):
        with TemporaryDirectory() as tmp:
            roots = parse_con_tree(write(tmp, text))
            return plan_order(build_plan(roots), iterations=iterations)

    def test_axis_ids_decide_nesting_lowest_outermost(self):
        # Written 99 first, but 98 must still be the OUTER loop.
        order = self.order("""
            action d2a
            axis 99
            scan 0 1 1
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """)
        # The first loop entered is the outer one. Strip indentation to read
        # the nesting: outer appears once per its own iteration.
        self.assertEqual(order[0], "d2a[0]")
        self.assertEqual(order[1], "  d2a[0]")
        self.assertEqual(order[2], "    a2d")

    def test_count_is_innermost_in_a_flat_config(self):
        order = self.order("""
            action count
            count 2
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """)
        self.assertEqual(order[0], "d2a[0]")
        self.assertEqual(order[1], "  count[0]")
        self.assertEqual(order[2], "    a2d")

    def test_count_of_one_adds_no_loop(self):
        """Today a count of 1 is not a loop at all; it must not become one."""
        order = self.order("""
            action count
            count 1
            end
            action d2a
            axis 98
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """)
        self.assertNotIn("count[0]", [line.strip() for line in order])

    def test_an_axis_restores_after_its_enclosing_iteration(self):
        order = self.order("""
            action d2a
            axis 98
            scan 0 1 1
            end
            action d2a
            axis 99
            scan 0 1 1
            end
            action a2d
            channel 0
            end
        """)
        stripped = [line.strip() for line in order]
        # The inner axis restores once per outer iteration, not once overall.
        self.assertEqual(stripped.count("d2a restore"), 3,
                         "inner restores twice, outer once")

    def test_the_repos_own_bringup_file(self):
        path = REPO / "examples" / "bringup_6351.con"
        order = plan_order(build_plan(parse_con_tree(str(path))), iterations=2)
        self.assertEqual(order,
                         ["d2a[0]", "  a2d", "d2a[1]", "  a2d", "d2a restore"])


class TestNestingChangesTheExperiment(unittest.TestCase):
    """The point of the whole change: where count sits is what it means."""

    def order(self, text):
        with TemporaryDirectory() as tmp:
            roots = parse_con_tree(write(tmp, text))
            validate_nesting(roots)
            return plan_order(build_plan(roots), iterations=2)

    OUTER = """
        action count
        count 2
          action d2a
          axis 98
          scan 0 1 1
            action a2d
            channel 0
            end
          end
        end
    """

    INNER = """
        action d2a
        axis 98
        scan 0 1 1
          action count
          count 2
            action a2d
            channel 0
            end
          end
        end
    """

    def test_count_outside_repeats_the_whole_sweep(self):
        order = self.order(self.OUTER)
        self.assertEqual(order[0], "count[0]")
        self.assertEqual(order[1], "  d2a[0]")
        self.assertEqual(order[2], "    a2d")

    def test_count_inside_averages_each_point(self):
        order = self.order(self.INNER)
        self.assertEqual(order[0], "d2a[0]")
        self.assertEqual(order[1], "  count[0]")
        self.assertEqual(order[2], "    a2d")

    def test_same_blocks_different_nesting_different_order(self):
        """Identical block types, identical counts - different experiment."""
        outer, inner = self.order(self.OUTER), self.order(self.INNER)
        self.assertEqual(
            sorted({line.strip().split('[')[0] for line in outer}),
            sorted({line.strip().split('[')[0] for line in inner}),
            "the two configs use the same block types",
        )
        self.assertNotEqual(outer, inner,
                            "but must not run in the same order")

    def test_acquisition_count_is_the_same_either_way(self):
        """Both take 4 traces; only the order they are taken in differs."""
        for text in (self.OUTER, self.INNER):
            with self.subTest(config=text.split()[1]):
                order = self.order(text)
                self.assertEqual(
                    sum(1 for line in order if line.strip() == "a2d"), 4)


if __name__ == "__main__":
    unittest.main()
