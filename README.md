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
flashback hard       # see which cards you're actually struggling with
```

`flashback sync` re-reads your deck files, so edit them freely — fix a
typo, add a card, delete one — and re-run `sync` to pick up the changes.
Existing cards keep their review history; only new questions start fresh.
Deleting an entire deck file works the same way: the next `sync` notices
it's gone and removes that deck's cards from the review database too,
rather than leaving them stuck showing up as due forever.

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
aren't allowed — `add` rejects it up front, and `sync` will refuse to load
a deck file that already has one (e.g. from hand-editing), rather than
silently keeping only one of them. The same question text in *different*
decks is fine and intentional: each deck is its own context, so those are
treated as two independent cards with independent schedules.

Since `Q:`, `A:`, and a line of three-or-more dashes are how the format
finds card boundaries, a question or answer can't itself contain a line
that looks like one of those markers — `add`/`edit` reject that up front,
rather than writing a file that misparses (or silently corrupts) on the
next `sync`. If you actually need to talk about dashes or the `Q:`/`A:`
syntax in a card, break up the line (e.g. a leading space, or `Q :`)
so it doesn't start the line on its own.

A question or answer also can't contain a control character other than a
newline or a tab — most notably ESC, which starts a terminal escape
sequence. `review` and `edit` print card text straight to the terminal, so
an embedded escape sequence could hide or overwrite what's actually
shown instead of just displaying as text; `add`/`edit` reject it the same
way as the structural-marker case above.

Nor can it contain an explicit Unicode bidirectional-formatting character
(RLO/LRO and the related embedding/isolate controls) — these can reorder
how the rest of the line displays without changing the underlying text,
the same trick used to disguise malicious filenames as harmless ones.
Ordinary right-to-left text (Hebrew, Arabic, etc.) and emoji sequences are
unaffected; only the small set of explicit formatting-control characters
is rejected.

A deck name is restricted the same way, for the same reason: `add`,
`remove`, and `edit` reject a control character or bidirectional-formatting
character in a deck name, since `sync`/`due`/`stats`/`review` all print the
deck name straight to the terminal too. Unlike card text, tab and newline
aren't allowed either — a deck name is a single-line identifier.

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
  effort the recall took. The interval is capped at 10 years, so a long
  streak of easy reviews can't push a card's next review date out far
  enough to break normal date arithmetic.

When there's nothing left to review, `flashback due` and `flashback review`
both tell you when the next card is actually scheduled:

```
$ flashback due
nothing due. go outside.
next card is due 2026-08-26 (in 6 days).
```

`flashback stats` shows the same thing per deck, in a `next` column — a
`-` there means that deck has cards due right now rather than a date in
the future. If you pass `--deck`, the next date reported is that deck's,
not the whole collection's.

## What you're bad at

The whole pitch of spaced repetition is that the tool works out what you
find hard and shows it to you more often. `flashback hard` says it out
loud:

```
$ flashback hard
1 card you missed at your last review:

[astronomy]
Q: What does a star's metallicity measure?
   due tomorrow

2 cards you've found hard before, but are getting right now:

[astronomy]
Q: What is the Chandrasekhar limit?
   correct at your last 4 reviews; next review 2026-08-26
[astronomy]
Q: Which planet has the shortest day?
   correct at your last 4 reviews; next review 2026-09-08
```

A card only appears here if your own grading has pushed its easiness below
where every card starts. That's not a simple tally of grades either way —
`again` (-0.8) and `hard` (-0.14) move easiness down far more than `easy`
(+0.1) moves it back up, so a single `hard` isn't undone by a single `easy`;
it takes two. If your grading hasn't pushed a card below its starting
point, `flashback hard` says so rather than ranking cards you're fine with.

The two groups matter, and they're the reason this isn't just a sorted
list. Easiness barely recovers once it falls (`good` doesn't raise it at
all; `easy` adds 0.1), so a card you struggled with a month ago and have
since got right four times running still scores exactly as badly as one
you missed this morning. Ranking on easiness alone would put a card you've
actually mastered at the top of a list headed "you're struggling with
this." The first group is what to worry about now; the second is progress.

Within the second group, cards aren't ranked by that same stuck easiness
either — a card graded `good` for months after one old slip would sit at
the top forever, since nothing about later `good` reviews ever moves it.
They're ranked by how soon the scheduler itself plans to check on each one
again: a card still being graded `hard` every few days ranks above one it's
already trusted for years, regardless of which one's easiness number reads
lower.

`--deck` limits it to one deck. `--limit` caps how many cards each group
shows (default 10, `0` for all); if it hides any, it says how many.

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
you'd want to diff or merge. The first time any command creates that
directory, it drops a `.gitignore` inside it too, so if your decks live in
a git repo you won't end up committing your review database along with
them without meaning to.

`add`/`remove`/`edit` never write a deck file in place — each writes the
new content to a temp file next to it and atomically renames it over the
original. If the write is interrupted partway (disk full, the process
killed), the deck file is left exactly as it was; you never end up with a
half-written or empty deck file.

`add`/`remove`/`edit` also serialize against each other, per deck: if two
run against the same deck at the same time (e.g. importing many cards from
a backgrounded shell loop), one waits for the other instead of both reading
the same starting content and one silently overwriting the other's change.
POSIX-only (Linux/macOS); on Windows this coordination is skipped.

`sync` commits each deck's changes to the review database as soon as
that deck is processed, not once at the very end. If `sync` is
interrupted partway through a run with many decks (Ctrl-C, a closed
terminal), every deck already reported as synced is genuinely saved —
only the deck in progress at the moment of interruption is left for the
next `sync` to pick up.

If a card gets removed (by `remove` + `sync`, possibly from another
terminal) after `review` has already shown it but before you grade it, or
if another `review` session (another terminal, another person sharing this
state dir) grades that same card first, `review` prints `card changed or
no longer exists elsewhere, skipped` instead of a `next review: ...` date
— either way, this grade can't actually be saved without silently
overwriting whatever already happened to the card, so it doesn't claim
otherwise.

## Development

```bash
python3 -m unittest discover -s tests
```

No external dependencies are needed to run the tool or its test suite —
everything is Python's standard library (`sqlite3`, `argparse`, `re`,
`unittest`).

## License

MIT — see [LICENSE](LICENSE).
