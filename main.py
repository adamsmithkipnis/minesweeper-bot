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
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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

def pick_hashtags(rng=None) -> list:
    """The always-on tag, then a fresh random sample of the pool.

    Posting the identical block of six tags every hour reads as a bot padding
    for reach; drawing them fresh each time reaches more corners of the
    network and looks like a person choosing. The game's own tag is never
    dropped, so the account stays findable under one stable name.
    """
    rng = rng or random
    always = config.HASHTAG_ALWAYS
    pool = [tag for tag in config.HASHTAG_POOL
            if tag.lower() != always.lower()]
    picked = rng.sample(pool, min(config.HASHTAG_COUNT, len(pool)))
    return ([always] if always else []) + picked


def _with_tags(text: str, tags: list | None = None) -> str:
    """Append hashtags, stopping at the first one that will not fit.

    Content wins over reach: the board information is built first and a tag is
    only added while the whole post still clears the limit, so discovery tags
    can never be the reason a post gets clamped.
    """
    out = text
    for i, tag in enumerate(pick_hashtags() if tags is None else tags):
        candidate = f"{out}{chr(10) if i == 0 else ' '}{tag}"
        if len(candidate) > bluesky.POST_LIMIT:
            break
        out = candidate
    return out


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
    return (f"🤖 {vote.total_voters} voters, no two agreeing — "
            f"so I broke the tie myself.")


def _knockout_ask_line(state: game.GameState, flags=(),
                       teach_flagging: bool = False) -> str:
    """The prompt for knockout play.

    Nothing about voting: everybody who replies acts, so the rules a reader
    needs are what a mine costs them and that flagging keeps them in the game
    afterwards.
    """
    left = state.total_safe - len(state.revealed)
    last = f"{game.row_letters(state.rows)[-1]}{state.cols}"
    counted = (f"{_plural(left, 'cell')} left, "
               f"{_plural(state.spares_left, 'spare')}"
               + (f", {len(flags)} flagged.\n" if flags else ".\n"))
    if teach_flagging:
        ask = (f"Everyone who replies opens a cell. A mine takes you out of "
               f"this board — you can still flag.\n")
    else:
        ask = (f"Reply a coordinate, A1 to {last}. Everyone who replies "
               f"opens one.\n")
    return f"{counted}{ask}{config.TURN_MINUTES} min."


def _ask_line(state: game.GameState, flags=(), teach_flagging: bool = False) -> str:
    """The prompt. Nobody flags a cell unless something invites them to.

    Voting is discoverable only because the post asks for a coordinate, and no
    amount of forgiving parsing fixes an instruction nobody gave. So the ask
    rotates: on turns where flagging is worth teaching it says so, and the
    hashtags give way to make room, because a prompt beats reach.
    """
    left = state.total_safe - len(state.revealed)
    last = f"{game.row_letters(state.rows)[-1]}{state.cols}"
    counted = (f"{_plural(left, 'cell')} left, {state.mine_count} mines"
               + (f", {len(flags)} flagged.\n" if flags else ".\n"))
    if teach_flagging:
        ask = (f"Open: D4 · Flag: 'flag C3' · Change vote: reply again.\n"
               f"One vote can decide; with 3+ voters, 2 must agree.\n")
    else:
        ask = f"Reply with a coordinate, A1 to {last}.\n"
    return f"{counted}{ask}{config.TURN_MINUTES} min."


def _should_teach_flagging(state: game.GameState, flags) -> bool:
    """Teach while nobody is flagging, then every third turn as a reminder."""
    if not flags:
        return True
    return state.turn_number % 3 == 0


def build_turn_text(state: game.GameState, coord: str, vote, source: str,
                    flags=()) -> str:
    return _with_tags(
        f"Turn {state.turn_number} · {coord} {_outcome_phrase(state, coord)}\n"
        f"{_credit_line(vote, source)}\n\n"
        f"{_ask_line(state, flags, _should_teach_flagging(state, flags))}")


def build_opening_text(state: game.GameState, record: dict) -> str:
    """The new-board post, budgeted so it can never be clamped.

    Knockout's copy is longer than voting's, and a tier-3 banner is longer
    than a tier-1 one, so the fixed layout that fitted in one mode at one tier
    overflowed in another. The parts a reader cannot play without are built
    first; the rest is added only while the whole post still fits.
    """
    tier = tier_of(state)
    banner = (f"💣 NEW BOARD — Tier {tier + 1} · "
              f"{state.rows}x{state.cols}, {state.mine_count} mines"
              + (f", {tier + 1}x points." if tier else "."))
    ask = (_knockout_ask_line(state, teach_flagging=True) if config.KNOCKOUT
           else _ask_line(state, teach_flagging=True))
    required = f"{banner}\n\n{ask}"

    rules = ("You each pick a cell. A mine takes you out; "
             f"{_plural(state.mine_budget, 'spare')} before the board falls."
             if config.KNOCKOUT else
             "You pick the rest — one mine ends the run.")
    optional = [f"I opened {state.last_coord} to start us off. {rules}"]
    if record["played"]:
        optional.append(f"All time: {record['cleared']} cleared, "
                        f"{record['exploded']} lost.")

    kept = []
    for line in optional:
        candidate = kept + [line]
        body = f"{banner}\n\n" + "\n".join(candidate) + f"\n\n{ask}"
        if len(body) <= bluesky.POST_LIMIT:
            kept = candidate
    if not kept:
        return _with_tags(required)
    return _with_tags(f"{banner}\n\n" + "\n".join(kept) + f"\n\n{ask}")


def build_tutorial_text(state: game.GameState) -> str:
    """Canonical copy for the pinned how-to post.

    Keeping this generated and tested prevents the public tutorial from
    drifting away from the parser and quorum rules again.
    """
    last = f"{game.row_letters(state.rows)[-1]}{state.cols}"
    return _with_tags(
        f"HOW TO PLAY · Reply on the latest board (to {last})\n\n"
        f"Open a cell: D4\n"
        f"Flag mines: flag C3 (several are okay)\n"
        f"Explain + play: C3 is a mine, so D4 is safe — D4\n"
        f"Change your vote: reply again; your latest choice counts.\n\n"
        f"One vote can decide. With 3+ voters, 2 must agree; "
        f"tied cells go to the earliest vote.")


def build_gameover_text(state: game.GameState, coord: str, vote, source: str,
                        had_safe: str, record: dict, crowd_moves: int = 0,
                        flag_line: str = "", mvp_line: str = "",
                        next_board_at: datetime | None = None) -> str:
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

    # The result, who called it, and when the next board starts are the parts
    # that must survive. Recognition lines are added only while the whole post
    # still fits — otherwise the clamp eats the end of the post instead, which
    # is how "New board in an hour" became "New bo…".
    next_phrase = (next_board_at.astimezone().strftime("%a %-I:%M %p %Z")
                   if next_board_at else f"in {_restart_phrase()}")
    required = (f"All time: {record['cleared']} cleared, "
                f"{record['exploded']} lost.\n"
                f"Next board: {next_phrase}. Follow to catch the opening.")
    optional = [
        mvp_line,
        f"You called {crowd_moves} of {_plural(state.turn_number, 'move')}."
        if crowd_moves else "",
        flag_line,
    ]

    kept = []
    for line in filter(None, optional):
        candidate = kept + [line]
        body = (f"{head}\n{_credit_line(vote, source)}\n\n"
                + "\n".join(candidate + [required]))
        if len(body) <= bluesky.POST_LIMIT:
            kept = candidate
    return _with_tags(f"{head}\n{_credit_line(vote, source)}\n\n"
                      + "\n".join(kept + [required]))


def build_flag_reply(coords: list, total_flagged: int) -> str:
    """Confirm a flag publicly.

    This is what breaks the chicken and egg: flags on the board teach the
    syntax, but somebody has to flag first. Answering the first person to try
    it shows everyone else in the thread that it worked.
    """
    which = coords[0] if len(coords) == 1 else ", ".join(coords[:3])
    return _with_tags(
        f"🚩 Flagged {which}. It shows on the board and costs no turn — "
        f"you can flag and still vote to open somewhere else in the same "
        f"reply.\n\n"
        f"{_plural(total_flagged, 'cell')} flagged so far.",
        tags=[config.REPLY_HASHTAG])


def build_credit_reply(state: game.GameState, coord: str, vote,
                       points: int = 0, this_game: int = 0,
                       all_time: int = 0) -> str:
    """Thank the caller, and tell them what it earned them.

    Scores are the point of coming back: a move is worth the cells it
    opened, so the reply is also the only place most people ever see their
    running total.
    """
    if state.status == game.EXPLODED:
        outcome = "and it was a mine. That's the run — thanks for playing."
    elif state.status == game.CLEARED:
        outcome = "and that cleared the board. Nice."
    else:
        outcome = f"and it {_outcome_phrase(state, coord)}."
    scored = f"+{_plural(points, 'point')}\n" if points else ""
    return _with_tags(
        f"🎯 Your call. We opened {coord} on turn {state.turn_number} "
        f"{outcome}\n\n"
        f"{scored}"
        f"This game: {this_game}\n"
        f"All time: {all_time}",
        tags=[config.REPLY_HASHTAG])


def build_flag_line(scores: list) -> str:
    """Who read the mines correctly on this board.

    Per board rather than all time: recognition without a leaderboard to
    maintain, and nobody accumulates a losing record they cannot shake.
    """
    scored = [s for s in scores if s["hits"]]
    if not scored:
        return ""
    best = scored[0]
    line = f"🚩 @{best['handle']} called {best['hits']} of {best['total']} mines"
    if len(scored) > 1:
        line += f", then @{scored[1]['handle']} ({scored[1]['hits']})"
    return line + "."


def build_mvp_line(leaders: list) -> str:
    """Compact board-end recognition in the main feed."""
    if not leaders:
        return ""
    best = leaders[0]
    return (f"🏅 Board MVP: @{best['handle']} — "
            f"{_plural(best['points'], 'point')}.")


def leaders_with_pending_move(leaders: list, did: str, handle: str,
                              points: int) -> list:
    """Include the just-played move without saving it before posting succeeds."""
    out = [dict(row) for row in leaders]
    if did:
        found = next((row for row in out if row["did"] == did), None)
        if found:
            found["points"] += points
            found["moves"] += 1
        else:
            out.append({"did": did, "handle": handle,
                        "points": points, "moves": 1})
    return sorted(out, key=lambda row: (-row["points"], row["moves"]))


def tier_of(state: game.GameState | None) -> int:
    """Which rung of the ladder a board is on, by its dimensions.

    Derived rather than stored, so an in-progress board survives a change to
    the tier list and no migration is needed.
    """
    if state is None or not config.TIERS:
        return 0
    for index, (rows, cols, _) in enumerate(config.TIERS):
        if (rows, cols) == (state.rows, state.cols):
            return index
    # An unrecognised size (hand-set in .env, or a retired tier) maps to the
    # nearest rung by area rather than silently resetting to the bottom.
    return min(range(len(config.TIERS)),
               key=lambda i: abs(config.TIERS[i][0] * config.TIERS[i][1]
                                 - state.rows * state.cols))


@dataclass
class Play:
    """One player's action, resolved."""
    did: str
    handle: str
    coord: str
    result: str
    points: int = 0


def mine_budget_for(tier: int) -> int:
    """Detonations a board absorbs before it fails.

    Measured at five players: tier + 1 loses about a fifth of boards on every
    rung, which is the right weight when being knocked out yourself is the
    main event. Tier + 2 leaves boards clearing 93-98%, at which point the
    collective stake stops meaning anything.
    """
    return tier + config.MINE_BUDGET_BASE if config.KNOCKOUT else 0


def tier_multiplier(state: game.GameState | None) -> int:
    """What a cell is worth on this board: tier 1 pays 1x, tier 3 pays 3x.

    Higher tiers are longer and likelier to end in a bang, so the reward has
    to rise with the stakes or the ladder is all cost. Scaling the points
    rather than handing out more cells per turn matters: the extra reward is
    spread over MORE turns, because harder boards run longer, instead of
    being concentrated into fewer.
    """
    return tier_of(state) + 1


def participation_of(previous: game.GameState, crowd_moves: int | None) -> float:
    if crowd_moves is None:
        crowd_moves = sum(1 for move in db.get_moves(previous.game_id, limit=1000)
                          if move["source"] == "crowd")
    return crowd_moves / previous.turn_number if previous.turn_number else 0.0


def next_tier(previous: game.GameState | None,
              crowd_moves: int | None = None) -> int:
    """The rung the next board sits on.

    Promotion needs a win *and* real participation, so the board only gets
    harder when the crowd is actually driving it. Demotion needs only poor
    participation: losing a hard board is the game working, but a board the
    bot had to play itself is one nobody is playing.
    """
    if previous is None or not config.ADAPTIVE_BOARD:
        return 0
    current = tier_of(previous)
    share = participation_of(previous, crowd_moves)
    if (previous.status == game.CLEARED
            and share >= config.GROW_PARTICIPATION):
        return min(current + 1, len(config.TIERS) - 1)
    if share < config.SHRINK_PARTICIPATION:
        return max(current - 1, 0)
    return current


def next_board_settings(previous: game.GameState | None,
                        crowd_moves: int | None = None) -> tuple:
    """(rows, cols, mines) for the next game."""
    if not config.ADAPTIVE_BOARD or not config.TIERS:
        return config.ROWS, config.COLS, config.MINES
    return config.TIERS[next_tier(previous, crowd_moves)]


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
                extra_dids: dict | None = None, flags=()) -> str:
    image = renderer.render_board(state, highlight=state.last_coord, flags=flags)
    alt = renderer.build_alt_text(state, flags)
    uri = bluesky.post_with_image(text, image, alt=alt, kind=kind,
                                  extra_dids=extra_dids)
    _remember(uri, kind, state.game_id, state.turn_number)
    return uri


def _crowd_decides(vote) -> bool:
    """Whether the crowd's pick is played rather than the bot's own move."""
    return (vote.votes >= config.QUORUM
            or vote.total_voters <= config.QUORUM)


def apply_knockout_moves(state: game.GameState, replies: list,
                        already_open: set, position, analysis) -> tuple:
    """Open one cell for every eligible player. Returns (plays, source).

    Order is by reply time, so the earliest replier acts first. A cell that
    somebody else's cascade already opened is skipped without penalty — being
    beaten to a square is not a wasted turn. Knocked-out players are excluded
    here, but they can still flag, which costs no turn and carries no risk.
    """
    # Benched players, not everyone ever knocked out: an elimination expires
    # after ELIMINATION_TURNS so a two-player board is not decided by whoever
    # steps on the first mine.
    excluded = db.benched(state.game_id, state.turn_number + 1,
                          config.ELIMINATION_TURNS)
    pending = votes.moves(replies, already_open, state.rows, state.cols,
                          excluded)
    state.turn_number += 1

    plays = []
    for move in pending:
        try:
            index = game.coord_to_index(move.coord)
        except ValueError:
            continue
        before = len(state.revealed)
        outcome = game.reveal(state, *index)
        if outcome == game.ALREADY:
            continue
        if outcome == game.MINE:
            try:
                db.eliminate(state.game_id, move.did, move.handle,
                             move.coord, state.turn_number)
            except Exception:
                logger.exception("Failed to record an elimination")
            plays.append(Play(move.did, move.handle, move.coord, outcome))
            logger.info("Turn %d: %s hit %s and is out (%d spare(s) left)",
                        state.turn_number, move.handle, move.coord,
                        state.spares_left)
        else:
            plays.append(Play(move.did, move.handle, move.coord, outcome,
                              (len(state.revealed) - before)
                              * tier_multiplier(state)))
        if state.status != game.ACTIVE:
            break

    if plays:
        return plays, "crowd"

    # Nobody eligible moved, so the board would otherwise freeze.
    cell, reason = solver.safest_move(position, analysis)
    coord = game.index_to_coord(*cell)
    before = len(state.revealed)
    outcome = game.reveal(state, *cell)
    logger.info("Turn %d by bot (%s): %s", state.turn_number, reason, coord)
    return [Play("", "", coord, outcome,
                 (len(state.revealed) - before) * tier_multiplier(state))], "bot"


def build_knockout_turn_text(state: game.GameState, plays: list,
                             flags=()) -> str:
    """The board post for a knockout turn."""
    opened = sum(p.points for p in plays if p.result == game.SAFE)
    movers = sum(1 for p in plays if p.did)
    out = [p for p in plays if p.result == game.MINE]
    scorers = sorted((p for p in plays if p.result == game.SAFE and p.did),
                     key=lambda p: -p.points)

    head = (f"Turn {state.turn_number} · {_plural(movers, 'player')} moved"
            if movers else f"Turn {state.turn_number} · nobody moved, so I did")
    lines = [head]
    for play in out[:2]:
        lines.append(f"💥 @{play.handle} hit {play.coord} and is out. "
                     f"{_plural(state.spares_left, 'spare')} left.")
    if scorers:
        lines.append(f"🏅 @{scorers[0].handle} +{scorers[0].points}")

    body = "\n".join(lines)
    ask = _knockout_ask_line(state, flags,
                             _should_teach_flagging(state, flags))
    while len(f"{body}\n\n{ask}") > bluesky.POST_LIMIT and len(lines) > 1:
        lines.pop()
        body = "\n".join(lines)
    return _with_tags(f"{body}\n\n{ask}")


def build_knockout_gameover_text(state: game.GameState, record: dict,
                                 survivors: int, entrants: int,
                                 mvp_line: str, next_board_at) -> str:
    tier = tier_of(state) + 1
    if state.status == game.CLEARED:
        head = (f"🎉 BOARD SWEPT in {_plural(state.turn_number, 'turn')} — "
                f"Tier {tier}.\n"
                f"{_plural(state.detonations, 'mine')} found the hard way, "
                f"{survivors} of {entrants} still standing.")
    else:
        head = (f"💥 BOARD LOST — {state.exploded_cell} was one mine too many "
                f"on Tier {tier}.\n"
                f"Cleared {len(state.revealed)} of {state.total_safe} cells; "
                f"{survivors} of {entrants} still standing.")

    next_phrase = (next_board_at.astimezone().strftime("%a %-I:%M %p %Z")
                   if next_board_at else f"in {_restart_phrase()}")
    required = (f"All time: {record['cleared']} cleared, "
                f"{record['exploded']} lost.\n"
                f"Next board: {next_phrase}. Follow to catch the opening.")

    kept = []
    for line in filter(None, [mvp_line]):
        body = f"{head}\n" + "\n".join(kept + [line, required])
        if len(body) <= bluesky.POST_LIMIT:
            kept.append(line)
    return _with_tags(f"{head}\n" + "\n".join(kept + [required]))


def _record_flags(state: game.GameState, claimed: dict, withdrawn: dict) -> None:
    """Persist this turn's mine claims. Never allowed to break a turn."""
    try:
        for coord, repliers in claimed.items():
            for reply in repliers:
                db.add_flag(state.game_id, coord, reply.did, reply.handle,
                            state.turn_number)
        for coord, repliers in withdrawn.items():
            for reply in repliers:
                db.remove_flag(state.game_id, coord, reply.did)
        if claimed or withdrawn:
            logger.info("Flags: %d claimed, %d withdrawn",
                        sum(len(v) for v in claimed.values()),
                        sum(len(v) for v in withdrawn.values()))
    except Exception:
        logger.exception("Failed to record flags")


def _resolve_flags(state: game.GameState, coord: str, result: str,
                   revealed_before: set, final: bool = False) -> None:
    """Score claims the board has just settled.

    Opening a cell disproves any flag on it; hitting a mine proves one. When
    the board ends, everything still outstanding is scored against the real
    layout — which is the only moment the hidden state is allowed to touch
    the flag record.
    """
    try:
        resolutions = {game.index_to_coord(*cell): False
                       for cell in set(state.revealed) - revealed_before}
        if result == game.MINE:
            resolutions[coord] = True
        if final:
            for outstanding in db.flag_counts(state.game_id):
                try:
                    cell = game.coord_to_index(outstanding)
                except ValueError:
                    continue
                resolutions.setdefault(outstanding, cell in state.mine_cells)
        scored = db.resolve_flags(state.game_id, resolutions)
        if scored:
            logger.info("Scored %d flag claim(s)", scored)
    except Exception:
        logger.exception("Failed to resolve flags")


def _confirm_first_flag(state: game.GameState, claimed: dict) -> None:
    """Answer the first person to flag anything on this board.

    Flags on the board teach the syntax, but somebody has to go first. A
    public confirmation shows the rest of the thread that it worked.
    """
    earliest, coords = None, []
    for coord, repliers in claimed.items():
        for reply in repliers:
            if earliest is None or (reply.created_at or "") < (earliest.created_at or ""):
                earliest = reply
    if earliest is None or not earliest.uri:
        return
    coords = sorted(c for c, rs in claimed.items()
                    if any(r.did == earliest.did for r in rs))
    try:
        uri = bluesky.post_reply(
            build_flag_reply(coords, len(db.flag_counts(state.game_id))),
            parent_uri=earliest.uri, parent_cid=earliest.cid,
            root_uri=earliest.root_uri, root_cid=earliest.root_cid,
            kind="flagack")
        _remember(uri, "flagack", state.game_id, state.turn_number)
    except Exception:
        logger.exception("Failed to confirm the first flag")


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

    # 1. Read the crowd. One reply can do both jobs: flagging costs no turn,
    #    so "C3 is a mine, so D4 is safe — D4" flags C3 and votes for D4.
    already_open = {game.index_to_coord(r, c) for (r, c) in state.revealed}
    replies = _fetch_replies(state.last_post_uri)
    vote = votes.tally(replies, already_open, state.rows, state.cols)

    first_flag_of_the_board = not db.flag_counts(state.game_id)
    claimed, withdrawn = votes.collect_flags(replies, already_open,
                                             state.rows, state.cols)
    _record_flags(state, claimed, withdrawn)

    if config.KNOCKOUT:
        _play_knockout_turn(state, replies, already_open, position, analysis,
                            first_flag_of_the_board, claimed)
        return

    # 2. Decide. The quorum exists to stop one stray vote carrying a turn when
    #    a real crowd has scattered — but it must never silence a small one.
    #    With no more voters than the quorum itself there is nothing to split,
    #    so whoever turned up decides. Applied strictly, a lone player would
    #    watch the bot play 100% of the turns, which is not a crowd game.
    if vote is not None and _crowd_decides(vote):
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
    revealed_before = set(state.revealed)
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
    # A move scores the cells it opened — a blank that cascades is worth
    # more than a single numbered cell, and a mine scores nothing.
    points = ((len(state.revealed) - len(revealed_before))
              * tier_multiplier(state))
    caller_did = vote.caller.did if (vote and source == "crowd" and vote.caller) else ""

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

    # Flags shown on the board are the crowd's claims and nothing more: a
    # right flag and a wrong one are drawn identically until the cell opens.
    flags = {c for c in db.flagged_coords(state.game_id, config.FLAG_QUORUM)
             if not state.coord_is_revealed(c)}

    finished = state.status != game.ACTIVE
    record = db.get_record()
    if finished:
        # The history row is deliberately written only after the post
        # succeeds, so include this pending result in the public all-time stat.
        record = dict(record)
        record[state.status] += 1
        record["played"] += 1
        # This turn's move is not in the table yet — it is recorded after the
        # post goes out — so count it here or the crowd loses credit for the
        # move that ended the board.
        crowd_moves = sum(1 for m in db.get_moves(state.game_id, limit=500)
                          if m["source"] == "crowd") + (source == "crowd")
        # Score every outstanding claim now that the layout is known — this
        # is the moment a flag stops being an opinion.
        _resolve_flags(state, coord, result, revealed_before, final=True)
        flag_line = build_flag_line(db.flag_scores(state.game_id))
        next_board_at = datetime.now().astimezone() + timedelta(
            seconds=config.RESTART_DELAY_SECONDS)
        leaders = leaders_with_pending_move(
            db.leaderboard(state.game_id), caller_did, state.last_caller, points)
        text = build_gameover_text(state, coord, vote, source,
                                   safe_alternative if result == game.MINE else "",
                                   record, crowd_moves, flag_line,
                                   build_mvp_line(leaders),
                                   next_board_at)
        kind = "gameover"
    else:
        text = build_turn_text(state, coord, vote, source, flags)
        kind = "turn"

    try:
        uri = _post_board(state, text, kind, extra_dids=extra, flags=flags)
    except Exception:
        logger.exception("Posting failed; turn not saved and will be replayed")
        return
    state.last_post_uri = uri

    # Recorded before the credit reply below so this turn's points are
    # already in the totals that reply quotes. It stays after the board
    # post, so a failed post still replays the turn cleanly.
    try:
        db.record_move(state, coord, result, source,
                       caller=state.last_caller, votes=state.last_votes,
                       voters=state.last_voters,
                       was_provably_safe=was_provably_safe,
                       did=caller_did, points=points)
        move_recorded = True
    except Exception:
        logger.exception("Failed to record move")
        move_recorded = False

    # 5. Credit the follower whose call was played. Non-essential: a failure
    #    must not roll back a turn that has already posted.
    if source == "crowd" and vote and vote.caller and vote.caller.uri:
        try:
            reply_uri = bluesky.post_reply(
                build_credit_reply(
                    state, coord, vote, points,
                    db.player_points(caller_did, state.game_id),
                    db.player_points(caller_did)),
                parent_uri=vote.caller.uri, parent_cid=vote.caller.cid,
                root_uri=vote.caller.root_uri, root_cid=vote.caller.root_cid)
            _remember(reply_uri, "credit", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to post credit reply")

    # 6. Score the flags this turn settled, then persist. Resolution happens
    #    after the post so a failed turn replays cleanly instead of scoring
    #    people against a move that never reached anybody.
    if not finished:
        _resolve_flags(state, coord, result, revealed_before)

    if first_flag_of_the_board and claimed:
        _confirm_first_flag(state, claimed)

    if not move_recorded:
        try:
            db.record_move(state, coord, result, source,
                           caller=state.last_caller, votes=state.last_votes,
                           voters=state.last_voters,
                           was_provably_safe=was_provably_safe,
                           did=caller_did, points=points)
        except Exception:
            logger.exception("Failed to record move on retry")
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


def _play_knockout_turn(state: game.GameState, replies: list,
                        already_open: set, position, analysis,
                        first_flag_of_the_board: bool, claimed: dict) -> None:
    """One knockout turn: everybody eligible opens a cell of their own.

    Same ordering discipline as the plurality path — the move is applied in
    memory, the post goes out, and only then is anything saved, so a network
    blip replays the turn instead of losing it.
    """
    revealed_before = set(state.revealed)
    entrants_before = db.player_count(state.game_id)
    plays, source = apply_knockout_moves(state, replies, already_open,
                                         position, analysis)

    last = plays[-1]
    state.last_coord = last.coord
    state.last_result = last.result
    state.last_source = source
    state.last_caller = last.handle
    state.last_votes = 0
    state.last_voters = sum(1 for p in plays if p.did)

    flags = {c for c in db.flagged_coords(state.game_id, config.FLAG_QUORUM)
             if not state.coord_is_revealed(c)}
    finished = state.status != game.ACTIVE
    record = db.get_record()

    extra = {p.handle: p.did for p in plays if p.did and p.handle}
    if finished:
        record = dict(record)
        record[state.status] += 1
        record["played"] += 1
        _resolve_flags(state, last.coord, last.result, revealed_before,
                       final=True)
        eliminated = db.eliminated(state.game_id)
        entrants = max(entrants_before, len(eliminated),
                       db.player_count(state.game_id))
        leaders = leaders_with_pending_move(
            db.leaderboard(state.game_id), "", "", 0)
        for play in plays:
            leaders = leaders_with_pending_move(leaders, play.did, play.handle,
                                                play.points)
        text = build_knockout_gameover_text(
            state, record, max(0, entrants - len(eliminated)), entrants,
            build_mvp_line(leaders),
            datetime.now().astimezone()
            + timedelta(seconds=config.RESTART_DELAY_SECONDS))
        kind = "gameover"
    else:
        text = build_knockout_turn_text(state, plays, flags)
        kind = "turn"

    try:
        uri = _post_board(state, text, kind, extra_dids=extra, flags=flags)
    except Exception:
        logger.exception("Posting failed; turn not saved and will be replayed")
        return
    state.last_post_uri = uri

    for play in plays:
        try:
            db.record_move(state, play.coord, play.result, source,
                           caller=play.handle, did=play.did,
                           points=play.points,
                           was_provably_safe=(
                               game.coord_to_index(play.coord) in analysis.safe))
        except Exception:
            logger.exception("Failed to record move %s", play.coord)

    if not finished:
        _resolve_flags(state, last.coord, last.result, revealed_before)
    if first_flag_of_the_board and claimed:
        _confirm_first_flag(state, claimed)

    db.save_state(state)

    if finished:
        db.record_finished(state)
        logger.info("Board %d %s on turn %d (%d of %d cells, %d detonations)",
                    state.game_id, state.status, state.turn_number,
                    len(state.revealed), state.total_safe, state.detonations)
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
    rows, cols, mines = next_board_settings(previous)
    state = game.new_game(game_id, rows=rows, cols=cols, mines=mines,
                          mine_budget=mine_budget_for(next_tier(previous)))
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

# Restarting the process must not push the next turn back by a whole interval.
# A deploy happens whenever anybody pushes, and APScheduler counts an interval
# job from when the scheduler starts — so without this, three deploys in an
# afternoon quietly walk the turn time later and later.
OVERDUE_GRACE = timedelta(seconds=30)


def next_turn_time(last: datetime | None, now: datetime,
                   interval_minutes: int) -> datetime:
    """When the first turn after a restart should land.

    Anchored to the last turn that actually posted, not to process start. If
    the bot was down past the deadline the turn is taken shortly after boot
    rather than instantly, so a crash loop cannot burn through a board.
    """
    interval = timedelta(minutes=interval_minutes)
    if last is None:
        return now + interval
    scheduled = last + interval
    return scheduled if scheduled > now else now + OVERDUE_GRACE


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
    mode = (f"knockout, elimination {config.ELIMINATION_TURNS} turns"
            if config.KNOCKOUT else f"voting, quorum {config.QUORUM}")
    tiers = ("tiers " + " ".join(f"{r}x{c}/{m}" for r, c, m in config.TIERS)
             if config.ADAPTIVE_BOARD else
             f"fixed {config.ROWS}x{config.COLS}/{config.MINES}")
    logger.info("Minesweeper bot starting (%s, %d min turns, %s, posting %s)",
                tiers, config.TURN_MINUTES, mode, config.POST_MODE)

    db.init_db()
    bluesky.login_with_retry()

    if args.play:
        play_turns(args.play)
        return

    state = db.load_state()
    if state is None or state.status != game.ACTIVE:
        start_new_game()

    first = next_turn_time(db.last_turn_at(), datetime.now(timezone.utc),
                           config.TURN_MINUTES)
    scheduler.add_job(game_tick, "interval", seconds=config.TURN_MINUTES * 60,
                      start_date=first.astimezone(), id="game_tick",
                      coalesce=True, max_instances=1,
                      misfire_grace_time=config.TURN_MINUTES * 60)
    logger.info("Scheduler started; one turn every %d minutes, next at %s",
                config.TURN_MINUTES, first.astimezone().strftime("%H:%M:%S %Z"))
    scheduler.start()


if __name__ == "__main__":
    main()
