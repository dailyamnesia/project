import unittest

from flashback.scheduler import Grade, ReviewState, review


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


if __name__ == "__main__":
    unittest.main()
