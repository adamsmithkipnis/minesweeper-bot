"""The follower history is a private record: appended, ordered, never posted."""

import os
import tempfile
import unittest

from helpers import config, db, main


class FollowerHistory(unittest.TestCase):
    def setUp(self):
        self._db = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "followers.db")
        db.init_db()
        self._real = main.bluesky.get_profile_counts

    def tearDown(self):
        config.DB_PATH = self._db
        main.bluesky.get_profile_counts = self._real

    def test_readings_accumulate_oldest_first(self):
        db.record_followers(10, 5, 100)
        db.record_followers(12, 5, 104)
        rows = db.follower_history()
        self.assertEqual([r["followers"] for r in rows], [10, 12])
        self.assertTrue(rows[0]["created_at"])

    def test_snapshot_records_what_the_account_reports(self):
        main.bluesky.get_profile_counts = lambda: {
            "followers": 42, "follows": 7, "posts": 900}
        main.snapshot_followers()
        self.assertEqual(db.follower_history()[0]["followers"], 42)

    def test_a_dry_run_records_nothing(self):
        main.bluesky.get_profile_counts = lambda: None
        main.snapshot_followers()
        self.assertEqual(db.follower_history(), [])

    def test_a_failed_lookup_is_a_gap_not_a_crash(self):
        def boom():
            raise RuntimeError("network")
        main.bluesky.get_profile_counts = boom
        main.snapshot_followers()
        self.assertEqual(db.follower_history(), [])

    def test_a_full_reset_keeps_the_history(self):
        db.record_followers(10)
        db.reset_all()
        self.assertEqual(len(db.follower_history()), 1)

    def test_csv_export(self):
        db.record_followers(10, 5, 100)
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.print_follower_history()
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "created_at_utc,followers,follows,posts")
        self.assertTrue(lines[1].endswith(",10,5,100"))


if __name__ == "__main__":
    unittest.main()
