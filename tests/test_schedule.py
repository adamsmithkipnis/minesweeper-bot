"""Turn timing across restarts.

Every push redeploys, and every deploy restarts the process. If the schedule
counted from process start, a few deploys in an afternoon would walk the turn
time later and later without anybody noticing.
"""

import unittest
from datetime import datetime, timedelta, timezone

from helpers import main as bot


NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


class NextTurnTime(unittest.TestCase):
    def test_anchors_to_the_last_turn_not_to_startup(self):
        # Last turn 50 minutes ago: the next one is due in 10, not in 60.
        last = NOW - timedelta(minutes=50)
        self.assertEqual(bot.next_turn_time(last, NOW, 60),
                         last + timedelta(minutes=60))
        self.assertEqual(bot.next_turn_time(last, NOW, 60) - NOW,
                         timedelta(minutes=10))

    def test_a_restart_does_not_move_the_deadline(self):
        """Three deploys in a row must not add three hours of delay."""
        last = NOW - timedelta(minutes=5)
        deadlines = {bot.next_turn_time(last, NOW + timedelta(minutes=i), 60)
                     for i in range(0, 50, 10)}
        self.assertEqual(len(deadlines), 1, "restarts changed the deadline")

    def test_fresh_board_waits_a_full_interval(self):
        self.assertEqual(bot.next_turn_time(None, NOW, 60),
                         NOW + timedelta(minutes=60))

    def test_overdue_takes_the_turn_soon_but_not_instantly(self):
        # Down for six hours: play shortly after boot, not six turns at once,
        # and not immediately, so a crash loop cannot burn through the board.
        last = NOW - timedelta(hours=6)
        due = bot.next_turn_time(last, NOW, 60)
        self.assertGreater(due, NOW)
        self.assertLess(due - NOW, timedelta(minutes=1))

    def test_respects_a_shorter_interval(self):
        last = NOW - timedelta(minutes=2)
        self.assertEqual(bot.next_turn_time(last, NOW, 10),
                         last + timedelta(minutes=10))


if __name__ == "__main__":
    unittest.main()
