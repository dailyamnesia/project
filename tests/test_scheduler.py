import unittest
from datetime import date, timedelta

from flashback.scheduler import MAX_INTERVAL_DAYS, Grade, ReviewState, review


class TestScheduler(unittest.TestCase):
    def test_first_good_review_sets_interval_to_one_day(self):
        new_state = review(ReviewState(), Grade.GOOD)
        self.assertEqual(new_state.repetitions, 1)
        self.assertEqual(new_state.interval_days, 1)

    def test_second_good_review_sets_interval_to_six_days(self):
        state = ReviewState(repetitions=1, interval_days=1, easiness=2.5)
        new_state = review(state, Grade.GOOD)
        self.assertEqual(new_state.repetitions, 2)
        self.assertEqual(new_state.interval_days, 6)

    def test_third_good_review_multiplies_by_easiness(self):
        state = ReviewState(repetitions=2, interval_days=6, easiness=2.5)
        new_state = review(state, Grade.GOOD)
        self.assertEqual(new_state.repetitions, 3)
        self.assertEqual(new_state.interval_days, round(6 * new_state.easiness))

    def test_again_resets_repetitions_and_interval(self):
        state = ReviewState(repetitions=5, interval_days=40, easiness=2.8)
        new_state = review(state, Grade.AGAIN)
        self.assertEqual(new_state.repetitions, 0)
        self.assertEqual(new_state.interval_days, 1)

    def test_easiness_never_drops_below_minimum(self):
        state = ReviewState(repetitions=3, interval_days=10, easiness=1.3)
        for _ in range(10):
            state = review(state, Grade.AGAIN)
        self.assertGreaterEqual(state.easiness, 1.3)

    def test_easy_reviews_increase_easiness(self):
        state = ReviewState()
        new_state = review(state, Grade.EASY)
        self.assertGreater(new_state.easiness, state.easiness)

    def test_hard_reviews_decrease_easiness_but_still_succeed(self):
        state = ReviewState(repetitions=2, interval_days=6, easiness=2.5)
        new_state = review(state, Grade.HARD)
        self.assertLess(new_state.easiness, state.easiness)
        self.assertEqual(new_state.repetitions, 3)

    def test_repeated_easy_reviews_cap_interval_instead_of_growing_unbounded(self):
        # Easiness has no ceiling, so interval_days = round(interval * easiness)
        # compounds exponentially on a run of EASY grades. Uncapped, this
        # reaches an interval so large that `today + timedelta(days=...)`
        # overflows datetime.date's range a few dozen reviews in (session 52
        # found it in 14, starting from defaults). The cap must hold no
        # matter how long the streak runs, and the result must always be a
        # valid, addable date.
        state = ReviewState()
        for _ in range(200):
            state = review(state, Grade.EASY)
            self.assertLessEqual(state.interval_days, MAX_INTERVAL_DAYS)
        date.today() + timedelta(days=state.interval_days)


if __name__ == "__main__":
    unittest.main()
