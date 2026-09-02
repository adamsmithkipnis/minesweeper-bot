"""Read-only local dashboard for the Minesweeper bot.

    python3 dashboard.py        # then open http://127.0.0.1:8766

Touches nothing. The database is opened with PRAGMA query_only so it
physically cannot write, and the live vote tally comes from Bluesky's public
AppView with no credentials at all. Running this cannot disturb a board in
progress.

SECURITY — and note this is the opposite of the Battleship dashboard's
warning. There, the risk was that the page showed both fleets. Here the whole
project exists because the hidden state stays hidden, so exposing this page
would hand away the answers to the game the bot is running. It binds to
127.0.0.1. Keep it local, or reach it over a private network. Do not
port-forward it.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
from flask import Flask, Response, jsonify, render_template_string

import config
import db
import game
import renderer
import solver
import votes

app = Flask(__name__)

PUBLIC_API = "https://public.api.bsky.app/xrpc/"
_http = httpx.Client(timeout=15)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def bot_status() -> dict:
    """Whether launchd reports a live PID for the bot."""
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return {"running": False, "pid": None, "known": False}
    for line in out.splitlines():
        if config.SERVICE in line:
            pid = line.split("\t")[0].strip()
            return {"running": pid.isdigit(), "pid": pid if pid.isdigit() else None,
                    "known": True}
    return {"running": False, "pid": None, "known": False}


def live_replies(post_uri: str) -> list:
    """Replies to the current post, straight from the public AppView.

    No credentials: this is the same data any logged-out visitor can see, so
    the dashboard cannot act on the account even by accident.
    """
    if not post_uri or post_uri.startswith("at://did:plc:dryrun"):
        return []
    try:
        response = _http.get(PUBLIC_API + "app.bsky.feed.getPostThread",
                             params={"uri": post_uri, "depth": 1})
        response.raise_for_status()
        thread = response.json().get("thread", {})
    except Exception:
        return []

    out = []
    for item in thread.get("replies", []) or []:
        post = item.get("post") or {}
        record = post.get("record") or {}
        author = post.get("author") or {}
        out.append(votes.Reply(
            did=author.get("did", ""), handle=author.get("handle", ""),
            text=record.get("text", ""), uri=post.get("uri", ""),
            cid=post.get("cid", ""), created_at=record.get("createdAt", "")))
    return out


def next_turn_at(state) -> str:
    """Rough estimate: the last post plus one turn interval."""
    with db.connect_readonly() as conn:
        row = conn.execute(
            "SELECT updated_at FROM game_state WHERE id = 1").fetchone()
    if not row or not row["updated_at"]:
        return ""
    try:
        stamp = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
        stamp = stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return (stamp + timedelta(minutes=config.TURN_MINUTES)).isoformat()


def snapshot() -> dict:
    state = db.load_state()
    data = {
        "bot": bot_status(),
        "record": db.get_record(),
        "history": db.get_history(10),
        "config": {
            "rows": config.ROWS, "cols": config.COLS, "mines": config.MINES,
            "turn_minutes": config.TURN_MINUTES, "quorum": config.QUORUM,
            "handle": config.HANDLE, "post_mode": config.POST_MODE,
        },
        "board": None,
    }
    if state is None:
        return data

    position = solver.Position.from_state(state)
    analysis = solver.analyze(position)
    already_open = {game.index_to_coord(r, c) for (r, c) in state.revealed}
    replies = live_replies(state.last_post_uri)
    standings = votes.breakdown(replies, already_open, state.rows, state.cols)
    winner = votes.tally(replies, already_open, state.rows, state.cols)

    flags = {c for c in db.flagged_coords(state.game_id, config.FLAG_QUORUM)
             if not state.coord_is_revealed(c)}
    flag_counts = db.flag_counts(state.game_id)

    data["board"] = {
        "game_id": state.game_id,
        "turn": state.turn_number,
        "status": state.status,
        "opened": len(state.revealed),
        "total": state.total_safe,
        "last_coord": state.last_coord,
        "last_result": state.last_result,
        "last_source": state.last_source,
        "post_uri": state.last_post_uri,
        "next_turn_at": next_turn_at(state),
        "grid": renderer.display_grid(state, flags),
        "flags": sorted(flags),
        "flag_counts": flag_counts,
        "flag_scores": db.flag_scores(state.game_id),
        "rows": game.row_letters(state.rows),
        # The solver's read on the position — the reason to run this page.
        "solver": {
            "safe": sorted(game.index_to_coord(*c) for c in analysis.safe),
            "mines": sorted(game.index_to_coord(*c) for c in analysis.mines),
            "level": solver.LEVEL_NAMES[analysis.level],
            "exact": analysis.exact,
        },
        "votes": standings,
        "winner": ({"coord": winner.coord, "votes": winner.votes,
                    "voters": winner.total_voters,
                    "caller": winner.caller_handle,
                    "meets_quorum": winner.votes >= config.QUORUM}
                   if winner else None),
    }
    data["moves"] = db.get_moves(state.game_id, limit=25)
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/v1/state")
def api_state():
    return jsonify(snapshot())


@app.route("/board.png")
def board_png():
    state = db.load_state()
    if state is None:
        return Response("no board", status=404)
    flags = {c for c in db.flagged_coords(state.game_id, config.FLAG_QUORUM)
             if not state.coord_is_revealed(c)}
    return Response(renderer.render_board(state, highlight=state.last_coord,
                                          flags=flags),
                    mimetype="image/png")


@app.route("/")
def index():
    return render_template_string(TEMPLATE, **snapshot())


TEMPLATE = """
<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Minesweeper bot</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0d1117; color:#e6edf3; margin:0;
         font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  main { max-width:1000px; margin:0 auto; padding:24px 20px 60px; }
  h1 { font-size:20px; margin:0 0 4px; }
  h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
       color:#8b949e; margin:28px 0 10px; font-weight:600; }
  .sub { color:#8b949e; margin:0 0 20px; }
  .cards { display:flex; flex-wrap:wrap; gap:12px; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:8px;
          padding:12px 16px; min-width:120px; }
  .card .n { font-size:22px; font-weight:600; }
  .card .l { color:#8b949e; font-size:12px; }
  .grid { display:flex; gap:24px; flex-wrap:wrap; align-items:flex-start; }
  img.board { border-radius:8px; border:1px solid #21262d; max-width:100%; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th { text-align:left; color:#8b949e; font-weight:600; padding:6px 10px 6px 0;
       border-bottom:1px solid #21262d; }
  td { padding:6px 10px 6px 0; border-bottom:1px solid #161b22; }
  code { background:#161b22; padding:1px 5px; border-radius:4px; }
  .ok { color:#4ade80; } .bad { color:#ff6b6b; } .warn { color:#fbbf24; }
  .pill { display:inline-block; padding:1px 8px; border-radius:999px;
          font-size:12px; background:#21262d; }
  .note { color:#8b949e; font-size:12px; margin-top:6px; }
</style>
<main>
  <h1>Minesweeper bot
    {% if bot.running %}<span class="pill ok">running · pid {{ bot.pid }}</span>
    {% elif bot.known %}<span class="pill bad">stopped</span>
    {% else %}<span class="pill warn">not loaded in launchd</span>{% endif %}
  </h1>
  <p class="sub">
    {{ config.handle or "no handle configured" }} ·
    {{ config.rows }}x{{ config.cols }}, {{ config.mines }} mines ·
    {{ config.turn_minutes }} min turns · quorum {{ config.quorum }}
    {% if config.post_mode != "live" %}
      · <span class="warn">POST_MODE={{ config.post_mode }}</span>{% endif %}
  </p>

  <div class="cards">
    <div class="card"><div class="n">{{ record.played }}</div>
      <div class="l">boards played</div></div>
    <div class="card"><div class="n ok">{{ record.cleared }}</div>
      <div class="l">cleared</div></div>
    <div class="card"><div class="n bad">{{ record.exploded }}</div>
      <div class="l">lost</div></div>
    <div class="card"><div class="n">{{ record.avg_turns }}</div>
      <div class="l">avg turns</div></div>
  </div>

  {% if board %}
  <h2>Board {{ board.game_id }} — turn {{ board.turn }} ({{ board.status }})</h2>
  <div class="grid">
    <div>
      <img class="board" src="/board.png" width="440"
           alt="Current board as followers see it">
      <div class="note">{{ board.opened }}/{{ board.total }} cells ·
        last move {{ board.last_coord }} ({{ board.last_result }},
        {{ board.last_source }})</div>
    </div>
    <div style="flex:1; min-width:280px">
      <h2 style="margin-top:0">Live votes</h2>
      {% if board.votes %}
      <table>
        <tr><th>Cell</th><th>Votes</th><th>First caller</th></tr>
        {% for row in board.votes %}
        <tr><td><code>{{ row.coord }}</code></td><td>{{ row.votes }}</td>
            <td>{{ row.first_caller }}</td></tr>
        {% endfor %}
      </table>
      <div class="note">
        {% if board.winner and board.winner.meets_quorum %}
          <span class="ok">{{ board.winner.coord }} would be played</span>
          ({{ board.winner.votes }} of {{ board.winner.voters }})
        {% else %}
          <span class="warn">below quorum — the bot would play its own move</span>
        {% endif %}
      </div>
      {% else %}
      <p class="note">No votes yet.</p>
      {% endif %}

      <h2>Flags</h2>
      {% if board.flags %}
      <p><code>{{ board.flags|join(', ') }}</code></p>
      <p class="note">
        {% set right = board.flags|select('in', board.solver.mines)|list %}
        {{ right|length }} of {{ board.flags|length }} are provably mines.
        The board draws them all the same — a wrong flag must look exactly
        like a right one.
      </p>
      {% else %}
      <p class="note">Nobody has flagged anything on this board.</p>
      {% endif %}
      {% if board.flag_scores %}
      <table>
        <tr><th>Flagger</th><th>Right</th><th>Scored</th></tr>
        {% for row in board.flag_scores %}
        <tr><td>{{ row.handle }}</td><td class="ok">{{ row.hits }}</td>
            <td>{{ row.total }}</td></tr>
        {% endfor %}
      </table>
      {% endif %}

      <h2>Solver</h2>
      <p class="note">
        This position needs <strong>{{ board.solver.level }}</strong> reasoning.
        {% if not board.solver.exact %}
          <span class="warn">(a frontier component was too large to enumerate)</span>
        {% endif %}
      </p>
      <p><span class="ok">{{ board.solver.safe|length }} provably safe</span>:
        <code>{{ board.solver.safe[:14]|join(', ') }}</code></p>
      <p><span class="bad">{{ board.solver.mines|length }} provably mined</span>:
        <code>{{ board.solver.mines[:14]|join(', ') }}</code></p>
    </div>
  </div>

  <h2>Recent moves</h2>
  <table>
    <tr><th>Turn</th><th>Cell</th><th>Result</th><th>Chosen by</th>
        <th>Votes</th><th>Provably safe?</th></tr>
    {% for move in moves %}
    <tr>
      <td>{{ move.turn_number }}</td>
      <td><code>{{ move.coord }}</code></td>
      <td class="{{ 'bad' if move.result == 'mine' else 'ok' }}">{{ move.result }}</td>
      <td>{{ move.caller or move.source }}</td>
      <td>{% if move.votes %}{{ move.votes }}/{{ move.voters }}{% else %}—{% endif %}</td>
      <td>{% if move.was_provably_safe == 1 %}yes
          {% elif move.was_provably_safe == 0 %}<span class="warn">no</span>
          {% else %}—{% endif %}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <h2>No board in progress</h2>
  {% endif %}

  <h2>Finished boards</h2>
  <table>
    <tr><th>Board</th><th>Result</th><th>Turns</th><th>Cleared</th><th>When</th></tr>
    {% for row in history %}
    <tr><td>{{ row.game_id }}</td>
        <td class="{{ 'ok' if row.status == 'cleared' else 'bad' }}">{{ row.status }}</td>
        <td>{{ row.turns }}</td>
        <td>{{ row.opened }}/{{ row.total }}</td>
        <td>{{ row.completed_at }}</td></tr>
    {% endfor %}
  </table>

  <p class="note" style="margin-top:30px">
    Read-only. The database is opened with PRAGMA query_only and votes are read
    from the public AppView with no credentials. This page shows the solver's
    answers — keep it on localhost.
  </p>
</main>
"""


def main() -> None:
    db.init_db()
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    app.run(host=host, port=config.DASHBOARD_PORT, debug=False)


if __name__ == "__main__":
    main()
