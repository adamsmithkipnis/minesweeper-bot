"""Points: what a move earns, and what the caller is told about it.

A move scores the cells it opened, so a blank that cascades is worth far more
than a single numbered cell and a mine is worth nothing. The score only ever
reaches anyone through the credit reply, so the wording is tested too.
"""

import os
import tempfile
import unittest

from helpers import config, db, game
import main


def a_move(state, did, points, result="safe"):
    db.record_move(state, "A1", result, "crowd", caller=f"{did}.test",
                   did=did, points=points)


class Points(unittest.TestCase):
    def setUp(self):
        # config.DB_PATH is read at import, so point the module at the temp
        # file directly — setting the environment variable here is too late.
        self.tmp = tempfile.mkdtemp()
        self._saved = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp, "score.db")
        db.init_db()
        self.state = game.new_game(1)

    def tearDown(self):
        config.DB_PATH = self._saved

    def test_points_accumulate_within_a_game(self):
        a_move(self.state, "did:a", 8)
        a_move(self.state, "did:a", 3)
        self.assertEqual(db.player_points("did:a", 1), 11)

    def test_all_time_spans_games_but_this_game_does_not(self):
        a_move(self.state, "did:a", 8)
        later = game.new_game(2)
        a_move(later, "did:a", 5)
        self.assertEqual(db.player_points("did:a", 1), 8)
        self.assertEqual(db.player_points("did:a", 2), 5)
        self.assertEqual(db.player_points("did:a"), 13)

    def test_players_are_scored_separately(self):
        a_move(self.state, "did:a", 8)
        a_move(self.state, "did:b", 2)
        self.assertEqual(db.player_points("did:a"), 8)
        self.assertEqual(db.player_points("did:b"), 2)

    def test_a_mine_scores_nothing(self):
        a_move(self.state, "did:a", 0, result="mine")
        self.assertEqual(db.player_points("did:a"), 0)

    def test_bot_moves_have_no_owner_and_score_nobody(self):
        db.record_move(self.state, "A1", "safe", "bot")
        self.assertEqual(db.player_points(""), 0)

    def test_leaderboard_ranks_by_points(self):
        a_move(self.state, "did:a", 3)
        a_move(self.state, "did:b", 9)
        board = db.leaderboard()
        self.assertEqual([r["did"] for r in board][:2], ["did:b", "did:a"])


class CreditReply(unittest.TestCase):
    class _Vote:
        votes = 3
        total_voters = 5
        caller = None
        caller_handle = "someone.test"

    def setUp(self):
        self.state = game.new_game(1)
        self.state.turn_number = 12

    def text(self, points, this_game, all_time):
        return main.build_credit_reply(self.state, "A1", self._Vote(),
                                       points, this_game, all_time)

    def test_shows_the_points_and_both_totals(self):
        out = self.text(8, 24, 123)
        self.assertIn("+8 points", out)
        self.assertIn("This game: 24", out)
        self.assertIn("All time: 123", out)

    def test_one_point_is_singular(self):
        self.assertIn("+1 point\n", self.text(1, 1, 1))

    def test_no_points_line_when_nothing_opened(self):
        out = self.text(0, 24, 123)
        self.assertNotIn("+0", out)
        self.assertIn("All time: 123", out)

    def test_fits_the_post_limit_even_with_big_numbers(self):
        self.assertLessEqual(len(self.text(81, 9999, 999999)), 300)


if __name__ == "__main__":
    unittest.main()
