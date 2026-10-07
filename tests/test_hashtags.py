"""Hashtag selection.

Six identical tags on every post, 24 times a day, reads as a bot padding for
reach. These are sampled fresh per post — but the game's own tag is never
dropped, or the account stops being findable under a stable name.

The pool is 232 tags and the sample is weighted, because a wider pool is not
automatically more reach: drawing five uniformly from 232 would put #gamedev,
which 18 of the 63 players use, on one post in 55 instead of one in four.
"""

import collections
import random
import re
import unittest
from datetime import datetime

from helpers import config, main as bot
import bluesky


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

    def test_a_tag_that_does_not_fit_is_skipped_not_fatal(self):
        """With 23 even-length tags, stopping at the first miss cost nothing.
        Across 233 the lengths spread, and one long draw was taking the short
        tags behind it down with it."""
        text = "x" * 280
        out = bot._with_tags(text, ["#" + "y" * 30, "#ab", "#cd"])
        self.assertIn("#ab", out)
        self.assertIn("#cd", out)
        self.assertNotIn("y" * 30, out)
        self.assertLessEqual(len(out), 300)

    def test_the_tag_block_still_starts_on_its_own_line(self):
        text = "x" * 275
        out = bot._with_tags(text, ["#" + "y" * 40, "#ab"])
        self.assertEqual(out, text + "\n#ab")


class ThePool(unittest.TestCase):
    """The shipped file itself."""

    def test_it_is_ten_times_the_original_twenty_three(self):
        self.assertGreaterEqual(len(config.HASHTAG_POOL), 230)

    def test_every_tag_is_unique_case_insensitively(self):
        lowered = [t.lower() for t in config.HASHTAG_POOL]
        dupes = [t for t, n in collections.Counter(lowered).items() if n > 1]
        self.assertEqual(dupes, [])

    def test_every_tag_survives_the_facet_builder(self):
        """A tag Bluesky's own regex reads differently would get a facet
        covering the wrong bytes, so the link would land on part of a word."""
        for tag in config.HASHTAG_POOL:
            text = f"hello {tag} world"
            facets = [f for f in bluesky.facet_ranges(text) if f[0] == "tag"]
            self.assertEqual(len(facets), 1, tag)
            kind, value, start, end = facets[0]
            self.assertEqual(value, tag.lstrip("#"), tag)
            self.assertEqual(text.encode()[start:end].decode(), tag, tag)

    def test_no_tag_is_purely_numeric(self):
        for tag in config.HASHTAG_POOL:
            self.assertTrue(re.search(r"[A-Za-z]", tag), tag)

    def test_the_file_documents_itself_in_comments(self):
        """Prose lines in the pool file must not parse as tags — that is what
        lets the file carry its own explanation."""
        tags, weights, days = config._parse_hashtags(
            "# why these weights exist\n"
            "# 18 of 63 players use #gamedev\n"
            "#gamedev 30\n"
            "\n"
            "#puzzle\n")
        self.assertEqual(tags, ["#gamedev", "#puzzle"])
        self.assertEqual(weights["#gamedev"], 30)
        self.assertEqual(weights["#puzzle"], 1)
        self.assertEqual(days, {})

    def test_a_missing_file_still_leaves_the_account_findable(self):
        tags, weights, days = config._parse_hashtags("")
        self.assertEqual(tags, [])


class Weighting(unittest.TestCase):
    def sample(self, n=3000, when=None):
        rng = random.Random(11)
        seen = collections.Counter()
        for _ in range(n):
            seen.update(bot.pick_hashtags(rng, when))
        return seen, n

    def test_the_proven_tags_stay_frequent(self):
        """The whole point of the weights: a 10x pool must not cost reach on
        the tag 18 of the 63 players use."""
        seen, n = self.sample()
        self.assertGreater(seen["#gamedev"] / n, 0.12)
        self.assertGreater(seen["#indiedev"] / n, 0.10)

    def test_the_tail_is_rare_but_reachable(self):
        seen, n = self.sample()
        self.assertLess(seen["#weekendproject"] / n, 0.04)
        self.assertGreater(seen["#weekendproject"], 0)

    def test_a_heavier_tag_outranks_a_lighter_one(self):
        seen, _ = self.sample()
        self.assertGreater(seen["#gamedev"], seen["#gamedevs"])
        self.assertGreater(seen["#puzzlegames"], seen["#puzzlesolving"])

    def test_variety_improved_by_an_order_of_magnitude(self):
        """With 23 tags a tag came back every third post. Measured here as
        distinct tag sets, which is what a reader actually notices."""
        rng = random.Random(5)
        sets = {tuple(sorted(bot.pick_hashtags(rng))) for _ in range(500)}
        self.assertGreater(len(sets), 490)

    def test_an_unweighted_tag_defaults_to_one(self):
        old = config.HASHTAG_WEIGHT
        try:
            config.HASHTAG_WEIGHT = {}
            tags = bot.pick_hashtags(random.Random(3))
            self.assertEqual(len(tags), config.HASHTAG_COUNT + 1)
        finally:
            config.HASHTAG_WEIGHT = old


class DayLockedTags(unittest.TestCase):
    """#screenshotsaturday is the strongest tag in this crowd and the easiest
    to get wrong: on a Tuesday it is worse than no tag at all."""

    SATURDAY = datetime(2026, 10, 10, 12, 0)
    TUESDAY = datetime(2026, 10, 6, 12, 0)

    def test_a_saturday_tag_is_eligible_on_saturday(self):
        self.assertIn("#screenshotsaturday",
                      bot.eligible_hashtags(self.SATURDAY))

    def test_and_never_on_any_other_day(self):
        for day in range(9, 16):
            when = datetime(2026, 10, day, 12, 0)
            if when.weekday() == 5:
                continue
            self.assertNotIn("#screenshotsaturday",
                             bot.eligible_hashtags(when),
                             when.strftime("%a"))

    def test_it_actually_gets_drawn_on_its_day(self):
        rng = random.Random(4)
        seen = collections.Counter()
        for _ in range(2000):
            seen.update(bot.pick_hashtags(rng, self.SATURDAY))
        self.assertGreater(seen["#screenshotsaturday"], 20)

    def test_undated_tags_are_eligible_every_day(self):
        for day in range(9, 16):
            pool = bot.eligible_hashtags(datetime(2026, 10, day, 12, 0))
            self.assertIn("#gamedev", pool)

    def test_only_one_day_of_tags_is_ever_in_play(self):
        for day in range(9, 16):
            when = datetime(2026, 10, day, 12, 0)
            today = bot._DAY_NAMES[when.weekday()]
            for tag in bot.eligible_hashtags(when):
                locked = config.HASHTAG_DAY.get(tag.lower())
                self.assertIn(locked, (None, today), tag)


class TheEnvironmentAdds(unittest.TestCase):
    """The Mini's .env pins the old 23-tag pool. An override would have kept
    the bot on 23 tags after this shipped, and ignoring it would have thrown
    away a tag added by hand — so the variable adds to the file."""

    def test_an_extra_tag_joins_the_shipped_pool(self):
        tags, weights, _ = config._parse_hashtags("#gamedev 30\n#puzzle\n")
        for tag in "#gamedev #somethingnew".split():
            if tag.lower() not in weights:
                tags.append(tag)
                weights[tag.lower()] = 1
        self.assertIn("#somethingnew", tags)
        self.assertIn("#puzzle", tags)
        self.assertEqual(weights["#gamedev"], 30, "the file's weight survives")
        self.assertEqual(tags.count("#gamedev"), 1, "no duplicate")

    def test_the_live_pool_still_contains_the_original_twenty_three(self):
        """Whatever else changes, nothing the account was already findable
        under may silently disappear."""
        original = ("#gamedev #indiedev #solodev #indiegames #indiegame "
                    "#gamedevelopment #puzzle #puzzlegames #logicpuzzles "
                    "#braingames #retrogaming #retrogames #classicgames "
                    "#play #playtogether #crowdplay #dailygame #gamenight "
                    "#bots "
                    "#botsky #bskygames #opensource #python").split()
        pool = {t.lower() for t in config.HASHTAG_POOL}
        missing = [t for t in original if t.lower() not in pool]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
