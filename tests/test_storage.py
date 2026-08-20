import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from flashback.parser import parse_deck
from flashback.scheduler import Grade
from flashback.storage import (
    SCHEMA,
    due_cards,
    next_due_date,
    open_db,
    prune_missing_decks,
    record_review,
    sync_deck,
)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "state.sqlite3"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_sync_adds_new_cards_as_due_today(self):
        cards = parse_deck("Q: hi\nA: hello\n")
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            added, removed = sync_deck(conn, "greetings", cards, today)
            self.assertEqual((added, removed), (1, 0))
            self.assertEqual(len(due_cards(conn, today)), 1)

    def test_sync_removes_deleted_cards(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n---\nQ: b\nA: 2\n"), today)
            added, removed = sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            self.assertEqual((added, removed), (0, 1))
            self.assertEqual(len(due_cards(conn, today)), 1)

    def test_resync_preserves_progress_and_updates_answer(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            row = due_cards(conn, today)[0]
            record_review(conn, row, Grade.GOOD, today)

            sync_deck(conn, "d", parse_deck("Q: a\nA: one\n"), today)
            later = today + timedelta(days=1)
            row = due_cards(conn, later)[0]
            self.assertEqual(row["answer"], "one")
            self.assertEqual(row["repetitions"], 1)

    def test_same_question_in_different_decks_are_independent_cards(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "geography", parse_deck("Q: capital?\nA: Paris\n"), today)
            sync_deck(conn, "trivia", parse_deck("Q: capital?\nA: a large sum, informally\n"), today)
            rows = due_cards(conn, today)
            self.assertEqual(len(rows), 2)
            self.assertEqual({r["deck"] for r in rows}, {"geography", "trivia"})
            self.assertEqual({r["id"] for r in rows}, {rows[0]["id"], rows[1]["id"]})
            self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_due_cards_with_empty_string_deck_filter_matches_nothing(self):
        # `if deck:` (the pre-fix check) treats "" the same as not passing --deck
        # at all, since an empty string is falsy in Python — so a caller asking
        # for one specific (if unusual) deck named "" would silently see every
        # deck's due cards instead of correctly seeing none. `deck is not None`
        # tells "no filter" (None) apart from "filter to this exact deck" ("").
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "geography", parse_deck("Q: capital?\nA: Paris\n"), today)
            self.assertEqual(len(due_cards(conn, today, deck="")), 0)
            self.assertEqual(len(due_cards(conn, today, deck=None)), 1)

    def test_next_due_date_returns_the_earliest_future_due_date(self):
        # The whole point is answering "when should I come back?" — so it has to
        # skip anything already due (that's `due_cards`' job) and pick the
        # soonest card still ahead of today, not just any future card.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n---\nQ: b\nA: 2\n---\nQ: c\nA: 3\n"), today)
            rows = {r["question"]: r for r in due_cards(conn, today)}
            conn.execute("UPDATE cards SET due_date = ? WHERE id = ?", ("2026-01-20", rows["a"]["id"]))
            conn.execute("UPDATE cards SET due_date = ? WHERE id = ?", ("2026-01-05", rows["b"]["id"]))
            # "c" stays due today, so it must not be what gets reported.
            self.assertEqual(next_due_date(conn, today), date(2026, 1, 5))

    def test_next_due_date_is_none_when_nothing_is_scheduled_later(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self.assertIsNone(next_due_date(conn, today))
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            # One card, due today — there is no *next* date to report yet.
            self.assertIsNone(next_due_date(conn, today))

    def test_next_due_date_respects_the_deck_filter(self):
        # `due --deck geology` reporting spanish's next date would be worse than
        # saying nothing: it's a confident answer to a question nobody asked.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "geology", parse_deck("Q: a\nA: 1\n"), today)
            sync_deck(conn, "spanish", parse_deck("Q: b\nA: 2\n"), today)
            conn.execute("UPDATE cards SET due_date = ? WHERE deck = ?", ("2026-01-30", "geology"))
            conn.execute("UPDATE cards SET due_date = ? WHERE deck = ?", ("2026-01-02", "spanish"))
            self.assertEqual(next_due_date(conn, today, deck="geology"), date(2026, 1, 30))
            self.assertEqual(next_due_date(conn, today, deck="spanish"), date(2026, 1, 2))
            self.assertIsNone(next_due_date(conn, today, deck="nonexistent"))

    def test_prune_missing_decks_removes_cards_for_a_deck_no_longer_present(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "geography", parse_deck("Q: capital?\nA: Paris\n"), today)
            sync_deck(conn, "trivia", parse_deck("Q: capital?\nA: a large sum\n"), today)
            pruned = prune_missing_decks(conn, {"geography"})
            self.assertEqual(pruned, [("trivia", 1)])
            rows = due_cards(conn, today)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["deck"], "geography")

    def test_prune_missing_decks_leaves_present_decks_untouched(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            pruned = prune_missing_decks(conn, {"d"})
            self.assertEqual(pruned, [])
            self.assertEqual(len(due_cards(conn, today)), 1)

    def test_concurrent_sync_of_the_same_new_cards_does_not_crash(self):
        """Regression test for a real race, found by running `sync` from
        several terminals against the same state file at once: two
        connections can both see a brand-new card as absent and both try
        to add it. The old SELECT-then-INSERT wasn't atomic across
        connections, so the second writer crashed with
        sqlite3.IntegrityError once the first had committed. sync_deck now
        uses INSERT OR IGNORE, so the loser of the race falls through to
        the update path instead of raising.

        Needs many decks and many threads together to reliably land two
        writers on the same card's INSERT at once — a single card and a
        handful of threads passed even against the old, buggy code."""
        today = date(2026, 1, 1)
        decks = {f"deck{i}": parse_deck(f"Q: q{i}?\nA: a{i}\n") for i in range(30)}
        errors = []
        barrier = threading.Barrier(15)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(SCHEMA)

        def worker():
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                barrier.wait()
                for name, cards in decks.items():
                    sync_deck(conn, name, cards, today)
                conn.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        with open_db(self.db_path) as conn:
            self.assertEqual(len(due_cards(conn, today)), len(decks))

    def test_record_review_pushes_due_date_forward(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            row = due_cards(conn, today)[0]
            due = record_review(conn, row, Grade.GOOD, today)
            self.assertEqual(due, today + timedelta(days=1))
            self.assertEqual(len(due_cards(conn, today)), 0)
            self.assertEqual(len(due_cards(conn, due)), 1)

    def test_record_review_on_already_deleted_card_returns_none(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            row = due_cards(conn, today)[0]
            conn.execute("DELETE FROM cards WHERE id = ?", (row["id"],))
            due = record_review(conn, row, Grade.GOOD, today)
            self.assertIsNone(due)


if __name__ == "__main__":
    unittest.main()
