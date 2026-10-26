"""Check marks: "probably a mine, not sure enough to flag".

Asked for in the October poll. The bar: a check is never read as a move or
a flag, it scores nothing, it shows on the board and in the alt text until
the cell opens or somebody clears it, and each player is told once what it
did — including how to open the cell, since "check D4" used to mean that.
"""

import os
import tempfile
import unittest

from helpers import config, db, game, main, votes
import renderer


def reply(text, did="did:a", at="2026-11-02T10:00:00Z", handle=None):
    return votes.Reply(did=did, handle=handle or f"{did[4:]}.bsky.social",
                       text=text, uri=f"at://{did}/{at}", cid="cid",
                       created_at=at, root_uri="at://root", root_cid="rc")


class Parsing(unittest.TestCase):
    def checks(self, text):
        return votes.parse_checks(text, 9, 9)

    def test_the_word_and_every_tick_mark_it(self):
        for text in ("check D4", "Check d4", "checking D4", "✅ D4", "D4 ✅",
                     "✔️ D4", "D4 ✔", "☑️ D4", "✓ D4"):
            self.assertEqual(self.checks(text), ({"D4"}, set()), text)

    def test_a_check_is_never_a_move(self):
        """The change players will feel: "check D4" used to open D4."""
        for text in ("check D4", "✅ D4", "D4 ✅"):
            self.assertIsNone(votes.parse_vote(text, 9, 9), text)

    def test_nor_a_flag(self):
        self.assertEqual(votes.parse_flags("check D4", 9, 9), (set(), set()))

    def test_it_carries_across_a_list(self):
        self.assertEqual(self.checks("check D4 and D5"), ({"D4", "D5"}, set()))
        self.assertEqual(self.checks("D4, D5 ✅"), ({"D4", "D5"}, set()))

    def test_a_check_and_a_move_in_one_reply(self):
        text = "check D4, so I'll go E5"
        self.assertEqual(self.checks(text), ({"D4"}, set()))
        self.assertEqual(votes.parse_vote(text, 9, 9), "E5")

    def test_unchecking(self):
        for text in ("uncheck D4", "un-check D4", "remove the check from D4",
                     "clear check on D4"):
            self.assertEqual(self.checks(text), (set(), {"D4"}), text)
            self.assertIsNone(votes.parse_vote(text, 9, 9), text)
        self.assertEqual(self.checks("uncheck D4 D5"), (set(), {"D4", "D5"}))

    def test_a_flag_outranks_a_check(self):
        text = "✅ D4 is a mine"
        self.assertEqual(votes.parse_flags(text, 9, 9), ({"D4"}, set()))
        self.assertEqual(self.checks(text), (set(), set()))

    def test_other_phrases_with_check_in_them_are_left_alone(self):
        self.assertEqual(votes.parse_vote("check out D4", 9, 9), "D4")
        self.assertEqual(self.checks("check out D4"), (set(), set()))

    def test_a_check_is_an_actionable_reply(self):
        """So a check on a stale post gets the 'reply on the newest' note."""
        self.assertTrue(votes.is_actionable("check D4", 9, 9))


class Collecting(unittest.TestCase):
    def collect(self, replies, opened=frozenset()):
        return votes.collect_check_changes(replies, set(opened), 9, 9)

    def test_the_latest_word_on_a_cell_wins_from_anybody(self):
        checks, unchecks = self.collect([
            reply("check D4", "did:a", "2026-11-02T10:00:00Z"),
            reply("uncheck D4", "did:b", "2026-11-02T10:01:00Z")])
        self.assertEqual(checks, {})
        self.assertEqual([r.did for r in unchecks["D4"]], ["did:b"])

    def test_only_the_askers_for_the_final_state_are_named(self):
        checks, _ = self.collect([
            reply("check D4", "did:a", "2026-11-02T10:00:00Z"),
            reply("uncheck D4", "did:b", "2026-11-02T10:01:00Z"),
            reply("✅ D4", "did:c", "2026-11-02T10:02:00Z")])
        self.assertEqual([r.did for r in checks["D4"]], ["did:a", "did:c"])

    def test_open_cells_are_ignored(self):
        self.assertEqual(self.collect([reply("check D4")], {"D4"}), ({}, {}))


class Storage(unittest.TestCase):
    def setUp(self):
        self._db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "checks.db")
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._db

    def test_one_shared_mark_per_cell(self):
        self.assertTrue(db.add_check(1, "D4", "did:a", "a", 3))
        self.assertFalse(db.add_check(1, "D4", "did:b", "b", 4))
        self.assertEqual(db.checked_coords(1), {"D4"})

    def test_anyone_can_clear_it(self):
        db.add_check(1, "D4", "did:a", "a", 3)
        self.assertTrue(db.remove_check(1, "D4"))
        self.assertFalse(db.remove_check(1, "D4"))
        self.assertEqual(db.checked_coords(1), set())

    def test_marks_belong_to_their_board(self):
        db.add_check(1, "D4", "did:a", "a", 3)
        self.assertEqual(db.checked_coords(2), set())

    def test_the_explanation_is_claimed_once(self):
        self.assertTrue(db.claim_check_tutorial("did:a"))
        self.assertFalse(db.claim_check_tutorial("did:a"))
        db.release_check_tutorial("did:a")
        self.assertTrue(db.claim_check_tutorial("did:a"))

    def test_a_full_reset_clears_them(self):
        db.add_check(1, "D4", "did:a", "a", 3)
        db.claim_check_tutorial("did:a")
        db.reset_all()
        self.assertEqual(db.checked_coords(1), set())
        self.assertTrue(db.claim_check_tutorial("did:a"))

    def test_a_check_scores_nothing(self):
        state = game.new_game(1)
        db.add_check(1, "D4", "did:a", "a", 3)
        self.assertEqual(db.player_points("did:a"), 0)
        self.assertEqual(db.leaderboard(), [])
        self.assertEqual(db.flag_scores(), [])
        self.assertEqual(state.mine_count, game.new_game(1).mine_count)


class Showing(unittest.TestCase):
    def board(self):
        state = game.new_game(1)
        hidden = [game.index_to_coord(r, c) for r in range(state.rows)
                  for c in range(state.cols) if (r, c) not in state.revealed]
        return state, hidden

    def test_a_marked_hidden_cell_shows_as_C(self):
        state, hidden = self.board()
        r, c = game.coord_to_index(hidden[0])
        self.assertEqual(renderer.display_grid(state, (), {hidden[0]})[r][c],
                         renderer.CHECK)

    def test_a_flag_shows_over_a_check(self):
        state, hidden = self.board()
        r, c = game.coord_to_index(hidden[0])
        grid = renderer.display_grid(state, {hidden[0]}, {hidden[0]})
        self.assertEqual(grid[r][c], renderer.FLAG)

    def test_an_opened_cell_drops_its_mark(self):
        state, _ = self.board()
        opened = game.index_to_coord(*next(iter(state.revealed)))
        grid = renderer.display_grid(state, (), {opened})
        self.assertNotIn(renderer.CHECK, [t for row in grid for t in row])

    def test_the_alt_text_counts_and_explains_them(self):
        state, hidden = self.board()
        alt = renderer.build_alt_text(state, (), set(hidden[:2]))
        self.assertIn("2 marked to check", alt)
        self.assertIn("C is a cell somebody has marked to check", alt)
        self.assertNotIn("marked to check", renderer.build_alt_text(state))

    def test_the_tick_is_drawn(self):
        state, hidden = self.board()
        plain = renderer.render_board(state, checks=())
        marked = renderer.render_board(state, checks={hidden[0]})
        self.assertNotEqual(plain, marked)

    def test_the_board_draws_the_emoji_itself(self):
        """Players type ✔️, so the board shows ✔️ — where the font exists."""
        if renderer._check_glyph(32) is None:
            self.skipTest("no Apple Color Emoji on this machine")
        glyph = renderer._check_glyph(32)
        self.assertEqual(glyph.mode, "RGBA")
        self.assertLessEqual(max(glyph.size), 32)

    def test_without_the_font_it_still_draws_a_check(self):
        state, hidden = self.board()
        saved = renderer._EMOJI_FONT
        renderer._EMOJI_FONT, renderer._check_glyphs = "/no/such/font", {}
        try:
            plain = renderer.render_board(state, checks=())
            marked = renderer.render_board(state, checks={hidden[0]})
        finally:
            renderer._EMOJI_FONT, renderer._check_glyphs = saved, {}
        self.assertNotEqual(plain, marked)

    def test_the_explanation_fits_and_says_how_to_open(self):
        for coords in (["D4"], ["J10", "J11", "K10", "K11"]):
            text = main.build_check_reply(coords)
            self.assertLessEqual(len(text), 300, coords)
            self.assertIn(f"Reply just {coords[0]}", text)
            self.assertIn("scores nothing", text)


class LiveTurn(unittest.TestCase):
    """game_tick end to end in both modes, with the network stubbed."""

    def setUp(self):
        self._saved = (config.DB_PATH, config.KNOCKOUT)
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "turn.db")
        db.init_db()
        self.state = game.new_game(1)
        self.state.last_post_uri = "at://board/1"
        db.save_state(self.state)
        self.hidden = sorted(
            game.index_to_coord(r, c) for r in range(self.state.rows)
            for c in range(self.state.cols)
            if (r, c) not in self.state.revealed
            and (r, c) not in self.state.mine_cells)
        self.posted, self.replies_sent = [], []
        self._real = (main.bluesky.get_replies, main.bluesky.post_with_image,
                      main.bluesky.post_reply, main._remember)
        main.bluesky.post_with_image = lambda text, image, alt="", **kw: (
            self.posted.append(alt) or "at://board/2")
        main.bluesky.post_reply = lambda text, **kw: (
            self.replies_sent.append(text) or "at://reply/1")
        main._remember = lambda *a, **k: None

    def tearDown(self):
        (main.bluesky.get_replies, main.bluesky.post_with_image,
         main.bluesky.post_reply, main._remember) = self._real
        config.DB_PATH, config.KNOCKOUT = self._saved

    def play(self, texts, knockout):
        config.KNOCKOUT = knockout
        replies = [reply(t, f"did:p{i}", f"2026-11-02T10:0{i}:00Z")
                   for i, t in enumerate(texts)]
        main.bluesky.get_replies = lambda uri: replies
        main.game_tick()

    def check_turn(self, knockout):
        target, move = self.hidden[-1], self.hidden[0]
        self.play([f"check {target}", move], knockout)
        self.assertIn(target, db.checked_coords(1))
        self.assertTrue(self.posted, "the board posted")
        self.assertIn("marked to check", self.posted[-1])
        explained = [t for t in self.replies_sent if "Marked" in t]
        self.assertEqual(len(explained), 1)
        self.assertFalse(db.load_state().coord_is_revealed(target)
                         if target != move else False)

    def test_voting_mode(self):
        self.check_turn(knockout=False)

    def test_knockout_mode(self):
        self.check_turn(knockout=True)

    def test_a_knocked_out_player_can_still_check(self):
        """The bench stops moves, not notes: like a flag, a check costs no
        turn and risks nothing, so sitting out is no reason to refuse it."""
        db.eliminate(1, "did:p0", "p0.bsky.social", "A1",
                     self.state.turn_number)
        target, move = self.hidden[-1], self.hidden[0]
        self.play([f"check {target}, then {move}"], knockout=True)
        self.assertIn(target, db.checked_coords(1))
        self.assertEqual([m for m in db.get_moves(1) if m["did"] == "did:p0"],
                         [], "the benched player's move was played")

    def test_the_knocked_out_reply_says_so(self):
        state = game.new_game(1)
        state.turn_number = 9999
        mine = main.Play("did:n", "newcomer.bsky.social", "J10", game.MINE,
                         reply_uri="at://n/x", reply_cid="c")
        text = main.build_knockout_credit_reply(state, mine, 9999, 999999,
                                                first_move=True)
        self.assertIn("You can still flag & check cells while you wait.", text)
        self.assertLessEqual(len(text), 300)

    def test_the_explanation_is_sent_once_per_player(self):
        target = self.hidden[-1]
        self.play([f"check {target}"], knockout=True)
        self.play([f"check {self.hidden[-2]}"], knockout=True)
        self.assertEqual(len([t for t in self.replies_sent if "Marked" in t]),
                         1)


if __name__ == "__main__":
    unittest.main()
