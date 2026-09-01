"""Board rendering and alt text.

The load-bearing test here is the leak guarantee: while a game is live, the
published board must not contain the mines in any form. That is the reason
this project exists rather than a third Battleship account.
"""

import random
import unittest

from helpers import game, solver

try:
    import renderer
    HAS_PILLOW = True
except ImportError:                                   # pragma: no cover
    HAS_PILLOW = False


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class DisplayGrid(unittest.TestCase):
    def test_a_live_board_never_shows_a_mine(self):
        """Play many boards and check every published grid, every turn."""
        for seed in range(40):
            state = game.new_game(seed, rng=random.Random(seed))
            while state.status == game.ACTIVE:
                grid = renderer.display_grid(state)
                flat = [token for row in grid for token in row]
                self.assertNotIn(renderer.MINE, flat)
                self.assertNotIn(renderer.EXPLODED, flat)
                # And every hidden mine really is rendered as hidden.
                for (r, c) in state.mine_cells:
                    self.assertEqual(grid[r][c], renderer.HIDDEN)

                position = solver.Position.from_state(state)
                cell, _ = solver.safest_move(position)
                game.reveal(state, *cell)

    def test_alt_text_never_leaks_either(self):
        for seed in range(40):
            state = game.new_game(seed, rng=random.Random(seed + 900))
            while state.status == game.ACTIVE:
                alt = renderer.build_alt_text(state)
                board = alt.split("\n")[2:2 + state.rows]
                self.assertNotIn("*", " ".join(board))
                position = solver.Position.from_state(state)
                cell, _ = solver.safest_move(position)
                game.reveal(state, *cell)

    def test_the_end_of_a_run_shows_every_mine(self):
        state = game.new_game(1, rng=random.Random(3))
        mine = sorted(state.mine_cells)[0]
        game.reveal(state, *mine)
        self.assertEqual(state.status, game.EXPLODED)

        grid = renderer.display_grid(state)
        self.assertEqual(grid[mine[0]][mine[1]], renderer.EXPLODED)
        for cell in state.mine_cells:
            if cell != mine:
                self.assertEqual(grid[cell[0]][cell[1]], renderer.MINE)

    def test_grid_matches_the_revealed_numbers(self):
        state = game.new_game(1, rng=random.Random(12))
        grid = renderer.display_grid(state)
        for (r, c), number in state.revealed.items():
            self.assertEqual(grid[r][c], str(number))


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class AltText(unittest.TestCase):
    def test_carries_the_whole_board_and_stays_short(self):
        """A 9x9 position fits, so alt text is the position, not a summary."""
        state = game.new_game(1, rng=random.Random(7))
        alt = renderer.build_alt_text(state)
        lines = alt.split("\n")
        rows = [ln for ln in lines if ln[:2] in
                [f"{letter}:" for letter in game.row_letters(state.rows)]]
        self.assertEqual(len(rows), state.rows)
        for line in rows:
            self.assertEqual(len(line.split(": ")[1].split(" ")), state.cols)
        self.assertLess(len(alt), 700)
        self.assertLessEqual(len(alt), renderer.ALT_LIMIT)

    def test_mentions_the_last_move(self):
        state = game.new_game(1, rng=random.Random(7))
        self.assertIn(state.last_coord, renderer.build_alt_text(state))

    def test_says_when_a_mine_was_hit(self):
        state = game.new_game(1, rng=random.Random(3))
        mine = sorted(state.mine_cells)[0]
        state.last_coord = game.index_to_coord(*mine)
        state.last_result = game.reveal(state, *mine)
        alt = renderer.build_alt_text(state)
        self.assertIn("hit a mine", alt)
        self.assertIn("X is the mine that was hit", alt)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class Image(unittest.TestCase):
    def test_renders_a_png(self):
        state = game.new_game(1, rng=random.Random(7))
        png = renderer.render_board(state, highlight=state.last_coord)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertLess(len(png), 900_000)      # Bluesky's upload ceiling is 1MB

    def test_highlight_of_a_bad_coordinate_is_ignored(self):
        state = game.new_game(1, rng=random.Random(7))
        self.assertTrue(renderer.render_board(state, highlight="Z99"))
        self.assertTrue(renderer.render_board(state, highlight=""))


if __name__ == "__main__":
    unittest.main()
