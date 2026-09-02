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
    r"flag|flagging|flagged|careful|beware|steer clear of|mine at|bomb at"
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

        claims_mine = (bool(_IS_MINE.match(after))
                       or bool(_FLAG_BEFORE.search(before))
                       or bool(_DANGER_BEFORE.search(before))
                       or bool(_DANGER_AFTER.match(after)))
        warning = claims_mine or bool(_NEGATION_BEFORE.search(before))
        choice = bool(vote_word.search(before)) and not warning
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
            if out[i].is_flag or out[i].is_unflag:
                continue
            neighbours = [j for j in (i - 1, i + 1) if 0 <= j < len(out)]
            for j in neighbours:
                if out[j].is_flag and _joined(min(i, j), max(i, j)):
                    out[i] = Mention(out[i].coord, is_warning=True, is_flag=True)
                    changed = True
                    break
        if not changed:
            break
    return out


def parse_vote(text: str, rows: int, cols: int) -> str | None:
    """The one coordinate a reply is voting for, or None.

    In order:
      1. the last coordinate introduced by a voting word,
      2. the only coordinate mentioned, if there is exactly one,
      3. the last coordinate mentioned — people finish on their choice.
    Coordinates the writer is warning about are never candidates.
    """
    candidates = [m for m in find_mentions(text, rows, cols)
                  if not m.is_warning and not m.is_unflag]
    if not candidates:
        return None

    chosen = [m.coord for m in candidates if m.is_choice]
    if chosen:
        return chosen[-1]

    distinct = {m.coord for m in candidates}
    if len(distinct) == 1:
        return candidates[0].coord

    return candidates[-1].coord


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


def collect_flags(replies: list, resolved: set, rows: int, cols: int) -> tuple:
    """Flag and unflag claims across replies, as {coord: [Reply, ...]}.

    A person may flag several cells in one reply — unlike voting, where they
    get one say. Cells that are already open are ignored: a flag on a revealed
    cell is a misreading, not a prediction.
    """
    flags, unflags = {}, {}
    for reply in sorted(replies, key=lambda r: r.created_at or ""):
        flagged, unflagged = parse_flags(reply.text, rows, cols)
        for coord in flagged - resolved:
            flags.setdefault(coord, []).append(reply)
        for coord in unflagged - resolved:
            unflags.setdefault(coord, []).append(reply)
    return flags, unflags


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
    parsed = []
    for reply in ordered:
        coord = parse_vote(reply.text, rows, cols)
        if coord is None or coord in unavailable:
            continue
        parsed.append((reply, coord))

    # Later replies replace earlier ones from the same account.
    votes = {reply.did: coord for reply, coord in parsed}

    # Credit goes to whoever called the winning cell first — but only among
    # the people still voting for it, so someone who moved on doesn't get
    # credited for a coordinate they abandoned.
    first = {}
    for reply, coord in parsed:
        if votes.get(reply.did) == coord:
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
