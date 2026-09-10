"""Difficulty tiers, and the public copy that announces them.

Tiers raise board size and mine density together. Size alone makes a board
longer and shallower — at fixed density a bigger board is proportionally more
open interior and less frontier, so more of its turns are trivially
decidable. Simulated over 150 boards each at 30-minute turns, a 12x12 at the
starting density runs 43 hours and contains 0.8 turns needing real deduction,
against 1.5 on the 9x9 it grew from.
"""

import unittest
from datetime import datetime, timezone

from helpers import config, game, main


class Tiers(unittest.TestCase):
    def setUp(self):
        self.saved = {name: getattr(config, name) for name in (
            "ADAPTIVE_BOARD", "ROWS", "COLS", "MINES", "TIERS",
            "GROW_PARTICIPATION", "SHRINK_PARTICIPATION")}
        config.ADAPTIVE_BOARD = True
        config.ROWS = config.COLS = 9
        config.MINES = 13
        config.TIERS = [(9, 9, 13), (10, 10, 18), (11, 11, 24)]
        config.GROW_PARTICIPATION = .75
        config.SHRINK_PARTICIPATION = .50

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(config, name, value)

    def board(self, tier=0, status=game.CLEARED, turns=10):
        rows, cols, mines = config.TIERS[tier]
        state = game.new_game(1, rows=rows, cols=cols, mines=mines)
        state.status = status
        state.turn_number = turns
        return state

    # -- which rung a board is on ------------------------------------------

    def test_tier_is_derived_from_the_board_itself(self):
        for index in range(len(config.TIERS)):
            self.assertEqual(main.tier_of(self.board(index)), index)

    def test_an_unknown_size_maps_to_the_nearest_rung(self):
        """A hand-set size in .env must not silently reset to the bottom.

        Nearest by area: a 12x12 (144) sits closest to the 11x11 top rung
        (121), and an 8x8 (64) to the 9x9 bottom one (81).
        """
        self.assertEqual(main.tier_of(game.new_game(1, rows=12, cols=12,
                                                    mines=30)), 2)
        self.assertEqual(main.tier_of(game.new_game(1, rows=8, cols=8,
                                                    mines=10)), 0)

    def test_no_previous_board_starts_at_the_bottom(self):
        self.assertEqual(main.next_board_settings(None), config.TIERS[0])

    # -- promotion and demotion --------------------------------------------

    def test_a_well_played_win_promotes(self):
        self.assertEqual(main.next_board_settings(self.board(0), 8),
                         config.TIERS[1])

    def test_a_win_the_bot_carried_does_not_promote(self):
        self.assertEqual(main.next_board_settings(self.board(0), 6),
                         config.TIERS[0])

    def test_losing_a_hard_board_holds_the_tier(self):
        """Losing is the game working. Only absent players demote a board."""
        lost = self.board(2, status=game.EXPLODED)
        self.assertEqual(main.next_board_settings(lost, 8), config.TIERS[2])

    def test_a_board_nobody_played_demotes(self):
        self.assertEqual(main.next_board_settings(self.board(2), 3),
                         config.TIERS[1])

    def test_the_ladder_is_bounded_at_both_ends(self):
        top = self.board(len(config.TIERS) - 1)
        self.assertEqual(main.next_board_settings(top, 10), config.TIERS[-1])
        self.assertEqual(main.next_board_settings(self.board(0), 0),
                         config.TIERS[0])

    def test_it_can_be_switched_off(self):
        config.ADAPTIVE_BOARD = False
        self.assertEqual(main.next_board_settings(self.board(0), 10),
                         (config.ROWS, config.COLS, config.MINES))

    def test_hysteresis_leaves_the_middle_alone(self):
        # Between the two thresholds nothing moves, so participation hovering
        # near the boundary does not seesaw the board every game.
        for crowd in (6, 7):
            self.assertEqual(main.next_board_settings(self.board(1), crowd),
                             config.TIERS[1])

    # -- reward scales with the stakes -------------------------------------

    def test_the_multiplier_rises_with_the_tier(self):
        for index in range(len(config.TIERS)):
            self.assertEqual(main.tier_multiplier(self.board(index)), index + 1)

    def test_config_parses_a_tier_string(self):
        self.assertEqual(config._parse_tiers("9x9:13, 10x10:18"),
                         [(9, 9, 13), (10, 10, 18)])


class CopyFitsInBothModes(unittest.TestCase):
    """Every public post, at every tier, in both modes.

    These builders read config at call time, and the suite used to inherit
    whatever .env happened to say — so a laptop with no .env tested voting
    mode only and reported a clean run for both. The Mini, running
    KNOCKOUT=1, found a 312-character opening post in seconds. Modes are now
    set explicitly here rather than inherited.
    """

    def setUp(self):
        self.saved = {n: getattr(config, n) for n in ("KNOCKOUT", "TIERS")}
        config.TIERS = [(9, 9, 13), (10, 10, 18), (11, 11, 24)]

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(config, name, value)

    def _boards(self):
        for index, (rows, cols, mines) in enumerate(config.TIERS):
            yield index, game.new_game(1, rows=rows, cols=cols, mines=mines,
                                       mine_budget=main.mine_budget_for(index))

    def test_the_opening_post_fits_in_both_modes_at_every_tier(self):
        record = {"played": 120, "cleared": 88, "exploded": 32}
        for knockout in (False, True):
            config.KNOCKOUT = knockout
            for index, state in self._boards():
                text = main.build_opening_text(state, record)
                self.assertLessEqual(
                    len(text), 300,
                    f"knockout={knockout} tier={index + 1}: {len(text)} chars")

    def test_the_opening_post_always_keeps_what_matters(self):
        """Trimming may drop the all-time record; it may not drop the ask."""
        record = {"played": 999, "cleared": 888, "exploded": 111}
        for knockout in (False, True):
            config.KNOCKOUT = knockout
            for index, state in self._boards():
                text = main.build_opening_text(state, record)
                self.assertIn("NEW BOARD", text)
                self.assertIn("min.", text, "the prompt must survive trimming")

    def test_turn_posts_fit_in_both_modes_at_every_tier(self):
        for knockout in (False, True):
            config.KNOCKOUT = knockout
            for index, state in self._boards():
                state.turn_number = 37
                if knockout:
                    plays = [main.Play(f"did:{i}", f"aratherlonghandle{i}.bsky.social",
                                       "C3", game.MINE if i == 0 else game.SAFE, 9)
                             for i in range(3)]
                    text = main.build_knockout_turn_text(state, plays)
                else:
                    text = main.build_turn_text(state, "D4", None, "bot")
                self.assertLessEqual(len(text), 300,
                                     f"knockout={knockout} tier={index + 1}")

    def test_gameover_posts_fit_in_both_modes_at_every_tier(self):
        record = {"cleared": 88, "exploded": 32, "played": 120}
        mvp = "🏅 Board MVP: @aratherlonghandle.bsky.social — 240 points."
        flags = "🚩 @anotherlonghandle.bsky.social called 9 of 24 mines."
        for knockout in (False, True):
            config.KNOCKOUT = knockout
            for index, state in self._boards():
                state.status = game.CLEARED
                state.turn_number = 41
                state.detonations = 3
                if knockout:
                    text = main.build_knockout_gameover_text(
                        state, record, survivors=2, entrants=9,
                        mvp_line=mvp, next_board_at=None)
                else:
                    text = main.build_gameover_text(
                        state, "D4", None, "bot", "", record, crowd_moves=38,
                        flag_line=flags, mvp_line=mvp, next_board_at=None)
                self.assertLessEqual(len(text), 300,
                                     f"knockout={knockout} tier={index + 1}")


class PublicCopy(unittest.TestCase):
    def setUp(self):
        self._knockout = config.KNOCKOUT
        config.KNOCKOUT = False        # explicit, not inherited from .env

    def tearDown(self):
        config.KNOCKOUT = self._knockout

    def test_the_opening_post_names_the_tier_and_the_reward(self):
        rows, cols, mines = config.TIERS[-1]
        state = game.new_game(1, rows=rows, cols=cols, mines=mines)
        text = main.build_opening_text(state, {"played": 3, "cleared": 2,
                                               "exploded": 1})
        self.assertIn(f"Tier {len(config.TIERS)}", text)
        self.assertIn("x points", text)
        self.assertLessEqual(len(text), 300)

    def test_the_first_tier_does_not_advertise_a_1x_multiplier(self):
        rows, cols, mines = config.TIERS[0]
        text = main.build_opening_text(
            game.new_game(1, rows=rows, cols=cols, mines=mines),
            {"played": 0, "cleared": 0, "exploded": 0})
        self.assertNotIn("1x points", text)

    def test_every_tier_opens_within_the_limit(self):
        for rows, cols, mines in config.TIERS:
            text = main.build_opening_text(
                game.new_game(1, rows=rows, cols=cols, mines=mines),
                {"played": 12, "cleared": 9, "exploded": 3})
            self.assertLessEqual(len(text), 300, f"{rows}x{cols}")

    def test_tutorial_matches_real_rules(self):
        text = main.build_tutorial_text(game.new_game(1))
        self.assertIn("Flag mines: flag C3", text)
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
            mvp_line="🏅 Board MVP: @a.test — 12 points.",
            next_board_at=datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc))
        self.assertIn("Board MVP", text)
        self.assertIn("Follow to catch the opening", text)
        self.assertIn("Next board:", text)
        self.assertLessEqual(len(text), 300)


if __name__ == "__main__":
    unittest.main()
