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

    def test_failure_leaves_text_semantics_unchanged(self):
        text = "Q: 2+2?\nA: 4\n"
        with self.assertRaises(ParseError):
            edit_card(text, "nope", new_answer="x")
        self.assertEqual(parse_deck(text), [Card(question="2+2?", answer="4")])


if __name__ == "__main__":
    unittest.main()
