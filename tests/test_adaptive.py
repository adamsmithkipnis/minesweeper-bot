"""Adaptive board sizing and public tutorial/game-over copy."""

import unittest
from datetime import datetime, timezone

from helpers import config, game, main


class AdaptiveBoard(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: getattr(config, name) for name in (
                "ADAPTIVE_BOARD", "ROWS", "COLS", "MINES",
                "MIN_BOARD_SIZE", "MAX_BOARD_SIZE",
                "GROW_PARTICIPATION", "SHRINK_PARTICIPATION")
        }
        config.ADAPTIVE_BOARD = True
        config.ROWS = config.COLS = 9
        config.MINES = 13
        config.MIN_BOARD_SIZE = 7
        config.MAX_BOARD_SIZE = 12
        config.GROW_PARTICIPATION = .75
        config.SHRINK_PARTICIPATION = .50

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(config, name, value)

    def state(self, size=9, status=game.CLEARED, turns=10):
        state = game.new_game(1, rows=size, cols=size,
                              mines=max(1, round(size * size * 13 / 81)))
        state.status = status
        state.turn_number = turns
        return state

    def test_well_participated_win_grows(self):
        self.assertEqual(main.next_board_settings(self.state(), 8), (10, 10, 16))

    def test_win_without_enough_participation_stays_same(self):
        self.assertEqual(main.next_board_settings(self.state(), 6), (9, 9, 13))

    def test_low_participation_shrinks_even_after_loss(self):
        old = self.state(status=game.EXPLODED)
        self.assertEqual(main.next_board_settings(old, 4), (8, 8, 10))

    def test_bounds_are_enforced(self):
        self.assertEqual(main.next_board_settings(self.state(12), 10)[:2], (12, 12))
        self.assertEqual(main.next_board_settings(
            self.state(7, status=game.EXPLODED), 0)[:2], (7, 7))


class PublicCopy(unittest.TestCase):
    def test_tutorial_matches_real_rules(self):
        text = main.build_tutorial_text(game.new_game(1))
        self.assertIn("Flag mines: flag C3", text)
        self.assertIn("several are okay", text)
        self.assertIn("latest choice counts", text)
        self.assertIn("With 3+ voters, 2 must agree", text)
        self.assertLessEqual(len(text), 300)

    def test_gameover_acquires_and_gives_exact_time(self):
        state = game.new_game(1)
        state.status = game.CLEARED
        state.turn_number = 12
        text = main.build_gameover_text(
            state, "D4", None, "bot", "",
            {"cleared": 2, "exploded": 1}, crowd_moves=9,
            mvp_line="🏅 Board MVP: @a.test — 12 cells opened.",
            next_board_at=datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc))
        self.assertIn("Board MVP", text)
        self.assertIn("Follow to catch the opening", text)
        self.assertIn("Next board:", text)
        self.assertLessEqual(len(text), 300)


if __name__ == "__main__":
    unittest.main()
