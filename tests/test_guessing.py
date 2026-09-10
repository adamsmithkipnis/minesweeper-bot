"""What the bot does when there is no provably safe cell.

A real board was lost to this. A 25-cell frontier exceeded the old
enumeration limit of 20, so the solver returned no probabilities at all, and
safest_move fell through to its last resort — picking by how many hidden
neighbours a cell had, which is a shape heuristic rather than a safety one.
It opened a cell at 24% and blew the board's last spare with nobody watching.
"""

import random
import unittest

from helpers import game, solver


def position_from_rows(rows, mine_count):
    """Build a Position from the same grid the alt text publishes."""
    revealed = {}
    for r, row in enumerate(rows):
        for c, token in enumerate(row.split()):
            if token.isdigit():
                revealed[(r, c)] = int(token)
    return solver.Position(rows=len(rows), cols=len(rows[0].split()),
                           mine_count=mine_count, revealed=revealed)


# The exact position that lost the board, read back off the published post.
LOST_BOARD = [
    "1 1 1 1 ? ? ? ? ?",
    "1 ? 2 2 ? ? 1 ? ?",
    "1 1 2 ? 3 2 2 ? ?",
    "0 0 1 1 2 ? 2 ? ?",
    "0 0 0 0 2 2 3 ? ?",
    "0 0 1 1 2 ? 2 ? ?",
    "1 1 2 ? 3 2 ? ? ?",
    "? ? 3 ? ? ? 2 ? ?",
    "? ? ? ? ? ? ? ? ?",
]


class TheBoardThatWasLost(unittest.TestCase):
    def setUp(self):
        self.position = position_from_rows(LOST_BOARD, mine_count=13)

    def test_the_frontier_is_now_enumerable(self):
        """25 cells, against an old limit of 20 and a new one of 30."""
        analysis = solver.analyze(self.position)
        self.assertTrue(analysis.exact,
                        "this position must not fall through to the estimate")
        self.assertTrue(analysis.probs)

    def test_it_no_longer_picks_the_cell_that_lost_the_board(self):
        analysis = solver.analyze(self.position)
        cell, reason = solver.safest_move(self.position, analysis)
        self.assertNotEqual(game.index_to_coord(*cell), "A5")
        self.assertIn("guess", reason, "there is genuinely no safe cell here")
        chosen = analysis.probs[cell]
        self.assertLess(chosen, analysis.probs[game.coord_to_index("A5")])
        self.assertLess(chosen, 0.10, "should find something much safer")


class WhenEnumerationIsUnavailable(unittest.TestCase):
    """The estimate is not exact, but it must beat guessing by shape."""

    def test_it_ranks_a_tight_constraint_as_more_dangerous(self):
        # A 1-of-2 is a coin flip; a 1-of-8 is not.
        cons = [(frozenset({(0, 0), (0, 1)}), 1),
                (frozenset({(5, c) for c in range(8)}), 1)]
        position = solver.Position(rows=9, cols=9, mine_count=2, revealed={})
        estimate = solver.estimate_probabilities(position, cons, set())
        self.assertAlmostEqual(estimate[(0, 0)], 0.5)
        self.assertAlmostEqual(estimate[(5, 0)], 0.125)
        self.assertLess(estimate[(5, 0)], estimate[(0, 0)])

    def test_a_cell_takes_its_worst_constraint(self):
        """Appearing in a safe-looking constraint must not launder a cell
        that another constraint says is dangerous."""
        shared = (0, 0)
        cons = [(frozenset({shared, (0, 1)}), 1),
                (frozenset({shared} | {(1, c) for c in range(7)}), 1)]
        position = solver.Position(rows=9, cols=9, mine_count=2, revealed={})
        estimate = solver.estimate_probabilities(position, cons, set())
        self.assertAlmostEqual(estimate[shared], 0.5)

    def test_estimates_stay_within_bounds(self):
        for seed in range(40):
            state = game.new_game(seed, rng=random.Random(seed))
            position = solver.Position.from_state(state)
            cons = solver.constraints(position)
            _, mines, leftover = solver._subset(cons)
            for value in solver.estimate_probabilities(
                    position, leftover, mines).values():
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_a_guess_is_always_available(self):
        """safest_move must return something playable however little it knows."""
        state = game.new_game(1, rng=random.Random(3))
        position = solver.Position.from_state(state)
        cell, reason = solver.safest_move(position)
        self.assertIn(cell, position.hidden())
        self.assertTrue(reason)


class SoundnessIsUnaffected(unittest.TestCase):
    def test_the_raised_limit_did_not_break_the_claims(self):
        """The guarantee that matters: never call a mine safe."""
        checked = 0
        for seed in range(60):
            state = game.new_game(seed, rng=random.Random(seed + 31),
                                  mine_budget=99)
            while state.status == game.ACTIVE and not state.is_cleared():
                position = solver.Position.from_state(state)
                analysis = solver.analyze(position)
                for cell in analysis.safe:
                    self.assertNotIn(cell, state.mine_cells)
                    checked += 1
                for cell in analysis.mines:
                    self.assertIn(cell, state.mine_cells)
                cell, _ = solver.safest_move(position, analysis)
                game.reveal(state, *cell)
        self.assertGreater(checked, 2000)


if __name__ == "__main__":
    unittest.main()


class DetonatedMinesAreKnown(unittest.TestCase):
    """A spent mine is public, and the solver must use it.

    It is drawn on the board and everybody watched it go off, so withholding
    it from the solver helps nobody — and a number beside one has a mine
    fewer left to account for, so ignoring it makes the constraints wrong.
    Until this was fixed a spent mine also stayed in the hidden set, so the
    bot could keep choosing an already-detonated cell and stall the board.
    """

    def board(self, detonations=1, seed=3):
        state = game.new_game(1, rng=random.Random(seed), mine_budget=99)
        for cell in sorted(state.mine_cells)[:detonations]:
            game.reveal(state, *cell)
        return state

    def test_a_spent_mine_is_no_longer_playable(self):
        state = self.board()
        position = solver.Position.from_state(state)
        for cell in state.spent_mines:
            self.assertNotIn(cell, position.hidden())

    def test_the_bot_cannot_choose_one(self):
        for seed in range(25):
            state = self.board(detonations=3, seed=seed)
            if state.status != game.ACTIVE:
                continue
            position = solver.Position.from_state(state)
            cell, _ = solver.safest_move(position)
            self.assertNotIn(cell, state.spent_mines)

    def test_no_constraint_still_contains_one(self):
        state = self.board(detonations=3)
        position = solver.Position.from_state(state)
        for cells, _ in solver.constraints(position):
            self.assertFalse(cells & state.spent_mines)

    def test_a_spent_mine_counts_against_its_neighbours(self):
        """A 1 beside a detonated mine has nothing dangerous left."""
        state = game.new_game(1, rows=5, cols=5, mines=1, mine_budget=9,
                              rng=random.Random(11))
        mine = next(iter(state.mine_cells))
        for cell in game.neighbors(*mine, 5, 5):
            if cell not in state.revealed:
                state.revealed[cell] = state.adjacent_mines(*cell)
        game.reveal(state, *mine)
        analysis = solver.analyze(solver.Position.from_state(state))
        self.assertEqual(analysis.mines, set(),
                         "the only mine is spent; nothing is still mined")

    def test_soundness_holds_across_boards_with_detonations(self):
        """The claims must survive knockout play, which no earlier soundness
        test exercised — they all used budget 0 and ended at the first mine."""
        checked = 0
        for seed in range(40):
            state = game.new_game(seed, rng=random.Random(seed + 91),
                                  mine_budget=99)
            guard = 0
            while state.status == game.ACTIVE and not state.is_cleared() and guard < 200:
                guard += 1
                position = solver.Position.from_state(state)
                analysis = solver.analyze(position)
                for cell in analysis.safe:
                    self.assertNotIn(cell, state.mine_cells,
                                     f"seed {seed}: called a mine safe")
                    checked += 1
                for cell in analysis.mines:
                    self.assertIn(cell, state.mine_cells)
                cell, _ = solver.safest_move(position, analysis)
                if game.reveal(state, *cell) == game.ALREADY:
                    self.fail("the solver chose a cell that was already open")
        self.assertGreater(checked, 2000)
        print(f"\n  verified {checked} safe claims across boards with detonations")
