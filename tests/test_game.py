"""Minesweeper rules: coordinates, placement, flood fill, win and loss."""

import random
import unittest

from helpers import make
import game


class Coordinates(unittest.TestCase):
    def test_round_trip(self):
        for r in range(9):
            for c in range(9):
                coord = game.index_to_coord(r, c)
                self.assertEqual(game.coord_to_index(coord), (r, c))

    def test_corners(self):
        self.assertEqual(game.index_to_coord(0, 0), "A1")
        self.assertEqual(game.index_to_coord(8, 8), "I9")
        self.assertEqual(game.coord_to_index("i9"), (8, 8))

    def test_rejects_garbage(self):
        for bad in ("", "A", "9", "ZZ"):
            with self.assertRaises(ValueError):
                game.coord_to_index(bad)


class Placement(unittest.TestCase):
    def test_opening_is_always_a_zero(self):
        """The bot's first click must never explode and never be a bare number.

        A crowd cannot reason about a blank grid, so the opening has to flood.
        """
        for seed in range(200):
            state = game.new_game(1, rng=random.Random(seed))
            self.assertEqual(state.status, game.ACTIVE)
            self.assertGreater(len(state.revealed), 1)
            opening = game.coord_to_index(state.last_coord)
            self.assertNotIn(opening, state.mine_cells)
            self.assertEqual(state.revealed[opening], 0)

    def test_mine_count_is_exact(self):
        for seed in range(50):
            state = game.new_game(1, mines=13, rng=random.Random(seed))
            self.assertEqual(len(state.mine_cells), 13)

    def test_refuses_impossible_density(self):
        with self.assertRaises(ValueError):
            game.new_game(1, rows=4, cols=4, mines=99)


class Reveal(unittest.TestCase):
    def test_flood_stops_at_numbers(self):
        # Single mine in the corner: opening the far corner floods the whole
        # board except the mine itself.
        state = make(4, 4, mines=[(0, 0)], revealed=[])
        self.assertEqual(game.reveal(state, 3, 3), game.SAFE)
        self.assertEqual(len(state.revealed), 15)
        self.assertEqual(state.revealed[(0, 1)], 1)
        self.assertEqual(state.revealed[(3, 3)], 0)

    def test_flood_never_opens_a_mine(self):
        for seed in range(50):
            state = game.new_game(1, rng=random.Random(seed))
            self.assertFalse(set(state.revealed) & state.mine_cells)

    def test_mine_ends_the_run(self):
        state = make(4, 4, mines=[(0, 0)], revealed=[(2, 2)])
        self.assertEqual(game.reveal(state, 0, 0), game.MINE)
        self.assertEqual(state.status, game.EXPLODED)
        self.assertEqual(state.exploded_cell, "A1")

    def test_reopening_is_a_no_op(self):
        state = make(4, 4, mines=[(0, 0)], revealed=[(2, 2)])
        self.assertEqual(game.reveal(state, 2, 2), game.ALREADY)

    def test_clearing_the_board_wins(self):
        state = make(3, 3, mines=[(0, 0)], revealed=[])
        game.reveal(state, 2, 2)
        self.assertEqual(state.status, game.CLEARED)
        self.assertTrue(state.is_cleared())
        self.assertEqual(len(state.revealed), state.total_safe)


if __name__ == "__main__":
    unittest.main()
