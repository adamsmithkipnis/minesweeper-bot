"""Headless pacing harness — how long does a board actually take?

Plays full games against a modelled crowd so a rules change that wrecks the
pacing fails visibly, instead of being discovered a week into a live board.

The crowd model: each voter names a provably-safe cell with probability
`--skill`, and otherwise names a random cell on the frontier, which may well
be a mine. Plurality wins. If the winning cell has fewer than `--quorum`
votes the crowd has not actually agreed on anything, so the bot plays its own
safest cell instead.

    python3 tests/simulate.py --games 300
    python3 tests/simulate.py --games 200 --voters 8 --skill 0.9
"""

import argparse
import random
import statistics
import sys
from collections import Counter

from helpers import game, solver     # noqa: F401  (helpers fixes sys.path)


def play(rows: int, cols: int, mines: int, voters: int, skill: float,
         quorum: int, rng: random.Random) -> dict:
    state = game.new_game(1, rows=rows, cols=cols, mines=mines, rng=rng)
    turns = 0
    bot_moves = 0
    levels = Counter()

    while state.status == game.ACTIVE and turns < 400:
        position = solver.Position.from_state(state)
        analysis = solver.analyze(position)
        levels[analysis.level] += 1

        hidden = [c for c in position.hidden() if c not in analysis.mines]
        frontier = sorted(
            c for c in hidden
            if any(n in position.revealed
                   for n in game.neighbors(c[0], c[1], rows, cols))
        ) or sorted(hidden)
        pool = sorted(analysis.safe)

        ballots = [
            rng.choice(pool) if (pool and rng.random() < skill)
            else rng.choice(frontier)
            for _ in range(voters)
        ]
        cell, count = Counter(ballots).most_common(1)[0]
        if count < quorum:
            cell, _ = solver.safest_move(position, analysis)
            bot_moves += 1

        turns += 1
        game.reveal(state, *cell)

    return {
        "turns": turns,
        "cleared": state.status == game.CLEARED,
        "bot_moves": bot_moves,
        "levels": levels,
        "progress": len(state.revealed) / state.total_safe,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=200)
    parser.add_argument("--rows", type=int, default=game.DEFAULT_ROWS)
    parser.add_argument("--cols", type=int, default=game.DEFAULT_COLS)
    parser.add_argument("--mines", type=int, default=game.DEFAULT_MINES)
    parser.add_argument("--voters", type=int, default=4)
    parser.add_argument("--skill", type=float, default=0.8)
    parser.add_argument("--quorum", type=int, default=2)
    parser.add_argument("--turn-minutes", type=int, default=60)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the median lands outside 18-28 turns")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    results = [play(args.rows, args.cols, args.mines, args.voters,
                    args.skill, args.quorum, rng)
               for _ in range(args.games)]

    turns = sorted(r["turns"] for r in results)
    cleared = [r for r in results if r["cleared"]]
    median = statistics.median(turns)
    levels = Counter()
    for r in results:
        levels.update(r["levels"])
    total_turns = sum(levels.values()) or 1

    def pct(n):
        return f"{n / total_turns * 100:5.1f}%"

    print(f"{args.rows}x{args.cols} with {args.mines} mines "
          f"({args.mines / (args.rows * args.cols):.1%} density), "
          f"{args.voters} voters at skill {args.skill}, quorum {args.quorum}")
    print(f"  {args.games} games")
    print(f"  boards cleared      {len(cleared) / args.games:6.1%}")
    print(f"  median turns        {median:6.1f}"
          f"   ({median * args.turn_minutes / 60:.0f}h at "
          f"{args.turn_minutes} min/turn)")
    print(f"  p10 / p90 turns     {turns[max(0, args.games // 10 - 1)]:3d} / "
          f"{turns[int(args.games * 0.9) - 1]}")
    print(f"  mean board cleared  "
          f"{statistics.mean(r['progress'] for r in results):6.1%}")
    print(f"  bot played for us   "
          f"{sum(r['bot_moves'] for r in results) / total_turns:6.1%} of turns")
    print(f"  turn difficulty     trivial {pct(levels[solver.TRIVIAL])}  "
          f"subset {pct(levels[solver.SUBSET])}  "
          f"enumeration {pct(levels[solver.ENUMERATED])}  "
          f"forced guess {pct(levels[solver.GUESS])}")

    if args.check and not 18 <= median <= 28:
        print(f"\nFAIL: median {median} turns is outside the 18-28 target; "
              f"a board should take about a day at one turn an hour.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
