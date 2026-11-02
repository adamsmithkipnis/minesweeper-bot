"""Opting out of @mentions: "no tags" and "tag me".

Players are tagged by default, because the mention's notification opens the
new board and regulars use it to jump straight to the next turn. Some find a
mention plus a credit reply per move too much, so either can opt out. An
opted-out name still appears — credit is the point — just without the "@",
which means no facet and no notification.
"""

import os
import random
import tempfile
import unittest

from helpers import config, db, game, main, votes
import bluesky


class ParsingTheRequest(unittest.TestCase):
    def test_ways_of_opting_out(self):
        for text in ("no tags", "No tag please", "notags", "tags off",
                     "untag me", "stop tagging me", "Stop tagging",
                     "don't tag me", "do not tag me", "no mentions",
                     "no @mentions please", "D4 — and no tags"):
            self.assertIs(votes.parse_tag_pref(text), False, text)

    def test_ways_of_opting_back_in(self):
        for text in ("tag me", "Tag me again", "tags on", "start tagging me"):
            self.assertIs(votes.parse_tag_pref(text), True, text)

    def test_dont_tag_me_is_not_tag_me(self):
        self.assertIs(votes.parse_tag_pref("please don't tag me"), False)

    def test_ordinary_replies_are_neither(self):
        for text in ("D4", "flag C3", "check A2", "tag, you're it",
                     "that hashtag is great", "", None):
            self.assertIsNone(votes.parse_tag_pref(text), text)


class Untagging(unittest.TestCase):
    DIDS = {"coil.bsky.social": "did:coil", "coil": "did:coil",
            "mehtis.bsky.social": "did:mehtis"}

    def setUp(self):
        self._saved = set(bluesky._untagged)

    def tearDown(self):
        bluesky.set_untagged(self._saved)

    def test_nobody_opted_out_changes_nothing(self):
        bluesky.set_untagged(())
        text = "🏅 @coil.bsky.social +5 · @mehtis.bsky.social +3"
        self.assertEqual(bluesky.untag(text, self.DIDS), text)

    def test_only_the_opted_out_player_loses_the_at(self):
        bluesky.set_untagged({"did:coil"})
        text = bluesky.untag(
            "🏅 @coil.bsky.social +5 · @mehtis.bsky.social +3", self.DIDS)
        self.assertEqual(text, "🏅 coil.bsky.social +5 · @mehtis.bsky.social +3")
        mentions = [f[1] for f in bluesky.facet_ranges(text, self.DIDS)
                    if f[0] == "mention"]
        self.assertEqual(mentions, ["did:mehtis"])

    def test_the_short_name_is_untagged_too(self):
        bluesky.set_untagged({"did:coil"})
        self.assertEqual(bluesky.untag("📅 BADGE — @coil has now played",
                                       self.DIDS),
                         "📅 BADGE — coil has now played")

    def test_a_name_with_no_did_is_left_alone(self):
        bluesky.set_untagged({"did:coil"})
        self.assertEqual(bluesky.untag("@stranger hi", self.DIDS),
                         "@stranger hi")

    def test_every_post_goes_through_it(self):
        saved = config.POST_MODE, config.DRY_DIR
        config.POST_MODE, config.DRY_DIR = "dry", tempfile.mkdtemp()
        try:
            bluesky.set_untagged({"did:coil"})
            bluesky.post_text("🫡 @coil.bsky.social called it",
                              extra_dids=self.DIDS)
            bluesky.post_reply("@coil.bsky.social hi", "at://p", "c",
                               extra_dids=self.DIDS)
            for name in os.listdir(config.DRY_DIR):
                with open(os.path.join(config.DRY_DIR, name)) as handle:
                    written = handle.read()
                self.assertNotIn("@coil", written, name)
                self.assertNotIn("mention did:coil", written, name)
        finally:
            config.POST_MODE, config.DRY_DIR = saved


class TheNoteInTaggingPosts(unittest.TestCase):
    """A post that tags somebody says how to turn tags off, and why not to."""

    def setUp(self):
        self._saved = set(bluesky._untagged), config.KNOCKOUT
        bluesky.set_untagged(())
        # Seeded: on some boards the news leaves no room, and the note is
        # rightly the thing that goes.
        self.state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        self.state.turn_number = 7

    def tearDown(self):
        untagged, config.KNOCKOUT = self._saved
        bluesky.set_untagged(untagged)

    def plays(self, *who, handle="{}.bsky.social"):
        return [main.Play(f"did:{w}", handle.format(w), "D4", game.SAFE, 5)
                for w in who]

    def vote(self, did="did:coil", handle="coil.bsky.social"):
        caller = votes.Reply(did=did, handle=handle, text="D4")
        return votes.VoteResult("D4", 2, 3, caller)

    def test_a_knockout_post_that_tags_carries_it(self):
        config.KNOCKOUT = True
        text = main.build_knockout_turn_text(self.state, self.plays("coil"))
        self.assertIn(main.TAG_NOTE, text)
        self.assertIn('"no tags"', text)
        self.assertLessEqual(len(text), 300)

    def test_a_voting_post_that_tags_carries_it(self):
        config.KNOCKOUT = False
        text = main.build_turn_text(self.state, "D4", self.vote(), "crowd")
        self.assertIn(main.TAG_NOTE, text)
        self.assertLessEqual(len(text), 300)

    def test_a_post_that_tags_nobody_leaves_it_out(self):
        config.KNOCKOUT = False
        self.assertNotIn(main.TAG_NOTE,
                         main.build_turn_text(self.state, "D4", None, "bot"))
        bluesky.set_untagged({"did:coil"})
        self.assertNotIn(main.TAG_NOTE, main.build_turn_text(
            self.state, "D4", self.vote(), "crowd"))
        config.KNOCKOUT = True
        self.assertNotIn(main.TAG_NOTE, main.build_knockout_turn_text(
            self.state, self.plays("coil")))

    def test_one_tagged_player_is_enough(self):
        config.KNOCKOUT = True
        bluesky.set_untagged({"did:coil"})
        text = main.build_knockout_turn_text(
            self.state, self.plays("coil", "mehtis"))
        self.assertIn(main.TAG_NOTE, text)

    def test_names_win_over_the_note_on_a_crowded_turn(self):
        config.KNOCKOUT = True
        crowded = self.plays(*[f"averylongplayerhandle{i}" for i in range(8)])
        text = main.build_knockout_turn_text(self.state, crowded)
        self.assertLessEqual(len(text), 300)
        self.assertIn("🏅", text)
        self.assertIn("min.", text)


class Storing(unittest.TestCase):
    def setUp(self):
        self._db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "tags.db")
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db

    def test_round_trip(self):
        self.assertTrue(db.set_tagging("did:a", False))
        self.assertFalse(db.set_tagging("did:a", False), "already off")
        self.assertEqual(db.untagged_dids(), {"did:a"})
        self.assertTrue(db.set_tagging("did:a", True))
        self.assertEqual(db.untagged_dids(), set())

    def test_a_reset_keeps_the_preference(self):
        db.set_tagging("did:a", False)
        db.reset_all()
        self.assertEqual(db.untagged_dids(), {"did:a"})


class RecordingFromReplies(Storing):
    def setUp(self):
        super().setUp()
        self.state = game.new_game(1)
        self.sent = []
        self._real = (bluesky.post_reply, bluesky.own_did, main._remember,
                      set(bluesky._untagged))
        bluesky.post_reply = lambda text, **kw: (
            self.sent.append((text, kw)) or "at://ack")
        bluesky.own_did = lambda: "did:bot"
        main._remember = lambda *a, **k: None

    def tearDown(self):
        (bluesky.post_reply, bluesky.own_did, main._remember,
         untagged) = self._real
        bluesky.set_untagged(untagged)
        super().tearDown()

    def reply(self, did, text, when, uri=None):
        return votes.Reply(did=did, handle=did[4:] + ".bsky.social",
                           text=text, uri=uri or f"at://{did}/{when}",
                           cid="c", created_at=when)

    def test_opting_out_is_recorded_confirmed_and_applied(self):
        main._record_tag_prefs(self.state, [self.reply("did:a", "no tags", "1")])
        self.assertEqual(db.untagged_dids(), {"did:a"})
        self.assertEqual(bluesky._untagged, {"did:a"},
                         "this turn's board must already honour it")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("tag me", self.sent[0][0])
        self.assertLessEqual(len(self.sent[0][0]), 300)

    def test_the_newest_request_wins_and_is_the_only_one_answered(self):
        main._record_tag_prefs(self.state, [
            self.reply("did:a", "tag me", "2"),
            self.reply("did:a", "no tags", "1")])
        self.assertEqual(db.untagged_dids(), set())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("again", self.sent[0][0])

    def test_a_reread_is_not_answered_twice(self):
        replies = [self.reply("did:a", "no tags", "1")]
        main._record_tag_prefs(self.state, replies)
        main._record_tag_prefs(self.state, replies)
        self.assertEqual(len(self.sent), 1)

    def test_its_own_confirmation_is_ignored(self):
        main._record_tag_prefs(self.state, [self.reply(
            "did:bot", main.build_tag_pref_reply(False), "1")])
        self.assertEqual(self.sent, [])
        self.assertEqual(db.untagged_dids(), set())

    def test_ordinary_replies_do_nothing(self):
        main._record_tag_prefs(self.state, [self.reply("did:a", "D4", "1")])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
