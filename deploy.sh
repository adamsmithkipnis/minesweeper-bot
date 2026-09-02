#!/usr/bin/env bash
#
# Pull, verify, and restart the bot. This is the whole deployment story:
# `git push` from anywhere, and either this script or the watcher that calls
# it does the rest. Nothing has to be pasted into a chat window.
#
#   ./deploy.sh                 pull and restart
#   ./deploy.sh --if-changed    do nothing unless origin has moved (the watcher)
#   ./deploy.sh --skip-tests    restart even if the test suite fails
#   ./deploy.sh --no-restart    update the checkout only
#
# The tests run before the service is restarted, so a bad push cannot take a
# live board down: the deploy aborts and the old code keeps playing.

set -euo pipefail

# launchd runs jobs with a minimal PATH — roughly /usr/bin:/bin:/usr/sbin:/sbin
# and nothing else. A Homebrew git lives outside that, so the watcher would
# fail to find `git` and die silently every five minutes, which looks exactly
# like "deploys stopped working for no reason".
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/.venv"
SERVICE="com.minesweeper.bot"
BRANCH="${DEPLOY_BRANCH:-main}"

IF_CHANGED=0
SKIP_TESTS=0
RESTART=1
for arg in "$@"; do
  case "$arg" in
    --if-changed) IF_CHANGED=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --no-restart) RESTART=0 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { say "ERROR: $*"; exit 1; }

cd "$REPO_DIR"

# An agent editing this checkout may have left work in progress. Pulling over
# it would either fail or quietly clobber it, so stop and say so instead.
if [ -n "$(git status --porcelain)" ]; then
  if [ "$IF_CHANGED" -eq 1 ]; then
    say "working tree is dirty; skipping automatic deploy"
    git status --short | sed 's/^/    /'
    exit 0
  fi
  die "working tree is dirty — commit or stash first:
$(git status --short)"
fi

command -v git >/dev/null 2>&1 || die "git is not on PATH ($PATH)"
git fetch --quiet origin "$BRANCH" || die "git fetch failed"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
  if [ "$IF_CHANGED" -eq 1 ]; then exit 0; fi
  say "already up to date at ${LOCAL:0:8}"
else
  say "updating ${LOCAL:0:8} -> ${REMOTE:0:8}"
  git pull --ff-only --quiet origin "$BRANCH" || die "git pull failed (not a fast-forward?)"
  git --no-pager log --oneline "$LOCAL..HEAD" | sed 's/^/    /'
fi

# Dependencies. Cheap when nothing changed, and it means a new requirement
# never has to be installed by hand.
if [ ! -x "$VENV/bin/python" ]; then
  say "creating virtualenv"
  /usr/bin/python3 -m venv "$VENV" || die "could not create $VENV"
fi
"$VENV/bin/pip" install --quiet --disable-pip-version-check -r requirements.txt \
  || die "dependency install failed"

if [ "$SKIP_TESTS" -eq 0 ]; then
  say "running tests"
  if ! "$VENV/bin/python" -m unittest discover -s tests -t tests -q 2>&1 | tail -5; then
    die "tests failed — not restarting; the running bot is untouched"
  fi
fi

if [ "$RESTART" -eq 0 ]; then
  say "done (no restart requested)"
  exit 0
fi

if ! launchctl print "gui/$(id -u)/$SERVICE" >/dev/null 2>&1; then
  say "service $SERVICE is not loaded; load it once with:"
  say "    cp com.minesweeper.bot.plist ~/Library/LaunchAgents/"
  say "    launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.minesweeper.bot.plist"
  exit 0
fi

say "restarting $SERVICE"
launchctl kickstart -k "gui/$(id -u)/$SERVICE" || die "kickstart failed"

sleep 3
PID="$(launchctl list | awk -v s="$SERVICE" '$3 == s {print $1}')"
if [ -n "$PID" ] && [ "$PID" != "-" ]; then
  say "running as pid $PID"
else
  die "service did not come back — check the log"
fi

LOG_PATH="$(grep -E '^LOG_PATH=' .env 2>/dev/null | cut -d= -f2- || true)"
if [ -n "${LOG_PATH:-}" ] && [ -f "$LOG_PATH" ]; then
  say "last lines of $LOG_PATH:"
  tail -5 "$LOG_PATH" | sed 's/^/    /'
fi
say "deployed ${REMOTE:0:8}"
