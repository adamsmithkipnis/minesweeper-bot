"""Wipe the bot back to a clean slate — for testing.

Deletes the bot's posts from Bluesky and/or clears the local database, so the
account looks untouched to anyone browsing it.

    python3 reset.py --dry-run          # show what would happen, change nothing
    python3 reset.py --db               # clear local data only, keep posts
    python3 reset.py --posts            # delete Bluesky posts only
    python3 reset.py --all              # both (the usual testing reset)
    python3 reset.py --all --keep-record   # ...but preserve the board record

Deleting posts is irreversible. Two things worth knowing:
  * Followers' replies live in their own repos and cannot be deleted here.
    They will remain on their profiles as replies to a missing post.
  * Stop the bot first. If it posts mid-wipe you are left with orphans, and
    deleting the post it is waiting on makes it fall back to its own moves.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

import bluesky
import config
import db


def bot_is_running() -> bool:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return False
    for line in out.splitlines():
        if config.SERVICE in line:
            return line.split("\t")[0].strip().isdigit()
    return False


def _launchctl(action: str) -> bool:
    uid = subprocess.run(["id", "-u"], capture_output=True,
                         text=True).stdout.strip()
    target = f"gui/{uid}/{config.SERVICE}"
    args = (["launchctl", "bootout", target] if action == "stop" else
            ["launchctl", "kickstart", "-k", target])
    return subprocess.run(args, capture_output=True, text=True).returncode == 0


def wipe_posts(dry_run: bool) -> None:
    bluesky.login()
    posts = bluesky.list_all_posts()
    print(f"{len(posts)} post(s) in @{config.HANDLE}")
    for post in posts[:5]:
        first_line = post["text"].splitlines()[0] if post["text"] else "(no text)"
        print(f"    {post['created_at'][:19]}  {first_line[:60]}")
    if len(posts) > 5:
        print(f"    ... and {len(posts) - 5} more")
    if not posts:
        return
    if dry_run:
        print("  [dry run] would delete all of the above")
        return

    def progress(done, total):
        print(f"\r  deleting {done}/{total}...", end="", flush=True)

    deleted, failed = bluesky.delete_posts([p["rkey"] for p in posts], progress)
    print(f"\r  deleted {len(deleted)}, failed {len(failed)}          ")
    db.forget_posts([p["uri"] for p in posts])


def wipe_db(dry_run: bool, keep_record: bool) -> None:
    state = db.load_state()
    record = db.get_record()
    print(f"local data: board {state.game_id if state else 'none'}"
          f"{f' (turn {state.turn_number}, {state.status})' if state else ''}, "
          f"{record['played']} finished, {db.count_posts()} posts logged")
    if dry_run:
        print("  [dry run] would clear the database"
              f"{' but keep the record' if keep_record else ''}")
        return
    db.reset_all(keep_record=keep_record)
    print("  cleared")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--posts", action="store_true",
                        help="delete the bot's Bluesky posts")
    parser.add_argument("--db", action="store_true",
                        help="clear the local database")
    parser.add_argument("--all", action="store_true", help="both")
    parser.add_argument("--keep-record", action="store_true",
                        help="keep the all-time board record")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen and change nothing")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation prompt")
    args = parser.parse_args()

    do_posts = args.posts or args.all
    do_db = args.db or args.all
    if not (do_posts or do_db):
        parser.print_help()
        return 1

    db.init_db()

    if not args.dry_run and bot_is_running():
        print(f"The bot ({config.SERVICE}) is running. Stop it first:")
        print(f"    launchctl bootout gui/$(id -u)/{config.SERVICE}")
        return 1

    if not args.dry_run and not args.yes:
        what = " and ".join(filter(None, [
            "delete every post on Bluesky" if do_posts else "",
            "clear the local database" if do_db else ""]))
        print(f"About to {what}. This cannot be undone.")
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    if do_posts:
        wipe_posts(args.dry_run)
    if do_db:
        wipe_db(args.dry_run, args.keep_record)

    print("Done." if not args.dry_run else "Dry run complete; nothing changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
