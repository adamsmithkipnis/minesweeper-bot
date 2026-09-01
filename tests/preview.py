"""Render sample boards to PNG for eyeballing, plus their alt text.

    python3 tests/preview.py [--out DIR]

Rendering is the one part of the bot that cannot be asserted meaningfully in
a unit test — a green 3 on a green cell passes every check and is unreadable.
So this writes the states worth looking at and prints the alt text beside
them: a fresh board, a board mid-game, a board down to a forced 50/50, and
the moment a run ends on a mine.
"""

import argparse
import os
import random

from helpers import game, solver
import renderer


def _play_until(state, stop):
    """Advance with solver moves until `stop(state, analysis)` is true."""
    for _ in range(400):
        position = solver.Position.from_state(state)
        analysis = solver.analyze(position)
        if stop(state, analysis) or state.status != game.ACTIVE:
            return analysis
        cell, _ = solver.safest_move(position, analysis)
        state.turn_number += 1
        state.last_coord = game.index_to_coord(*cell)
        state.last_result = game.reveal(state, *cell)
    return solver.analyze(solver.Position.from_state(state))


def sample_states():
    fresh = game.new_game(1, rng=random.Random(7))
    yield "01-fresh", fresh, "the board as it opens, before any votes"

    for seed in range(40):
        mid = game.new_game(2, rng=random.Random(seed + 100))
        _play_until(mid, lambda s, a: len(s.revealed) > s.total_safe * 0.6)
        if mid.status == game.ACTIVE:
            break
    yield "02-midgame", mid, "about two thirds cleared, still alive"

    fifty = game.new_game(3, rng=random.Random(5))
    for seed in range(60):
        fifty = game.new_game(3, rng=random.Random(seed))
        analysis = _play_until(fifty, lambda s, a: not a.safe and a.probs)
        if fifty.status == game.ACTIVE and not analysis.safe:
            break
    yield "03-fifty-fifty", fifty, "no provably safe cell — a real coin flip"

    boom = game.new_game(4, rng=random.Random(9))
    _play_until(boom, lambda s, a: len(s.revealed) > s.total_safe * 0.4)
    hidden = [c for c in solver.Position.from_state(boom).hidden()
              if c in boom.mine_cells]
    if hidden:
        boom.turn_number += 1
        boom.last_coord = game.index_to_coord(*hidden[0])
        boom.last_result = game.reveal(boom, *hidden[0])
    yield "04-exploded", boom, "the run ends: every mine shown, the hit one in red"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/minesweeper-preview")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    for name, state, caption in sample_states():
        png = renderer.render_board(state, highlight=state.last_coord)
        path = os.path.join(args.out, f"{name}.png")
        with open(path, "wb") as handle:
            handle.write(png)
        alt = renderer.build_alt_text(state)
        print(f"{path}  ({len(png):,} bytes) — {caption}")
        print(f"  alt text: {len(alt)} chars, status={state.status}")
        with open(os.path.join(args.out, f"{name}.txt"), "w") as handle:
            handle.write(alt)
    print(f"\nWrote to {args.out}")


if __name__ == "__main__":
    main()
