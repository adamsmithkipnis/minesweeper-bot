"""When the crowd decides and when the bot breaks a tie.

The quorum protects a scattered crowd from letting one stray vote carry a
turn. It must never silence a small one: applied strictly, a single player
watches the bot take every turn, which is not a game anybody would play.
"""

import unittest

from helpers import config, main as bot
from votes import VoteResult


def result(votes, voters):
    return VoteResult(coord="D4", votes=votes, total_voters=voters)


class CrowdDecides(unittest.TestCase):
    def setUp(self):
        self._old = config.QUORUM
        config.QUORUM = 2

    def tearDown(self):
        config.QUORUM = self._old

    def test_a_lone_voter_always_decides(self):
        self.assertTrue(bot._crowd_decides(result(votes=1, voters=1)))

    def test_two_voters_who_disagree_still_decide(self):
        # The tie already went to whoever called it first; overriding that
        # would mean two people showed up and neither was listened to.
        self.assertTrue(bot._crowd_decides(result(votes=1, voters=2)))

    def test_two_who_agree_decide(self):
        self.assertTrue(bot._crowd_decides(result(votes=2, voters=2)))

    def test_a_scattered_crowd_does_not(self):
        # Three or more voters and no two agree: nothing has been decided.
        self.assertFalse(bot._crowd_decides(result(votes=1, voters=3)))
        self.assertFalse(bot._crowd_decides(result(votes=1, voters=9)))

    def test_agreement_within_a_crowd_decides(self):
        self.assertTrue(bot._crowd_decides(result(votes=2, voters=9)))

    def test_quorum_of_one_never_overrides(self):
        config.QUORUM = 1
        for voters in range(1, 10):
            self.assertTrue(bot._crowd_decides(result(votes=1, voters=voters)))


if __name__ == "__main__":
    unittest.main()
