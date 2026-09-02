"""Vote parsing and tallying.

Battleship's rule was "first coordinate in the reply wins", which is safe when
replies are bare coordinates. Minesweeper replies reason out loud, so most of
these cases are about not mistaking someone's working for their vote.
"""

import unittest

from helpers import votes
from votes import Reply

ROWS = COLS = 9


def vote(text):
    return votes.parse_vote(text, ROWS, COLS)


class BareCoordinates(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(vote("D4"), "D4")
        self.assertEqual(vote("d4"), "D4")
        self.assertEqual(vote("  I9  "), "I9")

    def test_separators(self):
        for text in ("A-5", "A 5", "A,5", "a-5"):
            self.assertEqual(vote(text), "A5", text)

    def test_with_chatter(self):
        self.assertEqual(vote("D4 looks good to me!"), "D4")
        self.assertEqual(vote("gotta be d4 surely"), "D4")


class ArgumentsAndWorking(unittest.TestCase):
    """The failure mode Battleship's parser would walk straight into."""

    def test_deduction_before_the_choice(self):
        self.assertEqual(
            vote("C3 is a mine, so D4 must be safe — D4"), "D4")

    def test_the_mine_is_never_the_vote(self):
        self.assertIsNone(vote("C3 is a mine"))
        self.assertIsNone(vote("B2 is definitely a bomb"))
        self.assertIsNone(vote("E5 = mine"))
        self.assertIsNone(vote("F6 must be a mine"))

    def test_warnings_are_not_votes(self):
        self.assertIsNone(vote("flag C3"))
        self.assertIsNone(vote("don't open C3"))
        self.assertIsNone(vote("avoid A1"))
        self.assertIsNone(vote("careful with H8"))

    def test_warning_then_choice(self):
        self.assertEqual(vote("don't click C3, try D4"), "D4")
        self.assertEqual(vote("not B2 — I vote C7"), "C7")
        self.assertEqual(vote("A1 is a mine so go D9"), "D9")

    def test_vote_word_wins_over_position(self):
        self.assertEqual(vote("between A1 and B2 I pick B2"), "B2")
        self.assertEqual(vote("A1, B2, C3 — vote A1"), "A1")

    def test_last_vote_word_wins(self):
        self.assertEqual(vote("I vote A1, actually I pick B2"), "B2")

    def test_repeated_coordinate(self):
        self.assertEqual(vote("D4 D4 D4"), "D4")

    def test_falls_back_to_the_last_mention(self):
        # No voting word and no warning: people finish on their answer.
        self.assertEqual(vote("I think A1 or B2, probably B2"), "B2")


class DangerSymbols(unittest.TestCase):
    """The most natural way to flag a mine must not open it.

    Word boundaries do not apply to emoji, so these could never match through
    the word-based warning patterns — which made the flag emoji parse as an
    ordinary vote and turned "danger here" into "open this cell".
    """

    def test_flag_emoji_is_never_a_vote(self):
        for text in ("🚩 C3", "🚩C3", "C3 🚩", "🚩 - C3"):
            self.assertIsNone(vote(text), text)

    def test_other_danger_symbols(self):
        for text in ("💣 D4", "D4 💣", "⚠️ E5", "☠️ F6", "C3 ❌", "🛑 B2"):
            self.assertIsNone(vote(text), text)

    def test_a_warning_symbol_does_not_swallow_the_real_vote(self):
        self.assertEqual(vote("🚩 C3 so D4 is safe — D4"), "D4")
        self.assertEqual(vote("💣 A1, I vote B7"), "B7")

    def test_unflagging_is_not_a_vote_to_open(self):
        for text in ("unflag C3", "un-flag C3", "unmark C3"):
            self.assertIsNone(vote(text), text)

    def test_ordinary_votes_are_untouched(self):
        self.assertEqual(vote("D4"), "D4")
        self.assertEqual(vote("I vote D4"), "D4")


class EnglishHazards(unittest.TestCase):
    """Battleship's lesson: be conservative about ordinary words."""

    def test_bare_letters_are_not_votes(self):
        for text in ("a good move", "I agree", "b or c?", "go for it",
                     "A", "I", "yes!"):
            self.assertIsNone(vote(text), text)

    def test_the_pronoun_i_still_allows_i_column_votes(self):
        # 'I' is a word and a row letter. The digit is what makes it a vote.
        self.assertIsNone(vote("I would open something"))
        self.assertEqual(vote("I9"), "I9")
        self.assertEqual(vote("open i9 please"), "I9")

    def test_guards_from_battleship(self):
        self.assertIsNone(vote("sea5"))         # preceded by a letter
        self.assertIsNone(vote("A100"))         # followed by more digits
        self.assertIsNone(vote("that costs A100"))

    def test_off_board_coordinates(self):
        self.assertIsNone(vote("J1"))           # row J does not exist on 9x9
        self.assertIsNone(vote("A0"))
        self.assertIsNone(vote("Z9"))

    def test_pattern_follows_the_board_size(self):
        self.assertEqual(votes.parse_vote("J10", 10, 10), "J10")
        self.assertIsNone(votes.parse_vote("J10", 9, 9))


def reply(did, text, when="1", handle=None):
    return Reply(did=did, handle=handle or f"{did}.bsky.social",
                 text=text, uri=f"at://{did}/x", cid="c", created_at=when)


class Tallying(unittest.TestCase):
    def test_plurality_wins(self):
        result = votes.tally([
            reply("a", "D4", "1"), reply("b", "D4", "2"), reply("c", "E5", "3"),
        ], set(), ROWS, COLS)
        self.assertEqual(result.coord, "D4")
        self.assertEqual(result.votes, 2)
        self.assertEqual(result.total_voters, 3)
        self.assertEqual(result.caller_handle, "a.bsky.social")

    def test_one_vote_per_account_latest_counts(self):
        # People argue themselves around. Counting the earliest reply
        # silently discarded every correction: someone who said "D4" and
        # then "actually F6" watched the bot ignore the second one.
        result = votes.tally([
            reply("a", "D4", "1"), reply("a", "E5", "2"), reply("a", "F6", "3"),
        ], set(), ROWS, COLS)
        self.assertEqual(result.coord, "F6")
        self.assertEqual(result.total_voters, 1)

    def test_a_reply_without_a_coordinate_keeps_your_vote(self):
        result = votes.tally([
            reply("a", "D4", "1"), reply("a", "nice board!", "2"),
        ], set(), ROWS, COLS)
        self.assertEqual(result.coord, "D4")

    def test_flagging_does_not_use_up_your_vote(self):
        # The reported bug: flag a cell, then vote somewhere else.
        result = votes.tally([
            reply("a", "Flag E4", "1"), reply("a", "D4", "2"),
        ], set(), ROWS, COLS)
        self.assertEqual(result.coord, "D4")

    def test_changing_your_mind_hands_credit_to_a_current_voter(self):
        # "a" called B2 first but moved on, so B2 belongs to "b".
        tally_votes, first = votes.collect([
            reply("a", "B2", "1"), reply("a", "C3", "2"), reply("b", "B2", "3"),
        ], set(), ROWS, COLS)
        self.assertEqual(tally_votes["a"], "C3")
        self.assertEqual(first["B2"].handle, "b.bsky.social")
        self.assertEqual(first["C3"].handle, "a.bsky.social")

    def test_ties_go_to_whoever_called_it_first(self):
        result = votes.tally([
            reply("a", "E5", "2"), reply("b", "D4", "1"),
        ], set(), ROWS, COLS)
        self.assertEqual(result.coord, "D4")
        self.assertEqual(result.votes, 1)

    def test_votes_for_open_cells_are_dropped(self):
        result = votes.tally([
            reply("a", "D4", "1"), reply("b", "D4", "2"), reply("c", "E5", "3"),
        ], {"D4"}, ROWS, COLS)
        self.assertEqual(result.coord, "E5")
        self.assertEqual(result.total_voters, 1)

    def test_no_valid_votes(self):
        self.assertIsNone(votes.tally([
            reply("a", "nice board"), reply("b", "flag C3", "2"),
        ], set(), ROWS, COLS))
        self.assertIsNone(votes.tally([], set(), ROWS, COLS))

    def test_breakdown_matches_the_tally(self):
        replies = [reply("a", "D4", "1"), reply("b", "D4", "2"),
                   reply("c", "E5", "3"), reply("d", "junk", "4")]
        rows = votes.breakdown(replies, set(), ROWS, COLS)
        self.assertEqual([r["coord"] for r in rows], ["D4", "E5"])
        self.assertEqual(rows[0]["votes"], 2)
        self.assertEqual(rows[0]["first_caller"], "a.bsky.social")
        self.assertEqual(votes.tally(replies, set(), ROWS, COLS).coord,
                         rows[0]["coord"])


if __name__ == "__main__":
    unittest.main()
