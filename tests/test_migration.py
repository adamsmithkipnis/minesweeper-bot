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


if __name__ == "__main__":
    unittest.main()
