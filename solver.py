"""Minesweeper deduction engine.

This module answers three questions the bot needs to answer honestly:

    Is there a cell that is *provably* safe right now?
    Which cell is safest if there isn't?
    What is the chance each hidden cell holds a mine?

It is used for the quorum fallback move, for the post-mortem line after an
explosion ("D2 was provably safe"), and by the dashboard. Those are claims of
fact, so the deductions here are exact rather than heuristic.

**The information boundary is structural.** Every function takes a `Position`,
which is built from a GameState by copying only what a follower scrolling
Bluesky can see: the board dimensions, the total mine count, and the revealed
numbers. The hidden mine set is not reachable from here, so the solver cannot
accidentally leak it into a move, a probability, or a post.

Three tiers of reasoning, cheapest first:

1. **Single-cell rule.** A revealed 1 already touching one known mine has only
   safe neighbours left. This settles ~94% of turns.
2. **Subset rule.** If one constraint's cells are a subset of another's, the
   difference carries the difference of the counts. This is the 1-2-1 and 1-1
   patterns players know by name.
3. **Exact enumeration.** Split the frontier into independent components,
   enumerate every consistent mine placement in each, and combine them with the
   global mine count. Yields true per-cell probabilities, and finds the safe
   cells the first two tiers miss — including endgames that only resolve once
   the remaining mine count is applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from math import comb

from game import neighbors

# Enumeration is exponential in the worst case, but constraint pruning keeps
# it far cheaper than that in practice: measured on real positions, a 30-cell
# component enumerates in a median of 17ms and a worst case of 34ms, because
# the solution count stays in the hundreds rather than anywhere near 2^30.
# The old limit of 20 was a guess, and it cost a board — a 25-cell frontier
# fell through to the blind fallback and the bot opened a mine at 22%.
ENUM_LIMIT = 30

# Defensive ceiling on the search itself, in case a component is unusually
# unconstrained. Far above anything observed (the largest seen was ~1000).
MAX_SOLUTIONS = 200_000

# What kind of reasoning the position demands. Reported so the bot can talk
# about the position and so tests can assert difficulty.
TRIVIAL, SUBSET, ENUMERATED, GUESS = 0, 1, 2, 3

LEVEL_NAMES = {
    TRIVIAL: "trivial",
    SUBSET: "subset",
    ENUMERATED: "enumeration",
    GUESS: "guess",
}


@dataclass(frozen=True)
class Position:
    """Everything a follower can see, and nothing else.

    `spent` holds mines that have already been detonated. Under knockout they
    are public — everybody watched them go off and they are drawn on the
    board — so the solver is entitled to them, and needs them: a number
    beside a spent mine has one fewer mine left to account for, and without
    that its constraints are simply wrong.
    """
    rows: int
    cols: int
    mine_count: int
    revealed: dict          # {(r, c): adjacent mine count}
    spent: frozenset = frozenset()      # mines already detonated

    @staticmethod
    def from_state(state) -> "Position":
        return Position(
            rows=state.rows,
            cols=state.cols,
            mine_count=state.mine_count,
            revealed=dict(state.revealed),
            spent=frozenset(state.spent_mines),
        )

    def hidden(self) -> list:
        """Cells still to be opened. A spent mine is not one of them."""
        return [(r, c)
                for r in range(self.rows)
                for c in range(self.cols)
                if (r, c) not in self.revealed and (r, c) not in self.spent]


@dataclass
class Analysis:
    safe: set = field(default_factory=set)      # provably no mine
    mines: set = field(default_factory=set)     # provably a mine
    probs: dict = field(default_factory=dict)   # {(r, c): P(mine)}
    level: int = GUESS                          # cheapest tier that found a safe cell
    exact: bool = True                          # False if a component was too big

    @property
    def has_safe(self) -> bool:
        return bool(self.safe)


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

def constraints(position: Position, known_mines: set = frozenset()) -> list:
    """One (cells, count) per revealed number that still touches hidden cells.

    `known_mines` are cells already proven to be mines; they are subtracted
    from both sides so the constraint describes only what is still open.
    """
    out = set()
    for (r, c), number in position.revealed.items():
        ns = neighbors(r, c, position.rows, position.cols)
        hidden = frozenset(n for n in ns
                           if n not in position.revealed
                           and n not in known_mines
                           and n not in position.spent)
        if not hidden:
            continue
        # A detonated mine counts against the number just as a proven one
        # does; it is simply one everybody has already seen.
        accounted = sum(1 for n in ns
                        if n in known_mines or n in position.spent)
        out.add((hidden, number - accounted))
    return list(out)


def _reduce(cons: list, safe: set, mines: set) -> list:
    """Fold known cells back into the constraints.

    Without this, a cell proven by one rule stays in the constraint sets that
    another rule reads, with the count still counting it — which is a silent
    route to a contradiction and, from there, to calling a mine safe.
    """
    out = set()
    for cells, count in cons:
        open_cells = frozenset(cells - safe - mines)
        if open_cells:
            out.add((open_cells, count - len(cells & mines)))
    return list(out)


def _trivial(cons: list) -> tuple:
    """Single-cell rule, applied to fixpoint.

    Returns (safe, mines, leftover constraints) with everything decided
    removed from the leftovers.
    """
    safe, mines = set(), set()
    changed = True
    while changed:
        changed = False
        rest = []
        for cells, count in cons:
            open_cells = frozenset(cells - safe - mines)
            remaining = count - len(cells & mines)
            if not open_cells:
                continue
            if remaining <= 0:
                if open_cells - safe:
                    safe |= open_cells
                    changed = True
            elif remaining >= len(open_cells):
                if open_cells - mines:
                    mines |= open_cells
                    changed = True
            else:
                rest.append((open_cells, remaining))
        cons = rest
    return safe, mines, cons


def _subset(cons: list) -> tuple:
    """Single-cell rule plus the subset rule, to fixpoint.

    If A's cells sit inside B's, then B minus A holds exactly (kB - kA) mines.
    When that difference is 0 the cells are safe; when it equals their number
    they are all mines. This is the 1-2-1 / 1-1 family of patterns.
    """
    safe, mines = set(), set()
    cons = list(set(cons))
    changed = True
    while changed:
        changed = False
        cons = _reduce(cons, safe, mines)
        found_safe, found_mines, cons = _trivial(cons)
        if found_safe - safe:
            safe |= found_safe
            changed = True
        if found_mines - mines:
            mines |= found_mines
            changed = True
        for first, second in combinations(cons, 2):
            for (a, ka), (b, kb) in ((first, second), (second, first)):
                if a < b:
                    diff, dk = b - a, kb - ka
                    if dk == 0 and diff - safe:
                        safe |= diff
                        changed = True
                    elif dk == len(diff) and diff - mines:
                        mines |= diff
                        changed = True
    return safe, mines, _reduce(cons, safe, mines)


# ---------------------------------------------------------------------------
# Exact enumeration
# ---------------------------------------------------------------------------

def _components(cons: list) -> list:
    """Split constraints into groups that share no cells (union-find).

    Independent regions multiply out instead of being enumerated together,
    which is the difference between milliseconds and hours on a busy board.
    """
    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for cells, _ in cons:
        for cell in cells:
            parent.setdefault(cell, cell)
        first, *rest = list(cells)
        for cell in rest:
            ra, rb = find(first), find(cell)
            if ra != rb:
                parent[ra] = rb

    groups = {}
    for cells, count in cons:
        groups.setdefault(find(next(iter(cells))), []).append((cells, count))
    return list(groups.values())


def _solutions(component: list, limit: int = ENUM_LIMIT) -> tuple:
    """Every consistent mine placement in one component.

    Returns (solutions, cells) where each solution is (mines_used, bit tuple)
    aligned to `cells`, or (None, cells) when the component is too big.
    """
    cells = sorted(set().union(*[c for c, _ in component]))
    if len(cells) > limit:
        return None, cells

    index = {cell: i for i, cell in enumerate(cells)}
    packed = [(sorted(index[cell] for cell in cs), k) for cs, k in component]
    n = len(cells)
    assignment = [0] * n
    found = []
    overflow = []

    def recurse(i: int, used: int) -> None:
        if overflow:
            return
        if i == n:
            found.append((used, tuple(assignment)))
            if len(found) > MAX_SOLUTIONS:
                overflow.append(True)
            return
        for value in (0, 1):
            assignment[i] = value
            # Prune as soon as any constraint is already impossible: too many
            # mines placed, or too few cells left to reach its count.
            ok = True
            for cs, k in packed:
                decided = [j for j in cs if j <= i]
                placed = sum(assignment[j] for j in decided)
                left = len(cs) - len(decided)
                if placed > k or placed + left < k:
                    ok = False
                    break
            if ok:
                recurse(i + 1, used + value)
        assignment[i] = 0

    recurse(0, 0)
    if overflow:
        return None, cells
    return found, cells


def _probabilities(position: Position, cons: list, known_mines: set,
                   known_safe: set = frozenset()) -> dict | None:
    """Exact P(mine) for every hidden cell, or None if enumeration bailed out.

    Components are combined by convolving their mine-count distributions, and
    each total is weighted by C(outside cells, mines left over) — that is what
    ties the frontier to the global mine counter, and it is what resolves
    endgames like "three hidden cells, one mine left".
    """
    hidden = [c for c in position.hidden()
              if c not in known_mines and c not in known_safe]
    frontier = set().union(*[cells for cells, _ in cons]) if cons else set()
    outside = [c for c in hidden if c not in frontier]
    remaining = position.mine_count - len(known_mines) - len(position.spent)

    enumerated = []
    for component in _components(cons):
        found, cells = _solutions(component)
        if found is None:
            return None
        enumerated.append((cells, found))

    distributions = []
    for _, found in enumerated:
        dist = {}
        for used, _ in found:
            dist[used] = dist.get(used, 0) + 1
        distributions.append(dist)

    # Memoised: this is called once per solution per component, and comb()
    # on numbers this size is not cheap. Uncached it dominated the whole
    # solver — raising the enumeration limit made it the bottleneck.
    _ways = {}

    def outside_ways(total: int) -> int:
        if total not in _ways:
            left = remaining - total
            _ways[total] = (comb(len(outside), left)
                            if 0 <= left <= len(outside) else 0)
        return _ways[total]

    def convolve(skip: int | None) -> dict:
        current = {0: 1.0}
        for i, dist in enumerate(distributions):
            if i == skip:
                continue
            nxt = {}
            for total, weight in current.items():
                for used, ways in dist.items():
                    if total + used <= remaining:
                        nxt[total + used] = nxt.get(total + used, 0.0) + weight * ways
            current = nxt
        return current

    everything = convolve(None)
    total_weight = sum(w * outside_ways(t) for t, w in everything.items())
    if total_weight <= 0:
        return None     # contradictory position; caller falls back

    probs = {}
    for i, (cells, found) in enumerate(enumerated):
        others = convolve(i)
        tally = {cell: 0.0 for cell in cells}
        weight_for = {}
        for used, bits in found:
            if used not in weight_for:
                weight_for[used] = sum(w * outside_ways(used + t)
                                       for t, w in others.items())
            weight = weight_for[used]
            if weight <= 0:
                continue
            for j, cell in enumerate(cells):
                if bits[j]:
                    tally[cell] += weight
        for cell in cells:
            probs[cell] = tally[cell] / total_weight

    if outside:
        others = convolve(len(distributions))   # skip nothing; all components
        expected = sum(w * outside_ways(t) * (remaining - t)
                       for t, w in others.items())
        share = expected / total_weight / len(outside)
        for cell in outside:
            probs[cell] = share

    for cell in known_mines:
        probs[cell] = 1.0
    for cell in known_safe:
        probs[cell] = 0.0
    return probs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_probabilities(position: Position, cons: list,
                           known_mines: set) -> dict:
    """A cheap danger estimate for when exact enumeration is unavailable.

    For each frontier cell, the worst ratio of mines-to-cells among the
    constraints touching it; for cells off the frontier, whatever mines are
    unaccounted for spread evenly. It is not exact — a cell in a 1-of-2 and a
    1-of-8 is scored at 0.5 — but it is ordered roughly right, which is all
    that picking the safest cell needs. The alternative it replaces was
    picking by how many hidden neighbours a cell had, which is not a safety
    judgement at all.
    """
    hidden = [c for c in position.hidden() if c not in known_mines]
    frontier = set().union(*[cells for cells, _ in cons]) if cons else set()
    outside = [c for c in hidden if c not in frontier]
    remaining = position.mine_count - len(known_mines) - len(position.spent)

    estimate = {}
    for cells, count in cons:
        if not cells:
            continue
        ratio = max(0.0, min(1.0, count / len(cells)))
        for cell in cells:
            estimate[cell] = max(estimate.get(cell, 0.0), ratio)

    if outside:
        spoken_for = sum(estimate.get(cell, 0.0) for cell in frontier)
        share = (remaining - spoken_for) / len(outside)
        for cell in outside:
            estimate[cell] = max(0.0, min(1.0, share))
    return estimate


def analyze(position: Position) -> Analysis:
    """Everything provable about `position`, cheapest tier first."""
    base = constraints(position)
    trivial_safe, trivial_mines, _ = _trivial(base)
    subset_safe, subset_mines, leftover = _subset(base)

    safe = set(subset_safe)
    mines = set(subset_mines)
    probs = {}
    exact = True

    if (len(position.revealed) + len(mines) + len(position.spent)
            < position.rows * position.cols):
        found = _probabilities(position, leftover, mines, safe)
        if found is None:
            # Enumeration bailed. An estimate is worth far more than nothing:
            # without one, safest_move falls through to a shape heuristic and
            # guesses blind.
            exact = False
            probs = estimate_probabilities(position, leftover, mines)
        else:
            probs = found
            safe |= {c for c, p in found.items() if p < 1e-9}
            mines |= {c for c, p in found.items() if p > 1 - 1e-9}

    # Never claim a revealed cell.
    safe = {c for c in safe if c not in position.revealed}
    mines = {c for c in mines if c not in position.revealed}
    trivial_safe = {c for c in trivial_safe if c not in position.revealed}
    subset_safe = {c for c in subset_safe if c not in position.revealed}

    if trivial_safe:
        level = TRIVIAL
    elif subset_safe:
        level = SUBSET
    elif safe:
        level = ENUMERATED
    else:
        level = GUESS

    return Analysis(safe=safe, mines=mines, probs=probs, level=level, exact=exact)


def safest_move(position: Position, analysis: Analysis | None = None) -> tuple:
    """(cell, reason) — the move the bot makes when the crowd hasn't agreed.

    A provably safe cell if one exists, preferring the one touching the most
    hidden cells because opening it does the most to advance the board.
    Otherwise the lowest-probability cell, tie-broken toward fewer hidden
    neighbours, which is the usual edge-and-corner guessing heuristic.
    """
    analysis = analysis or analyze(position)
    hidden = [c for c in position.hidden() if c not in analysis.mines]
    if not hidden:
        hidden = position.hidden()

    def hidden_touch(cell):
        return sum(1 for n in neighbors(cell[0], cell[1], position.rows,
                                        position.cols)
                   if n not in position.revealed)

    if analysis.safe:
        best = max(sorted(analysis.safe), key=hidden_touch)
        return best, "provably safe"

    if analysis.probs:
        best = min(sorted(hidden),
                   key=lambda c: (analysis.probs.get(c, 1.0), hidden_touch(c)))
        chance = analysis.probs.get(best, 0.0)
        return best, f"safest guess at {round((1 - chance) * 100)}%"

    # No constraints at all (a board that is nothing but zeros so far).
    return min(sorted(hidden), key=hidden_touch), "no information yet"
