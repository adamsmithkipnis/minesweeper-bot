"""Knockout play: everyone acts, a mine costs the player not the board.

Letting everyone act is ruinous under sudden death — with N players each
independently risking a mine, survival per turn is skill^N, so at the 87.5%
accuracy this game actually shows, five players clear 52% of boards in five
turns, and it gets *worse* as the audience grows. Making a mine cost the
player instead of the board inverts that: eight players clear 98%.
"""

import os
import random
import tempfile
import unittest

from helpers import config, db, game, main, votes

try:
    import renderer
    HAS_PILLOW = True
except ImportError:                                   # pragma: no cover
    HAS_PILLOW = False


def reply(did, text, when="1"):
    return votes.Reply(did=did, handle=f"{did}.bsky.social", text=text,
                       uri=f"at://{did}/x", cid="c", created_at=when)


class Budget(unittest.TestCase):
    def board(self, budget=2, seed=3):
        return game.new_game(1, rng=random.Random(seed), mine_budget=budget)

    def test_the_board_absorbs_its_budget_then_fails(self):
        state = self.board(budget=2)
        mines = sorted(state.mine_cells)
        for i, cell in enumerate(mines[:2]):
            self.assertEqual(game.reveal(state, *cell), game.MINE)
            self.assertEqual(state.status, game.ACTIVE,
                             f"board should survive detonation {i + 1}")
        game.reveal(state, *mines[2])
        self.assertEqual(state.status, game.EXPLODED)
        self.assertEqual(state.detonations, 3)

    def test_budget_zero_is_the_original_game(self):
        state = self.board(budget=0)
        game.reveal(state, *sorted(state.mine_cells)[0])
        self.assertEqual(state.status, game.EXPLODED)

    def test_spares_left_counts_down(self):
        state = self.board(budget=2)
        self.assertEqual(state.spares_left, 2)
        game.reveal(state, *sorted(state.mine_cells)[0])
        self.assertEqual(state.spares_left, 1)

    def test_a_spent_mine_cannot_be_hit_twice(self):
        state = self.board(budget=3)
        cell = sorted(state.mine_cells)[0]
        self.assertEqual(game.reveal(state, *cell), game.MINE)
        self.assertEqual(game.reveal(state, *cell), game.ALREADY)
        self.assertEqual(state.detonations, 1, "a second hit must not re-charge")

    def test_spent_mines_do_not_count_towards_clearing(self):
        """They are revealed but they are not safe cells."""
        state = self.board(budget=99)
        for cell in sorted(state.mine_cells)[:5]:
            game.reveal(state, *cell)
        self.assertNotIn(sorted(state.mine_cells)[0], state.revealed)
        self.assertFalse(state.is_cleared())

    def test_budget_scales_with_the_tier(self):
        saved = config.KNOCKOUT
        try:
            config.KNOCKOUT = True
            self.assertEqual([main.mine_budget_for(t) for t in range(3)],
                             [1, 2, 3])
            config.KNOCKOUT = False
            self.assertEqual(main.mine_budget_for(2), 0,
                             "sudden death when knockout is off")
        finally:
            config.KNOCKOUT = saved


class Moves(unittest.TestCase):
    def test_one_move_per_player_their_latest(self):
        got = votes.moves([reply("a", "D4", "1"), reply("b", "E5", "2"),
                           reply("a", "actually F6", "3")], set(), 9, 9)
        self.assertEqual([(m.did, m.coord) for m in got],
                         [("a", "F6"), ("b", "E5")])

    def test_knocked_out_players_do_not_act(self):
        got = votes.moves([reply("a", "D4"), reply("b", "E5", "2")],
                          set(), 9, 9, exclude={"a"})
        self.assertEqual([m.did for m in got], ["b"])

    def test_a_flag_only_reply_is_not_a_move(self):
        self.assertEqual(votes.moves([reply("a", "flag C3")], set(), 9, 9), [])

    def test_knocked_out_players_can_still_flag(self):
        """Elimination is a change of role, not an exit."""
        flags, _ = votes.collect_flags([reply("a", "flag C3")], set(), 9, 9)
        self.assertEqual(sorted(flags), ["C3"])


class Persistence(unittest.TestCase):
    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "knockout.db")
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_knockout_state_survives_a_reload(self):
        """Without this the board reloads with budget 0 every turn and the
        first mine ends it — which is exactly what happened first time."""
        state = game.new_game(1, rng=random.Random(3), mine_budget=2)
        game.reveal(state, *sorted(state.mine_cells)[0])
        db.save_state(state)
        back = db.load_state()
        self.assertEqual(back.mine_budget, 2)
        self.assertEqual(back.detonations, 1)
        self.assertEqual(back.spent_mines, state.spent_mines)
        self.assertEqual(back.spares_left, 1)

    def test_eliminations_are_recorded_once_per_board(self):
        for _ in range(3):
            db.eliminate(1, "did:a", "a", "C3", 4)
        self.assertEqual(db.eliminated(1), {"did:a"})
        self.assertEqual(len(db.eliminations(1)), 1)

    def test_eliminations_are_per_board(self):
        db.eliminate(1, "did:a", "a", "C3", 4)
        self.assertEqual(db.eliminated(2), set())


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class Copy(unittest.TestCase):
    def setUp(self):
        self._old = config.KNOCKOUT
        config.KNOCKOUT = True

    def tearDown(self):
        config.KNOCKOUT = self._old

    def _state(self):
        return game.new_game(1, rng=random.Random(5), mine_budget=2)

    def test_a_turn_post_names_the_knockout_and_fits(self):
        state = self._state()
        state.turn_number = 7
        plays = [main.Play("did:a", "alice.bsky.social", "C3", game.MINE),
                 main.Play("did:b", "bob.bsky.social", "D4", game.SAFE, 5)]
        text = main.build_knockout_turn_text(state, plays)
        self.assertIn("hit C3 and is out", text)
        self.assertIn("@bob.bsky.social +5", text)
        self.assertLessEqual(len(text), 300)

    def test_a_turn_post_stays_within_the_limit_when_crowded(self):
        state = self._state()
        state.turn_number = 12
        plays = [main.Play(f"did:{i}", f"averyverylongplayerhandle{i}.bsky.social",
                           "C3", game.MINE) for i in range(3)]
        self.assertLessEqual(len(main.build_knockout_turn_text(state, plays)), 300)

    def test_the_prompt_does_not_mention_voting(self):
        text = main.build_knockout_turn_text(
            self._state(), [main.Play("did:a", "a", "D4", game.SAFE, 1)])
        self.assertNotIn("vote", text.lower())
        self.assertIn("Everyone who replies", text)

    def test_the_ending_reports_survivors(self):
        state = self._state()
        state.status = game.CLEARED
        state.turn_number = 14
        state.detonations = 3
        text = main.build_knockout_gameover_text(
            state, {"cleared": 2, "exploded": 1}, survivors=4, entrants=7,
            mvp_line="🏅 Board MVP: @a.test — 30 points.", next_board_at=None)
        self.assertIn("4 of 7 still standing", text)
        self.assertIn("3 mines found the hard way", text)
        self.assertLessEqual(len(text), 300)

    def test_a_lost_board_says_so(self):
        state = self._state()
        state.status = game.EXPLODED
        state.exploded_cell = "G7"
        state.turn_number = 9
        text = main.build_knockout_gameover_text(
            state, {"cleared": 1, "exploded": 2}, survivors=1, entrants=5,
            mvp_line="", next_board_at=None)
        self.assertIn("BOARD LOST", text)
        self.assertIn("G7", text)
        self.assertLessEqual(len(text), 300)


if __name__ == "__main__":
    unittest.main()
