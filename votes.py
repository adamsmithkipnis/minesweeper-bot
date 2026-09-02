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
    r"calling|call"
)

# Words that mean the opposite: the coordinate after them is being warned
# about, not chosen.
_AVOID_WORDS = (
    r"not|don'?t|do not|cannot|can'?t|never|avoid|isn'?t|except|but not|skip|"
    r"flag|flagging|flagged|careful|beware|steer clear of"
)

# "D4 is a mine", "D4 = bomb", "D4 must be a mine" — an assertion about a
# cell, never a request to open it.
_IS_MINE = re.compile(
    r"\s*(?:is|=|looks|seems|has to be|must be|might be|could be|:)\s*"
    r"(?:a\s+|an\s+|definitely\s+|probably\s+|clearly\s+)*"
    r"(?:mine|bomb|trap|dangerous|unsafe)",
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
_UNDO_BEFORE = re.compile(r"\b(?:un-?flag(?:ging|ged)?|unmark)\b[^A-Za-z0-9]{0,4}$",
                          re.IGNORECASE)

_NEGATION_BEFORE = re.compile(
    rf"\b(?:{_AVOID_WORDS})"
    rf"(?:[^A-Za-z0-9]{{0,4}}[A-Za-z]{{1,7}})?"
    rf"[^A-Za-z0-9]{{0,12}}$",
    re.IGNORECASE,
)

_LOOKBACK = 24      # characters of context examined on either side


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
    """Every coordinate in `text` as (coord, is_choice, is_warning).

    `is_choice` means a voting word introduces it. `is_warning` means the
    writer is calling it a mine or telling us to stay away.
    """
    text = text or ""
    pattern = coordinate_pattern(rows, cols)
    vote_word = _vote_word_pattern()
    out = []
    for match in pattern.finditer(text):
        coord = f"{match.group(1).upper()}{match.group(2)}"
        before = text[max(0, match.start() - _LOOKBACK):match.start()]
        after = text[match.end():match.end() + _LOOKBACK]

        undo = bool(_UNDO_BEFORE.search(before))
        warning = not undo and (
            bool(_IS_MINE.match(after))
            or bool(_NEGATION_BEFORE.search(before))
            or bool(_DANGER_BEFORE.search(before))
            or bool(_DANGER_AFTER.match(after))
        )
        if undo:
            # Neither a vote nor a warning: it is a request about a mark.
            continue
        choice = bool(vote_word.search(before)) and not warning
        out.append((coord, choice, warning))
    return out


def parse_vote(text: str, rows: int, cols: int) -> str | None:
    """The one coordinate a reply is voting for, or None.

    In order:
      1. the last coordinate introduced by a voting word,
      2. the only coordinate mentioned, if there is exactly one,
      3. the last coordinate mentioned — people finish on their choice.
    Coordinates the writer is warning about are never candidates.
    """
    mentions = find_mentions(text, rows, cols)
    candidates = [(coord, choice) for coord, choice, warning in mentions
                  if not warning]
    if not candidates:
        return None

    chosen = [coord for coord, choice in candidates if choice]
    if chosen:
        return chosen[-1]

    distinct = {coord for coord, _ in candidates}
    if len(distinct) == 1:
        return candidates[0][0]

    return candidates[-1][0]


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

    Oldest first, so an account's earliest reply is its vote and the follower
    credited for a coordinate is whoever called it first. Coordinates that are
    already open are dropped rather than counted — a vote for a revealed cell
    is a misreading of the board, not a choice.
    """
    votes, first = {}, {}
    for reply in sorted(replies, key=lambda r: r.created_at or ""):
        if reply.did in votes:
            continue
        coord = parse_vote(reply.text, rows, cols)
        if coord is None or coord in unavailable:
            continue
        votes[reply.did] = coord
        first.setdefault(coord, reply)
    return votes, first


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
