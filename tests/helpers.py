"""Shared test scaffolding: build a board with an exact, known mine layout."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import game      # noqa: E402
import solver    # noqa: E402
import votes     # noqa: E402
import main      # noqa: E402


def make(rows: int, cols: int, mines: list, revealed: list) -> game.GameState:
    """A GameState with `mines` placed and exactly `revealed` cells open.

    Cells are opened directly rather than through reveal(), so a test can set
    up a precise position without flood fill pulling in extra cells. Counts
    are computed from the mine layout, so the position is always consistent.
    """
    state = game.GameState(
        game_id=1, rows=rows, cols=cols,
        mine_count=len(mines), mine_cells=set(mines),
    )
    for cell in revealed:
        state.revealed[cell] = state.adjacent_mines(*cell)
    return state
