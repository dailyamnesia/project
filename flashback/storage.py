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

from .scheduler import Grade, ReviewState, review as apply_review

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
    digest = hashlib.sha1(f"{deck}\x00{question}".encode("utf-8")).hexdigest()
    return digest[:16]


@contextmanager
def open_db(db_path: Path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
        row = conn.execute("SELECT id FROM cards WHERE id = ?", (cid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO cards (id, deck, question, answer, due_date) VALUES (?, ?, ?, ?, ?)",
                (cid, deck, card.question, card.answer, today.isoformat()),
            )
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
    if deck:
        query += " AND deck = ?"
        params.append(deck)
    query += " ORDER BY due_date ASC"
    return conn.execute(query, params).fetchall()


def record_review(conn, card_row, grade: Grade, today: date):
    state = ReviewState(
        repetitions=card_row["repetitions"],
        interval_days=card_row["interval_days"],
        easiness=card_row["easiness"],
    )
    new_state = apply_review(state, grade)
    due = today + timedelta(days=new_state.interval_days)
    conn.execute(
        """UPDATE cards
           SET repetitions = ?, interval_days = ?, easiness = ?, due_date = ?, last_reviewed = ?
           WHERE id = ?""",
        (
            new_state.repetitions,
            new_state.interval_days,
            new_state.easiness,
            due.isoformat(),
            today.isoformat(),
            card_row["id"],
        ),
    )
    return due


def deck_stats(conn, today: date):
    return conn.execute(
        """SELECT deck,
                  COUNT(*) AS total,
                  SUM(CASE WHEN due_date <= ? THEN 1 ELSE 0 END) AS due
           FROM cards GROUP BY deck ORDER BY deck""",
        (today.isoformat(),),
    ).fetchall()
