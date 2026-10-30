"""The first-move welcome and the days-played badges.

Both exist for the same leak: in Sep–Oct 2026 more than a third of the
monthly players had played on only one day. So the bar is: a newcomer is
welcomed exactly once and told why to come back, a regular is never
welcomed, and a second day is celebrated once — including for somebody whose
only move that day hit a mine.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from helpers import config, db, game, main

PACIFIC = ZoneInfo("America/Los_Angeles")


class _Vote:
    votes = 3
    total_voters = 5
    caller = None
    caller_handle = "someone.test"


def _at(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _Database(unittest.TestCase):
    def setUp(self):
        self._saved = (config.DB_PATH, config.DAY_MILESTONES,
                       config.MILESTONES, config.LEADERBOARD_TZ)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "welcome.db")
        config.DAY_MILESTONES = [2, 3, 7]
        config.MILESTONES = [100, 500]
        config.LEADERBOARD_TZ = "America/Los_Angeles"
        db.init_db()
        self.state = game.new_game(1)

    def tearDown(self):
        (config.DB_PATH, config.DAY_MILESTONES, config.MILESTONES,
         config.LEADERBOARD_TZ) = self._saved

    def move(self, did, when=None, turn=1, points=1, game_id=1):
        self.state.game_id, self.state.turn_number = game_id, turn
        db.record_move(self.state, "D4", game.SAFE, "crowd",
                       caller=did.split(":")[-1] + ".bsky.social", did=did,
                       points=points)
        if when is not None:
            with db._connect() as conn:
                conn.execute("UPDATE moves SET created_at = ? WHERE id = "
                             "(SELECT MAX(id) FROM moves)", (_at(when),))

    def flag(self, did, when, turn=1):
        db.add_flag(1, "E5", did, did, turn)
        with db._connect() as conn:
            conn.execute("UPDATE flags SET created_at = ? WHERE id = "
                         "(SELECT MAX(id) FROM flags)", (_at(when),))


class WelcomeCopy(unittest.TestCase):
    def setUp(self):
        self._ladder = config.DAY_MILESTONES
        config.DAY_MILESTONES = [2, 3, 7]
        self.state = game.new_game(1)
        self.state.turn_number = 12

    def tearDown(self):
        config.DAY_MILESTONES = self._ladder

    def test_a_first_move_is_welcomed_and_told_why_to_return(self):
        out = main.build_credit_reply(self.state, "A1", _Vote(), 8, 8, 8,
                                      first_move=True)
        self.assertIn("first move", out)
        self.assertIn("a second day", out)
        self.assertIn("All time: 8", out)

    def test_nobody_else_is_welcomed(self):
        out = main.build_credit_reply(self.state, "A1", _Vote(), 8, 24, 123)
        self.assertNotIn("Welcome", out)

    def test_the_voting_reply_still_fits_with_big_numbers(self):
        self.state.turn_number = 9999
        out = main.build_credit_reply(self.state, "J10", _Vote(), 81, 9999,
                                      999999, first_move=True)
        self.assertLessEqual(len(out), 300)
        self.assertIn("first badge", out)

    def test_the_knockout_reply_carries_it_on_a_safe_move_and_a_mine(self):
        self.state.turn_number = 9999
        safe = main.Play("did:n", "newcomer.bsky.social", "J10", game.SAFE, 81,
                         reply_uri="at://n/x", reply_cid="c")
        mine = main.Play("did:n", "newcomer.bsky.social", "J10", game.MINE,
                         reply_uri="at://n/x", reply_cid="c")
        for play in (safe, mine):
            out = main.build_knockout_credit_reply(
                self.state, play, 9999, 999999, first_move=True)
            self.assertIn("first move", out, play.result)
            self.assertLessEqual(len(out), 300, play.result)

    def test_the_invitation_follows_the_ladder(self):
        config.DAY_MILESTONES = [3, 7]
        self.assertIn("3 different days", main.build_welcome_line())
        config.DAY_MILESTONES = []
        self.assertNotIn("badge", main.build_welcome_line())


class FirstMove(_Database):
    def test_this_turns_own_move_does_not_count_against_it(self):
        """Asked after the turn is recorded, which is when the reply goes."""
        self.move("did:n", turn=5)
        self.assertFalse(db.moved_before("did:n", 1, 5))

    def test_any_earlier_move_does(self):
        self.move("did:r", turn=4)
        self.move("did:r", turn=5)
        self.assertTrue(db.moved_before("did:r", 1, 5))

    def test_a_move_on_an_earlier_board_does(self):
        self.move("did:r", turn=5, game_id=1)
        self.assertTrue(db.moved_before("did:r", 2, 5))

    def test_the_bot_is_never_welcomed(self):
        self.assertTrue(db.moved_before("", 1, 5))

    def test_only_the_newcomer_in_a_knockout_turn_is_welcomed(self):
        self.move("did:r", turn=1)
        self.move("did:r", turn=2)
        self.move("did:n", turn=2)
        plays = [main.Play(d, d[4:] + ".bsky.social", "D4", game.SAFE, 1,
                           reply_uri=f"at://{d}/x", reply_cid="c")
                 for d in ("did:r", "did:n")]
        sent = {}
        real_reply, real_remember = main.bluesky.post_reply, main._remember
        main.bluesky.post_reply = lambda text, **kw: (
            sent.__setitem__(kw["parent_uri"], text) or "at://sent/1")
        main._remember = lambda *a, **k: None
        try:
            self.state.turn_number = 2
            main._credit_knockout_players(self.state, plays)
        finally:
            main.bluesky.post_reply, main._remember = real_reply, real_remember
        self.assertIn("first move", sent["at://did:n/x"])
        self.assertNotIn("first move", sent["at://did:r/x"])


class PlayDays(_Database):
    def test_days_are_local_dates_not_utc_ones(self):
        """05:00 UTC on the 21st is still the evening of the 20th in
        California, and two plays that evening are one day."""
        self.move("did:a", datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc))
        self.move("did:a", datetime(2026, 10, 20, 19, 0, tzinfo=PACIFIC))
        self.assertEqual(db.play_days("did:a", PACIFIC),
                         {datetime(2026, 10, 20).date()})

    def test_a_flag_claim_is_a_day_played(self):
        self.move("did:a", datetime(2026, 10, 20, 12, tzinfo=PACIFIC))
        self.flag("did:a", datetime(2026, 10, 22, 12, tzinfo=PACIFIC))
        self.assertEqual(len(db.play_days("did:a", PACIFIC)), 2)

    def test_other_players_do_not_count(self):
        self.move("did:a", datetime(2026, 10, 20, 12, tzinfo=PACIFIC))
        self.move("did:b", datetime(2026, 10, 21, 12, tzinfo=PACIFIC))
        self.assertEqual(len(db.play_days("did:a", PACIFIC)), 1)


class DayCrossings(_Database):
    def day(self, n, hour=12):
        return datetime(2026, 10, n, hour, tzinfo=PACIFIC)

    def test_a_second_day_is_a_badge(self):
        self.move("did:n", self.day(20))
        self.assertEqual(main.day_crossings("did:n", "n", now=self.day(20)), [])
        self.move("did:n", self.day(21), turn=2)
        crossings = main.day_crossings("did:n", "n", now=self.day(21))
        self.assertEqual([c["threshold"] for c in crossings], [2])
        self.assertEqual(crossings[0]["kind"], "days")
        self.assertEqual(crossings[0]["next"], 3)

    def test_it_is_announced_once(self):
        self.move("did:n", self.day(20))
        self.move("did:n", self.day(21), turn=2)
        main.day_crossings("did:n", "n", now=self.day(21))
        self.move("did:n", self.day(21, hour=18), turn=3)
        self.assertEqual(main.day_crossings("did:n", "n", now=self.day(21)), [])

    def test_a_regular_is_not_badged_for_days_long_past(self):
        """Switching this on mid-season must not post a regular's second day
        three weeks late."""
        for n in range(1, 6):
            self.move("did:r", self.day(n), turn=n)
        self.move("did:r", self.day(6), turn=6)
        self.assertEqual(main.day_crossings("did:r", "r", now=self.day(6)), [])
        self.assertEqual(db.milestones_reached("did:r", kind="days"), {2, 3})

    def test_but_one_reaching_a_rung_today_is(self):
        for n in range(1, 7):
            self.move("did:r", self.day(n), turn=n)
        self.move("did:r", self.day(7), turn=7)
        crossings = main.day_crossings("did:r", "r", now=self.day(7))
        self.assertEqual([c["threshold"] for c in crossings], [7])

    def test_a_failed_post_hands_the_badge_back(self):
        self.move("did:n", self.day(20))
        self.move("did:n", self.day(21), turn=2)
        main.day_crossings("did:n", "n", now=self.day(21))
        db.release_milestones("did:n", [2], kind="days")
        self.assertEqual(
            [c["threshold"] for c in
             main.day_crossings("did:n", "n", now=self.day(21))], [2])

    def test_the_two_ladders_never_share_a_claim(self):
        """A day rung and a point rung of the same number are different
        badges; claiming one must not swallow the other."""
        db.claim_milestones("did:a", 3, [2, 3], kind="days")
        self.assertEqual(db.claim_milestones("did:a", 3, [2, 3]), [2, 3])

    def test_switched_off_is_silent(self):
        config.DAY_MILESTONES = []
        self.move("did:n", self.day(20))
        self.move("did:n", self.day(21), turn=2)
        self.assertEqual(main.day_crossings("did:n", "n", now=self.day(21)), [])

    def test_day_badges_go_with_a_full_reset(self):
        db.claim_milestones("did:a", 3, [2, 3], kind="days")
        db.reset_all()
        self.assertEqual(db.milestones_reached("did:a", kind="days"), set())


class Announcing(_Database):
    """The live path: what gets posted, and what a failure leaves behind."""

    def setUp(self):
        super().setUp()
        self.posted = []
        self._real = (main.bluesky.post_text, main._remember,
                      main.day_crossings)
        main.bluesky.post_text = lambda text, **kw: (
            self.posted.append(text) or "at://sent/1")
        main._remember = lambda *a, **k: None
        # Played yesterday and today, as of the real clock: the day badge
        # is owed on this turn whatever date the suite runs on.
        today = datetime.now(PACIFIC).replace(hour=12)
        from datetime import timedelta
        self.yesterday, self.today = today - timedelta(days=1), today

    def tearDown(self):
        (main.bluesky.post_text, main._remember,
         main.day_crossings) = self._real
        super().tearDown()

    def test_a_move_that_hit_a_mine_still_earns_the_day(self):
        self.move("did:n", self.yesterday)
        self.move("did:n", turn=2, points=0)
        main._announce_milestones(self.state, [("did:n", "n.bsky.social", 0)])
        self.assertEqual(len(self.posted), 1)
        self.assertIn("2 different days", self.posted[0])

    def test_points_and_a_day_share_one_post(self):
        self.move("did:n", self.yesterday, points=50)
        self.move("did:n", turn=2, points=60)
        main._announce_milestones(self.state,
                                  [("did:n", "n.bsky.social", 60)])
        self.assertEqual(len(self.posted), 1)
        self.assertIn("passed 100", self.posted[0])
        self.assertIn("2 different days", self.posted[0])

    def test_a_player_with_two_moves_in_a_turn_is_counted_once(self):
        calls = []
        real = main.day_crossings
        main.day_crossings = lambda did, *a, **k: calls.append(did) or []
        main._announce_milestones(self.state, [("did:n", "n", 1),
                                               ("did:n", "n", 1)])
        main.day_crossings = real
        self.assertEqual(calls, ["did:n"])

    def test_a_failed_post_releases_the_day_badge(self):
        def down(text, **kw):
            raise RuntimeError("rate limited")
        main.bluesky.post_text = down
        self.move("did:n", self.yesterday)
        self.move("did:n", turn=2)
        main._announce_milestones(self.state, [("did:n", "n.bsky.social", 1)])
        self.assertEqual(db.milestones_reached("did:n", kind="days"), set())


class BadgeCopy(unittest.TestCase):
    def setUp(self):
        self._ladder = config.DAY_MILESTONES
        config.DAY_MILESTONES = [2, 3, 7]

    def tearDown(self):
        config.DAY_MILESTONES = self._ladder

    def badge(self, threshold=2, nxt=3, handle="coil.bsky.social",
              did="did:coil"):
        return {"did": did, "handle": handle, "kind": "days",
                "threshold": threshold, "points": threshold, "next": nxt}

    def test_the_first_badge_says_so(self):
        text, dids = main.build_milestone_text([self.badge()])
        self.assertIn("@coil", text)
        self.assertIn("2 different days", text)
        self.assertIn("First badge", text)
        self.assertIn("Next badge: 3 days", text)
        self.assertEqual(dids["coil"], "did:coil")
        self.assertLessEqual(len(text), 300)

    def test_a_later_badge_does_not_call_itself_the_first(self):
        text, _ = main.build_milestone_text([self.badge(7, 0)])
        self.assertNotIn("First badge", text)
        self.assertNotIn("Next badge", text)

    def test_mixed_with_point_milestones_it_still_fits(self):
        crossings = [self.badge(handle=f"averylongplayerhandle{i}.bsky.social",
                                did=f"did:{i}") for i in range(4)]
        crossings += [{"did": f"did:p{i}",
                       "handle": f"anotherlongplayerhandle{i}.bsky.social",
                       "threshold": 1000, "points": 1047, "rank": 0,
                       "next": 2500} for i in range(4)]
        text, _ = main.build_milestone_text(crossings)
        self.assertLessEqual(len(text), 300)
        self.assertIn("different days", text)


if __name__ == "__main__":
    unittest.main()
