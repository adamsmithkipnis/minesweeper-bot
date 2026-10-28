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
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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

_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def eligible_hashtags(when: datetime | None = None) -> list:
    """The pool minus the tags that belong to another day of the week.

    #screenshotsaturday is the most-used tag among the people who play this
    game and the easiest one to get wrong: on a Tuesday it is worse than no
    tag at all.
    """
    today = _DAY_NAMES[(when or datetime.now().astimezone()).weekday()]
    always = config.HASHTAG_ALWAYS.lower()
    return [tag for tag in config.HASHTAG_POOL
            if tag.lower() != always
            and config.HASHTAG_DAY.get(tag.lower(), today) == today]


def pick_hashtags(rng=None, when: datetime | None = None) -> list:
    """The always-on tag, then a fresh weighted sample of the pool.

    Posting the identical block of six tags every hour reads as a bot padding
    for reach; drawing them fresh each time reaches more corners of the
    network and looks like a person choosing. The game's own tag is never
    dropped, so the account stays findable under one stable name.

    The sample is **weighted**, because a wider pool is not automatically
    more reach. Of the 63 people who have played, 18 use #gamedev and 12 use
    #indiedev, while most of the pool is used by one or two; drawing five
    tags uniformly from 232 would put #gamedev on one post in 46 instead of
    one in five. Weighting keeps the proven tags frequent and spends the rest
    of each post on the tail, so variety costs nothing in reach.
    """
    rng = rng or random
    always = config.HASHTAG_ALWAYS
    pool = eligible_hashtags(when)
    weights = [max(1, config.HASHTAG_WEIGHT.get(tag.lower(), 1))
               for tag in pool]

    picked = []
    for _ in range(min(config.HASHTAG_COUNT, len(pool))):
        total = sum(weights)
        cut = rng.random() * total
        index = 0
        for index, weight in enumerate(weights):
            cut -= weight
            if cut <= 0:
                break
        picked.append(pool.pop(index))
        weights.pop(index)      # no replacement: a tag never repeats in a post
    return ([always] if always else []) + picked


def _with_tags(text: str, tags: list | None = None) -> str:
    """Append hashtags, skipping any that will not fit.

    Content wins over reach: the board information is built first and a tag is
    only added while the whole post still clears the limit, so discovery tags
    can never be the reason a post gets clamped.

    A tag that does not fit is skipped rather than ending the run. The pool
    used to be 23 tags of roughly even length, so stopping at the first miss
    cost nothing; across 233 the lengths spread from #dos to
    #collectiveintelligence, and one long draw was taking the short tags
    behind it down with it — about 12% of the tag slots on a tight post.
    """
    out, first = text, True
    for tag in (pick_hashtags() if tags is None else tags):
        candidate = f"{out}{chr(10) if first else ' '}{tag}"
        if len(candidate) > bluesky.POST_LIMIT:
            continue
        out, first = candidate, False
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _outcome_phrase(state: game.GameState, coord: str,
                    result: str | None = None) -> str:
    """"was safe, touching 2 mines" / "hit a mine".

    `result` is the outcome of *this* cell, and every caller that knows it
    must pass it. Several players move in one knockout turn, so
    `state.last_result` belongs to whoever moved last — falling back to it
    told two players who had opened safe cells that they had hit a mine,
    in the same reply that awarded them points, because a third player was
    knocked out later in the same turn.
    """
    if (result or state.last_result) == game.MINE:
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


def _explained(why: str) -> str:
    return f"🧠 {why}"


def build_turn_text(state: game.GameState, coord: str, vote, source: str,
                    flags=(), why: str = "") -> str:
    """The board post for a voting turn. `why` is the bot's deduction when it
    played the cell itself; it is shown only while the prompt still fits."""
    ask = _ask_line(state, flags, _should_teach_flagging(state, flags))
    lines = [f"Turn {state.turn_number} · {coord} {_outcome_phrase(state, coord)}",
             _credit_line(vote, source)]
    if source == "bot" and why:
        with_why = lines + [_explained(why)]
        if len("\n".join(with_why) + f"\n\n{ask}") <= bluesky.POST_LIMIT:
            lines = with_why
    return _with_tags("\n".join(lines) + f"\n\n{ask}")


def build_opening_text(state: game.GameState, record: dict) -> str:
    """The new-board post, budgeted so it can never be clamped.

    Knockout's copy is longer than voting's, and a tier-3 banner is longer
    than a tier-1 one, so the fixed layout that fitted in one mode at one tier
    overflowed in another. The parts a reader cannot play without are built
    first; the rest is added only while the whole post still fits.
    """
    tier = tier_of(state)
    if is_massive(state):
        banner = (f"💣💣 {massive_name().upper()} · "
                  f"{state.rows}x{state.cols}, {state.mine_count} mines.")
    else:
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
    if is_massive(state):
        # Second in line, ahead of the record: it is what makes today's board
        # a reason to turn up, and why the usual boards have stopped.
        optional.insert(0, "The weekly big board. It grows every week this "
                           "month; regular boards resume when it's done.")
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
        f"Flag mines: flag C3 · undo: unflag C3\n"
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
        flag_line,
        f"You called {crowd_moves} of {_plural(state.turn_number, 'move')}."
        if crowd_moves else "",
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


def build_check_reply(coords: list) -> str:
    """Explain a check mark, once, to each player who first places one.

    The phrase "check D4" used to read as a vote to open D4, so this is also
    where somebody who meant that finds out how to open it instead.
    """
    which = coords[0] if len(coords) == 1 else ", ".join(coords[:3])
    first = coords[0]
    return _with_tags(
        f"❓ Marked {which} to check — a note to everyone that it might be a "
        f"mine. It shows as a ? on the board, costs no turn and scores nothing.\n"
        f"Sure it's a mine? 'flag {first}' bets on it. Meant to open it? "
        f"Reply just {first}. 'uncheck {first}' clears the mark.",
        tags=[config.REPLY_HASHTAG])


def build_flag_reply(coords: list, total_flagged: int) -> str:
    """Confirm a flag publicly.

    This is what breaks the chicken and egg: flags on the board teach the
    syntax, but somebody has to flag first. Answering the first person to try
    it shows everyone else in the thread that it worked.
    """
    which = coords[0] if len(coords) == 1 else ", ".join(coords[:3])
    also = ("open a cell somewhere else" if config.KNOCKOUT
            else "vote to open somewhere else")
    return _with_tags(
        f"🚩 Flagged {which}. It shows on the board and costs no turn — "
        f"you can flag and still {also} in the same reply.\n"
        f"A flag is a bet: +1 if it's a mine, -1 if not, settled when the "
        f"cell opens or the board ends. 'unflag {coords[0]}' takes it back.\n\n"
        f"{_plural(total_flagged, 'cell')} flagged so far.",
        tags=[config.REPLY_HASHTAG])


def build_unflag_reply(cells: dict, total_flagged: int,
                       late: int = 0) -> str:
    """Acknowledge an unflag, and be honest when the flag stays up.

    Anyone may clear any flag, so a cell only stays up if somebody flagged it
    again after the unflag. `cells` maps each coordinate to how many people
    have it flagged now. `late` is how many of this player's own bets were
    taken back after the turn they were placed, which costs the same as
    being wrong — said plainly, since it is the one part that costs points.
    """
    shown = sorted(cells)[:3]
    cleared = [c for c in shown if not cells[c]]
    kept = [c for c in shown if cells[c]]
    parts = []
    if cleared:
        parts.append(f"{', '.join(cleared)} "
                     f"{'is' if len(cleared) == 1 else 'are'} off the board")
    for coord in kept:
        parts.append(f"{coord} is flagged again — "
                     f"{_plural(cells[coord], 'player')} flagged it after")
    cost = (f" Taking back a flag after the turn you placed it costs 1 point "
            f"({-late} this time)." if late else "")
    return _with_tags(
        f"🏳️ Unflagged. {'; '.join(parts)}.{cost}\n\n"
        f"{_plural(total_flagged, 'cell')} flagged now.",
        tags=[config.REPLY_HASHTAG])


def build_flag_cap_reply(placed: list, waiting: list) -> str:
    """Say which flags went on and which must wait for next turn."""
    kept = (f"{', '.join(placed[:3])} "
            f"{'is' if len(placed) == 1 else 'are'} flagged. " if placed else "")
    return _with_tags(
        f"⏳ {kept}{', '.join(waiting[:3])} will have to wait: "
        f"{_plural(config.FLAG_CAP, 'new flag')} a turn each, so say "
        f"{'it' if len(waiting) == 1 else 'them'} again on the next board post.",
        tags=[config.REPLY_HASHTAG])


def build_ignored_flag_reply(cells: dict) -> str:
    """Say why a flag change did nothing. `cells` maps coord -> wanted a flag."""
    parts = [f"{coord} was already {'unflagged' if wanted else 'flagged'} "
             f"by someone else this turn"
             for coord, wanted in sorted(cells.items())[:3]]
    return _with_tags(
        f"⏳ {'; '.join(parts)}. A cell's flag changes once per turn, so "
        f"ask again next turn.",
        tags=[config.REPLY_HASHTAG])


def build_knockout_credit_reply(state: game.GameState, play,
                                this_board: int, all_time: int,
                                first_move: bool = False) -> str:
    """Tell one player what their move did and what they are on.

    Every player acts every turn in knockout, so this is the only place most
    of them see a score at all — the board post has room for a name and a
    number, not a running total.
    """
    if play.result == game.MINE:
        head = (f"💥 You opened {play.coord} on turn {state.turn_number} — "
                f"a mine. You're out for "
                f"{_plural(config.ELIMINATION_TURNS, 'turn')}, then you're "
                f"back. You can still flag & check cells while you wait.")
        scored = ""
    else:
        head = (f"🎯 You opened {play.coord} on turn {state.turn_number} — "
                f"it {_outcome_phrase(state, play.coord, play.result)}.")
        scored = f"\n+{_plural(play.points, 'point')}"
    welcome = f"\n\n{build_welcome_line()}" if first_move else ""
    return _with_tags(
        f"{head}\n{scored}\n"
        f"This board: {this_board}\n"
        f"All time: {all_time}"
        f"{welcome}",
        tags=[config.REPLY_HASHTAG])


# A move whose cell an earlier move's cascade opened first was still the
# right call, so it scores a point even though it opened nothing itself.
# Players named it the Pity Point, so the reply does too.
CASCADE_CREDIT = 1

# Each must say the cell was valid, that a cascade opened it, and what it
# scored; {coord} and {points} are filled in.
PITY_POINT_LINES = (
    "🫶 Pity Point! {coord} was valid, but an earlier move's cascade opened "
    "it before yours was applied. +{points} for the right call.",
    "🌊 {coord} was valid, but an earlier move's cascade opened it before "
    "yours landed. Have a Pity Point: +{points} for the right call.",
    "🫠 So close. {coord} was valid, but an earlier move's cascade opened it "
    "first. Please accept this Pity Point with our condolences: +{points}.",
    "🏅 Introducing the Pity Point: {coord} was valid, but an earlier move's "
    "cascade opened it first. +{points}, and the board owes you one.",
)


def build_cascade_skip_reply(state: game.GameState, move) -> str:
    """Explain why a valid move earned one point after an earlier cascade."""
    if state.status == game.ACTIVE:
        next_step = (f"Turn {state.turn_number} is now open — reply to the "
                     f"newest board post to play.")
    else:
        next_step = "That move finished the board; the next board begins soon."
    # Seeded by the move, so a retried post says the same thing.
    line = random.Random(
        f"{state.game_id}:{state.turn_number}:{move.coord}:{move.did}"
    ).choice(PITY_POINT_LINES)
    return _with_tags(
        line.format(coord=move.coord,
                    points=_plural(CASCADE_CREDIT, 'point'))
        + f"\n\n{next_step}",
        tags=[config.REPLY_HASHTAG])


def build_duplicate_skip_reply(state: game.GameState, move) -> str:
    """Explain why an earlier caller of the same cell got there first."""
    if state.status == game.ACTIVE:
        next_step = (f"Turn {state.turn_number} is now open — reply to the "
                     f"newest board post to play.")
    else:
        next_step = "That move finished the board; the next board begins soon."
    return _with_tags(
        f"⏭️ {move.coord} was valid, but another player opened it first this "
        f"turn, so it earned 0 points.\n\n{next_step}",
        tags=[config.REPLY_HASHTAG])


def build_open_cell_reply(coord: str) -> str:
    """Say why a vote for an already-open cell was not counted."""
    return _with_tags(
        f"🧭 {coord} is already open, so that vote can't count. Pick a "
        f"hidden cell (a ? on the board) and reply again on the newest "
        f"board post.",
        tags=[config.REPLY_HASHTAG])


def build_stale_post_reply() -> str:
    """Say that replies to an earlier turn's post are not read."""
    return _with_tags(
        "⏰ This is an earlier turn's post, and I only read replies to the "
        "newest board post, so this didn't count. Reply there to play.",
        tags=[config.REPLY_HASHTAG])


def build_welcome_line() -> str:
    """The extra line a player's first-ever move reply carries.

    A first move is the moment somebody has shown they want to play, and
    the second day is where most of them are lost, so the line names the
    reason to come back rather than just saying hello.
    """
    ladder = config.DAY_MILESTONES
    if not ladder:
        return "👋 Welcome — that was your first move! Glad you're here."
    if ladder[0] <= 2:
        invite = "Come back another day to earn your first badge."
    else:
        invite = f"Play on {ladder[0]} different days to earn your first badge."
    return f"👋 Welcome — that was your first move! {invite}"


def _first_move(state: game.GameState, did: str) -> bool:
    """Best-effort: a failed lookup costs the welcome, never the reply."""
    try:
        return not db.moved_before(did, state.game_id, state.turn_number)
    except Exception:
        logger.exception("Failed to check for a first move")
        return False


def build_credit_reply(state: game.GameState, coord: str, vote,
                       points: int = 0, this_game: int = 0,
                       all_time: int = 0, first_move: bool = False) -> str:
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
    welcome = f"\n\n{build_welcome_line()}" if first_move else ""
    return _with_tags(
        f"🎯 Your call. We opened {coord} on turn {state.turn_number} "
        f"{outcome}\n\n"
        f"{scored}"
        f"This game: {this_game}\n"
        f"All time: {all_time}"
        f"{welcome}",
        tags=[config.REPLY_HASHTAG])


def build_flag_line(scores: list) -> str:
    """Who read the mines best on this board, as flag points.

    Per board rather than all time, and floored at zero in the database:
    recognition without a losing record anybody has to shake.
    """
    scored = [s for s in scores if s["points"] > 0][:2]
    if not scored:
        return ""
    return "🚩 Flags: " + " · ".join(
        f"@{s['handle']} +{s['points']}" for s in scored)


def build_mvp_line(leaders: list) -> str:
    """Compact board-end recognition in the main feed."""
    if not leaders:
        return ""
    best = leaders[0]
    return (f"🏅 Board MVP: @{best['handle']} — "
            f"{_plural(best['points'], 'point')}.")


# ---------------------------------------------------------------------------
# Milestones and the weekly table
# ---------------------------------------------------------------------------

def _short(handle: str) -> str:
    """The name people actually call each other by: the first label.

    A mention facet carries the DID, not the text, so "@coil" notifies the
    owner of coil.bsky.social exactly as the full handle would — and six full
    handles do not fit in 300 characters while six short ones do. Callers
    must pass both forms in `extra_dids` so either resolves.
    """
    return handle.split(".", 1)[0]


def _names(rows: list) -> tuple:
    """(display name per row, {name: did}) with collisions left long.

    Two *different* players whose handles share a first label would both read
    as the same @name, so in that case everybody in the post keeps their full
    handle rather than one of them being quietly mislabelled. The same player
    appearing twice — in both tables of the standings post — is not a
    collision, which is why this counts accounts and not rows.
    """
    shorts = [_short(row["handle"] or "") for row in rows]
    owners = {}
    for row, short in zip(rows, shorts):
        owners.setdefault(short, set()).add(row.get("did") or row["handle"])
    unique = all(len(dids) == 1 for dids in owners.values())
    display, dids = [], {}
    for row, short in zip(rows, shorts):
        handle = row["handle"] or ""
        name = short if unique and short else handle
        display.append(name)
        if row.get("did"):
            dids[handle] = row["did"]
            dids[short] = row["did"]
    return display, dids


def _comma(number: int) -> str:
    return f"{number:,}"


def build_milestone_text(crossings: list) -> str:
    """The post for one or more players passing a point milestone.

    One post per turn however many people crossed, because two celebrations
    in a row read as a malfunction. The rung is the headline and the running
    total is the proof, since the two differ: you pass 1,000 by landing on
    1,047, and saying "1,000" alone would look rounded.
    """
    if not crossings:
        return ""
    display, dids = _names(crossings)
    if len(crossings) == 1 and crossings[0].get("kind") == "days":
        first = crossings[0]
        lines = [f"📅 BADGE — @{display[0]} has now played on "
                 f"{first['threshold']} different days."]
        if first["threshold"] == (config.DAY_MILESTONES or [0])[0]:
            lines.append("First badge earned. Welcome back!")
        if first.get("next"):
            lines.append(f"Next badge: {first['next']} days.")
    elif len(crossings) == 1:
        first = crossings[0]
        head = (f"🏆 MILESTONE — @{display[0]} just passed "
                f"{_comma(first['threshold'])} points.")
        lines = [head, f"{_comma(first['points'])} cells opened all time."]
        if first.get("rank") == 1:
            lines[-1] = (f"{_comma(first['points'])} cells opened all time, "
                         f"top of the board.")
        if first.get("next"):
            lines.append(f"Next rung: {_comma(first['next'])}.")
    else:
        lines = ["🏆 MILESTONES"]
        for name, crossing in zip(display, crossings):
            if crossing.get("kind") == "days":
                lines.append(f"@{name} played on {crossing['threshold']} "
                             f"different days 📅")
                continue
            lines.append(f"@{name} passed {_comma(crossing['threshold'])} "
                         f"({_comma(crossing['points'])} all time)")
    # Drop from the bottom — the rung and the name are the post; the rest is
    # colour, and the tags are added only with whatever room is left.
    while len(lines) > 1 and len("\n".join(lines)) > bluesky.POST_LIMIT - 20:
        lines.pop()
    return _with_tags("\n".join(lines)), dids


def milestone_crossings(did: str, handle: str, total: int, earned: int,
                        game_id: int | None = None) -> list:
    """Claim and describe every rung this player has newly passed.

    `total` is their all-time points after this turn and `earned` is what
    this turn added, so the total before it is known without having to have
    stored it. The rungs are claimed here and posted by the caller: the claim
    is what stops the same congratulation going out twice, and the caller
    releases it if the post fails.
    """
    if not did or not config.MILESTONES:
        return []
    ladder = list(config.MILESTONES)
    # First sight of this player: everything already behind them is marked
    # silently, measured before this turn so a first move that cascades past
    # a rung is still celebrated.
    db.backfill_milestones(did, max(0, total - earned), ladder)
    thresholds = db.claim_milestones(did, total, ladder, game_id)
    if not thresholds:
        return []
    leaders = db.leaderboard(limit=1)
    rank = 1 if leaders and leaders[0]["did"] == did else 0
    crossings = []
    for threshold in thresholds:
        later = [t for t in ladder if t > threshold]
        crossings.append({"did": did, "handle": handle,
                          "threshold": threshold, "points": total,
                          "rank": rank, "next": later[0] if later else 0})
    # Several rungs at once means a backfilled player we are meeting for the
    # first time mid-cascade; announce the highest, which is the true news.
    return crossings[-1:]


def day_crossings(did: str, handle: str, game_id: int | None = None,
                  now: datetime | None = None) -> list:
    """Claim and describe any days-played rung this player has just reached.

    The same claim-then-post contract as `milestone_crossings`. The silent
    backfill is measured on the days *before* today, so switching this on
    does not badge a regular for their seventh day weeks after the fact,
    while somebody whose second day is today is still congratulated.
    """
    if not did or not config.DAY_MILESTONES:
        return []
    ladder = list(config.DAY_MILESTONES)
    tz = _leaderboard_tz()
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    days = db.play_days(did, tz)
    db.backfill_milestones(did, len({d for d in days if d < today}), ladder,
                           kind="days")
    thresholds = db.claim_milestones(did, len(days), ladder, game_id,
                                     kind="days")
    if not thresholds:
        return []
    threshold = thresholds[-1]
    later = [t for t in ladder if t > threshold]
    return [{"did": did, "handle": handle, "kind": "days",
             "threshold": threshold, "points": len(days),
             "next": later[0] if later else 0}]


def build_leaderboard_text(all_time: list, weekly: list, week_start,
                           week_end, flaggers: list = ()) -> str:
    """The Saturday standings post.

    Two tables in 300 characters means the names have to be short and the
    numbers have to be bare. The weekly table goes first: the all-time one
    barely moves week to week, and a table nobody can enter is not a reason
    to come back.
    """
    flaggers = [f for f in flaggers if f["points"] > 0][:3]
    rows = list(weekly) + list(all_time) + flaggers
    display, dids = _names(rows)
    weekly_names = display[:len(weekly)]
    all_time_names = display[len(weekly):len(weekly) + len(all_time)]
    flag_names = display[len(weekly) + len(all_time):]

    span = f"{week_start:%b %-d}–{week_end:%b %-d}"
    head = f"🏆 WEEKLY STANDINGS · {span}"

    def table(names, entries):
        return " · ".join(f"@{name} {_comma(row['points'])}"
                          for name, row in zip(names, entries))

    lines = [head]
    if weekly:
        lines.append("This week: " + table(weekly_names, weekly))
    else:
        lines.append("Nobody scored this week — the board is open.")
    if all_time:
        lines.append("All time: " + table(all_time_names, all_time))

    # Flag points are a separate table on purpose: they are a different
    # skill from opening cells, and folding them into one number would let
    # the bigger of the two drown the other. Shown only while it fits, and
    # trimmed first, since it is the newest and the least load-bearing.
    if flaggers:
        lines.append("🚩 Flags: " + table(flag_names, flaggers))

    # A scoreboard is a reason to look; an invitation is a reason to play.
    # Added before the tags and after the tables, so it takes the room a
    # sixth hashtag would have had and loses to a name if it comes to that.
    invite = "Reply a coordinate on the live board to make next week's table."

    # Trim the tables from the bottom row up rather than dropping a whole
    # table, so a crowded week still shows a podium of some size.
    while len("\n".join(lines)) > bluesky.POST_LIMIT - 20:
        if flaggers:
            flaggers = []
            lines = [line for line in lines if not line.startswith("🚩")]
        elif len(all_time_names) > 1:
            all_time_names, all_time = all_time_names[:-1], all_time[:-1]
            lines[-1] = "All time: " + table(all_time_names, all_time)
        elif len(weekly_names) > 1:
            weekly_names, weekly = weekly_names[:-1], weekly[:-1]
            lines[1] = "This week: " + table(weekly_names, weekly)
        elif len(lines) > 2:
            lines.pop()
        else:
            break
    if len("\n".join(lines + [invite])) <= bluesky.POST_LIMIT - 13:
        lines.append(invite)
    return _with_tags("\n".join(lines)), dids


def _mention_list(names: list, room: int) -> str:
    """'@a @b @c +2 more', as many names as fit in `room` characters."""
    for shown in range(len(names), 0, -1):
        rest = len(names) - shown
        text = " ".join(f"@{n}" for n in names[:shown])
        if rest:
            text += f" +{rest} more"
        if len(text) <= room:
            return text
    return f"+{len(names)} more" if names else ""


def build_arrivals_text(newcomers: list, returners: list) -> tuple:
    """The reply under the standings that names the week's new faces.

    The table names the same three people most weeks, and a table nobody
    new can enter is not a reason to come back. This names everybody who
    played for the first time and everybody who came back after a gap, which
    is what moves the monthly count. ("", {}) when there is nobody to name.
    """
    if not newcomers and not returners:
        return "", {}
    display, dids = _names(list(newcomers) + list(returners))
    new_names, back_names = display[:len(newcomers)], display[len(newcomers):]
    outro = "Glad you're here. Reply a coordinate on the live board to play."
    labels = [("🌱 New this week: ", new_names),
              ("👋 Welcome back: ", back_names)]
    labels = [(label, names) for label, names in labels if names]
    # Split the room between the lines, then let the first line keep
    # whatever the second did not need.
    budget = bluesky.POST_LIMIT - len(outro) - 2 - len(config.REPLY_HASHTAG) - 1
    budget -= sum(len(label) + 1 for label, _ in labels)
    lines = []
    for index, (label, names) in enumerate(labels):
        share = budget // (len(labels) - index)
        listed = _mention_list(names, share)
        budget -= len(listed)
        lines.append(label + listed)
    return _with_tags("\n".join(lines) + f"\n\n{outro}",
                      tags=[config.REPLY_HASHTAG]), dids


def build_board_thanks(players: list) -> tuple:
    """The reply under a cleared board that names everyone who played it.

    The game-over post has room for one MVP; this is the rest of the crowd.
    A mention notifies each of them, which is a reason to look and share.
    """
    if not players:
        return "", {}
    display, dids = _names(players)
    head = (f"🙌 Swept by {_plural(len(players), 'player')}. "
            f"Thank you, everyone:")
    room = bluesky.POST_LIMIT - len(head) - 1 - len(config.REPLY_HASHTAG) - 1
    return _with_tags(f"{head}\n{_mention_list(display, room)}",
                      tags=[config.REPLY_HASHTAG]), dids


def _reply_to_own_post(uri: str, text: str, kind: str, dids: dict,
                       game_id: int | None) -> None:
    """Thread a follow-up under a post this process just made. Best-effort."""
    cid = bluesky.cid_of(uri)
    if not text or not cid:
        return
    try:
        reply = bluesky.post_reply(text, parent_uri=uri, parent_cid=cid,
                                   kind=kind, extra_dids=dids)
        _remember(reply, kind, game_id)
    except Exception:
        logger.exception("Failed to post the %s reply", kind)


def _thank_the_board(state: game.GameState, uri: str) -> None:
    """Name everyone who played a cleared board, under its game-over post."""
    if state.status != game.CLEARED:
        return
    try:
        text, dids = build_board_thanks(db.board_players(state.game_id))
    except Exception:
        logger.exception("Failed to gather the board's players")
        return
    _reply_to_own_post(uri, text, "thanks", dids, state.game_id)


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
    # The big board ranks with the top rung, being longer than any of them;
    # what it pays is set separately, in tier_multiplier.
    if is_massive(state):
        return len(config.TIERS) - 1
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
    note: str = ""          # why the bot chose this, when the bot chose it
    why: str = ""           # the deduction behind it, for the reader
    reply_uri: str = ""     # the reply that asked for it, so we can answer it
    reply_cid: str = ""
    root_uri: str = ""
    root_cid: str = ""


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

    The big board pays 1x. It is less dense than the 11x11 (13-16% against
    20%), so each cell is safer, not riskier, and the volume is already the
    reward: at 3x a 22x22 would hand out about 1,200 points in a day, ten
    regular boards' worth, and swamp the weekly table and the milestones.
    """
    if is_massive(state):
        return 1
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
    harder when the crowd is actually driving it. Demotion needs a loss *and*
    poor participation: losing a hard board is the game working, but a board
    the bot had to play itself is one nobody is playing.

    A sweep never demotes. The bot fills every turn nobody takes, so on a
    quiet board it plays most of them — the first Tier 2 board was swept in
    46 turns at 42% participation and the next board opened at Tier 1, which
    sent the players who cleared it back down for clearing it.
    """
    if previous is None or not config.ADAPTIVE_BOARD:
        return 0
    # The big board is an interlude, not a rung: the ladder resumes at the
    # tier it would have used had the big board not happened.
    if is_massive(previous):
        try:
            resume = int(db.get_setting(_MASSIVE_RESUME, "0"))
        except (TypeError, ValueError):
            resume = 0
        return max(0, min(resume, len(config.TIERS) - 1))
    current = tier_of(previous)
    share = participation_of(previous, crowd_moves)
    if previous.status == game.CLEARED:
        if share >= config.GROW_PARTICIPATION:
            return min(current + 1, len(config.TIERS) - 1)
        return current
    if share < config.SHRINK_PARTICIPATION:
        return max(current - 1, 0)
    return current


_MASSIVE_DATE = "massive_last_date"
_MASSIVE_RESUME = "massive_resume_tier"
_DAY_NAMES_LONG = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                   "thu": "Thursday", "fri": "Friday", "sat": "Saturday",
                   "sun": "Sunday"}


def massive_name() -> str:
    """'Massive Monday' — named for whichever day it is configured on."""
    return f"Massive {_DAY_NAMES_LONG.get(config.MASSIVE_DAY, 'Monday')}"


def is_massive(state: game.GameState | None) -> bool:
    """Whether a board is the weekly big one, by its dimensions.

    Derived like `tier_of`, so nothing new has to be stored on the board. A
    size that is also a regular tier is never treated as the big board.
    """
    if state is None or not config.MASSIVE_BOARDS:
        return False
    shape = (state.rows, state.cols)
    return (shape in {(r, c) for r, c, _ in config.MASSIVE_BOARDS}
            and shape not in {(r, c) for r, c, _ in config.TIERS})


def massive_board_for(when: datetime) -> tuple | None:
    """(rows, cols, mines) if a board starting at `when` is the big one.

    Knockout only, on the configured day, and once per date: a big board
    that finishes the same afternoon is followed by a regular one. The size
    is the week of the month — first week the first size, fourth the last,
    and a fifth week repeats the last.
    """
    if (not config.KNOCKOUT or not config.MASSIVE_BOARDS
            or config.MASSIVE_DAY not in _DAYS):
        return None
    local = when.astimezone(_leaderboard_tz())
    if _DAYS[local.weekday()] != config.MASSIVE_DAY:
        return None
    # ISO dates compare correctly as text.
    if config.MASSIVE_START and local.date().isoformat() < config.MASSIVE_START:
        return None
    if db.get_setting(_MASSIVE_DATE) == local.date().isoformat():
        return None
    week = min((local.day - 1) // 7, len(config.MASSIVE_BOARDS) - 1)
    return config.MASSIVE_BOARDS[week]


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
                extra_dids: dict | None = None, flags=(), checks=()) -> str:
    image = renderer.render_board(state, highlight=state.last_coord,
                                  flags=flags, checks=checks)
    alt = renderer.build_alt_text(state, flags, checks)
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
    """Open one cell for every eligible player.

    Returns ``(plays, source, superseded, duplicates)``.  ``superseded``
    contains valid moves whose cells were opened by an earlier move's
    flood-fill during this same turn; each scores CASCADE_CREDIT, since the
    call was right. ``duplicates`` contains later callers of the exact cell
    another player opened first, which score nothing. Both kinds of caller
    deserve an answer explaining why their move disappeared.

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

    plays, superseded, duplicates = [], [], []
    cascade_opened = set()
    for index_in_turn, move in enumerate(pending):
        try:
            index = game.coord_to_index(move.coord)
        except ValueError:
            continue
        before_cells = set(state.revealed)
        position_before = solver.Position.from_state(state)
        outcome = game.reveal(state, *index)
        if outcome == game.SAFE and move.did:
            _record_assists(state, position_before, index)
        if outcome == game.ALREADY:
            if index in cascade_opened:
                superseded.append(move)
            else:
                duplicates.append(move)
            continue
        if outcome == game.MINE:
            try:
                db.eliminate(state.game_id, move.did, move.handle,
                             move.coord, state.turn_number)
            except Exception:
                logger.exception("Failed to record an elimination")
            plays.append(Play(move.did, move.handle, move.coord, outcome,
                              reply_uri=move.reply.uri,
                              reply_cid=move.reply.cid,
                              root_uri=move.reply.root_uri,
                              root_cid=move.reply.root_cid))
            logger.info("Turn %d: %s hit %s and is out (%d spare(s) left)",
                        state.turn_number, move.handle, move.coord,
                        state.spares_left)
        else:
            newly_opened = set(state.revealed) - before_cells
            cascade_opened.update(newly_opened - {index})
            plays.append(Play(move.did, move.handle, move.coord, outcome,
                              (len(state.revealed) - len(before_cells))
                              * tier_multiplier(state),
                              reply_uri=move.reply.uri,
                              reply_cid=move.reply.cid,
                              root_uri=move.reply.root_uri,
                              root_cid=move.reply.root_cid))
        if state.status != game.ACTIVE:
            # A clearing cascade can also consume moves we have not reached
            # yet.  Preserve those callers for the explanatory reply below.
            for later in pending[index_in_turn + 1:]:
                try:
                    later_index = game.coord_to_index(later.coord)
                except ValueError:
                    continue
                if later_index == index:
                    duplicates.append(later)
                elif later_index in cascade_opened:
                    superseded.append(later)
            break

    if plays:
        return plays, "crowd", superseded, duplicates

    # Nobody eligible moved, so the board would otherwise freeze.
    cell, reason = solver.safest_move(position, analysis)
    coord = game.index_to_coord(*cell)
    before = len(state.revealed)
    outcome = game.reveal(state, *cell)
    logger.info("Turn %d by bot (%s): %s", state.turn_number, reason, coord)
    return ([Play("", "", coord, outcome,
                  (len(state.revealed) - before) * tier_multiplier(state),
                  note=reason,
                  why=solver.explain(position, cell, analysis))],
            "bot", superseded, duplicates)


def build_knockout_turn_text(state: game.GameState, plays: list,
                             flags=()) -> str:
    """The board post for a knockout turn."""
    opened = sum(p.points for p in plays if p.result == game.SAFE)
    movers = sum(1 for p in plays if p.did)
    out = [p for p in plays if p.result == game.MINE]
    scorers = sorted((p for p in plays if p.result == game.SAFE and p.did),
                     key=lambda p: -p.points)

    if movers:
        head = f"Turn {state.turn_number} · {_plural(movers, 'player')} moved"
    else:
        # Say when the bot gambled. A turn where it took a real risk with the
        # board is the most interesting thing that can happen with nobody
        # watching, and reporting it as routine hides that.
        note = plays[0].note if plays else ""
        odds = re.search(r"(\d+)%", note)
        if odds:
            head = (f"Turn {state.turn_number} · nobody around and no safe "
                    f"cell. I gambled on {plays[0].coord} at {odds.group(1)}%.")
        else:
            head = (f"Turn {state.turn_number} · nobody moved, so I opened "
                    f"{plays[0].coord}." if plays else
                    f"Turn {state.turn_number} · nobody moved")
    lines = [head]
    # A quiet board is mostly the bot's own turns, so this post is what most
    # readers see. Show the deduction: it is the one thing that teaches the
    # game to somebody scrolling past. Added like the scorer line below —
    # only while the prompt still fits.
    if not movers and plays and plays[0].why:
        lines.append(_explained(plays[0].why))
    for play in out[:2]:
        lines.append(f"💥 @{play.handle} hit {play.coord} and is out. "
                     f"{_plural(state.spares_left, 'spare')} left.")

    # The prompt is fixed and must survive, so budget against its real length
    # rather than a guessed reserve — guessing let the scorer line pass its
    # own check and then be dropped whole by the trim below.
    ask = _knockout_ask_line(state, flags,
                             _should_teach_flagging(state, flags))
    room = bluesky.POST_LIMIT - len(ask) - 2
    if len("\n".join(lines)) > room:
        lines = [head]

    # Everyone who opened something, best first — not just the top scorer.
    # Names are added one at a time so a crowded turn keeps as many as fit and
    # counts the rest, instead of losing the line altogether.
    if scorers:
        shown, dropped = [], 0
        for play in scorers:
            entry = f"@{play.handle} +{play.points}"
            trial = lines + ["🏅 " + " · ".join(shown + [entry])]
            if len("\n".join(trial)) <= room:
                shown.append(entry)
            else:
                dropped += 1
        if shown:
            line = "🏅 " + " · ".join(shown)
            if dropped:
                line += f" +{dropped} more"
            if len("\n".join(lines + [line])) <= room:
                lines.append(line)

    body = "\n".join(lines)
    while len(f"{body}\n\n{ask}") > bluesky.POST_LIMIT and len(lines) > 1:
        lines.pop()
        body = "\n".join(lines)
    return _with_tags(f"{body}\n\n{ask}")


def _upcoming_massive(next_board_at) -> str:
    """'Massive Monday (14x14)' when the next board will be the big one.

    The post before the big board is the only warning anybody gets, so it
    says so. Best-effort: a failed lookup only loses the heads-up.
    """
    if not next_board_at:
        return ""
    try:
        board = massive_board_for(next_board_at)
    except Exception:
        logger.exception("Failed to check for an upcoming big board")
        return ""
    return f"{massive_name()} ({board[0]}x{board[1]})" if board else ""


def build_knockout_gameover_text(state: game.GameState, record: dict,
                                 survivors: int, entrants: int,
                                 mvp_line: str, next_board_at,
                                 flag_line: str = "") -> str:
    label = (massive_name() if is_massive(state)
             else f"Tier {tier_of(state) + 1}")
    if state.status == game.CLEARED:
        head = (f"🎉 BOARD SWEPT in {_plural(state.turn_number, 'turn')} — "
                f"{label}.\n"
                f"{_plural(state.detonations, 'mine')} found the hard way, "
                f"{survivors} of {entrants} still standing.")
    else:
        head = (f"💥 BOARD LOST — {state.exploded_cell} was one mine too many "
                f"on {label}.\n"
                f"Cleared {len(state.revealed)} of {state.total_safe} cells; "
                f"{survivors} of {entrants} still standing.")

    next_phrase = (next_board_at.astimezone().strftime("%a %-I:%M %p %Z")
                   if next_board_at else f"in {_restart_phrase()}")
    upcoming = _upcoming_massive(next_board_at)
    if upcoming:
        next_phrase = f"{upcoming}, {next_phrase}"
    required = (f"All time: {record['cleared']} cleared, "
                f"{record['exploded']} lost.\n"
                f"Next board: {next_phrase}. Follow to catch the opening.")

    kept = []
    for line in filter(None, [mvp_line, flag_line]):
        body = f"{head}\n" + "\n".join(kept + [line, required])
        if len(body) <= bluesky.POST_LIMIT:
            kept.append(line)
    return _with_tags(f"{head}\n" + "\n".join(kept + [required]))


def _record_flags(state: game.GameState, claimed: dict,
                  withdrawn: dict) -> tuple:
    """Persist this turn's mine claims. Never allowed to break a turn.

    Returns ``(accepted, capped)``: the claims that went on the record, and
    ``{did: [(reply, coord), ...]}`` for the ones that fell over the per-turn
    cap, so the people behind them can be told to try again next turn.
    """
    accepted, capped = {}, {}
    try:
        # Withdrawals first, so flag-then-unflag in one reply has already
        # been reduced to the latest word by collect_flag_changes. Clearing
        # the pennant is shared; taking back a bet is personal.
        for coord, repliers in withdrawn.items():
            db.remove_flag(state.game_id, coord)
            for reply in repliers:
                if reply.did:
                    db.withdraw_flag(state.game_id, coord, reply.did,
                                     state.turn_number)
        # Claims in reply order, so the first two somebody names are the
        # ones that count, not an accident of dictionary order.
        ordered = sorted(((reply, coord)
                          for coord, repliers in claimed.items()
                          for reply in repliers),
                         key=lambda item: (item[0].created_at or "",
                                           item[1]))
        placed = {}
        for reply, coord in ordered:
            if config.FLAG_CAP and placed.get(reply.did, 0) >= config.FLAG_CAP:
                capped.setdefault(reply.did, []).append((reply, coord))
                continue
            if db.add_flag(state.game_id, coord, reply.did, reply.handle,
                           state.turn_number):
                placed[reply.did] = placed.get(reply.did, 0) + 1
            accepted.setdefault(coord, []).append(reply)
        if claimed or withdrawn:
            logger.info("Flags: %d claimed, %d withdrawn, %d over the cap",
                        sum(len(v) for v in accepted.values()),
                        sum(len(v) for v in withdrawn.values()),
                        sum(len(v) for v in capped.values()))
    except Exception:
        logger.exception("Failed to record flags")
    return accepted, capped


def _record_assists(state: game.GameState, position, opened: tuple) -> None:
    """Credit whoever's flags proved the cell somebody just opened safe.

    `position` is the board as it stood before the move. Written before the
    post goes out, like an elimination: the row is keyed on the opened cell,
    so a replayed turn finds it already there.
    """
    try:
        standing = db.standing_flags(state.game_id)
        if not standing:
            return
        credits = solver.flag_assists(
            position, opened,
            {did: entry["coords"] for did, entry in standing.items()})
        for did, flag_coords in credits.items():
            db.record_assist(state.game_id, did, standing[did]["handle"],
                             game.index_to_coord(*opened), flag_coords,
                             state.turn_number)
    except Exception:
        logger.exception("Failed to record assists")


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


def _credit_knockout_players(state: game.GameState, plays: list) -> None:
    """Reply to each player who acted, with their score.

    Called after the moves are recorded so the totals include this turn.
    Each reply is independent: one failure must not cost anybody else theirs,
    and none of them may break a turn that has already posted.
    """
    for play in plays:
        if not play.did or not play.reply_uri:
            continue
        try:
            text = build_knockout_credit_reply(
                state, play,
                db.player_points(play.did, state.game_id),
                db.player_points(play.did),
                first_move=_first_move(state, play.did))
            uri = bluesky.post_reply(text, parent_uri=play.reply_uri,
                                     parent_cid=play.reply_cid,
                                     root_uri=play.root_uri,
                                     root_cid=play.root_cid)
            _remember(uri, "credit", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to credit %s", play.handle)


def _notify_cascade_skips(state: game.GameState, moves: list) -> None:
    """Answer valid callers whose cells an earlier move flood-opened.

    The new turn post has already succeeded before this runs, so it is safe
    to tell the player that another turn is open.  Like score replies, these
    notices are best-effort and cannot roll back the turn.
    """
    for move in moves:
        reply = move.reply
        if not move.did or not reply.uri:
            continue
        try:
            uri = bluesky.post_reply(
                build_cascade_skip_reply(state, move),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="cascade")
            _remember(uri, "cascade", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to explain cascade skip to %s",
                             move.handle)


def _notify_duplicate_skips(state: game.GameState, moves: list) -> None:
    """Answer later callers of a cell another player opened first."""
    for move in moves:
        reply = move.reply
        if not move.did or not reply.uri:
            continue
        try:
            uri = bluesky.post_reply(
                build_duplicate_skip_reply(state, move),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="duplicate")
            _remember(uri, "duplicate", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to explain duplicate move to %s",
                             move.handle)


_NOTICE_LIMIT = 10      # explanations per tick, so one thread can't flood
_STALE_LOOKBACK = 4     # earlier board posts re-read for late replies


def _send_notice(state: game.GameState, reply, text: str, kind: str) -> bool:
    """Answer one reply, once. Claim first, release if the post fails."""
    if not reply.uri or not db.claim_notice(reply.uri, kind):
        return False
    try:
        uri = bluesky.post_reply(
            text, parent_uri=reply.uri, parent_cid=reply.cid,
            root_uri=reply.root_uri, root_cid=reply.root_cid, kind=kind)
    except Exception:
        db.release_notice(reply.uri)
        logger.exception("Failed to explain a %s notice to %s", kind,
                         reply.handle)
        return False
    _remember(uri, kind, state.game_id, state.turn_number)
    return True


def _notify_open_cell_votes(state: game.GameState, replies: list,
                            already_open: set, budget: int) -> int:
    """Tell players whose only vote named a cell that is already open."""
    sent = 0
    for reply, coord in votes.unplayable_votes(replies, already_open,
                                               state.rows, state.cols):
        if sent >= budget:
            break
        if _send_notice(state, reply, build_open_cell_reply(coord), "opencell"):
            sent += 1
    return sent


def _as_utc(value: str):
    """An ISO or SQLite timestamp as an aware UTC datetime, or None."""
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _notify_stale_post_replies(state: game.GameState, budget: int) -> int:
    """Tell players who replied to a turn post after a newer one went up.

    Only the newest board post is read for votes, so those replies were
    never going to count. A reply sent before its successor was posted is
    left alone: it was on the live post when it was written.
    """
    boards = [p for p in db.get_posts(state.game_id)
              if p["kind"] in ("turn", "newgame")]     # newest first
    uris = [p["uri"] for p in boards]
    if state.last_post_uri not in uris:
        return 0
    me, sent = bluesky.own_did(), 0
    start = uris.index(state.last_post_uri)
    for i in range(start + 1, min(len(boards), start + 1 + _STALE_LOOKBACK)):
        went_stale = _as_utc(boards[i - 1]["created_at"])
        if went_stale is None:
            continue
        try:
            replies = bluesky.get_replies(boards[i]["uri"])
        except Exception:
            logger.exception("Failed to read old post %s", boards[i]["uri"])
            continue
        for reply in sorted(replies, key=lambda r: r.created_at or ""):
            if sent >= budget:
                return sent
            when = _as_utc(reply.created_at)
            if (reply.did == me or when is None or when <= went_stale
                    or not votes.is_actionable(reply.text, state.rows,
                                               state.cols)):
                continue
            if _send_notice(state, reply, build_stale_post_reply(), "stalepost"):
                sent += 1
    return sent


def _explain_uncounted_replies(state: game.GameState, replies: list,
                               already_open: set) -> None:
    """Best-effort: never let an explanation get in the way of the turn."""
    try:
        left = _NOTICE_LIMIT - _notify_open_cell_votes(
            state, replies, already_open, _NOTICE_LIMIT)
        if left > 0:
            _notify_stale_post_replies(state, left)
    except Exception:
        logger.exception("Failed to explain uncounted replies")


def _announce_milestones(state: game.GameState, movers: list) -> None:
    """One celebration post for everybody who passed a rung this turn.

    Called after the moves are recorded, so the totals it quotes match the
    ones the players were just told in their own replies. Like every other
    extra post, a failure here must not touch a turn that has already gone
    out — and a claim whose post failed is handed back so the next turn can
    try again.
    """
    crossings, seen = [], set()
    for did, handle, earned in movers:
        if not did:
            continue
        if earned > 0:
            try:
                crossings.extend(milestone_crossings(
                    did, handle, db.player_points(did), earned, state.game_id))
            except Exception:
                logger.exception("Failed to check milestones for %s", handle)
        # A day is played whatever the move scored, a mine included, and
        # counted once however many moves the player has in this turn.
        if did in seen:
            continue
        seen.add(did)
        try:
            crossings.extend(day_crossings(did, handle, state.game_id))
        except Exception:
            logger.exception("Failed to check day badges for %s", handle)
    if not crossings:
        return

    text, dids = build_milestone_text(crossings)
    try:
        uri = bluesky.post_text(text, kind="milestone", extra_dids=dids)
        _remember(uri, "milestone", state.game_id, state.turn_number)
        for crossing in crossings:
            logger.info("Milestone: %s passed %d %s (%d all time)",
                        crossing["handle"], crossing["threshold"],
                        crossing.get("kind", "points"), crossing["points"])
    except Exception:
        logger.exception("Failed to post a milestone; releasing the claims")
        for crossing in crossings:
            try:
                db.release_milestones(crossing["did"], [crossing["threshold"]],
                                      kind=crossing.get("kind", "points"))
            except Exception:
                logger.exception("Failed to release a milestone claim")


def _record_checks(state: game.GameState, replies: list,
                   already_open: set) -> dict:
    """Place and clear this turn's check marks; {coord: [Reply]} asked to mark.

    Recorded before the post, like flags, and idempotent the same way: a
    replayed turn reads the same replies and leaves the same marks. Never
    allowed to break a turn.
    """
    try:
        checks, unchecks = votes.collect_check_changes(
            replies, already_open, state.rows, state.cols)
        for coord in unchecks:
            db.remove_check(state.game_id, coord)
        for coord, repliers in checks.items():
            first = repliers[0]
            db.add_check(state.game_id, coord, first.did, first.handle,
                         state.turn_number)
        if checks or unchecks:
            logger.info("Checks: %d marked, %d cleared", len(checks),
                        len(unchecks))
        return checks
    except Exception:
        logger.exception("Failed to record check marks")
        return {}


def _visible_checks(state: game.GameState, flags=()) -> set:
    """Marks still worth drawing: on hidden cells, and under no flag."""
    try:
        return {c for c in db.checked_coords(state.game_id)
                if c not in flags and not state.coord_is_revealed(c)}
    except Exception:
        logger.exception("Failed to read check marks")
        return set()


def _confirm_first_checks(state: game.GameState, checked: dict) -> None:
    """Explain check marks once, to each player the first time they use one."""
    by_player = {}
    for coord, repliers in checked.items():
        for reply in repliers:
            if not reply.did or not reply.uri:
                continue
            entry = by_player.setdefault(reply.did,
                                         {"reply": reply, "coords": set()})
            entry["coords"].add(coord)
            if (reply.created_at or "") < (entry["reply"].created_at or ""):
                entry["reply"] = reply

    for did, entry in sorted(by_player.items(),
                             key=lambda item: item[1]["reply"].created_at or ""):
        reply = entry["reply"]
        try:
            if not db.claim_check_tutorial(did):
                continue
        except Exception:
            logger.exception("Failed to reserve check tutorial for %s",
                             reply.handle)
            continue
        try:
            uri = bluesky.post_reply(
                build_check_reply(sorted(entry["coords"])),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="checkack")
        except Exception:
            logger.exception("Failed to explain a check mark to %s",
                             reply.handle)
            try:
                db.release_check_tutorial(did)
            except Exception:
                logger.exception("Failed to release check tutorial for %s",
                                 reply.handle)
            continue
        _remember(uri, "checkack", state.game_id, state.turn_number)


def _confirm_first_flags(state: game.GameState, claimed: dict) -> None:
    """Teach each player once, when they place their first-ever flag."""
    by_player = {}
    for coord, repliers in claimed.items():
        for reply in repliers:
            if not reply.did or not reply.uri:
                continue
            entry = by_player.setdefault(reply.did,
                                         {"reply": reply, "coords": set()})
            entry["coords"].add(coord)
            if ((reply.created_at or "")
                    < (entry["reply"].created_at or "")):
                entry["reply"] = reply

    ordered = sorted(by_player.items(),
                     key=lambda item: item[1]["reply"].created_at or "")
    for did, entry in ordered:
        reply = entry["reply"]
        try:
            if not db.claim_flag_tutorial(did):
                continue
        except Exception:
            logger.exception("Failed to reserve flag tutorial for %s",
                             reply.handle)
            continue
        try:
            uri = bluesky.post_reply(
                build_flag_reply(sorted(entry["coords"]),
                                 len(db.flag_counts(state.game_id))),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="flagack")
        except Exception:
            logger.exception("Failed to confirm first flag for %s",
                             reply.handle)
            try:
                db.release_flag_tutorial(did)
            except Exception:
                logger.exception("Failed to release flag tutorial for %s",
                                 reply.handle)
            continue
        try:
            _remember(uri, "flagack", state.game_id, state.turn_number)
        except Exception:
            # The public reply succeeded. Keep the lifetime claim so a post
            # logging failure cannot make us send the tutorial twice.
            logger.exception("Failed to remember flag tutorial reply")


def _confirm_unflags(state: game.GameState, withdrawn: dict) -> None:
    """Answer each player who took a flag back, once per turn.

    What it reports is read from the database as it stands, not from what
    this turn removed, so a replayed turn cannot turn "off the board" into a
    misleading "you hadn't flagged it".
    """
    by_player = {}
    for coord, repliers in withdrawn.items():
        for reply in repliers:
            if not reply.did or not reply.uri:
                continue
            entry = by_player.setdefault(reply.did,
                                         {"reply": reply, "coords": set()})
            entry["coords"].add(coord)
            if ((reply.created_at or "")
                    < (entry["reply"].created_at or "")):
                entry["reply"] = reply

    ordered = sorted(by_player.values(),
                     key=lambda e: e["reply"].created_at or "")
    for entry in ordered:
        reply = entry["reply"]
        try:
            counts = db.flag_counts(state.game_id)
            cells = {c: counts.get(c, 0) for c in entry["coords"]}
            text = build_unflag_reply(
                cells, len(counts),
                db.late_withdrawals(state.game_id, reply.did,
                                    state.turn_number)
                if db.flag_scoring_active(state.game_id) else 0)
            uri = bluesky.post_reply(
                text, parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="unflagack")
            _remember(uri, "unflagack", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to confirm unflag for %s", reply.handle)


def _confirm_capped_flags(state: game.GameState, capped: dict,
                          claimed: dict) -> None:
    """Tell each player whose flags went over the per-turn cap, once."""
    for did, requests in capped.items():
        reply = min((r for r, _ in requests), key=lambda r: r.created_at or "")
        if not reply.did or not reply.uri:
            continue
        placed = sorted(c for c, repliers in claimed.items()
                        if any(r.did == did for r in repliers))
        waiting = sorted({c for _, c in requests} - set(placed))
        if not waiting:
            continue
        try:
            uri = bluesky.post_reply(
                build_flag_cap_reply(placed, waiting),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="flagcap")
            _remember(uri, "flagcap", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to answer capped flags for %s",
                             reply.handle)


def _confirm_ignored_flags(state: game.GameState, ignored: dict) -> None:
    """Tell each player whose flag change lost the turn, once per turn."""
    by_player = {}
    for coord, requests in ignored.items():
        for reply, wanted in requests:
            if not reply.did or not reply.uri:
                continue
            entry = by_player.setdefault(reply.did,
                                         {"reply": reply, "cells": {}})
            entry["cells"][coord] = wanted
            if ((reply.created_at or "")
                    < (entry["reply"].created_at or "")):
                entry["reply"] = reply

    ordered = sorted(by_player.values(),
                     key=lambda e: e["reply"].created_at or "")
    for entry in ordered:
        reply = entry["reply"]
        try:
            uri = bluesky.post_reply(
                build_ignored_flag_reply(entry["cells"]),
                parent_uri=reply.uri, parent_cid=reply.cid,
                root_uri=reply.root_uri, root_cid=reply.root_cid,
                kind="flagignored")
            _remember(uri, "flagignored", state.game_id, state.turn_number)
        except Exception:
            logger.exception("Failed to explain ignored flag for %s",
                             reply.handle)


class RepliesUnavailable(Exception):
    """The turn post's replies could not be read in full, so the turn waits.

    Playing on a bad read is playing the turn as if nobody moved: the bot
    opens a cell of its own, posts the next board, and every move on this one
    is stranded on a post that is no longer live. On 27 October Bluesky
    answered Turn 11's thread with a 200 and none of its three replies, and
    Turn 12 opened D10 over them.
    """


_FETCH_ATTEMPTS = 3
_FETCH_PAUSE = 5             # seconds between attempts
_FETCH_RETRY_MINUTES = 2     # then hand the turn to a one-off retry
# A reply the bot can never see (a blocked or deleted account) keeps a post's
# reply count above what any read returns, so a short read only holds the
# turn this many times before it is played with the fullest read so far.
_SHORT_HOLDS = 3
_short_holds: dict = {}      # post uri -> holds so far


def _fetch_replies(uri: str) -> list:
    if not uri:
        return []
    best, failed = None, 0
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        try:
            replies, complete = bluesky.read_replies(uri)
        except Exception:
            failed += 1
            logger.exception("Failed to fetch replies from %s (attempt %d/%d)",
                             uri, attempt, _FETCH_ATTEMPTS)
        else:
            if complete:
                _short_holds.pop(uri, None)
                return replies
            if best is None or len(replies) > len(best):
                best = replies
        if attempt < _FETCH_ATTEMPTS:
            time.sleep(_FETCH_PAUSE)
    if best is None:
        raise RepliesUnavailable(uri)           # nothing came back at all
    holds = _short_holds.get(uri, 0)
    if holds < _SHORT_HOLDS:
        _short_holds[uri] = holds + 1
        raise RepliesUnavailable(uri)
    logger.warning("Playing %s with %d replies after %d short reads", uri,
                   len(best), holds)
    _short_holds.pop(uri, None)
    return best


def _retry_turn_soon() -> None:
    """Play the turn again in a couple of minutes instead of a whole interval.

    Nothing has been saved or posted, so the retry starts from the same state
    and reads the same post, plus whatever arrived in between. Only the
    scheduler's own ticks get one; `--play` has no scheduler running.
    """
    if not scheduler.running:
        return
    run_date = datetime.now().astimezone() + timedelta(
        minutes=_FETCH_RETRY_MINUTES)
    scheduler.add_job(game_tick, "date", run_date=run_date,
                      id="game_tick_retry", replace_existing=True)
    logger.warning("Replies unreadable; retrying the turn at %s",
                   run_date.strftime("%H:%M:%S %Z"))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

# A retry and the regular tick are separate jobs, so the scheduler would run
# them side by side; two ticks at once would play the same turn twice.
_tick_lock = threading.Lock()


def game_tick() -> None:
    if not _tick_lock.acquire(blocking=False):
        logger.warning("A turn is already being played; skipping this tick")
        return
    try:
        _game_tick()
    except RepliesUnavailable:
        _retry_turn_soon()
    except Exception:
        logger.exception("game_tick failed")      # never kill the scheduler
    finally:
        _tick_lock.release()


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

    _explain_uncounted_replies(state, replies, already_open)

    claimed, withdrawn, ignored = votes.collect_flag_changes(
        replies, already_open, state.rows, state.cols,
        db.flagged_coords(state.game_id, config.FLAG_QUORUM))
    claimed, capped = _record_flags(state, claimed, withdrawn)
    checked = _record_checks(state, replies, already_open)

    if config.KNOCKOUT:
        _play_knockout_turn(state, replies, already_open, position, analysis,
                            claimed, withdrawn, ignored, capped, checked)
        return

    # 2. Decide. The quorum exists to stop one stray vote carrying a turn when
    #    a real crowd has scattered — but it must never silence a small one.
    #    With no more voters than the quorum itself there is nothing to split,
    #    so whoever turned up decides. Applied strictly, a lone player would
    #    watch the bot play 100% of the turns, which is not a crowd game.
    why = ""
    if vote is not None and _crowd_decides(vote):
        coord, source = vote.coord, "crowd"
        logger.info("Turn %d by crowd: %s (%d of %d votes, called by %s)",
                    state.turn_number + 1, coord, vote.votes,
                    vote.total_voters, vote.caller_handle or "unknown")
    else:
        cell, reason = solver.safest_move(position, analysis)
        coord, source = game.index_to_coord(*cell), "bot"
        why = solver.explain(position, cell, analysis)
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
    if result == game.SAFE and source == "crowd":
        _record_assists(state, position, index)
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
    flags = state.shown_flags(
        db.flagged_coords(state.game_id, config.FLAG_QUORUM))

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
        text = build_turn_text(state, coord, vote, source, flags, why)
        kind = "turn"

    try:
        uri = _post_board(state, text, kind, extra_dids=extra, flags=flags,
                          checks=_visible_checks(state, flags))
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
                    db.player_points(caller_did),
                    first_move=_first_move(state, caller_did)),
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

    if claimed:
        _confirm_first_flags(state, claimed)
    if checked:
        _confirm_first_checks(state, checked)
    if withdrawn:
        _confirm_unflags(state, withdrawn)
    if ignored:
        _confirm_ignored_flags(state, ignored)
    if capped:
        _confirm_capped_flags(state, capped, claimed)

    if not move_recorded:
        try:
            db.record_move(state, coord, result, source,
                           caller=state.last_caller, votes=state.last_votes,
                           voters=state.last_voters,
                           was_provably_safe=was_provably_safe,
                           did=caller_did, points=points)
        except Exception:
            logger.exception("Failed to record move on retry")

    # Announced last of the extras, so the total it quotes is the one the
    # recorded move produced rather than one this turn has yet to save.
    _announce_milestones(state, [(caller_did, state.last_caller, points)])
    # After the move is recorded, so the player who finished it is named.
    _thank_the_board(state, uri)
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
                        claimed: dict, withdrawn: dict,
                        ignored: dict | None = None,
                        capped: dict | None = None,
                        checked: dict | None = None) -> None:
    """One knockout turn: everybody eligible opens a cell of their own.

    Same ordering discipline as the plurality path — the move is applied in
    memory, the post goes out, and only then is anything saved, so a network
    blip replays the turn instead of losing it.
    """
    revealed_before = set(state.revealed)
    entrants_before = db.player_count(state.game_id)
    plays, source, superseded, duplicates = apply_knockout_moves(
        state, replies, already_open, position, analysis)

    last = plays[-1]
    state.last_coord = last.coord
    state.last_result = last.result
    state.last_source = source
    state.last_caller = last.handle
    state.last_votes = 0
    state.last_voters = sum(1 for p in plays if p.did)

    flags = state.shown_flags(
        db.flagged_coords(state.game_id, config.FLAG_QUORUM))
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
        for move in superseded:
            leaders = leaders_with_pending_move(leaders, move.did, move.handle,
                                                CASCADE_CREDIT)
        text = build_knockout_gameover_text(
            state, record, max(0, entrants - len(eliminated)), entrants,
            build_mvp_line(leaders),
            datetime.now().astimezone()
            + timedelta(seconds=config.RESTART_DELAY_SECONDS),
            build_flag_line(db.flag_scores(state.game_id)))
        kind = "gameover"
    else:
        text = build_knockout_turn_text(state, plays, flags)
        kind = "turn"

    try:
        uri = _post_board(state, text, kind, extra_dids=extra, flags=flags,
                          checks=_visible_checks(state, flags))
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
    for move in superseded:
        try:
            db.record_move(state, move.coord, game.SAFE, source,
                           caller=move.handle, did=move.did,
                           points=CASCADE_CREDIT,
                           was_provably_safe=(
                               game.coord_to_index(move.coord) in analysis.safe))
        except Exception:
            logger.exception("Failed to record cascade credit %s", move.coord)

    _credit_knockout_players(state, plays)
    _notify_cascade_skips(state, superseded)
    _notify_duplicate_skips(state, duplicates)
    _announce_milestones(state, [(p.did, p.handle, p.points) for p in plays]
                         + [(m.did, m.handle, CASCADE_CREDIT)
                            for m in superseded])
    _thank_the_board(state, uri)

    if not finished:
        _resolve_flags(state, last.coord, last.result, revealed_before)
    if claimed:
        _confirm_first_flags(state, claimed)
    if checked:
        _confirm_first_checks(state, checked)
    if withdrawn:
        _confirm_unflags(state, withdrawn)
    if ignored:
        _confirm_ignored_flags(state, ignored)
    if capped:
        _confirm_capped_flags(state, capped, claimed)

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


def _start_new_game(now: datetime | None = None) -> None:
    previous = db.load_state()
    game_id = (previous.game_id + 1) if previous else 1
    now = now or datetime.now(timezone.utc)
    massive = massive_board_for(now)
    if massive:
        rows, cols, mines = massive
        budget = config.MASSIVE_MINE_BUDGET
        resume = next_tier(previous)
    else:
        rows, cols, mines = next_board_settings(previous)
        budget = mine_budget_for(next_tier(previous))
    state = game.new_game(game_id, rows=rows, cols=cols, mines=mines,
                          mine_budget=budget)
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
    if massive:
        # Marked once the board exists, so a crash before this point retries
        # the big board rather than skipping a week of it.
        try:
            db.set_setting(_MASSIVE_RESUME, str(resume))
            db.set_setting(
                _MASSIVE_DATE,
                now.astimezone(_leaderboard_tz()).date().isoformat())
        except Exception:
            logger.exception("Failed to record the big board's date")
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


# ---------------------------------------------------------------------------
# The weekly standings post
# ---------------------------------------------------------------------------

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _leaderboard_tz():
    try:
        return ZoneInfo(config.LEADERBOARD_TZ)
    except Exception:
        logger.warning("Unknown LEADERBOARD_TZ %r; using local time",
                       config.LEADERBOARD_TZ)
        return datetime.now().astimezone().tzinfo


def previous_slot(now: datetime) -> datetime:
    """The most recent scheduled posting time at or before `now`.

    Computed in the configured zone so the slot keeps its wall-clock hour
    across a daylight-saving change, and returned as an aware datetime.
    """
    tz = _leaderboard_tz()
    local = now.astimezone(tz)
    try:
        wanted = _DAYS.index(config.LEADERBOARD_DAY.strip().lower()[:3])
    except ValueError:
        wanted = 5
    slot = local.replace(hour=config.LEADERBOARD_HOUR, minute=0, second=0,
                         microsecond=0)
    slot -= timedelta(days=(local.weekday() - wanted) % 7)
    if slot > local:
        slot -= timedelta(days=7)
    return slot


def leaderboard_due(now: datetime | None = None) -> bool:
    """Whether this week's post is owed.

    APScheduler recomputes a cron job's next run when the process starts, so
    a deploy that restarts the bot across the scheduled minute would skip the
    week in silence. The first one ever waits for its real slot rather than
    firing the moment this ships.
    """
    if not config.WEEKLY_LEADERBOARD:
        return False
    now = now or datetime.now(timezone.utc)
    last = db.last_post_at("leaderboard")
    if last is None:
        return False
    return last < previous_slot(now)


def snapshot_followers() -> None:
    """Record the follower count. Never raises: a missed reading is only a gap."""
    try:
        counts = bluesky.get_profile_counts()
        if counts is None:
            return
        db.record_followers(counts["followers"], counts["follows"],
                            counts["posts"])
        logger.info("Followers: %d", counts["followers"])
    except Exception:
        logger.exception("Failed to record the follower count")


def print_follower_history() -> None:
    """The history as CSV on stdout, timestamps in UTC."""
    print("created_at_utc,followers,follows,posts")
    for row in db.follower_history():
        print(f"{row['created_at']},{row['followers']},"
              f"{'' if row['follows'] is None else row['follows']},"
              f"{'' if row['posts'] is None else row['posts']}")


def weekly_leaderboard(now: datetime | None = None) -> str:
    """Post the standings: top of the week, and top of all time."""
    now = (now or datetime.now(timezone.utc)).astimezone(_leaderboard_tz())
    start = now - timedelta(days=7)
    top = max(1, config.LEADERBOARD_TOP)
    weekly = db.leaderboard(limit=top, since=start)
    all_time = db.leaderboard(limit=top)
    if not weekly and not all_time:
        logger.info("No scores yet; skipping the weekly standings")
        return ""
    text, dids = build_leaderboard_text(all_time, weekly, start, now,
                                        db.flag_scores())
    uri = bluesky.post_text(text, kind="leaderboard", extra_dids=dids)
    _remember(uri, "leaderboard", None)
    logger.info("Posted the weekly standings (%d this week, %d all time)",
                len(weekly), len(all_time))
    # The standings went out; the welcome under them is a bonus and must
    # never make a posted table look like a failure that needs repeating.
    try:
        newcomers, returners = db.weekly_arrivals(start)
        reply, reply_dids = build_arrivals_text(newcomers, returners)
    except Exception:
        logger.exception("Failed to gather the week's arrivals")
        reply, reply_dids = "", {}
    _reply_to_own_post(uri, reply, "arrivals", reply_dids, None)
    return uri


def post_weekly_leaderboard() -> None:
    try:
        weekly_leaderboard()
    except Exception:
        logger.exception("Weekly standings post failed")


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
    parser.add_argument("--leaderboard", action="store_true",
                        help="post the weekly standings now and exit")
    parser.add_argument("--followers", action="store_true",
                        help="print the recorded follower history as CSV "
                             "and exit")
    args = parser.parse_args()

    if args.followers:
        db.init_db()
        print_follower_history()
        return

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

    if args.leaderboard:
        weekly_leaderboard()
        return

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

    if config.FOLLOWER_SNAPSHOT_MINUTES > 0:
        scheduler.add_job(snapshot_followers, "interval",
                          minutes=config.FOLLOWER_SNAPSHOT_MINUTES,
                          next_run_time=datetime.now(timezone.utc),
                          id="follower_snapshot", coalesce=True,
                          max_instances=1, misfire_grace_time=600)
        logger.info("Recording followers every %d minutes",
                    config.FOLLOWER_SNAPSHOT_MINUTES)

    if config.WEEKLY_LEADERBOARD:
        scheduler.add_job(post_weekly_leaderboard, "cron",
                          day_of_week=config.LEADERBOARD_DAY,
                          hour=config.LEADERBOARD_HOUR, minute=0,
                          timezone=_leaderboard_tz(), id="weekly_leaderboard",
                          coalesce=True, max_instances=1,
                          misfire_grace_time=3600)
        slot = previous_slot(datetime.now(timezone.utc)) + timedelta(days=7)
        logger.info("Weekly standings every %s at %02d:00 %s, next at %s",
                    config.LEADERBOARD_DAY, config.LEADERBOARD_HOUR,
                    config.LEADERBOARD_TZ, slot.strftime("%Y-%m-%d %H:%M %Z"))
        if leaderboard_due():
            logger.info("Last week's standings were missed; posting now")
            post_weekly_leaderboard()

    scheduler.start()


if __name__ == "__main__":
    main()
