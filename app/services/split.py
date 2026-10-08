"""Brain Grade: one class grade split into what you know and what you turned in.

Every Canvas grade mixes two things: how a student did on tests and quizzes, and everything else
(homework, participation, getting work in on time). This pulls them apart, using the data a sync
already has and no AI:

* **Test grade**: the grade on tests, quizzes, midterms and finals alone (services/assessments.py
  decides which items those are; the student's own "is this a test?" answers win). Tests they
  never took (missing zeros) aren't counted here: they show up as missing work instead.
* **Everything else**: the grade on all the other graded work.
* **Lost to missing work**: how much higher the class grade would be if each grade for missing work
  (a zero, or the partial credit of a class's missing-work policy) had scored like the student's
  usual work in that group. Only Canvas's own missing flag counts; work the student marked done, or
  that Canvas excused, never does.
* **Lost to late penalties**: points Canvas's late policy took off (extension 1.5.1 and later).
* **Could cost**: missing work Canvas hasn't graded yet, which today's score leaves out; as zeros.
* **Points still on the table**: graded missing work Canvas still accepts (not locked, submitted
  online), with what each could bring back, biggest first; then ungraded missing work, with what it
  would cost if it's graded as a zero (turning that in protects the grade rather than raising it).

All the math goes through services/grades.py, so drops, weights and extra credit behave as in
Canvas. The two halves use the drops Canvas chose for the whole class (a drop rule never runs again
inside a half). Recovery is an estimate ("up to"), based on the student's own average, never a promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import undefer_group

from ..extensions import db
from ..models import Assignment, AssignmentGroup, Course, utcnow
from . import assessments, grades

MIN_TESTS = 2          # graded tests needed before a test grade means anything
MIN_TEST_SHARE = 0.15  # ...and the share of the grade they must carry
ONLINE = {"online_upload", "online_text_entry", "online_url", "media_recording", "student_annotation", "online_quiz",
          "discussion_topic"}


@dataclass
class Fix:
    assignment: Assignment
    # Percentage points of the class grade: for graded missing work, what turning it in at the
    # student's usual level could bring back; for ungraded missing work, what it would cost as a zero.
    # None when there's no average to go on.
    gain: float | None
    graded_zero: bool  # graded as missing work now (else Canvas hasn't graded it yet)

    @property
    def closes_at(self) -> datetime | None:
        return self.assignment.lock_at


@dataclass
class Split:
    status: str                    # ok / na (too few tests) / empty (nothing graded yet)
    canvas: float | None           # the class grade as Canvas computes it now
    tests: float | None = None
    other: float | None = None
    n_tests: int = 0               # graded tests and quizzes that count toward the grade
    missed_tests: int = 0          # tests graded as missing work (left out of the test grade)
    test_share: float = 0.0        # fraction of the grade that tests carry
    lost_missing: float = 0.0
    lost_late: float = 0.0
    could_cost: float = 0.0
    fixes: list[Fix] = field(default_factory=list)
    labels: dict = field(default_factory=dict)  # assignment id -> assessments.Judged
    n_missing: int = 0

    @property
    def gap(self) -> float:
        """How far the test grade sits above the class grade (positive: work, not knowledge, costs you)."""
        return round((self.tests or 0) - (self.canvas or 0), 2) if self.status == "ok" else 0.0

    @property
    def lost(self) -> float:
        return round(self.lost_missing + self.lost_late, 2)

    @property
    def message(self) -> str:
        if self.status == "empty":
            return "Nothing graded in this class yet."
        if self.status == "na":
            return "Not a test-based class (or not enough tests graded yet), so there's no separate test grade."
        if self.gap >= 2 and self.lost >= max(2.0, self.gap / 2):
            return f"Your tests say {self.tests:.1f}%. Missing and late work is what's pulling the class grade down."
        if (self.other or 0) - (self.tests or 0) >= 5:
            return "Your homework is carrying this class. The exam planner can help on the next test."
        if self.gap >= 2:
            return "Your tests are ahead of the rest of your graded work."
        if self.lost <= 0.05 and self.could_cost <= 0.05 and not self.fixes:
            return "Nothing lost to missing work, and your tests and other work line up."
        return "Both count. Here's the fastest fix." if self.fixes else "Both count."


def _graded(a: Assignment) -> bool:
    return a.score is not None and a.status == "graded" and not a.excused


def _counts(a: Assignment) -> bool:
    """Whether it can move the grade at all."""
    return (a.points_possible or 0) > 0 and not a.omit_from_final_grade and not a.excused


def _missing_zero(a: Assignment) -> bool:
    """Graded as missing work: Canvas's missing flag (its automatic missing-work policy can give
    partial credit) or a teacher-set missing status. The student's "done" always wins."""
    return _graded(a) and _counts(a) and not a.user_done and (a.missing or a.late_policy_status == "missing")


def _missing_ungraded(a: Assignment) -> bool:
    return a.status == "missing" and a.score is None and _counts(a) and not a.user_done


def _typical(assignments: list[Assignment]) -> tuple[dict, float | None]:
    """The student's own average (score / points) on real, graded work: per group, and overall."""
    per: dict = {}
    for a in assignments:
        if _graded(a) and not _missing_zero(a) and (a.points_possible or 0) > 0:
            e, p = per.get(a.group_canvas_id, (0.0, 0.0))
            per[a.group_canvas_id] = (e + float(a.score), p + float(a.points_possible))
    overall_e = sum(e for e, _ in per.values())
    overall_p = sum(p for _, p in per.values())
    return {g: e / p for g, (e, p) in per.items() if p > 0}, (overall_e / overall_p if overall_p else None)


def _pct(groups, assignments, weighted, what_if=None, drops=True) -> float | None:
    return grades.compute(groups, assignments, what_if, weighted, drops)["percent"]


def _open_online(a: Assignment, now: datetime) -> bool:
    return (a.lock_at is None or a.lock_at > now) and bool(set(a.submission_types or []) & ONLINE)


def _course_data(course: Course, with_detail: bool) -> tuple[list, list]:
    groups = db.session.scalars(select(AssignmentGroup).where(AssignmentGroup.course_id == course.id)).all()
    q = select(Assignment).where(Assignment.course_id == course.id)
    if with_detail:  # the test detector reads instructions and rubrics (encrypted, deferred)
        q = q.options(undefer_group("assignment_detail"))
    return groups, db.session.scalars(q).all()


def missing_work(course: Course, groups=None, assignments=None, now: datetime | None = None,
                 fixes_only: bool = False) -> dict:
    """The missing-work part alone (no test detection). fixes_only skips the totals (the dashboard)."""
    if groups is None or assignments is None:
        groups, assignments = _course_data(course, with_detail=False)
    now = now or utcnow()
    weighted = course.group_weighting
    out = {"canvas": None, "lost_missing": 0.0, "lost_late": 0.0, "could_cost": 0.0, "fixes": [], "n_missing": 0}
    zeros = [a for a in assignments if _missing_zero(a)]
    pending = [a for a in assignments if _missing_ungraded(a)]
    out["n_missing"] = len(zeros) + len(pending)
    if fixes_only and not any(_open_online(a, now) for a in zeros + pending):
        return out
    canvas = _pct(groups, assignments, weighted)
    out["canvas"] = canvas
    if canvas is None and not pending:
        return out
    per_group, overall = _typical(assignments)

    def usual(a):
        rate = per_group.get(a.group_canvas_id, overall)
        return None if rate is None or not a.points_possible else round(rate * float(a.points_possible), 4)

    if canvas is not None and not fixes_only:
        back = {a.id: usual(a) for a in zeros if usual(a) is not None}
        if back:
            out["lost_missing"] = max(0.0, round((_pct(groups, assignments, weighted, back) or 0) - canvas, 2))
        late = {a.id: float(a.score) + float(a.points_deducted) for a in assignments
                if _graded(a) and not _missing_zero(a) and (a.points_deducted or 0) > 0}
        if late:
            out["lost_late"] = max(0.0, round((_pct(groups, assignments, weighted, late) or 0) - canvas, 2))
        if pending:
            as_zero = _pct(groups, assignments, weighted, {a.id: 0.0 for a in pending})
            if as_zero is not None:
                out["could_cost"] = max(0.0, round(canvas - as_zero, 2))
    fixes = []
    zero_ids = {a.id for a in zeros}
    for a in zeros + pending:
        if not _open_online(a, now):
            continue
        guess = usual(a)
        if guess is None:
            fixes.append(Fix(a, None, a.id in zero_ids))
            continue
        if a.id in zero_ids:
            change = (_pct(groups, assignments, weighted, {a.id: guess}) or 0) - (canvas or 0)
        else:  # not graded yet: what it would cost as a zero
            change = (_pct(groups, assignments, weighted, {a.id: guess}) or 0) - (
                _pct(groups, assignments, weighted, {a.id: 0.0}) or 0)
        if change >= 0.05:  # a zero the class's drop rules already drop costs nothing
            fixes.append(Fix(a, round(change, 2), a.id in zero_ids))
    # Real gains first (graded missing work), then risks (not graded yet); biggest first in each.
    fixes.sort(key=lambda f: (not f.graded_zero, f.gain is None, -(f.gain or 0), f.assignment.lock_at or datetime.max))
    out["fixes"] = fixes
    return out


def split(user_id: int, course: Course, now: datetime | None = None) -> Split:
    groups, assignments = _course_data(course, with_detail=True)
    weighted = course.group_weighting
    work = missing_work(course, groups, assignments, now)
    labels = assessments.judge_course(user_id, course, assignments, groups)
    result = Split("empty", work["canvas"], lost_missing=work["lost_missing"], lost_late=work["lost_late"],
                   could_cost=work["could_cost"], fixes=work["fixes"], labels=labels, n_missing=work["n_missing"])
    if work["canvas"] is None:
        return result
    # Canvas picks drops on the whole class; each half then uses exactly what Canvas counted.
    full = grades.compute(groups, assignments, None, weighted)
    by_group = {g.group_id: g for g in full["groups"]}
    dropped = {i for g in full["groups"] for i in g.dropped}

    def counted(a) -> bool:
        g = by_group.get(a.group_canvas_id)
        return (_graded(a) and _counts(a) and a.id not in dropped and g is not None and g.possible > 0
                and (not full["weighted"] or (g.weight or 0) > 0))

    tests = [a for a in assignments if labels[a.id].is_test]
    others = [a for a in assignments if not labels[a.id].is_test]
    counted_tests = [a for a in tests if not _missing_zero(a) and a.id not in dropped]
    result.missed_tests = sum(1 for a in tests if _missing_zero(a) and a.id not in dropped)
    result.n_tests = sum(1 for a in counted_tests if counted(a))
    total_share = sum(j.share or 0 for j in labels.values())
    result.test_share = (sum(labels[a.id].share or 0 for a in tests) / total_share) if total_share else 0.0
    result.tests = _pct(groups, counted_tests, weighted, drops=False)
    result.other = _pct(groups, [a for a in others if a.id not in dropped], weighted, drops=False)
    ok = result.n_tests >= MIN_TESTS and result.test_share >= MIN_TEST_SHARE and result.tests is not None
    result.status = "ok" if ok else "na"
    return result


def points_on_the_table(user_id: int, limit: int = 8) -> list[tuple[Course, Fix]]:
    """Missing work still open in Canvas across the student's classes: real gains first, then risks."""
    from .. import queries
    from ..models import Assignment as A

    courses = [c for c in queries.visible_courses(user_id) if not (c.account is not None and c.account.lms == "ics")]
    if not courses:
        return []
    with_missing = set(db.session.scalars(select(A.course_id).where(
        A.course_id.in_([c.id for c in courses]), (A.missing.is_(True)) | (A.status == "missing")
        | (A.late_policy_status == "missing")).distinct()))
    out = []
    for course in courses:
        if course.id in with_missing:
            out += [(course, f) for f in missing_work(course, fixes_only=True)["fixes"]]
    out.sort(key=lambda cf: (not cf[1].graded_zero, cf[1].gain is None, -(cf[1].gain or 0)))
    return out[:limit]


LETTERS = ((93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"), (77, "C+"), (73, "C"), (70, "C-"), (67, "D+"),
           (63, "D"), (60, "D-"))


def letter(percent: float | None) -> str:
    """A common US letter scale (kept for reference; the share card uses the class's own letter)."""
    if percent is None:
        return "—"
    return next((l for cut, l in LETTERS if percent >= cut), "F")
