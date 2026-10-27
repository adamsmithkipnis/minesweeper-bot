"""A turn whose replies cannot be read waits; it is never played as empty.

Turn 12 of 27 October posted "nobody moved, so I opened D10" over three
replies on Turn 11. The bot can parse all three; what failed was reading the
thread, and a failed read used to come back as an empty list.
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from helpers import config, db, game, main, votes


def reply(text, did, at):
    return votes.Reply(did=did, handle=f"{did[4:]}.bsky.social", text=text,
                       uri=f"at://{did}/{at}", cid="cid", created_at=at,
                       root_uri="at://root", root_cid="rc")


class UnreadReplies(unittest.TestCase):
    def setUp(self):
        self._saved = (config.DB_PATH, main._FETCH_PAUSE, main.scheduler,
                       main.bluesky.get_replies, main.bluesky.post_with_image,
                       main.bluesky.post_reply, main._remember)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "turn.db")
        db.init_db()
        state = game.new_game(1)
        state.last_post_uri = "at://board/1"
        db.save_state(state)
        self.move = next(
            game.index_to_coord(r, c) for r in range(state.rows)
            for c in range(state.cols)
            if (r, c) not in state.revealed and (r, c) not in state.mine_cells)

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
         main.bluesky.get_replies, main.bluesky.post_with_image,
         main.bluesky.post_reply, main._remember) = self._saved

    def failing(self, times):
        """get_replies that raises `times` times, then returns one move."""
        replies = [reply(self.move, "did:p1", "2026-10-27T13:18:43Z")]

        def get_replies(uri):
            self.reads += 1
            if self.reads <= times:
                raise TimeoutError("read timed out")
            return replies
        main.bluesky.get_replies = get_replies

    def test_a_turn_with_unreadable_replies_is_not_played(self):
        self.failing(times=99)
        before = db.load_state()
        main.game_tick()
        after = db.load_state()
        self.assertEqual(self.posted, [], "a board went out")
        self.assertEqual(after.turn_number, before.turn_number)
        self.assertEqual(after.last_post_uri, "at://board/1")
        self.assertEqual(after.revealed, before.revealed)
        self.assertEqual(self.reads, main._FETCH_ATTEMPTS)

    def test_it_is_retried_in_minutes_not_a_whole_turn(self):
        self.failing(times=99)
        main.game_tick()
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0]["id"], "game_tick_retry")
        self.assertTrue(self.jobs[0]["replace_existing"])

    def test_the_retry_reads_the_moves_and_plays_them(self):
        self.failing(times=main._FETCH_ATTEMPTS)    # this tick fails
        main.game_tick()
        self.assertEqual(self.posted, [])
        main.game_tick()                             # the retry succeeds
        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("nobody moved", self.posted[0])
        self.assertTrue(db.load_state().coord_is_revealed(self.move))

    def test_one_failed_read_is_retried_within_the_tick(self):
        self.failing(times=1)
        main.game_tick()
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.jobs, [])
        self.assertTrue(db.load_state().coord_is_revealed(self.move))

    def test_play_by_hand_does_not_schedule(self):
        main.scheduler = SimpleNamespace(running=False,
                                         add_job=self.fail)
        self.failing(times=99)
        main.game_tick()
        self.assertEqual(self.posted, [])

    def test_two_ticks_never_play_at_once(self):
        self.failing(times=0)
        with main._tick_lock:
            main.game_tick()
        self.assertEqual(self.reads, 0)
        self.assertEqual(self.posted, [])
        main.game_tick()                             # and the lock is free
        self.assertEqual(len(self.posted), 1)


if __name__ == "__main__":
    unittest.main()
