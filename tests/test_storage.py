import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from flashback.parser import parse_deck
from flashback.scheduler import Grade
from flashback.storage import due_cards, open_db, record_review, sync_deck


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

    def test_record_review_pushes_due_date_forward(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            row = due_cards(conn, today)[0]
            due = record_review(conn, row, Grade.GOOD, today)
            self.assertEqual(due, today + timedelta(days=1))
            self.assertEqual(len(due_cards(conn, today)), 0)
            self.assertEqual(len(due_cards(conn, due)), 1)


if __name__ == "__main__":
    unittest.main()
