#!/usr/bin/env bash
#
# One read-only report on the live bot, so nobody has to paste a series of
# commands into a chat window to find out whether a deploy landed.
#
#   ./status.sh               the last 24 hours
#   ./status.sh --hours 72    a longer window for errors and board starts
#
# It reports the deployed commit against origin, whether the bot and the
# deploy watcher are running, the latest deploy result, errors in the
# window, recent board starts, and the big-board schedule. It changes
# nothing. The last line is OK or ATTENTION, and the exit status is 0 or 1
# to match.

set -uo pipefail

# The same PATH fix as deploy.sh: launchd and chat agents both run with a
# minimal PATH that leaves out a Homebrew git.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$REPO_DIR/.venv/bin/python"
SERVICE="com.minesweeper.bot"
WATCHER="com.minesweeper.deploy"
BRANCH="${DEPLOY_BRANCH:-main}"

HOURS=24
while [ $# -gt 0 ]; do
  case "$1" in
    --hours) HOURS="${2:-24}"; shift 2 ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$HOURS" in ''|*[!0-9]*) echo "--hours takes a whole number" >&2; exit 2 ;; esac

cd "$REPO_DIR" || exit 1
PROBLEMS=()
flag() { PROBLEMS+=("$1"); }
section() { printf '\n== %s\n' "$1"; }

LOG="$(grep -E '^LOG_PATH=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
LOG="${LOG:-minesweeper.log}"
DEPLOY_LOG="deploy.log"

# Log lines start "YYYY-MM-DD HH:MM:SS", which sorts as text, so the window
# is a string comparison against a cutoff in the same format. BSD date on
# the Mini, GNU date anywhere else.
CUTOFF="$(date -v-"${HOURS}"H '+%Y-%m-%d %H:%M:%S' 2>/dev/null \
          || date -d "-${HOURS} hours" '+%Y-%m-%d %H:%M:%S')"
since() { awk -v cut="$CUTOFF" 'substr($0, 1, 19) >= cut' "$1" 2>/dev/null; }
# "2026-11-02 00:05:12,345 INFO minesweeper: msg" -> "2026-11-02 00:05:12  msg"
tidy() { sed -E 's/^([0-9-]+ [0-9:]+),[0-9]+ [A-Z]+ [^ ]+: /\1  /'; }
# The message alone, for grouping repeats of the same failure.
message() { sed -E 's/^[0-9-]+ [0-9:,]+ [A-Z]+ [^ ]+: //'; }

printf 'Minesweeper bot status · %s · window: last %sh\n' \
  "$(date '+%a %b %-d %-I:%M %p %Z')" "$HOURS"

# ---------------------------------------------------------------------------
section "Deployed code"
HEAD_LINE="$(git --no-pager log -1 --format='%h %s (%cr)' 2>/dev/null)"
echo "running checkout: ${HEAD_LINE:-unknown}"
if git fetch --quiet origin "$BRANCH" 2>/dev/null; then
  BEHIND="$(git rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null || echo '?')"
  if [ "$BEHIND" = "?" ]; then
    echo "origin/$BRANCH: could not compare with the checkout"
    flag "could not compare the checkout with origin/$BRANCH"
  elif [ "$BEHIND" = "0" ]; then
    echo "origin/$BRANCH: up to date"
  else
    echo "origin/$BRANCH: $BEHIND commit(s) not deployed yet:"
    git --no-pager log --format='    %h %s (%cr)' "HEAD..origin/$BRANCH"
    # The watcher polls every five minutes, so a fresh push is normal.
    NEWEST="$(git log -1 --format=%ct "origin/$BRANCH")"
    if [ $(( $(date +%s) - NEWEST )) -gt 900 ]; then
      flag "origin/$BRANCH has been ahead of the checkout for over 15 minutes"
    fi
  fi
else
  echo "origin/$BRANCH: could not fetch (network?)"
  flag "could not reach origin to compare commits"
fi
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "working tree: DIRTY — deploy.sh will refuse to update until this is committed:"
  git status --short | sed 's/^/    /'
  flag "uncommitted changes in the checkout are blocking deploys"
else
  echo "working tree: clean"
fi

# ---------------------------------------------------------------------------
section "Services"
service_line() { launchctl list 2>/dev/null | awk -v s="$1" '$3 == s'; }
BOT="$(service_line "$SERVICE")"
if [ -z "$BOT" ]; then
  echo "bot: NOT LOADED"
  flag "$SERVICE is not loaded"
else
  PID="$(echo "$BOT" | awk '{print $1}')"
  LAST="$(echo "$BOT" | awk '{print $2}')"
  if [ "$PID" = "-" ]; then
    echo "bot: NOT RUNNING (last exit status $LAST)"
    flag "$SERVICE is loaded but not running"
  else
    # -15 is SIGTERM from a deploy's restart, not a crash.
    echo "bot: running, pid $PID (previous run ended with status $LAST)"
  fi
fi
WATCH="$(service_line "$WATCHER")"
if [ -z "$WATCH" ]; then
  echo "deploy watcher: NOT LOADED — pushes will not deploy"
  flag "$WATCHER is not loaded"
else
  echo "deploy watcher: loaded (last exit status $(echo "$WATCH" | awk '{print $2}'))"
fi

# ---------------------------------------------------------------------------
section "Latest deploy"
if [ -f "$DEPLOY_LOG" ]; then
  # The watcher prints nothing when origin has not moved, so the tail of
  # the log is the last deploy that actually did something.
  tail -8 "$DEPLOY_LOG" | sed 's/^/    /'
  if tail -8 "$DEPLOY_LOG" | grep -qE 'ERROR|tests failed|dirty'; then
    flag "the latest deploy did not complete — see above"
  fi
else
  echo "no $DEPLOY_LOG here"
fi

# ---------------------------------------------------------------------------
section "Bot log ($LOG)"
if [ ! -f "$LOG" ]; then
  echo "no log file at $LOG"
  flag "bot log $LOG not found"
else
  STARTED="$(grep -E 'Minesweeper bot starting' "$LOG" | tail -1 | cut -c1-19)"
  echo "last restart: ${STARTED:-not in log}"
  echo "last line:    $(tail -1 "$LOG" | tidy | cut -c1-160)"

  ERRORS="$(since "$LOG" | grep -E '^[0-9-]+ [0-9:,]+ (ERROR|CRITICAL) ')"
  COUNT="$(printf '%s' "$ERRORS" | grep -c . || true)"
  echo "errors in window: $COUNT"
  if [ "$COUNT" -gt 0 ]; then
    # Grouped by message, so one failure repeating every turn reads as one
    # problem with a count rather than a screenful of the same line.
    printf '%s\n' "$ERRORS" | message | cut -c1-140 | sort | uniq -c | sort -rn | head -8 \
      | sed 's/^/    /'
    echo "    most recent: $(printf '%s\n' "$ERRORS" | tail -1 | tidy | cut -c1-160)"
    flag "$COUNT error line(s) in the last ${HOURS}h"
  fi

  echo "board starts in window:"
  STARTS="$(since "$LOG" | grep -E 'Starting board ')"
  if [ -n "$STARTS" ]; then
    printf '%s\n' "$STARTS" | tail -6 | tidy | sed 's/^/    /'
  else
    echo "    none"
  fi
fi

# ---------------------------------------------------------------------------
section "Big board"
# Read from the bot's own config and database, so the sizes and the day are
# whatever .env says rather than a copy that could drift.
if [ -x "$PY" ]; then
  BIG="$("$PY" - <<'EOF' 2>&1
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config, db

days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
         "Saturday", "Sunday"]
boards = getattr(config, "MASSIVE_BOARDS", None)
sizes = [f"{r}x{c}" for r, c, _ in boards or []]
if boards is None:
    print("schedule: not in the deployed code yet (Massive Monday is PR #26)")
elif not config.KNOCKOUT:
    print("schedule: off (voting mode never runs the big board)")
elif config.MASSIVE_DAY not in days or not sizes:
    print("schedule: off (MASSIVE_DAY is empty)")
else:
    day = days.index(config.MASSIVE_DAY)
    print(f"schedule: every {names[day]}, sizes {' → '.join(sizes)}")
    try:
        last = db.get_setting("massive_last_date") or "never"
    except Exception as exc:
        last = f"unknown ({exc})"
    print(f"last run: {last}")
    tz = ZoneInfo(config.LEADERBOARD_TZ)
    today = datetime.now(tz).date()
    ahead = (day - today.weekday()) % 7
    if ahead == 0 and last == today.isoformat():
        ahead = 7
    nxt = today + timedelta(days=ahead)
    # Nothing runs before MASSIVE_START, so step forward a week at a time.
    start = getattr(config, "MASSIVE_START", "")
    while start and nxt.isoformat() < start:
        nxt += timedelta(days=7)
    if start:
        print(f"starts:   {start}")
    rung = config.MASSIVE_BOARDS[min((nxt.day - 1) // 7, len(sizes) - 1)]
    print(f"next:     {nxt:%a %b %-d} — {rung[0]}x{rung[1]}, {rung[2]} mines")
EOF
)"
  printf '%s\n' "$BIG"
  SIZES="$("$PY" -c 'import config; print("|".join(f"{r}x{c}" for r, c, _ in config.MASSIVE_BOARDS))' 2>/dev/null)"
else
  echo "no virtualenv at $PY; cannot read the schedule"
fi
SIZES="${SIZES:-14x14|18x18|22x22|26x26}"
if [ -f "$LOG" ]; then
  RUNS="$(grep -E "Starting board [0-9]+ \(($SIZES)," "$LOG" | tail -3)"
  echo "big boards started (whole log):"
  if [ -n "$RUNS" ]; then
    printf '%s\n' "$RUNS" | tidy | sed 's/^/    /'
  else
    echo "    none yet"
  fi
fi

# ---------------------------------------------------------------------------
echo
if [ ${#PROBLEMS[@]} -eq 0 ]; then
  echo "OK"
  exit 0
fi
echo "ATTENTION:"
for problem in "${PROBLEMS[@]}"; do echo "  - $problem"; done
exit 1
