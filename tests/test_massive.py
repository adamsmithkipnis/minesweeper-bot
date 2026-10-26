"""Massive Monday: the weekly big board.

The bar is: it starts once, on the right local day, at the right size for
the week of the month; it never runs in voting mode; the regular ladder
resumes exactly where it would have been; and a 26x26 board still fits in
every post, its alt text and its image.
"""

import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from helpers import config, db, game, main
import renderer

PACIFIC = ZoneInfo("America/Los_Angeles")

# November 2026: Mondays fall on the 2nd, 9th, 16th, 23rd and 30th.
MONDAYS = [datetime(2026, 11, d, 9, 0, tzinfo=PACIFIC)
           for d in (2, 9, 16, 23, 30)]


class _Config(unittest.TestCase):
    def setUp(self):
        self._saved = (config.DB_PATH, config.KNOCKOUT, config.MASSIVE_DAY,
                       config.MASSIVE_BOARDS, config.MASSIVE_MINE_BUDGET,
                       config.TIERS, config.ADAPTIVE_BOARD,
                       config.LEADERBOARD_TZ, config.POST_MODE,
                       config.DRY_DIR, config.MASSIVE_START)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "massive.db")
        config.KNOCKOUT = True
        config.MASSIVE_DAY = "mon"
        config.MASSIVE_BOARDS = [(14, 14, 31), (18, 18, 52), (22, 22, 77),
                                 (26, 26, 88)]
        config.MASSIVE_MINE_BUDGET = 5
        config.TIERS = [(9, 9, 13), (10, 10, 18), (11, 11, 24)]
        config.ADAPTIVE_BOARD = True
        config.LEADERBOARD_TZ = "America/Los_Angeles"
        config.MASSIVE_START = "2026-11-02"
        db.init_db()

    def tearDown(self):
        (config.DB_PATH, config.KNOCKOUT, config.MASSIVE_DAY,
         config.MASSIVE_BOARDS, config.MASSIVE_MINE_BUDGET, config.TIERS,
         config.ADAPTIVE_BOARD, config.LEADERBOARD_TZ, config.POST_MODE,
         config.DRY_DIR, config.MASSIVE_START) = self._saved

    def board(self, rows, cols, mines, status=game.ACTIVE, budget=5):
        state = game.new_game(1, rows=rows, cols=cols, mines=mines,
                              rng=random.Random(1), mine_budget=budget)
        state.status = status
        return state


class WhenItRuns(_Config):
    def sizes(self):
        return [main.massive_board_for(m)[:2] for m in MONDAYS]

    def test_it_grows_through_the_month(self):
        self.assertEqual(self.sizes(), [(14, 14), (18, 18), (22, 22),
                                        (26, 26), (26, 26)])

    def test_the_last_week_is_thirteen_percent(self):
        self.assertEqual(main.massive_board_for(MONDAYS[3]), (26, 26, 88))

    def test_it_resets_with_the_month(self):
        self.assertEqual(
            main.massive_board_for(datetime(2026, 12, 7, 9, tzinfo=PACIFIC)),
            (14, 14, 31))

    def test_no_other_day(self):
        for offset in range(1, 7):
            self.assertIsNone(
                main.massive_board_for(MONDAYS[0] + timedelta(days=offset)))

    def test_the_day_is_the_local_one(self):
        """Monday 01:00 UTC is still Sunday evening in California."""
        sunday_night = datetime(2026, 11, 2, 1, 0, tzinfo=ZoneInfo("UTC"))
        self.assertIsNone(main.massive_board_for(sunday_night))

    def test_once_per_date(self):
        db.set_setting("massive_last_date", "2026-11-02")
        self.assertIsNone(main.massive_board_for(MONDAYS[0]))
        self.assertIsNotNone(main.massive_board_for(MONDAYS[1]))

    def test_nothing_before_the_start_date(self):
        """It shipped on Sun Oct 25; the next morning was the fourth Monday,
        which would have opened a surprise 26x26 a week early."""
        oct_26 = datetime(2026, 10, 26, 0, 5, tzinfo=PACIFIC)
        self.assertIsNone(main.massive_board_for(oct_26))
        self.assertEqual(main.massive_board_for(MONDAYS[0]), (14, 14, 31))

    def test_the_shipped_default_starts_on_november_2(self):
        # Read from the source, not reloaded: reloading config would reset
        # every setting the rest of the suite has arranged.
        with open(config.__file__) as handle:
            self.assertIn('os.environ.get("MASSIVE_START", "2026-11-02")',
                          handle.read())

    def test_an_empty_start_date_is_no_limit(self):
        config.MASSIVE_START = ""
        self.assertIsNotNone(
            main.massive_board_for(datetime(2026, 10, 26, 9, tzinfo=PACIFIC)))

    def test_never_in_voting_mode(self):
        config.KNOCKOUT = False
        self.assertIsNone(main.massive_board_for(MONDAYS[0]))

    def test_switched_off(self):
        config.MASSIVE_DAY = ""
        self.assertIsNone(main.massive_board_for(MONDAYS[0]))

    def test_another_day_can_be_chosen(self):
        config.MASSIVE_DAY = "tue"
        self.assertIsNone(main.massive_board_for(MONDAYS[0]))
        self.assertIsNotNone(
            main.massive_board_for(MONDAYS[0] + timedelta(days=1)))
        self.assertEqual(main.massive_name(), "Massive Tuesday")


class WhatItIs(_Config):
    def test_a_big_board_is_recognised_by_its_size(self):
        self.assertTrue(main.is_massive(self.board(26, 26, 88)))
        self.assertFalse(main.is_massive(self.board(11, 11, 24)))
        self.assertFalse(main.is_massive(None))

    def test_a_size_that_is_also_a_tier_is_a_tier(self):
        config.MASSIVE_BOARDS = [(11, 11, 24)]
        self.assertFalse(main.is_massive(self.board(11, 11, 24)))

    def test_it_pays_like_the_top_tier(self):
        big = self.board(14, 14, 31)
        self.assertEqual(main.tier_of(big), 2)
        self.assertEqual(main.tier_multiplier(big), 3)

    def test_the_ladder_resumes_where_it_was(self):
        db.set_setting("massive_resume_tier", "1")
        done = self.board(26, 26, 88, status=game.CLEARED)
        self.assertEqual(main.next_tier(done), 1)

    def test_a_garbled_resume_tier_starts_at_the_bottom(self):
        db.set_setting("massive_resume_tier", "banana")
        done = self.board(26, 26, 88, status=game.EXPLODED)
        self.assertEqual(main.next_tier(done), 0)
        db.set_setting("massive_resume_tier", "9")
        self.assertEqual(main.next_tier(done), 2)


class StartingBoards(_Config):
    """The live path, posting to a dry-run directory."""

    def setUp(self):
        super().setUp()
        config.POST_MODE = "dry"
        config.DRY_DIR = tempfile.mkdtemp()
        # A Tier 2 board just ended, at a participation that keeps Tier 2.
        previous = self.board(10, 10, 18, status=game.CLEARED, budget=2)
        previous.turn_number = 20
        db.save_state(previous)

    def test_monday_opens_the_big_board_then_resumes_the_ladder(self):
        main._start_new_game(MONDAYS[0])
        big = db.load_state()
        self.assertEqual((big.rows, big.cols, big.mine_count), (14, 14, 31))
        self.assertEqual(big.mine_budget, 5)
        self.assertEqual(db.get_setting("massive_last_date"), "2026-11-02")

        big.status = game.CLEARED
        db.save_state(big)
        main._start_new_game(MONDAYS[0] + timedelta(hours=10))
        after = db.load_state()
        self.assertFalse(main.is_massive(after))
        self.assertEqual(main.tier_of(after),
                         int(db.get_setting("massive_resume_tier")))

    def test_any_other_day_is_a_regular_board(self):
        main._start_new_game(MONDAYS[0] + timedelta(days=2))
        self.assertFalse(main.is_massive(db.load_state()))
        self.assertEqual(db.get_setting("massive_last_date"), "")


class ItFits(_Config):
    RECORD = {"played": 999, "cleared": 900, "exploded": 99}

    def test_every_opening_post_fits(self):
        for rows, cols, mines in config.MASSIVE_BOARDS:
            text = main.build_opening_text(self.board(rows, cols, mines),
                                           self.RECORD)
            self.assertLessEqual(len(text), 300, f"{rows}x{cols}")
            self.assertIn("MASSIVE MONDAY", text)
            self.assertIn(f"{rows}x{cols}, {mines} mines", text)

    def test_the_game_over_post_fits_and_names_the_day(self):
        for status in (game.CLEARED, game.EXPLODED):
            state = self.board(26, 26, 88, status=status)
            state.turn_number, state.exploded_cell = 999, "Z26"
            text = main.build_knockout_gameover_text(
                state, self.RECORD, 12, 30,
                "🏅 Board MVP: @averylongplayerhandle — 9,999 points.",
                datetime(2026, 11, 3, 9, tzinfo=PACIFIC),
                "🚩 Flags: @averylongflagger +40 · @anotherflagger +30")
            self.assertLessEqual(len(text), 300, status)
            self.assertIn("Massive Monday", text)

    def test_the_board_before_it_gives_warning(self):
        state = self.board(11, 11, 24, status=game.CLEARED, budget=3)
        state.turn_number = 999
        text = main.build_knockout_gameover_text(
            state, self.RECORD, 12, 30,
            "🏅 Board MVP: @averylongplayerhandle — 9,999 points.",
            MONDAYS[0], "")
        self.assertIn("Next board: Massive Monday (14x14)", text)
        self.assertLessEqual(len(text), 300)

    def test_no_warning_on_an_ordinary_night(self):
        state = self.board(11, 11, 24, status=game.CLEARED, budget=3)
        text = main.build_knockout_gameover_text(
            state, self.RECORD, 1, 1, "", MONDAYS[0] + timedelta(days=1))
        self.assertNotIn("Massive", text)

    def test_a_turn_post_fits_with_a_crowd(self):
        state = self.board(26, 26, 88)
        state.turn_number = 999
        plays = [main.Play(f"did:{i}", f"averylongplayerhandle{i}.bsky.social",
                           "Z26", game.SAFE, 99) for i in range(12)]
        self.assertLessEqual(len(main.build_knockout_turn_text(state, plays)),
                             300)

    def test_the_alt_text_still_carries_the_whole_board(self):
        """At 26x26 the cell-by-cell description is most of its limit; a
        truncated one is a board a screen-reader user cannot play."""
        for status in (game.ACTIVE, game.EXPLODED):
            state = self.board(26, 26, 88, status=status)
            state.turn_number, state.exploded_cell = 999, "Z26"
            flags = [game.index_to_coord(*c)
                     for c in sorted(state.mine_cells)[:30]]
            alt = renderer.build_alt_text(state, flags)
            self.assertFalse(alt.endswith("…"), status)
            self.assertIn("Z: ", alt)

    def test_the_image_stays_well_under_the_upload_limit(self):
        state = self.board(26, 26, 88)
        png = renderer.render_board(state, highlight=state.last_coord)
        self.assertLess(len(png), 900_000)

    def test_every_coordinate_on_the_big_board_parses(self):
        import votes
        for coord in ("A1", "Z26", "M13", "z 26", "Y-10"):
            mentions = votes.find_mentions(f"I'd go {coord}", 26, 26)
            self.assertEqual(len(mentions), 1, coord)


if __name__ == "__main__":
    unittest.main()
