import unittest

from flashback.parser import Card, ParseError, parse_deck


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


if __name__ == "__main__":
    unittest.main()
