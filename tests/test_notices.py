"""Replies that cannot count should say so, once, instead of vanishing.

The case that prompted this: someone replied "I'll take F6" to a turn post
that had gone stale two turns earlier, naming a cell that was already open.
Either fault alone dropped the vote, and the bot said nothing about either.
"""

import os
import tempfile
import unittest

from helpers import config, db, game, main, votes


def reply(did, text, when="2026-10-15T04:08:08+00:00", uri=None):
    return votes.Reply(did=did, handle=f"{did}.bsky.social", text=text,
                       uri=uri or f"at://{did}/{when}", cid="c",
                       created_at=when)


class OpenCellVotes(unittest.TestCase):
    OPEN = {"F6", "A1"}

    def dead(self, *replies):
        return [(r.did, c) for r, c in
                votes.unplayable_votes(list(replies), self.OPEN, 11, 11)]

    def test_a_vote_for_an_open_cell_is_reported(self):
        self.assertEqual(self.dead(reply("a", "I'll take F6")), [("a", "F6")])

    def test_a_vote_for_a_hidden_cell_is_not(self):
        self.assertEqual(self.dead(reply("a", "I'll take J3")), [])

    def test_a_player_with_a_valid_vote_is_not_bothered(self):
        self.assertEqual(self.dead(reply("a", "F6", "1"),
                                   reply("a", "actually J3", "2")), [])

    def test_a_valid_vote_then_a_dead_one_still_counts_the_valid_one(self):
        # collect() keeps the valid vote, so there is nothing to explain.
        self.assertEqual(self.dead(reply("a", "J3", "1"),
                                   reply("a", "F6", "2")), [])

    def test_chat_without_a_coordinate_is_not_a_vote(self):
        self.assertEqual(self.dead(reply("a", "nice board!")), [])

    def test_a_flag_on_an_open_cell_is_not_a_dead_vote(self):
        self.assertEqual(self.dead(reply("a", "flag F6")), [])

    def test_actionable_means_vote_or_flag(self):
        self.assertTrue(votes.is_actionable("F6", 11, 11))
        self.assertTrue(votes.is_actionable("flag C3", 11, 11))
        self.assertTrue(votes.is_actionable("unflag C3", 11, 11))
        self.assertFalse(votes.is_actionable("good luck everyone", 11, 11))


class Wording(unittest.TestCase):
    def test_notices_fit_a_post_and_name_the_fix(self):
        open_cell = main.build_open_cell_reply("F6")
        stale = main.build_stale_post_reply()
        self.assertIn("F6 is already open", open_cell)
        self.assertIn("newest board post", open_cell)
        self.assertIn("newest board post", stale)
        for text in (open_cell, stale):
            self.assertLessEqual(len(text.encode()), 300)


class Sending(unittest.TestCase):
    def setUp(self):
        self._db, self._mode = config.DB_PATH, config.POST_MODE
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "notices.db")
        db.init_db()
        self.sent = []
        self._real = (main.bluesky.post_reply, main.bluesky.get_replies,
                      main.bluesky.own_did)
        main.bluesky.post_reply = lambda text, **kw: (
            self.sent.append((text, kw)) or f"at://bot/{len(self.sent)}")
        main.bluesky.own_did = lambda: "did:bot"
        self.replies_by_post = {}
        main.bluesky.get_replies = lambda uri: self.replies_by_post.get(uri, [])

    def tearDown(self):
        config.DB_PATH, config.POST_MODE = self._db, self._mode
        (main.bluesky.post_reply, main.bluesky.get_replies,
         main.bluesky.own_did) = self._real

    def state(self):
        state = game.GameState(game_id=7, rows=11, cols=11, mine_count=1,
                               mine_cells={(0, 0)})
        state.last_post_uri = "at://bot/turn3"
        return state

    def seed_posts(self):
        """Three board posts; turn1 and turn2 are stale, turn3 is live."""
        for uri, kind, when in (("at://bot/turn1", "newgame", "10:00:00"),
                                ("at://bot/turn2", "turn", "10:30:00"),
                                ("at://bot/turn3", "turn", "11:00:00")):
            db.record_post(uri, kind, game_id=7)
            with db._connect() as conn:
                conn.execute("UPDATE posts SET created_at = ? WHERE uri = ?",
                             (f"2026-10-15 {when}", uri))

    def test_an_open_cell_vote_is_answered_once(self):
        state = self.state()
        replies = [reply("a", "I'll take F6")]
        for _ in range(3):                      # three ticks re-read it
            main._notify_open_cell_votes(state, replies, {"F6"}, 10)
        self.assertEqual(len(self.sent), 1)
        text, kw = self.sent[0]
        self.assertIn("F6 is already open", text)
        self.assertEqual(kw["parent_uri"], replies[0].uri)

    def test_a_failed_post_is_retried_next_tick(self):
        state = self.state()
        replies = [reply("a", "F6")]
        real = main.bluesky.post_reply

        def boom(*a, **k):
            raise RuntimeError("network")
        main.bluesky.post_reply = boom
        main._notify_open_cell_votes(state, replies, {"F6"}, 10)
        main.bluesky.post_reply = real
        main._notify_open_cell_votes(state, replies, {"F6"}, 10)
        self.assertEqual(len(self.sent), 1)

    def test_the_budget_caps_a_burst(self):
        state = self.state()
        replies = [reply(f"p{i}", "F6", f"2026-10-15T04:00:0{i}+00:00")
                   for i in range(5)]
        sent = main._notify_open_cell_votes(state, replies, {"F6"}, 2)
        self.assertEqual(sent, 2)
        main._notify_open_cell_votes(state, replies, {"F6"}, 10)
        self.assertEqual(len(self.sent), 5)     # the rest follow, none twice

    def test_a_late_reply_to_an_old_post_is_answered_once(self):
        self.seed_posts()
        late = reply("a", "I'll take J3", "2026-10-15T11:30:00+00:00")
        self.replies_by_post["at://bot/turn2"] = [late]
        state = self.state()
        main._notify_stale_post_replies(state, 10)
        main._notify_stale_post_replies(state, 10)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("earlier turn's post", self.sent[0][0])
        self.assertEqual(self.sent[0][1]["parent_uri"], late.uri)

    def test_a_reply_made_while_that_post_was_live_is_left_alone(self):
        self.seed_posts()
        self.replies_by_post["at://bot/turn2"] = [
            reply("a", "J3", "2026-10-15T10:45:00+00:00"),   # live until 11:00
        ]
        self.replies_by_post["at://bot/turn1"] = [
            reply("b", "J3", "2026-10-15T10:20:00+00:00"),   # live until 10:30
        ]
        main._notify_stale_post_replies(self.state(), 10)
        self.assertEqual(self.sent, [])

    def test_chatter_and_the_bots_own_replies_are_not_answered(self):
        self.seed_posts()
        self.replies_by_post["at://bot/turn2"] = [
            reply("a", "good luck everyone", "2026-10-15T11:30:00+00:00"),
            reply("bot", "J3", "2026-10-15T11:30:00+00:00"),
        ]
        # The bot's DID in these fixtures is "bot"; own_did reports did:bot.
        main.bluesky.own_did = lambda: "bot"
        main._notify_stale_post_replies(self.state(), 10)
        self.assertEqual(self.sent, [])

    def test_the_live_post_is_never_called_stale(self):
        self.seed_posts()
        self.replies_by_post["at://bot/turn3"] = [
            reply("a", "J3", "2026-10-15T11:30:00+00:00")]
        main._notify_stale_post_replies(self.state(), 10)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
