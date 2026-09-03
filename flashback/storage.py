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
CREATE TABLE IF NOT EXISTS decks (
    name TEXT PRIMARY KEY,
    decks_dir TEXT
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
        # executescript, not execute: SCHEMA now creates two tables
        # (`cards` and `decks`), and Connection.execute rejects more than
        # one statement at a time.
        conn.executescript(SCHEMA)
        # Migration for a database whose `decks` table predates the
        # `decks_dir` column (see sync_deck/prune_missing_decks for why it
        # exists): `CREATE TABLE IF NOT EXISTS` above is a no-op against an
        # already-existing `decks` table, so an ALTER TABLE is the only way
        # to get the column onto a database created before this column did.
        # Existing rows get NULL, same as a fresh row would if a caller ever
        # inserted without specifying it — see prune_missing_decks for how
        # NULL is treated (as "unknown", not as a specific directory).
        existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(decks)")}
        if "decks_dir" not in existing_columns:
            conn.execute("ALTER TABLE decks ADD COLUMN decks_dir TEXT")
        # Backfill for a database that already had cards before the `decks`
        # table existed: without this, a deck that's never been re-synced
        # since upgrading has real rows in `cards` but no row in `decks`,
        # so every command that now reads deck existence from `decks` alone
        # (known_decks, deck_stats) would falsely claim "no decks yet" even
        # though `cards` proves otherwise. INSERT OR IGNORE makes this a
        # no-op once a deck is already tracked, so it's harmless to run on
        # every open, not just once.
        conn.execute("INSERT OR IGNORE INTO decks (name) SELECT DISTINCT deck FROM cards")
        yield conn
        conn.commit()
    finally:
        conn.close()


class DeckDirMismatch(Exception):
    """Raised by sync_deck when `deck` is already recorded under a different,
    concrete `decks_dir` than the one calling now.

    See sync_deck's own docstring for the failure this exists to prevent:
    without it, a deck-name collision between two unrelated `--decks-dir`s
    that happen to share one `--state-dir` isn't just invisible to
    prune_missing_decks's pruning (which decks_dir already fixed) — it
    actively corrupts sync_deck's own reconciliation, deleting or
    overwriting the other directory's cards outright, every time either
    directory is synced.
    """

    def __init__(self, deck: str, recorded_decks_dir: str, attempted_decks_dir: str):
        self.deck = deck
        self.recorded_decks_dir = recorded_decks_dir
        self.attempted_decks_dir = attempted_decks_dir
        super().__init__(
            f"deck {deck!r} was last synced from {recorded_decks_dir!r}, not "
            f"{attempted_decks_dir!r}"
        )


def sync_deck(conn, deck: str, cards, today: date, decks_dir: str = None):
    """Insert new cards from a parsed deck, refresh text for existing ones,
    and remove cards no longer present in the deck file.

    Returns (added, removed) counts.

    Also records `deck` in the `decks` table unconditionally, even when
    `cards` is empty. Before this table existed, a deck's presence was
    inferred entirely from `SELECT DISTINCT deck FROM cards` — which works
    right up until a deck file with zero cards (a legitimately empty deck,
    not a mistake) gets synced. That deck then has no rows anywhere in
    `cards`, so it's indistinguishable from a deck that was never synced at
    all: `known_decks()` doesn't list it, so `_check_deck_filter` in cli.py
    rejects `due --deck <it>` / `stats --deck <it>` / `hard --deck <it>`
    with "no such deck" — the exact false "you mistyped" that
    `_check_deck_filter` exists to prevent, just triggered by a deck that's
    real but currently card-less instead of one that never existed.

    `decks_dir` (the resolved `--decks-dir` this call's `deck` was actually
    read from, as cmd_sync passes it) is stamped onto the deck's row every
    time it's synced, overwriting whatever was there before — see
    prune_missing_decks for why this matters. It isn't part of a deck's
    identity (that's still just `deck`, matching `card_id`'s own scoping),
    only a record of where this deck was last actually seen.

    Raises DeckDirMismatch, touching nothing, if `deck` already has a
    *different*, concrete recorded `decks_dir` than the one passed here.
    `prune_missing_decks` already refuses to prune a deck that belongs to a
    different `--decks-dir` sharing this `--state-dir` — but that guard only
    protects a deck whose file goes missing. An ordinary deck-name collision
    between two unrelated `--decks-dir`s (each with its own, real, unrelated
    "spanish.md", say) was never given the same protection here: this
    function reconciles purely by `deck` name, so syncing decks-dir B would
    silently delete every one of decks-dir A's cards for that name not
    present in B's file (real review history included) and splice in B's
    unrelated content instead — even though A's own file on disk was never
    touched. Only checked when *both* the recorded and the incoming
    decks_dir are concrete (non-NULL): a NULL recorded value (never synced
    under code that stamps one, or the caller didn't pass one) is treated
    the same "unknown, not a proven collision" way prune_missing_decks
    already treats it, so this can't itself brick an ordinary first real
    sync after an upgrade.
    """
    existing_row = conn.execute("SELECT decks_dir FROM decks WHERE name = ?", (deck,)).fetchone()
    if (
        existing_row is not None
        and existing_row["decks_dir"] is not None
        and decks_dir is not None
        and existing_row["decks_dir"] != decks_dir
    ):
        raise DeckDirMismatch(deck, existing_row["decks_dir"], decks_dir)

    conn.execute("INSERT OR IGNORE INTO decks (name, decks_dir) VALUES (?, ?)", (deck, decks_dir))
    conn.execute("UPDATE decks SET decks_dir = ? WHERE name = ?", (decks_dir, deck))
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
    """Cards whose grading history has pushed easiness below its starting value,
    plus any card that was missed outright at its last review.

    Every card starts at DEFAULT_EASINESS, and only `again` (-0.8) and `hard`
    (-0.14) move it down — `good` leaves it alone and `easy` adds 0.1. So
    "easiness below default" is a reasonable stand-in for "cards you have, on
    balance, gotten wrong or found hard at some point" — but only a stand-in:
    easiness has no ceiling, so a long enough run of `easy` grades can push a
    card's easiness so far above the default that a single subsequent `again`
    (-0.8) still leaves it above where every card started. Without the
    `OR repetitions = 0` below, a card in exactly that state — just missed,
    `currently_missed` would be true — fails the `easiness < DEFAULT_EASINESS`
    condition and is silently dropped from this function's output entirely,
    even though `deck_stats`' "missed" column (which only checks
    `repetitions = 0 AND last_reviewed IS NOT NULL`, no easiness condition at
    all) still counts it, and the card was, in plain language, just missed. A
    currently-missed card is unconditionally included here for that reason;
    easiness alone still gates the second, "recovering" group below.

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
               WHERE last_reviewed IS NOT NULL AND (easiness < ? OR repetitions = 0)"""
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


def prune_missing_decks(conn, existing_deck_names, decks_dir: str = None):
    """Delete every card whose deck isn't in `existing_deck_names`; return [(deck, count), ...].

    `sync_deck` only reconciles cards *within* a deck it's handed a file for — if a
    deck file is deleted outright, nothing ever calls `sync_deck` for it, so its
    cards would otherwise sit in the database forever: still due, still counted in
    `stats`, still shown by `review`, but unreachable from `remove`/`edit` since both
    require the deck file to exist. This is the whole-deck counterpart to the
    per-card removal `sync_deck` already does.

    Enumerates candidates from the `decks` table, not `SELECT DISTINCT deck FROM
    cards`: a deck synced with zero cards has a `decks` row but no `cards` rows at
    all, so reading candidates from `cards` alone would never see it — its deleted
    file would leave a phantom `decks` entry behind forever, the same "database
    remembers something the filesystem no longer has" problem this function exists
    to fix for cards, just one layer up. `count` is still whatever `cards` had for
    that deck (zero for a deck that was already card-less).

    `decks_dir` is the resolved `--decks-dir` this run's `existing_deck_names` was
    actually built from (cmd_sync's own glob). A deck absent from
    `existing_deck_names` only proves its file is gone from *that* directory — not
    that the deck is gone at all, if the review database is shared (via a common
    `--state-dir`) across more than one `--decks-dir`. Without this check, syncing
    decks-dir B — even one with no deck-name overlap with decks-dir A at all — read
    as "every deck A has ever synced here is now missing" and deleted all of their
    cards, printing "deck file no longer exists" for files that were sitting on
    disk in decks-dir A, untouched, the whole time: a real, silent loss of another
    directory's review history, triggered by nothing more unusual than one `sync`
    run against the wrong (or simply a different) `--decks-dir`. A deck whose last
    recorded `decks_dir` doesn't match this run's is therefore left alone — it's
    simply out of scope for this run, not gone; only a sync of its own decks_dir
    can actually confirm that.

    A NULL `decks_dir` (a deck synced by a version of this database from before
    this column existed) requires an exact match too, i.e. is *never* pruned here
    until it's been seen by a real sync under the current code and had a concrete
    `decks_dir` stamped onto it. Treating NULL as an automatic match (an earlier
    version of this function did) reintroduces the exact bug this whole function
    exists to fix, for anyone upgrading from before this column existed: right
    after the `ALTER TABLE` migration, *every* pre-existing deck's `decks_dir` is
    NULL, indistinguishable from each other regardless of which `--decks-dir` each
    one actually came from — so the first post-upgrade sync of any one
    `--decks-dir` sharing that `--state-dir` would treat every other decks-dir's
    still-NULL decks as "belongs to me, and it's missing" and delete them, the
    identical cross-directory data loss the `decks_dir` column was added to
    prevent, just delayed until the moment a pre-existing database crosses the
    upgrade instead of requiring one. Erring toward "leave a NULL-`decks_dir` deck
    alone" costs a genuinely-deleted deck (file removed *before* ever being
    re-synced under a version of the code that records `decks_dir`) a pruning it
    would otherwise get automatically — it stays visible until synced again from
    its real original directory (which stamps a concrete value and makes it
    provably prunable from then on) — a strictly smaller, recoverable annoyance
    next to silently destroying another directory's review history outright.
    """
    rows = conn.execute("SELECT name, decks_dir FROM decks").fetchall()
    pruned = []
    for row in rows:
        deck = row["name"]
        if deck in existing_deck_names:
            continue
        if row["decks_dir"] != decks_dir:
            continue
        count = conn.execute("SELECT COUNT(*) FROM cards WHERE deck = ?", (deck,)).fetchone()[0]
        conn.execute("DELETE FROM cards WHERE deck = ?", (deck,))
        conn.execute("DELETE FROM decks WHERE name = ?", (deck,))
        pruned.append((deck, count))
    return pruned


def deck_stats(conn, today: date, deck: str = None):
    """Per-deck totals: total/due/missed counts plus the next future due date.

    LEFT JOINs `cards` onto `decks` rather than just grouping `cards` directly
    (deck's old approach) so a deck synced with zero cards still gets its own
    row here — "show per-deck totals" ought to include a real, currently-empty
    deck showing zero of everything, the same way `stats` already shows a
    fully caught-up deck as "0 due" rather than omitting it. Grouping `cards`
    alone silently dropped a card-less deck's row entirely, which then read,
    from `stats` output alone, as if the deck didn't exist — indistinguishable
    from a typo'd `--deck`, right up until `_check_deck_filter` correctly
    recognized the name and let the query through to prove it.
    """
    query = """SELECT decks.name AS deck,
                  COUNT(cards.id) AS total,
                  SUM(CASE WHEN cards.due_date <= ? THEN 1 ELSE 0 END) AS due,
                  SUM(CASE WHEN cards.repetitions = 0 AND cards.last_reviewed IS NOT NULL
                           THEN 1 ELSE 0 END) AS missed,
                  MIN(CASE WHEN cards.due_date > ? THEN cards.due_date END) AS next_due
           FROM decks
           LEFT JOIN cards ON cards.deck = decks.name"""
    params = [today.isoformat(), today.isoformat()]
    if deck is not None:
        query += " WHERE decks.name = ?"
        params.append(deck)
    query += " GROUP BY decks.name ORDER BY decks.name"
    return conn.execute(query, params).fetchall()


def known_decks(conn):
    """Every deck name that's currently synced, whether or not it has any cards.

    `due`/`review`/`hard`/`stats` filter by `deck` at the SQL level, so a typo'd
    `--deck` value has always silently matched zero rows and printed exactly
    what a caught-up deck prints — no way to tell "you're done" from "you
    mistyped." This is what a `--deck` argument is checked against.

    Reads from the `decks` table, not `SELECT DISTINCT deck FROM cards`: the
    latter can't see a deck synced with zero cards (a legitimately empty deck
    file has no rows in `cards` at all), so a real `--deck <empty deck>` used
    to fail this check with the same "no such deck" error as an actual typo.
    """
    return [row[0] for row in conn.execute("SELECT name FROM decks ORDER BY name")]
