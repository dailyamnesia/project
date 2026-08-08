"""Parse plain-text/markdown deck files into cards.

Deck file format:

    Q: What is the capital of France?
    A: Paris

    ---

    Q: What year did the French Revolution begin?
    A: 1789

Cards are separated by a line containing three or more dashes. Within a
card, everything after "Q:" up to the "A:" line is the question (so
questions can span multiple lines); everything after "A:" to the end of
the card is the answer.
"""

import re
from dataclasses import dataclass

CARD_SEPARATOR = re.compile(r"^-{3,}\s*$", re.MULTILINE)
Q_PREFIX = re.compile(r"^Q:\s?", re.IGNORECASE)
A_PREFIX = re.compile(r"^A:\s?", re.IGNORECASE)


@dataclass
class Card:
    question: str
    answer: str


class ParseError(ValueError):
    pass


def parse_deck(text: str) -> list[Card]:
    cards = []
    seen_questions = set()
    for block in CARD_SEPARATOR.split(text):
        block = block.strip()
        if not block:
            continue
        card = _parse_card(block)
        if card.question in seen_questions:
            raise ParseError(
                f"duplicate question in this deck: {card.question!r} — "
                "each card's question must be unique within a deck file, since "
                "review history is keyed on deck + question"
            )
        seen_questions.add(card.question)
        cards.append(card)
    return cards


def _parse_card(block: str) -> Card:
    question_lines = []
    answer_lines = []
    section = None

    for line in block.splitlines():
        if Q_PREFIX.match(line):
            section = "q"
            question_lines.append(Q_PREFIX.sub("", line, count=1))
        elif A_PREFIX.match(line):
            section = "a"
            answer_lines.append(A_PREFIX.sub("", line, count=1))
        elif section == "q":
            question_lines.append(line)
        elif section == "a":
            answer_lines.append(line)
        # lines before the first Q:/A: marker are ignored

    question = "\n".join(question_lines).strip()
    answer = "\n".join(answer_lines).strip()

    if not question:
        raise ParseError(f"card has no question:\n{block}")
    if not answer:
        raise ParseError(f"card has no answer for question: {question!r}")

    return Card(question=question, answer=answer)


def append_card(existing_text: str, question: str, answer: str) -> str:
    """Return deck file text with a new card appended.

    Adds a `---` separator before the new card if the file already has
    content, so this can be used both to create a deck file from scratch
    and to add a card to an existing one.
    """
    question = question.strip()
    answer = answer.strip()
    if not question:
        raise ParseError("question cannot be empty")
    if not answer:
        raise ParseError("answer cannot be empty")

    card_text = f"Q: {question}\nA: {answer}\n"
    existing = existing_text.rstrip()
    if not existing:
        return card_text
    return f"{existing}\n\n---\n\n{card_text}"
