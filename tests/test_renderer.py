"""Board rendering and alt text.

The load-bearing test here is the leak guarantee: while a game is live, the
published board must not contain the mines in any form. That is the reason
this project exists rather than a third Battleship account.
"""

import random
import unittest
from datetime import date

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

    def test_knockout_shows_spent_mines_and_only_spent_ones(self):
        """A detonated mine is public — everyone watched it go off.

        The guarantee that matters is unchanged: a mine nobody has hit is
        still drawn as an ordinary unopened tile.
        """
        for seed in range(30):
            state = game.new_game(seed, rng=random.Random(seed + 700),
                                  mine_budget=99)
            for cell in sorted(state.mine_cells)[:4]:
                game.reveal(state, *cell)
            self.assertEqual(state.status, game.ACTIVE)

            grid = renderer.display_grid(state)
            for (r, c) in state.mine_cells:
                expected = (renderer.EXPLODED if (r, c) in state.spent_mines
                            else renderer.HIDDEN)
                self.assertEqual(grid[r][c], expected)
            self.assertNotIn(renderer.MINE,
                             [t for row in grid for t in row])

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


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class Holidays(unittest.TestCase):
    def test_easter_lands_on_the_published_sundays(self):
        for day in (date(2024, 3, 31), date(2025, 4, 20), date(2026, 4, 5),
                    date(2027, 3, 28), date(2038, 4, 25)):
            self.assertEqual(renderer.easter_sunday(day.year), day)
            self.assertEqual(day.weekday(), 6)

    def test_thanksgiving_is_the_fourth_thursday(self):
        for day in (date(2026, 11, 26), date(2027, 11, 25), date(2028, 11, 23),
                    date(2030, 11, 28)):
            self.assertEqual(renderer.thanksgiving(day.year), day)

    def test_each_costume_has_its_window_and_no_more(self):
        H = renderer
        cases = {
            date(2026, 10, 24): None,
            date(2026, 10, 25): H.HALLOWEEN,
            date(2026, 10, 31): H.HALLOWEEN,
            date(2026, 11, 1): None,
            date(2026, 11, 26): H.THANKSGIVING,
            date(2026, 11, 27): None,
            date(2026, 12, 17): None,
            date(2026, 12, 18): H.CHRISTMAS,
            date(2026, 12, 25): H.CHRISTMAS,
            date(2026, 12, 26): None,
            date(2026, 12, 31): H.NEW_YEAR,
            date(2027, 1, 1): H.NEW_YEAR,
            date(2027, 1, 2): None,
            date(2027, 2, 6): H.LUNAR_NEW_YEAR,
            date(2027, 2, 14): H.VALENTINES,
            date(2027, 3, 14): H.PI_DAY,
            date(2027, 3, 17): H.ST_PATRICKS,
            date(2027, 3, 27): None,
            date(2027, 3, 28): H.EASTER,
            date(2027, 3, 29): None,
            date(2027, 4, 1): H.APRIL_FOOLS,
            date(2027, 7, 4): None,
            date(2027, 8, 13): H.FRIDAY_13TH,
            date(2027, 9, 1): H.BIRTHDAY,
            date(2027, 9, 13): None,            # a Monday the 13th
        }
        for day, expected in cases.items():
            self.assertEqual(renderer.holiday(day), expected, day)

    def test_easter_outranks_april_fools(self):
        self.assertEqual(renderer.easter_sunday(2029), date(2029, 4, 1))
        self.assertEqual(renderer.holiday(date(2029, 4, 1)), renderer.EASTER)

    def test_lunar_new_year_table_is_all_in_its_season(self):
        for year, day in renderer.LUNAR_NEW_YEARS.items():
            self.assertEqual(day.year, year)
            self.assertTrue(date(year, 1, 21) <= day <= date(year, 2, 20), day)

    def test_april_fools_turns_only_the_face_over(self):
        state = game.new_game(1, rng=random.Random(7))
        plain = renderer.render_board(state, day=date(2027, 7, 4))
        fools = renderer.render_board(state, day=date(2027, 4, 1))
        self.assertNotEqual(plain, fools)

    def test_every_costume_renders_in_every_mood(self):
        state = game.new_game(1, rng=random.Random(7))
        plain = renderer.render_board(state, day=date(2026, 7, 4))
        days = [date(2026, 10, 31), date(2026, 12, 25), date(2026, 4, 5),
                date(2027, 1, 1), date(2027, 2, 14), date(2027, 3, 17),
                date(2027, 4, 1), date(2027, 3, 14), date(2027, 8, 13),
                date(2026, 11, 26), date(2027, 2, 6), date(2027, 9, 1)]
        self.assertEqual(len({renderer.holiday(d) for d in days}), 12)
        for day in days:
            dressed = renderer.render_board(state, day=day)
            self.assertNotEqual(dressed, plain, day)
            for status in (game.CLEARED, game.EXPLODED):
                state.status = status
                self.assertTrue(renderer.render_board(state, day=day))
            state.status = game.ACTIVE


if __name__ == "__main__":
    unittest.main()
