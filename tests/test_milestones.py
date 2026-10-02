"""Milestone celebrations, and the weekly standings post.

Two public posts that name people, so the bar is: never congratulate the same
person for the same rung twice, never congratulate a long-standing player for
a rung they passed weeks ago, and never let six handles push the post past
300 characters.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from helpers import config, db, game, main


class Ledger(unittest.TestCase):
    """The table that makes a duplicate impossible."""

    def setUp(self):
        self._db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "milestones.db")
        db.init_db()
        self.ladder = [100, 500, 1000]

    def tearDown(self):
        config.DB_PATH = self._db

    def test_a_rung_is_claimed_once(self):
        self.assertEqual(db.claim_milestones("did:a", 120, self.ladder), [100])
        self.assertEqual(db.claim_milestones("did:a", 120, self.ladder), [])

    def test_claims_are_per_player(self):
        db.claim_milestones("did:a", 120, self.ladder)
        self.assertEqual(db.claim_milestones("did:b", 120, self.ladder), [100])

    def test_a_jump_claims_every_rung_it_passed(self):
        self.assertEqual(db.claim_milestones("did:a", 1200, self.ladder),
                         [100, 500, 1000])

    def test_a_released_claim_can_be_retried(self):
        db.claim_milestones("did:a", 120, self.ladder)
        db.release_milestones("did:a", [100])
        self.assertEqual(db.claim_milestones("did:a", 120, self.ladder), [100])

    def test_a_backfilled_rung_is_never_released(self):
        """A silent backfill is not a failed post, so a release must not
        reopen it — that is the route to congratulating a regular on 100."""
        db.backfill_milestones("did:a", 942, self.ladder)
        db.release_milestones("did:a", [100, 500])
        self.assertEqual(db.claim_milestones("did:a", 942, self.ladder), [])

    def test_the_backfill_only_ever_runs_once_per_player(self):
        """Run on every startup it would swallow a rung genuinely owed, so it
        applies only to a player with no rows at all."""
        db.backfill_milestones("did:a", 100, self.ladder)
        db.claim_milestones("did:a", 600, self.ladder)       # 500 announced
        db.release_milestones("did:a", [500])                # its post failed
        self.assertEqual(db.backfill_milestones("did:a", 600, self.ladder), 0)
        self.assertEqual(db.claim_milestones("did:a", 600, self.ladder), [500])

    def test_milestones_go_with_a_full_reset(self):
        db.claim_milestones("did:a", 120, self.ladder)
        db.reset_all()
        self.assertEqual(db.milestones_reached("did:a"), set())


class Crossings(unittest.TestCase):
    """What the live path hands the builder."""

    def setUp(self):
        self._db, self._ladder = config.DB_PATH, config.MILESTONES
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "crossings.db")
        config.MILESTONES = [100, 500, 1000]
        db.init_db()
        self.state = game.new_game(1)

    def tearDown(self):
        config.DB_PATH, config.MILESTONES = self._db, self._ladder

    def score(self, did, handle, points, turn=1):
        self.state.turn_number = turn
        db.record_move(self.state, "D4", game.SAFE, "crowd", caller=handle,
                       did=did, points=points)

    def test_a_new_player_crossing_is_celebrated(self):
        self.score("did:a", "a.bsky.social", 120)
        crossings = main.milestone_crossings("did:a", "a.bsky.social", 120, 120)
        self.assertEqual([c["threshold"] for c in crossings], [100])
        self.assertEqual(crossings[0]["points"], 120)
        self.assertEqual(crossings[0]["next"], 500)

    def test_an_established_player_is_not_congratulated_on_old_rungs(self):
        """The case that made the backfill necessary: switching this on while
        somebody already has 942 points must not announce their first 100."""
        self.score("did:a", "a.bsky.social", 942)
        self.score("did:a", "a.bsky.social", 30, turn=2)
        self.assertEqual(
            main.milestone_crossings("did:a", "a.bsky.social", 972, 30), [])

    def test_the_rung_they_then_cross_is_announced(self):
        self.score("did:a", "a.bsky.social", 942)
        main.milestone_crossings("did:a", "a.bsky.social", 942, 942)
        self.score("did:a", "a.bsky.social", 80, turn=2)
        crossings = main.milestone_crossings("did:a", "a.bsky.social", 1022, 80)
        self.assertEqual([c["threshold"] for c in crossings], [1000])

    def test_a_first_move_that_cascades_past_a_rung_still_counts(self):
        """The backfill is measured before this turn, so a 120-cell opening
        move by somebody we have never seen is a crossing, not history."""
        self.score("did:new", "new.bsky.social", 120)
        crossings = main.milestone_crossings("did:new", "new.bsky.social",
                                             120, 120)
        self.assertEqual([c["threshold"] for c in crossings], [100])

    def test_only_the_highest_rung_of_a_jump_is_announced(self):
        self.score("did:a", "a.bsky.social", 0)
        main.milestone_crossings("did:a", "a.bsky.social", 0, 0)
        self.score("did:a", "a.bsky.social", 1400, turn=2)
        crossings = main.milestone_crossings("did:a", "a.bsky.social", 1400,
                                             1400)
        self.assertEqual([c["threshold"] for c in crossings], [1000])
        self.assertEqual(db.milestones_reached("did:a"), {100, 500, 1000})

    def test_only_the_actual_leader_is_told_they_lead(self):
        self.score("did:a", "a.bsky.social", 1200)
        self.assertEqual(
            main.milestone_crossings("did:a", "a.bsky.social", 1200,
                                     1200)[0]["rank"], 1)
        self.score("did:b", "b.bsky.social", 150, turn=2)
        self.assertEqual(
            main.milestone_crossings("did:b", "b.bsky.social", 150,
                                     150)[0]["rank"], 0)

    def test_a_mine_earns_nothing_and_crosses_nothing(self):
        self.score("did:a", "a.bsky.social", 99)
        main.milestone_crossings("did:a", "a.bsky.social", 99, 99)
        self.assertEqual(
            main.milestone_crossings("did:a", "a.bsky.social", 99, 0), [])


class MilestoneCopy(unittest.TestCase):
    def crossing(self, handle="coil.bsky.social", did="did:coil",
                 threshold=1000, points=1047, rank=1, nxt=2500):
        return {"did": did, "handle": handle, "threshold": threshold,
                "points": points, "rank": rank, "next": nxt}

    def test_it_names_the_player_the_rung_and_the_total(self):
        text, dids = main.build_milestone_text([self.crossing()])
        self.assertIn("@coil", text)
        self.assertIn("passed 1,000 points", text)
        self.assertIn("1,047 cells opened", text)
        self.assertIn("top of the board", text)
        self.assertIn("Next rung: 2,500", text)
        self.assertLessEqual(len(text), 300)

    def test_the_short_name_still_resolves_to_the_account(self):
        """A mention facet carries the DID, so "@coil" notifies the owner of
        coil.bsky.social — which is the only reason six names ever fit."""
        _, dids = main.build_milestone_text([self.crossing()])
        self.assertEqual(dids["coil"], "did:coil")
        self.assertEqual(dids["coil.bsky.social"], "did:coil")

    def test_several_players_share_one_post(self):
        crossings = [self.crossing(f"player{i}.bsky.social", f"did:{i}",
                                   threshold=100 * (i + 1), points=100 * (i + 1))
                     for i in range(3)]
        text, _ = main.build_milestone_text(crossings)
        for i in range(3):
            self.assertIn(f"@player{i}", text)
        self.assertLessEqual(len(text), 300)

    def test_a_crowd_of_crossings_still_fits(self):
        crossings = [self.crossing(f"averylongplayerhandle{i}.bsky.social",
                                   f"did:{i}") for i in range(8)]
        text, _ = main.build_milestone_text(crossings)
        self.assertLessEqual(len(text), 300)
        self.assertIn("MILESTONE", text)

    def test_colliding_short_names_keep_their_full_handles(self):
        crossings = [self.crossing("coil.bsky.social", "did:a"),
                     self.crossing("coil.example.com", "did:b")]
        text, dids = main.build_milestone_text(crossings)
        self.assertIn("@coil.bsky.social", text)
        self.assertIn("@coil.example.com", text)

    def test_nothing_to_say_is_no_post(self):
        self.assertEqual(main.build_milestone_text([]), "")


class WeeklyCopy(unittest.TestCase):
    def rows(self, n, points=600, handle="player{}.bsky.social"):
        return [{"did": f"did:{i}", "handle": handle.format(i),
                 "points": points - i * 50, "moves": 10} for i in range(n)]

    def week(self):
        end = datetime(2026, 10, 3, 17, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
        return end - timedelta(days=7), end

    def test_both_tables_fit_in_one_post(self):
        start, end = self.week()
        text, dids = main.build_leaderboard_text(self.rows(3), self.rows(3),
                                                 start, end)
        self.assertIn("This week:", text)
        self.assertIn("All time:", text)
        self.assertIn("Sep 26–Oct 3", text)
        self.assertLessEqual(len(text), 300)

    def test_the_week_goes_first(self):
        """The all-time table barely moves; the one people can enter does."""
        start, end = self.week()
        text, _ = main.build_leaderboard_text(self.rows(3), self.rows(3),
                                              start, end)
        self.assertLess(text.index("This week:"), text.index("All time:"))

    def test_long_handles_lose_rows_not_the_post(self):
        start, end = self.week()
        long = "averyveryverylongplayerhandle{}.bsky.social"
        text, _ = main.build_leaderboard_text(
            self.rows(3, handle=long), self.rows(3, handle=long), start, end)
        self.assertLessEqual(len(text), 300)
        self.assertIn("This week:", text)
        self.assertIn("@averyveryverylongplayerhandle0", text,
                      "the week's leader must survive the trim")

    def test_it_invites_people_in_when_there_is_room(self):
        """A scoreboard is a reason to look; this is the reason to play."""
        start, end = self.week()
        text, _ = main.build_leaderboard_text(self.rows(3), self.rows(3),
                                              start, end)
        self.assertIn("Reply a coordinate", text)
        self.assertLessEqual(len(text), 300)

    def test_names_outrank_the_invitation(self):
        start, end = self.week()
        long = "averyveryverylongplayerhandle{}.bsky.social"
        text, _ = main.build_leaderboard_text(
            self.rows(3, handle=long), self.rows(3, handle=long), start, end)
        self.assertIn("@averyveryverylongplayerhandle0", text)
        self.assertNotIn("Reply a coordinate", text)

    def test_a_quiet_week_says_so(self):
        start, end = self.week()
        text, _ = main.build_leaderboard_text(self.rows(3), [], start, end)
        self.assertIn("Nobody scored this week", text)
        self.assertIn("All time:", text)
        self.assertLessEqual(len(text), 300)

    def test_every_name_can_be_notified(self):
        start, end = self.week()
        _, dids = main.build_leaderboard_text(self.rows(3), self.rows(3),
                                              start, end)
        for i in range(3):
            self.assertEqual(dids[f"player{i}"], f"did:{i}")


class WeeklyWindow(unittest.TestCase):
    """Which moves count as "this week", and when the post is owed."""

    def setUp(self):
        self._saved = {name: getattr(config, name) for name in (
            "DB_PATH", "WEEKLY_LEADERBOARD", "LEADERBOARD_DAY",
            "LEADERBOARD_HOUR", "LEADERBOARD_TZ")}
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "weekly.db")
        config.WEEKLY_LEADERBOARD = True
        config.LEADERBOARD_DAY = "sat"
        config.LEADERBOARD_HOUR = 17
        config.LEADERBOARD_TZ = "America/Los_Angeles"
        db.init_db()

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(config, name, value)

    def test_the_slot_is_the_configured_local_hour(self):
        # A Thursday evening: the last slot was the previous Saturday.
        now = datetime(2026, 10, 1, 20, 54,
                       tzinfo=ZoneInfo("America/Los_Angeles"))
        slot = main.previous_slot(now)
        self.assertEqual((slot.month, slot.day, slot.hour), (9, 26, 17))
        self.assertEqual(slot.strftime("%a"), "Sat")

    def test_the_slot_keeps_its_wall_clock_hour_across_dst(self):
        """Pacific loses an hour on 1 November 2026. A fixed UTC offset would
        move the post to 4pm; the zone keeps it at 5."""
        after = datetime(2026, 11, 8, 12, 0,
                         tzinfo=ZoneInfo("America/Los_Angeles"))
        slot = main.previous_slot(after)
        self.assertEqual(slot.hour, 17)
        self.assertEqual(slot.utcoffset(), timedelta(hours=-8))
        before = main.previous_slot(
            datetime(2026, 10, 25, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")))
        self.assertEqual(before.hour, 17)
        self.assertEqual(before.utcoffset(), timedelta(hours=-7))

    def test_the_slot_at_the_exact_minute_is_now(self):
        now = datetime(2026, 10, 3, 17, 0,
                       tzinfo=ZoneInfo("America/Los_Angeles"))
        self.assertEqual(main.previous_slot(now), now)

    def test_nothing_is_owed_before_the_first_one(self):
        """Shipping this must not fire a standings post mid-week."""
        self.assertFalse(main.leaderboard_due(
            datetime(2026, 10, 1, 20, 54, tzinfo=timezone.utc)))

    def test_a_restart_across_the_slot_catches_up(self):
        db.record_post("at://x/app.bsky.feed.post/1", "leaderboard")
        with db._connect() as conn:
            conn.execute("UPDATE posts SET created_at = ? WHERE kind = ?",
                         ("2026-09-26 23:30:00", "leaderboard"))
        sunday = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
        self.assertTrue(main.leaderboard_due(sunday))

    def test_one_already_posted_this_week_is_not_repeated(self):
        db.record_post("at://x/app.bsky.feed.post/2", "leaderboard")
        with db._connect() as conn:
            conn.execute("UPDATE posts SET created_at = ? WHERE kind = ?",
                         ("2026-10-04 00:05:00", "leaderboard"))
        self.assertFalse(main.leaderboard_due(
            datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)))

    def test_switched_off_is_never_due(self):
        config.WEEKLY_LEADERBOARD = False
        self.assertFalse(main.leaderboard_due())

    def test_the_window_counts_only_the_last_seven_days(self):
        state = game.new_game(1)
        state.turn_number = 1
        db.record_move(state, "D4", game.SAFE, "crowd", caller="old.bsky.social",
                       did="did:old", points=500)
        db.record_move(state, "D5", game.SAFE, "crowd", caller="new.bsky.social",
                       did="did:new", points=40)
        with db._connect() as conn:
            conn.execute("UPDATE moves SET created_at = '2026-08-01 00:00:00' "
                         "WHERE did = 'did:old'")
        since = datetime.now(timezone.utc) - timedelta(days=7)
        weekly = db.leaderboard(since=since)
        self.assertEqual([r["did"] for r in weekly], ["did:new"])
        self.assertEqual([r["did"] for r in db.leaderboard()],
                         ["did:old", "did:new"])

    def test_a_scoreless_player_is_not_on_the_table(self):
        state = game.new_game(1)
        state.turn_number = 1
        db.record_move(state, "D4", game.MINE, "crowd", caller="out.bsky.social",
                       did="did:out", points=0)
        self.assertEqual(db.leaderboard(), [])
