# Minesweeper Bluesky Bot

A game of Minesweeper played on one Bluesky account, where followers vote in
the replies for the next cell to open.

Every hour the bot posts the board and asks for a coordinate. Replies are
tallied, the winning cell is opened, and the follower who called it is named
in the next post and replied to. Standard rules: numbers count adjacent mines,
opening a blank floods its whole region, and **one mine ends the run**.

## Why Minesweeper

It fixes the two things that were wrong with the Battleship bot.

**Hidden information stays hidden.** Battleship published each team's fleet as
an image from a public account, so a voter for one side could open the other
account and read the ship positions. Minesweeper is solitaire: there is one
board, one account, and nothing published that anybody could read the answers
from. In this repo that is a structural property, not a habit — the solver
only ever sees a `Position` built from what a follower can see, and the
renderer's `display_grid` is the single place the hidden layer can surface.

**A vote can be right or wrong.** Nine people picking between three arbitrary
squares of empty ocean is a coin flip. A Minesweeper position has provably
safe cells, provably mined cells, and the occasional genuine 50/50 — so a
reply is a claim, and it can be demonstrably correct.

## What the simulation says

`tests/simulate.py` plays full boards against a modelled crowd. The numbers
that set the defaults:

- **~98% of positions contain a provably safe cell.** Almost every turn, some
  reply is provably right.
- **94% of turns are decidable by the single-cell rule** — the shallow "this 1
  is already satisfied" kind. Only about 1.3 turns a board need the subset
  rule or enumeration, and ~1.5% are forced guesses. The reasoning is real but
  most of it is easy; raising the mine count buys harder positions at the cost
  of more coin flips.
- **9x9 with 13 mines clears 85% of boards at a median of 24 turns** — about a
  day at one turn an hour, with a p10/p90 of 12 and 33.
- **The quorum is worth 18 points of clear rate.** Free coordinates split the
  vote: four voters can name four different *correct* cells while two agree on
  a mine, and the mine wins with 2 votes. Requiring two people to agree before
  the bot plays the crowd's pick takes boards cleared from 67% to 85%.

```bash
python3 tests/simulate.py --games 300              # the default board
python3 tests/simulate.py --quorum 0 --voters 8    # try it without the quorum
```

## How a turn works

1. Read the replies to the last post.
2. Tally them. One vote per account, earliest reply wins, ties go to whoever
   called the cell first. Votes for cells that are already open are dropped.
3. If the winning cell has at least `QUORUM` votes, open it. Otherwise the
   crowd has not actually agreed on anything — one stray vote would carry the
   turn — so the bot opens its own safest cell and the post says so.
4. Post the new board, and reply to the follower whose call was played.

If the post fails, nothing is saved: the next turn replays against the same
post rather than losing a turn to a network blip.

### Voting

Reply with a coordinate — `D4`, `d4`, `A-1`, `vote C7`. Replies argue, so the
parser reads them properly rather than grabbing the first coordinate it sees:

| Reply | Counts as |
| --- | --- |
| `D4` | D4 |
| `C3 is a mine, so D4 must be safe — D4` | D4 |
| `don't open C3, try D4` | D4 |
| `flag C3` / `C3 is a mine` | no vote |
| `I vote A1, actually I pick B2` | B2 |
| `a good move` / `I agree` | no vote |

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | Orchestrator and APScheduler loop |
| `game.py` | Rules, board generation, flood fill, win/loss (pure logic) |
| `solver.py` | Deduction: single-cell rule, subset rule, exact enumeration |
| `votes.py` | Coordinate parsing and vote tallying (no network dependency) |
| `renderer.py` | Board PNG and full-position alt text |
| `bluesky.py` | AT Protocol wrapper, richtext facets, post deletion, dry run |
| `db.py` | SQLite: board state, history, moves, post log |
| `dashboard.py` | Read-only local dashboard |
| `reset.py` | Wipe posts and/or local data for a clean slate |
| `deploy.sh` | Pull, test, restart — the whole deployment |
| `tests/` | Unit tests, the pacing simulator, the dry run, board previews |

## Setup

```bash
/usr/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in the handle and app password
.venv/bin/python main.py      # foreground; Ctrl+C to stop
```

Use a Bluesky **app password** (Settings → Privacy and Security → App
Passwords), never the account's real password. `.env` is gitignored and must
stay that way.

### Run as a daemon (macOS)

```bash
./setup.sh                  # builds the venv, installs the launchd jobs, starts them
./setup.sh --check          # what is installed and running right now
./setup.sh --dry-run        # show what it would do, change nothing
```

`setup.sh` is idempotent and fills the real paths into the launchd plists, so
nothing has to be hand-edited. It will not create or edit `.env`: the app
password is yours and should not pass through anything that logs it. On the
first run it writes `.env` from the example with everything filled in except
the password, and stops so you can add that one line.

### Deploying

`git push` is the deploy. On the Mini, `deploy.sh` pulls, installs, runs the
test suite, and only restarts the service if the tests pass — so a bad push
aborts the deploy and leaves the running board untouched.

```bash
./deploy.sh                  # pull and restart now
./deploy.sh --if-changed     # no-op unless origin moved (used by the watcher)
```

Install `com.minesweeper.deploy.plist` and the Mini polls origin every five
minutes, which means nothing has to be typed anywhere to ship a change.

## Testing

Everything runs headlessly, with no network and no credentials:

```bash
.venv/bin/python -m unittest discover -s tests -t tests   # rules, solver, parsing, rendering
.venv/bin/python tests/simulate.py --games 300            # pacing
.venv/bin/python tests/dryrun.py                          # a full board, posts written to disk
.venv/bin/python tests/preview.py                         # sample board images
```

`tests/dryrun.py` is the last check before going live. It runs the real turn
loop with `POST_MODE=dry` and a synthetic crowd, writing every post with its
character count, facet byte offsets, alt text and image, so the copy can be
read in order exactly as a follower would see it.

## Dashboard

```bash
.venv/bin/python dashboard.py      # http://127.0.0.1:8766
```

Read-only: the database is opened with `PRAGMA query_only` and votes are read
from Bluesky's public AppView with no credentials.

**Do not expose it.** The Battleship dashboard's warning was that it showed
both fleets; this one is worse, because it shows the solver's answers to the
board that is currently being played.

## Notes for anyone changing this

- **Facet ranges are UTF-8 byte offsets**, not character offsets, and the post
  copy is full of emoji. Build the text, clamp it to 300, *then* compute
  facets from the clamped text.
- **`config.py` calls `load_dotenv()` at import time.** Modules read
  `os.environ` while importing, which happens before `main()` runs, so loading
  `.env` inside `main()` would silently do nothing.
- **launchd redirects stdout into `LOG_PATH`**, so the stdout log handler is
  only added when stdout is a TTY. Otherwise every line is written twice.
- **SQLite is in WAL mode** so the dashboard can read while the bot writes. A
  `file:...?mode=ro` connection *fails* against WAL; `PRAGMA query_only=1` is
  the way to get a read-only connection.
- **Never commit `.env`.**
- **Do not advance or save game state until the post succeeds.**
- Vote parsing is conservative about English: a bare `a`, `b` or `I` is a word,
  not a vote. On this board `I` is also a row letter, so it only votes with a
  digit attached.
