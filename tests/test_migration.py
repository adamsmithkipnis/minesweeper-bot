"""Upgrading a database that already has a board in it.

_SCHEMA only builds tables that do not exist yet, so a new column reaches an
existing database solely through _ADDED_COLUMNS. Every other test in this
suite creates a fresh database, which means none of them can catch a missing
migration entry — and one went missing: the knockout columns were added to
_SCHEMA but not to _ADDED_COLUMNS, so the live bot raised on every save,
never advanced, and replayed the same turn into the same post for an hour.
"""

import os
import random
import re
import sqlite3
import tempfile
import unittest

from helpers import config, db, game

# game_state exactly as it stood before the knockout columns were added.
LEGACY_GAME_STATE = """
CREATE TABLE game_state (
    id INTEGER PRIMARY KEY, game_id INTEGER, rows INTEGER, cols INTEGER,
    mine_count INTEGER, mine_cells TEXT, revealed TEXT, turn_number INTEGER,
    status TEXT, last_post_uri TEXT, last_coord TEXT, last_result TEXT,
    last_source TEXT, last_caller TEXT, last_votes INTEGER,
    last_voters INTEGER, exploded_cell TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def declared_columns(table: str) -> set:
    """Column names the current _SCHEMA declares for `table`."""
    block = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);",
                      db._SCHEMA, re.S).group(1)
    names = set()
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        names.add(line.split()[0].rstrip(","))
    return names


class MigratingALiveDatabase(unittest.TestCase):
    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy.db")
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(LEGACY_GAME_STATE)
        conn.execute("INSERT INTO game_state (id, game_id, rows, cols, "
                     "mine_count, mine_cells, revealed, turn_number, status) "
                     "VALUES (1, 5, 9, 9, 13, '[]', '[]', 2, 'active')")
        conn.commit()
        conn.close()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_every_column_the_schema_declares_survives_the_upgrade(self):
        """The general guard: any future column added to _SCHEMA and
        forgotten in _ADDED_COLUMNS fails here."""
        db.init_db()
        with db._connect() as conn:
            live = {r["name"] for r in conn.execute("PRAGMA table_info(game_state)")}
        missing = declared_columns("game_state") - live
        self.assertEqual(missing, set(),
                         f"columns in _SCHEMA but not reachable by migration: "
                         f"{sorted(missing)}")

    def test_a_board_in_progress_can_still_be_saved(self):
        db.init_db()
        state = game.new_game(7, rng=random.Random(3), mine_budget=2)
        state.turn_number = 3
        game.reveal(state, *sorted(state.mine_cells)[0])
        db.save_state(state)                     # raised before the fix
        back = db.load_state()
        self.assertEqual(back.turn_number, 3)
        self.assertEqual(back.mine_budget, 2)
        self.assertEqual(back.detonations, 1)

    def test_migrating_twice_is_harmless(self):
        db.init_db()
        db.init_db()
        state = game.new_game(7, rng=random.Random(3), mine_budget=1)
        db.save_state(state)
        self.assertEqual(db.load_state().mine_budget, 1)


class MovesTable(unittest.TestCase):
    def test_the_moves_columns_are_also_reachable(self):
        old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy_moves.db")
        try:
            conn = sqlite3.connect(config.DB_PATH)
            conn.executescript(
                "CREATE TABLE moves (id INTEGER PRIMARY KEY, game_id INTEGER,"
                " turn_number INTEGER, coord TEXT, result TEXT, source TEXT,"
                " caller TEXT, votes INTEGER, voters INTEGER,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
            conn.commit(); conn.close()
            db.init_db()
            with db._connect() as conn:
                live = {r["name"] for r in conn.execute("PRAGMA table_info(moves)")}
            self.assertEqual(declared_columns("moves") - live, set())
        finally:
            config.DB_PATH = old


class FlagBets(unittest.TestCase):
    """The flags table as it stood before claims were scored as bets: no
    withdrawal column, and a plain unique index that would stop a fresh bet
    on a cell after a withdrawn one."""

    LEGACY = (
        "CREATE TABLE flags (id INTEGER PRIMARY KEY, game_id INTEGER,"
        " coord TEXT, did TEXT, handle TEXT, turn_number INTEGER,"
        " correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
        "CREATE UNIQUE INDEX idx_flags_one_per_person"
        " ON flags (game_id, coord, did);"
        "INSERT INTO flags (game_id, coord, did, handle, turn_number)"
        " VALUES (1, 'C3', 'did:a', 'a', 1);"
    )

    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy_bets.db")
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(self.LEGACY)
        conn.commit(); conn.close()
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_an_old_claim_still_shows_and_scores(self):
        self.assertEqual(db.flag_counts(1), {"C3": 1})
        db.resolve_flags(1, {"C3": True})
        self.assertEqual(db.flag_points("did:a", 1), 1)

    def test_a_withdrawn_claim_can_be_placed_again(self):
        db.withdraw_flag(1, "C3", "did:a", 1)
        self.assertTrue(db.add_flag(1, "C3", "did:a", "a", 2),
                        "the old one-row-per-cell index must be gone")

    def test_migrating_twice_is_harmless(self):
        db.init_db()
        self.assertEqual(db.flag_counts(1), {"C3": 1})

    def test_a_fresh_database_scores_every_board(self):
        self.assertEqual(db.flag_scoring_from(), 0)
        self.assertTrue(db.flag_scoring_active(1))


class FlagScoringStartsNextBoard(unittest.TestCase):
    """Deploying mid-board must not re-score the board in progress: the
    rules a board started under are the rules it ends under."""

    def setUp(self):
        self._old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "live_bets.db")
        conn = sqlite3.connect(config.DB_PATH)
        conn.executescript(LEGACY_GAME_STATE + FlagBets.LEGACY)
        conn.execute("INSERT INTO game_state (id, game_id, rows, cols, "
                     "mine_count, mine_cells, revealed, turn_number, status) "
                     "VALUES (1, 7, 9, 9, 13, '[]', '[]', 2, 'active')")
        conn.execute("INSERT INTO flags (game_id, coord, did, handle, "
                     "turn_number) VALUES (7, 'D4', 'did:a', 'a', 2)")
        conn.commit(); conn.close()
        db.init_db()

    def tearDown(self):
        config.DB_PATH = self._old

    def test_points_begin_with_the_board_after_the_live_one(self):
        self.assertEqual(db.flag_scoring_from(), 8)
        self.assertFalse(db.flag_scoring_active(7))
        self.assertTrue(db.flag_scoring_active(8))

    def test_the_live_board_still_shows_and_resolves_its_flags(self):
        self.assertEqual(db.flag_counts(7), {"D4": 1})
        db.resolve_flags(7, {"D4": True})
        self.assertEqual(db.flag_points("did:a", 7), 0, "no points yet")
        self.assertEqual(db.flag_scores(7), [], "nothing to announce either")

    def test_the_next_board_scores(self):
        db.add_flag(8, "C3", "did:a", "a", 1)
        db.resolve_flags(8, {"C3": True})
        self.assertEqual(db.flag_points("did:a", 8), 1)
        self.assertEqual(db.flag_points("did:a"), 1)

    def test_a_second_start_does_not_move_the_line(self):
        db.init_db()
        self.assertEqual(db.flag_scoring_from(), 8)


class FlagTutorialTable(unittest.TestCase):
    def test_existing_flaggers_are_backfilled_as_experienced_players(self):
        old = config.DB_PATH
        config.DB_PATH = os.path.join(tempfile.mkdtemp(), "legacy_flags.db")
        try:
            conn = sqlite3.connect(config.DB_PATH)
            conn.executescript(
                "CREATE TABLE flags (id INTEGER PRIMARY KEY, game_id INTEGER,"
                " coord TEXT, did TEXT, handle TEXT, turn_number INTEGER,"
                " correct INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
                "INSERT INTO flags (game_id, coord, did, handle, turn_number)"
                " VALUES (1, 'C3', 'did:experienced', 'old.bsky.social', 1);"
            )
            conn.commit(); conn.close()

            db.init_db()

            self.assertFalse(db.claim_flag_tutorial("did:experienced"))
            self.assertTrue(db.claim_flag_tutorial("did:new"))
        finally:
            config.DB_PATH = old


if __name__ == "__main__":
    unittest.main()
