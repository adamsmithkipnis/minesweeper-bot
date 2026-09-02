"""Hashtag selection.

Six identical tags on every post, 24 times a day, reads as a bot padding for
reach. These are sampled fresh per post — but the game's own tag is never
dropped, or the account stops being findable under a stable name.
"""

import random
import unittest

from helpers import config, main as bot


class Picking(unittest.TestCase):
    def test_the_game_tag_is_always_present(self):
        for seed in range(50):
            tags = bot.pick_hashtags(random.Random(seed))
            self.assertIn(config.HASHTAG_ALWAYS, tags)

    def test_it_comes_first_so_it_survives_a_tight_post(self):
        # Tags are appended while they fit, so position is priority.
        self.assertEqual(bot.pick_hashtags(random.Random(1))[0],
                         config.HASHTAG_ALWAYS)

    def test_count(self):
        tags = bot.pick_hashtags(random.Random(2))
        self.assertEqual(len(tags), config.HASHTAG_COUNT + 1)

    def test_no_duplicates(self):
        for seed in range(50):
            tags = bot.pick_hashtags(random.Random(seed))
            self.assertEqual(len(tags), len(set(tags)), tags)

    def test_all_come_from_the_pool(self):
        allowed = {t.lower() for t in config.HASHTAG_POOL}
        allowed.add(config.HASHTAG_ALWAYS.lower())
        for seed in range(30):
            for tag in bot.pick_hashtags(random.Random(seed)):
                self.assertIn(tag.lower(), allowed)

    def test_the_selection_actually_varies(self):
        seen = {tuple(sorted(bot.pick_hashtags(random.Random(s))))
                for s in range(30)}
        self.assertGreater(len(seen), 15, "tags are barely changing")

    def test_the_pool_is_wide_enough_to_be_worth_sampling(self):
        self.assertGreaterEqual(len(config.HASHTAG_POOL),
                                config.HASHTAG_COUNT * 3)

    def test_survives_a_pool_smaller_than_the_count(self):
        old_pool, old_count = config.HASHTAG_POOL, config.HASHTAG_COUNT
        try:
            config.HASHTAG_POOL = ["#a", "#b"]
            config.HASHTAG_COUNT = 5
            tags = bot.pick_hashtags(random.Random(1))
            self.assertEqual(len(tags), 3)
        finally:
            config.HASHTAG_POOL, config.HASHTAG_COUNT = old_pool, old_count

    def test_tags_never_push_a_post_over_the_limit(self):
        long_text = "x" * 290
        self.assertLessEqual(len(bot._with_tags(long_text)), 300)
        # And the content itself is never sacrificed for a tag.
        self.assertTrue(bot._with_tags(long_text).startswith(long_text))


if __name__ == "__main__":
    unittest.main()
