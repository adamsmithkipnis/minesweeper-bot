"""A player's replies under their own move are read; other people's are not.

@tatorlog replied "F7" to the turn post, then "Cancel F7. Meant F8" under
that. Only direct replies were fetched, so the correction was never seen and
F7 opened.
"""

import unittest
from types import SimpleNamespace

from helpers import config, votes  # noqa: F401  (config before bluesky)
import bluesky
import dashboard

TURN = "at://did:plc:bot/app.bsky.feed.post/turn"


def node(did, text, when, *children):
    """A thread item shaped like atproto's ThreadViewPost."""
    return SimpleNamespace(
        post=SimpleNamespace(
            author=SimpleNamespace(did=did, handle=did + ".bsky.social"),
            record=SimpleNamespace(text=text, created_at=when, reply=None),
            uri=f"at://{did}/post/{when}", cid="c" + when),
        replies=list(children))


def as_json(item):
    """The same item as the public AppView returns it to the dashboard."""
    post = item.post
    return {"post": {"uri": post.uri, "cid": post.cid,
                     "author": {"did": post.author.did,
                                "handle": post.author.handle},
                     "record": {"text": post.record.text,
                                "createdAt": post.record.created_at}},
            "replies": [as_json(c) for c in item.replies]}


# The real thread, plus the cases the rule has to get right.
THREAD = [
    node("tator", "F7", "01",
         node("bot", "🎯 You opened F7", "05"),
         node("tator", "Cancel F7. Meant F8", "02",
              node("tator", "Flag E1", "03"))),
    node("andrew", "H4", "04",
         node("alex", "good call, but D4 is better", "06",
              node("andrew", "D4 then", "07"))),
]


class Walk(unittest.TestCase):
    def texts(self, items):
        return [i.post.record.text for i in votes.with_follow_ups(
            items, bluesky._author_did, lambda i: i.replies)]

    def test_own_follow_ups_are_read_at_any_depth(self):
        texts = self.texts(THREAD)
        self.assertIn("Cancel F7. Meant F8", texts)
        self.assertIn("Flag E1", texts)

    def test_other_peoples_replies_under_a_move_are_not(self):
        texts = self.texts(THREAD)
        self.assertNotIn("🎯 You opened F7", texts)
        self.assertNotIn("good call, but D4 is better", texts)

    def test_the_chain_breaks_at_someone_else(self):
        # andrew's "D4 then" sits under alex, not under andrew's own move.
        self.assertNotIn("D4 then", self.texts(THREAD))

    def test_a_reply_with_no_author_carries_nothing(self):
        orphan = node("", "F7", "01", node("", "F8", "02"))
        self.assertEqual(self.texts([orphan]), ["F7"])


class BotAndDashboard(unittest.TestCase):
    def setUp(self):
        self._saved = (bluesky._client, config.POST_MODE, dashboard._http)
        config.POST_MODE = "live"
        thread = SimpleNamespace(replies=THREAD)
        self.asked = {}

        def get_post_thread(params):
            self.asked["bot"] = params
            return SimpleNamespace(thread=thread)

        bluesky._client = SimpleNamespace(app=SimpleNamespace(
            bsky=SimpleNamespace(feed=SimpleNamespace(
                get_post_thread=get_post_thread))))

        def get(url, params):
            self.asked["dashboard"] = params
            body = {"thread": {"replies": [as_json(i) for i in THREAD]}}
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: body)

        dashboard._http = SimpleNamespace(get=get)

    def tearDown(self):
        bluesky._client, config.POST_MODE, dashboard._http = self._saved

    def test_the_correction_replaces_the_move(self):
        moves = votes.moves(bluesky.get_replies(TURN), set(), 11, 11)
        self.assertEqual({m.did: m.coord for m in moves},
                         {"tator": "F8", "andrew": "H4"})
        self.assertGreater(self.asked["bot"]["depth"], 1)

    def test_the_dashboard_reads_the_same_replies(self):
        bot = sorted(r.uri for r in bluesky.get_replies(TURN))
        board = sorted(r.uri for r in dashboard.live_replies(TURN))
        self.assertEqual(bot, board)
        self.assertEqual(self.asked["dashboard"]["depth"],
                         self.asked["bot"]["depth"])


if __name__ == "__main__":
    unittest.main()
