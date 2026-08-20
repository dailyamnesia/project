import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from flashback.cli import main
from flashback.parser import parse_deck
from flashback.scheduler import Grade
from flashback.storage import due_cards, open_db, record_review
from flashback.storage import sync_deck as real_sync_deck

# Permission-based tests below don't mean anything as root, which ignores
# file-mode write protection entirely.
_RUNNING_AS_ROOT = hasattr(os, "getuid") and os.getuid() == 0


class TestAddCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

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

    def test_empty_deck_name_is_rejected_instead_of_becoming_a_hidden_dotfile(self):
        # An empty deck name isn't caught by the slash/./.. checks above, but has
        # the same "looks like it worked, isn't reachable the same way again"
        # shape: it writes to a file literally named ".md", and Path(...).stem
        # (what `sync` uses to recover the deck name from the file it globbed)
        # doesn't split a leading dot off as a suffix — so the deck comes back
        # named ".md" everywhere else instead of the "" it was added under.
        rc = self.run_flashback("add", "", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_control_character_is_rejected(self):
        # Deck names are echoed straight to the terminal by add's own
        # confirmation, sync, due, stats, and review — an embedded ESC used
        # to write and round-trip fine, then hide or overwrite part of every
        # one of those listings, the same risk _check_card_text already
        # blocks for card text but this deck-name path left wide open.
        rc = self.run_flashback("add", "evil\x1b[31mred", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_bidi_override_is_rejected(self):
        # RLO (U+202E) isn't a control character, but it reorders how
        # everything after it displays in every command that lists deck
        # names — the same Trojan-Source trick already blocked in card text.
        rc = self.run_flashback("add", "evil‮txt.exe", "-q", "hola?", "-a", "hello")
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

    def test_adding_the_same_question_twice_is_rejected_without_touching_the_file(self):
        # Without this check, this silently succeeds both times (a normal
        # "added" message, no error) and writes a deck file that `sync`
        # then refuses to load at all, since parse_deck's duplicate check
        # runs on every real read — the whole deck goes dark with no error
        # at the moment that actually caused it.
        rc1 = self.run_flashback("add", "trivia", "-q", "capital of France?", "-a", "Paris")
        self.assertEqual(rc1, 0)
        deck_path = self.decks_dir / "trivia.md"
        before = deck_path.read_text(encoding="utf-8")

        rc2 = self.run_flashback("add", "trivia", "-q", "capital of France?", "-a", "a different answer")
        self.assertEqual(rc2, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)
        self.assertEqual(len(parse_deck(before)), 1)

    def test_write_failure_does_not_destroy_the_deck_files_existing_cards(self):
        # Path.write_text opens in 'w' mode, which truncates the file to zero
        # bytes before writing a single byte of the new content. Anything
        # that interrupts the write after that point — disk full, the
        # process killed, permissions revoked mid-write — used to leave the
        # deck file empty, destroying every card it already held, not just
        # failing the one `add` in progress. simulate_disk_full replicates
        # that real truncate-then-fail sequence rather than just raising,
        # so this actually exercises the bug instead of skipping past it.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        def simulate_disk_full(self_path, data, encoding=None, errors=None, newline=None):
            self_path.write_bytes(b"")
            raise OSError("simulated disk full mid-write")

        with patch.object(Path, "write_text", simulate_disk_full):
            rc = self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)

    @unittest.skipIf(os.name == "nt", "the lock this guards against is POSIX-only (fcntl)")
    def test_concurrent_adds_to_the_same_deck_do_not_lose_cards(self):
        # Regression test for a real race: two `add`s to the same deck each
        # read the same starting file content, independently compute their
        # own updated version, and whichever writes last used to win
        # outright — the other process's card silently vanished, with a
        # normal "added" success message and exit code 0 on both sides.
        # _atomic_write_text's atomicity (session 48) doesn't help here:
        # this is a lost update between two otherwise-correct writers, not a
        # torn write. Many threads racing the same deck reliably interleaves
        # the read-modify-write windows; a single pair sometimes happened to
        # serialize anyway even against the old, unlocked code.
        barrier = threading.Barrier(8)
        errors = []

        def worker(i):
            barrier.wait()
            try:
                rc = self.run_flashback("add", "spanish", "-q", f"q{i}?", "-a", f"a{i}")
                if rc != 0:
                    errors.append(f"worker {i} exited {rc}")
            except Exception as exc:  # noqa: BLE001 - recording, not swallowing
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual({c.question for c in cards}, {f"q{i}?" for i in range(8)})


class TestRemoveCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

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

    def test_write_failure_does_not_destroy_the_deck_files_existing_cards(self):
        # Same reasoning as add's equivalent test: a write failure mid-`remove`
        # used to truncate the whole deck file, deleting every card it held —
        # not just leaving the targeted card un-removed.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        def simulate_disk_full(self_path, data, encoding=None, errors=None, newline=None):
            self_path.write_bytes(b"")
            raise OSError("simulated disk full mid-write")

        with patch.object(Path, "write_text", simulate_disk_full):
            rc = self.run_flashback("remove", "spanish", "-q", "hello?")

        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)


class TestEditCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

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

    def test_write_failure_does_not_destroy_the_deck_files_existing_cards(self):
        # Same reasoning as add's equivalent test: a write failure mid-`edit`
        # used to truncate the whole deck file, deleting every card it held —
        # not just leaving the intended edit unapplied.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        def simulate_disk_full(self_path, data, encoding=None, errors=None, newline=None):
            self_path.write_bytes(b"")
            raise OSError("simulated disk full mid-write")

        with patch.object(Path, "write_text", simulate_disk_full):
            rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "hola!")

        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)

    @unittest.skipIf(os.name == "nt", "the lock this guards against is POSIX-only (fcntl)")
    def test_concurrent_edits_to_different_cards_in_the_same_deck_do_not_lose_changes(self):
        # Same race as add's equivalent test, but for edit: each worker reads
        # the deck, prompting/argument-parsing takes some (real, if small)
        # time, then it writes an updated version back. Without serializing
        # this, two edits to two different cards in the same deck could each
        # compute their new text from the same pre-edit snapshot, and
        # whichever writes last would silently discard the other's change —
        # not just fail to apply it, but revert it with no error at all.
        for i in range(8):
            self.run_flashback("add", "spanish", "-q", f"q{i}?", "-a", f"original{i}")

        barrier = threading.Barrier(8)
        errors = []

        def worker(i):
            barrier.wait()
            try:
                rc = self.run_flashback(
                    "edit", "spanish", "-q", f"q{i}?", "--new-answer", f"updated{i}"
                )
                if rc != 0:
                    errors.append(f"worker {i} exited {rc}")
            except Exception as exc:  # noqa: BLE001 - recording, not swallowing
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        deck_path = self.decks_dir / "spanish.md"
        cards = {c.question: c.answer for c in parse_deck(deck_path.read_text(encoding="utf-8"))}
        self.assertEqual(cards, {f"q{i}?": f"updated{i}" for i in range(8)})


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

    def test_non_utf8_deck_file_is_skipped_not_a_crash(self):
        # A deck file isn't guaranteed to be valid UTF-8 (hand-edited, pasted
        # from somewhere with different encoding, etc.) — this used to crash
        # the whole sync with a raw UnicodeDecodeError traceback, and take
        # every other, unrelated deck's sync down with it, instead of just
        # skipping the one broken file the same way a ParseError already is.
        self.run_flashback("add", "french", "-q", "bonjour?", "-a", "hello")
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "badenc.md").write_bytes(b"Q: caf\xe9?\nA: coffee\n")

        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["question"] for r in rows}, {"bonjour?"})

    def test_interruption_mid_sync_still_saves_decks_already_processed(self):
        # Two decks to sync; the first succeeds and prints its "N new, M
        # removed" confirmation, then the second raises mid-sync (a real
        # KeyboardInterrupt, or any other crash reaching this point, has the
        # same shape). That confirmation for the first deck must be real,
        # not silently rolled back along with the interrupted second deck —
        # the same failure mode session 43 fixed for `review`.
        self.run_flashback("add", "alpha", "-q", "one?", "-a", "uno")
        self.run_flashback("add", "beta", "-q", "two?", "-a", "dos")

        calls = []

        def flaky_sync_deck(conn, deck, cards, today):
            calls.append(deck)
            if len(calls) == 2:
                raise KeyboardInterrupt("simulated interruption on second deck")
            return real_sync_deck(conn, deck, cards, today)

        with patch("flashback.cli.sync_deck", side_effect=flaky_sync_deck):
            # main() catches KeyboardInterrupt itself and exits cleanly with
            # code 1 rather than propagating it — same as a real Ctrl-C.
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 1)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = {row["deck"] for row in conn.execute("SELECT deck FROM cards")}
        # alpha was synced and its confirmation printed before beta's
        # interruption — it must actually be in the database, not rolled
        # back just because a later deck in the same run failed.
        self.assertIn("alpha", rows)

    def test_hand_created_deck_file_with_control_character_name_is_skipped(self):
        # Deck files are documented as normal to hand-edit/hand-create
        # directly, not just write through the CLI — add/remove/edit reject
        # a control-character deck name before writing, but a file created
        # or renamed by hand outside the CLI reaches sync unguarded. Without
        # this check, sync would happily load it and print the raw ESC byte
        # straight to the terminal in its own "N cards" confirmation line,
        # and in every due/stats/review listing afterward.
        self.run_flashback("add", "french", "-q", "bonjour?", "-a", "hello")
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "evil\x1b[31mred.md").write_text("Q: q1\nA: a1\n", encoding="utf-8")

        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        # Only the legitimate deck's card made it into the database — the
        # bad-named deck was skipped, not silently loaded.
        self.assertEqual({r["question"] for r in rows}, {"bonjour?"})

    def test_directory_matching_deck_glob_is_skipped_not_a_crash(self):
        # decks_dir.glob("*.md") matches directories too, not just files —
        # a directory that happens to end in .md used to crash the whole
        # sync (an uncaught IsADirectoryError) instead of skipping just that
        # one bogus entry.
        self.run_flashback("add", "french", "-q", "bonjour?", "-a", "hello")
        (self.decks_dir / "oddname.md").mkdir(parents=True)

        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["question"] for r in rows}, {"bonjour?"})


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

    def test_grading_a_card_removed_mid_session_does_not_claim_a_fake_save(self):
        # A second `flashback remove` + `sync` invocation can race an
        # in-progress `review` session: the card is shown and its answer
        # revealed, then deleted from the database before the person grades
        # it. `record_review`'s UPDATE then matches zero rows — the grade
        # was never saved, and `review` must say so instead of printing a
        # confirmed "next review" date for a card that no longer exists.
        self.run_flashback("add", "spanish", "-q", "one?", "-a", "uno")
        self.run_flashback("add", "spanish", "-q", "two?", "-a", "dos")
        self.run_flashback("sync")

        with open_db(self.state_dir / "state.sqlite3") as conn:
            two_id = conn.execute(
                "SELECT id FROM cards WHERE question = ?", ("two?",)
            ).fetchone()["id"]

        scripted = iter(["", "3", ""])  # reveal one, grade one good, reveal two

        def fake_input(prompt):
            try:
                return next(scripted)
            except StopIteration:
                # about to be asked to grade "two?" -- simulate a `remove` +
                # `sync` racing in between reveal and grade.
                with open_db(self.state_dir / "state.sqlite3") as conn:
                    conn.execute("DELETE FROM cards WHERE id = ?", (two_id,))
                    conn.commit()
                return "3"

        out = io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out):
            rc = self.run_flashback("review")
        self.assertEqual(rc, 0)

        output = out.getvalue()
        self.assertIn("card no longer exists, skipped", output)
        self.assertNotIn("next review", output.split("two?")[1])
        self.assertEqual(output.count("next review"), 1)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = {row["question"]: row for row in conn.execute("SELECT question FROM cards")}
        self.assertNotIn("two?", rows)


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


class TestNextDueReporting(unittest.TestCase):
    """`due`/`review`/`stats` should say when the next card actually comes back.

    Every card's `due_date` has always been in the database; before this, a
    person who caught up left with "nothing due. go outside." and no idea
    whether that meant tomorrow or next month.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def capture(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.run_flashback(*args)
        return rc, buf.getvalue()

    def _set_due(self, due, deck=None):
        with open_db(self.state_dir / "state.sqlite3") as conn:
            if deck is None:
                conn.execute("UPDATE cards SET due_date = ?", (due,))
            else:
                conn.execute("UPDATE cards SET due_date = ? WHERE deck = ?", (due, deck))

    def test_due_reports_the_next_due_date_when_nothing_is_due(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")
        self._set_due((date.today() + timedelta(days=6)).isoformat())

        rc, out = self.capture("due")
        self.assertEqual(rc, 0)
        self.assertIn("nothing due", out)
        self.assertIn((date.today() + timedelta(days=6)).isoformat(), out)
        self.assertIn("in 6 days", out)

    def test_due_says_tomorrow_rather_than_in_1_days(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")
        self._set_due((date.today() + timedelta(days=1)).isoformat())

        _, out = self.capture("due")
        self.assertIn("tomorrow", out)
        self.assertNotIn("in 1 days", out)

    def test_review_reports_the_same_next_due_date_as_due(self):
        # `due` and `review` print the identical "nothing due" message; if only
        # one of them learned to say when to come back, the two would drift.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")
        self._set_due((date.today() + timedelta(days=3)).isoformat())

        _, due_out = self.capture("due")
        _, review_out = self.capture("review")
        self.assertEqual(due_out, review_out)
        self.assertIn("in 3 days", review_out)

    def test_next_due_date_respects_the_deck_filter(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "geology", "-q", "batholith?", "-a", "big rock")
        self.run_flashback("sync")
        self._set_due((date.today() + timedelta(days=2)).isoformat(), deck="spanish")
        self._set_due((date.today() + timedelta(days=9)).isoformat(), deck="geology")

        _, out = self.capture("due", "--deck", "geology")
        self.assertIn("in 9 days", out)
        self.assertNotIn("in 2 days", out)

    def test_nothing_due_with_no_cards_at_all_says_nothing_about_a_next_date(self):
        # An empty database has no honest answer here — better to stay quiet
        # than to invent one.
        rc, out = self.capture("due")
        self.assertEqual(rc, 0)
        self.assertIn("nothing due", out)
        self.assertNotIn("next card is due", out)

    def test_stats_shows_each_decks_next_due_date(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "geology", "-q", "batholith?", "-a", "big rock")
        self.run_flashback("sync")
        self._set_due((date.today() + timedelta(days=4)).isoformat(), deck="geology")

        rc, out = self.capture("stats")
        self.assertEqual(rc, 0)
        self.assertIn("next", out.splitlines()[0])
        geology = next(line for line in out.splitlines() if line.startswith("geology"))
        spanish = next(line for line in out.splitlines() if line.startswith("spanish"))
        self.assertIn((date.today() + timedelta(days=4)).isoformat(), geology)
        # spanish is due right now, so it has no *future* date to report.
        self.assertTrue(spanish.rstrip().endswith("-"), spanish)


class TestHardCommand(unittest.TestCase):
    """`hard` should tell a learner which cards they're actually bad at.

    The scheduler has computed this since day one — easiness falls on every
    `again`/`hard` grade, and the whole pitch of spaced repetition is that the
    tool knows what you're struggling with. It just never had a way to say it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def capture(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.run_flashback(*args)
        return rc, buf.getvalue()

    def _grade(self, question, *grades):
        with open_db(self.state_dir / "state.sqlite3") as conn:
            for grade in grades:
                row = conn.execute(
                    "SELECT * FROM cards WHERE question = ?", (question,)
                ).fetchone()
                record_review(conn, row, grade, date.today())

    def test_hard_lists_a_card_the_learner_keeps_missing(self):
        self.run_flashback("add", "astro", "-q", "metallicity?", "-a", "not H or He")
        self.run_flashback("add", "astro", "-q", "parsec?", "-a", "3.26ly")
        self.run_flashback("sync")
        self._grade("metallicity?", Grade.AGAIN, Grade.AGAIN)

        rc, out = self.capture("hard")
        self.assertEqual(rc, 0)
        self.assertIn("metallicity?", out)
        self.assertIn("missed at your last review", out)
        # A card never graded down has no business on a list of what you're bad at.
        self.assertNotIn("parsec?", out)

    def test_hard_separates_a_recovered_card_from_one_missed_right_now(self):
        # The finding this command was built around: easiness alone can't tell
        # "missed this morning" from "struggled with weeks ago, fine now", so a
        # single hardest-first list would head itself with a mastered card.
        self.run_flashback("add", "astro", "-q", "chandrasekhar?", "-a", "1.4 Msun")
        self.run_flashback("add", "astro", "-q", "metallicity?", "-a", "not H or He")
        self.run_flashback("sync")
        self._grade("chandrasekhar?", Grade.AGAIN, Grade.AGAIN, Grade.AGAIN, *[Grade.GOOD] * 4)
        self._grade("metallicity?", Grade.HARD, Grade.AGAIN)

        rc, out = self.capture("hard")
        self.assertEqual(rc, 0)
        missed_at = out.index("you missed at your last review")
        recovering_at = out.index("you've found hard before")
        self.assertLess(missed_at, recovering_at)
        # Each card must land in the right section, not merely appear somewhere.
        self.assertLess(out.index("metallicity?"), recovering_at)
        self.assertGreater(out.index("chandrasekhar?"), recovering_at)
        self.assertIn("correct at your last 4 reviews", out)

    def test_hard_says_nothing_is_hard_rather_than_inventing_a_ranking(self):
        self.run_flashback("add", "astro", "-q", "parsec?", "-a", "3.26ly")
        self.run_flashback("sync")
        self._grade("parsec?", Grade.GOOD, Grade.EASY)

        rc, out = self.capture("hard")
        self.assertEqual(rc, 0)
        self.assertIn("nothing looks hard yet", out)
        self.assertNotIn("parsec?", out)

    def test_hard_with_no_decks_at_all_points_at_sync(self):
        rc, out = self.capture("hard")
        self.assertEqual(rc, 0)
        self.assertIn("run `flashback sync`", out)

    def test_hard_respects_the_deck_filter(self):
        self.run_flashback("add", "astro", "-q", "metallicity?", "-a", "not H or He")
        self.run_flashback("add", "french", "-q", "le fauteuil?", "-a", "armchair")
        self.run_flashback("sync")
        self._grade("metallicity?", Grade.AGAIN)
        self._grade("le fauteuil?", Grade.AGAIN)

        _, out = self.capture("hard", "--deck", "french")
        self.assertIn("le fauteuil?", out)
        self.assertNotIn("metallicity?", out)

    def test_hard_announces_what_the_limit_hides_instead_of_truncating_silently(self):
        for n in range(4):
            self.run_flashback("add", "astro", "-q", f"q{n}?", "-a", str(n))
        self.run_flashback("sync")
        for n in range(4):
            self._grade(f"q{n}?", Grade.AGAIN)

        _, out = self.capture("hard", "--limit", "2")
        self.assertIn("and 2 more", out)
        _, all_out = self.capture("hard", "--limit", "0")
        self.assertNotIn("more (raise --limit", all_out)
        for n in range(4):
            self.assertIn(f"q{n}?", all_out)

    def test_stats_counts_the_cards_currently_being_missed(self):
        # Without this the new command is undiscoverable: nothing else in the
        # tool would ever hint that it has something to say.
        self.run_flashback("add", "astro", "-q", "metallicity?", "-a", "not H or He")
        self.run_flashback("add", "astro", "-q", "parsec?", "-a", "3.26ly")
        self.run_flashback("sync")
        self._grade("metallicity?", Grade.AGAIN)
        self._grade("parsec?", Grade.GOOD)

        rc, out = self.capture("stats")
        self.assertEqual(rc, 0)
        self.assertIn("missed", out.splitlines()[0])
        astro = next(line for line in out.splitlines() if line.startswith("astro"))
        self.assertEqual(astro.split()[1:4], ["2", "0", "1"])


if __name__ == "__main__":
    unittest.main()
