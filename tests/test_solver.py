"""Solver correctness.

The bot publishes the solver's conclusions as fact — it plays its own move
when the crowd doesn't agree, and after an explosion it says whether a
provably safe cell was available. So the bar here is soundness: a cell called
safe must never be a mine.
"""

import random
import unittest

from helpers import make
import game
import solver


def analyze(state):
    return solver.analyze(solver.Position.from_state(state))


class SingleCellRule(unittest.TestCase):
    def test_a_one_with_a_single_hidden_neighbour_is_a_mine(self):
        # Mine at A1. Open everything around it except A1 itself.
        state = make(3, 3, mines=[(0, 0)],
                     revealed=[(0, 1), (1, 0), (1, 1), (1, 2), (2, 0),
                               (2, 1), (2, 2), (0, 2)])
        result = analyze(state)
        self.assertIn((0, 0), result.mines)
        self.assertEqual(result.safe, set())

    def test_a_satisfied_number_frees_its_neighbours(self):
        # Mine at A1, and A2 is left hidden alongside it. B2 reads 1; once A1
        # is proven to be the mine, A2 must be safe.
        state = make(3, 3, mines=[(0, 0)],
                     revealed=[(1, 0), (1, 1), (1, 2), (2, 0), (2, 1),
                               (2, 2), (0, 2)])
        result = analyze(state)
        self.assertIn((0, 0), result.mines)
        self.assertIn((0, 1), result.safe)
        self.assertEqual(result.level, solver.TRIVIAL)


class SubsetRule(unittest.TestCase):
    def test_difference_of_nested_constraints(self):
        """{x,y} holds 1 and {x,y,z} holds 1, so z is safe.

        Exercised directly on the rule because the cell objects are
        irrelevant to it — this is the 1-1 pattern in its purest form.
        """
        cons = [(frozenset({"x", "y"}), 1), (frozenset({"x", "y", "z"}), 1)]
        safe, mines, _ = solver._subset(cons)
        self.assertEqual(safe, {"z"})
        self.assertEqual(mines, set())

    def test_difference_that_is_all_mines(self):
        cons = [(frozenset({"x", "y"}), 1), (frozenset({"x", "y", "z"}), 2)]
        safe, mines, _ = solver._subset(cons)
        self.assertEqual(mines, {"z"})


class GlobalMineCount(unittest.TestCase):
    def test_counting_out_the_last_mine_frees_the_rest(self):
        """The endgame only the global counter can solve.

        One mine total, already pinned by a number. Every other hidden cell is
        therefore safe — including cells nowhere near the frontier, which no
        amount of local reasoning would ever settle.
        """
        mines = [(0, 0)]
        # Open a band that pins A1, leaving the whole bottom row hidden and
        # untouched by any number.
        revealed = [(0, 1), (1, 0), (1, 1), (1, 2), (0, 2), (0, 3), (1, 3)]
        state = make(4, 4, mines=mines, revealed=revealed)
        result = analyze(state)
        self.assertIn((0, 0), result.mines)
        for cell in [(3, 0), (3, 1), (3, 2), (3, 3)]:
            self.assertIn(cell, result.safe,
                          f"{game.index_to_coord(*cell)} should be provably safe")

    def test_probabilities_sum_to_the_remaining_mines(self):
        state = game.new_game(1, rng=random.Random(3))
        result = analyze(state)
        if result.probs:
            total = sum(result.probs.values())
            self.assertAlmostEqual(total, state.mine_count, places=6)


class ForcedGuess(unittest.TestCase):
    def test_a_true_fifty_fifty_has_no_safe_cell(self):
        # A1 . A3 with one mine and a 1 in the middle: the mine is on one end
        # or the other, and nothing in the position can say which.
        state = make(1, 3, mines=[(0, 0)], revealed=[(0, 1)])
        result = analyze(state)
        self.assertEqual(result.safe, set())
        self.assertEqual(result.level, solver.GUESS)
        self.assertAlmostEqual(result.probs[(0, 0)], 0.5)
        self.assertAlmostEqual(result.probs[(0, 2)], 0.5)

    def test_counting_frees_the_cell_beyond_the_frontier(self):
        """The 50/50 uses up the last mine, so the far cell is safe.

        Locally A4 is a mystery; globally it cannot hold a mine, because the
        one mine on the board is already spoken for by the pair at the ends.
        """
        state = make(1, 4, mines=[(0, 0)], revealed=[(0, 1)])
        result = analyze(state)
        self.assertEqual(result.safe, {(0, 3)})
        self.assertAlmostEqual(result.probs[(0, 0)], 0.5)
        self.assertAlmostEqual(result.probs[(0, 2)], 0.5)
        self.assertAlmostEqual(result.probs[(0, 3)], 0.0)

    def test_safest_move_still_returns_something(self):
        state = make(1, 3, mines=[(0, 0)], revealed=[(0, 1)])
        cell, reason = solver.safest_move(solver.Position.from_state(state))
        self.assertIn(cell, [(0, 0), (0, 2)])
        self.assertIn("50%", reason)


class Soundness(unittest.TestCase):
    """The test that actually matters: never call a mine safe.

    Plays full boards, and at every position cross-checks every claim the
    solver makes against the hidden layout it cannot see.
    """

    def test_claims_hold_across_many_boards(self):
        checked_safe = checked_mines = positions = 0
        for seed in range(120):
            rng = random.Random(seed)
            state = game.new_game(seed, rng=rng)
            while state.status == game.ACTIVE:
                position = solver.Position.from_state(state)
                result = solver.analyze(position)
                positions += 1

                for cell in result.safe:
                    self.assertNotIn(
                        cell, state.mine_cells,
                        f"seed {seed}: called {game.index_to_coord(*cell)} safe, "
                        f"it is a mine")
                    checked_safe += 1
                for cell in result.mines:
                    self.assertIn(
                        cell, state.mine_cells,
                        f"seed {seed}: called {game.index_to_coord(*cell)} a "
                        f"mine, it is safe")
                    checked_mines += 1
                for cell, p in result.probs.items():
                    self.assertGreaterEqual(p, -1e-9)
                    self.assertLessEqual(p, 1 + 1e-9)

                cell, _ = solver.safest_move(position, result)
                game.reveal(state, *cell)

        self.assertGreater(checked_safe, 5000)
        self.assertGreater(checked_mines, 1000)
        print(f"\n  verified {checked_safe} safe and {checked_mines} mine "
              f"claims across {positions} positions")

    def test_a_provably_safe_cell_is_almost_always_available(self):
        """The premise of the whole project, measured rather than assumed."""
        with_safe = total = 0
        for seed in range(60):
            state = game.new_game(seed, rng=random.Random(seed + 500))
            while state.status == game.ACTIVE:
                position = solver.Position.from_state(state)
                result = solver.analyze(position)
                total += 1
                with_safe += bool(result.safe)
                cell, _ = solver.safest_move(position, result)
                game.reveal(state, *cell)
        share = with_safe / total
        print(f"\n  {share:.1%} of positions contained a provably safe cell")
        self.assertGreater(share, 0.95)


if __name__ == "__main__":
    unittest.main()
