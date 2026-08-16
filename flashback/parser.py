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
import unicodedata
from dataclasses import dataclass

CARD_SEPARATOR = re.compile(r"^-{3,}\s*$", re.MULTILINE)
Q_PREFIX = re.compile(r"^Q:\s?", re.IGNORECASE)
A_PREFIX = re.compile(r"^A:\s?", re.IGNORECASE)

# Explicit bidirectional-formatting characters (Unicode's Bidi_Class values
# for the embedding/override/isolate controls, not the full Cf category —
# see _check_card_text for why the distinction matters).
BIDI_FORMATTING_CLASSES = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)


@dataclass
class Card:
    question: str
    answer: str


class ParseError(ValueError):
    pass


def parse_deck(text: str, *, validate: bool = True) -> list[Card]:
    """Parse deck file text into cards.

    `validate=False` skips `_check_card_text` on every card (duplicate-question
    detection still runs either way — that's a structural correctness check, not
    a dangerous-content one). Used internally by `append_card`/`remove_card`/
    `edit_card`, which only need to *locate* card(s) among the others, not re-vet
    every unrelated card's content on each call — otherwise one poisoned card
    (see the docstring on `_check_card_text`) would block adding, removing, or
    editing any other, unrelated card in the same deck. `sync` and any other real
    read of a deck file's content should keep the default `validate=True`.
    """
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
        if validate:
            # Deck files are meant to be hand-edited directly, not only written
            # through `add`/`edit` — so this check has to run here too, not just
            # in append_card. Without it, a control character or bidi-override
            # typed straight into a deck file sails through `sync` untouched and
            # only surfaces later, raw, when `review` prints it to the terminal:
            # exactly the scenario this check exists to prevent, just reached by
            # a different door. (The dash-separator and Q:/A:-prefix checks in
            # _check_card_text are effectively no-ops here, since a real
            # occurrence of either would already have split or reread the block
            # differently above — only the character-level checks can still fire
            # on text that's already been parsed.)
            _check_card_text(card.question, card.answer)
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


def _check_card_text(question: str, answer: str) -> None:
    """Raise ParseError if question/answer text would be misread as a structural marker,
    or would manipulate the terminal when the card is displayed.

    A line of three-or-more dashes reads back as a card separator, splitting
    one card into two (or more) on the next parse. A line starting with
    `Q:`/`A:` reads back as a new section marker, silently merging/splitting
    the card's actual content. Both write cleanly with no error at add-time
    and only misbehave later, on the next sync — so catch them here, before
    anything touches disk, same as the empty-question/answer checks above.

    Separately: `review` and `edit` print question/answer text straight to
    the terminal. A control character (most notably ESC, the start of an
    ANSI/OSC escape sequence) in that text isn't a parsing problem — it
    parses and displays "fine" — but it lets card content hide or overwrite
    what's shown, which defeats the point of a flashcard. Newline and tab
    are legitimate content (multi-line answers already rely on newlines) and
    stay allowed; every other control character is rejected.

    A related but distinct case: Unicode's explicit bidirectional-formatting
    characters (RLO/LRO and friends — the "Trojan Source" family, the same
    mechanism used to disguise malicious filenames as harmless ones) aren't
    control characters at all, so the check above doesn't catch them, but
    they can still reorder how the rest of the line displays — e.g. making
    "evil<RLO>txt.exe" print as "evilexe.txt". Rejecting the whole Cf
    ("format") category would also reject legitimate content — RTL marks,
    Arabic letter marks, and the variation selectors/ZWJ sequences emoji
    rely on are all Cf too — so this checks Unicode's narrower
    Bidi_Class property instead, which isolates just the
    embedding/override/isolate controls responsible for reordering.
    """
    for field_name, text in (("question", question), ("answer", answer)):
        for line in text.splitlines():
            if CARD_SEPARATOR.fullmatch(line):
                raise ParseError(
                    f"{field_name} contains a line of three or more dashes ({line!r}), which "
                    "flashback reads as a card separator — this would silently split the card "
                    "in two on the next sync"
                )
            if Q_PREFIX.match(line) or A_PREFIX.match(line):
                raise ParseError(
                    f"{field_name} contains a line starting with 'Q:' or 'A:' ({line!r}), which "
                    "flashback reads as the start of a new question/answer — this would "
                    "silently corrupt the card's content on the next sync"
                )
        for ch in text:
            if ch in ("\n", "\t"):
                continue
            if unicodedata.category(ch) == "Cc":
                raise ParseError(
                    f"{field_name} contains a control character ({ch!r}), which can hide or "
                    "overwrite what's shown on screen when the card is displayed — not allowed "
                    "in card text"
                )
            if unicodedata.bidirectional(ch) in BIDI_FORMATTING_CLASSES:
                raise ParseError(
                    f"{field_name} contains a bidirectional-formatting character (U+"
                    f"{ord(ch):04X}), which can reorder how surrounding text is displayed on "
                    "screen — not allowed in card text"
                )


def _format_card(question: str, answer: str) -> str:
    return f"Q: {question}\nA: {answer}\n"


def _append_block(existing_text: str, card_text: str) -> str:
    existing = existing_text.rstrip()
    if not existing:
        return card_text
    return f"{existing}\n\n---\n\n{card_text}"


def _render_deck(cards: list[Card]) -> str:
    """Render cards back to deck file text without re-validating their content.

    Used by `remove_card`/`edit_card` to rebuild a deck's text after locating
    a target card — the cards being carried over unchanged already round-tripped
    through the file once, so re-checking them here would only serve to block
    the operation on some other, unrelated poisoned card (see `parse_deck`'s
    `validate` parameter).
    """
    text = ""
    for card in cards:
        text = _append_block(text, _format_card(card.question, card.answer))
    return text


def append_card(existing_text: str, question: str, answer: str) -> str:
    """Return deck file text with a new card appended.

    Adds a `---` separator before the new card if the file already has
    content, so this can be used both to create a deck file from scratch
    and to add a card to an existing one. Raises ParseError if a card with
    the same question already exists in this deck — without this check,
    `add` would silently create a duplicate that then blocks `sync` for
    the whole deck (parse_deck's own duplicate check, further down this
    file, runs unconditionally on every read).

    Parses `existing_text` with `validate=False`: adding a new card
    shouldn't be blocked by some other, unrelated card in the same deck
    failing `_check_card_text` — same reasoning as `remove_card`/`edit_card`.
    """
    question = question.strip()
    answer = answer.strip()
    if not question:
        raise ParseError("question cannot be empty")
    if not answer:
        raise ParseError("answer cannot be empty")
    _check_card_text(question, answer)

    existing_cards = parse_deck(existing_text, validate=False)
    if any(card.question == question for card in existing_cards):
        raise ParseError(
            f"a card with this question already exists in this deck: {question!r}"
        )

    return _append_block(existing_text, _format_card(question, answer))


def remove_card(existing_text: str, question: str) -> str:
    """Return deck file text with the card matching `question` removed.

    Matching is exact after stripping, same as the comparison `card_id`
    normalizes on. Raises ParseError if no card matches — the caller (or a
    person hand-editing the file) got the question text wrong, and silently
    doing nothing would be worse than saying so.

    Parses with `validate=False`: removing one card shouldn't be blocked by
    some other, unrelated card in the same deck failing `_check_card_text`.
    """
    question = question.strip()
    cards = parse_deck(existing_text, validate=False)
    remaining = [card for card in cards if card.question != question]
    if len(remaining) == len(cards):
        raise ParseError(f"no card with that question found: {question!r}")

    return _render_deck(remaining)


def edit_card(
    existing_text: str, question: str, new_question: str | None = None, new_answer: str | None = None
) -> str:
    """Return deck file text with the card matching `question` updated in place.

    Unlike `remove_card` + `append_card`, this preserves the card's position
    in the file. At least one of `new_question`/`new_answer` must be given;
    the other field is left as-is. Raises ParseError if no card matches, if
    the resulting question/answer would be empty, or if a new question
    collides with another card already in the deck.

    Note for callers: changing the question changes what `storage.card_id`
    is keyed on, so (like remove + add) it resets that card's review
    history on the next sync. Changing only the answer does not — the
    card's id is unaffected, so its schedule carries over.

    Parses with `validate=False` and instead runs `_check_card_text` only on
    the new question/answer text being written: editing one card shouldn't be
    blocked by some other, unrelated card in the same deck failing that check,
    but the new content this call actually introduces still has to pass it.
    """
    if new_question is None and new_answer is None:
        raise ParseError("must provide a new question and/or a new answer to edit")

    question = question.strip()
    cards = parse_deck(existing_text, validate=False)

    updated = []
    found = False
    for card in cards:
        if card.question == question:
            found = True
            q = new_question.strip() if new_question is not None else card.question
            a = new_answer.strip() if new_answer is not None else card.answer
            if not q:
                raise ParseError("question cannot be empty")
            if not a:
                raise ParseError("answer cannot be empty")
            _check_card_text(q, a)
            updated.append(Card(question=q, answer=a))
        else:
            updated.append(card)
    if not found:
        raise ParseError(f"no card with that question found: {question!r}")

    seen = set()
    for card in updated:
        if card.question in seen:
            raise ParseError(
                f"duplicate question in this deck: {card.question!r} — "
                "each card's question must be unique within a deck file"
            )
        seen.add(card.question)

    return _render_deck(updated)
