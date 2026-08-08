# flashback

A plain-text, spaced-repetition flashcard tool for the terminal.

Cards live in ordinary markdown files, in an ordinary folder (or git repo).
`flashback` handles the scheduling — deciding when you're about to forget
something and should see it again — using a well-established algorithm (a
variant of SuperMemo's SM-2). Nothing is uploaded, no account is required,
and your deck files are still perfectly readable without the tool.

## Why

Most flashcard apps ask you to trust them with your data, learn their UI,
and stay subscribed. That's a reasonable trade if you want a slick mobile
app with sync built in. This is for the smaller case: you want to remember
things — vocabulary, definitions, facts, code you keep forgetting how to
write — and you'd rather keep that in a text file next to your notes than
inside someone else's app.

## Install

Requires Python 3.9+. No other dependencies.

```bash
git clone https://github.com/dailyamnesia/project.git flashback
cd flashback
pip install -e .
```

That gives you a `flashback` command. (Or skip installing entirely and run
`python3 -m flashback ...` from inside the repo.)

## Quick start

```bash
mkdir decks
cp examples/spanish-basics.md decks/

flashback sync       # load deck files into the review database
flashback review     # review whatever's due
flashback stats      # see totals per deck
```

`flashback sync` re-reads your deck files, so edit them freely — fix a
typo, add a card, delete one — and re-run `sync` to pick up the changes.
Existing cards keep their review history; only new questions start fresh.

## Deck file format

A deck is a markdown file with one or more cards, separated by a line of
three or more dashes:

```
Q: How do you say "hello" in Spanish?
A: Hola

---

Q: What's the Spanish word for "water"?
A: Agua
```

Questions and answers can span multiple lines — anything after `Q:` is
part of the question until an `A:` line starts, and everything after that
is the answer, until the next card. One file = one deck; the filename
(minus `.md`) is the deck's name.

## How scheduling works

Each card tracks three numbers: how many times in a row you've recalled it
correctly, the current interval between reviews, and an "easiness" factor.
When you review a card, you self-grade it — again, hard, good, or easy —
and those numbers update:

- **Again** means you didn't recall it: the streak resets and you'll see
  the card again tomorrow.
- **Hard / Good / Easy** all count as a successful recall. The interval
  grows — 1 day, then 6 days, then roughly `previous interval × easiness`
  — and the easiness factor adjusts up or down depending on how much
  effort the recall took.

This is a simplified version of SuperMemo's SM-2 algorithm (four grades
instead of SM-2's original six, to keep review fast). The exact math is in
[`flashback/scheduler.py`](flashback/scheduler.py), and its behavior is
pinned down by the tests in
[`tests/test_scheduler.py`](tests/test_scheduler.py) — that's the real
spec if the description above and the code ever disagree.

## Where things are stored

Card *content* stays in your deck files — plain text, meant to be
committed to git if you want. Review *state* (your progress, due dates,
per-card history) is kept separately in a local SQLite database at
`.flashback/state.sqlite3`, since that's specific to you and not something
you'd want to diff or merge. It's already in `.gitignore`.

## Development

```bash
python3 -m unittest discover -s tests
```

No external dependencies are needed to run the tool or its test suite —
everything is Python's standard library (`sqlite3`, `argparse`, `re`,
`unittest`).

## License

MIT — see [LICENSE](LICENSE).
