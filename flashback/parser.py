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
from typing import Optional

CARD_SEPARATOR = re.compile(r"^-{3,}\s*$", re.MULTILINE)
Q_PREFIX = re.compile(r"^Q:\s?", re.IGNORECASE)
A_PREFIX = re.compile(r"^A:\s?", re.IGNORECASE)

# Explicit bidirectional-formatting characters (Unicode's Bidi_Class values
# for the embedding/override/isolate controls, not the full Cf category —
# see _check_card_text for why the distinction matters).
BIDI_FORMATTING_CLASSES = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)

# Unicode's own LINE SEPARATOR (U+2028) and PARAGRAPH SEPARATOR (U+2029).
# Neither is in the Cc ("control") category — unicodedata.category() reports
# them as Zl/Zp — so the control-character check in _check_card_text doesn't
# catch them. But str.splitlines(), which _parse_card (and the line-based
# checks just below) both use to find line boundaries, treats them exactly
# like a real "\n". See _check_card_text for the consequence.
LINE_SEPARATOR_CHARS = frozenset({" ", " "})

# Unicode's "Tags" block. Originally meant for invisible language tagging, a
# use Unicode itself deprecated -- every code point in this block is defined
# with no visible glyph in any conformant font, so in modern practice the
# block is used almost exclusively to smuggle an entirely invisible
# secondary message inside text that otherwise looks perfectly ordinary on
# screen (the mechanism behind "ASCII smuggling"/hidden-prompt payloads).
# Like the bidi-override characters above, these aren't in the Cc category
# (so the control-character check doesn't catch them), and they're category
# Cf, same as the ZWJ/variation selectors legitimate emoji rely on (so a
# blanket Cf rejection would be wrong here too, exactly as explained above
# for bidi) -- but unlike a bidi override, which only *reorders* visible
# characters, a tag character hides content outright, with zero trace in
# what's actually displayed. See _check_card_text for the consequence.
UNICODE_TAG_RANGE = (0xE0000, 0xE007F)


def _is_unicode_tag_char(ch: str) -> bool:
    return UNICODE_TAG_RANGE[0] <= ord(ch) <= UNICODE_TAG_RANGE[1]


# U+FEFF, ZERO WIDTH NO-BREAK SPACE -- better known by its other job, the
# UTF-8/UTF-16 byte-order mark. `_read_deck_text` already strips one of these
# when it's the very first character of a file (via "utf-8-sig"), the one
# place it has a real, legitimate purpose. Anywhere else in a question,
# answer, or deck name it has none: it renders as nothing in every modern
# renderer (older ones that don't recognize it show a stray glyph, but never
# the same thing twice, and never anything a person would type on purpose),
# so unlike a bidi override or a Tags-block character (both category Cf, same
# as this), there's no legitimate-content trade-off in rejecting it the way
# there is for those two (bidi: real RTL text; Tags: nothing rejected here at
# all, the block was already vestigial) -- U+FEFF has no ordinary use as
# *content* anywhere outside position zero of a file. Left unchecked, it's
# invisible in exactly the same way a Tags-block character is: two questions
# that print identically on screen -- one with a stray BOM concatenated into
# it (pasted from another UTF-8 file, or two files joined by a script) and
# one without -- compare unequal as plain text, so `remove`/`edit`'s
# exact-match lookup reports "no card with that question found" for a
# question that's sitting right there, unchanged from the "looks the same
# but isn't" failure shape every other check in this module closes.
ZERO_WIDTH_NO_BREAK_SPACE = "﻿"

# U+200B, ZERO WIDTH SPACE. Also category Cf, also invisible in every modern
# renderer -- but unlike ZWJ/ZWNJ (U+200D/U+200C, deliberately left allowed
# just below) or the bidi/RTL controls, it has no legitimate rendering job
# left to protect: it doesn't join or separate glyphs, doesn't change how
# anything displays, and isn't part of any emoji or script-shaping sequence.
# In modern practice it shows up almost exclusively as a copy-paste artifact
# (many web pages insert it as an invisible "wrap hint") or deliberately, to
# smuggle invisible content -- the identical role U+FEFF and the Tags block
# already have checks for. Left unchecked, it's the same "looks the same,
# isn't" gap those two close: a question with one spliced in prints
# identically to the same question without it, yet compares unequal as text,
# so `remove`/`edit`'s exact-match lookup reports "no card with that
# question found" for a card that's genuinely sitting right there.
ZERO_WIDTH_SPACE = "​"


def normalize_question(question: str) -> str:
    """Normalize a question to NFC so it compares equal regardless of how its
    accented/composed characters happen to be encoded.

    Unicode allows some characters two equally valid encodings — e.g. "é" as
    one precomposed codepoint (NFC) or as "e" plus a combining acute accent
    (NFD) — that render identically and are indistinguishable to anyone
    reading the deck file, but compare unequal as plain Python strings.
    Without this, two "differently-typed" spellings of the same question
    could slip past parse_deck's duplicate check as if they were different
    cards, hash to different storage.card_id values (so they'd schedule and
    review as two unrelated cards despite looking like one), and make
    remove/edit's exact-match lookup report "no card with that question
    found" for a question that reads, on screen, exactly like one that's
    really there — the same "looks the same but silently isn't" failure
    shape the whitespace-stripping and the control-character/bidi-override
    checks on this same field already exist to close.

    Applied everywhere a question becomes (or is looked up as) a card's
    identity: when a deck file is parsed (`_parse_card`) and whenever a
    caller-supplied question is used to add, remove, or edit a specific card
    (`append_card`, `remove_card`, `edit_card`) — so a parsed card's
    `.question` and a freshly normalized search key always compare equal
    when they're the same text, regardless of which normalization form
    either one started out in.
    """
    return unicodedata.normalize("NFC", question)


@dataclass
class Card:
    question: str
    answer: str


class ParseError(ValueError):
    pass


def parse_deck(text: str, *, validate: bool = True) -> list[Card]:
    """Parse deck file text into cards.

    `validate=False` also skips the duplicate-question check below, not just
    `_check_card_text`. Both used to be described as separable (duplicate
    detection framed as a "structural correctness check" that should always
    run), but that turned out to be exactly the same "one poisoned card blocks
    every other, unrelated card" failure shape `_check_card_text` is skipped
    here to prevent -- just for a deck-wide *pair* of cards instead of one
    card's own content. A hand-edited (or merge-conflicted) deck file that
    picks up two cards sharing a question is a real, reachable state -- and
    with duplicate detection unconditional, it used to permanently lock
    `add`/`remove`/`edit` out of touching *any* other, unrelated card in that
    deck too, since all three locate their target by calling this function
    first. The only way out was hand-editing the file directly, defeating
    the entire point of `add`/`remove`/`edit` existing as an alternative to
    that.

    Used internally by `append_card`/`remove_card`/`edit_card`, which only
    need to *locate* card(s) among the others, not re-vet every unrelated
    card's content -- or the whole deck's uniqueness -- on each call. Each of
    those three still separately guards against the specific new duplicate
    *it* could introduce (see their own docstrings) -- this function skipping
    the check doesn't weaken that, it just stops it from firing on some
    *other*, untouched pair this call was never asked about. `sync` and any
    other real read of a deck file's content should keep the default
    `validate=True`, which still refuses to load a deck with a real
    duplicate anywhere in it, exactly as before.
    """
    cards = []
    seen_questions = set()
    for block in CARD_SEPARATOR.split(text):
        block = block.strip()
        if not block:
            continue
        card = _parse_card(block)
        if validate and card.question in seen_questions:
            raise ParseError(
                f"duplicate question in this deck: {card.question!r} -- "
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
            if section == "a":
                # A second 'Q:' line after this block's answer has already
                # started isn't a continuation of anything — a real card has
                # exactly one Q:-to-A: transition. Without this check, two
                # whole cards typed one after another but missing the '---'
                # separator between them (an easy hand-editing slip, and
                # exactly what a script or LLM generating deck text is prone
                # to) silently parse as a *single* card whose "question" is
                # the two questions joined by a newline and whose "answer" is
                # the two answers joined the same way — no error, and no
                # trace of it in sync's success output, the same "silently
                # corrupts" failure shape `_check_card_text` exists to catch
                # for content that arrives through add/edit instead of a
                # hand-edited file. By the time a merged block like that
                # reaches `_check_card_text`, the literal 'Q:'/'A:' prefixes
                # that would have tipped it off are already stripped by this
                # loop, so that check alone can't catch it — this has to be
                # caught here, while the prefixes are still visible.
                raise ParseError(
                    "card has a second 'Q:' line after its answer already started "
                    f"({line!r}) -- this looks like two cards run together because a "
                    f"'---' separator is missing between them:\n{block}"
                )
            if section == "q":
                # A second 'Q:' line while still *inside* the question — not
                # after an 'A:' has started — is the narrower sibling of the
                # check just above. A genuine multi-line question whose second
                # (or later) physical line happens to start with the literal
                # text "Q:" (e.g. a card about the flashback format itself, or
                # any question that legitimately continues with a line reading
                # "Q: ...") matched this same prefix, and — since `section` was
                # already "q", not "a" — used to fall through with no error at
                # all: this loop treated it as an ordinary continuation of the
                # question, but still ran it through `Q_PREFIX.sub`, silently
                # deleting that line's leading "Q:" from the stored text. The
                # question read back one "Q:" shorter on every future parse,
                # with nothing in sync's success output to hint at it — and
                # unlike the after-'A:' case above, `_check_card_text` can't
                # catch this one on its own pass either: the evidence (the
                # leading "Q:") is already gone from the final joined text by
                # the time that check runs, not just relocated where it can
                # still be spotted. `add`/`edit` already reject this content
                # up front for exactly this reason (see
                # test_new_question_with_embedded_q_prefix_line_raises) — a
                # hand-edited deck file reaching `parse_deck` deserves the same
                # protection.
                raise ParseError(
                    "card has a second 'Q:' line while its question is still being "
                    f"read ({line!r}) -- if this is meant to be part of the question "
                    "text rather than a new question, break up the line (e.g. a "
                    f"leading space) so it doesn't start with 'Q:':\n{block}"
                )
            section = "q"
            question_lines.append(Q_PREFIX.sub("", line, count=1))
        elif A_PREFIX.match(line):
            if section == "a":
                # Same shape, for the answer's own marker: a second 'A:' line
                # while the answer is already being read silently lost its
                # leading "A:" the same way a repeated 'Q:' line did above —
                # e.g. an answer that legitimately continues with a line
                # reading "A: ..." (documenting the format itself, say).
                raise ParseError(
                    "card has a second 'A:' line while its answer is still being "
                    f"read ({line!r}) -- if this is meant to be part of the answer "
                    "text rather than a new answer, break up the line (e.g. a "
                    f"leading space) so it doesn't start with 'A:':\n{block}"
                )
            section = "a"
            answer_lines.append(A_PREFIX.sub("", line, count=1))
        elif section == "q":
            question_lines.append(line)
        elif section == "a":
            answer_lines.append(line)
        elif line.strip():
            # A non-blank line before the first Q:/A: marker used to be
            # silently discarded here with no error and no trace in sync's
            # success output — the exact "silently corrupts" failure shape
            # this module's own docstring says add/edit exist to prevent,
            # just reached through a different door (a hand-edited deck
            # file with a stray line above a card's "Q:", not inside one).
            # `parse_deck` always strips each block before calling this, so
            # a genuinely blank line can never reach here — only real,
            # about-to-be-lost content can.
            raise ParseError(
                f"card has text before its first 'Q:' line, which would be silently "
                f"discarded ({line!r}):\n{block}"
            )

    question = normalize_question("\n".join(question_lines).strip())
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

    A third, similarly distinct case: Unicode's own LINE SEPARATOR (U+2028)
    and PARAGRAPH SEPARATOR (U+2029) aren't control characters either (so
    the Cc check doesn't catch them) and don't reorder anything (so the Bidi
    check doesn't either) — but every place this module finds line
    boundaries (`_parse_card`, and the loop just above this one) does so
    with `str.splitlines()`, which treats U+2028/U+2029 exactly like a real
    "\n". A question or answer containing one therefore parses fine and
    writes cleanly here, then silently reads back differently on the very
    next parse — split into an extra "line" that becomes part of the stored
    question/answer via a real newline the card never actually had — the
    same "looks the same, isn't" gap the NFC-normalization check on
    questions elsewhere in this module exists to close, just for line
    boundaries instead of accented characters. Copy-pasting text from a word
    processor or PDF (common sources of U+2028 line breaks) is enough to hit
    this without typing anything unusual.

    A fourth case, categorically different from the three above: an unpaired
    UTF-16 surrogate (U+D800-U+DFFF, Unicode category "Cs") isn't a real
    character at all -- it's not valid content, so it can't be hidden,
    reordered, or misread as a structural marker the way the checks above
    guard against. What it actually does is worse: it can never be encoded to
    UTF-8 (or any real interchange encoding) at all, `str.encode` raises
    `UnicodeEncodeError` on one unconditionally. A Python `str` can hold one
    anyway without complaint, because `sys.argv` (and, on POSIX, `input()`)
    decode raw bytes with the `surrogateescape` error handler, which maps any
    byte that isn't valid UTF-8 to exactly this kind of surrogate rather than
    raising -- so a `-q`/`-a`/deck-name argument built from non-UTF-8 bytes
    (a stray byte from a mismatched locale, a copy-paste of mojibake, binary
    data passed by mistake) reaches this function silently, with nothing
    about the string itself signaling a problem yet. Without this check, that
    content sails through here and through `append_card`/`edit_card`'s own
    validation, only to blow up in `_atomic_write_text`'s `encode="utf-8"`
    write -- an exception that then surfaces through `main`'s
    `UnicodeEncodeError` handler, which assumes any such crash is a
    terminal-output-encoding problem and tells the user to try
    `PYTHONIOENCODING=utf-8` or a UTF-8 locale. That advice cannot help here:
    the failure is in *writing the deck file*, not printing to a terminal,
    and no locale or encoding setting makes an unpaired surrogate valid --
    the underlying byte sequence it came from was never valid Unicode text to
    begin with. Catching it here instead gives a clean, accurate ParseError
    at the point the bad content was actually supplied.

    A fifth case: Unicode's "Tags" block (U+E0000-U+E007F, see
    UNICODE_TAG_RANGE) isn't a control character (so the Cc check doesn't
    catch it) and doesn't reorder anything (so the Bidi check doesn't
    either), but it's worse than either: every code point in the block has
    no visible glyph in any conformant font, so text built from it rides
    along completely invisibly inside a question or answer that still looks
    perfectly ordinary on screen -- an entire hidden secondary message with
    zero trace in what `review`/`edit` actually display, and (since it's
    real, distinct text, not a rendering trick) enough to make what looks
    like the exact same question `remove`/`edit` were given fail to match a
    card that's genuinely sitting right there, the same "looks the same,
    isn't" gap `normalize_question` closes for differently-normalized
    accents, just via invisible extra characters instead of a different
    encoding of the same visible ones.

    A sixth case: U+FEFF (see ZERO_WIDTH_NO_BREAK_SPACE), better known as the
    UTF-8/UTF-16 byte-order mark. `_read_deck_text` already strips one when
    it's literally the first character of a file -- its one legitimate job --
    but that guard says nothing about one appearing *inside* a question or
    answer (e.g. two files concatenated by a script, or text pasted from
    partway through a second BOM-prefixed file). Same category (Cf) as the
    bidi controls and the Tags block above, so neither of those checks catch
    it either, and the identical "looks the same, isn't" consequence as the
    Tags-block case: it's invisible in every renderer, so a question typed
    (or pasted) with a stray BOM in the middle reads on screen exactly like
    the same question without one, yet compares unequal as text -- the exact
    gap that makes `remove`/`edit`'s exact-match lookup report "no card with
    that question found" for a card that's genuinely right there.

    A seventh case: U+200B, ZERO WIDTH SPACE (see ZERO_WIDTH_SPACE). Also
    category Cf, so none of the checks above catch it either -- but unlike
    ZWJ/ZWNJ (U+200D/U+200C, deliberately left allowed, see
    test_emoji_sequence_with_zero_width_joiner_is_fine) or the bidi/RTL
    controls, it has no legitimate content to protect: it doesn't join or
    separate glyphs and isn't part of any emoji or script-shaping sequence,
    so unlike those, rejecting it costs nothing. It's invisible in every
    modern renderer, the identical "looks the same, isn't" consequence as
    U+FEFF just above -- a question with one spliced in (a common copy-paste
    artifact from web pages that use it as an invisible wrap hint) reads on
    screen exactly like the same question without it, yet compares unequal
    as text.
    """
    for field_name, text in (("question", question), ("answer", answer)):
        for line in text.splitlines():
            if CARD_SEPARATOR.fullmatch(line):
                raise ParseError(
                    f"{field_name} contains a line of three or more dashes ({line!r}), which "
                    "flashback reads as a card separator -- this would silently split the card "
                    "in two on the next sync"
                )
            if Q_PREFIX.match(line) or A_PREFIX.match(line):
                raise ParseError(
                    f"{field_name} contains a line starting with 'Q:' or 'A:' ({line!r}), which "
                    "flashback reads as the start of a new question/answer -- this would "
                    "silently corrupt the card's content on the next sync"
                )
        for ch in text:
            if ch in ("\n", "\t"):
                continue
            if unicodedata.category(ch) == "Cc":
                raise ParseError(
                    f"{field_name} contains a control character ({ch!r}), which can hide or "
                    "overwrite what's shown on screen when the card is displayed -- not allowed "
                    "in card text"
                )
            if unicodedata.category(ch) == "Cs":
                raise ParseError(
                    f"{field_name} contains an unpaired Unicode surrogate (U+{ord(ch):04X}), "
                    "which can't be encoded to UTF-8 or written to a deck file at all -- this "
                    "usually means invalid (non-UTF-8) byte data reached flashback as text, "
                    "often via a command-line argument; not allowed in card text"
                )
            if unicodedata.bidirectional(ch) in BIDI_FORMATTING_CLASSES:
                raise ParseError(
                    f"{field_name} contains a bidirectional-formatting character (U+"
                    f"{ord(ch):04X}), which can reorder how surrounding text is displayed on "
                    "screen -- not allowed in card text"
                )
            if ch in LINE_SEPARATOR_CHARS:
                raise ParseError(
                    f"{field_name} contains a Unicode line/paragraph separator (U+"
                    f"{ord(ch):04X}), which flashback's parser treats as a line break just "
                    "like a real newline -- this would silently change the card's stored text "
                    "on the next sync"
                )
            if _is_unicode_tag_char(ch):
                raise ParseError(
                    f"{field_name} contains a Unicode tag character (U+{ord(ch):04X}), which "
                    "has no visible glyph in any font and can hide an entire invisible message "
                    "inside text that looks perfectly ordinary on screen -- not allowed in card "
                    "text"
                )
            if ch == ZERO_WIDTH_NO_BREAK_SPACE:
                raise ParseError(
                    f"{field_name} contains a byte-order-mark character (U+FEFF), which is "
                    "invisible and would make this look identical to the same text without "
                    "it -- not allowed in card text"
                )
            if ch == ZERO_WIDTH_SPACE:
                raise ParseError(
                    f"{field_name} contains a zero-width space (U+200B), which is invisible "
                    "and would make this look identical to the same text without it -- not "
                    "allowed in card text"
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
    `add` would silently create a duplicate that then blocks `sync` (which
    calls parse_deck with the default `validate=True`) for the whole deck.

    Parses `existing_text` with `validate=False`: adding a new card
    shouldn't be blocked by some other, unrelated card in the same deck
    failing `_check_card_text` — same reasoning as `remove_card`/`edit_card`.
    """
    question = normalize_question(question.strip())
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

    Also raises ParseError, removing nothing, if *more than one* card matches
    `question` -- reachable despite `add`/`edit` both refusing to create a
    same-deck duplicate, because a deck file is documented as normal to
    hand-edit directly (see `parse_deck`'s own duplicate check, and this
    function's `validate=False` parse below, which deliberately tolerates a
    pre-existing duplicate elsewhere in the deck so it doesn't block removing
    some other, unrelated card). Filtering by `!=` here used to remove every
    card matching `question` at once, silently: precisely the "fix a
    hand-edited duplicate" case this function exists to help with, and
    exactly the case where deleting more than the one occurrence a caller
    asked for is real, silent data loss (the *other* duplicate's answer is
    gone too, with a plain "removed" success message giving no hint two cards
    vanished instead of one). Refusing here instead -- the same "don't guess
    which one you mean" response `_check_deck_collision` already gives a
    same-named-file collision in `cli.py` -- leaves both duplicates in place
    until the file is fixed by hand, rather than guessing which one to keep.

    Parses with `validate=False`: removing one card shouldn't be blocked by
    some other, unrelated card in the same deck failing `_check_card_text`.
    """
    question = normalize_question(question.strip())
    cards = parse_deck(existing_text, validate=False)
    matches = [card for card in cards if card.question == question]
    if not matches:
        raise ParseError(f"no card with that question found: {question!r}")
    if len(matches) > 1:
        raise ParseError(
            f"{len(matches)} cards share this same question ({question!r}) -- refusing to "
            "guess which one you mean to remove; fix the duplicate by hand, then remove/sync again"
        )

    remaining = [card for card in cards if card is not matches[0]]
    return _render_deck(remaining)


def edit_card(
    existing_text: str, question: str, new_question: Optional[str] = None, new_answer: Optional[str] = None
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
    The new-question-collision check below has the same shape: it only
    compares the new question against *other* cards' original questions, not
    a full rescan of the updated deck for any duplicate anywhere in it --
    otherwise a pre-existing duplicate pair elsewhere in the deck, unrelated
    to the card actually being edited, would block this edit too, the same
    "one poisoned pair blocks everything else" failure `validate=False` above
    already exists to prevent.

    Also raises ParseError, changing nothing, if *more than one* card matches
    `question` -- the same ambiguity `remove_card` refuses to guess through,
    reached the same way (a hand-edited duplicate `validate=False` above
    deliberately tolerates so it doesn't block editing some other, unrelated
    card). Without this, editing one occurrence of a duplicate-question pair
    used to update *every* card matching `question` at once: with only
    `new_answer` given, each duplicate silently got the same new answer,
    quietly discarding whichever one didn't already hold that text; with
    `new_question` given, every duplicate was renamed to the identical new
    question, still leaving the deck just as poisoned as before under the new
    name instead of fixing it -- and the new-question-collision check above
    can't catch that case, since it only compares the new question against
    cards whose *original* question differs from the one being searched for,
    which every duplicate here fails by construction.
    """
    if new_question is None and new_answer is None:
        raise ParseError("must provide a new question and/or a new answer to edit")

    question = normalize_question(question.strip())
    cards = parse_deck(existing_text, validate=False)

    matches = [card for card in cards if card.question == question]
    if not matches:
        raise ParseError(f"no card with that question found: {question!r}")
    if len(matches) > 1:
        raise ParseError(
            f"{len(matches)} cards share this same question ({question!r}) -- refusing to "
            "guess which one you mean to edit; fix the duplicate by hand, then edit/sync again"
        )
    target = matches[0]

    updated = []
    for card in cards:
        if card is target:
            q = normalize_question(new_question.strip()) if new_question is not None else card.question
            a = new_answer.strip() if new_answer is not None else card.answer
            if not q:
                raise ParseError("question cannot be empty")
            if not a:
                raise ParseError("answer cannot be empty")
            _check_card_text(q, a)
            if any(other.question == q for other in cards if other.question != question):
                raise ParseError(
                    f"a card with this question already exists in this deck: {q!r}"
                )
            updated.append(Card(question=q, answer=a))
        else:
            updated.append(card)

    return _render_deck(updated)
