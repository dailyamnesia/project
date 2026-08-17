"""SM-2 based spaced-repetition scheduling.

This is a simplified variant of the SuperMemo SM-2 algorithm: reviews are
graded on a 4-point scale (again/hard/good/easy) instead of SM-2's original
0-5 scale. The underlying easiness-factor and interval math is unchanged.
"""

from dataclasses import dataclass
from enum import Enum


class Grade(Enum):
    AGAIN = 0
    HARD = 3
    GOOD = 4
    EASY = 5


MIN_EASINESS = 1.3
DEFAULT_EASINESS = 2.5
# Easiness has no upper bound (only MIN_EASINESS floors it), so repeated EASY
# grades on the same card compound interval_days exponentially. Uncapped,
# enough consecutive EASY reviews push the resulting due date past what
# datetime can represent, and storage.py's `today + timedelta(days=...)`
# raises OverflowError. A ten-year interval is already far beyond anything
# spaced repetition needs to actually schedule.
MAX_INTERVAL_DAYS = 3650


@dataclass
class ReviewState:
    repetitions: int = 0
    interval_days: int = 0
    easiness: float = DEFAULT_EASINESS


def review(state: ReviewState, grade: Grade) -> ReviewState:
    """Return the next ReviewState after grading a card review."""
    q = grade.value

    easiness = state.easiness + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    easiness = max(easiness, MIN_EASINESS)

    if q < 3:
        # Failed recall: start the learning sequence over.
        repetitions = 0
        interval_days = 1
    else:
        repetitions = state.repetitions + 1
        if repetitions == 1:
            interval_days = 1
        elif repetitions == 2:
            interval_days = 6
        else:
            interval_days = min(round(state.interval_days * easiness), MAX_INTERVAL_DAYS)

    return ReviewState(
        repetitions=repetitions,
        interval_days=interval_days,
        easiness=easiness,
    )
