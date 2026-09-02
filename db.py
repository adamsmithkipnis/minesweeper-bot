"""SQLite persistence: the live board, finished boards, moves, and posts."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import config
import game
from game import GameState

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_state (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    rows INTEGER,
    cols INTEGER,
    mine_count INTEGER,
    mine_cells TEXT,
    revealed TEXT,
    turn_number INTEGER,
    status TEXT,
    last_post_uri TEXT,
    last_coord TEXT,
    last_result TEXT,
    last_source TEXT,
    last_caller TEXT,
    last_votes INTEGER,
    last_voters INTEGER,
    exploded_cell TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_history (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    status TEXT,              -- 'cleared' | 'exploded'
    turns INTEGER,
    opened INTEGER,
    total INTEGER,
    exploded_cell TEXT,
    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per turn: what was played, who called it, and how. Drives the
-- dashboard's history and makes it possible to answer "why did we open that?"
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    turn_number INTEGER,
    coord TEXT,
    result TEXT,              -- 'safe' | 'mine'
    source TEXT,              -- 'crowd' | 'bot' | 'opening'
    caller TEXT,
    votes INTEGER,
    voters INTEGER,
    was_provably_safe INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every post the bot creates, so a reset deletes exactly the bot's own posts
-- instead of indiscriminately emptying the account.
CREATE TABLE IF NOT EXISTS posts (
    uri TEXT PRIMARY KEY,
    rkey TEXT,
    cid TEXT,
    kind TEXT,                -- 'turn' | 'credit' | 'newgame' | 'gameover'
    game_id INTEGER,
    turn_number INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_posts_game ON posts (game_id);
CREATE INDEX IF NOT EXISTS idx_moves_game ON moves (game_id);
"""

# Columns added after first release. Existing databases get them by ALTER so
# a board in progress survives an upgrade.
_ADDED_COLUMNS = {
    "moves": [("was_provably_safe", "INTEGER")],
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def connect_readonly() -> sqlite3.Connection:
    """A connection that physically cannot write — for the dashboard.

    A `file:...?mode=ro` URI does NOT work here: in WAL mode SQLite needs to
    create a shared-memory index, which a read-only connection cannot do, and
    it fails with "unable to open database file". Opening normally and setting
    query_only gives the same guarantee and actually works.
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, coltype in columns:
            if name not in existing:
                logger.info("Migrating: adding %s.%s", table, name)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def init_db() -> None:
    with _connect() as conn:
        # WAL lets the dashboard read while the bot writes without either
        # blocking the other. It is a property of the file, set once. (Not
        # supported on network filesystems — keep the database on local disk.)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        _migrate(conn)


# ---------------------------------------------------------------------------
# The live board
# ---------------------------------------------------------------------------

def save_state(state: GameState) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO game_state (
                id, game_id, rows, cols, mine_count, mine_cells, revealed,
                turn_number, status, last_post_uri, last_coord, last_result,
                last_source, last_caller, last_votes, last_voters,
                exploded_cell, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                game_id=excluded.game_id, rows=excluded.rows,
                cols=excluded.cols, mine_count=excluded.mine_count,
                mine_cells=excluded.mine_cells, revealed=excluded.revealed,
                turn_number=excluded.turn_number, status=excluded.status,
                last_post_uri=excluded.last_post_uri,
                last_coord=excluded.last_coord,
                last_result=excluded.last_result,
                last_source=excluded.last_source,
                last_caller=excluded.last_caller,
                last_votes=excluded.last_votes,
                last_voters=excluded.last_voters,
                exploded_cell=excluded.exploded_cell,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                state.game_id, state.rows, state.cols, state.mine_count,
                json.dumps(sorted(list(cell) for cell in state.mine_cells)),
                json.dumps([[r, c, n] for (r, c), n in sorted(state.revealed.items())]),
                state.turn_number, state.status, state.last_post_uri,
                state.last_coord, state.last_result, state.last_source,
                state.last_caller, state.last_votes, state.last_voters,
                state.exploded_cell,
            ),
        )


def load_state() -> GameState | None:
    """The current board, or None (also on read failure)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM game_state ORDER BY updated_at DESC, id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        logger.exception("DB read failed; treating as no board in progress")
        return None
    if row is None:
        return None

    return GameState(
        game_id=row["game_id"],
        rows=row["rows"],
        cols=row["cols"],
        mine_count=row["mine_count"],
        mine_cells={tuple(cell) for cell in json.loads(row["mine_cells"])},
        revealed={(r, c): n for r, c, n in json.loads(row["revealed"])},
        turn_number=row["turn_number"],
        status=row["status"],
        last_post_uri=row["last_post_uri"] or "",
        last_coord=row["last_coord"] or "",
        last_result=row["last_result"] or "",
        last_source=row["last_source"] or "",
        last_caller=row["last_caller"] or "",
        last_votes=row["last_votes"] or 0,
        last_voters=row["last_voters"] or 0,
        exploded_cell=row["exploded_cell"] or "",
    )


# ---------------------------------------------------------------------------
# History and moves
# ---------------------------------------------------------------------------

def last_turn_at() -> datetime | None:
    """When the current board last advanced, as an aware UTC datetime.

    State is saved exactly once per turn, after the post succeeds, so
    `updated_at` is the time of the last turn that actually reached Bluesky.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT updated_at FROM game_state WHERE id = 1").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row["updated_at"]:
        return None
    try:
        stamp = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)   # SQLite CURRENT_TIMESTAMP is UTC


def record_finished(state: GameState) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO game_history
               (game_id, status, turns, opened, total, exploded_cell)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (state.game_id, state.status, state.turn_number,
             len(state.revealed), state.total_safe, state.exploded_cell),
        )


def record_move(state: GameState, coord: str, result: str, source: str,
                caller: str = "", votes: int = 0, voters: int = 0,
                was_provably_safe: bool | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO moves (game_id, turn_number, coord, result, source,
                                  caller, votes, voters, was_provably_safe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (state.game_id, state.turn_number, coord, result, source, caller,
             votes, voters,
             None if was_provably_safe is None else int(was_provably_safe)),
        )


def get_moves(game_id: int | None = None, limit: int = 100) -> list:
    where, params = ("WHERE game_id = ?", [game_id]) if game_id else ("", [])
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM moves {where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        ).fetchall()
    return [dict(r) for r in rows]


def get_record() -> dict:
    """All-time board results."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) n, AVG(turns) avg_turns FROM game_history "
            "GROUP BY status").fetchall()
    record = {"cleared": 0, "exploded": 0, "avg_turns": 0.0}
    total_turns = total_games = 0
    for row in rows:
        if row["status"] in record:
            record[row["status"]] = row["n"]
        total_games += row["n"]
        total_turns += (row["avg_turns"] or 0) * row["n"]
    record["played"] = total_games
    record["avg_turns"] = round(total_turns / total_games, 1) if total_games else 0.0
    return record


def get_history(limit: int = 20) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM game_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Post tracking (so a reset deletes precisely the bot's own posts)
# ---------------------------------------------------------------------------

def rkey_from_uri(uri: str) -> str:
    """'at://did:plc:x/app.bsky.feed.post/3abc' -> '3abc'."""
    return uri.rsplit("/", 1)[-1] if uri else ""


def record_post(uri: str, kind: str, cid: str = "", game_id: int | None = None,
                turn_number: int | None = None) -> None:
    if not uri:
        return
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO posts
               (uri, rkey, cid, kind, game_id, turn_number)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uri, rkey_from_uri(uri), cid, kind, game_id, turn_number),
        )


def get_posts(game_id: int | None = None) -> list:
    where, params = ("WHERE game_id = ?", [game_id]) if game_id else ("", [])
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM posts {where} ORDER BY created_at DESC, rowid DESC",
            params).fetchall()
    return [dict(r) for r in rows]


def count_posts() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) n FROM posts").fetchone()["n"]


def forget_posts(uris: list) -> None:
    if not uris:
        return
    with _connect() as conn:
        conn.executemany("DELETE FROM posts WHERE uri = ?", [(u,) for u in uris])


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset_game_state() -> None:
    """Forget the board in progress; keep history and the post log."""
    with _connect() as conn:
        conn.execute("DELETE FROM game_state")


def reset_all(keep_record: bool = False) -> None:
    """Full wipe for a clean slate — the testing reset."""
    with _connect() as conn:
        conn.execute("DELETE FROM game_state")
        conn.execute("DELETE FROM posts")
        if not keep_record:
            conn.execute("DELETE FROM game_history")
            conn.execute("DELETE FROM moves")
