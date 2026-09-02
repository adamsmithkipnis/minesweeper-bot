"""Flagging: parsing claims, scoring them, and not leaking the answer.

Flagging is free — it costs no turn — so a reply can flag one cell and vote to
open another. The load-bearing test here is the last class: a correct flag and
a wrong one must be drawn identically, or the board would be telling everyone
where the mines are.
"""

import os
import random
import tempfile
import unittest

from helpers import config, db, game, solver, votes

try:
    import renderer
    HAS_PILLOW = True
except ImportError:                                   # pragma: no cover
    HAS_PILLOW = False

ROWS = COLS = 9


def reply(did, text, when="1"):
    return votes.Reply(did=did, handle=f"{did}.bsky.social", text=text,
                       uri=f"at://{did}/x", cid="c", created_at=when)


class Parsing(unittest.TestCase):
    def flags(self, text):
        return sorted(votes.parse_flags(text, ROWS, COLS)[0])

    def test_the_ways_people_actually_say_it(self):
        for text in ("flag C3", "C3 is a mine", "C3 = mine", "C3 must be a mine",
                     "C3 is definitely a bomb", "🚩 C3", "mine at C3"):
            self.assertEqual(self.flags(text), ["C3"], text)

    def test_lists(self):
        self.assertEqual(self.flags("flag C3 and D5"), ["C3", "D5"])
        self.assertEqual(self.flags("C3, D4 and E5 are all mines"),
                         ["C3", "D4", "E5"])

    def test_a_flag_does_not_swallow_the_vote(self):
        """The whole point: one reply can flag and vote at once."""
        text = "C3 is a mine, so D4 is safe — D4"
        self.assertEqual(self.flags(text), ["C3"])
        self.assertEqual(votes.parse_vote(text, ROWS, COLS), "D4")

    def test_a_full_stop_ends_the_list(self):
        text = "D4. C3 is a mine"
        self.assertEqual(self.flags(text), ["C3"])
        self.assertEqual(votes.parse_vote(text, ROWS, COLS), "D4")

    def test_caution_is_not_a_mine_claim(self):
        # "careful with H8" is a hunch about risk, not a claim about a mine;
        # recording it as one would put words in somebody's mouth.
        for text in ("careful with H8", "don't open C3", "avoid A1"):
            self.assertEqual(self.flags(text), [], text)

    def test_unflagging(self):
        self.assertEqual(votes.parse_flags("unflag C3", ROWS, COLS)[1], {"C3"})

    def test_collect_ignores_open_cells(self):
        flags, _ = votes.collect_flags([reply("a", "flag C3 and D4")],
                                       {"C3"}, ROWS, COLS)
        self.assertEqual(sorted(flags), ["D4"])


class Scoring(unittest.TestCase):
    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "flags.db")
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_one_claim_per_person_per_cell(self):
        for _ in range(3):
            db.add_flag(1, "C3", "did:a", "a.bsky.social", 1)
        self.assertEqual(db.flag_counts(1), {"C3": 1})

    def test_several_people_can_flag_the_same_cell(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        db.add_flag(1, "C3", "did:b", "b", 1)
        self.assertEqual(db.flag_counts(1), {"C3": 2})
        self.assertEqual(db.flagged_coords(1, quorum=2), {"C3"})
        self.assertEqual(db.flagged_coords(1, quorum=3), set())

    def test_unflagging_removes_it(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        db.remove_flag(1, "C3", "did:a")
        self.assertEqual(db.flag_counts(1), {})

    def test_scoring_marks_right_and_wrong(self):
        db.add_flag(1, "C3", "did:a", "a", 1)      # really a mine
        db.add_flag(1, "D4", "did:a", "a", 1)      # not a mine
        db.resolve_flags(1, {"C3": True, "D4": False})
        scores = db.flag_scores(1)
        self.assertEqual(scores, [{"handle": "a", "hits": 1, "total": 2}])

    def test_resolved_claims_leave_the_board(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        db.resolve_flags(1, {"C3": True})
        self.assertEqual(db.flagged_coords(1), set(),
                         "a scored flag should no longer show as pending")

    def test_scoring_is_not_applied_twice(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        self.assertEqual(db.resolve_flags(1, {"C3": True}), 1)
        self.assertEqual(db.resolve_flags(1, {"C3": False}), 0)
        self.assertEqual(db.flag_scores(1)[0]["hits"], 1)

    def test_a_resolved_claim_cannot_be_withdrawn(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        db.resolve_flags(1, {"C3": True})
        db.remove_flag(1, "C3", "did:a")
        self.assertEqual(db.flag_scores(1)[0]["total"], 1)

    def test_boards_are_scored_separately(self):
        db.add_flag(1, "C3", "did:a", "a", 1)
        db.add_flag(2, "C3", "did:a", "a", 1)
        db.resolve_flags(1, {"C3": True})
        self.assertEqual(db.flag_counts(1), {})
        self.assertEqual(db.flag_counts(2), {"C3": 1})


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class DoesNotLeak(unittest.TestCase):
    """A flag says what the crowd thinks, never what the board knows."""

    def test_a_right_flag_and_a_wrong_one_look_identical(self):
        for seed in range(25):
            state = game.new_game(seed, rng=random.Random(seed))
            analysis = solver.analyze(solver.Position.from_state(state))
            if not analysis.mines or not analysis.safe:
                continue
            real = game.index_to_coord(*sorted(analysis.mines)[0])
            wrong = game.index_to_coord(*sorted(analysis.safe)[0])

            grid = renderer.display_grid(state, {real, wrong})
            r1, c1 = game.coord_to_index(real)
            r2, c2 = game.coord_to_index(wrong)
            self.assertEqual(grid[r1][c1], renderer.FLAG)
            self.assertEqual(grid[r2][c2], renderer.FLAG,
                             "a wrong flag must render the same as a right one")

    def test_flags_never_reveal_a_mine_mid_game(self):
        for seed in range(25):
            state = game.new_game(seed, rng=random.Random(seed + 40))
            flags = {game.index_to_coord(r, c) for (r, c) in
                     sorted(state.mine_cells)[:5]}
            grid = renderer.display_grid(state, flags)
            flat = [t for row in grid for t in row]
            self.assertNotIn(renderer.MINE, flat)
            self.assertNotIn(renderer.EXPLODED, flat)

    def test_alt_text_says_a_flag_is_a_claim(self):
        state = game.new_game(1, rng=random.Random(7))
        alt = renderer.build_alt_text(state, {"A1"})
        self.assertIn("somebody has flagged", alt)


if __name__ == "__main__":
    unittest.main()


class Unflagging(unittest.TestCase):
    """Taking a mark off should not read as a request to open the cell."""

    def test_phrasings_that_remove_a_flag(self):
        for text in ("unflag D6", "un-flag D6", "unmark D6",
                     "remove the flag on D6", "remove flag D6",
                     "clear the flag at D6"):
            with self.subTest(text=text):
                claimed, withdrawn = votes.parse_flags(text, 9, 9)
                self.assertIn("D6", withdrawn, f"{text!r} did not unflag")
                self.assertIsNone(votes.parse_vote(text, 9, 9),
                                  f"{text!r} was read as a vote to open")

    def test_unflag_and_vote_in_one_reply(self):
        claimed, withdrawn = votes.parse_flags("unflag D6 and vote D6", 9, 9)
        self.assertIn("D6", withdrawn)
        self.assertEqual(votes.parse_vote("unflag D6 and vote D6", 9, 9), "D6")
