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

Just want the `flashback` command:

```bash
pip install git+https://github.com/dailyamnesia/project.git
```

Want to read or change the source too:

```bash
git clone https://github.com/dailyamnesia/project.git flashback
cd flashback
pip install -e .
```

Either way you end up with a `flashback` command. (Or skip installing
entirely and run `python3 -m flashback ...` from inside a clone of the
repo.)

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

To add a card without hand-editing the file, use `flashback add`:

```bash
flashback add spanish-basics -q "How do you say 'thanks'?" -a "Gracias"
```

Leave off `-q`/`-a` and it'll prompt for them instead. The deck file (and
`decks/` itself) is created if it doesn't exist yet. Either way, run
`flashback sync` afterward to pick up the new card.

To remove a card by its question, without hand-editing the file, use
`flashback remove`:

```bash
flashback remove spanish-basics -q "How do you say 'thanks'?"
```

Leave off `-q` and it'll prompt instead. It's an error if no card in that
deck has that exact question. Run `flashback sync` afterward — that's also
when the card's review history actually gets deleted, same as if you'd
removed it from the file by hand.

To change a card's question and/or answer in place — instead of `remove`
then `add`, which loses the card's position in the file and makes you
retype whichever field didn't change — use `flashback edit`:

```bash
flashback edit spanish-basics -q "How do you say 'thanks'?" --new-answer "Gracias / Muchas gracias"
```

Leave off `--new-question`/`--new-answer` (and `-q`) and it'll prompt,
showing you the current question and answer first so you can see what
you're changing. At least one of `--new-question`/`--new-answer` is
required; whichever you don't pass is left alone. One thing worth knowing:
editing only the answer preserves the card's review history on the next
sync (it's the same card, as far as scheduling is concerned); editing the
question does not — `flashback sync` treats it as a new card and drops the
old history, same as a `remove` + `add` would, since review history is
keyed on the question text.

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

Two cards with the identical question text *within the same deck file*
aren't allowed — `sync` will refuse that file with an error, rather than
silently keeping only one of them. The same question text in *different*
decks is fine and intentional: each deck is its own context, so those are
treated as two independent cards with independent schedules.

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
