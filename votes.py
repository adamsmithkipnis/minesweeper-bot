"""Reading coordinates out of replies, and turning them into a decision.

Kept free of any AT Protocol dependency: this is text handling and counting,
so it is unit-testable without a network, credentials, or the atproto package.
`bluesky.py` adapts real replies into the small `Reply` record below.

**The hard part is that Minesweeper replies argue.** Battleship votes were
bare coordinates, so taking the first one in the text was fine. Here a reply
routinely reads "C3 is a mine, so D4 must be safe — D4", and taking the first
coordinate fires at C3: the mine. So a mention is classified before it is
counted, and a coordinate the writer is calling a mine is never treated as a
vote for opening it.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from dataclasses import dataclass

# Words that mark the coordinate after them as the writer's choice.
_VOTE_WORDS = (
    r"vote|votes|voting|pick|picks|picking|choose|choice|go|going|goes|play|"
    r"plays|open|opens|opening|reveal|reveals|click|clicks|try|hit|take|say|"
    r"calling|call|"
    # Corrections: "meant F8", "actually F8", "switch to F8", "make that F8".
    r"meant|mean|actually|instead|switch to|switching to|change to|"
    r"changing to|make that|make it"
)

# "F8 instead" names the choice after the coordinate rather than before it.
# "instead of F7" is the opposite, and is caught by _AVOID_WORDS.
_CHOICE_AFTER = re.compile(r"^[^A-Za-z0-9]{0,4}instead\b(?!\s+of\b)",
                           re.IGNORECASE)

# Words that mean the opposite: the coordinate after them is being warned
# about, not chosen.
_AVOID_WORDS = (
    r"not|don'?t|do not|cannot|can'?t|never|avoid|isn'?t|except|but not|skip|"
    r"flag|flagging|flagged|careful|beware|steer clear of|mine at|bomb at|"
    r"instead of|rather than"
)

# The subset of warnings that are a positive claim "there is a mine here".
# "flag C3" is one; "careful with C3" is not — the second is a hunch about
# risk, and turning it into a mine claim would put words in someone's mouth.
_FLAG_WORDS = r"flag|flagging|flagged|mine at|bomb at"
_FLAG_BEFORE = re.compile(rf"\b(?:{_FLAG_WORDS})[^A-Za-z0-9]{{0,6}}$", re.IGNORECASE)

# "D4 is a mine", "D4 = bomb", "D4 must be a mine" — an assertion about a
# cell, never a request to open it.
_IS_MINE = re.compile(
    r"\s*(?:is|are|=|looks|look|seems|seem|has to be|have to be|must be|"
    r"might be|could be|:)\s*"
    r"(?:a\s+|an\s+|all\s+|both\s+|definitely\s+|probably\s+|clearly\s+)*"
    r"(?:mines?|bombs?|traps?|dangerous|unsafe)",
    re.IGNORECASE,
)

# Danger symbols. These need their own pattern because \b word boundaries do
# not apply to emoji, so they can never match through _AVOID_WORDS — which is
# how "🚩 C3" came to read as a vote to OPEN C3: the most natural way anyone
# would flag a mine was the one phrasing that detonated it.
_DANGER = "\U0001F6A9\U0001F3F4\u26F3\U0001F4A3\U0001F4A5\u2620\u26A0\u274C\U0001F6D1\u2757\u203C"
_DANGER_BEFORE = re.compile(rf"[{_DANGER}][^A-Za-z0-9]{{0,4}}$")
_DANGER_AFTER = re.compile(rf"^[^A-Za-z0-9]{{0,4}}[{_DANGER}]")

# "unflag C3" is a request to take a mark off, never a request to open the
# cell. Left unhandled it parsed as a plain vote for C3.
_UNDO_BEFORE = re.compile(r"\b(?:un-?flag(?:ging|ged)?|unmark|remove(?:\s+the)?\s+flag(?:\s+(?:on|from|at))?|clear(?:\s+the)?\s+flag(?:\s+(?:on|from|at))?)\b[^A-Za-z0-9]{0,4}$",
                          re.IGNORECASE)

_NEGATION_BEFORE = re.compile(
    rf"\b(?:{_AVOID_WORDS})"
    rf"(?:[^A-Za-z0-9]{{0,4}}[A-Za-z]{{1,7}})?"
    rf"[^A-Za-z0-9]{{0,12}}$",
    re.IGNORECASE,
)

# "check D4", "✅ D4" or "❓ D4" marks a cell as worth a second look: probably a mine,
# but not sure enough to bet a flag on. It is a note to the other players,
# never a request to open the cell, and it scores nothing.
_CHECKS = "\u2705\u2714\u2611\u2713\u2753"    # ✅ ✔ ☑ ✓ ❓ (with or without FE0F)
_CHECK_BEFORE = re.compile(
    rf"(?:\bcheck(?:ing|ed)?\b|[{_CHECKS}])[^A-Za-z0-9]{{0,6}}$", re.IGNORECASE)
_CHECK_AFTER = re.compile(rf"^[^A-Za-z0-9]{{0,4}}[{_CHECKS}]")

# Taking a check mark off. Tested before _CHECK_BEFORE, which would
# otherwise read the "check" inside "un-check D4" as placing one.
_UNCHECK_BEFORE = re.compile(
    r"\b(?:un-?check(?:ing|ed)?|remove(?:\s+the)?\s+check(?:\s+(?:on|from|at))?"
    r"|clear(?:\s+the)?\s+check(?:\s+(?:on|from|at))?)\b[^A-Za-z0-9]{0,4}$",
    re.IGNORECASE)

# "Cancel F7" takes back a move. Left unhandled it was the only coordinate in
# the reply, so it parsed as a vote FOR F7, and "Cancel F7. Meant F8" named
# two cells with no voting word and counted as nothing: either way F7 opened.
_CANCEL_BEFORE = re.compile(
    r"\b(?:cancel(?:l?ed|l?ing)?|scratch(?:ed)?|scrap(?:ped)?|disregard|"
    r"withdraw(?:ing|n)?)\b"
    r"(?:\s+(?:my|the|that))?(?:\s+(?:vote|move|pick)(?:\s+(?:for|on|to|at))?)?"
    r"[^A-Za-z0-9]{0,4}$",
    re.IGNORECASE)

_LOOKBACK = 24      # characters of context examined on either side

# "flag C3 and D5" flags both. A flag carries to the next coordinate only
# across a bare connector — so "flag C3 and I vote D4" does not, because
# there are real words in between.
_CONNECTOR = re.compile(r"^[ \t,&+/–—-]*(?:and|or|also|plus)?[ \t,&+/–—-]*$",
                        re.IGNORECASE)


@dataclass(frozen=True)
class Mention:
    """One coordinate found in a reply, and what the writer meant by it."""
    coord: str
    is_choice: bool = False     # introduced by a voting word
    is_warning: bool = False    # being warned about rather than chosen
    is_flag: bool = False       # a positive claim that it holds a mine
    is_unflag: bool = False     # a request to take a mark off
    is_check: bool = False      # marked to check: maybe a mine, not a bet
    is_uncheck: bool = False    # a request to take a check mark off
    is_cancel: bool = False     # taking back a move: "cancel F7"


@dataclass(frozen=True)
class Reply:
    """The parts of a Bluesky reply that voting cares about."""
    did: str
    handle: str
    text: str
    uri: str = ""
    cid: str = ""
    created_at: str = ""
    root_uri: str = ""
    root_cid: str = ""


def coordinate_pattern(rows: int, cols: int) -> re.Pattern:
    """A coordinate matcher sized to the board.

    Battleship hardcoded A-J and 1-10. Built from the dimensions instead, a
    9x9 board yields A-I and 1-9 — and columns are listed largest-first so a
    two-digit column on a bigger board wins over its first digit.

    The guards are Battleship's, and they still earn their keep: not preceded
    by a letter (so "sea5" is not E5) and not followed by a digit (so the
    "A1" inside "A100" is not a vote).
    """
    letters = string.ascii_uppercase[:rows]
    columns = "|".join(str(n) for n in range(cols, 0, -1))
    return re.compile(
        rf"(?<![A-Za-z])([{letters}{letters.lower()}])\s?[-,]?\s?({columns})(?!\d)"
    )


def _vote_word_pattern() -> re.Pattern:
    return re.compile(rf"\b(?:{_VOTE_WORDS})[^A-Za-z0-9]{{0,6}}$", re.IGNORECASE)


def find_mentions(text: str, rows: int, cols: int) -> list:
    """Every coordinate in `text` as a Mention, with what the writer meant."""
    text = text or ""
    pattern = coordinate_pattern(rows, cols)
    vote_word = _vote_word_pattern()
    out, spans = [], []
    for match in pattern.finditer(text):
        coord = f"{match.group(1).upper()}{match.group(2)}"
        before = text[max(0, match.start() - _LOOKBACK):match.start()]
        after = text[match.end():match.end() + _LOOKBACK]

        if _UNDO_BEFORE.search(before):
            out.append(Mention(coord, is_unflag=True))
            spans.append((match.start(), match.end()))
            continue
        if _UNCHECK_BEFORE.search(before):
            out.append(Mention(coord, is_uncheck=True))
            spans.append((match.start(), match.end()))
            continue
        if _CANCEL_BEFORE.search(before):
            out.append(Mention(coord, is_cancel=True))
            spans.append((match.start(), match.end()))
            continue

        claims_mine = (bool(_IS_MINE.match(after))
                       or bool(_FLAG_BEFORE.search(before))
                       or bool(_DANGER_BEFORE.search(before))
                       or bool(_DANGER_AFTER.match(after)))
        # A flag is the stronger claim, so it wins when a reply makes both.
        if not claims_mine and (_CHECK_BEFORE.search(before)
                                or _CHECK_AFTER.match(after)):
            out.append(Mention(coord, is_warning=True, is_check=True))
            spans.append((match.start(), match.end()))
            continue
        warning = claims_mine or bool(_NEGATION_BEFORE.search(before))
        choice = ((bool(vote_word.search(before))
                   or bool(_CHOICE_AFTER.match(after))) and not warning)
        out.append(Mention(coord, is_choice=choice, is_warning=warning,
                           is_flag=claims_mine))
        spans.append((match.start(), match.end()))

    # Second pass: a flag carries across a bare connector to its neighbours,
    # which is how people list several at once — "flag C3 and D5" forwards,
    # "C3 and D4 are mines" backwards.
    def _joined(i, j):
        return _CONNECTOR.match(text[spans[i][1]:spans[j][0]]) is not None

    for _ in range(len(out)):          # settle chains like "C3, D4 and E5"
        changed = False
        for i in range(len(out)):
            if (out[i].is_flag or out[i].is_unflag or out[i].is_check
                    or out[i].is_uncheck or out[i].is_cancel):
                continue
            # A cancel never carries: a player has one move to take back, so
            # in "cancel F7, F8" the F8 is the replacement, not a second
            # cancel.
            # An unflag carries forwards only: "unflag E1 E5" clears both.
            # Left out, E5 was a plain mention and, being last, became the
            # vote — opening a cell somebody had just asked to unmark.
            if i > 0 and out[i - 1].is_unflag and _joined(i - 1, i):
                out[i] = Mention(out[i].coord, is_unflag=True)
                changed = True
                continue
            if i > 0 and out[i - 1].is_uncheck and _joined(i - 1, i):
                out[i] = Mention(out[i].coord, is_uncheck=True)
                changed = True
                continue
            neighbours = [j for j in (i - 1, i + 1) if 0 <= j < len(out)]
            for j in neighbours:
                if out[j].is_flag and _joined(min(i, j), max(i, j)):
                    out[i] = Mention(out[i].coord, is_warning=True, is_flag=True)
                    changed = True
                    break
            else:
                # A check carries both ways, like a flag: "check D4 and D5",
                # "D4, D5 ✅".
                for j in neighbours:
                    if out[j].is_check and _joined(min(i, j), max(i, j)):
                        out[i] = Mention(out[i].coord, is_warning=True,
                                         is_check=True)
                        changed = True
                        break
        if not changed:
            break
    return out


# A reply asking the others — "who wants to fall on the H1/H2 sword", "anyone
# brave enough for C3?" — names cells without choosing one. In knockout play a
# misread here opens the cell under the asker's name.
_ASKS_OTHERS = re.compile(
    r"\b(?:who|whoever|anyone|anybody|someone|somebody|which (?:one|cell)|should we|"
    r"shall we|do we|any takers|volunteers?)\b",
    re.IGNORECASE,
)


# "Unflag D8 and open it" — the pronoun is the cell just unflagged.
_OPEN_IT = re.compile(
    rf"\b(?:{_VOTE_WORDS})\s+(?:it|that|this|there)\b", re.IGNORECASE)


def parse_vote(text: str, rows: int, cols: int) -> str | None:
    """The one coordinate a reply is voting for, or None.

    In order:
      1. the last coordinate introduced by a voting word,
      2. the only coordinate mentioned, if there is exactly one,
      3. the last coordinate mentioned, when it repeats an earlier one —
         "A1 or B2, probably B2" settles on B2.
    Coordinates the writer is warning about are never candidates. Without a
    voting word, a question to the others or a bare list of alternatives
    ("H1/H2") is discussion, not a vote: taking the last cell of those is how
    "who wants to fall on the H1/H2 sword" opened H2.
    """
    candidates = [m for m in find_mentions(text, rows, cols)
                  if not m.is_warning and not m.is_unflag
                  and not m.is_uncheck and not m.is_cancel]
    if not candidates:
        undone = {m.coord for m in find_mentions(text, rows, cols)
                  if m.is_unflag}
        if len(undone) == 1 and _OPEN_IT.search(text or ""):
            return next(iter(undone))
        return None

    chosen = [m.coord for m in candidates if m.is_choice]
    if chosen:
        return chosen[-1]

    if _ASKS_OTHERS.search(text or ""):
        return None

    distinct = {m.coord for m in candidates}
    if len(distinct) == 1:
        return candidates[0].coord

    last = candidates[-1].coord
    if last in {m.coord for m in candidates[:-1]}:
        return last
    return None


def parse_flags(text: str, rows: int, cols: int) -> tuple:
    """(flagged, unflagged) coordinate sets from one reply.

    Flagging is free: it costs no turn, so a reply can flag a cell and vote to
    open another in the same breath — which is exactly how people write when
    they are reasoning out loud.
    """
    flagged, unflagged = set(), set()
    for mention in find_mentions(text, rows, cols):
        if mention.is_unflag:
            unflagged.add(mention.coord)
        elif mention.is_flag:
            flagged.add(mention.coord)
    return flagged, unflagged


def parse_cancels(text: str, rows: int, cols: int) -> set:
    """Coordinates a reply takes back: "cancel F7", "scratch my vote for F7"."""
    return {m.coord for m in find_mentions(text, rows, cols) if m.is_cancel}


def parse_checks(text: str, rows: int, cols: int) -> tuple:
    """(checked, unchecked) coordinate sets from one reply."""
    checked, unchecked = set(), set()
    for mention in find_mentions(text, rows, cols):
        if mention.is_uncheck:
            unchecked.add(mention.coord)
        elif mention.is_check:
            checked.add(mention.coord)
    return checked, unchecked


def collect_check_changes(replies: list, resolved: set, rows: int,
                          cols: int) -> tuple:
    """(checks, unchecks), each {coord: [Reply, ...]}, across replies.

    A check mark is one shared note per cell that anyone may place or take
    off, and it carries no points, so there is nothing to protect with the
    flags' first-change-per-turn rule: the latest word on a cell, from
    anybody, decides it. Cells already open are ignored. Each cell lists
    only the people who asked for the state it ends in.
    """
    ordered = sorted(replies, key=lambda r: r.created_at or "")
    parsed = []
    final = {}
    for reply in ordered:
        checked, unchecked = parse_checks(reply.text, rows, cols)
        checked, unchecked = checked - resolved, unchecked - resolved - checked
        parsed.append((reply, checked, unchecked))
        final.update({coord: True for coord in checked})
        final.update({coord: False for coord in unchecked})

    checks, unchecks = {}, {}
    for reply, checked, unchecked in parsed:
        for coord in checked:
            if final[coord]:
                checks.setdefault(coord, []).append(reply)
        for coord in unchecked:
            if not final[coord]:
                unchecks.setdefault(coord, []).append(reply)
    return checks, unchecks


def collect_flags(replies: list, resolved: set, rows: int, cols: int,
                  flagged_now: frozenset = frozenset()) -> tuple:
    """(flags, unflags) from `collect_flag_changes`, without the ignored."""
    return collect_flag_changes(replies, resolved, rows, cols, flagged_now)[:2]


def collect_flag_changes(replies: list, resolved: set, rows: int, cols: int,
                         flagged_now: frozenset = frozenset()) -> tuple:
    """(flags, unflags, ignored) across replies.

    flags and unflags are {coord: [Reply, ...]}; ignored is
    {coord: [(Reply, wanted_flag), ...]} for requests that lost the turn's
    first-change rule, so the people behind them can be told.

    A person may flag several cells in one reply — unlike voting, where they
    get one say. Cells that are already open are ignored: a flag on a revealed
    cell is a misreading, not a prediction.

    Anyone may change any cell's flag, but only once per turn: the first
    person to actually change it (relative to `flagged_now`, the cells
    showing a flag on the board) decides it, and anyone asking for the
    opposite later in the same turn is ignored until next turn.
    """
    # A person's own latest word on a cell wins, as with votes: "unflag C3"
    # and then "flag C3" means flagged, not both.
    latest = {}
    for seq, reply in enumerate(sorted(replies,
                                       key=lambda r: r.created_at or "")):
        flagged, unflagged = parse_flags(reply.text, rows, cols)
        for coord in flagged - resolved:
            latest[(reply.did, coord)] = (seq, True, reply)
        for coord in unflagged - resolved - flagged:
            latest[(reply.did, coord)] = (seq, False, reply)

    by_cell = {}
    for (_, coord), stance in latest.items():
        by_cell.setdefault(coord, []).append(stance)

    flags, unflags, ignored = {}, {}, {}
    for coord, stances in by_cell.items():
        stances.sort(key=lambda s: s[0])
        target = coord in flagged_now
        for _, is_flag, _ in stances:
            if is_flag != (coord in flagged_now):
                target = is_flag
                break
        for _, is_flag, reply in stances:
            if is_flag == target:
                (flags if is_flag else unflags).setdefault(coord, []).append(reply)
            else:
                ignored.setdefault(coord, []).append((reply, is_flag))
    return flags, unflags, ignored


@dataclass
class VoteResult:
    coord: str
    votes: int              # votes for the winning coordinate
    total_voters: int       # distinct accounts that cast a valid vote
    caller: Reply | None = None     # first to call the winning coordinate

    @property
    def caller_handle(self) -> str:
        return self.caller.handle if self.caller else ""


def collect(replies: list, unavailable: set, rows: int, cols: int) -> tuple:
    """Reduce replies to (votes by did, first caller by coordinate).

    An account gets one vote, and it is their **latest** one: people argue
    themselves around, and someone who says "E1" and then "actually D6" means
    D6. Counting the earliest reply instead silently discarded every
    correction, which is exactly what it looked like from the outside — you
    posted a new coordinate and the bot ignored it.

    A reply with no coordinate never costs anyone their vote, so flagging a
    cell and voting for another in separate replies both count. Coordinates
    that are already open are dropped rather than counted — a vote for a
    revealed cell is a misreading of the board, not a choice.
    """
    ordered = sorted(replies, key=lambda r: r.created_at or "")
    parsed, votes = [], {}
    for reply in ordered:
        coord = parse_vote(reply.text, rows, cols)
        if coord is None:
            # "Cancel F7" on its own withdraws the move, but only the move it
            # names: cancelling some other cell is a slip, not a withdrawal.
            if votes.get(reply.did) in parse_cancels(reply.text, rows, cols):
                del votes[reply.did]
            continue
        if coord in unavailable:
            continue
        parsed.append((reply, coord))
        # Later replies replace earlier ones from the same account.
        votes[reply.did] = coord

    # Credit goes to whoever called the winning cell first — but only among
    # the people still voting for it, so someone who moved on doesn't get
    # credited for a coordinate they abandoned.
    first = {}
    for reply, coord in parsed:
        if votes.get(reply.did) == coord:
            first.setdefault(coord, reply)
    return votes, first


# How deep under the board post to look for a player's follow-ups.
FOLLOW_UP_DEPTH = 6


def with_follow_ups(items: list, author_of, replies_of) -> list:
    """Each direct reply, plus the replies its author made underneath it.

    People fix a move by replying under it: "F7", then under that, "Cancel
    F7. Meant F8". Reading only direct replies to the board post never saw
    the correction, so F7 opened. A nested reply counts only while the chain
    above it is all the same author's; anyone else replying under a move is
    discussing it, and reading that as a move would play their chatter.

    The accessors keep this free of Bluesky's types, so the bot (atproto
    objects) and the dashboard (JSON) walk threads the same way.
    """
    out = []

    def walk(item, did):
        for child in replies_of(item) or []:
            if did and author_of(child) == did:
                out.append(child)
                walk(child, did)

    for item in items or []:
        out.append(item)
        walk(item, author_of(item))
    return out


def unplayable_votes(replies: list, unavailable: set, rows: int,
                     cols: int) -> list:
    """[(reply, coord)] for players whose votes all named an open cell.

    `collect` drops those votes silently, which from the outside looks like
    being ignored. A player with another, valid vote this turn is left out:
    their say still counts, so there is nothing to explain. One entry per
    account, their latest dead vote.
    """
    ordered = sorted(replies, key=lambda r: r.created_at or "")
    dead, alive = {}, set()
    for reply in ordered:
        coord = parse_vote(reply.text, rows, cols)
        if coord is None:
            # Withdrawn, so there is nothing left to explain.
            if (reply.did in dead and dead[reply.did][1]
                    in parse_cancels(reply.text, rows, cols)):
                del dead[reply.did]
            continue
        if coord in unavailable:
            dead[reply.did] = (reply, coord)
        else:
            alive.add(reply.did)
    return [pair for did, pair in dead.items() if did not in alive]


def is_actionable(text: str, rows: int, cols: int) -> bool:
    """Whether a reply tried to vote or flag, as opposed to just chatting."""
    if parse_vote(text, rows, cols) is not None:
        return True
    if parse_cancels(text, rows, cols):
        return True
    flagged, unflagged = parse_flags(text, rows, cols)
    if flagged or unflagged:
        return True
    checked, unchecked = parse_checks(text, rows, cols)
    return bool(checked or unchecked)


@dataclass
class Move:
    """One player's action for a turn."""
    did: str
    handle: str
    coord: str
    reply: Reply


def moves(replies: list, unavailable: set, rows: int, cols: int,
          exclude: set = frozenset()) -> list:
    """Every eligible player's cell for this turn, earliest replier first.

    Knockout play acts on all of these rather than only the plurality winner:
    one action each, so nobody's reply is discarded. The per-account rule is
    the same as voting — their latest coordinate — so a correction still
    works. `exclude` holds accounts already knocked out of this board; they
    can still flag, which costs no turn and carries no risk.
    """
    ordered = sorted(replies, key=lambda r: r.created_at or "")
    votes, _ = collect(replies, unavailable, rows, cols)

    seen, out = set(), []
    for reply in ordered:
        did = reply.did
        if did in seen or did in exclude or did not in votes:
            continue
        seen.add(did)
        out.append(Move(did=did, handle=reply.handle, coord=votes[did],
                        reply=reply))
    return out


def breakdown(replies: list, unavailable: set, rows: int, cols: int) -> list:
    """Every coordinate currently voted for, most votes first.

    The same reduction as `tally`, exposed so the dashboard's live view and
    the bot can never disagree about the standings.
    """
    votes, first = collect(replies, unavailable, rows, cols)
    out = []
    for coord, count in Counter(votes.values()).most_common():
        caller = first.get(coord)
        out.append({
            "coord": coord,
            "votes": count,
            "first_caller": caller.handle if caller else "",
        })
    return out


def tally(replies: list, unavailable: set, rows: int, cols: int) -> VoteResult | None:
    """The winning coordinate, or None when nobody cast a valid vote.

    Ties go to whichever coordinate was called first, which rewards being
    early and is at least explicable to the people who voted.
    """
    votes, first = collect(replies, unavailable, rows, cols)
    if not votes:
        return None

    counts = Counter(votes.values())
    best = max(counts.values())
    tied = [coord for coord, n in counts.items() if n == best]
    if len(tied) > 1:
        tied.sort(key=lambda c: (first[c].created_at or "", c))
    coord = tied[0]

    return VoteResult(coord=coord, votes=best, total_voters=len(votes),
                      caller=first.get(coord))
