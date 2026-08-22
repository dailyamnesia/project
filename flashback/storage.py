"""SQLite-backed storage for review state.

Card *content* lives in plain-text deck files (git-friendly, human
editable). This module only stores the review state (scheduling data,
history) that's specific to a reviewer and shouldn't live in git.
"""

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from .scheduler import DEFAULT_EASINESS, Grade, ReviewState, review as apply_review

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    deck TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    repetitions INTEGER NOT NULL DEFAULT 0,
    interval_days INTEGER NOT NULL DEFAULT 0,
    easiness REAL NOT NULL DEFAULT 2.5,
    due_date TEXT NOT NULL,
    last_reviewed TEXT
);
"""


def card_id(deck: str, question: str) -> str:
    # Deliberately scoped to (deck, question), not question alone: the same
    # question text in two different decks is treated as two independent
    # cards, each with its own schedule. Decks are the unit of context here
    # (e.g. "capital" could mean something different in a geography deck vs.
    # a trivia deck), so this is a feature, not a bug — unlike a duplicate
    # question *within* one deck, which parser.parse_deck rejects outright.
    digest = hashlib.sha1(f"{deck}\x00{question}".encode("utf-8")).hexdigest()
    return digest[:16]


def ensure_state_dir(state_dir: Path) -> Path:
    """Create `--state-dir` if it doesn't exist yet, and if this call is
    what created it, drop a `.gitignore` (`*`) inside so a user who puts
    their decks under git doesn't also commit their review database by
    accident — it's personal review state, not deck content, the same
    reasoning the README already gives for keeping it out of git.

    Only writes the `.gitignore` when *this* call created the directory.
    If it already existed, leave it alone — `--state-dir` is user-supplied
    and could point at a directory that predates flashback for some other
    reason (e.g. `--state-dir .`); silently blanket-ignoring an existing
    directory's contents would be a real footgun, not a convenience.
    """
    state_dir = Path(state_dir)
    is_new = not state_dir.exists()
    state_dir.mkdir(parents=True, exist_ok=True)
    if is_new:
        (state_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    return state_dir


@contextmanager
def open_db(db_path: Path):
    db_path = Path(db_path)
    ensure_state_dir(db_path.parent)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def sync_deck(conn, deck: str, cards, today: date):
    """Insert new cards from a parsed deck, refresh text for existing ones,
    and remove cards no longer present in the deck file.

    Returns (added, removed) counts.
    """
    seen_ids = set()
    added = 0
    for card in cards:
        cid = card_id(deck, card.question)
        seen_ids.add(cid)
        # INSERT OR IGNORE + rowcount, not a SELECT-then-INSERT: the latter is
        # a check-then-act race across processes (two concurrent `sync` runs
        # can both see "not found" and both try to INSERT the same new card),
        # which surfaced as a raw UNIQUE constraint crash under real concurrent
        # use. Losing this race just means the row already exists by the time
        # our statement runs, same as the pre-existing-card case below.
        cursor = conn.execute(
            "INSERT OR IGNORE INTO cards (id, deck, question, answer, due_date) VALUES (?, ?, ?, ?, ?)",
            (cid, deck, card.question, card.answer, today.isoformat()),
        )
        if cursor.rowcount:
            added += 1
        else:
            conn.execute("UPDATE cards SET answer = ? WHERE id = ?", (card.answer, cid))

    existing = conn.execute("SELECT id FROM cards WHERE deck = ?", (deck,)).fetchall()
    removed = 0
    for row in existing:
        if row["id"] not in seen_ids:
            conn.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
            removed += 1

    return added, removed


def due_cards(conn, today: date, deck: str = None):
    query = "SELECT * FROM cards WHERE due_date <= ?"
    params = [today.isoformat()]
    if deck is not None:
        query += " AND deck = ?"
        params.append(deck)
    query += " ORDER BY due_date ASC"
    return conn.execute(query, params).fetchall()


def next_due_date(conn, today: date, deck: str = None):
    """Earliest due date strictly after `today`, or None if nothing is scheduled later.

    `due_cards` answers "what can I review right now". This answers the question
    that follows immediately whenever that answer is "nothing": when to come back.
    Every card already carries its own `due_date`, so the tool has always known
    this — it just never had a way to say it.
    """
    query = "SELECT MIN(due_date) FROM cards WHERE due_date > ?"
    params = [today.isoformat()]
    if deck is not None:
        query += " AND deck = ?"
        params.append(deck)
    row = conn.execute(query, params).fetchone()
    value = row[0] if row is not None else None
    return date.fromisoformat(value) if value else None


def hard_cards(conn, deck: str = None):
    """Cards whose grading history has pushed easiness below its starting value.

    Every card starts at DEFAULT_EASINESS, and only `again` (-0.8) and `hard`
    (-0.14) move it down — `good` leaves it alone and `easy` adds 0.1. So this
    isn't an arbitrary cutoff: it's exactly "cards you have, on balance, gotten
    wrong or found hard at some point".

    Ordered worst-first, but "worst" deliberately leads with `currently_missed`
    rather than with easiness alone. Easiness only ever creeps back up (+0.1 at
    best, and never past MIN_EASINESS's floor in a hurry) — `good` leaves it
    untouched entirely — so a card graded `good` for months after one long-ago
    slip reads exactly as low as one missed this morning, forever: nothing
    about later `good` reviews ever moves the number that decides inclusion or
    rank.

    Within a group, `interval_days` — not easiness — breaks the tie: it's the
    scheduler's own current confidence, shrinking the moment a card is missed
    and growing every time it's confirmed since, so it tracks "still uncertain
    about this one" far better than a floor easiness can't leave once it's hit
    it. A card the scheduler already trusts for another decade sorts behind
    one it plans to re-check in a week, even if the decade-out card's easiness
    happens to read lower — a real case (session 68): one old slip followed by
    many `good` reviews leaves a card mastered by every measure but easiness
    outranking one still being graded `hard` every few days, because easiness
    alone can't tell "recovered long ago" from "still shaky right now".
    Easiness remains the second-level tiebreak for cards the scheduler is
    equally unsure about, and `repetitions` (resets to 0 on any failed recall,
    so it's the part of the stored state that tracks *lately*) breaks further
    ties.
    """
    query = """SELECT *, (repetitions = 0) AS currently_missed
               FROM cards
               WHERE easiness < ? AND last_reviewed IS NOT NULL"""
    params = [DEFAULT_EASINESS]
    if deck is not None:
        query += " AND deck = ?"
        params.append(deck)
    query += """ ORDER BY currently_missed DESC, interval_days ASC, easiness ASC,
                 repetitions ASC, question ASC"""
    return conn.execute(query, params).fetchall()


def record_review(conn, card_row, grade: Grade, today: date):
    """Apply a grade to `card_row` and persist the result; return the new due date.

    `card_row` was fetched earlier (by `due_cards`, at the start of a `review`
    session) and grading happens later, after the person reads the question,
    reveals the answer, and thinks about it — an interval with no upper bound
    on how long it can run. If a second `review` session (another terminal,
    another person sharing this state dir) grades the *same* card in that
    window, its UPDATE has already moved `repetitions`/`interval_days`/
    `easiness` on from what `card_row` remembers. Writing this review's result
    with a plain `WHERE id = ?` would still match — the row still exists — and
    would silently overwrite the other session's already-saved grade with one
    computed from stale numbers, while both sessions print a confident "next
    review: ..." as if each were the only one that happened. That's the same
    shape of lost update `_deck_lock` prevents for concurrent add/remove/edit,
    just one layer down, in the database rather than the file.

    The `WHERE` clause below also requires the three fields grading actually
    reads from (`repetitions`, `interval_days`, `easiness`) to still match what
    `card_row` saw; if another process already moved them, this UPDATE matches
    zero rows, same observable outcome as the card having been deleted
    entirely — the caller already treats a zero-rowcount UPDATE as "nothing to
    report as saved" for that reason.
    """
    state = ReviewState(
        repetitions=card_row["repetitions"],
        interval_days=card_row["interval_days"],
        easiness=card_row["easiness"],
    )
    new_state = apply_review(state, grade)
    due = today + timedelta(days=new_state.interval_days)
    cursor = conn.execute(
        """UPDATE cards
           SET repetitions = ?, interval_days = ?, easiness = ?, due_date = ?, last_reviewed = ?
           WHERE id = ? AND repetitions = ? AND interval_days = ? AND easiness = ?""",
        (
            new_state.repetitions,
            new_state.interval_days,
            new_state.easiness,
            due.isoformat(),
            today.isoformat(),
            card_row["id"],
            card_row["repetitions"],
            card_row["interval_days"],
            card_row["easiness"],
        ),
    )
    if cursor.rowcount == 0:
        return None
    return due


def prune_missing_decks(conn, existing_deck_names):
    """Delete every card whose deck isn't in `existing_deck_names`; return [(deck, count), ...].

    `sync_deck` only reconciles cards *within* a deck it's handed a file for — if a
    deck file is deleted outright, nothing ever calls `sync_deck` for it, so its
    cards would otherwise sit in the database forever: still due, still counted in
    `stats`, still shown by `review`, but unreachable from `remove`/`edit` since both
    require the deck file to exist. This is the whole-deck counterpart to the
    per-card removal `sync_deck` already does.
    """
    rows = conn.execute("SELECT DISTINCT deck FROM cards").fetchall()
    pruned = []
    for row in rows:
        deck = row["deck"]
        if deck in existing_deck_names:
            continue
        count = conn.execute("SELECT COUNT(*) FROM cards WHERE deck = ?", (deck,)).fetchone()[0]
        conn.execute("DELETE FROM cards WHERE deck = ?", (deck,))
        pruned.append((deck, count))
    return pruned


def deck_stats(conn, today: date):
    return conn.execute(
        """SELECT deck,
                  COUNT(*) AS total,
                  SUM(CASE WHEN due_date <= ? THEN 1 ELSE 0 END) AS due,
                  SUM(CASE WHEN repetitions = 0 AND last_reviewed IS NOT NULL
                           THEN 1 ELSE 0 END) AS missed,
                  MIN(CASE WHEN due_date > ? THEN due_date END) AS next_due
           FROM cards GROUP BY deck ORDER BY deck""",
        (today.isoformat(), today.isoformat()),
    ).fetchall()
