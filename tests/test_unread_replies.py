"""A turn whose replies cannot be read in full waits; it is never played as
empty.

Turn 12 of 27 October posted "nobody moved, so I opened D10" over three
replies on Turn 11. Bluesky answered the thread request with a 200 and none
of them; the replies showed up later. A request that fails outright (the
24 October outage) did the same, because a failed read came back as [].
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from helpers import config, db, game, main, votes
import bluesky

TURN = "at://did:plc:bot/app.bsky.feed.post/turn"


def reply(text, did, at):
    return votes.Reply(did=did, handle=f"{did[4:]}.bsky.social", text=text,
                       uri=f"at://{did}/{at}", cid="cid", created_at=at,
                       root_uri="at://root", root_cid="rc")


class HeldTurn(unittest.TestCase):
    def setUp(self):
        self._saved = (config.DB_PATH, main._FETCH_PAUSE, main.scheduler,
                       main.bluesky.read_replies, main.bluesky.post_with_image,
                       main.bluesky.post_reply, main._remember)
        main._short_holds.clear()
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "turn.db")
        db.init_db()
        state = game.new_game(1)
        state.last_post_uri = "at://board/1"
        db.save_state(state)
        self.move = next(
            game.index_to_coord(r, c) for r in range(state.rows)
            for c in range(state.cols)
            if (r, c) not in state.revealed and (r, c) not in state.mine_cells)
        self.replies = [reply(self.move, "did:p1", "2026-10-27T13:18:43Z")]

        main._FETCH_PAUSE = 0
        self.jobs, self.posted, self.reads = [], [], 0
        main.scheduler = SimpleNamespace(
            running=True, add_job=lambda *a, **k: self.jobs.append(k))
        main.bluesky.post_with_image = lambda text, image, alt="", **kw: (
            self.posted.append(text) or "at://board/2")
        main.bluesky.post_reply = lambda text, **kw: "at://reply/1"
        main._remember = lambda *a, **k: None

    def tearDown(self):
        (config.DB_PATH, main._FETCH_PAUSE, main.scheduler,
         main.bluesky.read_replies, main.bluesky.post_with_image,
         main.bluesky.post_reply, main._remember) = self._saved
        main._short_holds.clear()

    def reading(self, bad, how):
        """read_replies that goes wrong `bad` times, then reads the move.

        how="raise" fails outright; how="short" is the 27 October case, a
        good response that is missing the replies its count promises."""
        def read_replies(uri):
            self.reads += 1
            if self.reads <= bad:
                if how == "raise":
                    raise TimeoutError("read timed out")
                return [], False
            return self.replies, True
        main.bluesky.read_replies = read_replies

    def assert_not_played(self, before):
        after = db.load_state()
        self.assertEqual(self.posted, [], "a board went out")
        self.assertEqual(after.turn_number, before.turn_number)
        self.assertEqual(after.last_post_uri, "at://board/1")
        self.assertEqual(after.revealed, before.revealed)

    def assert_played(self):
        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("nobody moved", self.posted[0])
        self.assertTrue(db.load_state().coord_is_revealed(self.move))

    def test_a_failed_read_holds_the_turn(self):
        self.reading(bad=99, how="raise")
        before = db.load_state()
        main.game_tick()
        self.assert_not_played(before)
        self.assertEqual(self.reads, main._FETCH_ATTEMPTS)

    def test_a_short_read_holds_the_turn(self):
        self.reading(bad=99, how="short")
        before = db.load_state()
        main.game_tick()
        self.assert_not_played(before)
        self.assertEqual(self.reads, main._FETCH_ATTEMPTS)

    def test_it_is_retried_in_minutes_not_a_whole_turn(self):
        self.reading(bad=99, how="short")
        main.game_tick()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0]["id"], "game_tick_retry")
        self.assertTrue(self.jobs[0]["replace_existing"])

    def test_the_retry_reads_the_moves_and_plays_them(self):
        for how in ("raise", "short"):
            with self.subTest(how=how):
                self.setUp()
                self.reading(bad=main._FETCH_ATTEMPTS, how=how)
                main.game_tick()
                self.assertEqual(self.posted, [])
                main.game_tick()                     # the retry reads it all
                self.assert_played()
                self.tearDown()

    def test_one_bad_read_is_absorbed_within_the_tick(self):
        for how in ("raise", "short"):
            with self.subTest(how=how):
                self.setUp()
                self.reading(bad=1, how=how)
                main.game_tick()
                self.assert_played()
                self.assertEqual(self.jobs, [])
                self.tearDown()

    def test_a_count_that_never_matches_stops_holding(self):
        """A blocked account's reply counts but never shows. After a few
        holds the turn is played with what can be seen."""
        main.bluesky.read_replies = lambda uri: (self.replies, False)
        for _ in range(main._SHORT_HOLDS):
            main.game_tick()
            self.assertEqual(self.posted, [])
        main.game_tick()
        self.assert_played()

    def test_an_outage_holds_without_a_cap(self):
        self.reading(bad=99, how="raise")
        for _ in range(main._SHORT_HOLDS + 2):
            main.game_tick()
        self.assertEqual(self.posted, [])

    def test_play_by_hand_does_not_schedule(self):
        main.scheduler = SimpleNamespace(running=False, add_job=self.fail)
        self.reading(bad=99, how="raise")
        main.game_tick()
        self.assertEqual(self.posted, [])

    def test_two_ticks_never_play_at_once(self):
        self.reading(bad=0, how="raise")
        with main._tick_lock:
            main.game_tick()
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.posted, [])
        main.game_tick()                             # and the lock is free
        self.assert_played()


def thread_item(did, text, rkey, *children):
    return SimpleNamespace(
        post=SimpleNamespace(
            author=SimpleNamespace(did=did, handle=did + ".bsky.social"),
            record=SimpleNamespace(text=text, created_at=rkey,
                                   reply=SimpleNamespace(
                                       parent=SimpleNamespace(uri=TURN),
                                       root=SimpleNamespace(uri=TURN,
                                                            cid="rc"))),
            uri=f"at://{did}/post/{rkey}", cid="c" + rkey),
        replies=list(children))


def notification(did, text, rkey, parent=TURN):
    item = thread_item(did, text, rkey).post
    item.record.reply.parent = SimpleNamespace(uri=parent)
    return SimpleNamespace(author=item.author, record=item.record,
                           uri=item.uri, cid=item.cid, reason="reply")


class Reading(unittest.TestCase):
    """bluesky.read_replies against a stubbed client."""

    def setUp(self):
        self._saved = (bluesky._client, config.POST_MODE)
        config.POST_MODE = "live"
        self.thread, self.count, self.notes = [], 0, []
        self.notes_fail = False
        self.by_depth, self.depths = {}, []

        def get_post_thread(params):
            self.depths.append(params["depth"])
            return SimpleNamespace(thread=SimpleNamespace(
                post=SimpleNamespace(reply_count=self.count),
                replies=self.by_depth.get(params["depth"], self.thread)))

        def list_notifications(params):
            self.asked = params
            if self.notes_fail:
                raise TimeoutError("notifications timed out")
            return SimpleNamespace(notifications=self.notes)

        bluesky._client = SimpleNamespace(app=SimpleNamespace(bsky=SimpleNamespace(
            feed=SimpleNamespace(get_post_thread=get_post_thread),
            notification=SimpleNamespace(
                list_notifications=list_notifications))))

    def tearDown(self):
        bluesky._client, config.POST_MODE = self._saved

    def texts(self):
        replies, complete = bluesky.read_replies(TURN)
        return sorted(r.text for r in replies), complete

    def test_the_27_october_thread_is_read_from_notifications(self):
        # The thread came back empty; the replies were real.
        self.count = 3
        self.notes = [notification("andrew", "Flag f4, e11", "1"),
                      notification("andrew", "E4", "2"),
                      notification("dire", "E10", "3")]
        self.assertEqual(self.texts(),
                         (["E10", "E4", "Flag f4, e11"], True))
        self.assertEqual(self.asked["reasons"], ["reply"])

    def test_an_empty_thread_with_replies_counted_is_short(self):
        self.count = 3
        self.assertEqual(self.texts(), ([], False))
        self.assertEqual(len(set(self.depths)), len(self.depths),
                         "a depth was asked twice; its answer is cached")

    def test_a_cached_empty_answer_is_dodged_by_another_depth(self):
        # Turn 10 on 27 October: 7 replies at depth 5, none at depth 6.
        self.count = 2
        self.thread = [thread_item("andrew", "E4", "2"),
                       thread_item("dire", "E10", "3")]
        self.by_depth = {bluesky._THREAD_DEPTHS[0]: []}
        self.assertEqual(self.texts(), (["E10", "E4"], True))
        self.assertEqual(self.depths, list(bluesky._THREAD_DEPTHS[:2]))

    def test_a_whole_thread_is_asked_for_once(self):
        self.count = 1
        self.thread = [thread_item("andrew", "E4", "2")]
        self.texts()
        self.assertEqual(self.depths, [bluesky._THREAD_DEPTHS[0]])

    def test_a_full_thread_is_complete(self):
        self.count = 2
        self.thread = [thread_item("andrew", "E4", "2"),
                       thread_item("dire", "E10", "3")]
        self.notes = [notification("dire", "E10", "3")]   # not doubled
        self.assertEqual(self.texts(), (["E10", "E4"], True))

    def test_notifications_for_other_posts_are_ignored(self):
        self.notes = [notification("andrew", "E4", "2",
                                   parent="at://did:plc:bot/post/older")]
        self.assertEqual(self.texts(), ([], True))

    def test_follow_ups_still_count_once_against_the_total(self):
        # reply_count counts direct replies; a follow-up under one is extra.
        self.count = 1
        self.thread = [thread_item("tator", "F7", "1",
                                   thread_item("tator", "Meant F8", "2"))]
        self.assertEqual(self.texts(), (["F7", "Meant F8"], True))

    def test_a_notification_failure_still_reads_the_thread(self):
        self.notes_fail = True
        self.count = 1
        self.thread = [thread_item("andrew", "E4", "2")]
        self.assertEqual(self.texts(), (["E4"], True))

    def test_get_replies_is_the_same_read(self):
        self.count = 1
        self.notes = [notification("dire", "E10", "3")]
        self.assertEqual([r.text for r in bluesky.get_replies(TURN)], ["E10"])


if __name__ == "__main__":
    unittest.main()
