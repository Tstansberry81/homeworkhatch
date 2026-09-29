"""Study planner: spreads upcoming work across the days before each deadline.

Deterministic on purpose, so the plan is predictable and instant: earliest deadline
first, each item's estimated minutes split over the days available before it's due,
never exceeding the student's daily study budget. Anything that can't fit is flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..models import Assignment
from ..utils import to_local
from .coins import work_type

HORIZON_DAYS = 14


def estimate_minutes(a: Assignment) -> int:
    kind = work_type(a)
    if kind == "test":
        return 180
    if kind == "quiz":
        return 60
    points = a.points_possible or 10
    return int(max(20, min(240, 25 + points * 1.2)))


@dataclass
class PlanItem:
    assignment: Assignment
    minutes: int
    due_local: datetime | None


@dataclass
class PlanDay:
    day: date
    capacity: int
    items: list[PlanItem] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(i.minutes for i in self.items)


def build_plan(assignments: list[Assignment], today: date, minutes_per_day: int, user=None) -> dict:
    minutes_per_day = max(15, min(int(minutes_per_day or 90), 12 * 60))
    days = [PlanDay(today + timedelta(days=i), minutes_per_day) for i in range(HORIZON_DAYS)]
    todo = []
    for a in assignments:
        if a.due_at is None:
            continue
        status = a.effective_status
        # In-class exams and quizzes have nothing to submit online but need the most prep;
        # other in-class items (participation, etc.) don't get study time.
        in_class_test = status == "no_submission" and work_type(a) in {"test", "quiz"}
        if status not in {"upcoming", "past_due", "missing"} and not in_class_test:
            continue
        due_local = to_local(a.due_at, user)
        todo.append((due_local, a))
    todo.sort(key=lambda t: t[0])

    at_risk = []
    for due_local, a in todo:
        need = estimate_minutes(a)
        due_day = due_local.date()
        # Finish by the day before (or the same day for items due late in the day).
        last = due_day if due_local.hour >= 17 else due_day - timedelta(days=1)
        window = [d for d in days if d.day <= last]
        if not window:  # due today/overdue: whatever fits today
            window = days[:1]
        remaining = need
        per_day = max(15, -(-need // len(window)))
        for d in window:
            if remaining <= 0:
                break
            free = d.capacity - d.used
            if free <= 0:
                continue
            chunk = min(per_day, free, remaining)
            d.items.append(PlanItem(a, chunk, due_local))
            remaining -= chunk
        # Second pass: pour leftovers into any free time in the window.
        for d in window:
            if remaining <= 0:
                break
            free = d.capacity - d.used
            if free > 0:
                chunk = min(free, remaining)
                d.items.append(PlanItem(a, chunk, due_local))
                remaining -= chunk
        if remaining > 0:
            at_risk.append({"assignment": a, "short_minutes": remaining, "due_local": due_local})

    # Merge split entries for the same assignment on the same day.
    for d in days:
        merged: dict[int, PlanItem] = {}
        for item in d.items:
            if item.assignment.id in merged:
                merged[item.assignment.id].minutes += item.minutes
            else:
                merged[item.assignment.id] = item
        d.items = list(merged.values())
    return {"days": days, "at_risk": at_risk, "minutes_per_day": minutes_per_day}
