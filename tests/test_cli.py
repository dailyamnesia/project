import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from flashback.cli import main
from flashback.parser import parse_deck
from flashback.storage import due_cards, open_db

# Permission-based tests below don't mean anything as root, which ignores
# file-mode write protection entirely.
_RUNNING_AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


class TestAddCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_creates_deck_file_and_dir_if_missing(self):
        self.assertFalse(self.decks_dir.exists())
        rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        self.assertTrue(deck_path.exists())
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].question, "hello?")
        self.assertEqual(cards[0].answer, "hola")

    def test_appends_to_existing_deck_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1].question, "goodbye?")

    def test_empty_question_fails_without_touching_file(self):
        rc = self.run_flashback("add", "spanish", "-q", "   ", "-a", "hola")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "spanish.md").exists())

    def test_deck_name_with_slash_is_rejected_instead_of_landing_outside_decks_dir(self):
        # A slash either escapes decks-dir (`../x`) or lands somewhere `sync`'s
        # non-recursive glob never looks (`x/y`) — either way the card would look
        # added (a success message, a file on disk) but never become reachable
        # again. Reject it up front instead.
        rc = self.run_flashback("add", "vocab/spanish", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_of_dotdot_is_rejected(self):
        rc = self.run_flashback("add", "..", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_answer_with_embedded_separator_line_is_rejected_without_writing_file(self):
        # Without this check, this would write successfully (a normal "added"
        # message) but corrupt the file: the embedded "---" reads back as a
        # card separator, splitting one card into two invalid ones, and the
        # whole deck file then fails to parse on the next `sync`.
        rc = self.run_flashback("add", "markdown", "-q", "what's a rule?", "-a", "like so:\n---\ndone")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "markdown.md").exists())

    def test_answer_with_embedded_q_prefix_line_is_rejected_without_writing_file(self):
        # Without this check, this writes successfully with no error at all —
        # the embedded "Q:" line reads back as a new question marker,
        # silently merging the example text into the real question/answer.
        rc = self.run_flashback("add", "syntax", "-q", "how do cards work?", "-a", "start with:\nQ: like this")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "syntax.md").exists())

    def test_answer_with_escape_character_is_rejected_without_writing_file(self):
        # Without this check, this writes successfully and parses fine — the
        # problem only shows up later, when `review` prints the answer
        # straight to the terminal and the escape sequence hides or
        # overwrites part of what's shown instead of just displaying as text.
        rc = self.run_flashback("add", "trivia", "-q", "capital of France?", "-a", "before\x1b[8mhidden\x1b[0mafter")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "trivia.md").exists())

    def test_answer_with_bidi_override_is_rejected_without_writing_file(self):
        # RLO (U+202E) isn't a control character, but it reorders how
        # everything after it displays — the same trick used to disguise
        # malicious filenames as harmless ones.
        rc = self.run_flashback("add", "trivia", "-q", "filename?", "-a", "evil‮txt.exe")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "trivia.md").exists())


class TestRemoveCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_removes_a_card_from_an_existing_deck(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        rc = self.run_flashback("remove", "spanish", "-q", "hello?")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].question, "goodbye?")

    def test_missing_deck_file_fails(self):
        rc = self.run_flashback("remove", "no-such-deck", "-q", "hello?")
        self.assertEqual(rc, 1)

    def test_no_matching_question_fails_without_touching_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        rc = self.run_flashback("remove", "spanish", "-q", "not there")
        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)

    def test_deck_name_with_slash_is_rejected(self):
        rc = self.run_flashback("remove", "vocab/spanish", "-q", "hello?")
        self.assertEqual(rc, 1)

    def test_removes_unrelated_card_despite_a_poisoned_card_hand_edited_into_the_deck(self):
        # A control character typed straight into the deck file (bypassing
        # add/edit's own checks entirely) used to block remove of any other,
        # unrelated card in that deck too, since parser.remove_card re-vetted
        # every card in the file, not just the one being removed.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        deck_path.write_text(
            deck_path.read_text(encoding="utf-8") + "\n---\n\nQ: bad\nA: bell\x07here\n",
            encoding="utf-8",
        )

        rc = self.run_flashback("remove", "spanish", "-q", "hello?")
        self.assertEqual(rc, 0)

        cards = parse_deck(deck_path.read_text(encoding="utf-8"), validate=False)
        self.assertEqual([c.question for c in cards], ["bad"])


class TestEditCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_edits_answer_in_place(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "hola!")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["hello?", "goodbye?"])
        self.assertEqual(cards[0].answer, "hola!")

    def test_edits_question_in_place(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-question", "hi?")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(cards[0].question, "hi?")

    def test_matches_question_with_surrounding_whitespace(self):
        # `remove` and `edit_card()` both strip -q before matching (parsed
        # questions are already stripped by the parser); cmd_edit's own
        # pre-lookup compared the raw, unstripped arg and silently missed a
        # real card whenever -q carried leading/trailing whitespace, even
        # though the identical `remove -q "  hello?  "` succeeded.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")

        rc = self.run_flashback("edit", "spanish", "-q", "  hello?  ", "--new-answer", "hola!")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(cards[0].answer, "hola!")

    def test_missing_deck_file_fails(self):
        rc = self.run_flashback("edit", "no-such-deck", "-q", "hello?", "--new-answer", "x")
        self.assertEqual(rc, 1)

    def test_no_matching_question_fails_without_touching_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        rc = self.run_flashback("edit", "spanish", "-q", "not there", "--new-answer", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)

    def test_deck_name_with_slash_is_rejected(self):
        rc = self.run_flashback("edit", "vocab/spanish", "-q", "hello?", "--new-answer", "x")
        self.assertEqual(rc, 1)

    def test_edits_unrelated_card_despite_a_poisoned_card_hand_edited_into_the_deck(self):
        # Same reasoning as remove's equivalent test — and cmd_edit has its
        # own separate pre-lookup (to print the current Q/A before prompting)
        # that used to call parse_deck with full validation too, so this
        # exercises a second, CLI-level fix point, not just parser.edit_card.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        deck_path.write_text(
            deck_path.read_text(encoding="utf-8") + "\n---\n\nQ: bad\nA: bell\x07here\n",
            encoding="utf-8",
        )

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "hola!")
        self.assertEqual(rc, 0)

        cards = parse_deck(deck_path.read_text(encoding="utf-8"), validate=False)
        self.assertEqual(cards[0].answer, "hola!")


class TestSyncCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def test_deleting_a_deck_file_removes_its_cards_on_next_sync(self):
        # Without prune_missing_decks, this deck's cards would sit in the database
        # forever: sync only reconciles decks it's handed a file for, so a deck file
        # deleted outright is never noticed. The cards would stay "due" and visible
        # in stats, but unreachable from `remove`/`edit`, since both require the
        # deck file to still exist.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        (self.decks_dir / "spanish.md").unlink()
        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        rc = self.run_flashback("due")
        self.assertEqual(rc, 0)

    def test_deleted_deck_cards_are_gone_from_due_after_sync(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "french", "-q", "bonjour?", "-a", "hello")
        self.run_flashback("sync")

        (self.decks_dir / "spanish.md").unlink()
        self.run_flashback("sync")

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["deck"] for r in rows}, {"french"})

    def test_deck_file_that_fails_to_parse_does_not_lose_its_previously_synced_cards(self):
        # A deck file that still exists but currently fails to parse (e.g. a typo
        # mid-edit) is not the same as a deck that was deleted — sync should skip
        # it with an error, not treat it as gone and prune its cards.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        (self.decks_dir / "spanish.md").write_text("not a valid card\n", encoding="utf-8")
        self.run_flashback("sync")

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual(len(rows), 1)


class TestReviewCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def test_eof_during_review_exits_cleanly_instead_of_crashing(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        with patch("builtins.input", side_effect=EOFError):
            rc = self.run_flashback("review")
        self.assertEqual(rc, 1)

    def test_keyboard_interrupt_during_review_exits_cleanly_instead_of_crashing(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            rc = self.run_flashback("review")
        self.assertEqual(rc, 1)

    def test_interruption_mid_session_still_saves_cards_already_graded(self):
        # Three cards due; grade the first two normally, then EOF (a dropped
        # stdin/terminal, same shape as a real Ctrl-D) hits on the third
        # card's reveal prompt. The two already-graded cards each printed a
        # "next review: ..." confirmation before the interruption — that
        # confirmation must be real, not silently rolled back along with the
        # incomplete third card.
        self.run_flashback("add", "spanish", "-q", "one?", "-a", "uno")
        self.run_flashback("add", "spanish", "-q", "two?", "-a", "dos")
        self.run_flashback("add", "spanish", "-q", "three?", "-a", "tres")
        self.run_flashback("sync")

        with patch(
            "builtins.input",
            side_effect=["", "3", "", "3", EOFError],
        ):
            rc = self.run_flashback("review")
        self.assertEqual(rc, 1)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = {
                row["question"]: row
                for row in conn.execute("SELECT question, repetitions, due_date FROM cards")
            }
        self.assertEqual(rows["one?"]["repetitions"], 1)
        self.assertEqual(rows["two?"]["repetitions"], 1)
        self.assertNotEqual(rows["one?"]["due_date"], date.today().isoformat())
        self.assertNotEqual(rows["two?"]["due_date"], date.today().isoformat())
        # the interrupted third card was never graded, so it's untouched
        self.assertEqual(rows["three?"]["repetitions"], 0)


@unittest.skipIf(_RUNNING_AS_ROOT, "root ignores file-mode write protection")
class TestStateDirAccessErrors(unittest.TestCase):
    """An unwritable --state-dir/--decks-dir is a real, user-triggerable
    situation (permissions, a read-only mount, a path that collides with an
    existing file) — it should exit cleanly with a one-line message, not a
    raw traceback exposing internal file paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.decks_dir.mkdir()
        (self.decks_dir / "spanish.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def test_readonly_state_dir_exits_cleanly_on_sync(self):
        self.state_dir.mkdir()
        self.state_dir.chmod(0o555)
        self.addCleanup(self.state_dir.chmod, 0o755)

        rc = self.run_flashback("sync")
        self.assertEqual(rc, 1)

    def test_readonly_state_dir_exits_cleanly_on_stats(self):
        self.state_dir.mkdir()
        self.state_dir.chmod(0o555)
        self.addCleanup(self.state_dir.chmod, 0o755)

        rc = self.run_flashback("stats")
        self.assertEqual(rc, 1)

    def test_state_dir_path_colliding_with_existing_file_exits_cleanly(self):
        self.state_dir.write_text("not a directory", encoding="utf-8")

        rc = self.run_flashback("sync")
        self.assertEqual(rc, 1)

    def test_readonly_decks_dir_exits_cleanly_on_add(self):
        self.decks_dir.chmod(0o555)
        self.addCleanup(self.decks_dir.chmod, 0o755)

        rc = self.run_flashback("add", "newdeck", "-q", "q?", "-a", "a")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
