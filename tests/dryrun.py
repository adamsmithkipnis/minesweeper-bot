"""Play a whole board with no network, no credentials, and a fake crowd.

    python3 tests/dryrun.py                 # crowd of 5 votes per turn
    python3 tests/dryrun.py --voters 0      # nobody votes; the bot plays alone
    python3 tests/dryrun.py --skill 0.5     # a careless crowd

This is the last check before anything goes near a live account. It runs the
real `main.game_tick`, so it exercises the parts the unit tests cannot: post
copy against the 300-character limit, facet byte offsets over emoji, the
quorum branch, the credit reply, the board image, and the database writes.

Every post is written to DRY_DIR as text plus a PNG, so the posts can be read
in order afterwards exactly as a follower would see them.
"""

import argparse
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def configure(out_dir: str, db_path: str) -> None:
    """Settings must be in place before the modules that read them import."""
    os.environ["POST_MODE"] = "dry"
    os.environ["DRY_DIR"] = out_dir
    os.environ["DB_PATH"] = db_path
    os.environ.setdefault("BLUESKY_HANDLE", "minesweeper.bsky.social")


def seed_boards(rng: random.Random) -> None:
    """Make board generation reproducible, so a bad run can be re-examined.

    game.new_game builds its own Random() when none is passed, which draws
    from the OS and ignores random.seed().
    """
    import game

    original = game.new_game

    def seeded(game_id, **kwargs):
        kwargs.setdefault("rng", rng)
        return original(game_id, **kwargs)

    game.new_game = seeded


def install_fake_crowd(voters: int, skill: float, rng: random.Random) -> None:
    """Point bluesky.get_replies at a synthetic crowd.

    Each voter names a provably safe cell with probability `skill`, and
    otherwise something plausible on the frontier — the same model the pacing
    simulation uses. Replies are shaped like real ones, including the handle
    and DID, so the mention facet in the credit reply is built for real.
    """
    import bluesky
    import db
    import game
    import solver
    import votes

    def fake_replies(post_uri: str) -> list:
        state = db.load_state()
        if state is None or state.status != game.ACTIVE or voters <= 0:
            return []
        position = solver.Position.from_state(state)
        analysis = solver.analyze(position)
        hidden = [c for c in position.hidden() if c not in analysis.mines]
        frontier = [c for c in hidden
                    if any(n in position.revealed
                           for n in game.neighbors(c[0], c[1], state.rows,
                                                   state.cols))] or hidden
        safe = sorted(analysis.safe)

        out = []
        for i in range(voters):
            cell = (rng.choice(safe) if safe and rng.random() < skill
                    else rng.choice(frontier))
            coord = game.index_to_coord(*cell)
            text = rng.choice([
                coord,
                f"I vote {coord}",
                f"{coord} looks safe to me",
                f"gotta be {coord}",
                f"the 1 at {game.index_to_coord(*rng.choice(list(state.revealed)))} "
                f"means {coord} is clear — {coord}",
            ])
            out.append(votes.Reply(
                did=f"did:plc:voter{i}", handle=f"voter{i}.bsky.social",
                text=text, uri=f"at://did:plc:voter{i}/app.bsky.feed.post/{i}",
                cid="bafyfake", created_at=f"2026-09-01T00:{i:02d}:00Z",
                root_uri=post_uri, root_cid="bafyroot"))
        return out

    bluesky.get_replies = fake_replies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voters", type=int, default=5)
    parser.add_argument("--skill", type=float, default=0.8)
    parser.add_argument("--turns", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default="/tmp/minesweeper-dryrun")
    parser.add_argument("--keep", action="store_true",
                        help="keep a previous run's output instead of wiping it")
    args = parser.parse_args()

    if not args.keep and os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)
    db_path = os.path.join(tempfile.mkdtemp(), "dryrun.db")
    configure(args.out, db_path)

    import main as bot
    import db
    import game

    bot.setup_logging()
    db.init_db()
    bot.bluesky.login()
    seed_boards(random.Random(args.seed))
    install_fake_crowd(args.voters, args.skill, random.Random(args.seed + 1))

    bot.start_new_game()
    for _ in range(args.turns):
        state = db.load_state()
        if state is None or state.status != game.ACTIVE:
            break
        bot.game_tick()

    state = db.load_state()
    posts = sorted(f for f in os.listdir(args.out) if f.endswith(".txt"))
    longest = 0
    for name in posts:
        with open(os.path.join(args.out, name)) as handle:
            body = handle.read().split("\n--- ")[0]
        longest = max(longest, len(body.rstrip("\n")))

    print(f"\nBoard {state.game_id} ended {state.status} on turn "
          f"{state.turn_number}: {len(state.revealed)} of {state.total_safe} "
          f"cells cleared")
    print(f"{len(posts)} posts written to {args.out}")
    print(f"longest post: {longest} chars (limit 300)")

    moves = db.get_moves()
    crowd = [m for m in moves if m["source"] == "crowd"]
    print(f"{len(crowd)} of {len(moves)} moves came from the crowd")

    if longest > 300:
        print("FAIL: a post exceeded the character limit")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
