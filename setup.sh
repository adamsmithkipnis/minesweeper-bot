#!/usr/bin/env bash
#
# One-time setup on the machine that runs the bot (the Mac Mini).
#
#   ./setup.sh                    install the bot and the deploy watcher
#   ./setup.sh --with-dashboard   also install the local dashboard
#   ./setup.sh --dry-run          show what would happen, change nothing
#   ./setup.sh --check            report what is currently installed
#
# Idempotent: safe to run again after a change. It builds the virtualenv,
# writes the launchd jobs with this checkout's real paths, and starts them.
#
# It will NOT create or edit .env. The app password belongs to you and should
# not pass through anything that logs. If .env is missing, this script writes
# a template and stops so you can fill in the one line by hand.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_DIR/.venv"
AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"

WITH_DASHBOARD=0
DRY_RUN=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --with-dashboard) WITH_DASHBOARD=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '  %s\n' "$*"; }
step() { printf '\n▸ %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
run() { if [ "$DRY_RUN" -eq 1 ]; then say "would run: $*"; else "$@"; fi; }

SERVICES=(com.minesweeper.bot com.minesweeper.deploy)
[ "$WITH_DASHBOARD" -eq 1 ] && SERVICES+=(com.minesweeper.dashboard)

status_report() {
  step "Status"
  say "repo:   $REPO_DIR ($(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout'))"
  say "venv:   $([ -x "$VENV/bin/python" ] && "$VENV/bin/python" -V 2>&1 || echo 'not created')"
  say ".env:   $([ -f "$REPO_DIR/.env" ] && echo present || echo MISSING)"
  for service in com.minesweeper.bot com.minesweeper.deploy com.minesweeper.dashboard; do
    local line pid
    line="$(launchctl list | awk -v s="$service" '$3 == s {print $1}')"
    if [ -z "$line" ]; then
      say "$service: not loaded"
    elif [ "$line" = "-" ]; then
      say "$service: loaded, not running"
    else
      say "$service: running (pid $line)"
    fi
  done
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  status_report
  exit 0
fi

# ---------------------------------------------------------------------------

step "Checking the checkout"
[ -f "$REPO_DIR/main.py" ] || die "run this from the minesweeper-bot checkout"
say "$REPO_DIR"

step "Commit guard"
run git -C "$REPO_DIR" config core.hooksPath hooks
say "pre-commit hook active — .env and app-password-shaped strings are blocked"

step "Virtualenv and dependencies"
if [ ! -x "$VENV/bin/python" ]; then
  run /usr/bin/python3 -m venv "$VENV"
  say "created $VENV"
else
  say "already present"
fi
run "$VENV/bin/pip" install --quiet --disable-pip-version-check -r "$REPO_DIR/requirements.txt"
say "dependencies installed"

step "Credentials"
if [ ! -f "$REPO_DIR/.env" ]; then
  if [ "$DRY_RUN" -eq 0 ]; then
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    # Point the data files at this checkout rather than the example paths.
    /usr/bin/sed -i '' \
      -e "s|^DB_PATH=.*|DB_PATH=$REPO_DIR/minesweeper.db|" \
      -e "s|^LOG_PATH=.*|LOG_PATH=$REPO_DIR/minesweeper.log|" \
      "$REPO_DIR/.env"
    chmod 600 "$REPO_DIR/.env"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    say "would write $REPO_DIR/.env from the example and stop for the password"
    exit 0
  fi
  cat <<MSG

  Wrote $REPO_DIR/.env from the example, with one line left to fill in:

      BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx

  Create an app password at Settings > Privacy and Security > App Passwords
  (never the account's real password), put it in that line, then run this
  script again. Nothing else needs editing.

MSG
  exit 1
fi
if grep -q '^BLUESKY_APP_PASSWORD=xxxx' "$REPO_DIR/.env"; then
  die "$REPO_DIR/.env still has the placeholder app password. Fill it in and re-run."
fi
chmod 600 "$REPO_DIR/.env"
say "handle: $(grep -E '^BLUESKY_HANDLE=' "$REPO_DIR/.env" | cut -d= -f2-)"
say "app password: set (not shown)"

step "Tests"
if [ "$DRY_RUN" -eq 0 ]; then
  "$VENV/bin/python" -m unittest discover -s "$REPO_DIR/tests" -t "$REPO_DIR/tests" -q \
    2>&1 | tail -3 || die "tests failed — not installing anything"
else
  say "would run the test suite"
fi

step "launchd jobs"
run mkdir -p "$AGENTS"
for service in "${SERVICES[@]}"; do
  template="$REPO_DIR/$service.plist"
  target="$AGENTS/$service.plist"
  [ -f "$template" ] || die "missing template $template"
  if [ "$DRY_RUN" -eq 1 ]; then
    say "would write $target with paths pointing at $REPO_DIR"
  else
    /usr/bin/sed "s|/ABSOLUTE/PATH/TO/minesweeper-bot|$REPO_DIR|g" \
      "$template" > "$target"
  fi
  # Reload so a changed plist actually takes effect.
  if launchctl print "gui/$UID_NUM/$service" >/dev/null 2>&1; then
    run launchctl bootout "gui/$UID_NUM/$service" || true
  fi
  run launchctl bootstrap "gui/$UID_NUM" "$target"
  say "$service installed"
done

if [ "$DRY_RUN" -eq 1 ]; then
  printf '\nDry run complete; nothing changed.\n'
  exit 0
fi

sleep 3
status_report

cat <<MSG

Done. The bot posts its first board within a minute; watch it with:

    tail -f $REPO_DIR/minesweeper.log

From here, deploying is just \`git push\` — the watcher polls origin every
five minutes and runs deploy.sh, which refuses to restart on a failing test
suite.
MSG
