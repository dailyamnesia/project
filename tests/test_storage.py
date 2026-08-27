import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from flashback.parser import parse_deck
from flashback.scheduler import DEFAULT_EASINESS, Grade
from flashback.storage import (
    SCHEMA,
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

    def _graded(self, conn, today, deck, text, grades):
        """Sync one deck and grade each card through a list of grades in order."""
        sync_deck(conn, deck, parse_deck(text), today)
        for question, sequence in grades.items():
            for grade in sequence:
                row = conn.execute(
                    "SELECT * FROM cards WHERE deck = ? AND question = ?", (deck, question)
                ).fetchone()
                record_review(conn, row, grade, today)

    def test_hard_cards_only_returns_cards_graded_down_from_the_default(self):
        # DEFAULT_EASINESS is where every card starts and only `again`/`hard`
        # move it down, so "below the default" is exactly "you got this wrong
        # or found it hard at some point" — not an arbitrary cutoff.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(
                conn,
                today,
                "d",
                "Q: missed\nA: 1\n---\nQ: fine\nA: 2\n---\nQ: never reviewed\nA: 3\n",
                {"missed": [Grade.AGAIN], "fine": [Grade.GOOD, Grade.EASY]},
            )
            questions = [row["question"] for row in hard_cards(conn)]
            self.assertEqual(questions, ["missed"])

    def test_hard_cards_includes_a_currently_missed_card_even_if_old_easy_streaks_kept_it_above_default(self):
        # hard_cards' own WHERE clause used to require easiness < DEFAULT_EASINESS
        # for *every* row, including ones flagged currently_missed — but easiness
        # only ever tracks the running total of every grade a card has ever
        # gotten, not just the most recent one. A long enough run of `easy`
        # grades (+0.1 each, no ceiling) can push a card's easiness high enough
        # that a single subsequent `again` (-0.8) still leaves it above where
        # every card starts, even though the card was, in plain language, just
        # missed. deck_stats' "missed" column has no such easiness condition
        # (just repetitions = 0 AND last_reviewed IS NOT NULL) and counts this
        # card; hard_cards used to silently drop it from its output entirely,
        # contradicting both deck_stats and its own "missed at your last
        # review" framing for the exact card that phrase describes.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(
                conn,
                today,
                "d",
                "Q: easy streak then missed\nA: 1\n",
                {"easy streak then missed": [Grade.EASY] * 9 + [Grade.AGAIN]},
            )
            row = conn.execute(
                "SELECT * FROM cards WHERE deck = ? AND question = ?",
                ("d", "easy streak then missed"),
            ).fetchone()
            self.assertEqual(row["repetitions"], 0)
            self.assertGreaterEqual(row["easiness"], DEFAULT_EASINESS)

            rows = hard_cards(conn)
            self.assertEqual([r["question"] for r in rows], ["easy streak then missed"])
            self.assertTrue(rows[0]["currently_missed"])

    def test_hard_cards_puts_a_currently_missed_card_above_an_older_harder_one(self):
        # The heart of it: easiness barely recovers (`good` adds nothing, `easy`
        # adds 0.1), so a card struggled with long ago and since mastered reads
        # exactly as low as one missed this morning. Ranking on easiness alone
        # would head the list with a card the learner has actually got.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(
                conn,
                today,
                "d",
                "Q: recovered\nA: 1\n---\nQ: still missing\nA: 2\n",
                {
                    # Floored at MIN_EASINESS, then four correct reviews running.
                    "recovered": [Grade.AGAIN, Grade.AGAIN, Grade.AGAIN]
                    + [Grade.GOOD] * 4,
                    # One `hard`: a *higher* easiness, but missed most recently.
                    "still missing": [Grade.HARD, Grade.AGAIN],
                },
            )
            rows = hard_cards(conn)
            self.assertEqual([row["question"] for row in rows], ["still missing", "recovered"])
            self.assertLess(rows[1]["easiness"], rows[0]["easiness"])
            self.assertTrue(rows[0]["currently_missed"])
            self.assertFalse(rows[1]["currently_missed"])

    def test_hard_cards_ranks_by_scheduler_confidence_not_stale_easiness(self):
        # `good` never moves easiness at all, so a card slipped once and then
        # gotten right five times running in a row still carries the same low
        # easiness as a card only ever graded `hard` twice — even though the
        # scheduler itself is far more confident about the first (a much
        # longer interval_days) than the second. Ranking on easiness alone
        # would put the mastered card above the one still being graded
        # `hard` every few days — the exact "confident wrong answer" failure
        # this function's docstring already warns easiness-only sorting
        # produces, just reached through interval_days instead of
        # currently_missed.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(
                conn,
                today,
                "d",
                "Q: mastered after one slip\nA: 1\n---\nQ: still hard right now\nA: 2\n",
                {
                    "mastered after one slip": [Grade.AGAIN] + [Grade.GOOD] * 5,
                    "still hard right now": [Grade.HARD, Grade.HARD],
                },
            )
            rows = hard_cards(conn)
            self.assertEqual(
                [row["question"] for row in rows],
                ["still hard right now", "mastered after one slip"],
            )
            self.assertLess(rows[0]["interval_days"], rows[1]["interval_days"])
            # The easiness ordering runs the *other* way — confirming this
            # is genuinely interval_days doing the work, not a coincidence.
            self.assertGreater(rows[0]["easiness"], rows[1]["easiness"])

    def test_hard_cards_breaks_ties_through_the_full_sort_chain(self):
        # Session 68's real usage found a genuine three-way tie on
        # interval_days that fell through easiness, then repetitions, then
        # finally question text — every level of the documented ORDER BY
        # actually exercised at once, live, none of it staged. No test had
        # ever driven a tie deep enough to reach the third or fourth key;
        # the existing tests each differ on the very first one
        # (interval_days) and never even reach easiness. Direct field
        # writes here (like test_next_due_date_respects_the_deck_filter's
        # own due_date UPDATE above) construct exact ties that grading
        # sequences alone can't reliably hit — the point is the ORDER BY
        # chain, not how a card arrived at these numbers.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(
                conn,
                today,
                "d",
                "Q: zulu\nA: 1\n---\nQ: yankee\nA: 2\n---\nQ: bravo\nA: 3\n---\nQ: alpha\nA: 4\n",
                {q: [Grade.AGAIN, Grade.GOOD] for q in ("zulu", "yankee", "bravo", "alpha")},
            )
            # All four tie on interval_days; zulu alone differs on easiness
            # (loses); yankee ties easiness with bravo/alpha but differs on
            # repetitions (loses); bravo and alpha tie on everything but
            # question text, where alpha sorts first.
            conn.execute(
                "UPDATE cards SET interval_days = 10, easiness = 1.5, repetitions = 5 "
                "WHERE question = 'zulu'"
            )
            conn.execute(
                "UPDATE cards SET interval_days = 10, easiness = 1.3, repetitions = 5 "
                "WHERE question = 'yankee'"
            )
            conn.execute(
                "UPDATE cards SET interval_days = 10, easiness = 1.3, repetitions = 2 "
                "WHERE question IN ('bravo', 'alpha')"
            )
            rows = hard_cards(conn)
            self.assertEqual(
                [row["question"] for row in rows], ["alpha", "bravo", "yankee", "zulu"]
            )

    def test_hard_cards_respects_the_deck_filter(self):
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            self._graded(conn, today, "geology", "Q: a\nA: 1\n", {"a": [Grade.AGAIN]})
            self._graded(conn, today, "spanish", "Q: b\nA: 2\n", {"b": [Grade.AGAIN]})
            self.assertEqual([r["question"] for r in hard_cards(conn, deck="geology")], ["a"])
            self.assertEqual([r["question"] for r in hard_cards(conn, deck="spanish")], ["b"])
            self.assertEqual(hard_cards(conn, deck="nonexistent"), [])

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

    def test_prune_missing_decks_does_not_delete_a_deck_synced_from_a_different_decks_dir(self):
        # Regression test: a review database shared (via a common --state-dir)
        # across more than one --decks-dir used to have no way to tell "this
        # deck's file is genuinely gone" apart from "this deck just isn't part
        # of the --decks-dir being synced right now". Syncing decks-dir B
        # (with no overlap in deck names with decks-dir A at all) treated every
        # deck ever synced from A as missing and deleted all of it, even
        # though A's files were never touched.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "spanish", parse_deck("Q: hello\nA: hola\n"), today, "/decks/A")
            # Syncing an unrelated decks-dir ("B") that has never heard of
            # "spanish" must not prune it: "spanish" simply isn't in scope for
            # this run, it isn't gone.
            sync_deck(conn, "french", parse_deck("Q: bonjour\nA: hi\n"), today, "/decks/B")
            pruned = prune_missing_decks(conn, {"french"}, "/decks/B")
            self.assertEqual(pruned, [])
            self.assertEqual({r["deck"] for r in due_cards(conn, today)}, {"spanish", "french"})

    def test_prune_missing_decks_still_prunes_within_the_same_decks_dir(self):
        # The fix above must not make pruning inert: a deck missing from a
        # sync of its *own* decks-dir is still genuinely gone.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "spanish", parse_deck("Q: hello\nA: hola\n"), today, "/decks/A")
            pruned = prune_missing_decks(conn, set(), "/decks/A")
            self.assertEqual(pruned, [("spanish", 1)])
            self.assertEqual(due_cards(conn, today), [])

    def test_prune_missing_decks_treats_a_null_decks_dir_as_matching_any(self):
        # A deck synced by a pre-upgrade version of this database (or by a
        # caller that never passed decks_dir) has no recorded decks_dir at
        # all. That must not be mistaken for "belongs to some other
        # directory" -- it has to keep behaving like it did before decks_dir
        # existed, so upgrading doesn't change pruning for the common case of
        # a single --decks-dir used consistently.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "spanish", parse_deck("Q: hello\nA: hola\n"), today)
            pruned = prune_missing_decks(conn, set(), "/decks/A")
            self.assertEqual(pruned, [("spanish", 1)])

    def test_known_decks_includes_a_deck_synced_with_zero_cards(self):
        # Regression test: a deck file that parses fine but has no cards in it
        # (a legitimately empty deck, not a mistake) used to leave no trace at
        # all in `cards`, so `known_decks` — which used to read
        # `SELECT DISTINCT deck FROM cards` — couldn't tell it apart from a
        # deck that was never synced. That made cli.py's `_check_deck_filter`
        # reject `--deck <that deck>` as "no such deck" even right after a
        # successful `sync` reported it by name.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "empty", [], today)
            sync_deck(conn, "full", parse_deck("Q: a\nA: 1\n"), today)
            self.assertEqual(known_decks(conn), ["empty", "full"])

    def test_prune_missing_decks_also_removes_a_zero_card_deck_whose_file_is_gone(self):
        # The old `SELECT DISTINCT deck FROM cards` candidate list couldn't see
        # a zero-card deck either, so deleting its file left it in
        # `known_decks` forever with no way to prune it back out.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "empty", [], today)
            pruned = prune_missing_decks(conn, set())
            self.assertEqual(pruned, [("empty", 0)])
            self.assertEqual(known_decks(conn), [])

    def test_a_database_with_cards_but_no_decks_table_rows_is_backfilled_on_open(self):
        # Regression test for an upgrade case the `decks` table itself
        # introduces: a database created before `decks` existed (or one
        # whose `cards` rows otherwise predate the table) has real cards
        # but no matching `decks` row, since only `sync_deck` ever wrote to
        # `decks` and it was never re-run. Without a backfill, `known_decks`
        # and `deck_stats` would falsely report "no decks yet" for a deck
        # that plainly has cards — inserted directly here, bypassing
        # `sync_deck`, to simulate exactly that pre-existing state.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO cards (id, deck, question, answer, repetitions,"
                " interval_days, easiness, due_date) VALUES (?, ?, ?, ?, 0, 1, 2.5, ?)",
                ("legacy0000000001", "legacy", "old question", "old answer", today.isoformat()),
            )
            conn.execute("DELETE FROM decks")
        with open_db(self.db_path) as conn:
            self.assertEqual(known_decks(conn), ["legacy"])
            rows = {row["deck"]: row for row in deck_stats(conn, today)}
            self.assertEqual(rows["legacy"]["total"], 1)

    def test_deck_stats_includes_a_zero_card_deck_with_zero_totals(self):
        # `stats`' whole job is per-deck totals; a deck with zero cards should
        # show up with 0s, not vanish from the table entirely as if it had
        # never been synced (the old `GROUP BY deck` over `cards` alone had no
        # row to group when a deck had no cards).
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "empty", [], today)
            sync_deck(conn, "full", parse_deck("Q: a\nA: 1\n"), today)
            rows = {row["deck"]: row for row in deck_stats(conn, today)}
            self.assertEqual(set(rows), {"empty", "full"})
            self.assertEqual(rows["empty"]["total"], 0)
            self.assertEqual(rows["empty"]["due"], 0)
            self.assertEqual(rows["empty"]["missed"], 0)
            self.assertIsNone(rows["empty"]["next_due"])

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
            conn.executescript(SCHEMA)

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

    def test_record_review_does_not_clobber_a_concurrent_review_of_the_same_card(self):
        # Two `review` sessions (two terminals, or two people sharing a state
        # dir) can both fetch the same due card before either grades it —
        # grading happens after a person reads the question and thinks about
        # it, an arbitrarily long window. If the second session's UPDATE were
        # keyed on `id` alone, it would silently overwrite the first session's
        # already-saved grade using the stale repetitions/interval_days/
        # easiness it read before the first session's write landed — the
        # first person's review would vanish with no error, even though
        # `review` told them it was saved.
        today = date(2026, 1, 1)
        with open_db(self.db_path) as conn:
            sync_deck(conn, "d", parse_deck("Q: a\nA: 1\n"), today)
            stale_row = due_cards(conn, today)[0]

            # Session A grades first and commits.
            due_a = record_review(conn, stale_row, Grade.EASY, today)
            self.assertIsNotNone(due_a)

            # Session B is still holding the *original* row it fetched before
            # session A's write — its UPDATE must not match now that the row
            # has moved on.
            due_b = record_review(conn, stale_row, Grade.AGAIN, today)
            self.assertIsNone(due_b)

            # The database reflects session A's grade (EASY), not session B's
            # stale-based one (AGAIN) clobbering it.
            saved = conn.execute(
                "SELECT repetitions, interval_days, easiness, due_date FROM cards WHERE id = ?",
                (stale_row["id"],),
            ).fetchone()
            self.assertEqual(saved["repetitions"], 1)
            self.assertEqual(saved["due_date"], due_a.isoformat())
            self.assertGreater(saved["easiness"], stale_row["easiness"])

    def test_open_db_seeds_a_gitignore_in_a_freshly_created_state_dir(self):
        # A user following the README into their own git-tracked decks
        # folder has no reason to know the review database needs its own
        # .gitignore entry — flashback should make that true on its own the
        # first time it creates the directory, not rely on the README to
        # tell them.
        fresh_state_dir = Path(self.tmpdir.name) / "fresh" / ".flashback"
        with open_db(fresh_state_dir / "state.sqlite3"):
            pass
        self.assertEqual((fresh_state_dir / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_open_db_does_not_touch_a_gitignore_in_a_preexisting_state_dir(self):
        # --state-dir is user-supplied; if it already pointed at something
        # before flashback ran, that's not a directory flashback created and
        # it shouldn't start dropping files into it that weren't there
        # before, however small.
        preexisting = Path(self.tmpdir.name)  # setUp's TemporaryDirectory, already exists
        db_path = preexisting / "state.sqlite3"
        with open_db(db_path):
            pass
        self.assertFalse((preexisting / ".gitignore").exists())

    def test_ensure_state_dir_does_not_overwrite_a_hand_edited_gitignore(self):
        state_dir = Path(self.tmpdir.name) / "fresh2"
        ensure_state_dir(state_dir)
        (state_dir / ".gitignore").write_text("*\n!keep-this.txt\n", encoding="utf-8")
        # A later call (e.g. the next `flashback sync`) must not clobber a
        # line the user added themselves.
        ensure_state_dir(state_dir)
        self.assertEqual(
            (state_dir / ".gitignore").read_text(encoding="utf-8"), "*\n!keep-this.txt\n"
        )


if __name__ == "__main__":
    unittest.main()
