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


class Explanations(unittest.TestCase):
    """The bot says *why* its cell is safe, so the sentence must be true.

    Each case pins the cheapest justification, because that is the one a
    reader can check against the board by eye.
    """

    def explain(self, state, cell):
        position = solver.Position.from_state(state)
        return solver.explain(position, cell, solver.analyze(position))

    def test_a_satisfied_number_is_named_with_its_mine(self):
        # Mine at A1; B2 reads 1 and A1 is its only possible mine, so A2 is
        # clear because the 1 at B2 is already spoken for.
        state = make(3, 3, mines=[(0, 0)],
                     revealed=[(1, 0), (1, 1), (1, 2), (2, 0), (2, 1),
                               (2, 2), (0, 2)])
        why = self.explain(state, (0, 1))
        self.assertIn("already has its mine at A1", why)
        self.assertIn("A2 is clear", why)
        self.assertRegex(why, r"The 1 at (A3|B1|B2)")

    def test_a_number_with_several_mines_lists_them(self):
        # Mines in all four corners of a 3x3 with A2 the only other hidden
        # cell: every number next to A2 is a 2 or the 4, so the cheapest
        # justification has two mines to list.
        state = make(3, 3, mines=[(0, 0), (0, 2), (2, 0), (2, 2)],
                     revealed=[(1, 0), (1, 1), (1, 2), (2, 1)])
        why = self.explain(state, (0, 1))
        self.assertRegex(why, r"The 2 at (B1|B3) already has all 2 of its "
                              r"mines \((A1 and C1|A3 and C3)\), so A2 is clear")

    def test_the_subset_rule_names_both_numbers(self):
        # 1-1 pattern along a wall. B1 sees {A1, A2}: 1 and B2 sees
        # {A1, A2, A3}: 1, so A3 is clear — but which of A1/A2 holds the
        # mine stays open, and B3's mine could be A4 or B4, so no number
        # beside A3 is satisfied and only the pair of numbers explains it.
        state = make(2, 5, mines=[(0, 0), (0, 3)],
                     revealed=[(1, 0), (1, 1), (1, 2)])
        why = self.explain(state, (0, 2))
        self.assertEqual(why, "The 1 at B2 gets every mine it still needs "
                              "from the cells it shares with the 1 at B1, "
                              "so A3 is clear.")

    def test_enumeration_gets_the_honest_catch_all(self):
        # Only the global count frees A4: one mine, pinned to A1 or A3.
        state = make(1, 4, mines=[(0, 0)], revealed=[(0, 1)])
        why = self.explain(state, (0, 3))
        self.assertIn("Every way the remaining mines can fit", why)
        self.assertIn("A4 clear", why)

    def test_a_guess_has_no_explanation(self):
        state = make(1, 3, mines=[(0, 0)], revealed=[(0, 1)])
        self.assertEqual(self.explain(state, (0, 0)), "")

    def test_the_named_number_really_is_satisfied(self):
        """Across random boards, whenever the sentence names a number and its
        mines, that number must sit next to exactly those mines."""
        import re
        checked = 0
        for seed in range(200):
            state = game.new_game(1, rng=random.Random(seed))
            position = solver.Position.from_state(state)
            analysis = solver.analyze(position)
            for cell in sorted(analysis.safe)[:3]:
                why = solver.explain(position, cell, analysis)
                m = re.match(r"The (\d) at (\w\d+) already has (?:its mine at|"
                             r"all \d of its mines \()(.+?)\)?, so", why)
                if not m:
                    continue
                number, at = int(m.group(1)), game.coord_to_index(m.group(2))
                named = {game.coord_to_index(c.strip())
                         for c in m.group(3).replace(" and ", ", ").split(",")}
                self.assertEqual(state.revealed[at], number)
                self.assertEqual(len(named), number)
                for mine in named:
                    self.assertIn(mine, state.mine_cells)
                    self.assertIn(mine, set(game.neighbors(
                        at[0], at[1], state.rows, state.cols)))
                checked += 1
        self.assertGreater(checked, 50)
