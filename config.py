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

# Pacing.
TURN_MINUTES = int(os.environ.get("TURN_MINUTES", "60"))
RESTART_DELAY_SECONDS = int(os.environ.get("RESTART_DELAY_SECONDS", "3600"))

# How many people must agree before the crowd's pick is played. Below this the
# crowd has not actually agreed on anything — one stray vote would carry the
# turn — so the bot plays its own safest cell and says so. Worth 18 points of
# clear rate against vote splitting; see tests/simulate.py.
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

# Appended to posts in this order, and only while they still fit under the
# 300-character limit — so a tag can never push the board information out.
HASHTAGS = os.environ.get(
    "HASHTAGS",
    "#Minesweeper #gamedev #indiedev #solodev #indiegames #play",
).split()

# The one tag that goes on replies to individual followers. Six hashtags in a
# personal reply reads as spam; in a broadcast post it reads as reach.
REPLY_HASHTAG = os.environ.get("REPLY_HASHTAG", "#Minesweeper")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8766"))
SERVICE = "com.minesweeper.bot"
