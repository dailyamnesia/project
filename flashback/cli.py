"""Command-line interface for flashback."""

import argparse
import os
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Optional

from . import __version__
from .parser import (
    BIDI_FORMATTING_CLASSES,
    LINE_SEPARATOR_CHARS,
    ParseError,
    _check_card_text,
    append_card,
    edit_card,
    normalize_question,
    parse_deck,
    remove_card,
)
from .scheduler import Grade
from .storage import (
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
    """
    if "/" in name or "\\" in name or name in (".", ".."):
        return f"invalid deck name: {name!r} (deck names can't contain a path separator)"
    if not name:
        return "invalid deck name: '' (deck name can't be empty)"
    for ch in name:
        if unicodedata.category(ch) == "Cc":
            return (
                f"invalid deck name: {name!r} (contains a control character {ch!r}, "
                "which can hide or overwrite what's shown on screen)"
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
    return None


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
    """
    tmp_path = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _deck_lock_path(args, deck: str) -> Path:
    return Path(args.state_dir) / "locks" / f"{deck}.lock"


@contextmanager
def _deck_lock(lock_path: Path):
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

    Locks a file under `--state-dir`, not a file living alongside the deck
    itself, using an OS-level advisory lock (`fcntl.flock`) rather than a
    lock file whose mere existence signals "locked": `flock` is released
    automatically when its file descriptor closes, including if the holding
    process is killed, so there's no stale lock to clean up by hand, and
    nothing new shows up next to the user's own deck files. POSIX-only, like
    the rest of this project has no separate Windows handling either; on
    Windows this is a no-op and the pre-existing race remains, no worse than
    before this fix.
    """
    if fcntl is None:
        yield
        return
    # add/remove/edit never touch the database, so this is the only mkdir
    # of `--state-dir` on their path — route it through the same helper
    # `open_db` uses so a `.gitignore` gets seeded here too, not just when
    # `sync`/`review`/etc. happen to run first.
    ensure_state_dir(lock_path.parent.parent)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """
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
        # Maps each deck_name already synced *this run* to the file that was
        # synced under it, so a second, colliding file (see the check below)
        # can name the file it lost to instead of just disappearing.
        synced_from = {}
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
                print(f"skipping {deck_file}: {name_error}", file=sys.stderr)
                continue
            if deck_name in synced_from:
                # Two *physically different* files (different bytes on disk,
                # confirmed distinct by decks_dir.glob returning both) can
                # each normalize to the same deck_name — e.g. one written
                # before NFC-normalization existed and one after, or one
                # just pasted from somewhere with a different composition.
                # sync_deck's own reconciliation ("delete any card of this
                # deck not in the file just handed to it") assumes it's the
                # only source for that deck name in this run; calling it
                # again under the same name would make it see the first
                # file's already-inserted cards as leftovers and silently
                # delete whichever aren't repeated in this second file —
                # real data loss, not merely a cosmetic double listing.
                # Skipping the second file avoids that; the first one synced
                # (by sort order) wins, and this file's cards are picked up
                # once the collision is resolved by renaming one of them.
                print(
                    f"skipping {deck_file}: deck name {deck_name!r} collides with "
                    f"{synced_from[deck_name]} — both normalize to the same name; "
                    "rename one of the files so they're distinct decks (or merge them "
                    "by hand) and sync again",
                    file=sys.stderr,
                )
                continue
            try:
                # utf-8-sig, not utf-8: strips a leading UTF-8 byte-order-mark if
                # present (common from Notepad and various editors/export tools)
                # instead of decoding it as a real character that then reads as
                # text before the file's first "Q:" line — see _read_deck_text.
                cards = parse_deck(deck_file.read_text(encoding="utf-8-sig"))
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
            added, removed = sync_deck(conn, deck_name, cards, today, decks_dir_key)
            # Commit each deck immediately rather than relying on open_db's
            # single end-of-session commit: an interruption partway through a
            # multi-deck sync (KeyboardInterrupt, a crash on a later deck)
            # skipped that final commit entirely, silently rolling back every
            # deck synced earlier in the same run too — even ones that had
            # already printed "N new, M removed" as if it were saved.
            conn.commit()
            synced_from[deck_name] = deck_file
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
    decks_dir.mkdir(parents=True, exist_ok=True)
    deck_path = decks_dir / f"{args.deck}.md"

    question = args.question if args.question is not None else input("Q: ")
    answer = args.answer if args.answer is not None else input("A: ")

    with _deck_lock(_deck_lock_path(args, args.deck)):
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
    deck_path = decks_dir / f"{args.deck}.md"
    if not deck_path.exists():
        print(f"no such deck: {deck_path}", file=sys.stderr)
        return 1

    question = args.question if args.question is not None else input("Q: ")

    with _deck_lock(_deck_lock_path(args, args.deck)):
        try:
            existing_text = _read_deck_text(deck_path)
            new_text = remove_card(existing_text, question)
        except ParseError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        _atomic_write_text(deck_path, new_text)
    print(
        f"removed from {deck_path} (run `flashback sync` to pick it up — "
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
    deck_path = decks_dir / f"{args.deck}.md"
    if not deck_path.exists():
        print(f"no such deck: {deck_path}", file=sys.stderr)
        return 1

    # normalize_question here mirrors edit_card()'s own normalization of this
    # same search key below — without it, a -q spelled in a different (but
    # visually identical) Unicode normalization form than what's stored would
    # fail this pre-lookup and error out before ever reaching edit_card(),
    # the same gap that used to exist here for surrounding whitespace.
    question = normalize_question((args.question if args.question is not None else input("Q: ")).strip())

    try:
        preview_text = _read_deck_text(deck_path)
        # validate=False: this is just a lookup to show the card's current
        # text before prompting — it shouldn't be blocked by some other,
        # unrelated card in the same deck failing _check_card_text.
        # edit_card() below still validates whatever new text is actually
        # written.
        match = next((c for c in parse_deck(preview_text, validate=False) if c.question == question), None)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if match is None:
        print(f"error: no card with that question found: {question!r}", file=sys.stderr)
        return 1

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
        print(f"current Q: {match.question}")
        new_question = input("new Q (blank to keep): ").strip() or None
        print(f"current A: {match.answer}")
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
    with _deck_lock(_deck_lock_path(args, args.deck)):
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
            " (question changed — this card's review history will reset on the next"
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
    """
    if deck is None:
        return None
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
        print("nothing looks hard yet — no card's easiness has dropped below where")
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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
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
