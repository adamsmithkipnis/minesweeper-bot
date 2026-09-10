"""Settings, read from .env at import time.

**Import this before anything reads a setting.** Modules read os.environ while
they are being imported, which happens before main() runs — so a load_dotenv()
call inside main() is too late and silently ignores .env entirely. Doing it
here, at module scope, is what makes that impossible.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Account. One account: Minesweeper is solitaire, which is the point — there
# is no second profile anybody could open to read the hidden state from.
HANDLE = os.environ.get("BLUESKY_HANDLE", "")
APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")

# Board. 9x9 with 13 mines runs a median of 24 turns; see tests/simulate.py.
ROWS = int(os.environ.get("ROWS", "9"))
COLS = int(os.environ.get("COLS", "9"))
MINES = int(os.environ.get("MINES", "13"))

# Difficulty tiers. A well-participated win promotes the next board a tier; a
# board mostly played by the bot demotes it. Separate grow and shrink
# thresholds give hysteresis, so middling participation does not seesaw.
#
# Tiers raise size and mine DENSITY together, which is the part that matters.
# Growing dimensions alone makes a board longer and *shallower* — at a fixed
# density a larger board is proportionally more open interior and less
# frontier, so more of its turns are trivially decidable. Simulated over 150
# boards each, at 30-minute turns:
#
#     9x9/13   16%   75% cleared   10h   1.2 turns needing real deduction
#     10x10/18 18%   73% cleared   15h   2.3
#     11x11/24 20%   61% cleared   20h   3.5
#     12x12/23 16%   83% cleared   43h   0.8   <- size alone: longer, shallower
#
# The ladder stops at three tiers on purpose. A fourth (12x12/32 at 22%)
# clears only 38%: a full day invested and two boards in three end in a bang.
ADAPTIVE_BOARD = os.environ.get("ADAPTIVE_BOARD", "1").lower() not in (
    "0", "false", "no")
GROW_PARTICIPATION = float(os.environ.get("GROW_PARTICIPATION", "0.75"))
SHRINK_PARTICIPATION = float(os.environ.get("SHRINK_PARTICIPATION", "0.50"))


def _parse_tiers(raw: str) -> list:
    """"9x9:13,10x10:18" -> [(9, 9, 13), (10, 10, 18)]."""
    tiers = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        shape, _, mines = chunk.partition(":")
        rows, _, cols = shape.lower().partition("x")
        tiers.append((int(rows), int(cols), int(mines)))
    return tiers


TIERS = _parse_tiers(os.environ.get(
    "TIERS", "9x9:13, 10x10:18, 11x11:24"))

# Pacing.
TURN_MINUTES = int(os.environ.get("TURN_MINUTES", "30"))
RESTART_DELAY_SECONDS = int(os.environ.get("RESTART_DELAY_SECONDS", "3600"))

# How many people must agree before the crowd's pick is played — but only
# once there are more voters than this. With one or two people voting there
# is nothing to split, so whoever turned up decides; applied strictly, a lone
# player would watch the bot take 100% of the turns. Above that it is worth
# real clear rate against vote splitting; see tests/simulate.py.
QUORUM = int(os.environ.get("QUORUM", "2"))

# How many people must flag a cell before the flag appears on the board.
# One, deliberately: at this audience size requiring two would mean flags
# essentially never show, and a flag that never appears teaches nobody.
FLAG_QUORUM = int(os.environ.get("FLAG_QUORUM", "1"))

DB_PATH = os.environ.get("DB_PATH", "minesweeper.db")
LOG_PATH = os.environ.get("LOG_PATH", "")

# 'live' posts to Bluesky. 'dry' writes every post to DRY_DIR instead and
# never touches the network, so a full game can be played through without
# credentials.
POST_MODE = os.environ.get("POST_MODE", "live")
DRY_DIR = os.environ.get("DRY_DIR", "dry-run")

# Hashtags. One is always present so the game is findable under its own name;
# the rest are sampled fresh per post from a larger pool, so the account does
# not post the identical block of tags 24 times a day. Tags are appended only
# while they still fit under the 300-character limit, so reach can never push
# the board information out of a post.
HASHTAG_ALWAYS = os.environ.get("HASHTAG_ALWAYS", "#Minesweeper")
HASHTAG_COUNT = int(os.environ.get("HASHTAG_COUNT", "5"))
HASHTAG_POOL = os.environ.get(
    "HASHTAG_POOL",
    "#gamedev #indiedev #solodev #indiegames #indiegame #gamedevelopment "
    "#puzzle #puzzlegames #logicpuzzles #braingames "
    "#retrogaming #retrogames #classicgames "
    "#play #playtogether #crowdplay #dailygame #gamenight "
    "#bots #botsky #bskygames #opensource #python",
).split()

# The one tag that goes on replies to individual followers. Six hashtags in a
# personal reply reads as spam; in a broadcast post it reads as reach.
REPLY_HASHTAG = os.environ.get("REPLY_HASHTAG", "#Minesweeper")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8766"))
SERVICE = "com.minesweeper.bot"
