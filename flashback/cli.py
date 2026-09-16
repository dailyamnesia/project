"""Command-line interface for flashback."""

import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from . import __version__
from .parser import (
    BIDI_FORMATTING_CLASSES,
    LINE_SEPARATOR_CHARS,
    ZERO_WIDTH_NO_BREAK_SPACE,
    ZERO_WIDTH_SPACE,
    ParseError,
    _check_card_text,
    _is_unicode_tag_char,
    append_card,
    edit_card,
    normalize_question,
    parse_deck,
    remove_card,
)
from .scheduler import Grade
from .storage import (
    DeckDirMismatch,
    deck_stats,
    due_cards,
    ensure_state_dir,
    hard_cards,
    known_decks,
    next_due_date,
    open_db,
    prune_missing_decks,
    record_review,
    sync_deck,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

GRADE_KEYS = {
    "1": Grade.AGAIN,
    "2": Grade.HARD,
    "3": Grade.GOOD,
    "4": Grade.EASY,
    "again": Grade.AGAIN,
    "hard": Grade.HARD,
    "good": Grade.GOOD,
    "easy": Grade.EASY,
}


def _db_path(args) -> Path:
    return Path(args.state_dir) / "state.sqlite3"


def _normalize_deck_name(name: str) -> str:
    """Strip surrounding whitespace and NFC-normalize a deck name, the same way
    question/answer text is treated before it's used as a card's identity, and
    for the same reason: a deck name is an identity, not just display text.
    Unicode allows some characters two equally valid encodings (e.g. "é" as one
    precomposed codepoint, or as "e" plus a combining acute accent) that render
    identically but compare unequal as plain Python strings — and a trailing
    space is invisible in terminal output entirely (`stats`'s deck-name column
    is padded to a fixed width, so "spanish" and "spanish " render as identical
    text), making it an even easier typo to make unknowingly than a Unicode
    encoding mismatch.

    Without this, two "differently-typed" spellings of the same deck name — both
    reading, on screen, as exactly the same deck — silently become two different
    deck files on disk (`decks_dir / f"{name}.md"` differs byte-for-byte even
    though the two names look identical) and two unrelated rows in the `decks`
    table, so cards added under one spelling are invisible to `remove`/`edit`/
    `--deck` lookups made under the other, and `sync`/`stats` list what looks
    like one deck twice. The exact "looks the same but silently isn't" failure
    shape `normalize_question` already exists to close for questions, just for
    deck names instead.

    Applied to every deck name before it's used to build a path, locked, looked
    up in the database, or compared against one: `add`/`remove`/`edit`'s `deck`
    argument, `sync`'s deck name recovered from a file's stem, and `due`/
    `review`/`stats`/`hard`'s `--deck` filter.
    """
    return unicodedata.normalize("NFC", name.strip())


def _invalid_deck_name(name: str) -> Optional[str]:
    """Return an error message if `name` can't be used as a deck file's stem, else None.

    A deck name with a path separator either escapes decks-dir silently (`../x`) or
    lands in a subdirectory `sync`'s non-recursive glob never looks at (`x/y`) — both
    look like they worked (a success message, a file on disk) but the card never
    becomes reachable through the tool again.

    A bare `.` or `..` (no slash at all) is rejected too, but for a different reason
    and with its own message: `decks_dir / f"{name}.md"` turns either into an
    ordinary, harmless filename (`..md`/`...md`), not an actual directory reference,
    so there's no path-escape risk here the way there is for a name containing a real
    separator. The problem is purely that a deck named exactly "." or ".." would sit
    in every deck listing (`sync`, `due`, `stats`, `review`) looking like a shell's
    "current/parent directory", not a deck — confusing on sight, and an easy typo to
    make unknowingly if a script builds `--deck`/deck-name arguments from a path.
    Blaming this on "a path separator", as one shared message with the check above
    used to, is simply false for these two: neither one contains a "/" or "\\" at
    all, so that explanation doesn't match what the user actually typed.

    An empty name has the same "looks like it worked" shape for a different reason:
    it writes to a file literally named `.md`, and `Path(...).stem` — used by `sync`
    to recover the deck name from the file it globbed — doesn't split a leading dot
    off as a suffix (the same rule that keeps `.gitignore`'s stem as `.gitignore`,
    not empty), so the deck reappears everywhere else (`sync`, `due`, `stats`) named
    `.md` instead of the empty string it was added under. Rejecting it here means
    `add`/`remove`/`edit` never disagree with `sync` about what a deck is named.

    A control character or Unicode bidirectional-formatting character has the
    same risk here as in card text (see `_check_card_text` in `parser.py`):
    every command that lists a deck (`add`'s confirmation, `sync`, `due`,
    `stats`, `review`) prints its name straight to the terminal, so an
    embedded ESC or an RLO/LRO override can hide or reorder what's shown just
    as easily through a deck name as through a question or answer. Unlike
    card text, tab and newline aren't given an exception here — a deck name
    is a single-line identifier, and either one already breaks `stats`'s
    tabular layout.

    Unicode's own LINE SEPARATOR/PARAGRAPH SEPARATOR (U+2028/U+2029) have
    that same "breaks stats's tabular layout" effect as tab/newline — most
    terminals render them as a line break — but aren't in the Cc category
    the control-character check above catches (they're Zl/Zp) and don't
    reorder anything either, so they need their own check, same as
    `_check_card_text` needs one for question/answer text.

    An unpaired Unicode surrogate (U+D800-U+DFFF, category "Cs") gets the
    same rejection as it does in `_check_card_text`, and for the identical
    reason: it isn't a real character, it can never be encoded to UTF-8, so
    `decks_dir / f"{name}.md"`'s content would never actually be writable
    (`_atomic_write_text`'s `encode="utf-8"` raises `UnicodeEncodeError`
    unconditionally on one). A deck name built from non-UTF-8 bytes reaches
    here silently, since `sys.argv` decodes anything that isn't valid UTF-8
    with the `surrogateescape` handler instead of raising -- so without this
    check, that crash surfaces from deep inside `_atomic_write_text` and gets
    caught by `main`'s `UnicodeEncodeError` handler, which (wrongly, for this
    cause) blames the terminal's output encoding and suggests a UTF-8 locale.
    No locale setting fixes an unpaired surrogate; catching it here instead
    gives a clean, accurate error at the point the bad name was supplied.

    A Unicode "Tags" block character (U+E0000-U+E007F, see
    `_is_unicode_tag_char` in `parser.py`) has the same "looks the same,
    isn't" risk here as it does in card text: it has no visible glyph in
    any font, so two decks whose names read identically on screen (in
    `sync`'s deck listing, `add`'s confirmation, `--deck`'s filter) could
    actually be different names underneath, and a `--deck` value typed to
    match what's displayed would silently fail to match the deck it looks
    identical to. Same narrow-range check as `_check_card_text` uses, for
    the same reason a blanket Cf rejection would wrongly catch legitimate
    ZWJ/variation-selector characters in an emoji-bearing deck name.

    U+FEFF (`ZERO_WIDTH_NO_BREAK_SPACE` in `parser.py`), the byte-order mark,
    gets the same rejection for the same reason: it's invisible everywhere
    outside position zero of a file (the one place `_read_deck_text` already
    strips it), so a deck name with one spliced in (e.g. built by a script
    that concatenates a BOM-prefixed value) reads identically to the same
    name without it in every listing this module prints, while comparing
    unequal to it -- the identical "looks the same, isn't" gap the Tags-block
    check above exists to close.

    U+200B (`ZERO_WIDTH_SPACE` in `parser.py`), the zero-width space, gets
    the same rejection for the same reason: unlike ZWJ/ZWNJ (deliberately
    still allowed, see the Tags-block paragraph above) it isn't part of any
    legitimate emoji or script-shaping sequence, and unlike a bidi override
    it doesn't just reorder text someone can still read -- it's invisible in
    every renderer, so a deck name with one spliced in (a common copy-paste
    artifact from web pages that use it as an invisible wrap hint) reads
    identically to the same name without it, while comparing unequal to it.
    """
    if "/" in name or "\\" in name:
        return f"invalid deck name: {name!r} (deck names can't contain a path separator)"
    if name in (".", ".."):
        return f"invalid deck name: {name!r} (deck names can't be '.' or '..')"
    if not name:
        return "invalid deck name: '' (deck name can't be empty)"
    for ch in name:
        if unicodedata.category(ch) == "Cc":
            return (
                f"invalid deck name: {name!r} (contains a control character {ch!r}, "
                "which can hide or overwrite what's shown on screen)"
            )
        if unicodedata.category(ch) == "Cs":
            return (
                f"invalid deck name: {name!r} (contains an unpaired Unicode surrogate "
                f"U+{ord(ch):04X}, which can't be encoded to UTF-8 or written to a file at all)"
            )
        if unicodedata.bidirectional(ch) in BIDI_FORMATTING_CLASSES:
            return (
                f"invalid deck name: {name!r} (contains a bidirectional-formatting "
                f"character U+{ord(ch):04X}, which can reorder how surrounding text "
                "is displayed on screen)"
            )
        if ch in LINE_SEPARATOR_CHARS:
            return (
                f"invalid deck name: {name!r} (contains a Unicode line/paragraph "
                f"separator U+{ord(ch):04X}, which displays as a line break and "
                "breaks stats's tabular layout)"
            )
        if _is_unicode_tag_char(ch):
            return (
                f"invalid deck name: {name!r} (contains a Unicode tag character "
                f"U+{ord(ch):04X}, which has no visible glyph in any font and can "
                "make two visually-identical deck names actually differ)"
            )
        if ch == ZERO_WIDTH_NO_BREAK_SPACE:
            return (
                f"invalid deck name: {name!r} (contains a byte-order-mark character "
                "U+FEFF, which is invisible and can make two visually-identical deck "
                "names actually differ)"
            )
        if ch == ZERO_WIDTH_SPACE:
            return (
                f"invalid deck name: {name!r} (contains a zero-width space U+200B, "
                "which is invisible and can make two visually-identical deck names "
                "actually differ)"
            )
    return None


def _find_deck_path(decks_dir: Path, deck_name: str) -> Path:
    """Return the on-disk file backing `deck_name` (already `_normalize_deck_name`d), the
    same way `sync` finds it, instead of just guessing `decks_dir / f"{deck_name}.md"`.

    A deck file isn't guaranteed to already be named in NFC, even though `deck_name` always
    is by the time it reaches here: deck files are documented as normal to hand-create or
    hand-rename outside the CLI (see `cmd_sync`'s handling of a hand-created, oddly-named
    file), and a normalization-happy filesystem such as macOS's (HFS+/APFS) stores accented
    file names as NFD by default — a byte-for-byte NFD name that survives a `git clone` onto
    a normalization-preserving filesystem like Linux's completely untouched, since git stores
    file names as literal bytes either way.

    `cmd_sync` already normalizes `deck_file.stem` (see `_normalize_deck_name`) before using
    it as the deck's identity, so `due`/`stats`/`review`/`hard` all correctly show such a
    deck as existing and populated. Guessing the path from `deck_name` alone, as `add`/
    `remove`/`edit` used to, only ever matches a file that already happens to be NFC-named —
    against an NFD-named one it matches nothing, so `remove`/`edit` wrongly reported "no such
    deck" for a deck `stats` had just shown as real, and `add` (which creates a file when none
    is found) went ahead and silently wrote a *second*, colliding file next to the first —
    exactly the "two files, one deck name" collision `sync` otherwise refuses to merge and
    warns about, just self-inflicted here, with the new card silently dropped from every sync
    thereafter (the losing file of a collision is skipped, not merged) and no hint in `add`'s
    own cheerful success message that anything went wrong.

    Searches the same `decks_dir.glob("*.md")` sync itself iterates, in the same sorted
    order. When two or more physically different files collide on `deck_name`, this
    picks the first one by that same sort order — but callers must check
    `_check_deck_collision` first and refuse to proceed if it reports one: sync (session
    155) deliberately stopped treating any one colliding file as "canonical" and now
    refuses to touch any of them, precisely because there's no way to tell which one is
    real from inside the tool. Silently picking one here anyway would just move that same
    guessing back into add/remove/edit.

    Falls back to the plain NFC-guessed path when no existing file matches: a deck that
    genuinely doesn't exist yet (or `decks_dir` itself doesn't exist yet). `add` uses that
    path to create the new file; `remove`/`edit` use it only to report where they looked.
    """
    if decks_dir.is_dir():
        for candidate in sorted(decks_dir.glob("*.md")):
            if _normalize_deck_name(candidate.stem) == deck_name:
                return candidate
    return decks_dir / f"{deck_name}.md"


def _describe_collision_paths(paths) -> str:
    """Render a list of colliding deck-file paths so the paths are actually
    distinguishable from each other on screen.

    The whole point of a collision message is "rename the files so they're
    distinct" — but the files collide *because* they normalize to the same
    deck name, and the most common real way that happens (see
    `_find_deck_path`'s docstring) is two different Unicode normalization
    forms of the same visible text, e.g. "café" as one precomposed codepoint
    (NFC) versus "e" + a combining acute accent (NFD). Both render on screen
    as the exact same glyphs — that's the whole reason they're a collision in
    the first place — so plain `str(path)`-joining here used to print the
    same-looking path twice, e.g. "…/café.md, …/café.md", with genuinely no
    way for a person reading it to tell which listed path is which file, let
    alone act on "rename the files so they're distinct."

    `repr()` doesn't help either: Python only escapes a string's *unprintable*
    characters, and a combining mark like U+0301 is printable (it just renders
    combined with the character before it) — so `repr()` of an NFD name still
    prints as plain "café", identical to the NFC one. `ascii()` forces every
    non-ASCII character to an escaped `\\xXX`/`\\uXXXX` form regardless of
    printability, which is exactly the disambiguation needed here: the NFC
    name becomes "caf\\xe9.md" and the NFD name becomes "cafe\\u0301.md" —
    visibly different, and precise enough to actually resolve by hand.
    """
    return ", ".join(ascii(str(path)) for path in paths)


def _check_deck_collision(decks_dir: Path, deck_name: str) -> Optional[str]:
    """Return an error message if more than one physically distinct file in `decks_dir`
    normalizes to `deck_name`, else None.

    `_find_deck_path` above silently returns the first (sorted) match when this happens —
    fine for locating the *one* real file in the ordinary case, but a real collision (two
    files that both happen to normalize to the same deck name — see `_find_deck_path`'s own
    docstring for how this arises, e.g. an NFD-named file surviving a `git clone` from
    macOS next to an NFC one) means that "first match" is an arbitrary pick, not a correct
    one. `cmd_sync` already detects this exact situation and refuses to touch either file
    (session 155) rather than gamble on sort order — but `add`/`remove`/`edit` had no
    equivalent check, so `_find_deck_path` picked one of the two files anyway: `add` could
    silently write a new card into a throwaway or unrelated file while the deck's real,
    already-synced cards (review history included) sat untouched in the other, colliding
    file; `remove`/`edit` could silently operate on the wrong file's content entirely,
    reporting "no card with that question found" for a question that's really there, just
    in the file this picked the other one over — the exact ambiguity `sync` already refuses
    to guess through, just reached through a different door.

    Checked separately from `_find_deck_path` (rather than folded into it) so every caller
    can refuse up front, the same way `_invalid_deck_name`'s callers do, instead of acting
    on a path that might be the wrong one.
    """
    if not decks_dir.is_dir():
        return None
    matches = [
        candidate
        for candidate in sorted(decks_dir.glob("*.md"))
        if _normalize_deck_name(candidate.stem) == deck_name
    ]
    if len(matches) <= 1:
        return None
    names = _describe_collision_paths(matches)
    return (
        f"{len(matches)} files collide on this same deck name ({names}) -- refusing to "
        "guess which one you mean; rename the files so they're distinct decks (or merge "
        "them by hand), then sync and try again"
    )


def _atomic_write_text(path: Path, data: str) -> None:
    """Replace `path`'s content with `data` without ever leaving it truncated.

    `Path.write_text` opens in 'w' mode, which truncates the file to zero
    bytes *before* writing anything — anything that interrupts the write
    after that point (disk full, the process killed, permissions revoked
    mid-write) leaves the deck file empty, destroying every card it held,
    not just failing the one add/remove/edit that was in progress. Writing
    to a sibling temp file and `os.replace`-ing it into place means a failed
    write only ever loses the disposable temp file — `path` itself is
    either the old content or the new content, never a partial one.

    If `path` is itself a symlink (a deck file kept somewhere else and
    linked into `decks_dir` -- e.g. a shared repo of deck content), the
    temp file is written next to and replaces the *resolved target*, not
    `path` itself. `os.replace` doesn't follow a symlink at its destination
    -- it replaces that directory entry outright -- so writing to `path`
    directly would silently sever the symlink on the very first add/
    remove/edit, turning it into an ordinary, independent regular file
    holding only this write's content, while the real target file (and
    anything else pointing at it) is left disconnected and unaware of the
    change. `path.is_symlink()` is false for a path that doesn't exist yet
    (a brand new deck being created by `add`), so that case still creates
    an ordinary file at `path`, unchanged from before.

    A symlink that loops back on itself (directly, e.g. `ln -s spanish.md
    spanish.md`, or through a longer chain) is a real, reachable filesystem
    state -- a typo'd `ln -s`, or two half-finished scripts each linking the
    other's output -- not just a hypothetical, and `path.exists()` (used
    earlier, e.g. by `add`, to decide there's no existing content to read)
    correctly reports False for one, the same as it does for an ordinary
    broken symlink. But `Path.resolve()` does its own, separate cycle
    detection and raises a bare `RuntimeError("Symlink loop from ...")`, not
    an `OSError`, when it finds one -- unlike every other real filesystem
    failure in this function (a missing parent directory, permissions),
    which surfaces as an `OSError` that `main`'s existing handler already
    turns into a clean, one-line message. Without catching it here and
    re-raising as `OSError`, a symlink-loop deck file crashed with a raw
    traceback exposing local paths instead.
    """
    if path.is_symlink():
        try:
            target = path.resolve()
        except RuntimeError as exc:
            raise OSError(f"{path} is a symlink loop -- can't resolve it to a real file") from exc
    else:
        target = path
    tmp_path = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _lock_dir() -> Path:
    """Directory holding deck lock files — a seam the test suite patches.

    Otherwise every add/remove/edit invocation in the test suite drops a
    real, never-cleaned-up file (see `_deck_lock_path`) into the actual
    system temp directory; patching this lets tests redirect them into a
    temp directory that's already torn down at the end of each test.
    """
    return Path(tempfile.gettempdir())


def _deck_lock_path(decks_dir: Path, deck: str) -> Path:
    """Return the lock file path for `deck` in `decks_dir`.

    Keyed by `decks_dir`'s *resolved* (absolute, symlink-followed) path plus
    the deck name, the same identity `cmd_sync`/`sync_deck`/`DeckDirMismatch`
    already use to recognize "this is the same deck" across differently
    -- but equivalently -- spelled `--decks-dir` values (a relative path from
    one cwd, an absolute path, a path through a symlink). `--state-dir` is
    deliberately *not* part of this key: unlike `--decks-dir`, nothing ties
    a `--state-dir` to a particular deck file at all, and two flashback
    invocations are free to use different `--state-dir`s (different cwds
    with the default `--state-dir .flashback`, say) while still pointing at
    the exact same shared `--decks-dir` -- see `_deck_lock`'s docstring for
    why locking under `--state-dir` used to silently fail to protect exactly
    that case.

    The lock file itself lives under the system temp directory, not inside
    `decks_dir`: `_deck_lock` still needs somewhere all cooperating
    processes can find without already agreeing on a `--state-dir`, but a
    real, visible file dropped next to the user's own deck files (especially
    one that's never cleaned up, since `flock` -- not deletion -- is what
    signals "unlocked") is exactly what the earlier, `--state-dir`-based
    design went out of its way to avoid. The hash keeps the filename short
    and free of any character `decks_dir`/`deck` could themselves contain.
    """
    key = hashlib.sha1(f"{decks_dir.resolve()}\x00{deck}".encode("utf-8")).hexdigest()
    return _lock_dir() / f"flashback-{key}.lock"


@contextmanager
def _deck_lock(lock_path: Path, state_dir: Path):
    """Serialize add/remove/edit's read-modify-write section for one deck file.

    Without this, two flashback processes touching the *same* deck at once
    (e.g. a shell loop backgrounding several `add` calls to import many
    cards quickly) can each read the same starting content, compute their
    own updated version independently, and whichever writes last silently
    wins — the other process's card is dropped entirely, with no error and
    a normal "added"/"edited"/"removed" success message printed by both.
    `_atomic_write_text` already makes each individual write atomic, but
    atomicity alone doesn't help here: this is a lost update between two
    otherwise-correct writers racing each other, not a torn write.

    `lock_path` (see `_deck_lock_path`) is keyed by `--decks-dir`, not
    `--state-dir`: an earlier version of this lock lived under `--state-dir`
    instead, which silently stopped protecting anything the moment two
    cooperating invocations used *different* `--state-dir`s pointed at the
    same shared `--decks-dir` (verified directly: 16 concurrent `add`s to a
    fresh deck, one per distinct `--state-dir`, lost 9 of 16 cards with the
    old, `--state-dir`-keyed lock -- the exact silent lost-update this
    function exists to prevent, just reached through a door the old key
    couldn't see). Nothing stops two invocations from doing this deliberately
    (a shared `--decks-dir` with a personal `--state-dir` per collaborator)
    or by accident (the default `--state-dir` is relative, so running from
    two different working directories against one absolute `--decks-dir`
    already does it).

    Uses an OS-level advisory lock (`fcntl.flock`) rather than a lock file
    whose mere existence signals "locked": `flock` is released automatically
    when its file descriptor closes, including if the holding process is
    killed, so there's no stale lock to clean up by hand. POSIX-only, like
    the rest of this project has no separate Windows handling either; on
    Windows this is a no-op and the pre-existing race remains, no worse than
    before this fix.

    Still separately touches `state_dir` (creating and `.gitignore`-seeding
    it via the same `ensure_state_dir` helper `open_db` uses) even though the
    lock itself no longer lives there: add/remove/edit never call `open_db`,
    so this remains the one place a fresh `--state-dir` gets seeded on that
    path, and callers (and their tests) still expect that to happen.
    """
    ensure_state_dir(state_dir)
    if fcntl is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _is_symlink_loop(path: Path) -> bool:
    """True if `path` is a symlink that can never be resolved to a real file
    because it loops back on itself -- directly (`ln -s spanish.md
    spanish.md`) or through a longer chain.

    `_atomic_write_text` already detects exactly this on the *write* side
    (see its own docstring) by catching the bare `RuntimeError` that
    `Path.resolve()` raises for a cycle. This is the same detection, reused
    on the *read* side by `_read_deck_text` and by `cmd_remove`/`cmd_edit`'s
    own up-front `deck_path.exists()` checks -- both of which, without this,
    treat a looping symlink exactly like a deck file that was never there or
    was deleted (see `_read_deck_text`'s docstring for why that's wrong).
    """
    if not path.is_symlink():
        return False
    try:
        path.resolve()
    except RuntimeError:
        return True
    return False


def _read_deck_text(deck_path: Path) -> str:
    """Read a deck file as UTF-8, raising ParseError (not UnicodeDecodeError) on bad bytes.

    `cmd_sync` already skips a deck file that isn't valid UTF-8 instead of crashing the
    whole run (session 47) — but add/remove/edit each read one *specific* deck file the
    user named, where "skip it and continue" isn't an option, and they called
    `Path.read_text` directly with no such guard. `UnicodeDecodeError` is a `ValueError`
    subclass, not an `OSError`, so it isn't caught by main()'s existing OSError handler
    either: a corrupted or hand-mis-encoded deck file crashed add/remove/edit with a raw
    traceback exposing local paths, unlike every other user-facing failure in this file.
    Raising ParseError here lets every call site reuse the `except ParseError` handling
    it already has, instead of adding a second, separate except clause at each one.

    `encoding="utf-8-sig"`, not plain `"utf-8"`: Notepad and various other editors and
    export tools default to writing a UTF-8 byte-order-mark (U+FEFF) at the start of a
    file. Plain `"utf-8"` decodes that BOM as a real, visible character rather than
    stripping it, so it lands as the first character of whatever the first line of the
    file is — silently turning a perfectly well-formed "Q: ..." first line into
    "﻿Q: ..." from the parser's point of view. That doesn't match `Q_PREFIX`, so
    `_parse_card` reads it as content *before* the card's first "Q:" line and rejects
    the whole card, with an error that shows a confusing `﻿` escape instead of
    naming the actual problem. `"utf-8-sig"` strips a leading BOM if present and
    otherwise decodes identically to `"utf-8"`, so this is safe for every file, BOM or
    not.

    Also refuses to read anything that isn't a regular file (following symlinks --
    see `_atomic_write_text` for the symlinked-deck-file case this deliberately still
    allows, since `Path.is_file()` resolves a symlink before checking its target's
    type). A `--decks-dir` is documented as normal to hand-populate, and a FIFO
    (named pipe) sitting there -- created by hand, by another program, or left
    behind by some unrelated tool -- is a real, if unusual, way that can happen.
    `open()` on a FIFO's read end blocks at the kernel level until some other
    process opens its write end, forever if nothing ever does, so a plain
    `read_text()` call here doesn't fail on one, it hangs the *entire* invocation
    indefinitely: for `sync`, not just that one deck skipped but the whole run --
    every other deck, including ones already synced and reported this run -- stuck
    with no error, no timeout, and no way out short of killing the process by hand;
    for `add`/`remove`/`edit`, which read one specific deck file with no "skip and
    continue" option to fall back on, the same hang with nothing to show for it at
    all. `Path.is_file()` is safe to check first because it's backed by `stat()`,
    not `open()` -- stat-ing a FIFO returns instantly and reports its real type; only
    actually opening one for an ordinary read blocks. (The identical failure shape,
    a stray FIFO silently exhausting the one resource -- a thread, here a whole
    process -- needed to serve everything else, already cost `journal`'s
    `server.js` a full-site DoS from a single stray file, fixed in an earlier
    session of this same project's rotation; this file never got the equivalent
    check until now.)

    Checks non-existence separately from, and before, that same `Path.is_file()`
    call -- a missing path also makes `is_file()` return False, exactly like a
    FIFO/device/socket/directory does, so folding the two into one check-and-raise
    (as this used to) blames a deck file that simply isn't there anymore on being
    "a FIFO, device, socket, or similar special file, or a directory", which is
    false and actively misleading for this cause. `remove`/`edit` both call this
    a second time, inside `_deck_lock`, after already confirming the file existed
    once earlier in the same command -- `edit` right after an interactive prompt
    for the new question/answer text that can, per its own docstring, "take
    arbitrarily long", and `remove` right after its own prompt for `-q` when it's
    omitted -- so another process (a concurrent `remove` + `sync`, or a person
    deleting the file by hand) deleting the deck file in that window is a real,
    reachable sequence, not a hypothetical: the earlier existence check has
    already passed by the time it happens, and nothing about a FIFO, device,
    socket, or directory was ever involved.

    Checks `_is_symlink_loop` before that same non-existence check, for the
    identical reason: a symlink that loops back on itself (see that
    function's own docstring) makes `Path.exists()` return False exactly
    like a deleted deck file does -- `Path.exists()` follows symlinks and,
    since a loop can never resolve to a real file, reports "not there" for
    one the same way it does for a path with nothing at all. Without this
    check first, a hand-created (or two-half-finished-scripts-created) loop
    at a deck file's path was blamed on having "been deleted (by hand, or by
    another flashback invocation)" -- false on both counts, since the file
    was never touched, let alone deleted, and this reads back the exact same
    way on every subsequent `sync`/`remove`/`edit`, not just once right after
    it's created. `_atomic_write_text` already gives an accurate, distinct
    message for this same cycle on the write side (see its own docstring);
    this is that same diagnosis, reached from the read side instead --
    `sync`'s own read of such a file, or `remove`/`edit`'s, not `add`'s write
    to a brand-new one.
    """
    if _is_symlink_loop(deck_path):
        raise ParseError(f"{deck_path} is a symlink loop -- can't resolve it to a real file")
    if not deck_path.exists():
        raise ParseError(
            f"{deck_path} no longer exists -- it may have been deleted (by hand, or "
            "by another flashback invocation) since this command started"
        )
    if not deck_path.is_file():
        raise ParseError(
            f"{deck_path} is not a regular file (it looks like a FIFO, device, "
            "socket, or similar special file, or a directory) -- flashback only "
            "reads plain deck files, since opening some special files for an "
            "ordinary read can block forever instead of failing"
        )
    try:
        return deck_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError(f"{deck_path} is not valid UTF-8 ({exc})") from exc


def cmd_sync(args):
    decks_dir = Path(args.decks_dir)
    if not decks_dir.is_dir():
        print(f"no such directory: {decks_dir}", file=sys.stderr)
        return 1
    # Resolved to an absolute path so the same `--decks-dir` value typed from
    # two different working directories doesn't look like two different
    # directories, and so it's stable to store/compare across runs — see
    # sync_deck/prune_missing_decks for why a deck's *last-synced-from*
    # directory needs to be recorded and checked at all: a `--state-dir`
    # shared across more than one `--decks-dir` (nothing stops a user from
    # doing this, e.g. a copy-pasted command with the wrong `--decks-dir`, or
    # deliberately pointing `--state-dir` somewhere central) used to let one
    # decks-dir's sync silently delete another, unrelated decks-dir's cards
    # the moment their deck names weren't a perfect match, printing "deck
    # file no longer exists" for files that were never touched.
    decks_dir_key = str(decks_dir.resolve())

    today = date.today()
    with open_db(_db_path(args)) as conn:
        total_added = total_removed = 0
        deck_names = set()
        # Grouped by normalized deck name *before* any file is actually
        # synced, so a collision between two (or more) physically distinct
        # files can be detected up front — see below for why discovering it
        # only once the second file is reached, after the first has already
        # been synced, isn't good enough.
        files_by_deck = {}
        for deck_file in sorted(decks_dir.glob("*.md")):
            # NFC-normalized so a deck file whose name happens to be encoded
            # in a different (but visually identical) Unicode normalization
            # form than what add/remove/edit would have written still counts
            # as the same deck identity everywhere else — see
            # _normalize_deck_name.
            deck_name = _normalize_deck_name(deck_file.stem)
            # Added to deck_names before the name check below (not after), so
            # a deck that was already synced under this name in a past run
            # doesn't get pruned by prune_missing_decks just because its name
            # is now rejected — the same "currently unusable isn't the same
            # as deleted" reasoning already applied to a ParseError/
            # UnicodeDecodeError/OSError below.
            deck_names.add(deck_name)
            # add/remove/edit already reject a bad deck name before writing,
            # but a deck file can also be created or renamed by hand outside
            # the CLI (documented as normal — see parse_deck's own validate
            # path) — without this check, sync would read the file fine and
            # print its control-character/bidi-override-laden name straight
            # to the terminal in every command that lists decks afterward.
            name_error = _invalid_deck_name(deck_name)
            if name_error is not None:
                # deck_file!r, not deck_file: name_error already reprs the
                # offending deck name so a control character/bidi-override
                # never reaches the terminal raw (see _invalid_deck_name) —
                # but deck_file is a Path built from that exact same bad
                # name, and str-interpolating it here would print the
                # identical raw byte right next to the safely-reprd copy,
                # undoing the whole point of the check for this one message.
                print(f"skipping {deck_file!r}: {name_error}", file=sys.stderr)
                continue
            files_by_deck.setdefault(deck_name, []).append(deck_file)

        for deck_name, deck_files in files_by_deck.items():
            if len(deck_files) > 1:
                # Two or more *physically different* files (different bytes
                # on disk, confirmed distinct by decks_dir.glob returning all
                # of them) can each normalize to the same deck_name — e.g.
                # one written before NFC-normalization existed and one after,
                # or one just pasted from somewhere with a different
                # composition. sync_deck's own reconciliation ("delete any
                # card of this deck not in the file just handed to it")
                # assumes it's the only source for that deck name in this
                # run. Picking one file to "win" (by sort order, as this used
                # to) and reconciling against it is real data loss, not
                # merely a cosmetic double listing: if the deck already had
                # real, previously-synced cards — review history included —
                # and the file that happens to sort first is a brand-new,
                # unrelated file, reconciliation deletes every one of those
                # established cards from the database, even though neither
                # file on disk was touched. So no file is synced at all while
                # a collision exists: the deck's existing database state (if
                # any) is left exactly as it was, until a person resolves the
                # collision by renaming one of the files and syncing again.
                names = _describe_collision_paths(deck_files)
                print(
                    f"skipping {deck_name!r}: {len(deck_files)} files collide on "
                    f"this same deck name ({names}) -- not syncing any of them, so "
                    "this deck's existing review history (if any) is left "
                    "untouched; rename the files so they're distinct decks (or "
                    "merge them by hand) and sync again",
                    file=sys.stderr,
                )
                continue
            deck_file = deck_files[0]
            try:
                # _read_deck_text, not a raw deck_file.read_text(...): besides
                # the BOM-stripping this used to inline here directly, it also
                # refuses to actually open a FIFO/device/socket/directory
                # sitting at this path instead of blocking sync's entire run
                # forever on one — see that function's own docstring.
                cards = parse_deck(_read_deck_text(deck_file))
            except (ParseError, UnicodeDecodeError, OSError) as exc:
                # A deck file that isn't valid UTF-8, or isn't even a regular
                # file (e.g. a directory happens to match *.md), is the same
                # kind of "skip this one deck, don't lose the rest" situation
                # as a ParseError — without catching these too, either one
                # crashed the whole sync with a raw traceback, taking every
                # other deck's changes down with it instead of just the one
                # deck that's actually broken.
                print(f"skipping {deck_file}: {exc}", file=sys.stderr)
                continue
            try:
                added, removed = sync_deck(conn, deck_name, cards, today, decks_dir_key)
            except DeckDirMismatch as exc:
                # This deck name is already owned by a different, concrete
                # --decks-dir recorded in this same --state-dir — reconciling
                # against it here would silently delete or overwrite that
                # other, unrelated deck's cards (review history included),
                # even though its own file was never touched. Same "don't
                # guess, refuse" response as the same-directory physical-file
                # collision above, just for a collision across directories
                # instead of within one.
                print(
                    f"skipping {deck_name!r}: {exc} -- syncing it from here would "
                    "silently delete or overwrite that other directory's cards for "
                    "this same deck name, which is almost certainly a different, "
                    "unrelated deck that just happens to share the name; rename one "
                    "of the two deck files so they're distinct, or use a separate "
                    "--state-dir per --decks-dir, then sync again",
                    file=sys.stderr,
                )
                continue
            # Commit each deck immediately rather than relying on open_db's
            # single end-of-session commit: an interruption partway through a
            # multi-deck sync (KeyboardInterrupt, a crash on a later deck)
            # skipped that final commit entirely, silently rolling back every
            # deck synced earlier in the same run too — even ones that had
            # already printed "N new, M removed" as if it were saved.
            conn.commit()
            total_added += added
            total_removed += removed
            print(f"{deck_name}: {_cards(len(cards))} ({added} new, {removed} removed)")
        pruned = prune_missing_decks(conn, deck_names, decks_dir_key)
        conn.commit()
        for deck_name, count in pruned:
            total_removed += count
            print(f"{deck_name}: deck file no longer exists, removed {count} card(s)")
    print(f"synced. {total_added} new, {total_removed} removed total.")
    return 0


def cmd_add(args):
    args.deck = _normalize_deck_name(args.deck)
    error = _invalid_deck_name(args.deck)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    decks_dir = Path(args.decks_dir)
    collision_error = _check_deck_collision(decks_dir, args.deck)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 1
    deck_path = _find_deck_path(decks_dir, args.deck)
    # ensure_state_dir(state_dir) before decks_dir.mkdir(...), not after: when
    # --decks-dir and --state-dir happen to be the same not-yet-existing
    # directory (a real, anticipated configuration -- see _deck_lock_path's
    # and ensure_state_dir's own docstrings, both of which single out
    # `--state-dir .` by name), decks_dir.mkdir(...) would otherwise bring
    # that shared directory into existence first. _deck_lock below calls
    # ensure_state_dir(state_dir) again to seed --state-dir's .gitignore, but
    # by then it would no longer look "new", so the .gitignore protection
    # ensure_state_dir exists to provide would silently never fire for this
    # deck's very first add. Calling it here first -- before decks_dir can
    # possibly create the same path -- makes the order state_dir/decks_dir
    # are actually brought into existence match what "is this state_dir new"
    # is supposed to mean, regardless of whether the two paths coincide.
    ensure_state_dir(Path(args.state_dir))
    decks_dir.mkdir(parents=True, exist_ok=True)

    question = args.question if args.question is not None else input("Q: ")
    answer = args.answer if args.answer is not None else input("A: ")

    with _deck_lock(_deck_lock_path(decks_dir, args.deck), Path(args.state_dir)):
        # Re-check for a collision now, not just once before the interactive
        # question/answer prompts above: those prompts (like edit's, per its
        # own docstring) can take arbitrarily long, and a second, colliding
        # deck file (e.g. a hand-created or hand-renamed one -- see
        # _find_deck_path's own docstring for how that happens) can appear in
        # that window. Without this, `deck_path` above -- picked while there
        # was still only one candidate file (or none at all) -- goes stale.
        collision_error = _check_deck_collision(decks_dir, args.deck)
        if collision_error:
            print(f"error: {collision_error}", file=sys.stderr)
            return 1
        # Also re-resolve deck_path itself, not just the collision check: if
        # this deck had *no* file yet when deck_path was guessed above, and
        # exactly one now exists (created by hand, or by another process,
        # during the prompts), the guessed path and the real file are two
        # different paths -- neither one a "collision" by _check_deck_collision's
        # own definition, since only one file exists either way -- so without
        # this, `add` would still silently create a second, unrelated file at
        # the stale guessed path instead of appending to the real one.
        deck_path = _find_deck_path(decks_dir, args.deck)
        try:
            existing_text = _read_deck_text(deck_path) if deck_path.exists() else ""
            new_text = append_card(existing_text, question, answer)
        except ParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _atomic_write_text(deck_path, new_text)
    print(f"added to {deck_path} (run `flashback sync` to pick it up)")
    return 0


def cmd_remove(args):
    args.deck = _normalize_deck_name(args.deck)
    error = _invalid_deck_name(args.deck)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    decks_dir = Path(args.decks_dir)
    collision_error = _check_deck_collision(decks_dir, args.deck)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 1
    deck_path = _find_deck_path(decks_dir, args.deck)
    # `or _is_symlink_loop(deck_path)`: a symlink that loops back on itself
    # makes plain `.exists()` report False exactly like a deck that was
    # never created does (see `_read_deck_text`'s docstring) -- without this,
    # a deck file that's genuinely sitting right there, just as an unusable
    # loop, was misreported as "no such deck" instead of the accurate
    # "symlink loop" error `_read_deck_text` below now gives once this lets
    # it through to that check instead of bailing out here first.
    if not deck_path.exists() and not _is_symlink_loop(deck_path):
        print(f"no such deck: {deck_path}", file=sys.stderr)
        return 1

    question = args.question if args.question is not None else input("Q: ")

    with _deck_lock(_deck_lock_path(decks_dir, args.deck), Path(args.state_dir)):
        # Re-check for a collision now, not just once before the (possibly
        # interactive, possibly long) -q prompt above -- see cmd_add's
        # identical re-check for why a second, colliding deck file appearing
        # in that window can't be caught by the earlier check alone.
        collision_error = _check_deck_collision(decks_dir, args.deck)
        if collision_error:
            print(f"error: {collision_error}", file=sys.stderr)
            return 1
        # Also re-resolve deck_path itself, not just the collision check: the
        # file backing this deck name can be renamed (e.g. to a different
        # Unicode normalization form of the same accented name -- see
        # _find_deck_path's own docstring) during the -q prompt above without
        # ever becoming a "collision" by _check_deck_collision's own
        # definition, since only one file exists either way. Without this,
        # `remove` would read the now-stale `deck_path` computed before the
        # rename, see it's gone, and wrongly report "no longer exists" for a
        # deck that's still very much there under its new on-disk name --
        # exactly the staleness cmd_add's identical re-resolve already
        # guards against.
        deck_path = _find_deck_path(decks_dir, args.deck)
        try:
            existing_text = _read_deck_text(deck_path)
            new_text = remove_card(existing_text, question)
        except ParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _atomic_write_text(deck_path, new_text)
    print(
        f"removed from {deck_path} (run `flashback sync` to pick it up -- "
        "this card's review history will be deleted on next sync)"
    )
    return 0


def cmd_edit(args):
    args.deck = _normalize_deck_name(args.deck)
    error = _invalid_deck_name(args.deck)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    decks_dir = Path(args.decks_dir)
    collision_error = _check_deck_collision(decks_dir, args.deck)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 1
    deck_path = _find_deck_path(decks_dir, args.deck)
    # See cmd_remove's identical check for why a symlink loop has to be let
    # through here rather than reported as "no such deck".
    if not deck_path.exists() and not _is_symlink_loop(deck_path):
        print(f"no such deck: {deck_path}", file=sys.stderr)
        return 1

    # normalize_question here mirrors edit_card()'s own normalization of this
    # same search key below — without it, a -q spelled in a different (but
    # visually identical) Unicode normalization form than what's stored would
    # fail this pre-lookup and error out before ever reaching edit_card(),
    # the same gap that used to exist here for surrounding whitespace.
    question = normalize_question((args.question if args.question is not None else input("Q: ")).strip())

    # Re-check for a collision now, not just once before the (possibly
    # interactive, possibly long) -q prompt above -- see cmd_add's identical
    # re-check for why a second, colliding deck file appearing in that window
    # can't be caught by the earlier check alone. Without this, the
    # `_find_deck_path` re-resolve just below -- which silently picks one of
    # two colliding files by sort order once a collision exists, per its own
    # docstring -- could read the preview from the *wrong* file: showing the
    # user a "current Q/A" that isn't their real card's content at all (or
    # failing to find their question there and wrongly reporting "no card
    # with that question found" for one that's genuinely there, just in the
    # other, colliding file), before the later collision check inside the
    # lock ever gets a chance to refuse the operation.
    collision_error = _check_deck_collision(decks_dir, args.deck)
    if collision_error:
        print(f"error: {collision_error}", file=sys.stderr)
        return 1
    # Re-resolve deck_path itself, not just reuse the guess from before this
    # (possibly interactive, possibly long) -q prompt: the file backing this
    # deck name can be renamed in that window -- e.g. to a different Unicode
    # normalization form of the same accented name, see _find_deck_path's own
    # docstring -- without ever becoming a "collision" (only one file exists
    # either way). Without this, the preview read just below would use the
    # now-stale path, see it's gone, and wrongly report "no longer exists"
    # for a deck that's still there under its new on-disk name.
    deck_path = _find_deck_path(decks_dir, args.deck)

    try:
        preview_text = _read_deck_text(deck_path)
        # validate=False: this is just a lookup to show the card's current
        # text before prompting — it shouldn't be blocked by some other,
        # unrelated card in the same deck failing _check_card_text.
        # edit_card() below still validates whatever new text is actually
        # written.
        matches = [c for c in parse_deck(preview_text, validate=False) if c.question == question]
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not matches:
        print(f"error: no card with that question found: {question!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        # edit_card() below already refuses to guess which of several
        # same-question cards is meant (a hand-edited duplicate, tolerated
        # here by the validate=False parse above so it doesn't block editing
        # some other, unrelated card) -- but that check doesn't run until
        # after this preview has already picked one of them (via `next`,
        # arbitrarily, by parse order) to print as "current Q/A" and prompt
        # for new content against. Checking here too, before printing or
        # prompting for anything, avoids showing one arbitrarily-chosen
        # duplicate's content as if it were the card's only content and only
        # then discovering the ambiguity after the person has already
        # answered both prompts.
        print(
            f"error: {len(matches)} cards share this same question ({question!r}) -- refusing to "
            "guess which one you mean to edit; fix the duplicate by hand, then edit/sync again",
            file=sys.stderr,
        )
        return 1
    match = matches[0]

    new_question = args.new_question
    new_answer = args.new_answer
    if new_question is None and new_answer is None:
        # The matched card's own text is about to be printed straight to the
        # terminal below — the same risk _check_card_text guards against for
        # sync/review, and the reason this deliberately doesn't use the
        # validate=False lookup above for this specific card, even though
        # that lookup is correct for every *other* card in the deck.
        try:
            _check_card_text(match.question, match.answer)
        except ParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # Both shown before either prompt -- the README promises "showing
        # you the current question and answer first so you can see what
        # you're changing", which means seeing the whole card before
        # deciding on either new value, not discovering the current answer
        # only after already having answered the new-question prompt.
        print(f"current Q: {match.question}")
        print(f"current A: {match.answer}")
        new_question = input("new Q (blank to keep): ").strip() or None
        new_answer = input("new A (blank to keep): ").strip() or None
        if new_question is None and new_answer is None:
            print("nothing changed.")
            return 0

    # Re-read existing_text fresh here, inside the lock, rather than reusing
    # preview_text above: the interactive prompting in between can take
    # arbitrarily long, and the file may have changed since preview_text was
    # read (by another flashback process, or by hand). edit_card() below
    # must act on the current on-disk content, not a stale snapshot from
    # before the prompts.
    with _deck_lock(_deck_lock_path(decks_dir, args.deck), Path(args.state_dir)):
        # Re-check for a collision now, not just once before the interactive
        # prompts above -- see cmd_add's identical re-check for why a second,
        # colliding deck file appearing during that (potentially arbitrarily
        # long, per this function's own docstring) window can't be caught by
        # the earlier check alone.
        collision_error = _check_deck_collision(decks_dir, args.deck)
        if collision_error:
            print(f"error: {collision_error}", file=sys.stderr)
            return 1
        # Re-resolve deck_path again here too, for the identical reason as
        # the re-resolve before the preview read above: the (possibly
        # arbitrarily long, per this function's own docstring) new-question/
        # new-answer prompts are a second window in which the file can be
        # renamed without tripping the collision check just above.
        deck_path = _find_deck_path(decks_dir, args.deck)
        try:
            existing_text = _read_deck_text(deck_path)
            new_text = edit_card(existing_text, question, new_question=new_question, new_answer=new_answer)
        except ParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _atomic_write_text(deck_path, new_text)
    note = ""
    # Compare normalized forms, not raw ones: edit_card() below normalizes
    # new_question before storing it (same as -q's own lookup normalization,
    # see the NFC/NFD tests above), so a --new-question that's merely a
    # different Unicode normalization form of the *same* text as the old
    # question produces the exact same stored question, hence the exact same
    # storage.card_id on the next sync -- no history is actually reset, so
    # this note must not claim otherwise. A raw comparison used to fire this
    # note in exactly that case, contradicting the card's real, unreset
    # review history.
    if new_question is not None and normalize_question(new_question.strip()) != question:
        note = (
            " (question changed -- this card's review history will reset on the next"
            " sync, since it's keyed on question text)"
        )
    print(f"edited in {deck_path} (run `flashback sync` to pick it up){note}")
    return 0


def _check_deck_filter(conn, deck):
    """Return an error message if `deck` doesn't match any deck the database
    currently knows about, else None.

    Only checked once the database has at least one deck at all — an empty
    database already gets its own "no decks yet" message from each command's
    existing check, and that's the more honest thing to say there than "no
    such deck." Otherwise a mistyped `--deck` has always silently matched
    zero rows and printed exactly what a caught-up deck prints, with no way
    to tell the two apart.

    Runs `_invalid_deck_name` first, before even checking the database: a
    `--deck` value with an unpaired Unicode surrogate isn't just "not a deck
    that exists" the way a plain typo is -- `known_decks()` and the
    `due_cards`/`deck_stats`/`hard_cards` queries these callers run next all
    bind `deck` as a raw SQL parameter, and sqlite3 has to encode it to UTF-8
    to do that, which raises `UnicodeEncodeError` unconditionally for one.
    On an empty database (no decks synced yet), the check below used to
    return None unconditionally -- there being no "known decks" to compare
    against -- so a surrogate-laden `--deck` sailed straight through to that
    crash. `main()`'s `UnicodeEncodeError` handler then caught it and blamed
    "the current terminal or output," suggesting a UTF-8 locale, which is
    simply wrong here: nothing was ever printed, the failure was in binding
    a SQL parameter, and no locale setting makes an unpaired surrogate
    valid. `add`/`remove`/`edit` already reject this (and control
    characters, bidi overrides, path separators, etc.) in their own `deck`
    argument via `_invalid_deck_name` -- no real deck could ever be named
    this, so rejecting it here up front, before any query runs, is both
    accurate and consistent with the rest of the CLI.
    """
    if deck is None:
        return None
    name_error = _invalid_deck_name(deck)
    if name_error is not None:
        return name_error
    known = known_decks(conn)
    if not known or deck in known:
        return None
    return f"no such deck: {deck!r}. known decks: {', '.join(known)}"


def _print_nothing_due(conn, today, deck):
    """The 'nothing due' message, plus when the next card actually comes back.

    Shared by `due` and `review` so the two can't drift: both are answering the
    same question, and "nothing due" on its own leaves the reader guessing
    whether to check again tomorrow or in a month.
    """
    print("nothing due. go outside.")
    next_due = next_due_date(conn, today, deck)
    if next_due is None:
        return
    days = (next_due - today).days
    when = "tomorrow" if days == 1 else f"in {days} days"
    print(f"next card is due {next_due.isoformat()} ({when}).")


def cmd_due(args):
    today = date.today()
    if args.deck is not None:
        args.deck = _normalize_deck_name(args.deck)
    with open_db(_db_path(args)) as conn:
        error = _check_deck_filter(conn, args.deck)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        rows = due_cards(conn, today, args.deck)
        if not rows:
            _print_nothing_due(conn, today, args.deck)
            return 0
    by_deck = {}
    for row in rows:
        by_deck[row["deck"]] = by_deck.get(row["deck"], 0) + 1
    for deck, count in sorted(by_deck.items()):
        print(f"{deck}: {count} due")
    return 0


def cmd_stats(args):
    today = date.today()
    if args.deck is not None:
        args.deck = _normalize_deck_name(args.deck)
    with open_db(_db_path(args)) as conn:
        error = _check_deck_filter(conn, args.deck)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        rows = deck_stats(conn, today, args.deck)
    if not rows:
        print("no decks yet. run `flashback sync` first.")
        return 0
    print(f"{'deck':<20} {'total':>6} {'due':>6} {'missed':>7}  next")
    for row in rows:
        print(
            f"{row['deck']:<20} {row['total']:>6} {row['due'] or 0:>6} "
            f"{row['missed'] or 0:>7}  {row['next_due'] or '-'}"
        )
    return 0


def _cards(count):
    return f"{count} card" if count == 1 else f"{count} cards"


def _print_hard_group(rows, limit, detail):
    """Print one group of hard cards, in `review`'s own [deck]/Q: shape.

    Truncation is announced rather than silent: a list of what you're bad at
    that quietly stops at ten would read as a complete answer when it isn't.
    """
    shown = rows[:limit] if limit > 0 else rows
    for row in shown:
        print(f"[{row['deck']}]")
        print(f"Q: {row['question']}")
        print(f"   {detail(row)}")
    hidden = len(rows) - len(shown)
    if hidden:
        print(f"... and {hidden} more (raise --limit to see them)")


def cmd_hard(args):
    today = date.today()
    if args.deck is not None:
        args.deck = _normalize_deck_name(args.deck)
    with open_db(_db_path(args)) as conn:
        # Read deck existence from `decks`, not `SELECT COUNT(*) FROM cards`:
        # a deck synced with zero cards has a `decks` row but no `cards`
        # rows, so counting `cards` alone can't tell "nothing synced yet"
        # from "the only synced deck happens to be card-less" — the same
        # distinction `stats`, `known_decks`, and `prune_missing_decks`
        # already draw correctly (session 97).
        no_decks_yet = not known_decks(conn)
        error = _check_deck_filter(conn, args.deck)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        rows = hard_cards(conn, args.deck)
    if no_decks_yet:
        print("no decks yet. run `flashback sync` first.")
        return 0
    if not rows:
        print("nothing looks hard yet -- no card's easiness has dropped below where")
        print("it started (`again`/`hard` move it down far more than `easy` moves")
        print("it back up, so it's not a simple tally of grades either way).")
        return 0

    # Two groups, not one ranked list. Easiness alone can't tell "missed this
    # morning" apart from "struggled with a month ago, fine now" — it barely
    # recovers once it's fallen — so a single hardest-first list would put a
    # card you've since mastered at the top. See storage.hard_cards.
    missed = [row for row in rows if row["currently_missed"]]
    recovering = [row for row in rows if not row["currently_missed"]]

    if missed:
        print(f"{_cards(len(missed))} you missed at your last review:\n")
        _print_hard_group(missed, args.limit, lambda row: _when_due(row["due_date"], today))
    if recovering:
        if missed:
            print()
        print(f"{_cards(len(recovering))} you've found hard before, but are getting right now:\n")
        _print_hard_group(
            recovering,
            args.limit,
            lambda row: f"{_streak(row['repetitions'])}; next review {row['due_date']}",
        )
    return 0


def _when_due(due_date, today):
    days = (date.fromisoformat(due_date) - today).days
    if days <= 0:
        return "due now"
    if days == 1:
        return "due tomorrow"
    return f"due {due_date}"


def _streak(count):
    """`repetitions` is the run of correct reviews since the last failed one."""
    if count == 1:
        return "correct at your last review"
    return f"correct at your last {count} reviews"


def cmd_review(args):
    today = date.today()
    if args.deck is not None:
        args.deck = _normalize_deck_name(args.deck)
    with open_db(_db_path(args)) as conn:
        error = _check_deck_filter(conn, args.deck)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        rows = due_cards(conn, today, args.deck)
        if not rows:
            _print_nothing_due(conn, today, args.deck)
            return 0

        print(f"{len(rows)} card(s) due. (again=1, hard=2, good=3, easy=4, q=quit)\n")
        reviewed = 0
        for row in rows:
            print(f"[{row['deck']}]")
            print(f"Q: {row['question']}")
            input("  (press enter to reveal answer) ")
            print(f"A: {row['answer']}")

            grade = None
            while grade is None:
                raw = input("  how did you do? [again/hard/good/easy/q] ").strip().lower()
                if raw in ("q", "quit"):
                    print(f"\nstopped after {reviewed} card(s).")
                    return 0
                grade = GRADE_KEYS.get(raw)
                if grade is None:
                    print("  please enter again, hard, good, easy, or q")

            due = record_review(conn, row, grade, today)
            # Commit each card immediately rather than relying on open_db's
            # single end-of-session commit: an interruption (EOFError from a
            # dropped stdin, KeyboardInterrupt, a closed terminal) partway
            # through a review skips that final commit entirely, which would
            # otherwise silently roll back every card graded earlier in the
            # same session too — even ones that already printed "next
            # review: ..." as if they were saved.
            conn.commit()
            if due is None:
                # Either the card was removed (e.g. by `remove` + `sync` in
                # another invocation) between being shown and being graded, or
                # another concurrent `review` session graded this same card
                # first (see record_review's optimistic-concurrency check) —
                # either way nothing from this grade was saved, so don't claim
                # a next-review date that never happened.
                print("  card changed or no longer exists elsewhere, skipped\n")
                continue
            print(f"  next review: {due.isoformat()}\n")
            reviewed += 1

        print(f"done. reviewed {reviewed} card(s).")
    return 0


def _non_negative_int(value: str) -> int:
    """argparse `type=` for `--limit`: reject negative counts instead of silently treating them as "show all".

    `_print_hard_group` only special-cases `limit > 0` versus everything
    else, so without this, `--limit -1` (a typo for a small positive number,
    or a mistaken guess that negative means "not limited") would fall
    through to the same "show every row" behavior as the documented `0`,
    with no error — a silent divergence between what was typed and what
    happened, for a flag whose whole job is to cap how much gets printed.
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be 0 or a positive integer, got {value!r}")
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or a positive integer, got {value!r}")
    return parsed


def _add_shared_dir_args(parser, *, top_level=False):
    """Add `--decks-dir`/`--state-dir` to `parser`.

    Every other option in this CLI (`-q`, `-a`, `--deck`, `--limit`) is typed
    after the subcommand, so these two need to work there as well, not just
    before it. `argparse.SUBParsersAction` parses the tokens after the
    subcommand into a fresh namespace and then copies *all* of its
    attributes onto the outer one, including untouched defaults — so if a
    subparser copy had its own ordinary default, typing `--decks-dir` only
    before the subcommand would get silently overwritten by the subparser's
    default the moment any subcommand ran. `argparse.SUPPRESS` keeps the
    attribute off the inner namespace entirely unless the user actually types
    the flag after the subcommand, so a value set before the subcommand
    survives untouched.
    """
    parser.add_argument(
        "--decks-dir",
        default="decks" if top_level else argparse.SUPPRESS,
        help="directory of *.md deck files (default: ./decks)",
    )
    parser.add_argument(
        "--state-dir",
        default=".flashback" if top_level else argparse.SUPPRESS,
        help="directory to store review state (default: ./.flashback)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="flashback", description="A plain-text, spaced-repetition flashcard tool."
    )
    parser.add_argument("--version", action="version", version=f"flashback {__version__}")
    _add_shared_dir_args(parser, top_level=True)

    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="load deck files into the review database")
    _add_shared_dir_args(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_add = sub.add_parser(
        "add", help="add a card to a deck file (creates it if it doesn't exist)"
    )
    p_add.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_add.add_argument("-q", "--question", help="the question (prompted for if omitted)")
    p_add.add_argument("-a", "--answer", help="the answer (prompted for if omitted)")
    _add_shared_dir_args(p_add)
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="remove a card from a deck file, by question")
    p_remove.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_remove.add_argument("-q", "--question", help="the question to remove (prompted for if omitted)")
    _add_shared_dir_args(p_remove)
    p_remove.set_defaults(func=cmd_remove)

    p_edit = sub.add_parser("edit", help="edit a card's question and/or answer in place")
    p_edit.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_edit.add_argument("-q", "--question", help="the question to edit (prompted for if omitted)")
    p_edit.add_argument("--new-question", help="replacement question text (kept as-is if omitted)")
    p_edit.add_argument("--new-answer", help="replacement answer text (kept as-is if omitted)")
    _add_shared_dir_args(p_edit)
    p_edit.set_defaults(func=cmd_edit)

    p_due = sub.add_parser("due", help="show how many cards are due, per deck")
    p_due.add_argument("--deck", help="limit to a single deck")
    _add_shared_dir_args(p_due)
    p_due.set_defaults(func=cmd_due)

    p_review = sub.add_parser("review", help="review due cards")
    p_review.add_argument("--deck", help="limit to a single deck")
    _add_shared_dir_args(p_review)
    p_review.set_defaults(func=cmd_review)

    p_stats = sub.add_parser("stats", help="show per-deck totals")
    p_stats.add_argument("--deck", help="limit to a single deck")
    _add_shared_dir_args(p_stats)
    p_stats.set_defaults(func=cmd_stats)

    p_hard = sub.add_parser("hard", help="show the cards you've found hardest")
    p_hard.add_argument("--deck", help="limit to a single deck")
    p_hard.add_argument(
        "--limit",
        type=_non_negative_int,
        default=10,
        help="most cards to show per group (default: 10; 0 for all)",
    )
    _add_shared_dir_args(p_hard)
    p_hard.set_defaults(func=cmd_hard)

    return parser


def _invalid_dir_arg(flag: str, value: str) -> Optional[str]:
    """Return an error message if `value` (a --decks-dir/--state-dir argument)
    contains an unpaired Unicode surrogate, control character, bidirectional-
    formatting character, Unicode line/paragraph separator, or Unicode "Tags"
    block character, else None.

    `_invalid_deck_name` and `_check_card_text` already reject these same kinds
    of characters in deck names and card text, for the reasons spelled out at
    length in both: `sys.argv` decodes anything that isn't valid UTF-8 with the
    `surrogateescape` error handler instead of raising, so a `--decks-dir`/
    `--state-dir` value built from non-UTF-8 bytes (a stray byte from a
    mismatched locale, mojibake pasted into a script, binary data passed by
    mistake) reaches this function silently, with nothing about the string
    itself signaling a problem yet -- and a control character or bidi override
    reaches it just as silently from a copy-pasted or scripted value.

    Unlike a deck name or card text, though, `--decks-dir`/`--state-dir` were
    never covered by either check -- they're ordinary filesystem paths, built
    with `Path(...)` and never routed through `_invalid_deck_name` or
    `_check_card_text` at all. Without the surrogate check, such a value sails
    straight through argument parsing and into real filesystem operations
    (`mkdir`, `glob`, opening the sqlite database, writing a deck file) that
    can succeed or partially succeed on plenty of filesystems even with a
    surrogate byte embedded in the path -- only the *next* thing that tries to
    print that same path (a command's own success message, an error message
    that echoes the path back) hits `UnicodeEncodeError` on stdout. `main`'s
    existing `UnicodeEncodeError` handler then blames "the current terminal or
    output" and suggests a UTF-8 locale or `PYTHONIOENCODING=utf-8` -- advice
    that cannot help here, since the underlying byte sequence was never valid
    Unicode text to begin with, and (worse) the command's actual file work may
    already have completed successfully by the time this misleading, exit-1
    error prints, leaving no way to tell from the output alone that anything
    actually worked.

    Without the control-character/bidi/line-separator checks, `--decks-dir`/
    `--state-dir` are printed raw (not `repr()`'d) in a comparable number of
    places `add`/`remove`/`edit`/`sync` already print a deck name in --
    `cmd_add`'s "added to {deck_path} ..." confirmation, `cmd_sync`'s
    "no such directory: {decks_dir}", `_read_deck_text`'s ParseError message,
    and more -- so a `--decks-dir` value containing an embedded ESC sequence
    or a Trojan-Source RLO/LRO override reaches the terminal exactly the way
    `_invalid_deck_name`'s own docstring describes for a deck name, just
    through a sibling argument that never got the same guard. Checking here,
    before `args.func` runs anything at all, gives a clean, accurate error at
    the point the bad value was supplied, instead of a false "your terminal's
    encoding is wrong" diagnosis after the fact (for a surrogate) or silent
    hidden/reordered output (for a control character or bidi override).

    Also missing until now: `_invalid_deck_name`'s Unicode "Tags" block check
    (`_is_unicode_tag_char`, U+E0000-U+E007F). Every code point in that block
    has no visible glyph in any conformant font, so it doesn't corrupt display
    the way a control character or bidi override does -- but it means two
    `--decks-dir`/`--state-dir` values that read as exactly the same path on
    screen (in a shell prompt, a script, this tool's own "no such directory:
    {decks_dir}"/"added to {deck_path} ..." messages) can silently be two
    different real paths, each with its own directory, deck files, and
    review database, underneath -- the identical "looks the same but isn't"
    failure `_invalid_deck_name` already exists to prevent for a deck name,
    just reached through a sibling argument that was never given the same
    check when this function was first added.

    Also missing until now: `_invalid_deck_name`'s U+FEFF (byte-order-mark,
    `ZERO_WIDTH_NO_BREAK_SPACE` in `parser.py`) check, for the identical
    reason -- invisible everywhere outside position zero of a file, so a
    `--decks-dir`/`--state-dir` value with one spliced into the middle reads
    identically to the same path without it, in every message this module
    prints, while pointing at a different real directory underneath.

    Also missing until now: `_invalid_deck_name`'s U+200B (zero-width space,
    `ZERO_WIDTH_SPACE` in `parser.py`) check, for the identical reason --
    invisible in every renderer, with no legitimate joining/shaping role the
    way ZWJ/ZWNJ have, so a `--decks-dir`/`--state-dir` value with one
    spliced in reads identically to the same path without it.
    """
    for ch in value:
        if unicodedata.category(ch) == "Cs":
            return (
                f"invalid {flag}: {value!r} (contains an unpaired Unicode surrogate "
                f"U+{ord(ch):04X}, which can't be encoded to UTF-8 or used as a real "
                "path at all -- this usually means invalid (non-UTF-8) byte data "
                "reached flashback as a command-line argument)"
            )
        if unicodedata.category(ch) == "Cc":
            return (
                f"invalid {flag}: {value!r} (contains a control character {ch!r}, "
                "which can hide or overwrite what's shown on screen)"
            )
        if unicodedata.bidirectional(ch) in BIDI_FORMATTING_CLASSES:
            return (
                f"invalid {flag}: {value!r} (contains a bidirectional-formatting "
                f"character U+{ord(ch):04X}, which can reorder how surrounding text "
                "is displayed on screen)"
            )
        if ch in LINE_SEPARATOR_CHARS:
            return (
                f"invalid {flag}: {value!r} (contains a Unicode line/paragraph "
                f"separator U+{ord(ch):04X}, which displays as a line break)"
            )
        if _is_unicode_tag_char(ch):
            return (
                f"invalid {flag}: {value!r} (contains a Unicode tag character "
                f"U+{ord(ch):04X}, which has no visible glyph in any font and can "
                "make two visually-identical paths actually differ)"
            )
        if ch == ZERO_WIDTH_NO_BREAK_SPACE:
            return (
                f"invalid {flag}: {value!r} (contains a byte-order-mark character "
                "U+FEFF, which is invisible and can make two visually-identical "
                "paths actually differ)"
            )
        if ch == ZERO_WIDTH_SPACE:
            return (
                f"invalid {flag}: {value!r} (contains a zero-width space U+200B, "
                "which is invisible and can make two visually-identical paths "
                "actually differ)"
            )
    return None


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    for flag, attr in (("--decks-dir", "decks_dir"), ("--state-dir", "state_dir")):
        # Both always exist on `args` by the time parsing finishes: the
        # top-level parser gives each a real default ("decks"/".flashback"),
        # and `_add_shared_dir_args`'s `argparse.SUPPRESS` default on every
        # subparser means a value set before the subcommand is never
        # overwritten by typing the subcommand -- see that helper's docstring.
        error = _invalid_dir_arg(flag, getattr(args, attr))
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    try:
        return args.func(args)
    except EOFError:
        print("\nerror: no more input.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nerror: interrupted.", file=sys.stderr)
        return 1
    except OSError as exc:
        # decks-dir and state-dir are both user-supplied and can point
        # somewhere unwritable (permissions, a file where a directory's
        # expected, a read-only mount) — without this, that surfaces as a
        # raw traceback instead of a one-line error. exc's own text already
        # names the offending path.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except UnicodeEncodeError as exc:
        # Card/deck text is checked for control characters and bidi
        # overrides before it's ever written (parser._check_card_text,
        # _invalid_deck_name) — but neither check (nor anything else in this
        # codebase) has any way to know what *encoding* stdout will actually
        # be using at print() time. That comes from the environment (locale,
        # PYTHONIOENCODING, a pipe or redirect into something that forces
        # ASCII) — a minimal container image or a plain "C"/"POSIX" locale
        # with no UTF-8 support are both common, real ways to end up here —
        # not from anything flashback controls. A perfectly ordinary
        # non-ASCII question, answer, or deck name (café is the running
        # example throughout this codebase's own docstrings) is exactly the
        # legitimate content this tool is designed to support, yet printing
        # it — in `add`'s own confirmation, `sync`, `due`, `stats`, `review`,
        # `hard`, or even an error message that echoes the offending text
        # back via repr() — crashed with a raw traceback instead of the
        # one-line message every other user-facing failure in this file
        # gets. `UnicodeEncodeError` is a `ValueError` subclass, not an
        # `OSError`, so the handler just above this one never caught it.
        # Like the sqlite3.Error case below, some of this command's output
        # may already be on the screen above this message — printing partway
        # through a multi-deck `sync`/`stats` run before hitting the one
        # deck whose name or content doesn't survive this stream's encoding
        # is a real, reachable sequence, not a hypothetical.
        # This message must itself be pure ASCII: it prints on the exact
        # stream that was just proven unable to encode something, so any
        # non-ASCII character here (an em dash, a curly quote) would trip
        # the identical UnicodeEncodeError right back, one frame up, with
        # nothing to catch it there.
        print(
            f"error: couldn't print card/deck text to the terminal ({exc}). "
            "this looks like an encoding limitation of the current terminal "
            "or output, not a problem with the content itself - try running "
            "with a UTF-8 locale (or PYTHONIOENCODING=utf-8).",
            file=sys.stderr,
        )
        return 1
    except sqlite3.Error as exc:
        # This handler wraps *all* of args.func(args), not just open_db's own
        # connect() — sync/review/hard all keep using the connection well
        # after opening it (per-deck/per-card commits, later SELECTs), so a
        # sqlite3.Error raised here doesn't mean opening the database failed;
        # it can just as easily be a `commit()` losing a lock-contention race
        # against another flashback process sharing this --state-dir (a real
        # "database is locked" OperationalError, reproduced by racing two
        # processes against the same state dir) after several decks/cards
        # were already saved and their success lines already printed. Saying
        # "couldn't open" here would flatly contradict output already on the
        # screen above it, so this is worded to hold regardless of when the
        # error actually struck. Unlike OSError, sqlite3's own message
        # doesn't include the path (e.g. "unable to open database file"), so
        # name it ourselves.
        print(f"error: problem with the review database in {args.state_dir!r}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
