import ast
import io
import os
import sqlite3
import tempfile
import threading
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


def _patch_lock_dir(testcase):
    """Redirect flashback's deck-lock files into testcase's own temp dir.

    Without this, every add/remove/edit call in the suite drops a real
    file into the actual system temp directory that nothing ever cleans
    up (see flashback.cli._deck_lock_path); testcase's own temp dir is
    already removed by its addCleanup(self._tmp.cleanup), so redirecting
    lock files there sweeps them away for free instead of leaking.
    """
    patcher = patch("flashback.cli._lock_dir", return_value=Path(testcase._tmp.name))
    testcase.addCleanup(patcher.stop)
    patcher.start()


class TestAddCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
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

    def test_add_with_a_deck_name_near_the_filesystem_name_length_limit_succeeds(self):
        # Nothing in `_invalid_deck_name` (or anywhere else) limits how long
        # a deck name can be, but every real filesystem caps how long a
        # single path *component* can be -- 255 bytes on ext4 and most other
        # Linux filesystems. A deck name a handful of bytes under that cap
        # already produces a valid "{name}.md" target filename on its own.
        #
        # `_atomic_write_text` used to build its temp file's name by
        # decorating the *target's* name directly (a leading dot plus
        # ".tmp{pid}"), which added enough overhead to push the *temp*
        # file's name past the 255-byte limit even though the real target
        # name it was about to replace would have fit comfortably under it
        # -- so `add` failed with a raw "OSError: [Errno 36] File name too
        # long" on a deck name nothing else in this CLI ever rejected or
        # warned about. Confirmed directly before the fix: a 251-character
        # deck name produces a 254-byte "{name}.md" target (fits), but the
        # old temp name came to 266 bytes and failed outright.
        #
        # 251 is chosen so "{name}.md" (254 bytes) fits under the 255-byte
        # limit but the old, longer temp-file scheme did not -- pinning
        # down the exact boundary this regression is about, rather than an
        # arbitrarily large name that could fail for an unrelated reason.
        deck_name = "a" * 251
        rc = self.run_flashback("add", deck_name, "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / f"{deck_name}.md"
        self.assertTrue(deck_path.exists())
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].question, "hello?")

    def test_deck_lock_file_does_not_leak_into_the_real_system_temp_dir(self):
        real_tmp_before = set(Path(tempfile.gettempdir()).glob("flashback-*.lock"))

        rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 0)

        real_tmp_after = set(Path(tempfile.gettempdir()).glob("flashback-*.lock"))
        self.assertEqual(
            real_tmp_after,
            real_tmp_before,
            "add left a lock file in the real system temp dir instead of "
            "this test's own (already-cleaned-up) temp dir",
        )
        self.assertTrue(
            list(Path(self._tmp.name).glob("flashback-*.lock")),
            "expected the lock file to land inside the patched (test-owned) "
            "temp dir instead",
        )

    def test_appends_to_existing_deck_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1].question, "goodbye?")

    @unittest.skipIf(os.name == "nt", "symlinked deck files aren't exercised on Windows")
    def test_add_to_symlinked_deck_file_preserves_the_symlink(self):
        # A deck file is allowed to be a symlink -- e.g. into a separate,
        # shared repo of deck content that's kept outside `decks_dir`
        # itself. `_atomic_write_text` writes the new content to a sibling
        # temp file and `os.replace`s it into place; `os.replace` does NOT
        # follow a symlink at the destination, it *replaces the symlink
        # entry itself* -- so without special-casing this, the very first
        # `add` to a symlinked deck file silently turns it into an ordinary,
        # independent regular file. The real target file is left holding
        # only the cards it had before, now permanently disconnected from
        # decks_dir even though nothing printed a warning and `add`'s own
        # success message still claims to have added to the same path.
        real_dir = Path(self._tmp.name) / "shared"
        real_dir.mkdir()
        real_path = real_dir / "spanish-real.md"
        real_path.write_text("Q: hello?\nA: hola\n", encoding="utf-8")

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        link_path = self.decks_dir / "spanish.md"
        os.symlink(real_path, link_path)

        rc = self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")
        self.assertEqual(rc, 0)

        self.assertTrue(link_path.is_symlink(), "add replaced the symlink with a regular file")
        self.assertEqual(os.path.realpath(link_path), os.path.realpath(real_path))

        cards = parse_deck(real_path.read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["hello?", "goodbye?"])

    @unittest.skipIf(os.name == "nt", "symlinked deck files aren't exercised on Windows")
    def test_add_to_self_referential_symlink_deck_file_fails_cleanly(self):
        # A deck file is allowed to be a symlink (see the preceding test) --
        # but nothing stops that symlink from being a loop: pointing at
        # itself, directly or through a chain, rather than at a real file.
        # That's a real, reachable filesystem state -- a hand-typed `ln -s`
        # typo, or two half-finished scripts each linking the other's
        # output -- not just a hypothetical.
        #
        # `deck_path.exists()` correctly reports False for a loop (a plain
        # OSError(ELOOP) from stat(), which pathlib treats the same as "not
        # there"), so `add` correctly treats it as "no existing content to
        # read" and tries to create the file, same as it would for a broken
        # symlink. But `_atomic_write_text` then calls `path.resolve()` to
        # find the real target to write through -- and `Path.resolve()`
        # does its own, separate cycle detection and raises a bare
        # `RuntimeError("Symlink loop from ...")`, not an `OSError`, when it
        # finds one. That isn't caught by main()'s `except OSError` handler
        # (the one that already gives a clean message for the sibling
        # broken-symlink case), so it used to crash with a raw traceback
        # exposing local paths instead of a one-line error.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        link_path = self.decks_dir / "spanish.md"
        os.symlink(link_path, link_path)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err.getvalue())
        self.assertIn("symlink loop", err.getvalue().lower())

    def test_empty_question_fails_without_touching_file(self):
        rc = self.run_flashback("add", "spanish", "-q", "   ", "-a", "hola")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "spanish.md").exists())

    def test_deck_name_with_differing_unicode_normalization_form_is_the_same_deck(self):
        # "é" can be spelled as one precomposed codepoint (NFC) or as "e" plus
        # a combining acute accent (NFD) — both render identically. This is
        # exactly the case parser.normalize_question exists to handle for
        # question text (see test_matches_question_with_differing_unicode_
        # normalization_form in TestRemoveCommand/TestEditCommand below), but
        # the deck *name* itself — used to build the deck's file path and its
        # `decks` table row — got no equivalent normalization. Without it,
        # two "differently-typed" spellings of the same deck name silently
        # become two different files on disk (byte-for-byte different names,
        # even though they look identical) and two unrelated decks in the
        # database, instead of one deck with two cards.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        rc1 = self.run_flashback("add", nfc, "-q", "Q1", "-a", "A1")
        rc2 = self.run_flashback("add", nfd, "-q", "Q2", "-a", "A2")
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)

        md_files = sorted(self.decks_dir.glob("*.md"))
        self.assertEqual(
            len(md_files), 1, f"expected one deck file, got {[f.name for f in md_files]}"
        )
        cards = parse_deck(md_files[0].read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["Q1", "Q2"])

    def test_add_finds_an_existing_deck_file_named_in_a_different_unicode_normalization_form(self):
        # A deck file isn't only ever created by `add` itself (which always
        # normalizes the name it writes to NFC) -- deck files are documented
        # as normal to hand-create outside the CLI, and a normalization-happy
        # filesystem such as macOS's (HFS+/APFS) stores accented file names
        # as NFD by default, a byte-for-byte NFD name that survives a `git
        # clone` onto Linux untouched. `sync` already recognizes such a file
        # as the deck it normalizes to (see TestSyncCommand), but `add` used
        # to guess the deck's path as decks_dir / f"{normalized_name}.md" --
        # which, against an NFD-named file, matches nothing, so `add` treated
        # a real, populated deck as brand new and created a *second*,
        # colliding file next to the first instead of appending to it. The
        # new card then silently vanishes from every future `sync` (the
        # collision-detection added for hand-created files causes the losing
        # file to be skipped every run), with a cheerful "added to ..."
        # message giving no hint that anything went wrong.
        nfd = unicodedata.normalize("NFD", "café")
        nfc = unicodedata.normalize("NFC", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / f"{nfd}.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")

        rc = self.run_flashback("add", nfc, "-q", "adios?", "-a", "goodbye")
        self.assertEqual(rc, 0)

        md_files = sorted(self.decks_dir.glob("*.md"))
        self.assertEqual(
            len(md_files), 1, f"expected one deck file, got {[f.name for f in md_files]}"
        )
        cards = parse_deck(md_files[0].read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["hola?", "adios?"])

    def test_add_refuses_when_deck_name_collides_between_two_physical_files(self):
        # `cmd_sync` already refuses to touch a deck when two physically
        # different files both normalize to the same deck name (session 155)
        # rather than gamble on which one is "real". `_find_deck_path` picks
        # the first (sort-order) match regardless, and `add` used to trust
        # that pick blindly: it silently wrote the new card into whichever
        # file sorted first -- which could easily be a throwaway or unrelated
        # file, not the deck's real, already-established one -- with a
        # cheerful "added to ..." message giving no hint that a second,
        # colliding file even existed.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: uno\nA: one\n", encoding="utf-8")
        other = self.decks_dir / f"{nfd}.md"
        other.write_text("Q: tres\nA: three\n", encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("add", nfc, "-q", "cuatro?", "-a", "four")
        self.assertEqual(rc, 1)
        self.assertIn("collide", stderr.getvalue())

        # Neither file was touched -- not the established one, and not the
        # colliding one either, since there's no way to tell which is real.
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: uno\nA: one\n")
        self.assertEqual(other.read_text(encoding="utf-8"), "Q: tres\nA: three\n")

    def test_collision_error_lets_the_two_colliding_paths_be_told_apart(self):
        # The message above tells a person to "rename the files so they're
        # distinct" -- but an NFC-named file and an NFD-named file collide
        # *because* they render as the exact same glyphs on screen ("café"
        # either way), so joining plain str(path) for each one (as this
        # message used to) printed the identical-looking path twice, e.g.
        # "...café.md, ...café.md", with no way for a person reading it to
        # tell which listed path is which real file on disk, let alone act
        # on the instruction to rename one of them.
        #
        # repr() doesn't fix this either: Python only escapes a string's
        # *unprintable* characters, and NFD's combining acute accent
        # (U+0301) is printable -- it just renders merged with the letter
        # before it -- so repr() of the NFD name still prints as plain
        # "café", identical to the NFC one. Only ascii(), which forces every
        # non-ASCII character to an escaped \xXX/\uXXXX form regardless of
        # printability, actually makes the two paths look different
        # ("caf\xe9.md" vs "café.md") -- so that's what the error
        # message must contain, not the human-visible spelling.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        nfc_path = self.decks_dir / f"{nfc}.md"
        nfd_path = self.decks_dir / f"{nfd}.md"
        nfc_path.write_text("Q: uno\nA: one\n", encoding="utf-8")
        nfd_path.write_text("Q: tres\nA: three\n", encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("add", nfc, "-q", "cuatro?", "-a", "four")
        self.assertEqual(rc, 1)
        message = stderr.getvalue()
        self.assertIn(ascii(str(nfc_path)), message)
        self.assertIn(ascii(str(nfd_path)), message)

    def test_refuses_when_a_colliding_file_appears_during_the_interactive_prompt(self):
        # `_check_deck_collision` above only runs once, before the (possibly
        # interactive, possibly arbitrarily long -- see cmd_edit's own
        # docstring for why that's not hypothetical) `-q`/`-a` prompts. A
        # second, colliding deck file -- hand-created, or written by another
        # flashback invocation entirely, both explicitly normal per
        # `_find_deck_path`'s own docstring -- can appear in that window,
        # after the one-time check already passed. Without a fresh check
        # right before the write, `add` would go on to use the now-stale
        # `deck_path` computed before the second file existed, writing a
        # brand new, unrelated file for this deck name instead of refusing
        # the way it already does when the collision exists from the start.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: uno\nA: one\n", encoding="utf-8")

        def fake_input(prompt):
            (self.decks_dir / f"{nfd}.md").write_text("Q: tres\nA: three\n", encoding="utf-8")
            return "dos?" if prompt == "Q: " else "two"

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("add", nfc)
        self.assertEqual(rc, 1)
        self.assertIn("collide", err.getvalue())

        # Neither the established file nor the newly-appeared one was
        # touched -- add must not guess which one "wins".
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: uno\nA: one\n")
        self.assertEqual(
            (self.decks_dir / f"{nfd}.md").read_text(encoding="utf-8"), "Q: tres\nA: three\n"
        )
        self.assertEqual(len(list(self.decks_dir.glob("*.md"))), 2)

    def test_appends_to_a_file_that_appears_during_the_interactive_prompt_instead_of_duplicating_it(self):
        # The narrower sibling of the case above: this deck has *no* file at
        # all when `add` starts (so the one-time collision check, and
        # `_find_deck_path`'s own guessed path, both see zero candidates),
        # but exactly one appears -- hand-created, or by another process --
        # during the prompts. That's not a collision by `_check_deck_collision`'s
        # own definition (only one file exists either way), but the guessed
        # path computed before the file existed is now simply wrong: without
        # re-resolving it fresh, `add` would silently create a *second*,
        # unrelated file at the stale guessed path instead of appending to
        # the real one that just appeared, with a cheerful "added to ..."
        # message giving no hint that anything went wrong.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        def fake_input(prompt):
            self.decks_dir.mkdir(parents=True, exist_ok=True)
            (self.decks_dir / f"{nfd}.md").write_text("Q: uno\nA: one\n", encoding="utf-8")
            return "dos?" if prompt == "Q: " else "two"

        with patch("builtins.input", side_effect=fake_input):
            rc = self.run_flashback("add", nfc)
        self.assertEqual(rc, 0)

        md_files = sorted(self.decks_dir.glob("*.md"))
        self.assertEqual(
            len(md_files), 1, f"expected one deck file, got {[f.name for f in md_files]}"
        )
        cards = parse_deck(md_files[0].read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["uno", "dos?"])

    def test_deck_name_with_leading_or_trailing_whitespace_is_the_same_deck(self):
        # _normalize_deck_name NFC-normalized a deck name but never stripped
        # surrounding whitespace, unlike question/answer text — so a plain
        # typo like a trailing space ("spanish " instead of "spanish")
        # silently created a second, unrelated deck file and database row,
        # rather than being folded into the existing "spanish" deck. Worse
        # than the Unicode-normalization case: `stats`'s deck-name column is
        # padded to a fixed width, so "spanish" and "spanish " render as
        # visually identical rows, making the resulting duplicate look like a
        # bug in flashback itself rather than a typo in the deck name that
        # created it.
        rc1 = self.run_flashback("add", "spanish", "-q", "Q1", "-a", "A1")
        rc2 = self.run_flashback("add", "spanish ", "-q", "Q2", "-a", "A2")
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)

        md_files = sorted(self.decks_dir.glob("*.md"))
        self.assertEqual(
            len(md_files), 1, f"expected one deck file, got {[f.name for f in md_files]}"
        )
        cards = parse_deck(md_files[0].read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["Q1", "Q2"])

    def test_question_with_unicode_line_separator_is_rejected(self):
        # U+2028 (LINE SEPARATOR) — a real-world hazard since some word
        # processors and PDF viewers insert it for soft line breaks on
        # copy-paste — isn't a control character and doesn't reorder
        # anything, so it used to sail straight through `add`. But
        # str.splitlines(), which the parser uses everywhere to find line
        # boundaries, treats it exactly like a real "\n": the question
        # `add` wrote to the deck file wasn't the question the very next
        # `sync` (or a `remove`/`edit` lookup using that same original text)
        # read back. Rejecting it here, like every other character that
        # doesn't round-trip safely, closes that gap.
        rc = self.run_flashback("add", "spanish", "-q", "before after", "-a", "hola")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "spanish.md").exists())

    def test_added_question_is_findable_by_remove_using_the_exact_same_text(self):
        # A general round-trip sanity check: whatever text `add` accepts for
        # a question must still compare equal to itself after being written
        # to the deck file and re-parsed, or `remove`/`edit` can never find
        # the card again by the question the user actually typed.
        question = "capital of France?"
        rc = self.run_flashback("add", "geo", "-q", question, "-a", "Paris")
        self.assertEqual(rc, 0)
        rc = self.run_flashback("remove", "geo", "-q", question)
        self.assertEqual(rc, 0)

    def test_add_seeds_state_dir_gitignore_even_though_it_never_touches_the_db(self):
        # add/remove/edit never call open_db, only _deck_lock — this is the
        # one place a fresh --state-dir could be created without ever going
        # through open_db's own .gitignore seeding, if the two paths weren't
        # both wired to the same helper.
        self.assertFalse(self.state_dir.exists())
        rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 0)
        self.assertEqual((self.state_dir / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_add_seeds_state_dir_gitignore_even_when_state_dir_is_the_decks_dir(self):
        # `--decks-dir . --state-dir .` (or any other spelling that makes the
        # two coincide) is a real, anticipated configuration -- ensure_state_dir's
        # own docstring and _deck_lock_path's docstring both single out
        # `--state-dir .` by name as a real reason two flashback invocations
        # can share -- or a single invocation's --decks-dir/--state-dir can
        # themselves be -- the very same directory, not just a hypothetical.
        #
        # `cmd_add` creates a brand-new --decks-dir itself, via
        # `decks_dir.mkdir(...)`, *before* ever entering `_deck_lock` -- the
        # one place that seeds --state-dir's .gitignore via `ensure_state_dir`.
        # When the two paths are the same not-yet-existing directory, that
        # earlier mkdir has already brought it into existence by the time
        # `ensure_state_dir` gets to ask "is this --state-dir new?", so the
        # answer comes back "no" even though, from --state-dir's own
        # perspective, this is genuinely the first time it's ever been
        # touched -- silently skipping the exact .gitignore protection
        # ensure_state_dir exists to provide, right when a freshly-created,
        # git-committable directory needs it most.
        shared = Path(self._tmp.name) / "shared"
        self.assertFalse(shared.exists())
        rc = main(["--decks-dir", str(shared), "--state-dir", str(shared), "add", "spanish", "-q", "hello?", "-a", "hola"])
        self.assertEqual(rc, 0)
        self.assertEqual((shared / ".gitignore").read_text(encoding="utf-8"), "*\n")

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

    def test_deck_name_of_dot_or_dotdot_error_does_not_blame_a_path_separator(self):
        # "." and ".." are rejected by the same `if` as an actual "/" or "\\" in
        # the name, but neither one *contains* a path separator -- so the shared
        # error message text ("deck names can't contain a path separator") is
        # simply false when it fires for one of these two, not just imprecise:
        # a user hitting this for `flashback add . ...` or `flashback add .. ...`
        # sees a reason that doesn't match what they actually typed, with no
        # slash anywhere in sight to explain it.
        for name in (".", ".."):
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = self.run_flashback("add", name, "-q", "hola?", "-a", "hello")
            self.assertEqual(rc, 1)
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)
            self.assertNotIn("path separator", stderr.getvalue())

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

    def test_deck_name_with_unicode_line_separator_is_rejected(self):
        # U+2028 (LINE SEPARATOR) isn't a control character (category Zl, not
        # Cc) and doesn't reorder anything, so neither of _invalid_deck_name's
        # existing checks catches it — but parser.py's own LINE_SEPARATOR_CHARS
        # check (used for question/answer text in _check_card_text) exists
        # precisely because every place this codebase finds line boundaries
        # treats U+2028 exactly like a real "\n". _invalid_deck_name's own
        # docstring says a deck name is rejected control characters "since
        # either one already breaks stats's tabular layout" — U+2028 breaks
        # that same tabular layout when a terminal renders it as a line
        # break, so it should be rejected here for the same reason, the same
        # way it already is for card text.
        rc = self.run_flashback("add", "evil deck", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_unpaired_surrogate_is_rejected(self):
        # A lone surrogate (U+D800-U+DFFF) reaches here as an ordinary Python
        # str whenever sys.argv decodes non-UTF-8 command-line bytes with the
        # 'surrogateescape' handler (PEP 383) -- a stray byte from a
        # mismatched locale or copy-pasted mojibake, not anything exotic.
        # Without this check, _invalid_deck_name let it through, and the
        # actual failure only surfaced later in _atomic_write_text's
        # encode="utf-8" write, as a raw UnicodeEncodeError that main()'s own
        # handler then misdiagnosed as a terminal-output-encoding problem
        # (suggesting a UTF-8 locale, which can't fix an unpaired surrogate).
        rc = self.run_flashback("add", "evil\udcffdeck", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_unicode_tag_character_is_rejected(self):
        # A Unicode "Tags" block character (U+E0000-U+E007F) has no visible
        # glyph in any font — it's category Cf, same as the ZWJ/variation
        # selectors a legitimate emoji-bearing deck name relies on, so
        # neither the Cc check nor a blanket Cf rejection catches (or should
        # catch) it. Without this check, two decks whose names print
        # identically in `sync`'s listing could actually be different names
        # underneath, and a `--deck` value typed to match what's displayed
        # would silently fail to match the deck it looks identical to — the
        # same "looks the same, isn't" risk this check already blocks for
        # card text via parser.py's `_is_unicode_tag_char`.
        rc = self.run_flashback("add", f"evil{chr(0xE0041)}deck", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_byte_order_mark_is_rejected(self):
        # U+FEFF (the UTF-8/UTF-16 byte-order mark) is invisible everywhere
        # outside position zero of a file -- the one place _read_deck_text
        # already strips it -- so a deck name with one spliced into the
        # middle (e.g. built by a script that concatenates a BOM-prefixed
        # value) prints identically to the same name without it in every
        # listing this tool produces, while comparing unequal to it: the
        # same "looks the same, isn't" risk already blocked for a Unicode
        # tag character just above.
        rc = self.run_flashback("add", "evil﻿deck", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_deck_name_with_zero_width_space_is_rejected(self):
        # U+200B (ZERO WIDTH SPACE) is invisible in every renderer, the same
        # "looks the same, isn't" risk already blocked above for the Tags
        # block and the byte-order mark -- but unlike ZWJ/ZWNJ, it isn't
        # part of any legitimate emoji or script-shaping sequence, so
        # rejecting it costs nothing.
        rc = self.run_flashback("add", f"evil{chr(0x200B)}deck", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertFalse(self.decks_dir.exists())

    def test_answer_with_unpaired_surrogate_is_rejected_without_writing_file(self):
        # Same failure shape as the deck-name case above, just for card text:
        # caught here as a clean ParseError instead of crashing later in
        # _atomic_write_text with a UnicodeEncodeError that main() then
        # misreports as a terminal-encoding problem.
        rc = self.run_flashback("add", "trivia", "-q", "capital of France?", "-a", "answer with \udcff in it")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "trivia.md").exists())

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

    def test_adding_to_a_deck_file_with_a_utf8_bom_succeeds(self):
        # Notepad and various other editors/export tools default to writing a
        # UTF-8 byte-order-mark (U+FEFF) at the start of a file. Reading with
        # plain "utf-8" decodes that BOM as a real character rather than
        # stripping it, so it lands as the first character of the first
        # line — turning "Q: hello?" into "﻿Q: hello?", which doesn't
        # match Q_PREFIX. That used to make _parse_card treat the whole first
        # card as stray text before any "Q:" line and reject it, so `add`
        # failed on every deck file saved with a BOM, even though the
        # content is otherwise perfectly well-formed.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "spanish.md").write_bytes(b"\xef\xbb\xbfQ: hello?\nA: hola\n")

        rc = self.run_flashback("add", "spanish", "-q", "bye?", "-a", "adios")
        self.assertEqual(rc, 0)

        cards = {c.question: c.answer for c in parse_deck((self.decks_dir / "spanish.md").read_text(encoding="utf-8-sig"))}
        self.assertEqual(cards, {"hello?": "hola", "bye?": "adios"})

    def test_non_utf8_existing_deck_file_fails_cleanly_instead_of_a_raw_traceback(self):
        # `sync` already skips a deck file that isn't valid UTF-8 instead of
        # crashing (session 47) — but `add` reads the *specific* deck file it
        # was told to add to with a plain Path.read_text and no such guard,
        # and UnicodeDecodeError is a ValueError subclass main()'s existing
        # OSError/sqlite3.Error handlers don't catch either. Adding a card to
        # an existing, corrupted deck used to crash with a raw traceback
        # exposing local paths instead of the clean "error: ..." shape every
        # other failure in this file gets.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "spanish.md").write_bytes(b"Q: caf\xe9?\nA: coffee\n")

        rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 1)

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

    @unittest.skipIf(os.name == "nt", "the lock this guards against is POSIX-only (fcntl)")
    def test_concurrent_adds_to_the_same_deck_from_different_state_dirs_do_not_lose_cards(self):
        # Same lost-update race as test_concurrent_adds_to_the_same_deck_do_not_lose_cards
        # above, but with each worker using its own --state-dir while sharing
        # one --decks-dir -- an ordinary thing for two flashback invocations
        # to do (nothing ties --state-dir to a particular --decks-dir, and
        # --state-dir's default is relative, so simply running from two
        # different working directories against one shared, absolute
        # --decks-dir already does this by accident).
        #
        # _deck_lock used to key its lock file's path purely off --state-dir
        # (a file under `Path(args.state_dir) / "locks"`), so two invocations
        # with different --state-dirs never contended on the same lock at
        # all -- each thought it alone was serializing access to the deck
        # file, while in fact nothing was serializing them against each
        # other. Confirmed directly against the pre-fix code: 16 concurrent
        # `add`s to a fresh deck, one per distinct --state-dir, lost roughly
        # half of the 16 cards to exactly this silent lost update, with every
        # worker still printing a normal "added" message and exiting 0.
        barrier = threading.Barrier(8)
        errors = []

        def worker(i):
            state_dir = Path(self._tmp.name) / f"state-{i}"
            barrier.wait()
            try:
                rc = main(
                    [
                        "--decks-dir",
                        str(self.decks_dir),
                        "--state-dir",
                        str(state_dir),
                        "add",
                        "spanish",
                        "-q",
                        f"q{i}?",
                        "-a",
                        f"a{i}",
                    ]
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
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual({c.question for c in cards}, {f"q{i}?" for i in range(8)})

    @unittest.skipIf(os.name == "nt", "the lock this guards against is POSIX-only (fcntl)")
    def test_concurrent_adds_via_symlinked_decks_sharing_one_real_file_do_not_lose_cards(self):
        # Same lost-update race as test_concurrent_adds_to_the_same_deck_do_not_lose_cards
        # above, but reached through a symlink instead of a shared --decks-dir
        # or a shared --state-dir. _atomic_write_text already documents a
        # symlinked deck file as normal ("a deck file kept somewhere else and
        # linked into decks_dir -- e.g. a shared repo of deck content"), which
        # means the *real* file two `add`s race over can be identical even
        # when each `add` uses its own, distinct --decks-dir: two
        # collaborators each pointing a personal decks directory's
        # "spanish.md" at one shared file via a symlink, say.
        #
        # `_deck_lock_path` used to key its lock file purely off `decks_dir`'s
        # own resolved path plus the deck name -- never resolving `deck_path`
        # itself -- so two `add`s through two different `--decks-dir`s (each
        # containing nothing but a symlink to the one shared real file) got
        # two different lock keys despite both actually writing the same
        # target, reintroducing the exact silent lost-update race this lock
        # exists to prevent, just through a symlink instead of the
        # `--state-dir` door the preceding test already closed. Confirmed
        # directly against the pre-fix code: 8 concurrent `add`s, one per
        # personal --decks-dir all symlinking the same shared deck file, lost
        # 4 of 8 cards, every worker still printing a normal "added" message
        # and exiting 0.
        shared = Path(self._tmp.name) / "shared-spanish.md"
        shared.write_text("", encoding="utf-8")
        decks_dirs = []
        for i in range(8):
            d = Path(self._tmp.name) / f"decks-{i}"
            d.mkdir()
            os.symlink(shared, d / "spanish.md")
            decks_dirs.append(d)

        barrier = threading.Barrier(8)
        errors = []

        def worker(i):
            state_dir = Path(self._tmp.name) / f"state-{i}"
            barrier.wait()
            try:
                rc = main(
                    [
                        "--decks-dir",
                        str(decks_dirs[i]),
                        "--state-dir",
                        str(state_dir),
                        "add",
                        "spanish",
                        "-q",
                        f"q{i}?",
                        "-a",
                        f"a{i}",
                    ]
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
        cards = parse_deck(shared.read_text(encoding="utf-8"))
        self.assertEqual({c.question for c in cards}, {f"q{i}?" for i in range(8)})

    @unittest.skipIf(os.name == "nt", "the lock this guards against is POSIX-only (fcntl)")
    def test_concurrent_adds_survive_the_deck_file_being_renamed_to_its_nfd_form_mid_race(self):
        # Regression test for a lock-key staleness bug: cmd_add computes
        # deck_path once (a guess via _find_deck_path), then -- after
        # whatever the -q/-a prompts above take, which can run arbitrarily
        # long -- used *that same, possibly stale* deck_path to build the
        # deck lock's key, even though it goes on to re-resolve deck_path
        # *again*, freshly, immediately before actually reading/writing,
        # specifically because the file can have been renamed to a
        # different (but equally valid) Unicode normalization form of the
        # same deck name in that window (see _find_deck_path's own
        # docstring for why that's real, not hypothetical -- an NFD-named
        # file replacing an NFC one, e.g. via a filename-normalizing
        # script). Locking on the stale pre-rename guess while reading and
        # writing whatever the fresh re-resolve finds meant two concurrent
        # adds to the exact same real target could end up computing two
        # different lock keys and never actually serialize against each
        # other at all -- confirmed directly against the pre-fix code below
        # via the one deliberately-injected rename this test forces.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        nfc_name = unicodedata.normalize("NFC", "café")
        nfd_name = unicodedata.normalize("NFD", "café")
        nfc_path = self.decks_dir / f"{nfc_name}.md"
        nfd_path = self.decks_dir / f"{nfd_name}.md"
        nfc_path.write_text("Q: existing?\nA: yes\n", encoding="utf-8")

        from flashback import cli

        real_find_deck_path = cli._find_deck_path
        b_done = threading.Event()
        thread_a_holder = {}
        real_atomic_write_text = cli._atomic_write_text

        # Simulates another process (or a person, or a normalizing script)
        # renaming the deck file to its NFD spelling in the window between
        # add's first, pre-prompt lookup and its lock-protected re-resolve
        # -- exactly the window `_find_deck_path`'s own docstring describes.
        # Fires on the very first call, whichever of the two racing `add`s
        # happens to make it, so which one ends up the "victim" holding a
        # stale lock key isn't fixed in advance -- only that one of them
        # does.
        first_call_done = threading.Event()

        def fake_find_deck_path(decks_dir_arg, deck_name_arg):
            result = real_find_deck_path(decks_dir_arg, deck_name_arg)
            if not first_call_done.is_set():
                first_call_done.set()
                if nfc_path.exists():
                    os.rename(nfc_path, nfd_path)
            return result

        # Forces thread A's read-modify-write to straddle thread B's own
        # full read-modify-write cycle: without an actual pause here, both
        # adds could easily just run one after another, which would be
        # correct (if slow) even with the bug. A short timeout (rather than
        # an unconditional wait) means this can't hang the suite if the fix
        # already serializes the two adds under one real lock -- in that
        # case B blocks on the OS-level lock A holds, never reaches the
        # point that sets b_done, and A simply proceeds once the timeout
        # lapses, by which point there is nothing left to race.
        def fake_atomic_write_text(path, data):
            if threading.current_thread() is thread_a_holder.get("thread"):
                b_done.wait(timeout=2)
            return real_atomic_write_text(path, data)

        def worker_a():
            rc = self.run_flashback("add", nfc_name, "-q", "from-a?", "-a", "A")
            self.assertEqual(rc, 0)

        def worker_b():
            first_call_done.wait(timeout=2)
            rc = self.run_flashback("add", nfc_name, "-q", "from-b?", "-a", "B")
            self.assertEqual(rc, 0)
            b_done.set()

        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)
        thread_a_holder["thread"] = thread_a

        with patch("flashback.cli._find_deck_path", side_effect=fake_find_deck_path), patch(
            "flashback.cli._atomic_write_text", side_effect=fake_atomic_write_text
        ):
            thread_a.start()
            thread_b.start()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())

        final_path = nfd_path if nfd_path.exists() else nfc_path
        cards = parse_deck(final_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {c.question for c in cards},
            {"existing?", "from-a?", "from-b?"},
            "one add's card was silently lost -- the two concurrent adds locked "
            "on two different keys for what was, the whole time, the exact same "
            "real deck file",
        )


class TestRemoveCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
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

    @unittest.skipIf(os.name == "nt", "symlinked deck files aren't exercised on Windows")
    def test_self_referential_symlink_deck_file_fails_with_accurate_message(self):
        # `add` (see test_add_to_self_referential_symlink_deck_file_fails_cleanly
        # in TestAddCommand) already reports a self-referential symlink loop
        # (`ln -s spanish.md spanish.md`, or a longer chain) as exactly that
        # -- a real deck file that genuinely exists but can never be resolved
        # to actual content, not a deck that was never created.
        #
        # `remove`'s own up-front check here used a plain `deck_path.exists()`
        # to decide whether to bother reading the file at all -- but
        # `Path.exists()` follows symlinks, and a loop can never resolve, so
        # it reports False for one exactly the same way it does for a deck
        # that's never existed. Without a check for the loop specifically,
        # `remove` (and `edit`, and `sync` reading the same file) said "no
        # such deck"/"no longer exists -- it may have been deleted", which is
        # false on both counts: the file was never deleted, and never even
        # touched -- it's sitting right there the whole time, just as an
        # unusable loop.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        link_path = self.decks_dir / "spanish.md"
        os.symlink(link_path, link_path)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("remove", "spanish", "-q", "hello?")
        self.assertEqual(rc, 1)
        message = err.getvalue()
        self.assertIn("symlink loop", message.lower())
        self.assertNotIn("no such deck", message)
        self.assertNotIn("no longer exists", message)

    def test_deck_file_deleted_while_prompting_for_question_fails_with_accurate_message(self):
        # `remove`'s existence check runs before the (possibly interactive)
        # `-q` prompt, then the file is read again for real inside
        # `_deck_lock` afterward -- a window in which another process (a
        # concurrent `remove` + `sync`, or a person deleting the file by
        # hand) can delete the deck file entirely. `_read_deck_text` used to
        # blame this on the file being "a FIFO, device, socket, or similar
        # special file, or a directory" -- the same message a FIFO gets --
        # which is simply false when the real cause is that the path no
        # longer exists at all.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"

        def fake_input(prompt):
            deck_path.unlink()
            return "hello?"

        out = io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stderr(out):
            rc = self.run_flashback("remove", "spanish")
        self.assertEqual(rc, 1)
        message = out.getvalue()
        self.assertIn("no longer exists", message)
        self.assertNotIn("FIFO", message)

    def test_refuses_when_a_colliding_file_appears_during_the_interactive_prompt(self):
        # `_check_deck_collision` runs once, before the (possibly
        # interactive) `-q` prompt -- but a second, colliding deck file
        # (hand-created, or written by an unrelated process; both explicitly
        # normal per `_find_deck_path`'s own docstring) can appear while
        # `remove` is sitting at that prompt. Without a fresh check right
        # before the write, `remove` would silently go on to modify the one
        # file it already knew about, leaving the deck in a now-colliding
        # state with no warning at all in its own "removed from ..."
        # success message -- even though invoking `remove` fresh at that
        # point would refuse immediately.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: hola\nA: hello\n", encoding="utf-8")

        def fake_input(prompt):
            (self.decks_dir / f"{nfd}.md").write_text("Q: adios\nA: bye\n", encoding="utf-8")
            return "hola"

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("remove", nfc)
        self.assertEqual(rc, 1)
        self.assertIn("collide", err.getvalue())
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: hola\nA: hello\n")

    def test_finds_deck_file_renamed_during_the_interactive_prompt(self):
        # The narrower sibling of the collision case just above, same shape
        # as add's own "appears during the prompt" test: this deck starts
        # with exactly *one* file, which gets renamed -- not duplicated -- to
        # a different (but visually identical) Unicode normalization form of
        # the same name while `remove` is sitting at the `-q` prompt. That's
        # never a "collision" by _check_deck_collision's own definition
        # (still only one file, either way), so the fresh recheck the
        # collision case relies on can't catch it -- `deck_path` itself, computed
        # from the *old* name before the rename, has to be re-resolved too.
        # Without that, `remove` read the now-stale path, found nothing
        # there, and wrongly reported the deck as gone entirely, even though
        # `stats`/`sync` would still show it as a real, populated deck under
        # its new on-disk name.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        original = self.decks_dir / f"{nfc}.md"
        original.write_text("Q: hola\nA: hello\n", encoding="utf-8")
        renamed = self.decks_dir / f"{nfd}.md"

        def fake_input(prompt):
            original.rename(renamed)
            return "hola"

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("remove", nfc)
        self.assertEqual(rc, 0, err.getvalue())

        self.assertEqual([p.name for p in self.decks_dir.glob("*.md")], [f"{nfd}.md"])
        self.assertEqual(parse_deck(renamed.read_text(encoding="utf-8")), [])

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

    def test_matches_question_with_differing_unicode_normalization_form(self):
        # "é" can be spelled as one precomposed codepoint (NFC) or as "e"
        # plus a combining acute accent (NFD) — both render identically, the
        # same way "hello?" and "  hello?  " both read as the same question.
        # -q in a different, but visually indistinguishable, normalization
        # form than what was stored must still match.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)
        self.run_flashback("add", "spanish", "-q", nfc, "-a", "coffee shop")

        rc = self.run_flashback("remove", "spanish", "-q", nfd)
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        self.assertEqual(parse_deck(deck_path.read_text(encoding="utf-8")), [])

    def test_finds_deck_file_whose_on_disk_name_is_a_different_unicode_normalization_form(self):
        # A deck's *file name* isn't guaranteed to already be NFC, even though
        # _normalize_deck_name always normalizes the --deck argument to NFC
        # before it's used: deck files are documented as normal to hand-create
        # or hand-rename outside the CLI (see the control-character-named-file
        # test in TestSyncCommand), and macOS's filesystem (HFS+/APFS) stores
        # accented file names as NFD by default — a byte-for-byte NFD name
        # that survives a `git clone` onto Linux untouched, since git stores
        # file names as literal bytes. `sync` already normalizes
        # `deck_file.stem` before using it as the deck's identity, so `stats`/
        # `due`/`review`/`hard` all correctly show such a deck as existing and
        # populated. `remove` has to find the same file `sync` found — not
        # just guess `decks_dir / f"{normalized_name}.md"`, which only ever
        # matches a file that's already NFC and reports a false "no such
        # deck" against one that plainly does exist (`stats` just said so).
        nfd = unicodedata.normalize("NFD", "café")
        nfc = unicodedata.normalize("NFC", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / f"{nfd}.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        # Confirm sync really does treat this as an existing, populated deck
        # (the premise of the bug: remove disagreeing with sync/stats).
        with open_db(self.state_dir / "state.sqlite3") as conn:
            self.assertEqual(len(due_cards(conn, date.today())), 1)

        rc = self.run_flashback("remove", nfc, "-q", "hola?")
        self.assertEqual(rc, 0)

        cards = parse_deck((self.decks_dir / f"{nfd}.md").read_text(encoding="utf-8"))
        self.assertEqual(cards, [])
        # No second, colliding file should have been created.
        self.assertEqual([p.name for p in self.decks_dir.glob("*.md")], [f"{nfd}.md"])

    def test_remove_refuses_when_deck_name_collides_between_two_physical_files(self):
        # Same collision `cmd_sync` already refuses to touch (session 155),
        # reached through `remove` instead: `_find_deck_path` used to pick
        # whichever of the two colliding files sorted first regardless, so
        # `remove` could silently report success while editing a file that
        # had nothing to do with the deck's real, already-established cards
        # -- or, as reproduced here, fail with a false "no card with that
        # question found" for a question that's really there, just in the
        # *other* colliding file.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: uno\nA: one\n", encoding="utf-8")
        other = self.decks_dir / f"{nfd}.md"
        other.write_text("Q: tres\nA: three\n", encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("remove", nfc, "-q", "uno")
        self.assertEqual(rc, 1)
        self.assertIn("collide", stderr.getvalue())

        # Neither file was touched.
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: uno\nA: one\n")
        self.assertEqual(other.read_text(encoding="utf-8"), "Q: tres\nA: three\n")

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

    def test_non_utf8_deck_file_fails_cleanly_instead_of_a_raw_traceback(self):
        # Same reasoning as add's equivalent test: `remove` reads the
        # specific deck file it was told to touch with a plain
        # Path.read_text and no UnicodeDecodeError guard, unlike `sync`.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "spanish.md").write_bytes(b"Q: caf\xe9?\nA: coffee\n")

        rc = self.run_flashback("remove", "spanish", "-q", "hello?")
        self.assertEqual(rc, 1)

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
        _patch_lock_dir(self)
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

    @unittest.skipIf(os.name == "nt", "symlinked deck files aren't exercised on Windows")
    def test_self_referential_symlink_deck_file_fails_with_accurate_message(self):
        # See TestRemoveCommand's identical test: `edit`'s own up-front
        # `deck_path.exists()` check has the same blind spot as `remove`'s --
        # a self-referential symlink loop reports False from `.exists()`
        # exactly like a deck that never existed, so this used to be
        # misreported as "no such deck" instead of the accurate "symlink
        # loop" diagnosis `_read_deck_text` already gives `sync`/`add`.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        link_path = self.decks_dir / "spanish.md"
        os.symlink(link_path, link_path)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "x")
        self.assertEqual(rc, 1)
        message = err.getvalue()
        self.assertIn("symlink loop", message.lower())
        self.assertNotIn("no such deck", message)
        self.assertNotIn("no longer exists", message)

    def test_deck_file_deleted_during_interactive_prompt_fails_with_accurate_message(self):
        # cmd_edit's own docstring notes that existing_text is deliberately
        # re-read fresh, inside the lock, rather than reusing the text read
        # for the preview -- "the interactive prompting in between can take
        # arbitrarily long, and the file may have changed since preview_text
        # was read". A concurrent process (or a person by hand) deleting the
        # deck file during that window is exactly the race that comment
        # anticipates, but the resulting error used to misdiagnose the
        # cause: _read_deck_text blamed a missing file on being "a FIFO,
        # device, socket, or similar special file, or a directory" -- the
        # same wording a FIFO gets -- which is false when the file simply
        # isn't there anymore.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"

        prompted = []

        def fake_input(prompt):
            prompted.append(prompt)
            if len(prompted) == 1:
                # about to answer "new Q (blank to keep): " -- simulate
                # another process deleting the deck file while the user is
                # still sitting at this prompt.
                deck_path.unlink()
                return ""
            return "hola!"

        out = io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(out):
            rc = self.run_flashback("edit", "spanish", "-q", "hello?")
        self.assertEqual(rc, 1)
        message = out.getvalue()
        self.assertIn("no longer exists", message)
        self.assertNotIn("FIFO", message)

    def test_refuses_when_a_colliding_file_appears_during_the_interactive_prompt(self):
        # Same race as the deletion case just above, but for a colliding
        # file appearing instead of the deck file disappearing: `edit`'s
        # one-time `_check_deck_collision`, run before the interactive
        # prompts, can't see a second, colliding deck file (hand-created, or
        # written by an unrelated process -- both explicitly normal per
        # `_find_deck_path`'s own docstring) that appears while `edit` is
        # sitting at one of those prompts. Without a fresh check right
        # before the write, `edit` would silently go on to modify the file
        # it already knew about, leaving the deck in a now-colliding state
        # with no warning in its own "edited in ..." success message.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: hola\nA: hello\n", encoding="utf-8")

        prompted = []

        def fake_input(prompt):
            prompted.append(prompt)
            if len(prompted) == 1:
                # about to answer "new Q (blank to keep): " -- simulate
                # another process/person creating a colliding deck file
                # while still sitting at this prompt.
                (self.decks_dir / f"{nfd}.md").write_text("Q: adios\nA: bye\n", encoding="utf-8")
                return ""
            return "hi"

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            # Neither --new-question nor --new-answer given, so cmd_edit goes
            # interactive and prompts for both -- the "new A" answer ("hi")
            # is what would have been written, had the fresh recheck not
            # caught the collision first.
            rc = self.run_flashback("edit", nfc, "-q", "hola")
        self.assertEqual(rc, 1)
        self.assertIn("collide", err.getvalue())
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: hola\nA: hello\n")

    def test_refuses_when_a_colliding_file_appears_during_the_dash_q_prompt(self):
        # Narrower still than the collision case just above: that test only
        # covers a colliding file appearing during the *second* interactive
        # window (the new-Q/new-A prompts, after a card's already been found
        # and its preview printed) -- and by then, a fresh
        # _check_deck_collision right before the final write already catches
        # it. This one covers the *first* window -- the "-q" prompt itself,
        # when -q is omitted -- which sits before the preview read's own
        # deck_path re-resolve. That re-resolve used to run with no fresh
        # _check_deck_collision of its own (unlike every other deck_path
        # re-resolve in add/remove/edit), so a second, colliding deck file
        # appearing while the user is still sitting at "Q: " let
        # `_find_deck_path` silently pick one of the two colliding files by
        # sort order and print *its* content as the "current Q/A" preview --
        # content that might not even belong to the deck the user thinks
        # they're editing -- with no hint that a collision existed. Neither
        # --new-question nor --new-answer is passed here, so a real session
        # goes on to show that misleading preview and prompt for new text.
        # If the user then answers both prompts blank (keeping the -- as far
        # as they can tell -- unchanged card), `cmd_edit` prints "nothing
        # changed" and returns 0 *before* ever reaching the final-write
        # recheck inside the lock: the collision is never reported at all,
        # not even the "refuses but explains why" outcome the second-window
        # collision case above gets. Only a user who actually types a new
        # answer reaches that later recheck and gets an (equally late)
        # honest error.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: hola\nA: hello (real answer)\n", encoding="utf-8")

        prompted = []

        def fake_input(prompt):
            prompted.append(prompt)
            if prompt == "Q: ":
                # Another process/person creates a colliding deck file while
                # still sitting at this very first prompt.
                (self.decks_dir / f"{nfd}.md").write_text(
                    "Q: hola\nA: WRONG unrelated answer from the colliding file\n", encoding="utf-8"
                )
                return "hola"
            # Only reached on unfixed code, which reads the wrong file's
            # content, finds a "match" there too, and goes on to prompt for
            # new text based on it.
            return ""

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("edit", nfc)
        self.assertEqual(rc, 1)
        self.assertIn("collide", err.getvalue())
        # The fix catches the collision right after the "-q" prompt, before
        # ever reading a preview or prompting for new text -- so the
        # interactive session must stop there, having asked exactly one
        # question.
        self.assertEqual(prompted, ["Q: "])
        # No misleading preview of either file's content was ever printed.
        self.assertNotIn("current A:", out.getvalue())
        self.assertNotIn("WRONG unrelated answer", out.getvalue())
        # Neither colliding file was touched.
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: hola\nA: hello (real answer)\n")
        self.assertEqual(
            (self.decks_dir / f"{nfd}.md").read_text(encoding="utf-8"),
            "Q: hola\nA: WRONG unrelated answer from the colliding file\n",
        )

    def test_finds_deck_file_renamed_during_the_interactive_prompt(self):
        # The narrower sibling of the collision case just above, same shape
        # as add's own "appears during the prompt" test (and remove's
        # identical sibling test): this deck starts with exactly *one* file,
        # renamed -- not duplicated -- to a different Unicode normalization
        # form of the same name while `edit` is sitting at the `-q` prompt.
        # Never a "collision" by _check_deck_collision's own definition, so
        # the fresh recheck alone can't catch it; `deck_path` itself has to
        # be re-resolved too, both before the preview read and again inside
        # the lock. Without that, edit's preview read used the now-stale
        # path, found nothing there, and wrongly reported the deck as gone
        # entirely, even though `stats`/`sync` would still show it as a real,
        # populated deck under its new on-disk name.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        original = self.decks_dir / f"{nfc}.md"
        original.write_text("Q: hola\nA: hello\n", encoding="utf-8")
        renamed = self.decks_dir / f"{nfd}.md"

        def fake_input(prompt):
            original.rename(renamed)
            return "hola"

        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback("edit", nfc, "--new-answer", "hello!")
        self.assertEqual(rc, 0, err.getvalue())

        self.assertEqual([p.name for p in self.decks_dir.glob("*.md")], [f"{nfd}.md"])
        cards = parse_deck(renamed.read_text(encoding="utf-8"))
        self.assertEqual(cards[0].answer, "hello!")

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

    def test_matches_question_with_differing_unicode_normalization_form(self):
        # Same case as remove's equivalent test, but this exercises cmd_edit's
        # own separate pre-lookup (used to print the current Q/A before
        # prompting) too, not just parser.edit_card — see
        # test_matches_question_with_surrounding_whitespace above, which
        # documents that same pre-lookup needed its own fix for whitespace.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)
        self.run_flashback("add", "spanish", "-q", nfc, "-a", "coffee shop")

        rc = self.run_flashback("edit", "spanish", "-q", nfd, "--new-answer", "coffee")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(cards[0].answer, "coffee")

    def test_finds_deck_file_whose_on_disk_name_is_a_different_unicode_normalization_form(self):
        # Same gap as remove's equivalent test: `edit` also used to guess the
        # deck's path as decks_dir / f"{normalized_name}.md" instead of
        # finding whatever file `sync` actually treats as this deck, so a
        # deck whose file name happens to already be a different (but
        # visually identical) Unicode normalization form -- e.g. one hand-
        # created, or produced by a normalization-happy filesystem such as
        # macOS's -- was invisible to `edit` even though `sync`/`stats` show
        # it as a real, populated deck.
        nfd = unicodedata.normalize("NFD", "café")
        nfc = unicodedata.normalize("NFC", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / f"{nfd}.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)

        rc = self.run_flashback("edit", nfc, "-q", "hola?", "--new-answer", "hi")
        self.assertEqual(rc, 0)

        cards = parse_deck((self.decks_dir / f"{nfd}.md").read_text(encoding="utf-8"))
        self.assertEqual(cards[0].answer, "hi")
        # No second, colliding file should have been created.
        self.assertEqual([p.name for p in self.decks_dir.glob("*.md")], [f"{nfd}.md"])

    def test_edit_refuses_when_deck_name_collides_between_two_physical_files(self):
        # Same collision `cmd_sync` already refuses to touch (session 155),
        # reached through `edit` instead: `_find_deck_path` used to silently
        # pick whichever of the two colliding files sorted first, so `edit`
        # could operate on the wrong file entirely -- reporting "no card with
        # that question found" for a question that's really there, just in
        # the other colliding file, or worse, silently editing an unrelated
        # file's content under the deck's name.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text("Q: uno\nA: one\n", encoding="utf-8")
        other = self.decks_dir / f"{nfd}.md"
        other.write_text("Q: tres\nA: three\n", encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("edit", nfc, "-q", "uno", "--new-answer", "ONE")
        self.assertEqual(rc, 1)
        self.assertIn("collide", stderr.getvalue())

        # Neither file was touched.
        self.assertEqual(established.read_text(encoding="utf-8"), "Q: uno\nA: one\n")
        self.assertEqual(other.read_text(encoding="utf-8"), "Q: tres\nA: three\n")

    def test_new_question_differing_only_in_unicode_normalization_form_does_not_warn_of_reset(self):
        # --new-question is normalized (edit_card -> normalize_question) before
        # it's compared/stored, exactly like -q already is on the lookup side
        # (see test_matches_question_with_differing_unicode_normalization_form
        # above). So a --new-question that's merely a different normalization
        # form of the *same* text as the old question produces the exact same
        # stored (NFC) question, hence the exact same storage.card_id on the
        # next sync -- this is, in the README's own words, "the same card, as
        # far as scheduling is concerned," and its review history survives.
        #
        # cmd_edit's "review history will reset" note used to compare the raw,
        # un-normalized --new-question against the (already normalized) old
        # question, so it fired here even though nothing about the card's
        # identity actually changed -- a false claim contradicted by the
        # untouched review history right below it.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)
        self.run_flashback("add", "spanish", "-q", nfc, "-a", "hola")
        self.run_flashback("sync")

        with open_db(self.state_dir / "state.sqlite3") as conn:
            row = conn.execute("SELECT * FROM cards").fetchone()
            due = record_review(conn, row, Grade.GOOD, date.today())
            self.assertIsNotNone(due)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.run_flashback("edit", "spanish", "-q", nfc, "--new-question", nfd)
        self.assertEqual(rc, 0)
        self.assertNotIn("will reset", out.getvalue())

        self.run_flashback("sync")
        with open_db(self.state_dir / "state.sqlite3") as conn:
            row = conn.execute("SELECT * FROM cards").fetchone()
            # Review history (built up by the record_review call above) must
            # have survived the edit + re-sync -- proving the note would have
            # been lying had it fired.
            self.assertEqual(row["repetitions"], 1)
            self.assertIsNotNone(row["last_reviewed"])

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

    def test_interactive_preview_refuses_to_print_the_matched_cards_own_poisoned_text(self):
        # The previous test confirms an *unrelated* poisoned card doesn't
        # block editing this one — that's the validate=False lookup working
        # as intended. This is the opposite case: the poison is on the card
        # actually being edited, and interactive edit (no --new-question/
        # --new-answer) prints "current Q"/"current A" straight to the
        # terminal before prompting. Without its own _check_card_text call,
        # that print bypasses the exact protection sync/review enforce for
        # every other card, and a control character in the answer (e.g. ESC,
        # or here BEL) would reach the terminal raw.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        deck_path.write_text("Q: hello?\nA: bad\x07answer\n", encoding="utf-8")

        out = io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("should not prompt")), redirect_stdout(
            out
        ):
            rc = self.run_flashback("edit", "spanish", "-q", "hello?")
        self.assertEqual(rc, 1)
        self.assertNotIn("\x07", out.getvalue())
        self.assertNotIn("current A", out.getvalue())

        # refused before any write, so the poisoned text is untouched on disk
        self.assertEqual(deck_path.read_text(encoding="utf-8"), "Q: hello?\nA: bad\x07answer\n")

    def test_interactive_preview_shows_current_answer_before_prompting_for_new_question(self):
        # The README promises this prompt shows "the current question and
        # answer first so you can see what you're changing" -- both, before
        # either prompt. The unfixed code instead printed "current Q", then
        # immediately blocked on input() for the new question, and only
        # printed "current A" afterward -- so a user deciding what to
        # replace the question with couldn't see the card's current answer
        # for context until after they'd already answered that first
        # prompt. Confirmed against the unfixed code: the first input()
        # call happened with "current A:" not yet anywhere in stdout.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")

        seen_answer_before_first_prompt = []

        def fake_input(prompt):
            seen_answer_before_first_prompt.append("current A: hola" in out.getvalue())
            return ""

        out = io.StringIO()
        with patch("builtins.input", side_effect=fake_input), redirect_stdout(out):
            rc = self.run_flashback("edit", "spanish", "-q", "hello?")
        self.assertEqual(rc, 0)
        self.assertEqual(seen_answer_before_first_prompt, [True, True])

    def test_interactive_preview_refuses_to_guess_which_duplicate_to_edit(self):
        # cmd_edit's own preview lookup (parse_deck(..., validate=False) then
        # a plain `next(...)` for the *first* match) predates edit_card()'s
        # own duplicate-refusal fix (parser.edit_card raises when more than
        # one card shares a question) and was never updated to match: given a
        # hand-edited deck with two cards sharing a question, this picked one
        # of them arbitrarily, printed its answer as "current A" and prompted
        # for new question/answer text -- only for edit_card(), called much
        # later, to then refuse the whole edit as ambiguous. Confirmed
        # against the unfixed code: this printed "current A: hello" (one
        # arbitrary duplicate's answer, not flagged as ambiguous in any way),
        # accepted a new-answer prompt, and only then said "2 cards share
        # this same question ... refusing to guess" -- a wasted round of
        # prompts, and a misleading preview, for something that was always
        # going to be refused.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        deck_path = self.decks_dir / "spanish.md"
        deck_text = "Q: hola?\nA: hello\n\n---\n\nQ: hola?\nA: HELLO-DUPLICATE\n"
        deck_path.write_text(deck_text, encoding="utf-8")

        out = io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("should not prompt")), redirect_stdout(
            out
        ):
            rc = self.run_flashback("edit", "spanish", "-q", "hola?")
        self.assertEqual(rc, 1)
        self.assertNotIn("current A", out.getvalue())

        # refused before any write, so the deck is untouched on disk.
        self.assertEqual(deck_path.read_text(encoding="utf-8"), deck_text)

    def test_non_utf8_deck_file_fails_cleanly_instead_of_a_raw_traceback(self):
        # Same reasoning as add's equivalent test: `edit`'s preview read (and
        # its later re-read inside the lock) both used a plain Path.read_text
        # with no UnicodeDecodeError guard, unlike `sync`.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "spanish.md").write_bytes(b"Q: caf\xe9?\nA: coffee\n")

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "hola!")
        self.assertEqual(rc, 1)

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
        _patch_lock_dir(self)
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

    def test_sync_per_deck_line_uses_singular_card_for_exactly_one(self):
        # cmd_sync's per-deck summary built its own "N cards" string directly,
        # never through the _cards() helper hard's output already uses for
        # exactly this reason (session 66) — so a deck with exactly one card
        # printed "solo: 1 cards (1 new, 0 removed)".
        self.run_flashback("add", "solo", "-q", "only?", "-a", "yes")
        _, out = self.capture("sync")
        self.assertIn("solo: 1 card (1 new, 0 removed)", out)
        self.assertNotIn("1 cards", out)

    def test_sync_per_deck_line_uses_plural_card_for_two(self):
        self.run_flashback("add", "pair", "-q", "one?", "-a", "a")
        self.run_flashback("add", "pair", "-q", "two?", "-a", "b")
        _, out = self.capture("sync")
        self.assertIn("pair: 2 cards (2 new, 0 removed)", out)

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

    def test_syncing_a_different_decks_dir_sharing_this_state_dir_does_not_prune_the_first(self):
        # Regression test: nothing stops --state-dir from being shared across
        # more than one --decks-dir (a copy-pasted command with the wrong
        # --decks-dir, or a --state-dir deliberately pointed somewhere
        # central). Before decks_dir-scoped pruning, syncing decks-dir B --
        # even one with no deck-name overlap with decks-dir A at all -- made
        # every deck A had ever synced here look "missing" and deleted all of
        # it, printing "deck file no longer exists" for a file that was never
        # touched.
        other_decks_dir = Path(self._tmp.name) / "other-decks"
        other_decks_dir.mkdir()
        (other_decks_dir / "french.md").write_text("Q: bonjour?\nA: hello\n", encoding="utf-8")

        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out = self.capture(
            "--decks-dir", str(other_decks_dir), "--state-dir", str(self.state_dir), "sync"
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("no longer exists", out)

        # spanish.md was never touched, but proving the fix actually means
        # checking the database, not just the file on disk.
        self.assertTrue((self.decks_dir / "spanish.md").exists())
        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["deck"] for r in rows}, {"spanish", "french"})

    def test_syncing_a_different_decks_dir_with_a_colliding_deck_name_does_not_corrupt_the_first(self):
        # The test above shows prune_missing_decks is already scoped
        # correctly across --decks-dirs sharing one --state-dir. But an
        # ordinary deck-*name* collision between two unrelated --decks-dirs
        # (each with its own real "spanish.md", not one missing file) never
        # got the same protection: sync_deck reconciles purely by deck name,
        # so syncing decks-dir B used to silently delete decks-dir A's
        # already-established "spanish" cards -- real review history
        # included -- and splice in B's unrelated content, even though A's
        # own file on disk was never touched.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")
        with open_db(self.state_dir / "state.sqlite3") as conn:
            row = due_cards(conn, date.today())[0]
            record_review(conn, row, Grade.GOOD, date.today())  # give it real history

        other_decks_dir = Path(self._tmp.name) / "other-decks"
        other_decks_dir.mkdir()
        (other_decks_dir / "spanish.md").write_text(
            "Q: unrelated question\nA: unrelated answer\n", encoding="utf-8"
        )

        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback(
                "--decks-dir", str(other_decks_dir), "--state-dir", str(self.state_dir), "sync"
            )
        self.assertEqual(rc, 0)
        # The collision is reported, not silently absorbed -- and the losing
        # deck's summary line (which would only ever print on an actual
        # sync) must not appear, since nothing was actually synced for it.
        self.assertIn("spanish", err.getvalue())
        self.assertNotIn("spanish: 1 card", out.getvalue())

        # decks-dir A's file was never touched...
        self.assertIn("hello?", (self.decks_dir / "spanish.md").read_text(encoding="utf-8"))
        # ...and its database state -- including the review history just
        # recorded -- must survive completely intact, not be replaced by
        # decks-dir B's unrelated card.
        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = {r["question"]: r for r in due_cards(conn, date.today() + timedelta(days=1))}
        self.assertIn("hello?", rows)
        self.assertEqual(rows["hello?"]["repetitions"], 1)
        self.assertNotIn("unrelated question", rows)

    def test_decks_dir_mismatch_message_lets_the_two_colliding_directories_be_told_apart(self):
        # Same shape as test_collision_error_lets_the_two_colliding_paths_be_told_apart
        # above, but for DeckDirMismatch's own message (storage.py) instead of
        # _check_deck_collision's -- the two are separate call sites that both
        # print a pair of paths a person needs to tell apart, and the ascii()
        # fix for the NFC/NFD-collision case (a recently-fixed bug) only
        # touched the collision-within-one-directory message, not this one.
        #
        # Here the *directories themselves* (not the deck file names) are two
        # different Unicode normalization forms of the same visible text, so
        # decks_dir_key -- str(Path(...).resolve()) -- differs only in
        # normalization between the two runs. DeckDirMismatch's message used
        # plain !r (repr()) for both paths, which -- like the collision bug --
        # doesn't escape a printable combining mark, so both paths render as
        # the identical "café" text even though they're genuinely different
        # directories on disk.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        base = Path(self._tmp.name)
        decks_dir_a = base / nfc / "decks"
        decks_dir_b = base / nfd / "decks"
        decks_dir_a.mkdir(parents=True)
        decks_dir_b.mkdir(parents=True)
        (decks_dir_a / "spanish.md").write_text("Q: hello?\nA: hola\n", encoding="utf-8")
        (decks_dir_b / "spanish.md").write_text(
            "Q: unrelated question\nA: unrelated answer\n", encoding="utf-8"
        )

        rc = self.capture(
            "--decks-dir", str(decks_dir_a), "--state-dir", str(self.state_dir), "sync"
        )[0]
        self.assertEqual(rc, 0)

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = self.run_flashback(
                "--decks-dir", str(decks_dir_b), "--state-dir", str(self.state_dir), "sync"
            )
        self.assertEqual(rc, 0)
        message = err.getvalue()
        self.assertIn("was last synced from", message)
        self.assertIn(ascii(str(decks_dir_a.resolve())), message)
        self.assertIn(ascii(str(decks_dir_b.resolve())), message)

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

    def test_deck_file_with_utf8_bom_syncs_normally_instead_of_being_rejected(self):
        # Same BOM issue as add's equivalent test, hit through sync instead:
        # a deck file saved with a leading UTF-8 byte-order-mark used to fail
        # to parse entirely ("card has text before its first 'Q:' line"),
        # skipping the whole file instead of syncing its (perfectly valid)
        # card.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "greetings.md").write_bytes(b"\xef\xbb\xbfQ: hello?\nA: hola\n")

        rc, out = self.capture("sync")
        self.assertEqual(rc, 0)
        self.assertIn("greetings: 1 card (1 new, 0 removed)", out)
        self.assertNotIn("skipping", out)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["question"] for r in rows}, {"hello?"})

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

        def flaky_sync_deck(conn, deck, cards, today, decks_dir=None):
            calls.append(deck)
            if len(calls) == 2:
                raise KeyboardInterrupt("simulated interruption on second deck")
            return real_sync_deck(conn, deck, cards, today, decks_dir)

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

    def test_db_lock_contention_mid_sync_does_not_claim_the_database_never_opened(self):
        # Two flashback processes sharing one --state-dir can race on the
        # same sqlite file — a second deck's commit losing that race raises
        # sqlite3.OperationalError("database is locked") *after* open_db
        # already succeeded and the first deck's "N new, M removed" line
        # already printed. main()'s sqlite3.Error handler used to always say
        # "couldn't open the review database", which flatly contradicts the
        # success line already on the screen above it and the row that's
        # actually sitting in the database (verified below).
        self.run_flashback("add", "alpha", "-q", "one?", "-a", "uno")
        self.run_flashback("add", "beta", "-q", "two?", "-a", "dos")

        calls = []

        def flaky_sync_deck(conn, deck, cards, today, decks_dir=None):
            calls.append(deck)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real_sync_deck(conn, deck, cards, today, decks_dir)

        stderr = io.StringIO()
        with patch("flashback.cli.sync_deck", side_effect=flaky_sync_deck):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = self.run_flashback("sync")
        self.assertEqual(rc, 1)

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = {row["deck"] for row in conn.execute("SELECT deck FROM cards")}
        # Same data-safety property as the KeyboardInterrupt case above: the
        # deck synced before the failure must really be saved.
        self.assertIn("alpha", rows)
        # The error text must not claim the database was never opened — it
        # demonstrably was, for both the CREATE TABLE at open_db() and
        # alpha's own successful commit moments earlier in this same run.
        self.assertNotIn("couldn't open", stderr.getvalue())

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

    def test_hand_created_deck_file_with_control_character_name_does_not_leak_raw_bytes_in_the_skip_message(self):
        # The test above proves the bad-named deck's cards never reach the
        # database. But the *warning that explains why* is printed too --
        # "skipping {deck_file}: {name_error}" -- and name_error already
        # reprs the offending name (see _invalid_deck_name), specifically so
        # the raw control character/bidi-override never reaches the
        # terminal. deck_file itself is a Path built straight from this same
        # bad-named file, though, and gets interpolated with plain str(),
        # not repr() -- printing the identical raw ESC byte this whole check
        # exists to keep off the screen, right next to the safely-reprd copy
        # of it, defeating the point of the check for this one message.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "evil\x1b[31mred.md").write_text("Q: q1\nA: a1\n", encoding="utf-8")

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)
        self.assertNotIn("\x1b", err.getvalue())

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

    @unittest.skipIf(os.name == "nt", "symlinked deck files aren't exercised on Windows")
    def test_self_referential_symlink_deck_file_is_skipped_with_an_accurate_message(self):
        # A deck file that's a self-referential symlink (ln -s spanish.md
        # spanish.md, or a longer chain) makes Path.exists() report False --
        # it follows symlinks, and a loop can never resolve to a real file --
        # exactly the same way a deck file does that's genuinely been
        # deleted. _read_deck_text used to fold the two together, so sync
        # blamed this on the file having "been deleted (by hand, or by
        # another flashback invocation)", which is false: the file was never
        # touched, let alone deleted, and reads back identically on every
        # subsequent sync, not just the first one after it's created.
        self.run_flashback("add", "french", "-q", "bonjour?", "-a", "hello")
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        link_path = self.decks_dir / "spanish.md"
        os.symlink(link_path, link_path)

        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)
        message = err.getvalue()
        self.assertIn("symlink loop", message.lower())
        self.assertNotIn("no longer exists", message)

        # The unrelated, real deck is unaffected.
        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        self.assertEqual({r["question"] for r in rows}, {"bonjour?"})

    def test_two_deck_files_colliding_after_nfc_normalization_do_not_lose_cards(self):
        # _normalize_deck_name (added to fix the "two differently-typed
        # spellings of one deck name become two decks" gap for add/remove/
        # edit) normalizes whatever deck_file.stem sync finds on disk too.
        # But sync doesn't only see names it wrote itself — deck files are
        # documented as normal to hand-create, and two *physically different*
        # files can each be a differently-normalized spelling of the same
        # visible name (e.g. one written before this normalization existed,
        # one after, or one just pasted from somewhere with a different
        # composition). Both then normalize to the same deck_name and each
        # used to get its own sync_deck() call under that identical name.
        # sync_deck's own reconciliation ("delete any card of this deck not
        # in the file just handed to it") assumes it's the only source for
        # that deck in this run — called twice for the same name, the second
        # call saw the first call's already-inserted cards as leftovers and
        # deleted whichever of them weren't repeated in the second file, even
        # though both files' "N new, M removed" lines printed as if
        # everything were saved. sync now refuses to sync *either* colliding
        # file at all (see the sibling test below for why even "one of them,
        # picked by sort order" isn't safe), rather than silently losing data.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / f"{nfc}.md").write_text(
            "Q: nfc-only question\nA: nfc answer\n", encoding="utf-8"
        )
        (self.decks_dir / f"{nfd}.md").write_text(
            "Q: nfd-only question\nA: nfd answer\n", encoding="utf-8"
        )

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)
        self.assertIn("collide", stderr.getvalue())

        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows = due_cards(conn, date.today())
        # Neither file's cards are synced while the collision exists — there
        # is no safe way to prefer one file over the other, so the deck is
        # left alone entirely rather than gambling on whichever sorts first.
        self.assertEqual(len(rows), 0)

        # Neither file on disk was touched — sync only ever reads deck
        # files, so both must still contain exactly what they started with,
        # letting the user resolve the collision by renaming one of them.
        self.assertIn(
            "nfc-only question", (self.decks_dir / f"{nfc}.md").read_text(encoding="utf-8")
        )
        self.assertIn(
            "nfd-only question", (self.decks_dir / f"{nfd}.md").read_text(encoding="utf-8")
        )

        # Idempotent: syncing again doesn't change anything either.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc2 = self.run_flashback("sync")
        self.assertEqual(rc2, 0)
        with open_db(self.state_dir / "state.sqlite3") as conn:
            rows2 = due_cards(conn, date.today())
        self.assertEqual(rows2, [])

    def test_collision_message_lets_the_two_colliding_paths_be_told_apart(self):
        # Same gap as TestAddCommand's equivalent test, just for sync's own
        # collision message rather than _check_deck_collision's: an
        # NFC-named file and an NFD-named file collide *because* they render
        # as the identical glyphs on screen, so this message's plain
        # str(path)-joining used to print the same-looking "café.md" twice,
        # with no way to tell from the message alone which listed path is
        # which physical file -- defeating the message's own "rename the
        # files so they're distinct" instruction. Only an ascii()-escaped
        # form of each path (forcing the non-ASCII bytes to \xXX/\uXXXX
        # regardless of printability) actually differs between the two --
        # repr() does not, since NFD's combining accent is printable and
        # renders merged with the preceding letter either way.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        nfc_path = self.decks_dir / f"{nfc}.md"
        nfd_path = self.decks_dir / f"{nfd}.md"
        nfc_path.write_text("Q: nfc-only question\nA: nfc answer\n", encoding="utf-8")
        nfd_path.write_text("Q: nfd-only question\nA: nfd answer\n", encoding="utf-8")

        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)
        message = stderr.getvalue()
        self.assertIn(ascii(str(nfc_path)), message)
        self.assertIn(ascii(str(nfd_path)), message)

    def test_a_new_colliding_file_does_not_wipe_an_already_established_decks_cards(self):
        # The narrower, more serious sibling gap the fix above closes: the
        # old "first file by sort order wins" rule didn't just mean the
        # *losing* file's cards were skipped — if the deck already existed
        # in the database from a previous, ordinary sync, and the file that
        # happened to sort first this run was a brand-new, unrelated file, a
        # full sync_deck() reconciliation against that new file's contents
        # deleted every one of the deck's real, already-established cards
        # (review history included), even though neither physical file was
        # ever touched. A deck's actual identity was decided by an arbitrary
        # Unicode sort order having nothing to do with which file it was
        # really synced from before.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)
        # NFD's combining accent (U+0301) sorts before NFC's precomposed
        # "é" (U+00E9) as plain code points, so the brand-new NFD file below
        # is guaranteed to sort first — confirming the exact ordering this
        # bug depends on, not assuming it.
        self.assertEqual(sorted([f"{nfc}.md", f"{nfd}.md"])[0], f"{nfd}.md")

        self.decks_dir.mkdir(parents=True, exist_ok=True)
        established = self.decks_dir / f"{nfc}.md"
        established.write_text(
            "Q: established question one\nA: answer one\n"
            "---\n"
            "Q: established question two\nA: answer two\n",
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = self.run_flashback("sync")
        self.assertEqual(rc, 0)
        with open_db(self.state_dir / "state.sqlite3") as conn:
            established_rows = due_cards(conn, date.today())
        self.assertEqual(
            {r["question"] for r in established_rows},
            {"established question one", "established question two"},
        )

        # A brand-new, unrelated file appears that happens to normalize to
        # the same deck name and sort before the established file.
        (self.decks_dir / f"{nfd}.md").write_text(
            "Q: unrelated new question\nA: unrelated new answer\n", encoding="utf-8"
        )
        stderr2 = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr2):
            rc2 = self.run_flashback("sync")
        self.assertEqual(rc2, 0)
        self.assertIn("collide", stderr2.getvalue())

        with open_db(self.state_dir / "state.sqlite3") as conn:
            after_rows = due_cards(conn, date.today())
        # The established deck's real cards must still be there, untouched —
        # not replaced by the new file's unrelated content, and not deleted
        # outright.
        self.assertEqual(
            {r["question"] for r in after_rows},
            {"established question one", "established question two"},
        )

        # Both files on disk remain exactly as written.
        self.assertIn("established question one", established.read_text(encoding="utf-8"))


class TestReviewCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
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
        self.assertIn("card changed or no longer exists elsewhere, skipped", output)
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
        _patch_lock_dir(self)
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


class TestOutputEncodingErrors(unittest.TestCase):
    """sys.stdout's encoding comes from the environment (locale,
    PYTHONIOENCODING, a pipe/redirect into something that forces ASCII) —
    not from flashback. A minimal container image or a plain "C"/"POSIX"
    locale with no UTF-8 support are both real, reachable ways for stdout to
    end up unable to encode a perfectly ordinary non-ASCII question,
    answer, or deck name (café is the running example throughout this
    codebase's own docstrings). print() raising UnicodeEncodeError in that
    situation is a ValueError subclass, not an OSError, so main()'s
    existing OSError handler doesn't catch it — before this test's fix,
    this crashed with a raw traceback instead of the one-line message every
    other user-facing failure in this file gets."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def _run_with_ascii_stdout(self, *args):
        # A TextIOWrapper around an in-memory buffer, explicitly opened with
        # the 'ascii' codec, reproduces exactly what a restrictive
        # locale/PYTHONIOENCODING does to the real sys.stdout/sys.stderr —
        # without needing to spawn a subprocess or touch the real
        # environment just to exercise this.
        ascii_stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        ascii_stderr = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with redirect_stdout(ascii_stdout), redirect_stderr(ascii_stderr):
            rc = self.run_flashback(*args)
            # Nothing written to either stream survives past process exit
            # in the real CLI, but flushing here is what would surface a
            # *second* UnicodeEncodeError raised while trying to print this
            # very error message (e.g. from a non-ASCII character left in
            # the message itself) — TextIOWrapper buffers by default, so an
            # un-flushed write can hide that failure from this test.
            ascii_stdout.flush()
            ascii_stderr.flush()
        return rc, ascii_stderr.buffer.getvalue().decode("ascii")

    def test_non_ascii_card_content_on_ascii_stdout_exits_cleanly_on_stats(self):
        self.run_flashback("add", "café-deck", "-q", "¿Qué tal?", "-a", "Bien, gracias")
        self.run_flashback("sync")

        rc, stderr = self._run_with_ascii_stdout("stats")

        self.assertEqual(rc, 1)
        self.assertIn("couldn't print", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_non_ascii_card_content_on_ascii_stdout_exits_cleanly_on_due(self):
        self.run_flashback("add", "café-deck", "-q", "¿Qué tal?", "-a", "Bien, gracias")
        self.run_flashback("sync")

        rc, stderr = self._run_with_ascii_stdout("due")

        self.assertEqual(rc, 1)
        self.assertIn("couldn't print", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_non_ascii_deck_name_on_ascii_stdout_exits_cleanly_on_sync(self):
        self.decks_dir.mkdir(parents=True)
        (self.decks_dir / "café.md").write_text("Q: q1?\nA: a1\n", encoding="utf-8")

        rc, stderr = self._run_with_ascii_stdout("sync")

        self.assertEqual(rc, 1)
        self.assertIn("couldn't print", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_removing_a_purely_ascii_card_succeeds_under_ascii_stdout(self):
        """Every test above deliberately puts non-ASCII text *in the card/deck
        content* and confirms that fails cleanly rather than crashing raw. But
        flashback's own hardcoded success message for `remove` — "removed from
        ... (run `flashback sync` to pick it up -- this card's review history
        will be deleted on next sync)" — used to contain a literal Unicode em
        dash of its own, entirely independent of anything the user typed. That
        means removing a card with a completely ordinary, all-ASCII question
        and deck name still crashed under a restrictive/ASCII stdout, which
        contradicts the exact guarantee this whole test class exists to check
        (and contradicts the "not a problem with the content itself" wording
        of the UnicodeEncodeError handler's own message, printed when this
        fails)."""
        self.run_flashback("add", "plain-deck", "-q", "plain question?", "-a", "plain answer")

        rc, stderr = self._run_with_ascii_stdout("remove", "plain-deck", "-q", "plain question?")

        self.assertEqual(rc, 0, f"remove of purely-ASCII content should succeed under ASCII stdout; stderr={stderr!r}")

    def test_hard_with_no_hard_cards_succeeds_under_ascii_stdout(self):
        """Same shape of bug as the `remove` case above, for `hard`'s "nothing
        looks hard yet" message, which also used to contain a literal em dash
        with no card content involved at all."""
        self.run_flashback("add", "plain-deck", "-q", "plain question?", "-a", "plain answer")
        self.run_flashback("sync")

        rc, stderr = self._run_with_ascii_stdout("hard")

        self.assertEqual(rc, 0, f"'hard' with only ASCII content should succeed under ASCII stdout; stderr={stderr!r}")


class TestUserFacingMessagesAreAscii(unittest.TestCase):
    """Every command in this file has been carefully taught to fail cleanly
    (a one-line "error: ..." message, never a raw traceback) when a
    restrictive stdout encoding (a minimal container, a plain "C"/"POSIX"
    locale) can't print some non-ASCII card or deck content -- see
    TestOutputEncodingErrors above, and the UnicodeEncodeError handler in
    cli.main, whose own comment says "this message must itself be pure
    ASCII" for exactly this reason.

    But several of flashback's own hardcoded, always-printed messages (not
    user content) were written with a literal Unicode em dash instead of an
    ASCII hyphen -- e.g. remove's success line and hard's "nothing looks
    hard yet" message (see TestOutputEncodingErrors' new ascii-stdout
    regression tests just above). Under the exact same restrictive stdout
    this codebase otherwise defends against, those messages crash on their
    own, with entirely ASCII card/deck content in play -- contradicting both
    the point of this whole defense and the crash message's own claim that
    it's "not a problem with the content itself".

    Modeled on tests/test_python_compat.py's existing ast-based source scan
    (same project, same technique, different property): walks every string
    literal in cli.py/parser.py that isn't a docstring (comments and
    docstrings are never printed, so non-ASCII prose there is harmless) and
    fails if any contains a character outside ASCII. parser.LINE_SEPARATOR_CHARS,
    parser.ZERO_WIDTH_NO_BREAK_SPACE, and parser.ZERO_WIDTH_SPACE are the
    deliberate exceptions: all four characters (U+2028/U+2029/U+FEFF/U+200B)
    are data being matched against, not text ever printed to a terminal --
    every message that reports one names it by its ASCII "U+FEFF"/"U+2028"
    form instead.
    """

    FLASHBACK_DIR = Path(__file__).resolve().parent.parent / "flashback"

    def _docstring_ids(self, tree):
        ids = set()
        candidates = [tree] + [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        for node in candidates:
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
        return ids

    def test_no_non_ascii_characters_in_printed_message_literals(self):
        comparison_data_chars = {"\u2028", "\u2029", "\ufeff", "\u200b"}
        offenders = []
        for filename in ("cli.py", "parser.py"):
            path = self.FLASHBACK_DIR / filename
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
            doc_ids = self._docstring_ids(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if id(node) in doc_ids:
                    continue
                if node.value in comparison_data_chars:
                    # parser.LINE_SEPARATOR_CHARS / ZERO_WIDTH_NO_BREAK_SPACE:
                    # data compared against deck/card text, never printed on
                    # its own.
                    continue
                if any(ord(c) > 127 for c in node.value):
                    offenders.append((filename, node.lineno, node.value))
        self.assertEqual(
            offenders,
            [],
            f"non-ASCII character(s) found in a user-facing message literal: {offenders}",
        )


class TestNextDueReporting(unittest.TestCase):
    """`due`/`review`/`stats` should say when the next card actually comes back.

    Every card's `due_date` has always been in the database; before this, a
    person who caught up left with "nothing due. go outside." and no idea
    whether that meant tomorrow or next month.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
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

    def test_stats_deck_filter_shows_only_that_deck(self):
        # `due`/`review`/`hard` all take `--deck`; `stats` should too, the
        # same way its own README section already claims it does.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "geology", "-q", "batholith?", "-a", "big rock")
        self.run_flashback("sync")

        rc, out = self.capture("stats", "--deck", "geology")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("geology") for line in lines))
        self.assertFalse(any(line.startswith("spanish") for line in lines))


class TestHardCommand(unittest.TestCase):
    """`hard` should tell a learner which cards they're actually bad at.

    The scheduler has computed this since day one — easiness falls on every
    `again`/`hard` grade, and the whole pitch of spaced repetition is that the
    tool knows what you're struggling with. It just never had a way to say it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
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

    def test_hard_does_not_claim_no_decks_yet_for_a_deck_synced_with_zero_cards(self):
        # A deck synced with zero cards has a row in `decks` but none in
        # `cards`. `stats`/`known_decks`/`prune_missing_decks` already treat
        # that as a real, synced deck (session 97) — `hard` must too.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "empty.md").write_text("", encoding="utf-8")
        rc, sync_out = self.capture("sync")
        self.assertEqual(rc, 0)
        self.assertIn("empty: 0 cards (0 new, 0 removed)", sync_out)

        rc, stats_out = self.capture("stats")
        self.assertEqual(rc, 0)
        self.assertTrue(any(line.startswith("empty") for line in stats_out.splitlines()))

        rc, out = self.capture("hard")
        self.assertEqual(rc, 0)
        self.assertNotIn("no decks yet", out)
        self.assertIn("nothing looks hard yet", out)

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

    def test_hard_rejects_a_negative_limit_instead_of_silently_showing_everything(self):
        # `_print_hard_group` only special-cases `limit > 0` vs. everything
        # else, so a negative value (a typo, or a guess that negative means
        # "unlimited") would otherwise fall through to the same "show every
        # row" behavior as the documented `0`, with no indication anything
        # unusual happened — the opposite of what someone asking to *cap* the
        # output would expect from a negative number.
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                self.run_flashback("hard", "--limit", "-5")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--limit", stderr.getvalue())

    def test_hard_rejects_a_non_numeric_limit_with_a_clean_message(self):
        # `--limit`'s `type=` callable (`_non_negative_int`) only wraps the
        # negative-number case in its own `argparse.ArgumentTypeError`. A
        # value that isn't a valid integer at all (a typo, e.g. `--limit al`
        # meant to be `--limit all`) instead lets `int(value)`'s bare
        # `ValueError` escape uncaught. argparse's own fallback handling for
        # that turns it into "invalid %s value: %r" % (type_func.__name__, ...)
        # — since the function is named with a leading underscore as an
        # internal implementation detail, that leaks "_non_negative_int"
        # straight into a user-facing error message instead of a clean
        # description of what was actually expected.
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                self.run_flashback("hard", "--limit", "abc")
        self.assertEqual(ctx.exception.code, 2)
        self.assertNotIn("_non_negative_int", stderr.getvalue())
        self.assertIn("--limit", stderr.getvalue())

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


class TestDeckFilterValidation(unittest.TestCase):
    """`due`/`review`/`hard`/`stats` should reject a `--deck` that matches nothing.

    Each of the four filters by deck at the SQL level, so a typo has always
    silently matched zero rows and printed exactly what a caught-up deck
    prints ("nothing due") — no way to tell "you're done" from "you
    mistyped." Only checked once the database actually knows about at least
    one deck; an empty database keeps its existing "no decks yet" message,
    which is the more honest thing to say there.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def run_flashback(self, *args):
        return main(
            ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
        )

    def capture(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.run_flashback(*args)
        return rc, out.getvalue(), err.getvalue()

    def test_due_rejects_a_deck_name_matching_nothing(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out, err = self.capture("due", "--deck", "italian")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no such deck: 'italian'", err)
        self.assertIn("spanish", err)

    def test_review_rejects_a_deck_name_matching_nothing(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out, err = self.capture("review", "--deck", "italian")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no such deck: 'italian'", err)

    def test_hard_rejects_a_deck_name_matching_nothing(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out, err = self.capture("hard", "--deck", "italian")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no such deck: 'italian'", err)

    def test_stats_rejects_a_deck_name_matching_nothing(self):
        # The README documents `stats --deck` in the same breath as
        # `due`/`review`'s next-due-date filtering, but `stats`'s own
        # subparser never actually grew a `--deck` argument to match.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out, err = self.capture("stats", "--deck", "italian")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("no such deck: 'italian'", err)

    def test_a_real_deck_name_is_unaffected(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("sync")

        rc, out, err = self.capture("due", "--deck", "spanish")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_an_empty_database_keeps_its_own_no_decks_message_instead(self):
        # Nothing has ever synced, so there's no honest "known decks" list to
        # offer — the existing "no decks yet" message is the more truthful
        # answer than "no such deck", not a case this check should touch.
        rc, out, err = self.capture("hard", "--deck", "italian")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("run `flashback sync`", out)

        rc, out, err = self.capture("due", "--deck", "italian")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("nothing due", out)

    def test_deck_filter_with_unpaired_surrogate_is_rejected_cleanly(self):
        # A lone surrogate (U+D800-U+DFFF) reaches `--deck` the same way it
        # reaches add/remove/edit's own deck-name argument: sys.argv decodes
        # non-UTF-8 command-line bytes with the 'surrogateescape' handler
        # (PEP 383), so a stray byte from a mismatched locale or copy-pasted
        # mojibake lands here as an ordinary Python str with no error yet.
        # _invalid_deck_name already rejects this for add/remove/edit's own
        # deck argument, but due/review/stats/hard's `--deck` *filter* never
        # ran it -- and unlike a merely-nonexistent name (caught safely by
        # _check_deck_filter's `!r`-escaped "no such deck" message), a
        # surrogate can't even be encoded to UTF-8 at all. On a database with
        # no decks yet, `_check_deck_filter` used to return None
        # unconditionally (matching the "empty database" case tested above),
        # so this sailed straight through to due_cards/deck_stats/hard_cards'
        # raw SQL, whose parameter binding crashed with UnicodeEncodeError.
        # That crash was then caught by main()'s UnicodeEncodeError handler,
        # which blames "the current terminal or output" and suggests a UTF-8
        # locale -- completely wrong advice, since nothing was ever printed;
        # the failure was in binding a SQL parameter, and no locale setting
        # makes an unpaired surrogate valid.
        for command in ("due", "review", "stats", "hard"):
            rc, out, err = self.capture(command, "--deck", "\udc80")
            self.assertEqual(rc, 1, f"{command} --deck <surrogate>: rc={rc} out={out!r} err={err!r}")
            self.assertIn("invalid deck name", err)
            self.assertNotIn("terminal", err)

    def test_a_deck_synced_with_zero_cards_is_not_treated_as_nonexistent(self):
        # Regression test: a deck file that parses fine but currently has no
        # cards in it (e.g. every card was hand-removed, or it's a fresh file
        # someone's about to fill in) used to leave no trace in the `cards`
        # table at all, so `--deck <that deck>` was indistinguishable from a
        # typo — `due`/`stats`/`hard` all rejected it with "no such deck"
        # right after `sync` had just reported it by name.
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        (self.decks_dir / "empty.md").write_text("", encoding="utf-8")
        self.run_flashback("add", "full", "-q", "hello?", "-a", "hola")
        rc, sync_out, _ = self.capture("sync")
        self.assertEqual(rc, 0)
        self.assertIn("empty: 0 cards (0 new, 0 removed)", sync_out)

        rc, out, err = self.capture("due", "--deck", "empty")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("nothing due", out)

        rc, out, err = self.capture("hard", "--deck", "empty")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("nothing looks hard yet", out)

        rc, out, err = self.capture("stats", "--deck", "empty")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertIn("empty", out)
        self.assertIn("0", out)

        # Unfiltered `stats` should list the empty deck too, not silently
        # drop it as if it had never been synced.
        rc, out, err = self.capture("stats")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("empty") for line in lines))
        self.assertTrue(any(line.startswith("full") for line in lines))

    def test_mistyped_deck_flag_is_rejected_not_silently_ignored(self):
        # `due`/`review`/`stats`/`hard` each carry both a meaningful `--deck`
        # (the filter tested throughout this class) and an inert, inherited
        # `--decks-dir` (see _add_shared_dir_args -- accepted on every
        # subcommand for a consistent flag surface, but never actually read
        # by any of these four, which only touch --state-dir). "--decks" is
        # a very plausible typo of "--deck" for a command whose whole subject
        # is decks -- and, with argparse's default abbreviation matching, it
        # was also a valid *unique* abbreviation of "--decks-dir" ("--deck"
        # itself is too short to be a prefix of "--decks", so argparse's
        # ambiguity check never even saw a conflict). That meant `stats
        # --decks spanish` used to silently bind "spanish" to the unused
        # --decks-dir instead of the real --deck filter, with no error at
        # all, and print every synced deck instead of just the one named --
        # exactly the "typo silently does something else, looks like it
        # worked" failure shape `_check_deck_filter` exists to prevent for a
        # mistyped *value*, just reached through a mistyped *flag name*
        # instead. build_parser() now disables abbreviation on every parser
        # (allow_abbrev=False) so a flag-name typo is a clean, loud error
        # instead of a silent, wrong match.
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "italian", "-q", "ciao?", "-a", "hello")
        self.run_flashback("sync")

        for command in ("due", "review", "stats", "hard"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_flashback(command, "--decks", "spanish")
            self.assertEqual(ctx.exception.code, 2, f"{command} --decks spanish")
            self.assertIn("unrecognized arguments", err.getvalue())
            # Above all, this must not be silently treated as an accepted,
            # unfiltered run that lists both decks -- the exact silent
            # failure this test guards against.
            self.assertNotIn("italian", out.getvalue())


class TestGlobalDirOptionsPlacement(unittest.TestCase):
    # `--decks-dir`/`--state-dir` used to be defined only on the top-level
    # parser, so every other option in this CLI (`-q`, `-a`, `--deck`,
    # `--limit`) could be typed after the subcommand but these two could
    # not — argparse rejected them there as "unrecognized arguments", and
    # `add --help` etc. never even mentioned them. This class covers both
    # placements, plus the specific regression a naive fix (re-adding the
    # options to each subparser with their own ordinary defaults) would
    # introduce: argparse.SubParsersAction copies every attribute of the
    # post-subcommand namespace onto the outer one, including untouched
    # defaults, so a value set before the subcommand would be silently
    # reset back to the default the moment any subcommand ran.

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"

    def test_flags_after_subcommand_are_accepted(self):
        rc = main(
            [
                "add",
                "spanish",
                "-q",
                "hello?",
                "-a",
                "hola",
                "--decks-dir",
                str(self.decks_dir),
                "--state-dir",
                str(self.state_dir),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.decks_dir / "spanish.md").exists())

    def test_flags_before_subcommand_still_work(self):
        rc = main(
            [
                "--decks-dir",
                str(self.decks_dir),
                "--state-dir",
                str(self.state_dir),
                "add",
                "spanish",
                "-q",
                "hello?",
                "-a",
                "hola",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.decks_dir / "spanish.md").exists())

    def test_flag_before_subcommand_is_not_silently_reset_to_default(self):
        # The naive-fix regression guard: giving --decks-dir only before
        # "add" must not get clobbered by "add"'s own subparser default.
        from flashback.cli import build_parser

        parser = build_parser()
        ns = parser.parse_args(
            ["--decks-dir", str(self.decks_dir), "add", "french", "-q", "hi", "-a", "salut"]
        )
        self.assertEqual(ns.decks_dir, str(self.decks_dir))

    def test_mixed_placement_both_take_effect(self):
        rc = main(
            [
                "--decks-dir",
                str(self.decks_dir),
                "add",
                "spanish",
                "-q",
                "hello?",
                "-a",
                "hola",
                "--state-dir",
                str(self.state_dir),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertTrue((self.decks_dir / "spanish.md").exists())
        self.assertTrue(self.state_dir.exists())

    def test_add_help_documents_shared_dir_options(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                main(["add", "--help"])
        self.assertIn("--decks-dir", buf.getvalue())
        self.assertIn("--state-dir", buf.getvalue())


class TestDirArgSurrogateValidation(unittest.TestCase):
    """--decks-dir/--state-dir reach flashback as ordinary sys.argv strings,
    decoded with the same 'surrogateescape' handler (PEP 383) that lets a
    stray non-UTF-8 command-line byte become an unpaired Unicode surrogate in
    a Python str -- the same reachable, real-world cause _invalid_deck_name
    and _check_card_text already guard against for deck names and card text.

    Before this fix, neither --decks-dir nor --state-dir was checked for
    this at all: the surrogate sailed through argument parsing and into real
    filesystem work (mkdir, the deck file write `add` performs), only
    surfacing once some later print() of that same path hit
    UnicodeEncodeError on stdout -- which main()'s existing handler then
    misdiagnosed as "the current terminal or output" and suggested a UTF-8
    locale, advice that cannot fix a surrogate baked into the path itself.
    Worse, for `add`, that crash happened *after* the card had already been
    written to disk, so the command exited 1 with a misleading error while
    having actually succeeded.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
        # A lone surrogate, exactly as sys.argv would decode a stray
        # non-UTF-8 byte via 'surrogateescape'.
        self.bad_component = b"decks-\xff-bad".decode("utf-8", "surrogateescape")

    def test_decks_dir_with_unpaired_surrogate_is_rejected_before_writing_the_card(self):
        bad_decks_dir = os.path.join(self._tmp.name, self.bad_component)
        state_dir = os.path.join(self._tmp.name, ".flashback")

        rc = main(
            ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
        )

        self.assertEqual(rc, 1)
        # The whole point: this must fail before any file work happens, not
        # partway through -- with the card already saved and a misleading
        # "terminal encoding" message printed on top of that success.
        self.assertFalse(os.path.exists(bad_decks_dir))

    def test_state_dir_with_unpaired_surrogate_is_rejected_before_creating_it(self):
        decks_dir = Path(self._tmp.name) / "decks"
        decks_dir.mkdir()
        (decks_dir / "spanish.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        bad_state_dir = os.path.join(self._tmp.name, self.bad_component)

        rc = main(["--decks-dir", str(decks_dir), "--state-dir", bad_state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_state_dir))

    def test_error_message_does_not_misdiagnose_this_as_a_terminal_encoding_problem(self):
        # Plain redirect_stderr(io.StringIO()) wouldn't reproduce the actual
        # pre-fix failure here: StringIO never encodes at all, so printing a
        # surrogate through it never raises -- which is exactly why this
        # bug's symptom (a crash from main()'s *own* print of `deck_path`)
        # only shows up against a real encoding-enforcing text stream, the
        # same reason TestOutputEncodingErrors above drives its checks
        # through an ascii-encoded io.TextIOWrapper rather than plain
        # capture. Reusing that same technique here (with 'utf-8', not
        # 'ascii') proves the point even more strongly: this crashes even
        # against a perfectly UTF-8-capable stream, because an unpaired
        # surrogate can never be encoded to UTF-8 at all, by any stream.
        bad_decks_dir = os.path.join(self._tmp.name, self.bad_component)
        state_dir = os.path.join(self._tmp.name, ".flashback")
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")

        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = main(
                ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
            )
            stdout.flush()
            stderr.flush()

        self.assertEqual(rc, 1)
        message = stderr.buffer.getvalue().decode("utf-8")
        self.assertIn("surrogate", message)
        # The misdiagnosis this replaces: no locale setting fixes a
        # surrogate that's actually baked into the path itself.
        self.assertNotIn("locale", message)
        self.assertNotIn("terminal", message)


class TestDirArgControlCharAndBidiValidation(unittest.TestCase):
    """--decks-dir/--state-dir are printed raw (not repr()'d) in a comparable
    number of places add/remove/edit/sync already print a deck name --
    cmd_add's "added to {deck_path} ..." confirmation, cmd_sync's "no such
    directory: {decks_dir}", _read_deck_text's ParseError message, and more --
    so an embedded control character (e.g. an ESC clear-screen sequence) or a
    Unicode bidirectional-formatting override (the "Trojan Source" family) can
    hide or reorder what's shown on screen exactly the way _invalid_deck_name's
    own docstring describes for a deck name, just through a sibling argument
    that (before this fix) was only ever checked for an unpaired surrogate.

    Also covers a Unicode "Tags" block character (U+E0000-U+E007F, see
    parser._is_unicode_tag_char): _invalid_deck_name already rejects one in a
    deck name because it has no visible glyph in any font, so two names that
    print identically can secretly differ underneath -- _invalid_dir_arg was
    never given the matching check, so the identical "looks the same but
    isn't" gap existed for --decks-dir/--state-dir too, until now.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)

    def test_decks_dir_with_control_character_is_rejected_before_writing_the_card(self):
        bad_decks_dir = os.path.join(self._tmp.name, "de\x1bcks")
        state_dir = os.path.join(self._tmp.name, ".flashback")

        rc = main(
            ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
        )

        self.assertEqual(rc, 1)
        # The whole point: fail before any file work happens, not with the
        # card already saved and the raw ESC byte echoed in a success
        # message.
        self.assertFalse(os.path.exists(bad_decks_dir))

    def test_state_dir_with_bidi_override_is_rejected_before_creating_it(self):
        decks_dir = Path(self._tmp.name) / "decks"
        decks_dir.mkdir()
        (decks_dir / "spanish.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        bad_state_dir = os.path.join(self._tmp.name, "evil‮txt.exe")

        rc = main(["--decks-dir", str(decks_dir), "--state-dir", bad_state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_state_dir))

    def test_decks_dir_error_message_names_the_bidi_character(self):
        bad_decks_dir = os.path.join(self._tmp.name, "evil‮txt.exe")
        state_dir = os.path.join(self._tmp.name, ".flashback")
        buf = io.StringIO()

        with redirect_stderr(buf):
            rc = main(["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertIn("bidirectional-formatting", buf.getvalue())

    def test_decks_dir_with_unicode_tag_character_is_rejected_before_writing_the_card(self):
        # A Unicode "Tags" block character (U+E0000-U+E007F) has no visible
        # glyph in any font, so it doesn't corrupt display the way a control
        # character or bidi override does -- but _invalid_deck_name already
        # rejects it in a deck *name* for exactly this reason: two names that
        # print identically can secretly be different strings underneath.
        # _invalid_dir_arg was never given the same check, so two
        # --decks-dir values that look byte-for-byte identical on screen
        # (one plain, one with an invisible tag character spliced in) were
        # silently accepted as two different real directories -- reproduced
        # directly against the unfixed code: `main()` returned 0 for both,
        # each creating its own directory/state, with no error hinting the
        # second one wasn't the same path the first one appeared to be.
        bad_decks_dir = os.path.join(self._tmp.name, f"de{chr(0xE0041)}cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")

        rc = main(
            ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
        )

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_decks_dir))

    def test_state_dir_with_unicode_tag_character_is_rejected_before_creating_it(self):
        decks_dir = Path(self._tmp.name) / "decks"
        decks_dir.mkdir()
        (decks_dir / "spanish.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        bad_state_dir = os.path.join(self._tmp.name, f".flashback{chr(0xE0041)}")

        rc = main(["--decks-dir", str(decks_dir), "--state-dir", bad_state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_state_dir))

    def test_decks_dir_error_message_names_the_tag_character(self):
        bad_decks_dir = os.path.join(self._tmp.name, f"de{chr(0xE0041)}cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")
        buf = io.StringIO()

        with redirect_stderr(buf):
            rc = main(["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertIn("Unicode tag character", buf.getvalue())

    def test_decks_dir_with_byte_order_mark_is_rejected_before_writing_the_card(self):
        # U+FEFF (the byte-order mark) is invisible everywhere outside
        # position zero of a file, so it doesn't corrupt display the way a
        # control character or bidi override does -- but _invalid_deck_name
        # already rejects it in a deck name for exactly this reason: two
        # names (or here, two --decks-dir values) that print identically can
        # secretly be different strings underneath. _invalid_dir_arg was
        # never given the same check.
        bad_decks_dir = os.path.join(self._tmp.name, "de﻿cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")

        rc = main(
            ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
        )

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_decks_dir))

    def test_state_dir_with_byte_order_mark_is_rejected_before_creating_it(self):
        decks_dir = Path(self._tmp.name) / "decks"
        decks_dir.mkdir()
        (decks_dir / "spanish.md").write_text("Q: hola?\nA: hello\n", encoding="utf-8")
        bad_state_dir = os.path.join(self._tmp.name, ".flashback﻿")

        rc = main(["--decks-dir", str(decks_dir), "--state-dir", bad_state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_state_dir))

    def test_decks_dir_error_message_names_the_byte_order_mark(self):
        bad_decks_dir = os.path.join(self._tmp.name, "de﻿cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")
        buf = io.StringIO()

        with redirect_stderr(buf):
            rc = main(["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertIn("byte-order-mark", buf.getvalue())

    def test_decks_dir_with_zero_width_space_is_rejected_before_writing_the_card(self):
        # U+200B (zero-width space) is invisible everywhere, the same
        # "looks the same, isn't" risk already blocked above for the
        # byte-order mark -- but _invalid_dir_arg was never given the same
        # check for it either.
        bad_decks_dir = os.path.join(self._tmp.name, f"de{chr(0x200B)}cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")

        rc = main(
            ["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "add", "spanish", "-q", "hi", "-a", "hola"]
        )

        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(bad_decks_dir))

    def test_decks_dir_error_message_names_the_zero_width_space(self):
        bad_decks_dir = os.path.join(self._tmp.name, f"de{chr(0x200B)}cks")
        state_dir = os.path.join(self._tmp.name, ".flashback")
        buf = io.StringIO()

        with redirect_stderr(buf):
            rc = main(["--decks-dir", bad_decks_dir, "--state-dir", state_dir, "sync"])

        self.assertEqual(rc, 1)
        self.assertIn("zero-width space", buf.getvalue())


@unittest.skipUnless(hasattr(os, "mkfifo"), "mkfifo is POSIX-only, like the rest of this project's locking")
class TestFifoDeckFileDoesNotHang(unittest.TestCase):
    """`sync`/`add`/`remove`/`edit` all read a deck file with a plain
    Path.read_text() (via _read_deck_text, or -- before this fix -- directly
    in cmd_sync). A FIFO (named pipe) sitting at a *.md path -- created by
    hand, by another program, or left behind by some unrelated tool; a
    --decks-dir is documented as normal to hand-populate -- opens for read
    instantly but doesn't return any data, or hit EOF, until some other
    process opens its write end: forever, if nothing ever does. `open()`
    blocking like that turns one stray file into a hang of the *entire*
    invocation, not a per-deck skip the way an unreadable/non-UTF8 file
    already gets -- for `sync` specifically, every other deck in the same
    run, including ones already synced and reported before reaching this
    one, is stuck behind it too, with no error and no timeout.

    Each test below runs the real CLI call in a background thread with a
    bounded join() instead of calling it directly, so a regression (the
    call actually hanging) fails this test quickly instead of freezing the
    whole suite -- the thread itself is left running in that case, but as a
    daemon thread blocked in a single open() syscall with nothing left to
    do, it doesn't do anything else, and dies with the process either way.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _patch_lock_dir(self)
        self.decks_dir = Path(self._tmp.name) / "decks"
        self.state_dir = Path(self._tmp.name) / ".flashback"
        self.decks_dir.mkdir()
        os.mkfifo(self.decks_dir / "spanish.md")

    def _run_with_timeout(self, *args, timeout=5):
        """Run `main(args)` in a background thread; return its rc, or None
        if it didn't finish within `timeout` seconds (i.e. it hung)."""
        result = {}
        buf = io.StringIO()

        def target():
            with redirect_stdout(buf), redirect_stderr(buf):
                result["rc"] = main(
                    ["--decks-dir", str(self.decks_dir), "--state-dir", str(self.state_dir), *args]
                )

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        self.assertFalse(thread.is_alive(), f"flashback {' '.join(args)} hung reading a FIFO deck file")
        return result.get("rc"), buf.getvalue()

    def test_sync_does_not_hang_on_a_fifo_deck_file(self):
        rc, out = self._run_with_timeout("sync")
        self.assertEqual(rc, 0)
        self.assertIn("skipping", out)
        self.assertIn("not a regular file", out)

    def test_add_does_not_hang_reading_an_existing_fifo_deck_file(self):
        rc, out = self._run_with_timeout("add", "spanish", "-q", "hola?", "-a", "hello")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", out)

    def test_remove_does_not_hang_reading_a_fifo_deck_file(self):
        rc, out = self._run_with_timeout("remove", "spanish", "-q", "hola?")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", out)

    def test_edit_does_not_hang_reading_a_fifo_deck_file(self):
        rc, out = self._run_with_timeout("edit", "spanish", "-q", "hola?", "--new-answer", "hi")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", out)


if __name__ == "__main__":
    unittest.main()
