import unicodedata
import unittest

from flashback.parser import Card, ParseError, append_card, edit_card, parse_deck, remove_card


class TestParser(unittest.TestCase):
    def test_parses_single_card(self):
        cards = parse_deck("Q: capital of France?\nA: Paris\n")
        self.assertEqual(cards, [Card(question="capital of France?", answer="Paris")])

    def test_parses_multiple_cards_separated_by_dashes(self):
        text = "Q: 2+2?\nA: 4\n---\nQ: 3+3?\nA: 6\n"
        cards = parse_deck(text)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0].answer, "4")
        self.assertEqual(cards[1].answer, "6")

    def test_multiline_question_and_answer(self):
        text = (
            "Q: what does this function return?\n"
            "def f(x):\n"
            "    return x + 1\n"
            "A: x plus one,\n"
            "an integer.\n"
        )
        cards = parse_deck(text)
        self.assertEqual(len(cards), 1)
        self.assertIn("def f(x):", cards[0].question)
        self.assertIn("an integer.", cards[0].answer)

    def test_blank_text_yields_no_cards(self):
        self.assertEqual(parse_deck("   \n\n  "), [])

    def test_missing_answer_raises(self):
        with self.assertRaises(ParseError):
            parse_deck("Q: no answer here")

    def test_missing_question_raises(self):
        with self.assertRaises(ParseError):
            parse_deck("A: answer with no question")

    def test_text_before_the_first_q_marker_raises_instead_of_being_dropped(self):
        # Deck files are meant to be hand-edited, and a stray line above a
        # card's "Q:" (e.g. a misplaced question, forgotten to be prefixed)
        # used to be silently discarded — `sync` reported full success with
        # no warning, and the line was gone from both the deck file and the
        # database with no trace. Raising here, the same way an embedded
        # "Q:"/"A:" line inside a field already does, turns that into an
        # error naming the actual lost text instead of quietly eating it.
        with self.assertRaises(ParseError):
            parse_deck("What is the capital of France?\nQ: (trick question)\nA: Paris\n")

    def test_two_cards_missing_the_separator_between_them_raises_instead_of_merging(self):
        # Forgetting the '---' between two cards (an easy hand-editing slip,
        # and exactly the kind of thing a script or LLM generating deck text
        # can produce) used to silently parse as *one* card: the two
        # questions joined by a newline, the two answers joined by a newline
        # — no error, and nothing in sync's success output to hint that two
        # cards became one. The embedded-'Q:'/'A:'-line check in
        # _check_card_text can't catch this itself: by the time that check
        # runs, the second card's 'Q:'/'A:' prefixes have already been
        # stripped and merged into the first card's fields by _parse_card.
        text = "Q: first question\nA: first answer\n\nQ: second question\nA: second answer\n"
        with self.assertRaises(ParseError):
            parse_deck(text)

    def test_repeated_q_prefix_line_while_still_reading_the_question_raises(self):
        # Narrower sibling of the missing-separator check above: a second
        # 'Q:'-prefixed line while `section` is still "q" (not yet "a") used
        # to fall through with no error at all, since the guard above only
        # fires once the answer has already started. A genuine multi-line
        # question whose second physical line happens to start with the
        # literal text "Q:" (e.g. a card *about* the flashback format, or any
        # question that legitimately continues with a line reading "Q: ...")
        # had that line's leading "Q:" silently deleted by `Q_PREFIX.sub`
        # instead — the question came back one "Q:" shorter on every future
        # parse, with nothing in sync's success output to hint at it.
        # `_check_card_text` can't catch this on its own pass either: unlike
        # the missing-separator case, the deleted "Q:" isn't just relocated
        # somewhere else in the final text where that check could still spot
        # it — it's gone outright. `append_card` already rejects this content
        # up front (see test_new_question_with_embedded_q_prefix_line_raises
        # below); a hand-edited deck file reaching `parse_deck` needs the
        # same protection.
        text = "Q: line one\nQ: line two, literally about the Q: marker\nA: answer\n"
        with self.assertRaises(ParseError):
            parse_deck(text)

    def test_repeated_a_prefix_line_while_still_reading_the_answer_raises(self):
        # Same shape as the 'Q:' case just above, for the answer's own
        # marker: a second 'A:' line while the answer is already being read
        # silently lost its leading "A:" the same way, e.g. an answer that
        # legitimately continues with a line reading "A: ..." (documenting
        # the format itself, say).
        text = "Q: question\nA: line one\nA: line two, literally about the A: marker\n"
        with self.assertRaises(ParseError):
            parse_deck(text)

    def test_duplicate_question_in_same_deck_raises(self):
        text = "Q: hola\nA: hi\n---\nQ: hola\nA: hello (again)\n"
        with self.assertRaises(ParseError):
            parse_deck(text)

    def test_same_question_in_different_decks_is_fine(self):
        # parse_deck only sees one file at a time, so this isn't a duplicate
        # from its point of view — cross-deck duplicates are a separate,
        # deliberate design choice (see storage.card_id).
        cards_a = parse_deck("Q: hola\nA: hi\n")
        cards_b = parse_deck("Q: hola\nA: hi\n")
        self.assertEqual(cards_a, cards_b)

    def test_control_character_typed_directly_into_a_deck_file_raises(self):
        # Deck files are meant to be hand-edited, not only written through
        # `add`/`edit` — append_card's checks don't run for text that
        # reaches parse_deck this way, so parse_deck has to check for itself.
        # Without this, a control character typed straight into the file
        # would sync cleanly and only surface later, raw, when `review`
        # prints it to the terminal.
        with self.assertRaises(ParseError):
            parse_deck("Q: question\nA: answer with a bell\x07 in it\n")

    def test_bidi_override_typed_directly_into_a_deck_file_raises(self):
        with self.assertRaises(ParseError):
            parse_deck("Q: question\nA: evil‮txt.exe\n")

    def test_unpaired_surrogate_in_answer_raises(self):
        # A lone surrogate (U+D800-U+DFFF) can't occur in a real UTF-8 deck
        # file (surrogates have no valid UTF-8 encoding, so a file containing
        # one would fail to decode before ever reaching parse_deck) -- but it
        # can reach append_card/edit_card directly as a Python str, since
        # sys.argv decodes non-UTF-8 command-line bytes with the
        # 'surrogateescape' handler instead of raising. Without this check,
        # such a question/answer would sail through validation here and only
        # blow up later with UnicodeEncodeError when _atomic_write_text tries
        # to actually write it -- a crash that main()'s UnicodeEncodeError
        # handler misdiagnoses as a terminal-output-encoding problem, even
        # though no locale setting can make an unpaired surrogate valid.
        with self.assertRaises(ParseError):
            parse_deck("Q: question\nA: answer with \udcff in it\n")
        with self.assertRaises(ParseError):
            append_card("", "question", "answer with \udcff in it")

    def test_unpaired_surrogate_in_question_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "question with \udcff in it", "answer")

    def test_duplicate_question_differing_only_in_unicode_normalization_form_raises(self):
        # "é" has two equally valid Unicode encodings: one precomposed
        # codepoint (NFC, U+00E9) or "e" followed by a combining acute accent
        # (NFD, U+0065 U+0301). They render identically — a person reading
        # the deck file can't tell them apart — so, like the whitespace-only
        # difference the duplicate check already ignores, this must still be
        # caught as the same question, not silently treated as two cards.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfc, nfd)  # sanity: genuinely different strings
        text = f"Q: {nfc}\nA: first\n---\nQ: {nfd}\nA: second\n"
        with self.assertRaises(ParseError):
            parse_deck(text)


class TestAppendCard(unittest.TestCase):

    def test_appends_to_empty_text_with_no_separator(self):
        text = append_card("", "capital of France?", "Paris")
        self.assertEqual(text, "Q: capital of France?\nA: Paris\n")
        self.assertEqual(parse_deck(text), [Card(question="capital of France?", answer="Paris")])

    def test_appends_to_whitespace_only_text_with_no_separator(self):
        text = append_card("   \n\n  ", "2+2?", "4")
        self.assertNotIn("---", text)

    def test_appends_to_existing_text_with_separator(self):
        existing = "Q: 2+2?\nA: 4\n"
        text = append_card(existing, "3+3?", "6")
        cards = parse_deck(text)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0], Card(question="2+2?", answer="4"))
        self.assertEqual(cards[1], Card(question="3+3?", answer="6"))

    def test_strips_surrounding_whitespace(self):
        text = append_card("", "  spaced question?  ", "  spaced answer  ")
        self.assertEqual(text, "Q: spaced question?\nA: spaced answer\n")

    def test_empty_question_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "   ", "answer")

    def test_empty_answer_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "question", "   ")

    def test_duplicate_question_in_same_deck_raises(self):
        # Without this check, `add`-ing the same question twice silently
        # succeeds both times and writes a deck file that `parse_deck`
        # (validate=True, what `sync` calls) then refuses to read at all —
        # bricking the whole deck through the CLI with no error at the
        # moment that actually caused it.
        existing = append_card("", "capital of France?", "Paris")
        with self.assertRaises(ParseError):
            append_card(existing, "capital of France?", "a different answer")

    def test_duplicate_question_check_ignores_surrounding_whitespace(self):
        existing = append_card("", "capital of France?", "Paris")
        with self.assertRaises(ParseError):
            append_card(existing, "  capital of France?  ", "a different answer")

    def test_duplicate_question_check_does_not_block_an_unrelated_add(self):
        existing = append_card("", "capital of France?", "Paris")
        text = append_card(existing, "capital of Spain?", "Madrid")
        self.assertEqual(len(parse_deck(text)), 2)

    def test_duplicate_question_check_is_not_blocked_by_other_poisoned_card(self):
        # append_card parses existing_text with validate=False, same reasoning
        # as remove_card/edit_card: an unrelated card already on disk that
        # fails _check_card_text shouldn't block adding a new, distinct card.
        existing = "Q: question\nA: answer with a bell\x07 in it\n"
        text = append_card(existing, "a new question", "a new answer")
        self.assertEqual(len(parse_deck(text, validate=False)), 2)

    def test_answer_with_embedded_separator_line_raises(self):
        # a line of 3+ dashes inside the answer would read back as a card
        # separator and silently split this one card into two on the next
        # sync — reject it here instead of writing a file that misparses.
        with self.assertRaises(ParseError):
            append_card("", "what's a markdown rule?", "like so:\n---\ndone")

    def test_question_with_embedded_separator_line_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "explain this:\n---\nabove is dashes", "answer")

    def test_answer_with_embedded_q_prefix_line_raises(self):
        # a line starting with "Q:" inside the answer would read back as the
        # start of a new question, silently merging/corrupting content.
        with self.assertRaises(ParseError):
            append_card("", "how do I format a card?", "start with:\nQ: your question")

    def test_answer_with_embedded_a_prefix_line_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "how do I format a card?", "then:\nA: your answer")

    def test_two_dash_line_is_fine(self):
        # only 3+ dashes are read as a separator (see CARD_SEPARATOR) — a
        # shorter line like "--" is ordinary content and shouldn't be flagged.
        text = append_card("", "question", "see --\nthe notes")
        self.assertEqual(parse_deck(text)[0].answer, "see --\nthe notes")

    def test_answer_with_escape_character_raises(self):
        # ESC starts an ANSI/OSC escape sequence — `review` prints answer
        # text straight to the terminal, so this could hide or overwrite
        # what's actually shown (e.g. SGR "conceal") instead of just
        # displaying as text.
        with self.assertRaises(ParseError):
            append_card("", "question", "before\x1b[8mhidden\x1b[0mafter")

    def test_question_with_escape_character_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "before\x1b[2Jafter", "answer")

    def test_answer_with_other_control_character_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "question", "answer with a bell\x07 in it")

    def test_newline_and_tab_in_answer_are_fine(self):
        # multi-line answers already rely on embedded newlines; tabs are
        # ordinary formatting. Neither can manipulate the terminal on their
        # own, so only they are exempted from the control-character check.
        text = append_card("", "question", "line one\n\tindented line two")
        self.assertEqual(parse_deck(text)[0].answer, "line one\n\tindented line two")

    def test_answer_with_right_to_left_override_raises(self):
        # RLO (U+202E) forces everything after it to display in reverse
        # order until popped — the same mechanism used to disguise malicious
        # filenames as harmless ones ("evil‮txt.exe" prints as
        # "evilexe.txt"). Not a control character, so the Cc check above
        # doesn't catch it.
        with self.assertRaises(ParseError):
            append_card("", "question", "evil‮txt.exe")

    def test_question_with_left_to_right_override_raises(self):
        with self.assertRaises(ParseError):
            append_card("", "before‭after", "answer")

    def test_answer_with_other_bidi_formatting_characters_raises(self):
        # embedding and isolate controls (not just the override pair) can
        # also reorder surrounding text, so all nine explicit formatting
        # codes are rejected, not just RLO/LRO.
        for ch in ("‪", "‫", "‬", "⁦", "⁧", "⁨", "⁩"):
            with self.assertRaises(ParseError):
                append_card("", "question", f"answer with {ch} in it")

    def test_right_to_left_text_without_formatting_controls_is_fine(self):
        # ordinary RTL scripts (Hebrew, Arabic) are common, legitimate card
        # content — only the explicit formatting *controls* are rejected,
        # not text whose natural directionality is right-to-left.
        text = append_card("", "מה זה?", "זה בסדר")
        self.assertEqual(parse_deck(text)[0], Card(question="מה זה?", answer="זה בסדר"))

    def test_question_with_unicode_line_separator_raises(self):
        # U+2028 (LINE SEPARATOR) isn't a control character (Cc, so the Cc
        # check doesn't catch it) and doesn't reorder anything (so the bidi
        # check doesn't either) — but str.splitlines(), which _parse_card
        # uses to find line boundaries, treats it exactly like a real "\n".
        # Without this check, a question containing one parses and writes
        # fine here, then silently comes back as a *different* string (a
        # real newline in place of the invisible separator) the next time
        # the deck file is parsed — e.g. the very next `sync`, or a
        # `remove`/`edit` lookup using the exact same text originally passed
        # to `add`.
        with self.assertRaises(ParseError):
            append_card("", "before after", "answer")

    def test_answer_with_unicode_paragraph_separator_raises(self):
        # U+2029 (PARAGRAPH SEPARATOR) is splitlines()'s other non-Cc,
        # non-bidi line boundary; same reasoning as U+2028 above.
        with self.assertRaises(ParseError):
            append_card("", "question", "before after")

    def test_hand_edited_line_separator_would_silently_change_the_question_on_reparse(self):
        # Demonstrates the actual failure this check exists to prevent:
        # bypass validation (the same way parse_deck's own `validate=False`
        # path does, e.g. for a hand-edited file scenario) to see what a
        # line separator embedded in a deck file actually becomes on parse.
        # The text `add` would have written is not the text a later parse
        # reads back — exactly the "looks the same, isn't" gap this check
        # closes for every other caller.
        text = "Q: before after\nA: answer\n"
        cards = parse_deck(text, validate=False)
        self.assertEqual(len(cards), 1)
        self.assertNotEqual(cards[0].question, "before after")
        self.assertEqual(cards[0].question, "before\nafter")

    def test_emoji_sequence_with_zero_width_joiner_is_fine(self):
        # ZWJ-joined emoji sequences (e.g. family/skin-tone emoji) and
        # variation selectors are in the same Unicode "format" category as
        # the bidi controls, but they don't reorder anything and are
        # extremely common in ordinary text — must not be rejected.
        family = "\U0001f468‍\U0001f469‍\U0001f467"
        text = append_card("", "family emoji?", family)
        self.assertEqual(parse_deck(text)[0].answer, family)


class TestRemoveCard(unittest.TestCase):
    def test_removes_the_matching_card(self):
        text = "Q: 2+2?\nA: 4\n---\nQ: 3+3?\nA: 6\n"
        text = remove_card(text, "2+2?")
        cards = parse_deck(text)
        self.assertEqual(cards, [Card(question="3+3?", answer="6")])

    def test_removes_last_remaining_card(self):
        text = remove_card("Q: 2+2?\nA: 4\n", "2+2?")
        self.assertEqual(parse_deck(text), [])

    def test_leaves_other_cards_untouched_and_in_order(self):
        text = "Q: a\nA: 1\n---\nQ: b\nA: 2\n---\nQ: c\nA: 3\n"
        text = remove_card(text, "b")
        cards = parse_deck(text)
        self.assertEqual([c.question for c in cards], ["a", "c"])

    def test_matches_after_stripping_whitespace(self):
        text = "Q: 2+2?\nA: 4\n"
        text = remove_card(text, "  2+2?  ")
        self.assertEqual(parse_deck(text), [])

    def test_no_matching_question_raises(self):
        with self.assertRaises(ParseError):
            remove_card("Q: 2+2?\nA: 4\n", "not in this deck")

    def test_no_matching_question_leaves_text_semantics_unchanged(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            remove_card(text, "nope")
        # the original text itself is untouched (remove_card doesn't mutate
        # its argument); this just documents that ParseError is raised
        # before any card is dropped, not partway through.
        self.assertEqual(parse_deck(text), [Card(question="2+2?", answer="4")])

    def test_removes_unrelated_card_despite_another_poisoned_card_in_the_deck(self):
        # A control character elsewhere in the deck (e.g. typed straight into
        # the file, bypassing append_card's checks — see parse_deck's own
        # control-character test) would previously block removing any other,
        # unrelated card too, since remove_card used to call parse_deck with
        # its default full validation.
        text = "Q: a\nA: 1\n---\nQ: bad\nA: bell\x07here\n"
        result = remove_card(text, "a")
        self.assertEqual(parse_deck(result, validate=False), [Card(question="bad", answer="bell\x07here")])

    def test_matches_despite_differing_unicode_normalization_form(self):
        # Same "café" case as parse_deck's duplicate-detection test: the
        # question stored in the file and the question text passed in to
        # look it up can be spelled with the same characters in different
        # Unicode normalization forms and still render identically — that
        # has to match, the same way surrounding whitespace already does.
        nfc = unicodedata.normalize("NFC", "café")
        nfd = unicodedata.normalize("NFD", "café")
        text = append_card("", nfc, "coffee shop")
        result = remove_card(text, nfd)
        self.assertEqual(parse_deck(result), [])


class TestEditCard(unittest.TestCase):
    def test_edits_answer_only_and_keeps_position(self):
        text = "Q: a\nA: 1\n---\nQ: b\nA: 2\n---\nQ: c\nA: 3\n"
        text = edit_card(text, "b", new_answer="two")
        cards = parse_deck(text)
        self.assertEqual([c.question for c in cards], ["a", "b", "c"])
        self.assertEqual(cards[1].answer, "two")

    def test_edits_question_only(self):
        text = "Q: 2+2?\nA: 4\n"
        text = edit_card(text, "2+2?", new_question="what is 2+2?")
        self.assertEqual(parse_deck(text), [Card(question="what is 2+2?", answer="4")])

    def test_edits_both_question_and_answer(self):
        text = "Q: 2+2?\nA: 4\n"
        text = edit_card(text, "2+2?", new_question="2 plus 2?", new_answer="four")
        self.assertEqual(parse_deck(text), [Card(question="2 plus 2?", answer="four")])

    def test_no_matching_question_raises(self):
        with self.assertRaises(ParseError):
            edit_card("Q: 2+2?\nA: 4\n", "not there", new_answer="x")

    def test_no_new_fields_raises(self):
        with self.assertRaises(ParseError):
            edit_card("Q: 2+2?\nA: 4\n", "2+2?")

    def test_empty_new_question_raises(self):
        with self.assertRaises(ParseError):
            edit_card("Q: 2+2?\nA: 4\n", "2+2?", new_question="   ")

    def test_empty_new_answer_raises(self):
        with self.assertRaises(ParseError):
            edit_card("Q: 2+2?\nA: 4\n", "2+2?", new_answer="   ")

    def test_new_question_colliding_with_another_card_raises(self):
        text = "Q: a\nA: 1\n---\nQ: b\nA: 2\n"
        with self.assertRaises(ParseError):
            edit_card(text, "a", new_question="b")

    def test_new_answer_with_embedded_separator_line_raises(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "2+2?", new_answer="see:\n---\ndone")

    def test_new_question_with_embedded_q_prefix_line_raises(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "2+2?", new_question="how do cards work?\nQ: like this")

    def test_new_answer_with_escape_character_raises(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "2+2?", new_answer="four\x1b[8m (hidden)\x1b[0m")

    def test_new_answer_with_bidi_override_raises(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "2+2?", new_answer="evil‮txt.exe")

    def test_failure_leaves_text_semantics_unchanged(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "nope", new_answer="x")
        self.assertEqual(parse_deck(text), [Card(question="2+2?", answer="4")])

    def test_edits_unrelated_card_despite_another_poisoned_card_in_the_deck(self):
        # Same reasoning as remove_card's equivalent test: one poisoned card
        # elsewhere in the deck shouldn't block editing a different, unrelated
        # card.
        text = "Q: a\nA: 1\n---\nQ: bad\nA: bell\x07here\n"
        result = edit_card(text, "a", new_answer="one")
        cards = parse_deck(result, validate=False)
        self.assertEqual(cards[0], Card(question="a", answer="one"))
        self.assertEqual(cards[1], Card(question="bad", answer="bell\x07here"))

    def test_new_content_is_still_validated_despite_a_poisoned_card_elsewhere(self):
        # The validate=False parse used to locate the target card must not
        # weaken validation of the *new* text this call actually writes.
        text = "Q: a\nA: 1\n---\nQ: bad\nA: bell\x07here\n"
        with self.assertRaises(ParseError):
            edit_card(text, "a", new_answer="four\x1b[8m (hidden)\x1b[0m")


if __name__ == "__main__":
    unittest.main()
