"""Orchestrator: the scheduled loop that plays a crowd-voted Minesweeper board.

One account, one board, one turn every TURN_MINUTES. Each turn:

    read the replies to the last post   ->  votes.tally
    if enough people agree, open that cell; otherwise open the safest cell
    post the new board and ask for the next coordinate
    reply to the follower whose call was played, so they get a notification

Sudden death: one mine ends the run, and the post-mortem says whether a
provably safe cell was on the table. A new board starts after a delay.

The ordering in `_game_tick` matters: the move is applied in memory, the post
goes out, and only then is state saved. A network blip therefore replays the
turn against the same post's replies instead of silently losing it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

import bluesky
import config
import db
import game
import renderer
import solver
import votes

logger = logging.getLogger("minesweeper")

scheduler = BlockingScheduler()


# ---------------------------------------------------------------------------
# Post copy
# ---------------------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _outcome_phrase(state: game.GameState, coord: str) -> str:
    """"was safe, touching 2 mines" / "hit a mine"."""
    if state.last_result == game.MINE:
        return "hit a mine"
    try:
        count = state.revealed[game.coord_to_index(coord)]
    except (KeyError, ValueError):
        return "was safe"
    if count == 0:
        return "was blank — the whole region opened up"
    return f"was safe, touching {_plural(count, 'mine')}"


def _credit_line(vote, source: str) -> str:
    """Say who chose this cell. Naming the caller is the point: it makes a
    follower's influence visible, and the matching reply notifies them."""
    if source == "crowd" and vote is not None:
        return (f"🫡 @{vote.caller_handle} called it "
                f"({vote.votes} of {vote.total_voters} votes)")
    if vote is None:
        return "🤖 No votes came in, so I played it myself."
    return (f"🤖 No consensus — best cell had {_plural(vote.votes, 'vote')}, "
            f"so I played it myself.")


def _ask_line(state: game.GameState) -> str:
    left = state.total_safe - len(state.revealed)
    last = f"{game.row_letters(state.rows)[-1]}{state.cols}"
    return (f"{_plural(left, 'cell')} left, {state.mine_count} mines.\n"
            f"Reply with a coordinate, A1 to {last}.\n"
            f"{config.TURN_MINUTES} min. {config.HASHTAG}")


def build_turn_text(state: game.GameState, coord: str, vote, source: str) -> str:
    return (f"Turn {state.turn_number} · {coord} {_outcome_phrase(state, coord)}\n"
            f"{_credit_line(vote, source)}\n\n"
            f"{_ask_line(state)}")


def build_opening_text(state: game.GameState, record: dict) -> str:
    scoreline = ""
    if record["played"]:
        scoreline = (f"All time: {record['cleared']} cleared, "
                     f"{record['exploded']} lost.\n")
    return (f"💣 NEW BOARD — {state.rows}x{state.cols}, "
            f"{state.mine_count} mines.\n\n"
            f"I opened {state.last_coord} to start us off. "
            f"You pick the rest — one mine ends the run.\n"
            f"{scoreline}\n"
            f"{_ask_line(state)}")


def build_gameover_text(state: game.GameState, coord: str, vote, source: str,
                        had_safe: str, record: dict, crowd_moves: int = 0) -> str:
    opened, total = len(state.revealed), state.total_safe
    if state.status == game.CLEARED:
        head = (f"🎉 BOARD CLEARED in {_plural(state.turn_number, 'turn')}!\n"
                f"{coord} finished it — all {state.mine_count} mines found, "
                f"none of them opened.")
    else:
        head = (f"💥 BOOM — {coord} was a mine. "
                f"That ends it on turn {state.turn_number}.\n"
                f"Cleared {opened} of {total} cells.")
        if had_safe:
            head += f" {had_safe} was provably safe."

    yours = (f"You called {crowd_moves} of "
             f"{_plural(state.turn_number, 'move')}.\n" if crowd_moves else "")
    tail = (f"{yours}"
            f"All time: {record['cleared']} cleared, {record['exploded']} lost.\n"
            f"New board in {_restart_phrase()}. {config.HASHTAG}")
    return f"{head}\n{_credit_line(vote, source)}\n\n{tail}"


def build_credit_reply(state: game.GameState, coord: str, vote) -> str:
    if state.status == game.EXPLODED:
        outcome = "and it was a mine. That's the run — thanks for playing."
    elif state.status == game.CLEARED:
        outcome = "and that cleared the board. Nice."
    else:
        outcome = f"and it {_outcome_phrase(state, coord)}."
    return (f"🎯 Your call. We opened {coord} on turn {state.turn_number} "
            f"{outcome}\n\n"
            f"({vote.votes} of {vote.total_voters} votes) {config.HASHTAG}")


def _restart_phrase() -> str:
    minutes = config.RESTART_DELAY_SECONDS // 60
    if minutes >= 120:
        return f"{minutes // 60} hours"
    if minutes == 60:
        return "an hour"
    return f"{minutes} minutes"


# ---------------------------------------------------------------------------
# Posting helpers
# ---------------------------------------------------------------------------

def _remember(uri: str, kind: str, game_id: int, turn: int | None = None) -> None:
    """Post tracking must never break posting, so failures only log."""
    try:
        db.record_post(uri, kind, game_id=game_id, turn_number=turn)
    except Exception:
        logger.exception("Failed to record post %s", uri)


def _post_board(state: game.GameState, text: str, kind: str,
                extra_dids: dict | None = None) -> str:
    image = renderer.render_board(state, highlight=state.last_coord)
    alt = renderer.build_alt_text(state)
    uri = bluesky.post_with_image(text, image, alt=alt, kind=kind,
                                  extra_dids=extra_dids)
    _remember(uri, kind, state.game_id, state.turn_number)
    return uri


def _fetch_replies(uri: str) -> list:
    if not uri:
        return []
    try:
        return bluesky.get_replies(uri)
    except Exception:
        logger.exception("Failed to fetch replies from %s", uri)
        return []


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def game_tick() -> None:
    try:
        _game_tick()
    except Exception:
        logger.exception("game_tick failed")      # never kill the scheduler


def _game_tick() -> None:
    state = db.load_state()
    if state is None:
        logger.warning("No board found on tick; starting one")
        start_new_game()
        return
    if state.status != game.ACTIVE:
        logger.info("Board %s finished (%s); waiting for the restart timer",
                    state.game_id, state.status)
        return

    position = solver.Position.from_state(state)
    analysis = solver.analyze(position)

    # 1. Read the crowd.
    already_open = {game.index_to_coord(r, c) for (r, c) in state.revealed}
    vote = votes.tally(_fetch_replies(state.last_post_uri), already_open,
                       state.rows, state.cols)

    # 2. Decide. Below the quorum the crowd has not actually agreed on
    #    anything — a single stray vote would carry the turn — so the bot
    #    plays its own safest cell and the post says so.
    if vote is not None and vote.votes >= config.QUORUM:
        coord, source = vote.coord, "crowd"
        logger.info("Turn %d by crowd: %s (%d of %d votes, called by %s)",
                    state.turn_number + 1, coord, vote.votes,
                    vote.total_voters, vote.caller_handle or "unknown")
    else:
        cell, reason = solver.safest_move(position, analysis)
        coord, source = game.index_to_coord(*cell), "bot"
        logger.info("Turn %d by bot (%s): %s%s", state.turn_number + 1, reason,
                    coord,
                    f" — best crowd cell had {vote.votes} vote(s)" if vote else
                    " — no votes")

    index = game.coord_to_index(coord)
    was_provably_safe = index in analysis.safe
    # Named before the move, for the post-mortem if this turn ends the run.
    safe_alternative = (game.index_to_coord(*sorted(analysis.safe)[0])
                        if analysis.safe else "")

    # 3. Play it.
    state.turn_number += 1
    result = game.reveal(state, *index)
    if result == game.ALREADY:
        logger.error("Turn %d picked an open cell %s; skipping",
                     state.turn_number, coord)
        return
    state.last_coord = coord
    state.last_result = result
    state.last_source = source
    state.last_caller = vote.caller_handle if (vote and source == "crowd") else ""
    state.last_votes = vote.votes if (vote and source == "crowd") else 0
    state.last_voters = vote.total_voters if vote else 0

    # 4. Post. On failure nothing is saved, so the next tick replays this turn
    #    against the same post rather than losing it.
    extra = {}
    if source == "crowd" and vote and vote.caller:
        extra[vote.caller.handle] = vote.caller.did

    finished = state.status != game.ACTIVE
    record = db.get_record()
    if finished:
        # This turn's move is not in the table yet — it is recorded after the
        # post goes out — so count it here or the crowd loses credit for the
        # move that ended the board.
        crowd_moves = sum(1 for m in db.get_moves(state.game_id, limit=500)
                          if m["source"] == "crowd") + (source == "crowd")
        text = build_gameover_text(state, coord, vote, source,
                                   safe_alternative if result == game.MINE else "",
                                   record, crowd_moves)
        kind = "gameover"
    else:
        text = build_turn_text(state, coord, vote, source)
        kind = "turn"

    try:
        uri = _post_board(state, text, kind, extra_dids=extra)
    except Exception:
        logger.exception("Posting failed; turn not saved and will be replayed")
        return
    state.last_post_uri = uri

    # 5. Credit the follower whose call was played. Non-essential: a failure
    #    must not roll back a turn that has already posted.
    if source == "crowd" and vote and vote.caller and vote.caller.uri:
        try:
            reply_uri = bluesky.post_reply(
                build_credit_reply(state, coord, vote),
                parent_uri=vote.caller.uri, parent_cid=vote.caller.cid,
                root_uri=vote.caller.root_uri, root_cid=vote.caller.root_cid)
            _remember(reply_uri, "credit", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to post credit reply")

    # 6. Persist.
    try:
        db.record_move(state, coord, result, source,
                       caller=state.last_caller, votes=state.last_votes,
                       voters=state.last_voters,
                       was_provably_safe=was_provably_safe)
    except Exception:
        logger.exception("Failed to record move")
    db.save_state(state)

    if finished:
        db.record_finished(state)
        logger.info("Board %d %s on turn %d (%d of %d cells)", state.game_id,
                    state.status, state.turn_number, len(state.revealed),
                    state.total_safe)
        run_date = datetime.now() + timedelta(seconds=config.RESTART_DELAY_SECONDS)
        scheduler.add_job(start_new_game, "date", run_date=run_date,
                          id=f"restart_{state.game_id}", replace_existing=True)
        logger.info("Next board scheduled for %s", run_date)


def start_new_game() -> None:
    try:
        _start_new_game()
    except Exception:
        logger.exception("start_new_game failed")


def _start_new_game() -> None:
    previous = db.load_state()
    game_id = (previous.game_id + 1) if previous else 1
    state = game.new_game(game_id, rows=config.ROWS, cols=config.COLS,
                          mines=config.MINES)
    logger.info("Starting board %d (%dx%d, %d mines), opened at %s",
                game_id, state.rows, state.cols, state.mine_count,
                state.last_coord)

    try:
        state.last_post_uri = _post_board(
            state, build_opening_text(state, db.get_record()), "newgame")
    except Exception:
        logger.exception("Failed to post the new board")
        # Save anyway: the board exists, and the next tick can still play
        # (with no post to read, the bot simply plays its own move).

    db.save_state(state)
    try:
        db.record_move(state, state.last_coord, game.SAFE, "opening")
    except Exception:
        logger.exception("Failed to record the opening move")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """Log to LOG_PATH, and to the screen as well when interactive.

    launchd redirects stdout into the same file named by LOG_PATH, so adding a
    stdout handler on top of the file handler writes every line twice. stdout
    is a TTY only when running in the foreground, which is exactly when the
    screen echo is wanted.
    """
    handlers = []
    if config.LOG_PATH:
        handlers.append(logging.FileHandler(config.LOG_PATH))
    if not handlers or sys.stdout.isatty():
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def play_turns(count: int) -> None:
    """Run `count` turns back to back, starting a board if there isn't one.

    For dry runs and for operations — playing a single turn by hand after a
    restart is much easier than waiting out the interval.
    """
    if db.load_state() is None:
        start_new_game()
    for _ in range(count):
        state = db.load_state()
        if state is not None and state.status != game.ACTIVE:
            logger.info("Board finished; starting the next one")
            start_new_game()
            continue
        game_tick()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--play", type=int, metavar="N",
                        help="play N turns immediately and exit, instead of "
                             "starting the scheduler")
    args = parser.parse_args()

    setup_logging()
    logger.info("Minesweeper bot starting (%dx%d, %d mines, %d min turns, "
                "quorum %d, mode %s)", config.ROWS, config.COLS, config.MINES,
                config.TURN_MINUTES, config.QUORUM, config.POST_MODE)

    db.init_db()
    bluesky.login_with_retry()

    if args.play:
        play_turns(args.play)
        return

    state = db.load_state()
    if state is None or state.status != game.ACTIVE:
        start_new_game()

    scheduler.add_job(game_tick, "interval", seconds=config.TURN_MINUTES * 60,
                      id="game_tick", coalesce=True, max_instances=1)
    logger.info("Scheduler started; one turn every %d minutes",
                config.TURN_MINUTES)
    scheduler.start()


if __name__ == "__main__":
    main()
