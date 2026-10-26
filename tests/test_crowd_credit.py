"""Naming the whole crowd: the week's arrivals and a cleared board's players.

The standings name the same three people most weeks and the game-over post
has room for one MVP, so these two replies are where everybody else is
named. Both are follow-ups to a post that has already gone out, so neither
may ever break or repeat it, and both must fit in 300 characters however
many people they name.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from helpers import config, db, game, main
import bluesky


def _at(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _Database(unittest.TestCase):
    NOW = datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)

    def setUp(self):
        self._db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "crowd.db")
        db.init_db()
        self.state = game.new_game(1)
        self.since = self.NOW - timedelta(days=7)

    def tearDown(self):
        config.DB_PATH = self._db

    def move(self, did, days_ago, points=1, game_id=1, turn=1):
        self.state.game_id, self.state.turn_number = game_id, turn
        db.record_move(self.state, "D4", game.SAFE, "crowd",
                       caller=did[4:] + ".bsky.social", did=did, points=points)
        self._stamp("moves", days_ago)

    def flag(self, did, days_ago, game_id=1):
        db.add_flag(game_id, "E5", did, did[4:] + ".bsky.social", 1)
        self._stamp("flags", days_ago)

    def _stamp(self, table, days_ago):
        with db._connect() as conn:
            conn.execute(f"UPDATE {table} SET created_at = ? WHERE id = "
                         f"(SELECT MAX(id) FROM {table})",
                         (_at(self.NOW - timedelta(days=days_ago)),))


class Arrivals(_Database):
    def arrivals(self):
        new, back = db.weekly_arrivals(self.since)
        return [p["did"] for p in new], [p["did"] for p in back]

    def test_a_first_move_this_week_is_a_newcomer(self):
        self.move("did:new", 2)
        self.assertEqual(self.arrivals(), (["did:new"], []))

    def test_a_regular_is_neither(self):
        for day in (20, 15, 10, 5, 1):
            self.move("did:reg", day)
        self.assertEqual(self.arrivals(), ([], []))

    def test_somebody_back_after_a_gap_is_welcomed_back(self):
        self.move("did:back", 30)
        self.move("did:back", 3)
        self.assertEqual(self.arrivals(), ([], ["did:back"]))

    def test_a_gap_shorter_than_two_weeks_is_not_a_return(self):
        self.move("did:busy", 12)
        self.move("did:busy", 3)
        self.assertEqual(self.arrivals(), ([], []))

    def test_nobody_absent_this_week_is_named(self):
        self.move("did:gone", 30)
        self.assertEqual(self.arrivals(), ([], []))

    def test_a_flag_claim_counts_as_turning_up(self):
        self.flag("did:flagger", 2)
        self.assertEqual(self.arrivals(), (["did:flagger"], []))

    def test_newcomers_are_ranked_by_this_weeks_points(self):
        self.move("did:small", 4, points=2)
        self.move("did:big", 3, points=40)
        self.assertEqual(self.arrivals()[0], ["did:big", "did:small"])

    def test_the_bot_is_never_a_newcomer(self):
        self.state.turn_number = 1
        db.record_move(self.state, "A1", game.SAFE, "opening")
        self.assertEqual(self.arrivals(), ([], []))


class BoardPlayers(_Database):
    def test_everyone_who_moved_or_flagged_is_named_once(self):
        self.move("did:a", 1, points=3)
        self.move("did:a", 1, points=4, turn=2)
        self.move("did:b", 1, points=9)
        self.flag("did:c", 1)
        players = db.board_players(1)
        self.assertEqual([p["did"] for p in players],
                         ["did:b", "did:a", "did:c"])
        self.assertEqual(players[1]["points"], 7)

    def test_other_boards_do_not_count(self):
        self.move("did:a", 1, game_id=1)
        self.move("did:b", 1, game_id=2)
        self.assertEqual([p["did"] for p in db.board_players(1)], ["did:a"])


class ArrivalsCopy(unittest.TestCase):
    def people(self, n, prefix="player"):
        return [{"did": f"did:{prefix}{i}",
                 "handle": f"{prefix}{i}.bsky.social", "points": n - i}
                for i in range(n)]

    def test_both_groups_are_named(self):
        text, dids = main.build_arrivals_text(self.people(2, "new"),
                                              self.people(1, "back"))
        self.assertIn("New this week: @new0 @new1", text)
        self.assertIn("Welcome back: @back0", text)
        self.assertEqual(dids["new0"], "did:new0")
        self.assertLessEqual(len(text), 300)

    def test_an_empty_group_gets_no_line(self):
        text, _ = main.build_arrivals_text(self.people(2, "new"), [])
        self.assertNotIn("Welcome back", text)

    def test_nobody_to_name_is_no_reply(self):
        self.assertEqual(main.build_arrivals_text([], []), ("", {}))

    def test_a_crowd_still_fits_and_says_how_many_more(self):
        text, _ = main.build_arrivals_text(
            self.people(30, "averylongnewcomerhandle"),
            self.people(30, "averylongreturninghandle"))
        self.assertLessEqual(len(text), 300)
        self.assertIn("more", text)
        self.assertIn("New this week", text)
        self.assertIn("Welcome back", text)
        self.assertIn("live board", text)


class ThanksCopy(unittest.TestCase):
    def players(self, n, stem="player"):
        return [{"did": f"did:{i}", "handle": f"{stem}{i}.bsky.social",
                 "points": n - i} for i in range(n)]

    def test_it_names_everyone_it_can(self):
        text, dids = main.build_board_thanks(self.players(4))
        self.assertIn("Swept by 4 players", text)
        for i in range(4):
            self.assertIn(f"@player{i}", text)
        self.assertEqual(dids["player0"], "did:0")

    def test_a_big_crowd_is_trimmed_from_the_bottom(self):
        text, _ = main.build_board_thanks(
            self.players(40, "averylongplayerhandle"))
        self.assertLessEqual(len(text), 300)
        self.assertIn("@averylongplayerhandle0", text)
        self.assertIn("more", text)

    def test_one_player_is_singular(self):
        self.assertIn("1 player.", main.build_board_thanks(self.players(1))[0])

    def test_nobody_is_no_reply(self):
        self.assertEqual(main.build_board_thanks([]), ("", {}))


class _Posting(_Database):
    """Stubs the network; records what would have been posted."""

    def setUp(self):
        super().setUp()
        self.replies = []
        self._real = (bluesky.post_text, bluesky.post_reply, main._remember)
        bluesky.post_reply = lambda text, **kw: (
            self.replies.append((text, kw)) or "at://reply/1")
        main._remember = lambda *a, **k: None

    def tearDown(self):
        bluesky.post_text, bluesky.post_reply, main._remember = self._real
        super().tearDown()


class ThankingTheBoard(_Posting):
    def setUp(self):
        super().setUp()
        bluesky._cids["at://gameover/1"] = "cid-1"
        self.move("did:a", 0, points=5)

    def test_a_cleared_board_is_thanked_under_its_own_post(self):
        self.state.status = game.CLEARED
        main._thank_the_board(self.state, "at://gameover/1")
        self.assertEqual(len(self.replies), 1)
        text, kw = self.replies[0]
        self.assertIn("@a", text)
        self.assertEqual(kw["parent_uri"], "at://gameover/1")
        self.assertEqual(kw["parent_cid"], "cid-1")

    def test_a_lost_board_and_a_live_one_are_not(self):
        for status in (game.EXPLODED, game.ACTIVE):
            self.state.status = status
            main._thank_the_board(self.state, "at://gameover/1")
        self.assertEqual(self.replies, [])

    def test_a_post_we_did_not_make_gets_no_reply(self):
        self.state.status = game.CLEARED
        main._thank_the_board(self.state, "at://somebody-else/1")
        self.assertEqual(self.replies, [])

    def test_a_failed_reply_is_only_logged(self):
        def down(text, **kw):
            raise RuntimeError("rate limited")
        bluesky.post_reply = down
        self.state.status = game.CLEARED
        main._thank_the_board(self.state, "at://gameover/1")   # no raise


class StandingsThread(_Posting):
    def setUp(self):
        super().setUp()
        self._top = config.LEADERBOARD_TOP
        config.LEADERBOARD_TOP = 3
        self.posted = []

        def post(text, **kw):
            self.posted.append(text)
            bluesky._cids["at://standings/1"] = "cid-s"
            return "at://standings/1"
        bluesky.post_text = post
        self.move("did:reg", 30, points=50)
        self.move("did:reg", 2, points=20)
        self.move("did:new", 1, points=8)

    def tearDown(self):
        config.LEADERBOARD_TOP = self._top
        super().tearDown()

    def test_the_arrivals_reply_threads_under_the_standings(self):
        main.weekly_leaderboard(self.NOW)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(len(self.replies), 1)
        text, kw = self.replies[0]
        self.assertIn("New this week: @new", text)
        self.assertIn("Welcome back: @reg", text)
        self.assertEqual(kw["parent_uri"], "at://standings/1")

    def test_a_failed_reply_leaves_the_standings_posted(self):
        def down(text, **kw):
            raise RuntimeError("rate limited")
        bluesky.post_reply = down
        self.assertEqual(main.weekly_leaderboard(self.NOW), "at://standings/1")


class RememberingCids(unittest.TestCase):
    def test_a_real_post_keeps_its_cid(self):
        class Posted:
            uri, cid = "at://x/1", "cid-x"
        self.assertEqual(bluesky._remember_cid(Posted()), "at://x/1")
        self.assertEqual(bluesky.cid_of("at://x/1"), "cid-x")

    def test_an_unknown_post_has_none(self):
        self.assertEqual(bluesky.cid_of("at://never/1"), "")


if __name__ == "__main__":
    unittest.main()
