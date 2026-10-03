"""Knockout play: everyone acts, a mine costs the player not the board.

Letting everyone act is ruinous under sudden death — with N players each
independently risking a mine, survival per turn is skill^N, so at the 87.5%
accuracy this game actually shows, five players clear 52% of boards in five
turns, and it gets *worse* as the audience grows. Making a mine cost the
player instead of the board inverts that: eight players clear 98%.
"""

import copy
import os
import random
import tempfile
import unittest

from helpers import config, db, game, main, solver, votes

try:
    import renderer
    HAS_PILLOW = True
except ImportError:                                   # pragma: no cover
    HAS_PILLOW = False


def reply(did, text, when="1"):
    return votes.Reply(did=did, handle=f"{did}.bsky.social", text=text,
                       uri=f"at://{did}/x", cid="c", created_at=when)


class Budget(unittest.TestCase):
    def board(self, budget=2, seed=3):
        return game.new_game(1, rng=random.Random(seed), mine_budget=budget)

    def test_the_board_absorbs_its_budget_then_fails(self):
        state = self.board(budget=2)
        mines = sorted(state.mine_cells)
        for i, cell in enumerate(mines[:2]):
            self.assertEqual(game.reveal(state, *cell), game.MINE)
            self.assertEqual(state.status, game.ACTIVE,
                             f"board should survive detonation {i + 1}")
        game.reveal(state, *mines[2])
        self.assertEqual(state.status, game.EXPLODED)
        self.assertEqual(state.detonations, 3)

    def test_budget_zero_is_the_original_game(self):
        state = self.board(budget=0)
        game.reveal(state, *sorted(state.mine_cells)[0])
        self.assertEqual(state.status, game.EXPLODED)

    def test_spares_left_counts_down(self):
        state = self.board(budget=2)
        self.assertEqual(state.spares_left, 2)
        game.reveal(state, *sorted(state.mine_cells)[0])
        self.assertEqual(state.spares_left, 1)

    def test_a_spent_mine_cannot_be_hit_twice(self):
        state = self.board(budget=3)
        cell = sorted(state.mine_cells)[0]
        self.assertEqual(game.reveal(state, *cell), game.MINE)
        self.assertEqual(game.reveal(state, *cell), game.ALREADY)
        self.assertEqual(state.detonations, 1, "a second hit must not re-charge")

    def test_spent_mines_do_not_count_towards_clearing(self):
        """They are revealed but they are not safe cells."""
        state = self.board(budget=99)
        for cell in sorted(state.mine_cells)[:5]:
            game.reveal(state, *cell)
        self.assertNotIn(sorted(state.mine_cells)[0], state.revealed)
        self.assertFalse(state.is_cleared())

    def test_budget_scales_with_the_tier(self):
        saved = config.KNOCKOUT
        try:
            config.KNOCKOUT = True
            self.assertEqual([main.mine_budget_for(t) for t in range(3)],
                             [1, 2, 3])
            config.KNOCKOUT = False
            self.assertEqual(main.mine_budget_for(2), 0,
                             "sudden death when knockout is off")
        finally:
            config.KNOCKOUT = saved


class Moves(unittest.TestCase):
    def test_one_move_per_player_their_latest(self):
        got = votes.moves([reply("a", "D4", "1"), reply("b", "E5", "2"),
                           reply("a", "actually F6", "3")], set(), 9, 9)
        self.assertEqual([(m.did, m.coord) for m in got],
                         [("a", "F6"), ("b", "E5")])

    def test_knocked_out_players_do_not_act(self):
        got = votes.moves([reply("a", "D4"), reply("b", "E5", "2")],
                          set(), 9, 9, exclude={"a"})
        self.assertEqual([m.did for m in got], ["b"])

    def test_a_flag_only_reply_is_not_a_move(self):
        self.assertEqual(votes.moves([reply("a", "flag C3")], set(), 9, 9), [])

    def test_knocked_out_players_can_still_flag(self):
        """Elimination is a change of role, not an exit."""
        flags, _ = votes.collect_flags([reply("a", "flag C3")], set(), 9, 9)
        self.assertEqual(sorted(flags), ["C3"])


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class CascadeCollisions(unittest.TestCase):
    def setUp(self):
        self._knockout, self._db = config.KNOCKOUT, config.DB_PATH
        config.KNOCKOUT = True
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "cascade.db")
        db.init_db()

    def tearDown(self):
        config.KNOCKOUT, config.DB_PATH = self._knockout, self._db

    def _cascade_pair(self, state):
        """A hidden click and another hidden cell its flood-fill opens."""
        hidden = [(r, c) for r in range(state.rows) for c in range(state.cols)
                  if (r, c) not in state.revealed
                  and (r, c) not in state.mine_cells]
        for first in hidden:
            candidate = copy.deepcopy(state)
            before = set(candidate.revealed)
            if game.reveal(candidate, *first) != game.SAFE:
                continue
            opened = set(candidate.revealed) - before - {first}
            if opened:
                return (game.index_to_coord(*first),
                        game.index_to_coord(*sorted(opened)[0]))
        self.fail("seed produced no hidden cascade pair")

    def test_a_move_consumed_by_an_earlier_cascade_is_preserved(self):
        state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        first, consumed = self._cascade_pair(state)
        position = solver.Position.from_state(state)
        analysis = solver.analyze(position)
        already = {game.index_to_coord(r, c) for r, c in state.revealed}

        plays, source, superseded = main.apply_knockout_moves(
            state,
            [reply("first", first, "1"), reply("second", consumed, "2")],
            already, position, analysis)

        self.assertEqual(source, "crowd")
        self.assertEqual([p.did for p in plays], ["first"])
        self.assertEqual([(m.did, m.coord) for m in superseded],
                         [("second", consumed)])

    def test_calling_the_same_cell_is_not_mislabeled_as_a_cascade(self):
        state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        first, _ = self._cascade_pair(state)
        position = solver.Position.from_state(state)
        analysis = solver.analyze(position)
        already = {game.index_to_coord(r, c) for r, c in state.revealed}

        plays, source, superseded = main.apply_knockout_moves(
            state,
            [reply("first", first, "1"), reply("second", first, "2")],
            already, position, analysis)

        self.assertEqual(source, "crowd")
        self.assertEqual([p.did for p in plays], ["first"])
        self.assertEqual(superseded, [])

    def test_the_skipped_player_is_told_why_and_where_to_play(self):
        state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        state.turn_number = 14
        move = votes.Move("did:a", "a.bsky.social", "E3",
                          reply("did:a", "E3"))
        sent = []
        real_reply, real_remember = main.bluesky.post_reply, main._remember
        main.bluesky.post_reply = lambda text, **kw: (
            sent.append((text, kw)) or "at://sent/1")
        main._remember = lambda *a, **k: None
        try:
            main._notify_cascade_skips(state, [move])
        finally:
            main.bluesky.post_reply, main._remember = real_reply, real_remember

        self.assertEqual(len(sent), 1)
        text, kwargs = sent[0]
        self.assertIn("E3 was valid", text)
        self.assertIn("cascade opened it", text)
        self.assertIn("earned 0 points", text)
        self.assertIn("Turn 14 is now open", text)
        self.assertEqual(kwargs["parent_uri"], move.reply.uri)
        self.assertLessEqual(len(text), 300)

    def test_a_finishing_cascade_does_not_claim_a_new_turn_is_open(self):
        state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        state.status = game.CLEARED
        move = votes.Move("did:a", "a.bsky.social", "E3",
                          reply("did:a", "E3"))
        text = main.build_cascade_skip_reply(state, move)
        self.assertIn("next board begins soon", text)
        self.assertNotIn("now open", text)


class Persistence(unittest.TestCase):
    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "knockout.db")
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_knockout_state_survives_a_reload(self):
        """Without this the board reloads with budget 0 every turn and the
        first mine ends it — which is exactly what happened first time."""
        state = game.new_game(1, rng=random.Random(3), mine_budget=2)
        game.reveal(state, *sorted(state.mine_cells)[0])
        db.save_state(state)
        back = db.load_state()
        self.assertEqual(back.mine_budget, 2)
        self.assertEqual(back.detonations, 1)
        self.assertEqual(back.spent_mines, state.spent_mines)
        self.assertEqual(back.spares_left, 1)

    def test_eliminations_are_recorded_once_per_board(self):
        for _ in range(3):
            db.eliminate(1, "did:a", "a", "C3", 4)
        self.assertEqual(db.eliminated(1), {"did:a"})
        self.assertEqual(len(db.eliminations(1)), 1)

    def test_eliminations_are_per_board(self):
        db.eliminate(1, "did:a", "a", "C3", 4)
        self.assertEqual(db.eliminated(2), set())


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class Copy(unittest.TestCase):
    def setUp(self):
        self._old = config.KNOCKOUT
        config.KNOCKOUT = True

    def tearDown(self):
        config.KNOCKOUT = self._old

    def _state(self):
        return game.new_game(1, rng=random.Random(5), mine_budget=2)

    def test_a_turn_post_names_the_knockout_and_fits(self):
        state = self._state()
        state.turn_number = 7
        plays = [main.Play("did:a", "alice.bsky.social", "C3", game.MINE),
                 main.Play("did:b", "bob.bsky.social", "D4", game.SAFE, 5)]
        text = main.build_knockout_turn_text(state, plays)
        self.assertIn("hit C3 and is out", text)
        self.assertIn("@bob.bsky.social +5", text)
        self.assertLessEqual(len(text), 300)

    def test_a_turn_post_stays_within_the_limit_when_crowded(self):
        state = self._state()
        state.turn_number = 12
        plays = [main.Play(f"did:{i}", f"averyverylongplayerhandle{i}.bsky.social",
                           "C3", game.MINE) for i in range(3)]
        self.assertLessEqual(len(main.build_knockout_turn_text(state, plays)), 300)

    def test_the_prompt_does_not_mention_voting(self):
        text = main.build_knockout_turn_text(
            self._state(), [main.Play("did:a", "a", "D4", game.SAFE, 1)])
        self.assertNotIn("vote", text.lower())
        self.assertIn("Everyone who replies", text)

    def test_the_ending_reports_survivors(self):
        state = self._state()
        state.status = game.CLEARED
        state.turn_number = 14
        state.detonations = 3
        text = main.build_knockout_gameover_text(
            state, {"cleared": 2, "exploded": 1}, survivors=4, entrants=7,
            mvp_line="🏅 Board MVP: @a.test — 30 points.", next_board_at=None)
        self.assertIn("4 of 7 still standing", text)
        self.assertIn("3 mines found the hard way", text)
        self.assertLessEqual(len(text), 300)

    def test_a_lost_board_says_so(self):
        state = self._state()
        state.status = game.EXPLODED
        state.exploded_cell = "G7"
        state.turn_number = 9
        text = main.build_knockout_gameover_text(
            state, {"cleared": 1, "exploded": 2}, survivors=1, entrants=5,
            mvp_line="", next_board_at=None)
        self.assertIn("BOARD LOST", text)
        self.assertIn("G7", text)
        self.assertLessEqual(len(text), 300)


if __name__ == "__main__":
    unittest.main()


class EliminationExpiry(unittest.TestCase):
    """Being knocked out costs a few turns, not the rest of the day.

    With two active players and a permanent elimination, a tier-3 board
    benches someone for a median of 40 turns with a tail near 500, and the
    bot plays 102 turns alone because the board cannot finish without them.
    At four turns those become 3, 6 and 0.4.
    """

    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "expiry.db")
        db.init_db()
        db.eliminate(1, "did:a", "a", "C3", turn_number=5)

    def tearDown(self):
        config.DB_PATH = self._old

    def test_benched_for_exactly_the_configured_turns(self):
        for turn in (6, 7, 8, 9):
            self.assertEqual(db.benched(1, turn, 4), {"did:a"},
                             f"should still be out on turn {turn}")
        self.assertEqual(db.benched(1, 10, 4), set(),
                         "should be back on turn 10")

    def test_zero_means_the_whole_board(self):
        for turn in (6, 50, 500):
            self.assertEqual(db.benched(1, turn, 0), {"did:a"})

    def test_the_tally_still_counts_everyone_ever_knocked_out(self):
        """Survivor counts must not forget somebody who came back."""
        self.assertEqual(db.eliminated(1), {"did:a"})
        self.assertEqual(db.benched(1, 20, 4), set())
        self.assertEqual(db.eliminated(1), {"did:a"})

    def test_a_second_elimination_restarts_the_clock(self):
        db.eliminate(1, "did:b", "b", "D4", turn_number=12)
        self.assertEqual(db.benched(1, 13, 4), {"did:b"})
        self.assertNotIn("did:a", db.benched(1, 13, 4))


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed (use .venv/bin/python)")
class EveryMoverIsAcknowledged(unittest.TestCase):
    """Everyone who acted is named, and everyone who acted is answered.

    Knockout has no single winning caller, so a turn post that names only the
    best scorer leaves most of the people who played invisible — and the board
    post has no room for running totals, which is what people come back for.
    """

    def setUp(self):
        self._knockout = config.KNOCKOUT
        config.KNOCKOUT = True
        self.state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        self.state.turn_number = 7

    def tearDown(self):
        config.KNOCKOUT = self._knockout

    def plays(self, n, points=5, handle="player{}.bsky.social"):
        return [main.Play(f"did:{i}", handle.format(i), "D4", game.SAFE,
                          points - i, reply_uri=f"at://did:{i}/x",
                          reply_cid="c") for i in range(n)]

    def test_all_scorers_are_named_when_they_fit(self):
        text = main.build_knockout_turn_text(self.state, self.plays(3))
        for i in range(3):
            self.assertIn(f"@player{i}.bsky.social", text)

    def test_a_crowded_turn_counts_the_ones_it_cannot_fit(self):
        text = main.build_knockout_turn_text(
            self.state,
            self.plays(8, handle="averylongplayerhandle{}.bsky.social"))
        self.assertLessEqual(len(text), 300)
        self.assertIn("more", text)
        self.assertIn("@averylongplayerhandle0.bsky.social", text,
                      "the best scorer must survive the trim")

    def test_the_prompt_always_survives(self):
        """The scorer line used to pass its own budget check and then be
        dropped whole by the final trim, taking every name with it."""
        for count in (1, 3, 8, 20):
            text = main.build_knockout_turn_text(self.state, self.plays(count))
            self.assertLessEqual(len(text), 300, f"{count} scorers")
            self.assertIn("min.", text, f"{count} scorers lost the prompt")
            self.assertIn("🏅", text, f"{count} scorers lost the scoreline")

    def test_the_reply_carries_both_totals(self):
        reply = main.build_knockout_credit_reply(
            self.state, self.plays(1)[0], this_board=14, all_time=203)
        self.assertIn("+5 points", reply)
        self.assertIn("This board: 14", reply)
        self.assertIn("All time: 203", reply)
        self.assertLessEqual(len(reply), 300)

    def test_the_reply_to_a_knocked_out_player_says_how_long(self):
        play = main.Play("did:a", "a.bsky.social", "G7", game.MINE,
                         reply_uri="at://a/x", reply_cid="c")
        reply = main.build_knockout_credit_reply(self.state, play, 14, 203)
        self.assertIn("a mine", reply)
        self.assertIn(f"{config.ELIMINATION_TURNS} turns", reply)
        self.assertIn("All time: 203", reply)
        self.assertLessEqual(len(reply), 300)

    def test_every_player_who_acted_gets_answered(self):
        sent = []
        real_reply, real_points, real_remember = (
            main.bluesky.post_reply, main.db.player_points, main._remember)
        main.bluesky.post_reply = lambda text, **kw: (
            sent.append((text, kw.get("parent_uri"))) or "at://sent/1")
        main.db.player_points = lambda did, game_id=None: 10
        main._remember = lambda *a, **k: None
        try:
            plays = self.plays(4)
            plays.append(main.Play("", "", "Z9", game.SAFE, 1))   # the bot
            main._credit_knockout_players(self.state, plays)
        finally:
            (main.bluesky.post_reply, main.db.player_points,
             main._remember) = real_reply, real_points, real_remember

        self.assertEqual(len(sent), 4, "one reply per player, none for the bot")
        self.assertEqual({uri for _, uri in sent},
                         {f"at://did:{i}/x" for i in range(4)})

    def test_one_failed_reply_does_not_cost_the_others(self):
        sent = []
        real_reply, real_points, real_remember = (
            main.bluesky.post_reply, main.db.player_points, main._remember)

        def flaky(text, **kw):
            if kw.get("parent_uri") == "at://did:1/x":
                raise RuntimeError("rate limited")
            sent.append(kw.get("parent_uri"))
            return "at://sent/1"

        main.bluesky.post_reply = flaky
        main.db.player_points = lambda did, game_id=None: 10
        main._remember = lambda *a, **k: None
        try:
            main._credit_knockout_players(self.state, self.plays(4))
        finally:
            (main.bluesky.post_reply, main.db.player_points,
             main._remember) = real_reply, real_points, real_remember
        self.assertEqual(len(sent), 3)


class QuietTurnsTeach(unittest.TestCase):
    """A turn nobody took shows the deduction, not just the cell.

    Live, 83 of 135 turns in the first three days were the bot's own —
    "nobody moved, so I opened D4" was most of the feed and taught nothing.
    """

    def setUp(self):
        self._knockout, self._db = config.KNOCKOUT, config.DB_PATH
        config.KNOCKOUT = True
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "quiet.db")
        db.init_db()
        self.state = game.new_game(1, rng=random.Random(5), mine_budget=2)
        self.state.turn_number = 9

    def tearDown(self):
        config.KNOCKOUT, config.DB_PATH = self._knockout, self._db

    def bot(self, why, note="provably safe"):
        return [main.Play("", "", "D4", game.SAFE, 3, note=note, why=why)]

    def test_the_reason_is_shown(self):
        why = "The 1 at C3 already has its mine at B2, so D4 is clear."
        text = main.build_knockout_turn_text(self.state, self.bot(why))
        self.assertIn("nobody moved, so I opened D4.", text)
        self.assertIn("🧠 " + why, text)
        self.assertLessEqual(len(text), 300)

    def test_a_gamble_is_reported_as_odds_not_reasoning(self):
        text = main.build_knockout_turn_text(
            self.state, self.bot("", note="safest guess at 78%"))
        self.assertIn("gambled on D4 at 78%", text)
        self.assertNotIn("🧠", text)

    def test_the_prompt_outranks_the_reason(self):
        text = main.build_knockout_turn_text(self.state, self.bot("x " * 200))
        self.assertLessEqual(len(text), 300)
        self.assertIn("min.", text)
        self.assertNotIn("🧠", text)

    def test_a_player_turn_never_carries_it(self):
        plays = [main.Play("did:a", "a.bsky.social", "D4", game.SAFE, 3,
                           why="should not appear")]
        self.assertNotIn("🧠", main.build_knockout_turn_text(self.state, plays))

    def test_the_bot_move_carries_a_reason(self):
        """apply_knockout_moves fills in `why` from the solver, so the live
        path — not just the builder — produces the sentence."""
        position = solver.Position.from_state(self.state)
        analysis = solver.analyze(position)
        if not analysis.safe:
            self.skipTest("this seed opens onto a guess")
        plays, source, superseded = main.apply_knockout_moves(
            self.state, [], set(), position, analysis)
        self.assertEqual(source, "bot")
        self.assertEqual(superseded, [])
        self.assertTrue(plays[0].why.endswith(f"{plays[0].coord} is clear."),
                        plays[0].why)

    def test_the_voting_mode_post_shows_it_too(self):
        config.KNOCKOUT = False
        why = "The 1 at C3 already has its mine at B2, so D4 is clear."
        text = main.build_turn_text(self.state, "D4", None, "bot", why=why)
        self.assertIn("I played it myself", text)
        self.assertIn("🧠 " + why, text)
        self.assertLessEqual(len(text), 300)
        crowd = main.build_turn_text(self.state, "D4", None, "crowd", why=why)
        self.assertNotIn("🧠", crowd)
