"""A post that Bluesky published but whose response never arrived is adopted.

Board 92's opening post went up, the response timed out, and the bot carried
on believing it had no post, so the three players who replied to it were
never read or answered.
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from helpers import config  # noqa: F401  (config before bluesky)
import bluesky

POST = "at://did:plc:bot/app.bsky.feed.post/"
PARENT = "at://did:plc:player/app.bsky.feed.post/abc"


def _record(rkey, text, ago=0, parent=""):
    created = datetime.now(timezone.utc) - timedelta(seconds=ago)
    reply = (SimpleNamespace(parent=SimpleNamespace(uri=parent))
             if parent else None)
    return SimpleNamespace(
        uri=POST + rkey, cid="cid-" + rkey,
        value=SimpleNamespace(text=text, created_at=created.isoformat(),
                              reply=reply))


class FakeClient:
    def __init__(self, records=(), list_fails=0):
        self.me = SimpleNamespace(did="did:plc:bot")
        self.records = list(records)
        self.list_fails = list_fails
        self.listed = 0
        self.com = SimpleNamespace(atproto=SimpleNamespace(
            repo=SimpleNamespace(list_records=self._list)))

    def _list(self, params):
        self.listed += 1
        if self.list_fails:
            self.list_fails -= 1
            raise ConnectionError("still down")
        return SimpleNamespace(records=self.records)

    def send_post(self, **kwargs):
        raise TimeoutError("The read operation timed out")

    send_image = send_post


class Adopt(unittest.TestCase):
    def setUp(self):
        self._saved = (bluesky._client, config.POST_MODE,
                       bluesky._ADOPT_PAUSE)
        config.POST_MODE = "live"
        bluesky._ADOPT_PAUSE = 0

    def tearDown(self):
        (bluesky._client, config.POST_MODE,
         bluesky._ADOPT_PAUSE) = self._saved

    def test_a_published_board_post_is_adopted(self):
        text = "💣 NEW BOARD — Tier 3"
        bluesky._client = FakeClient([_record("newer", "other"),
                                      _record("board", text)])
        uri = bluesky.post_with_image(text, b"png", kind="newgame")
        self.assertEqual(uri, POST + "board")
        self.assertEqual(bluesky.cid_of(uri), "cid-board")

    def test_a_post_that_never_went_up_still_fails(self):
        bluesky._client = FakeClient([_record("old", "something else")])
        with self.assertRaises(TimeoutError):
            bluesky.post_text("Turn 4 · 3 players moved")
        self.assertEqual(bluesky._client.listed, bluesky._ADOPT_ATTEMPTS)

    def test_an_older_post_with_the_same_text_is_not_adopted(self):
        text = "Turn 1 · nobody moved"
        bluesky._client = FakeClient([_record("yesterday", text, ago=86400)])
        with self.assertRaises(TimeoutError):
            bluesky.post_text(text)

    def test_a_late_commit_is_found_on_a_later_look(self):
        text = "Turn 2 · 2 players moved"
        bluesky._client = FakeClient([_record("turn", text)], list_fails=1)
        self.assertEqual(bluesky.post_text(text), POST + "turn")

    def test_a_reply_is_matched_to_its_own_parent(self):
        text = "🎯 You opened K5 on turn 5"
        bluesky._client = FakeClient([
            _record("theirs", text, parent=PARENT + "-other"),
            _record("mine", text, parent=PARENT),
        ])
        uri = bluesky.post_reply(text, PARENT, "cid-parent")
        self.assertEqual(uri, POST + "mine")

    def test_a_board_post_is_not_mistaken_for_a_reply(self):
        text = "same words"
        bluesky._client = FakeClient([_record("reply", text, parent=PARENT)])
        with self.assertRaises(TimeoutError):
            bluesky.post_text(text)


if __name__ == "__main__":
    unittest.main()
