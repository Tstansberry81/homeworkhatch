"""Course grade math, matching how Canvas computes a student's current score.

- Only graded (or hypothetically scored) work counts; excused work is ignored, and
  zero-point items count only as extra credit.
- Within a group, "drop lowest/highest N" removes the scores that make the group
  percentage highest (lowest) — Canvas picks the combination, not simply the lowest
  percentages, because assignments can be worth different points.
- Weighted courses average group percentages by weight, re-normalized over the groups
  that have graded work. Unweighted courses divide total points earned by points possible.

`what_if` overrides let a student try hypothetical scores for any assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ..models import Assignment, AssignmentGroup


@dataclass
class Scored:
    assignment_id: int
    score: float
    possible: float
    hypothetical: bool = False


@dataclass
class GroupResult:
    group_id: str | None
    name: str
    weight: float | None
    earned: float
    possible: float
    dropped: list[int]
    count: int

    @property
    def percent(self) -> float | None:
        return None if self.possible <= 0 else 100 * self.earned / self.possible


EXHAUSTIVE_LIMIT = 5000  # combinations to try before falling back to a greedy choice


def _apply_drops(items: list[Scored], drop_lowest: int, drop_highest: int,
                 never_drop: set[int] | None = None) -> tuple[list[Scored], list[Scored]]:
    """Return (kept, dropped), choosing drops the way Canvas does. `never_drop` ids are never dropped."""
    protected = [s for s in items if never_drop and s.assignment_id in never_drop]
    items = [s for s in items if not (never_drop and s.assignment_id in never_drop)]
    kept, dropped = _drops(items, drop_lowest, drop_highest)
    return kept + protected, dropped


def _drops(items: list[Scored], drop_lowest: int, drop_highest: int) -> tuple[list[Scored], list[Scored]]:
    from math import comb

    n = len(items)
    drop_lowest = max(0, min(drop_lowest, n - 1)) if n else 0
    drop_highest = max(0, min(drop_highest, n - 1 - drop_lowest)) if n else 0
    kept = list(items)
    dropped: list[Scored] = []

    def pct(group: list[Scored]) -> float:
        possible = sum(s.possible for s in group)
        return (sum(s.score for s in group) / possible) if possible > 0 else 0.0

    for count, keep_best in ((drop_lowest, True), (drop_highest, False)):
        if count <= 0 or len(kept) <= count:
            continue
        if comb(len(kept), count) <= EXHAUSTIVE_LIMIT:  # exhaustive while it's cheap
            best = None
            for combo in combinations(range(len(kept)), count):
                rest = [s for i, s in enumerate(kept) if i not in combo]
                value = pct(rest)
                if best is None or (value > best[0] if keep_best else value < best[0]):
                    best = (value, combo)
            combo = set(best[1])
        else:  # large groups: greedy by percentage
            order = sorted(range(len(kept)), key=lambda i: (kept[i].score / kept[i].possible) if kept[i].possible else 0)
            combo = set(order[:count] if keep_best else order[-count:])
        dropped += [s for i, s in enumerate(kept) if i in combo]
        kept = [s for i, s in enumerate(kept) if i not in combo]
    return kept, dropped


def compute(groups: list[AssignmentGroup], assignments: list[Assignment],
            what_if: dict[int, float] | None = None, weighted: bool | None = None) -> dict:
    """`weighted` is Canvas's "weight final grade based on assignment groups" setting; when
    unknown (None) it's inferred from whether any group has a weight."""
    what_if = what_if or {}
    by_group: dict[str | None, list[Scored]] = {}
    for a in assignments:
        if a.excused or a.points_possible is None or a.points_possible < 0 or getattr(a, "omit_from_final_grade", False):
            continue
        possible = float(a.points_possible)
        if a.id in what_if and what_if[a.id] is not None:
            scored = Scored(a.id, float(what_if[a.id]), possible, hypothetical=True)
        elif a.score is not None and a.status == "graded":
            scored = Scored(a.id, float(a.score), possible)
        else:
            continue
        if possible == 0 and scored.score <= 0:
            continue  # zero-point items only matter as extra credit
        by_group.setdefault(a.group_canvas_id, []).append(scored)

    known = {g.canvas_id: g for g in groups}
    canvas_to_id = {a.canvas_id: a.id for a in assignments}
    results: list[GroupResult] = []
    for group_id in list(known) + [gid for gid in by_group if gid not in known]:
        g = known.get(group_id)
        items = by_group.get(group_id, [])
        never = {canvas_to_id[c] for c in (getattr(g, "never_drop", None) or []) if c in canvas_to_id} if g else set()
        kept, dropped = _apply_drops(items, g.drop_lowest if g else 0, g.drop_highest if g else 0, never)
        results.append(GroupResult(group_id, g.name if g else "Other", g.weight if g else None,
                                   sum(s.score for s in kept), sum(s.possible for s in kept),
                                   [s.assignment_id for s in dropped], len(items)))

    if weighted is None:
        weighted = any((r.weight or 0) > 0 for r in results)
    if weighted:
        active = [r for r in results if r.possible > 0 and (r.weight or 0) > 0]
        total_weight = sum(r.weight for r in active)
        # Canvas scales up when the graded groups' weights total under 100%, but not down when
        # they total more (extra-credit groups can push a grade past 100%).
        divisor = min(total_weight, 100.0)
        percent = (sum(r.percent * r.weight for r in active) / divisor) if divisor else None
    else:
        earned = sum(r.earned for r in results)
        possible = sum(r.possible for r in results)
        percent = (100 * earned / possible) if possible else None
    return {"percent": None if percent is None else round(percent, 2), "weighted": weighted, "groups": results}


def needed_on(groups, assignments, target_assignment_id: int, goal_percent: float,
              weighted: bool | None = None) -> float | None:
    """Smallest score on one assignment that reaches goal_percent overall (None if impossible)."""
    target = next((a for a in assignments if a.id == target_assignment_id), None)
    if target is None or not target.points_possible:
        return None
    lo, hi = 0.0, float(target.points_possible) * 1.5  # allow some extra credit headroom
    best = compute(groups, assignments, {target_assignment_id: hi}, weighted)["percent"]
    if best is None or best < goal_percent:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        value = compute(groups, assignments, {target_assignment_id: mid}, weighted)["percent"] or 0
        if value >= goal_percent:
            hi = mid
        else:
            lo = mid
    return round(hi, 2)
