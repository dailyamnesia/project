import tempfile
import unittest
from pathlib import Path

from flashback.cli import main
from flashback.parser import parse_deck


class TestAddCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_creates_deck_file_and_dir_if_missing(self):
        self.assertFalse(self.decks_dir.exists())
        rc = self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        self.assertTrue(deck_path.exists())
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].question, "hello?")
        self.assertEqual(cards[0].answer, "hola")

    def test_appends_to_existing_deck_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1].question, "goodbye?")

    def test_empty_question_fails_without_touching_file(self):
        rc = self.run_flashback("add", "spanish", "-q", "   ", "-a", "hola")
        self.assertEqual(rc, 1)
        self.assertFalse((self.decks_dir / "spanish.md").exists())


class TestRemoveCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_removes_a_card_from_an_existing_deck(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        rc = self.run_flashback("remove", "spanish", "-q", "hello?")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].question, "goodbye?")

    def test_missing_deck_file_fails(self):
        rc = self.run_flashback("remove", "no-such-deck", "-q", "hello?")
        self.assertEqual(rc, 1)

    def test_no_matching_question_fails_without_touching_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        rc = self.run_flashback("remove", "spanish", "-q", "not there")
        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)


class TestEditCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.decks_dir = Path(self._tmp.name) / "decks"

    def run_flashback(self, *args):
        return main(["--decks-dir", str(self.decks_dir), *args])

    def test_edits_answer_in_place(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        self.run_flashback("add", "spanish", "-q", "goodbye?", "-a", "adios")

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-answer", "hola!")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual([c.question for c in cards], ["hello?", "goodbye?"])
        self.assertEqual(cards[0].answer, "hola!")

    def test_edits_question_in_place(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")

        rc = self.run_flashback("edit", "spanish", "-q", "hello?", "--new-question", "hi?")
        self.assertEqual(rc, 0)

        deck_path = self.decks_dir / "spanish.md"
        cards = parse_deck(deck_path.read_text(encoding="utf-8"))
        self.assertEqual(cards[0].question, "hi?")

    def test_missing_deck_file_fails(self):
        rc = self.run_flashback("edit", "no-such-deck", "-q", "hello?", "--new-answer", "x")
        self.assertEqual(rc, 1)

    def test_no_matching_question_fails_without_touching_file(self):
        self.run_flashback("add", "spanish", "-q", "hello?", "-a", "hola")
        deck_path = self.decks_dir / "spanish.md"
        before = deck_path.read_text(encoding="utf-8")

        rc = self.run_flashback("edit", "spanish", "-q", "not there", "--new-answer", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(deck_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
