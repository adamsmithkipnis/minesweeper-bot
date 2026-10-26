"""SQLite persistence: the live board, finished boards, moves, and posts."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

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
    mine_budget INTEGER DEFAULT 0,
    detonations INTEGER DEFAULT 0,
    spent_mines TEXT,
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
    did TEXT,                 -- stable id: handles change, this does not
    points INTEGER DEFAULT 0, -- cells this move opened, 0 for a mine
    votes INTEGER,
    voters INTEGER,
    was_provably_safe INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Crowd mine-claims. Flagging costs no turn, so this is a parallel record to
-- moves: several people may flag the same cell, and each claim is scored on
-- its own once the cell is resolved.
--
-- A claim is a bet: +1 when the cell turns out to be a mine, -1 when it does
-- not. It can be taken back for nothing on the turn it was placed, and for
-- the same -1 as being wrong on any later turn — otherwise flagging on a
-- hunch and withdrawing once the neighbours disprove it would be free money.
-- The pennant on the board is shared and anyone may clear it (`shown`), but
-- that only hides the mark; it does not touch anybody else's bet.
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    coord TEXT,
    did TEXT,
    handle TEXT,
    turn_number INTEGER,
    correct INTEGER,          -- NULL until the cell is opened or the board ends
    withdrawn_turn INTEGER,   -- NULL while the claim stands
    shown INTEGER DEFAULT 1,  -- 0 once somebody cleared the pennant
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One standing claim per person per cell. A withdrawn claim keeps its row
-- (and its penalty), so flagging the cell again starts a fresh bet.
DROP INDEX IF EXISTS idx_flags_one_per_person;

-- One-row settings the code decides for itself. `flag_scoring_from` is the
-- first board whose flags count for points: set to the board after the live
-- one when the scoring columns arrive, so a rules change lands at a board
-- boundary and the game in progress finishes under the rules it started on.
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Somebody's flags made a cell provably safe and somebody opened it. Paid
-- at board end, and only if every flag behind it was right. Keyed on the
-- opened cell so a replayed turn cannot credit the same assist twice.
CREATE TABLE IF NOT EXISTS assists (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    did TEXT,
    handle TEXT,
    coord TEXT,               -- the cell that was opened
    flag_coords TEXT,         -- the flags that made it safe, space-separated
    turn_number INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (game_id, did, coord)
);

-- Players who have already received the one-time flag tutorial. This is
-- separate from flags because an unresolved claim can be withdrawn and
-- deleted, but the tutorial must still remain a lifetime one-time event.
CREATE TABLE IF NOT EXISTS flag_tutorials (
    did TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cells somebody has marked to check: probably a mine, not sure enough to
-- bet a flag on. One shared mark per cell that anyone may place or clear;
-- `did` is whoever placed it. Scores nothing, so nothing here is a claim.
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    coord TEXT,
    did TEXT,
    handle TEXT,
    turn_number INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (game_id, coord)
);

-- Players who have had the one-time reply explaining check marks.
CREATE TABLE IF NOT EXISTS check_tutorials (
    did TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Knocked-out players, per board. A mine takes the player who opened it out
-- of that board rather than ending it for everyone; they can still flag.
CREATE TABLE IF NOT EXISTS eliminations (
    id INTEGER PRIMARY KEY,
    game_id INTEGER,
    did TEXT,
    handle TEXT,
    coord TEXT,
    turn_number INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_eliminated_once
    ON eliminations (game_id, did);

-- Milestones already celebrated, one row per player per rung. The unique
-- index is the whole point: it makes a public "you passed 1,000!" post
-- impossible to send twice, however a turn is retried or replayed.
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY,
    did TEXT,
    threshold INTEGER,
    points INTEGER,           -- the total at the moment it was crossed
    game_id INTEGER,
    announced INTEGER DEFAULT 0,  -- 0 = backfilled silently, 1 = posted
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_milestones_once
    ON milestones (did, threshold);

-- The same ledger for the days-played ladder, where `threshold` is a number
-- of distinct days and `points` the day count when it was crossed. A table
-- of its own rather than a kind column, so a day rung of 100 can never
-- collide with the 100-point rung under the unique index.
CREATE TABLE IF NOT EXISTS day_milestones (
    id INTEGER PRIMARY KEY,
    did TEXT,
    threshold INTEGER,
    points INTEGER,
    game_id INTEGER,
    announced INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_day_milestones_once
    ON day_milestones (did, threshold);

-- Replies the bot has already explained itself to (a vote for an open cell,
-- a reply to an old turn post). Claimed before the answer goes out and
-- released if it fails, so nobody is told twice however often a tick re-reads
-- the same thread.
CREATE TABLE IF NOT EXISTS notices (
    reply_uri TEXT PRIMARY KEY,
    kind TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- The account's follower count over time, for the owner's own records.
-- Deliberately not cleared by reset_all: it describes the account, not a game.
CREATE TABLE IF NOT EXISTS follower_counts (
    id INTEGER PRIMARY KEY,
    followers INTEGER,
    follows INTEGER,
    posts INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP   -- UTC
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
    "moves": [
        ("did", "TEXT"),
        ("points", "INTEGER DEFAULT 0"),
        ("was_provably_safe", "INTEGER"),
    ],
    "game_state": [
        ("mine_budget", "INTEGER DEFAULT 0"),
        ("detonations", "INTEGER DEFAULT 0"),
        ("spent_mines", "TEXT"),
    ],
    "flags": [
        ("withdrawn_turn", "INTEGER"),
        ("shown", "INTEGER DEFAULT 1"),
    ],
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


def _migrate(conn: sqlite3.Connection) -> set:
    """Add missing columns; returns the {(table, column)} it added."""
    added = set()
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, coltype in columns:
            if name not in existing:
                logger.info("Migrating: adding %s.%s", table, name)
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
                added.add((table, name))
    return added


def init_db() -> None:
    with _connect() as conn:
        # WAL lets the dashboard read while the bot writes without either
        # blocking the other. It is a property of the file, set once. (Not
        # supported on network filesystems — keep the database on local disk.)
        conn.execute("PRAGMA journal_mode=WAL")
        had_flag_tutorials = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'flag_tutorials'"
        ).fetchone() is not None
        conn.executescript(_SCHEMA)
        added = _migrate(conn)
        if ("flags", "withdrawn_turn") in added:
            # Scoring arrives on a live database: the board in progress
            # finishes under the rules it started on, and points begin with
            # the next one. A fresh database has no board to protect.
            live = conn.execute(
                "SELECT game_id FROM game_state "
                "ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
            if live and live["game_id"]:
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES "
                    "('flag_scoring_from', ?)", (str(live["game_id"] + 1),))
                logger.info("Flag points begin with board %d",
                            live["game_id"] + 1)
        # After the migration, because the index needs the columns it adds.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_flags_one_standing_claim "
            "ON flags (game_id, coord, did) "
            "WHERE correct IS NULL AND withdrawn_turn IS NULL")
        if not had_flag_tutorials:
            # Existing flaggers are experienced users. Backfill them once on
            # migration so deploying this feature does not tutorial-spam the
            # established player base on their next flag.
            conn.execute(
                "INSERT OR IGNORE INTO flag_tutorials (did) "
                "SELECT DISTINCT did FROM flags "
                "WHERE did IS NOT NULL AND did != ''"
            )


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
                exploded_cell, mine_budget, detonations, spent_mines,
                updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, CURRENT_TIMESTAMP)
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
                mine_budget=excluded.mine_budget,
                detonations=excluded.detonations,
                spent_mines=excluded.spent_mines,
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
                state.mine_budget,
                state.detonations,
                json.dumps(sorted(list(cell) for cell in state.spent_mines)),
            ),
        )


def _int(row, name: str) -> int:
    try:
        return row[name] or 0
    except (IndexError, KeyError):
        return 0


def _json(row, name: str) -> list:
    try:
        raw = row[name]
    except (IndexError, KeyError):
        return []
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


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
        mine_budget=_int(row, "mine_budget"),
        detonations=_int(row, "detonations"),
        spent_mines={tuple(cell) for cell in _json(row, "spent_mines")},
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
                was_provably_safe: bool | None = None,
                did: str = "", points: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO moves (game_id, turn_number, coord, result, source,
                                  caller, did, points, votes, voters,
                                  was_provably_safe)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (state.game_id, state.turn_number, coord, result, source, caller,
             did, points, votes, voters,
             None if was_provably_safe is None else int(was_provably_safe)),
        )


def player_count(game_id: int) -> int:
    """Distinct accounts that have taken a move on this board."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT did) n FROM moves "
            "WHERE game_id = ? AND did != ''", (game_id,)).fetchone()
    return row["n"] if row else 0


def player_points(did: str, game_id: int | None = None) -> int:
    """Points this account has scored — in one game, or across all of them.

    A move scores the number of cells it opened, so a lucky blank that
    cascades is worth more than a single numbered cell. Mines score nothing.
    """
    if not did:
        return 0
    clause, params = "WHERE did = ?", [did]
    if game_id is not None:
        clause += " AND game_id = ?"
        params.append(game_id)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(points), 0) AS n FROM moves {clause}",
            params).fetchone()
    return int(row["n"] or 0)


def leaderboard(game_id: int | None = None, limit: int = 5,
                since: datetime | None = None) -> list:
    """Highest scorers, best first: [{did, handle, points, moves}].

    `since` windows it by move time, which is what makes a weekly table
    possible. created_at is written by SQLite as UTC, so the bound is
    converted rather than passed through as local time.
    """
    clause, params = "WHERE did IS NOT NULL AND did != ''", []
    if game_id is not None:
        clause += " AND game_id = ?"
        params.append(game_id)
    if since is not None:
        clause += " AND created_at >= ?"
        params.append(_utc_text(since))
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT did,
                       (SELECT caller FROM moves m2 WHERE m2.did = m1.did
                         ORDER BY created_at DESC LIMIT 1) AS handle,
                       SUM(points) AS points, COUNT(*) AS moves
                FROM moves m1 {clause}
                GROUP BY did
                HAVING SUM(points) > 0
                ORDER BY points DESC, moves ASC LIMIT ?""",
            params).fetchall()
    return [dict(r) for r in rows]


def _utc_text(when: datetime) -> str:
    """A datetime as SQLite writes CURRENT_TIMESTAMP: UTC, no offset."""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d %H:%M:%S")


# The two ladders share one shape, so they share the code; `kind` picks the
# table and is never interpolated from anywhere but here.
_LADDER_TABLES = {"points": "milestones", "days": "day_milestones"}


def milestones_reached(did: str, kind: str = "points") -> set:
    """Rungs this player has already been credited with."""
    if not did:
        return set()
    table = _LADDER_TABLES[kind]
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT threshold FROM {table} WHERE did = ?", (did,)).fetchall()
    return {int(r["threshold"]) for r in rows}


def backfill_milestones(did: str, points: int, ladder: list,
                        kind: str = "points") -> int:
    """Mark the rungs a player has *already* passed, without announcing them.

    Called once per player, the first time they come up, so switching this
    feature on does not congratulate a 942-point regular on their first 100.
    Only ever applied to a player with no rows at all: once they have any, a
    rung still missing is a post that is genuinely owed, and a restart must
    not swallow it.
    """
    if not did:
        return 0
    table = _LADDER_TABLES[kind]
    with _connect() as conn:
        seen = conn.execute(f"SELECT COUNT(*) n FROM {table} WHERE did = ?",
                            (did,)).fetchone()["n"]
        if seen:
            return 0
        rows = [(did, t, points) for t in ladder if t <= points]
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} (did, threshold, points, "
            "announced) VALUES (?, ?, ?, 0)", rows)
    return len(rows)


def claim_milestones(did: str, points: int, ladder: list,
                     game_id: int | None = None,
                     kind: str = "points") -> list:
    """Reserve every unawarded rung at or below `points`; return them.

    Claiming before posting rather than after is what makes a duplicate
    impossible: the unique index rejects the second claim even if a turn is
    replayed. If the post then fails, `release_milestones` hands them back so
    the next turn tries again.
    """
    if not did:
        return []
    table = _LADDER_TABLES[kind]
    claimed = []
    with _connect() as conn:
        for threshold in ladder:
            if threshold > points:
                break
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO {table} (did, threshold, points, "
                "game_id, announced) VALUES (?, ?, ?, ?, 1)",
                (did, threshold, points, game_id))
            if cursor.rowcount:
                claimed.append(threshold)
    return claimed


def release_milestones(did: str, thresholds: list,
                       kind: str = "points") -> None:
    """Undo claims whose post never went out."""
    if not did or not thresholds:
        return
    table = _LADDER_TABLES[kind]
    with _connect() as conn:
        conn.executemany(f"DELETE FROM {table} WHERE did = ? AND "
                         "threshold = ? AND announced = 1",
                         [(did, t) for t in thresholds])


def play_days(did: str, tz) -> set:
    """The local dates on which this account made a move or a flag claim.

    The same definition as a daily active user: one recorded action is a day
    played, whether it opened a cell, hit a mine or only planted a flag.
    Stored times are UTC, so the conversion happens here rather than in SQL,
    which has no idea when daylight saving changes.
    """
    if not did:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT created_at FROM moves WHERE did = ? "
            "UNION ALL SELECT created_at FROM flags WHERE did = ?",
            (did, did)).fetchall()
    days = set()
    for row in rows:
        stamp = str(row["created_at"] or "").replace("T", " ").split(".")[0]
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        days.add(when.replace(tzinfo=timezone.utc).astimezone(tz).date())
    return days


def _activity(game_id: int | None = None) -> list:
    """Every recorded action by a person, oldest first.

    [{did, handle, points, at}] — a move scores its points, a flag claim
    scores 0 here, and `at` is the UTC text SQLite wrote. Moves by the bot
    have no DID and are left out.
    """
    where, params = "WHERE did IS NOT NULL AND did != ''", []
    if game_id is not None:
        where += " AND game_id = ?"
        params.append(game_id)
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT did, caller AS handle, points, created_at AS at
                FROM moves {where}
                UNION ALL
                SELECT did, handle, 0 AS points, created_at AS at
                FROM flags {where}
                ORDER BY at""", params + params).fetchall()
    return [dict(r) for r in rows]


def board_players(game_id: int) -> list:
    """Everybody who opened a cell or planted a flag on this board.

    Best scorer first, then in order of arrival, so a trimmed list keeps the
    people who carried the board. [{did, handle, points}], one per account,
    under the handle they used most recently.
    """
    players = {}
    for row in _activity(game_id):
        entry = players.setdefault(row["did"], {"did": row["did"],
                                                "handle": "", "points": 0,
                                                "order": len(players)})
        entry["handle"] = row["handle"] or entry["handle"]
        entry["points"] += row["points"] or 0
    ranked = sorted(players.values(), key=lambda p: (-p["points"], p["order"]))
    return [{k: p[k] for k in ("did", "handle", "points")} for p in ranked]


def weekly_arrivals(since: datetime, gap_days: int = 14) -> tuple:
    """(newcomers, returners) active since `since`, best scorer first.

    A newcomer's first action ever falls inside the window. A returner had
    played before, but not in the `gap_days` before the window opened — the
    people who drifted off and came back. Each is [{did, handle, points}],
    with points scored inside the window.
    """
    start = _utc_text(since)
    gap = _utc_text(since - timedelta(days=gap_days))
    seen = {}
    for row in _activity():
        entry = seen.setdefault(row["did"], {
            "did": row["did"], "handle": "", "points": 0, "first": row["at"],
            "last_before": None, "active": False})
        entry["handle"] = row["handle"] or entry["handle"]
        if str(row["at"]) >= start:
            entry["active"] = True
            entry["points"] += row["points"] or 0
        else:
            entry["last_before"] = str(row["at"])
    newcomers, returners = [], []
    for entry in seen.values():
        if not entry["active"]:
            continue
        if str(entry["first"]) >= start:
            newcomers.append(entry)
        elif entry["last_before"] and entry["last_before"] < gap:
            returners.append(entry)

    def ranked(entries):
        entries.sort(key=lambda e: (-e["points"], str(e["first"])))
        return [{k: e[k] for k in ("did", "handle", "points")}
                for e in entries]
    return ranked(newcomers), ranked(returners)


def moved_before(did: str, game_id: int, turn_number: int) -> bool:
    """Whether this account has a move on any turn other than this one.

    This turn's own rows are excluded rather than counted, so the answer is
    the same whether it is asked before or after the turn is recorded.
    """
    if not did:
        return True
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM moves WHERE did = ? "
            "AND NOT (game_id = ? AND turn_number = ?) LIMIT 1",
            (did, game_id, turn_number)).fetchone()
    return row is not None


def last_post_at(kind: str) -> datetime | None:
    """When the bot last posted something of this kind, UTC.

    The weekly leaderboard needs it: APScheduler recomputes a cron job's next
    run at startup, so a restart across the scheduled minute would otherwise
    skip that week silently.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT created_at FROM posts WHERE kind = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (kind,)).fetchone()
    if row is None or not row["created_at"]:
        return None
    stamp = str(row["created_at"]).replace("T", " ").split(".")[0]
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


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
# Flags
# ---------------------------------------------------------------------------

def add_check(game_id: int, coord: str, did: str, handle: str,
              turn_number: int) -> bool:
    """Mark a cell to check; True if it was not already marked."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO checks (game_id, coord, did, handle, "
            "turn_number) VALUES (?, ?, ?, ?, ?)",
            (game_id, coord, did, handle, turn_number))
        return cur.rowcount == 1


def remove_check(game_id: int, coord: str) -> bool:
    """Clear a cell's check mark; True if there was one."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM checks WHERE game_id = ? AND coord = ?",
                           (game_id, coord))
        return cur.rowcount > 0


def checked_coords(game_id: int) -> set:
    """Every cell marked to check on this board, opened or not."""
    with _connect() as conn:
        rows = conn.execute("SELECT coord FROM checks WHERE game_id = ?",
                            (game_id,)).fetchall()
    return {r["coord"] for r in rows}


def claim_check_tutorial(did: str) -> bool:
    """Reserve a player's one-time check-mark explanation."""
    if not did:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO check_tutorials (did) VALUES (?)", (did,))
        return cur.rowcount == 1


def release_check_tutorial(did: str) -> None:
    """Let a failed explanation be tried again on the player's next check."""
    if not did:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM check_tutorials WHERE did = ?", (did,))


def claim_flag_tutorial(did: str) -> bool:
    """Reserve a player's lifetime one-time flag tutorial."""
    if not did:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO flag_tutorials (did) VALUES (?)", (did,))
        return cur.rowcount == 1


def release_flag_tutorial(did: str) -> None:
    """Let a failed tutorial post be attempted on the player's next flag."""
    if not did:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM flag_tutorials WHERE did = ?", (did,))

def record_followers(followers: int, follows: int | None = None,
                     posts: int | None = None) -> None:
    """Append one reading of the account's follower count."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO follower_counts (followers, follows, posts) "
            "VALUES (?, ?, ?)", (followers, follows, posts))


def follower_history(since: str | None = None) -> list:
    """Readings oldest first: [{created_at (UTC), followers, follows, posts}]."""
    where, params = ("WHERE created_at >= ?", [since]) if since else ("", [])
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT created_at, followers, follows, posts "
            f"FROM follower_counts {where} ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]


def claim_notice(reply_uri: str, kind: str) -> bool:
    """Reserve the one explanation owed to a reply. False if already given."""
    if not reply_uri:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notices (reply_uri, kind) VALUES (?, ?)",
            (reply_uri, kind))
        return cur.rowcount == 1


def release_notice(reply_uri: str) -> None:
    """Let a failed explanation be attempted again on the next tick."""
    if not reply_uri:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM notices WHERE reply_uri = ?", (reply_uri,))


def add_flag(game_id: int, coord: str, did: str, handle: str,
             turn_number: int) -> bool:
    """Record one person's claim that `coord` holds a mine.

    Returns True if this placed a new bet. Repeating a standing claim only
    puts the pennant back if somebody had cleared it, and costs nothing — so
    it does not count against the per-turn cap either.
    """
    with _connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO flags
               (game_id, coord, did, handle, turn_number)
               VALUES (?, ?, ?, ?, ?)""",
            (game_id, coord, did, handle, turn_number))
        if cur.rowcount:
            return True
        conn.execute(
            "UPDATE flags SET shown = 1 WHERE game_id = ? AND coord = ? "
            "AND did = ? AND correct IS NULL AND withdrawn_turn IS NULL",
            (game_id, coord, did))
        return False


def remove_flag(game_id: int, coord: str) -> None:
    """Take the pennant off a cell, whoever put it there.

    The mark on the board is shared, so anyone may clear it. The bets behind
    it are personal and stay standing: Bob disagreeing with Alice does not
    cancel Alice's claim, and it cannot let her dodge a wrong one either.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE flags SET shown = 0 WHERE game_id = ? AND coord = ? "
            "AND correct IS NULL AND withdrawn_turn IS NULL", (game_id, coord))


def withdraw_flag(game_id: int, coord: str, did: str, turn_number: int) -> bool:
    """Take back one person's own standing claim. Scored claims are final.

    The row stays, with the turn it was withdrawn on: same turn as placed is
    free, any later turn is scored like a wrong flag. Returns True if there
    was a standing claim to withdraw.
    """
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE flags SET withdrawn_turn = ? WHERE game_id = ? "
            "AND coord = ? AND did = ? AND correct IS NULL "
            "AND withdrawn_turn IS NULL",
            (turn_number, game_id, coord, did))
        return bool(cur.rowcount)


_STANDING = "correct IS NULL AND withdrawn_turn IS NULL"


def flagged_coords(game_id: int, quorum: int = 1) -> set:
    """Cells currently showing at least `quorum` flags."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT coord, COUNT(*) n FROM flags "
            f"WHERE game_id = ? AND {_STANDING} AND shown = 1 "
            f"GROUP BY coord HAVING n >= ?", (game_id, quorum)).fetchall()
    return {row["coord"] for row in rows}


def flag_counts(game_id: int) -> dict:
    """{coord: number of people showing a flag on it}."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT coord, COUNT(*) n FROM flags "
            f"WHERE game_id = ? AND {_STANDING} AND shown = 1 GROUP BY coord",
            (game_id,)).fetchall()
    return {row["coord"]: row["n"] for row in rows}


def standing_flags(game_id: int) -> dict:
    """Every standing bet, shown or not: {did: {"handle": ..., "coords": set}}."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT did, handle, coord FROM flags "
            f"WHERE game_id = ? AND {_STANDING} AND did != ''",
            (game_id,)).fetchall()
    out = {}
    for r in rows:
        entry = out.setdefault(r["did"], {"handle": r["handle"], "coords": set()})
        entry["coords"].add(r["coord"])
    return out


def resolve_flags(game_id: int, coords: dict) -> int:
    """Score claims now that the truth is known.

    `coords` maps a coordinate to whether it actually held a mine. Only
    standing claims are touched, so scoring cannot be applied twice, and a
    withdrawn claim keeps whatever the withdrawal cost.
    """
    if not coords:
        return 0
    with _connect() as conn:
        total = 0
        for coord, was_mine in coords.items():
            cur = conn.execute(
                f"UPDATE flags SET correct = ? "
                f"WHERE game_id = ? AND coord = ? AND {_STANDING}",
                (int(bool(was_mine)), game_id, coord))
            total += cur.rowcount
        return total


def record_assist(game_id: int, did: str, handle: str, coord: str,
                  flag_coords: list, turn_number: int) -> None:
    """Somebody opened `coord`, and this person's flags proved it safe."""
    if not did or not flag_coords:
        return
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO assists
               (game_id, did, handle, coord, flag_coords, turn_number)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, did, handle, coord, " ".join(sorted(flag_coords)),
             turn_number))


# What one claim row is worth. Settled claims pay out on the truth; a
# withdrawn one costs the penalty unless it was taken back the same turn.
_CLAIM_POINTS = """CASE
    WHEN correct = 1 THEN 1
    WHEN correct = 0 THEN -1
    WHEN withdrawn_turn IS NOT NULL AND withdrawn_turn > turn_number THEN -1
    ELSE 0 END"""


def get_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


def flag_scoring_from() -> int:
    """The first board whose flags count for points (0 = all of them)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'flag_scoring_from'"
        ).fetchone()
    return int(row["value"]) if row else 0


def flag_scoring_active(game_id: int) -> bool:
    return game_id >= flag_scoring_from()


def flag_scores(game_id: int | None = None) -> list:
    """Per-person flag record, best first: [{did, handle, hits, total, points}].

    `points` is the board's flag score, floored at zero — recognition rather
    than a losing record — and includes paid assists. With no game it is the
    sum of the per-board floors, so a bad board never eats a good one.
    """
    # A board from before points existed scores nobody, so the switch lands
    # between boards rather than halfway through one.
    where, params = "WHERE game_id >= ?", [flag_scoring_from()]
    if game_id:
        where, params = where + " AND game_id = ?", params + [game_id]
    with _connect() as conn:
        claims = conn.execute(
            f"""SELECT game_id, did, handle,
                       SUM(correct = 1) hits,
                       SUM(correct IS NOT NULL) total,
                       SUM({_CLAIM_POINTS}) points
                FROM flags {where}
                GROUP BY game_id, did""", params).fetchall()
        correct = {(r["game_id"], r["did"], r["coord"]) for r in conn.execute(
            f"SELECT game_id, did, coord FROM flags {where} AND correct = 1",
            params).fetchall()}
        assists = conn.execute(
            f"SELECT game_id, did, handle, flag_coords FROM assists {where}",
            params).fetchall()
    per_board = {}
    for r in claims:
        per_board[(r["game_id"], r["did"])] = {
            "did": r["did"], "handle": r["handle"],
            "hits": r["hits"] or 0, "total": r["total"] or 0,
            "points": r["points"] or 0}
    for a in assists:
        if all((a["game_id"], a["did"], c) in correct
               for c in a["flag_coords"].split()):
            entry = per_board.setdefault((a["game_id"], a["did"]), {
                "did": a["did"], "handle": a["handle"],
                "hits": 0, "total": 0, "points": 0})
            entry["points"] += 1
    totals = {}
    for (_, did), row in per_board.items():
        out = totals.setdefault(did, {"did": did, "handle": row["handle"],
                                      "hits": 0, "total": 0, "points": 0})
        out["handle"] = row["handle"]
        out["hits"] += row["hits"]
        out["total"] += row["total"]
        out["points"] += max(0, row["points"])
    return sorted(totals.values(),
                  key=lambda r: (-r["points"], -r["hits"], r["total"]))


def late_withdrawals(game_id: int, did: str, turn_number: int) -> int:
    """How many of this person's bets were taken back this turn, late."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n FROM flags WHERE game_id = ? AND did = ? "
            "AND withdrawn_turn = ? AND withdrawn_turn > turn_number",
            (game_id, did, turn_number)).fetchone()
    return row["n"] if row else 0


def flag_points(did: str, game_id: int | None = None) -> int:
    """One person's flag score — this board, or all time."""
    if not did:
        return 0
    return next((r["points"] for r in flag_scores(game_id) if r["did"] == did),
                0)


# ---------------------------------------------------------------------------
# Knockout
# ---------------------------------------------------------------------------

def eliminate(game_id: int, did: str, handle: str, coord: str,
              turn_number: int) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO eliminations
               (game_id, did, handle, coord, turn_number)
               VALUES (?, ?, ?, ?, ?)""",
            (game_id, did, handle, coord, turn_number))


def eliminated(game_id: int) -> set:
    """Every DID knocked out on this board at any point — for the tally."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT did FROM eliminations WHERE game_id = ?", (game_id,)).fetchall()
    return {row["did"] for row in rows}


def benched(game_id: int, turn_number: int, duration: int) -> set:
    """DIDs that cannot act on this turn.

    Knocked out on turn T with `duration` 4, a player misses turns T+1 to
    T+4 and acts again on T+5. With `duration` 0 the elimination lasts the
    whole board.

    Expiry is what makes knockout safe at small crowd sizes. Measured with
    two active players on a tier-3 board, a permanent elimination costs a
    median of 40 turns of spectating with a tail near 500, and the bot ends
    up playing 102 turns by itself because the board cannot finish without
    the people it knocked out. At four turns those numbers are 3, 6 and 0.4.
    """
    with _connect() as conn:
        if duration <= 0:
            rows = conn.execute(
                "SELECT did FROM eliminations WHERE game_id = ?",
                (game_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT did FROM eliminations "
                "WHERE game_id = ? AND turn_number >= ?",
                (game_id, turn_number - duration)).fetchall()
    return {row["did"] for row in rows}


def eliminations(game_id: int) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM eliminations WHERE game_id = ? ORDER BY id",
            (game_id,)).fetchall()
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
            conn.execute("DELETE FROM flags")
            conn.execute("DELETE FROM assists")
            conn.execute("DELETE FROM flag_tutorials")
            conn.execute("DELETE FROM checks")
            conn.execute("DELETE FROM check_tutorials")
            conn.execute("DELETE FROM notices")
            conn.execute("DELETE FROM eliminations")
            # Points go with the moves, so the milestones they earned have to
            # go too, or nobody can ever reach their first hundred again.
            conn.execute("DELETE FROM milestones")
            conn.execute("DELETE FROM day_milestones")
