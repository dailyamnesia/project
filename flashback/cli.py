"""Command-line interface for flashback."""

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from . import __version__
from .parser import ParseError, append_card, edit_card, parse_deck, remove_card
from .scheduler import Grade
from .storage import deck_stats, due_cards, open_db, prune_missing_decks, record_review, sync_deck

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


def _invalid_deck_name(name: str) -> Optional[str]:
    """Return an error message if `name` can't be used as a deck file's stem, else None.

    A deck name with a path separator either escapes decks-dir silently (`../x`) or
    lands in a subdirectory `sync`'s non-recursive glob never looks at (`x/y`) — both
    look like they worked (a success message, a file on disk) but the card never
    becomes reachable through the tool again.
    """
    if "/" in name or "\\" in name or name in (".", ".."):
        return f"invalid deck name: {name!r} (deck names can't contain a path separator)"
    return None


def cmd_sync(args):
    decks_dir = Path(args.decks_dir)
    if not decks_dir.is_dir():
        print(f"no such directory: {decks_dir}", file=sys.stderr)
        return 1

    today = date.today()
    with open_db(_db_path(args)) as conn:
        total_added = total_removed = 0
        deck_names = set()
        for deck_file in sorted(decks_dir.glob("*.md")):
            deck_name = deck_file.stem
            deck_names.add(deck_name)
            try:
                cards = parse_deck(deck_file.read_text(encoding="utf-8"))
            except ParseError as exc:
                print(f"skipping {deck_file}: {exc}", file=sys.stderr)
                continue
            added, removed = sync_deck(conn, deck_name, cards, today)
            total_added += added
            total_removed += removed
            print(f"{deck_name}: {len(cards)} cards ({added} new, {removed} removed)")
        for deck_name, count in prune_missing_decks(conn, deck_names):
            total_removed += count
            print(f"{deck_name}: deck file no longer exists, removed {count} card(s)")
    print(f"synced. {total_added} new, {total_removed} removed total.")
    return 0


def cmd_add(args):
    error = _invalid_deck_name(args.deck)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    decks_dir = Path(args.decks_dir)
    decks_dir.mkdir(parents=True, exist_ok=True)
    deck_path = decks_dir / f"{args.deck}.md"

    question = args.question if args.question is not None else input("Q: ")
    answer = args.answer if args.answer is not None else input("A: ")

    existing_text = deck_path.read_text(encoding="utf-8") if deck_path.exists() else ""
    try:
        new_text = append_card(existing_text, question, answer)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    deck_path.write_text(new_text, encoding="utf-8")
    print(f"added to {deck_path} (run `flashback sync` to pick it up)")
    return 0


def cmd_remove(args):
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

    existing_text = deck_path.read_text(encoding="utf-8")
    try:
        new_text = remove_card(existing_text, question)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    deck_path.write_text(new_text, encoding="utf-8")
    print(
        f"removed from {deck_path} (run `flashback sync` to pick it up — "
        "this card's review history will be deleted on next sync)"
    )
    return 0


def cmd_edit(args):
    error = _invalid_deck_name(args.deck)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    decks_dir = Path(args.decks_dir)
    deck_path = decks_dir / f"{args.deck}.md"
    if not deck_path.exists():
        print(f"no such deck: {deck_path}", file=sys.stderr)
        return 1

    question = (args.question if args.question is not None else input("Q: ")).strip()

    existing_text = deck_path.read_text(encoding="utf-8")
    try:
        match = next((c for c in parse_deck(existing_text) if c.question == question), None)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if match is None:
        print(f"error: no card with that question found: {question!r}", file=sys.stderr)
        return 1

    new_question = args.new_question
    new_answer = args.new_answer
    if new_question is None and new_answer is None:
        print(f"current Q: {match.question}")
        new_question = input("new Q (blank to keep): ").strip() or None
        print(f"current A: {match.answer}")
        new_answer = input("new A (blank to keep): ").strip() or None
        if new_question is None and new_answer is None:
            print("nothing changed.")
            return 0

    try:
        new_text = edit_card(existing_text, question, new_question=new_question, new_answer=new_answer)
    except ParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    deck_path.write_text(new_text, encoding="utf-8")
    note = ""
    if new_question is not None and new_question.strip() != question:
        note = (
            " (question changed — this card's review history will reset on the next"
            " sync, since it's keyed on question text)"
        )
    print(f"edited in {deck_path} (run `flashback sync` to pick it up){note}")
    return 0


def cmd_due(args):
    today = date.today()
    with open_db(_db_path(args)) as conn:
        rows = due_cards(conn, today, args.deck)
    if not rows:
        print("nothing due. go outside.")
        return 0
    by_deck = {}
    for row in rows:
        by_deck[row["deck"]] = by_deck.get(row["deck"], 0) + 1
    for deck, count in sorted(by_deck.items()):
        print(f"{deck}: {count} due")
    return 0


def cmd_stats(args):
    today = date.today()
    with open_db(_db_path(args)) as conn:
        rows = deck_stats(conn, today)
    if not rows:
        print("no decks yet. run `flashback sync` first.")
        return 0
    print(f"{'deck':<20} {'total':>6} {'due':>6}")
    for row in rows:
        print(f"{row['deck']:<20} {row['total']:>6} {row['due'] or 0:>6}")
    return 0


def cmd_review(args):
    today = date.today()
    with open_db(_db_path(args)) as conn:
        rows = due_cards(conn, today, args.deck)
        if not rows:
            print("nothing due. go outside.")
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
            print(f"  next review: {due.isoformat()}\n")
            reviewed += 1

        print(f"done. reviewed {reviewed} card(s).")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="flashback", description="A plain-text, spaced-repetition flashcard tool."
    )
    parser.add_argument("--version", action="version", version=f"flashback {__version__}")
    parser.add_argument(
        "--decks-dir", default="decks", help="directory of *.md deck files (default: ./decks)"
    )
    parser.add_argument(
        "--state-dir",
        default=".flashback",
        help="directory to store review state (default: ./.flashback)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="load deck files into the review database")
    p_sync.set_defaults(func=cmd_sync)

    p_add = sub.add_parser(
        "add", help="add a card to a deck file (creates it if it doesn't exist)"
    )
    p_add.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_add.add_argument("-q", "--question", help="the question (prompted for if omitted)")
    p_add.add_argument("-a", "--answer", help="the answer (prompted for if omitted)")
    p_add.set_defaults(func=cmd_add)

    p_remove = sub.add_parser("remove", help="remove a card from a deck file, by question")
    p_remove.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_remove.add_argument("-q", "--question", help="the question to remove (prompted for if omitted)")
    p_remove.set_defaults(func=cmd_remove)

    p_edit = sub.add_parser("edit", help="edit a card's question and/or answer in place")
    p_edit.add_argument("deck", help="deck name (the deck file's stem, e.g. 'spanish-basics')")
    p_edit.add_argument("-q", "--question", help="the question to edit (prompted for if omitted)")
    p_edit.add_argument("--new-question", help="replacement question text (kept as-is if omitted)")
    p_edit.add_argument("--new-answer", help="replacement answer text (kept as-is if omitted)")
    p_edit.set_defaults(func=cmd_edit)

    p_due = sub.add_parser("due", help="show how many cards are due, per deck")
    p_due.add_argument("--deck", help="limit to a single deck")
    p_due.set_defaults(func=cmd_due)

    p_review = sub.add_parser("review", help="review due cards")
    p_review.add_argument("--deck", help="limit to a single deck")
    p_review.set_defaults(func=cmd_review)

    p_stats = sub.add_parser("stats", help="show per-deck totals")
    p_stats.set_defaults(func=cmd_stats)

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
        # Unlike OSError, sqlite3's own message doesn't include the path
        # (e.g. "unable to open database file"), so name it ourselves.
        print(f"error: couldn't open the review database in {args.state_dir!r}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
