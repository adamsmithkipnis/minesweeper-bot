"""Minesweeper rules, board generation, and win/loss detection.

Pure logic only — no I/O, no network, no rendering.

The board is two layers and everything else derives from them:

    mine_cells  frozenset of (row, col) holding the hidden mines
    revealed    {(row, col): adjacent_mine_count} for cells that are open

A cell is "hidden" simply by not being in `revealed`. There is no third
grid of display values: the renderer and the solver both work from these
two, so they can never disagree about the position.

Rows are letters from 'A', columns are 1-based numbers, so a 9x9 board
addresses A1 through I9.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field

# Board defaults. The simulation in tests/simulate.py drives these: 13 mines
# on 9x9 gives a median of 22-24 turns, which is about a day at one turn an
# hour. Drop MINES to 11 for a gentler board (median 19).
DEFAULT_ROWS = 9
DEFAULT_COLS = 9
DEFAULT_MINES = 13

# Outcomes of a reveal.
SAFE, MINE, ALREADY = "safe", "mine", "already"

# Game statuses.
ACTIVE, CLEARED, EXPLODED = "active", "cleared", "exploded"


def row_letters(rows: int) -> str:
    """'A'..'I' for a 9-row board."""
    return string.ascii_uppercase[:rows]


def index_to_coord(r: int, c: int) -> str:
    """(0, 0) -> 'A1'."""
    return f"{string.ascii_uppercase[r]}{c + 1}"


def coord_to_index(coord: str) -> tuple:
    """'A1' -> (0, 0). Raises ValueError on anything malformed."""
    coord = coord.strip().upper()
    if len(coord) < 2:
        raise ValueError(f"bad coordinate: {coord!r}")
    r = string.ascii_uppercase.index(coord[0])
    return r, int(coord[1:]) - 1


def neighbors(r: int, c: int, rows: int, cols: int) -> list:
    """The up-to-8 cells touching (r, c), clipped to the board."""
    return [
        (r + dr, c + dc)
        for dr in (-1, 0, 1)
        for dc in (-1, 0, 1)
        if (dr or dc) and 0 <= r + dr < rows and 0 <= c + dc < cols
    ]


@dataclass
class GameState:
    game_id: int
    rows: int
    cols: int
    mine_count: int
    mine_cells: set                       # {(r, c), ...} — the hidden state
    revealed: dict = field(default_factory=dict)   # {(r, c): adjacent count}
    turn_number: int = 0        # turns played by the crowd; the opening is 0
    status: str = ACTIVE
    last_post_uri: str = ""

    # What happened on the previous turn, reported in the next post.
    last_coord: str = ""
    last_result: str = ""                 # SAFE | MINE
    last_source: str = ""                 # 'crowd' | 'bot' | 'opening'
    last_caller: str = ""                 # handle credited for the move
    last_votes: int = 0
    last_voters: int = 0
    exploded_cell: str = ""               # the mine that ended the run

    # ---- derived helpers -------------------------------------------------

    @property
    def total_safe(self) -> int:
        return self.rows * self.cols - self.mine_count

    def adjacent_mines(self, r: int, c: int) -> int:
        return sum(1 for n in neighbors(r, c, self.rows, self.cols)
                   if n in self.mine_cells)

    def hidden_cells(self) -> list:
        return [(r, c)
                for r in range(self.rows)
                for c in range(self.cols)
                if (r, c) not in self.revealed]

    def is_cleared(self) -> bool:
        """Every non-mine cell is open — the crowd has won."""
        return len(self.revealed) >= self.total_safe

    def coord_is_revealed(self, coord: str) -> bool:
        try:
            return coord_to_index(coord) in self.revealed
        except ValueError:
            return False

    def in_bounds(self, coord: str) -> bool:
        try:
            r, c = coord_to_index(coord)
        except ValueError:
            return False
        return 0 <= r < self.rows and 0 <= c < self.cols


def _place_mines(rows: int, cols: int, count: int, safe_cell: tuple,
                 rng: random.Random) -> set:
    """Scatter `count` mines, keeping `safe_cell` and all its neighbours clear.

    Clearing the neighbours too is what makes the opening click a zero, so
    the board opens with a flood-filled region instead of a lone number.
    """
    forbidden = {safe_cell} | set(neighbors(safe_cell[0], safe_cell[1], rows, cols))
    pool = [(r, c) for r in range(rows) for c in range(cols)
            if (r, c) not in forbidden]
    if len(pool) < count:
        raise ValueError(
            f"{count} mines will not fit on {rows}x{cols} with a safe opening")
    return set(rng.sample(pool, count))


def new_game(game_id: int, rows: int = DEFAULT_ROWS, cols: int = DEFAULT_COLS,
             mines: int = DEFAULT_MINES,
             rng: random.Random | None = None) -> GameState:
    """A fresh board with its opening region already revealed.

    The bot always makes the first click itself. A crowd cannot reason about
    a blank grid, so turn 1 has to arrive with numbers on it — and opening on
    a guaranteed zero means nobody can lose the game on move one.
    """
    rng = rng or random.Random()
    start = (rng.randrange(rows), rng.randrange(cols))
    state = GameState(
        game_id=game_id,
        rows=rows,
        cols=cols,
        mine_count=mines,
        mine_cells=_place_mines(rows, cols, mines, start, rng),
    )
    reveal(state, *start)
    state.last_coord = index_to_coord(*start)
    state.last_result = SAFE
    state.last_source = "opening"
    return state


def reveal(state: GameState, r: int, c: int) -> str:
    """Open (r, c), flood-filling through zeros. Mutates `state`.

    Returns SAFE, MINE, or ALREADY. Hitting a mine sets status to EXPLODED —
    sudden death, one mine ends the run.
    """
    if (r, c) in state.revealed:
        return ALREADY

    if (r, c) in state.mine_cells:
        state.status = EXPLODED
        state.exploded_cell = index_to_coord(r, c)
        return MINE

    # Iterative flood fill. Zeros pull in their neighbours; numbered cells
    # are opened but do not propagate.
    stack = [(r, c)]
    while stack:
        cell = stack.pop()
        if cell in state.revealed:
            continue
        count = state.adjacent_mines(*cell)
        state.revealed[cell] = count
        if count == 0:
            for n in neighbors(cell[0], cell[1], state.rows, state.cols):
                if n not in state.revealed and n not in state.mine_cells:
                    stack.append(n)

    if state.is_cleared():
        state.status = CLEARED
    return SAFE
