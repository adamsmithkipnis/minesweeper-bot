# Working agreement for this repo

Two agents edit this repo: Claude Code (usually from the laptop) and OpenClaw
(on the Mac Mini, at `/Users/robot/minesweeper-bot`). The Mini's checkout is
also the deployment target, so an uncommitted edit there is both a lost change
and a blocked deploy.

## Git

- **Pull before editing**: `git pull --rebase origin main`.
- **Commit and push immediately** when you finish a change. Never leave work
  sitting uncommitted in the Mini's checkout — `deploy.sh` refuses to touch a
  dirty tree, so the bot silently stops receiving updates until it is cleaned
  up.
- `git push` is the deploy. The watcher on the Mini polls origin every five
  minutes and runs `deploy.sh --if-changed`.
- **Never commit `.env`.** It holds the Bluesky app password. It is gitignored;
  keep it that way. `.env` exists only on the Mini and is created by hand once.

## Before pushing

```bash
.venv/bin/python -m unittest discover -s tests -t tests
```

`deploy.sh` runs this too and aborts the restart if it fails, so a red suite
means the change simply never reaches the live bot. If a change affects the
rules, the solver, or the voting policy, also run:

```bash
.venv/bin/python tests/simulate.py --games 300 --check   # pacing stays ~a day
.venv/bin/python tests/dryrun.py                         # a full board end to end
```

## Things that will bite you

These are load-bearing and each one has already cost a debugging session, here
or in the Battleship bot:

- The solver must only ever see `solver.Position`, never a `GameState`. That
  is what makes it impossible for hidden mines to leak into a move, a
  probability, or a post.
- `renderer.display_grid` is the only place the hidden layer becomes visible,
  and only once the game is over. Both the image and the alt text go through
  it. Do not add a second path.
- Post text: build, clamp to 300, *then* compute facets — the offsets are
  UTF-8 bytes, not characters.
- `config.py` must be imported before anything reads a setting; it calls
  `load_dotenv()` at import time on purpose.
- Do not save game state before the post succeeds.
- The dashboard shows the solver's answers. It binds to localhost. Leave it
  there.

## Operating the bot

```bash
launchctl list | grep minesweeper                       # is it running
launchctl kickstart -k gui/$(id -u)/com.minesweeper.bot # restart
tail -f minesweeper.log                                 # watch it
.venv/bin/python main.py --play 1                       # play one turn by hand
.venv/bin/python reset.py --dry-run --all               # see what a wipe would do
```
