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
  local head behind
  head="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
  say "repo:   $REPO_DIR (at $head)"
  if git -C "$REPO_DIR" fetch --quiet origin main 2>/dev/null; then
    behind="$(git -C "$REPO_DIR" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
    if [ "${behind:-0}" -gt 0 ]; then
      say "        BEHIND origin/main by $behind commit(s) — deploys are not landing"
    else
      say "        up to date with origin/main"
    fi
  else
    say "        could not reach origin (network, or git not on PATH)"
  fi
  say "venv:   $([ -x "$VENV/bin/python" ] && "$VENV/bin/python" -V 2>&1 || echo 'not created')"
  say ".env:   $([ -f "$REPO_DIR/.env" ] && echo present || echo MISSING)"
  say "git:    $(command -v git || echo 'NOT ON PATH')"
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
  if [ -f "$REPO_DIR/deploy.log" ]; then
    say ""
    say "last lines of deploy.log:"
    tail -6 "$REPO_DIR/deploy.log" | sed 's/^/    /'
  else
    say "deploy.log: none yet — the watcher has never run"
  fi
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
# Check the shape rather than the literal placeholder. Matching only on
# "xxxx" meant that editing the placeholder in .env.example — as happened
# once — would quietly disable this guard and let the bot start with no
# credentials at all.
APP_PW="$(grep -E '^BLUESKY_APP_PASSWORD=' "$REPO_DIR/.env" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
if ! printf '%s' "$APP_PW" | grep -qE '^[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}$' \
   || printf '%s' "$APP_PW" | grep -qi '^xxxx'; then
  die "BLUESKY_APP_PASSWORD in $REPO_DIR/.env is not a real app password.
  Expected the xxxx-xxxx-xxxx-xxxx shape from Settings > Privacy and Security
  > App Passwords. Fill it in and re-run."
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

# `launchctl bootout` returns before the job has finished unloading, and
# bootstrapping into that gap fails with "Bootstrap failed: 5: Input/output
# error" — leaving the service unloaded. So: only reload when the plist
# actually changed, wait for the unload to complete, and retry.
wait_until_gone() {
  local target="$1" i
  for i in $(seq 1 40); do
    launchctl print "$target" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  return 1
}

reload_service() {
  local service="$1" plist="$2"
  local target="gui/$UID_NUM/$service"
  local i

  if launchctl print "$target" >/dev/null 2>&1; then
    launchctl bootout "$target" >/dev/null 2>&1 || true
    wait_until_gone "$target" || say "  (still unloading; continuing)"
  fi

  # bootstrap can return "5: Input/output error" for a while after an
  # unload, and again right after the plist file has been replaced. It is
  # transient, so retry with a growing pause rather than giving up after a
  # few seconds — the old three-tries-then-kickstart could not recover,
  # because kickstart needs a service that is already loaded.
  for i in 1 1 2 2 3 3 5 5; do
    if launchctl bootstrap "gui/$UID_NUM" "$plist" >/dev/null 2>&1; then
      return 0
    fi
    # If it is loaded again by now, a kick is all that is needed.
    if launchctl print "$target" >/dev/null 2>&1; then
      launchctl kickstart -k "$target" >/dev/null 2>&1 && return 0
    fi
    sleep "$i"
  done

  # Last resort: the legacy loader still works in some cases where
  # bootstrap keeps returning EIO.
  if launchctl load -w "$plist" >/dev/null 2>&1 \
     && launchctl print "$target" >/dev/null 2>&1; then
    say "  ($service loaded via the legacy loader)"
    return 0
  fi
  return 1
}

# A service that is loaded but not running is still a stopped bot, so
# check the end state rather than trusting the loader's exit status.
verify_loaded() {
  local service="$1"
  launchctl print "gui/$UID_NUM/$service" >/dev/null 2>&1
}

step "launchd jobs"
run mkdir -p "$AGENTS"
failed=""
for service in "${SERVICES[@]}"; do
  template="$REPO_DIR/$service.plist"
  target="$AGENTS/$service.plist"
  [ -f "$template" ] || die "missing template $template"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "would write $target with paths pointing at $REPO_DIR"
    continue
  fi

  tmp="$(mktemp)"
  /usr/bin/sed "s|/ABSOLUTE/PATH/TO/minesweeper-bot|$REPO_DIR|g" "$template" > "$tmp"

  if [ -f "$target" ] && cmp -s "$tmp" "$target" \
     && launchctl print "gui/$UID_NUM/$service" >/dev/null 2>&1; then
    # Unchanged and already loaded: a kick avoids the unload race entirely.
    rm -f "$tmp"
    if launchctl kickstart -k "gui/$UID_NUM/$service" >/dev/null 2>&1 \
       && verify_loaded "$service"; then
      say "$service restarted"
    else
      say "$service could not be restarted"
      failed="$failed $service"
    fi
    continue
  fi

  mv "$tmp" "$target"
  chmod 644 "$target"
  if reload_service "$service" "$target" && verify_loaded "$service"; then
    say "$service installed"
  else
    say "$service FAILED to load"
    failed="$failed $service"
  fi
done

if [ -n "$failed" ]; then
  printf '\n' >&2
  for service in $failed; do
    say "Could not load $service. Try it directly:"
    say "    launchctl bootout gui/$UID_NUM/$service 2>/dev/null; \\"
    say "    launchctl bootstrap gui/$UID_NUM $AGENTS/$service.plist"
  done
  die "one or more services are not loaded — the bot may be stopped"
fi

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
