"""The exam study planner: turns each upcoming test, quiz or exam into dated study sessions.

Everything here is arithmetic on synced data (no AI), so it costs nothing to run.

- **Stakes** set the size of a plan: a weekly quiz worth 2% gets two 25-minute sessions, a
  cumulative final ten hour-long ones spread over 18 days (tier table below).
- **Spacing:** sessions are spread out, wide gaps early and tight ones near the test (Cepeda et
  al. 2008; Dunlosky et al. 2013 rate distributed practice and practice testing "high utility").
- **Methods** decide what happens in each session (active recall, practice tests, blank-page
  recall, mixed practice, explaining); pacing (Pomodoro and gentler timers) is a separate choice.
- **A daily cap** keeps the total across every plan reasonable; overflow moves to an earlier free
  day, then trims the less important sessions.
- **Missed sessions** roll quietly into the next one: no overdue pile, no red badges (students
  with ADHD in the research said those make them quit).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from ..extensions import db
from ..models import Assignment, CalendarEvent, Deck, DeckTest, PracticeQuiz, StudyPlan, StudySession, utcnow
from ..utils import local_now, user_zone

# tier: (first session this many days before, number of sessions, minutes each)
TIERS = {
    "micro": (1, 1, 20),
    "low": (3, 2, 25),
    "medium": (6, 4, 40),
    "high": (10, 6, 50),
    "major": (14, 8, 60),
    "final": (18, 10, 60),
}
TIER_ORDER = list(TIERS)
TIER_LABELS = {"micro": "Tiny", "low": "Small", "medium": "Medium", "high": "Big", "major": "Major", "final": "Final"}
KIND_LABELS = {"final": "Final", "midterm": "Midterm", "test": "Test", "quiz": "Quiz"}
# How long a practice test should run, like the real thing.
TEST_MINUTES = {"quiz": 15, "test": 50, "midterm": 75, "final": 120}

STEM_COURSE = re.compile(r"\b(math|stat|stats|phys|physics|chem|chemistry|econ|cs|comp|engr|acct|calc|calculus|algebra)\b", re.I)


@dataclass(frozen=True)
class Method:
    key: str
    name: str
    blurb: str  # what you do
    evidence: str  # why, in a line
    best_for: str
    main: str  # the session role it fills sessions with


METHODS = {m.key: m for m in (
    Method("spaced", "Spaced recall + practice test",
           "Short sessions on different days: answer from memory, check, repeat the misses. "
           "One timed practice test a few days before, then fix what you missed.",
           "Practice testing and spaced practice are the only two study techniques rated \"high utility\" "
           "in the big review of the research (Dunlosky et al., 2013).",
           "Almost everything. The default.", "learn"),
    Method("recall", "Active recall",
           "Flashcards and self-quizzing every session: type the answer before you flip, and the ones "
           "you miss come back until you get them.",
           "Retrieval beat rereading by about 50% a week later (Roediger & Karpicke, 2006), and works "
           "for students with ADHD too (Knouse et al., 2016).",
           "Terms, definitions, formulas, vocab, weekly quizzes.", "learn"),
    Method("practice", "Practice tests",
           "Timed, closed-book tests sized like the real one, each followed by going over every miss.",
           "A practice test 1-6 days before the exam has one of the largest effects measured "
           "(Adesope et al., 2017), and it lowers test anxiety (Yang et al., 2023).",
           "Midterms, finals, timed exams, test anxiety.", "practice_test"),
    Method("blurt", "Blank-page recall",
           "Pick a topic, close everything and write all you remember for 10 minutes. Then open your "
           "notes and mark what you missed. Next time, start with the gaps.",
           "Writing out what you remember beat concept-mapping with the book open (Karpicke & Blunt, "
           "2011, Science). It only works with the checking step.",
           "Essay and short-answer exams, processes, history. Needs no cards at all.", "blurt"),
    Method("mixed", "Mixed practice",
           "Problems from different topics shuffled together, so you have to pick the method each time, "
           "like on the exam.",
           "Mixed beat blocked practice 74% to 42% on a test a month later (Rohrer et al., 2015).",
           "Math, stats, physics, chem, econ, CS.", "mixed"),
    Method("explain", "Explain it",
           "Explain each concept in plain words as if teaching a friend, without notes. Every spot you go "
           "vague is a gap: check it, fix it.",
           "Self-explanation and \"why is this true?\" questions both help (Bisra et al., 2018; Dunlosky "
           "et al., 2013).",
           "Concepts, mechanisms, theories, essay questions.", "explain"),
)}

PACINGS = {
    "25_5": ("Pomodoro 25 / 5", 25, 5),
    "50_10": ("Deep 50 / 10", 50, 10),
    "12_3": ("Short 12 / 3", 12, 3),
    "flow": ("Flowtime: break when focus dips", 0, 0),
    "none": ("No timer", 0, 0),
}

ROLES = {
    "pretest": ("Check, then study", "Start with a short check on every topic (guessing is fine), then spend the rest on what you missed."),
    "learn": ("Recall", "Answer from memory, check, and keep going until every card is right once."),
    "practice_test": ("Practice test", "Timed and closed-book, sized like the real one. Phone away."),
    "review": ("Fix the misses", "Go over every question you missed on the practice test until you'd get it right."),
    "misses": ("Last look", "A light pass over what you've missed so far. Stop early and sleep."),
    "blurt": ("Blank page", "Pick a topic and write everything you remember. Then check your notes and mark the gaps."),
    "explain": ("Explain it", "Explain each concept in plain words without notes; check and fix every vague spot."),
    "mixed": ("Mixed practice", "Problems from several topics, shuffled. Decide the method before you solve."),
    "cram": ("Quick pass", "Short on time: one retrieval pass over the most important material, then reread only the misses."),
}


# ---------------------------------------------------------------- stakes


def tier_for(kind: str, share: float | None, hints: dict | None = None) -> str:
    """How big a plan this test deserves, from its kind and its share of the grade."""
    hints = hints or {}
    if share and share > 0:
        tier = ("major" if share >= 0.20 else "high" if share >= 0.10 else "medium" if share >= 0.04
                else "low" if share >= 0.01 else "micro")
    else:
        tier = {"final": "major", "midterm": "high", "test": "medium", "quiz": "low"}.get(kind, "medium")
    lo, hi = {"final": ("high", "major"), "midterm": ("medium", "major"), "test": ("low", "major"),
              "quiz": ("micro", "high" if (share or 0) >= 0.10 else "medium")}.get(kind, ("micro", "major"))
    i = min(max(TIER_ORDER.index(tier), TIER_ORDER.index(lo)), TIER_ORDER.index(hi))
    if hints.get("retake"):
        i = max(TIER_ORDER.index("low"), i - 1)
    if hints.get("optional"):
        i = min(i, TIER_ORDER.index("low"))
    tier = TIER_ORDER[i]
    return "final" if kind == "final" and tier == "major" else tier


def recommend_method(course) -> str:
    text = f"{getattr(course, 'course_code', '') or ''} {getattr(course, 'name', '') or ''}"
    return "mixed" if STEM_COURSE.search(text) else "spaced"


# ---------------------------------------------------------------- the schedule for one test


def _round5(x: float) -> int:
    return int(5 * round(x / 5))


def local_day(dt: datetime | None, user) -> date | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(user_zone(user)).date()


def ideal_sessions(plan: StudyPlan, user, today: date) -> list[dict]:
    """The plan's sessions as if nothing else existed: [{day, minutes, role, protected}], earliest first.
    Some may be in the past; the caller decides what to do with those."""
    exam_day = local_day(plan.exam_at, user)
    if exam_day is None:
        return []
    w, n, minutes = TIERS.get(plan.tier, TIERS["medium"])
    d = (exam_day - today).days  # whole days from today to the test, today excluded
    if d <= 0:  # it's today (or past): one quick pass now
        return [{"day": today, "minutes": 30 if plan.tier in ("micro", "low") else 45, "role": "cram", "protected": True}]
    if d < w:  # found late: fewer, longer sessions, same total
        total = n * minutes
        n = min(n, max(1, d))
        minutes = min(90, max(20, _round5(total / n)))
        w = d
    offsets: list[int] = []
    for k in range(n):
        off = max(1, round(w * ((n - k) / n) ** 1.5))
        while off in offsets and off < w + 2:  # same day as another: one day earlier
            off += 1
        if off in offsets:
            continue  # nowhere to go: merge into that day
        offsets.append(off)
    offsets.sort(reverse=True)
    if len(offsets) < n:  # merged: share the minutes out
        minutes = min(90, _round5(n * minutes / len(offsets)))
    days = [exam_day - timedelta(days=o) for o in offsets]
    roles = _roles(plan.method, len(days), d, plan.tier, offsets)
    if len(days) == 1 and d <= 1:
        minutes = min(minutes, 60)  # the night before: one focused hour, then sleep
    out = []
    for day, role in zip(days, roles):
        m = minutes
        if role == "practice_test":
            m = max(minutes, min(120, TEST_MINUTES.get(plan.kind, 50)))
        elif role == "misses" and len(days) >= 3:
            m = min(minutes, 30)
        out.append({"day": day, "minutes": m, "role": role, "protected": role in ("practice_test", "misses", "cram")})
    return out


def _roles(method: str, n: int, d: int, tier: str, offsets: list[int]) -> list[str]:
    main = METHODS.get(method, METHODS["spaced"]).main
    if n == 1:
        return ["cram" if d <= 1 else main]
    roles = [main] * n
    if method == "explain":  # alternate explaining with recall
        roles = ["explain" if i % 2 == 0 else "learn" for i in range(n)]
    big = TIER_ORDER.index(tier) >= TIER_ORDER.index("medium")
    if big and d >= 3 and method in ("spaced", "recall", "practice", "mixed"):
        roles[0] = "pretest"
    roles[-1] = "misses"
    if big and n >= 3:
        # The practice test 3-4 days out (1-6 days before works best), the fix-up right after.
        candidates = [i for i in range(1, n - 1) if offsets[i] >= 2]
        if candidates:
            best = min(candidates, key=lambda i: (abs(offsets[i] - 3.5), -i))
            roles[best] = "practice_test"
            if best + 1 < n - 1:
                roles[best + 1] = "review"
    elif n == 2 and method == "practice":
        roles[0] = "practice_test"
    if method == "practice":
        # Practice-test focus: every other middle session is a test, each followed by its review.
        for i in range(1, n - 1):
            if roles[i] == main and (i == 1 or roles[i - 1] not in ("practice_test",)):
                roles[i] = "practice_test" if roles[i - 1] != "practice_test" else "review"
        for i in range(1, n - 1):
            if roles[i - 1] == "practice_test" and roles[i] == "practice_test":
                roles[i] = "review"
    return roles


# ---------------------------------------------------------------- every plan together


def active_plans(user_id: int) -> list[StudyPlan]:
    return db.session.scalars(select(StudyPlan).where(StudyPlan.user_id == user_id, StudyPlan.status == "active")
                              .order_by(StudyPlan.exam_at)).all()


def refresh(user) -> list[str]:
    """Bring plans in line with Canvas (moved dates, renamed items), close finished ones, and roll
    missed sessions forward. Returns quiet notes for the page ("Moved Tuesday's session to today")."""
    now = utcnow()
    today = local_now(user).date()
    notes: list[str] = []
    changed = False
    for plan in active_plans(user.id):
        source_when = None
        if plan.assignment_id:
            a = db.session.get(Assignment, plan.assignment_id)
            if a is not None:
                source_when = a.due_at
                if a.name and a.name != plan.title:
                    plan.title = a.name[:500]
        elif plan.event_id:
            e = db.session.get(CalendarEvent, plan.event_id)
            if e is not None:
                source_when = e.start_at
        if source_when and source_when != plan.exam_at:
            old = local_day(plan.exam_at, user)
            plan.exam_at = source_when
            if old and old != local_day(source_when, user):
                notes.append(f"{plan.title} moved to {local_day(source_when, user):%a %b %-d}; its plan moved with it.")
            changed = True
        if plan.exam_at and plan.exam_at < now - timedelta(hours=6):
            plan.status = "done"
            changed = True
            continue
        missed = db.session.scalars(select(StudySession).where(
            StudySession.plan_id == plan.id, StudySession.done_at.is_(None), StudySession.started_at.is_(None),
            StudySession.day < today.isoformat()).order_by(StudySession.day)).all()
        if missed:
            day = date.fromisoformat(missed[0].day)
            notes.append(f"Moved {day:%A}'s {plan.title} session forward." if len(missed) == 1
                         else f"Moved {len(missed)} missed {plan.title} sessions forward.")
            changed = True
    if changed:
        schedule(user)
    db.session.commit()
    return notes


def schedule(user, today: date | None = None) -> None:
    """(Re)build every active plan's future sessions together, within the daily cap. Sessions already
    done or started are kept and count toward their day; everything else is regenerated."""
    today = today or local_now(user).date()
    cap = max(30, min(user.study_minutes_per_day or 120, 720))
    plans = [p for p in active_plans(user.id) if p.exam_at]
    load: dict[date, int] = {}
    keep: dict[int, list[StudySession]] = {}
    for p in plans:
        keep[p.id] = []
        # Query, don't trust p.sessions: within one request it can still hold rows we replaced.
        for s in db.session.scalars(select(StudySession).where(StudySession.plan_id == p.id)).all():
            if s.done_at or s.started_at:
                keep[p.id].append(s)
                d = date.fromisoformat(s.day)
                load[d] = load.get(d, 0) + (s.minutes_done or s.minutes)
            else:
                db.session.delete(s)
    db.session.flush()
    big_test_days = {local_day(p.exam_at, user) for p in plans if TIER_ORDER.index(p.tier) >= TIER_ORDER.index("high")}

    # 1. What each plan wants, adjusted for sessions already done (ahead) or missed (behind).
    order = sorted(plans, key=lambda p: (-TIER_ORDER.index(p.tier), p.exam_at))  # higher stakes, then sooner
    wanted: list[dict] = []
    for rank, p in enumerate(order):
        ideal = ideal_sessions(p, user, today)
        exam_day = local_day(p.exam_at, user)
        done = len(keep[p.id])
        past = [x for x in ideal if x["day"] < today]
        future = [x for x in ideal if x["day"] >= today]
        if done > len(past):  # ahead of plan: drop that many of the earliest unprotected sessions
            extra = done - len(past)
            for x in list(future):
                if extra and not x["protected"]:
                    future.remove(x)
                    extra -= 1
        elif len(past) > done and future:  # behind: part of the missed time rides along with the next session
            behind = sum(x["minutes"] for x in past[done:])
            future[0] = {**future[0], "minutes": min(90, future[0]["minutes"] + behind // 2)}
        elif len(past) > done and exam_day and exam_day >= today:
            future = [{"day": today, "minutes": 30, "role": "cram", "protected": True}]
        taken = {date.fromisoformat(x.day) for x in keep[p.id]}
        for x in future:
            if x["day"] in taken:  # already studied for this test that day
                continue
            wanted.append({**x, "plan": p, "rank": rank, "exam_day": exam_day})

    def limit(x: dict, d: date) -> int:
        # At most 30 minutes for other tests on the day of a big test.
        return 30 if d in big_test_days and d != x["exam_day"] else cap

    def busy(d: date) -> int:
        return load.get(d, 0) + sum(x["minutes"] for x in wanted if x["day"] == d)

    def has_day(x: dict, d: date) -> bool:
        return any(y is not x and y["plan"] is x["plan"] and y["day"] == d for y in wanted)

    # 2. Fit each day under the cap: move the least important sessions to an earlier free day, then
    #    shorten them (never below 15 minutes), then drop them. Practice tests and the night-before
    #    review are never moved, shortened or dropped.
    for d in sorted({x["day"] for x in wanted}):
        here = lambda: sorted((x for x in wanted if x["day"] == d), key=lambda x: (x["protected"], -x["rank"]))  # noqa: E731
        over = lambda: busy(d) > cap or any(  # noqa: E731
            sum(y["minutes"] for y in wanted if y["day"] == d and y["exam_day"] != x["exam_day"]) + load.get(d, 0) > limit(x, d)
            for x in wanted if x["day"] == d)
        for x in here():
            if not over():
                break
            if x["protected"]:
                continue
            # ...but not ahead of the plan's first session (the warm-up check comes first).
            first = min((y["day"] for y in wanted if y["plan"] is x["plan"] and y is not x), default=today)
            for back in (1, 2, 3):
                alt = d - timedelta(days=back)
                if alt >= today and alt > first and not has_day(x, alt) and busy(alt) + x["minutes"] <= min(cap, limit(x, alt)):
                    x["day"] = alt
                    break
        for x in here():
            if not over() or x["protected"]:
                continue
            spare = cap - (busy(d) - x["minutes"])
            x["minutes"] = max(15, min(x["minutes"], 5 * (spare // 5)))
        for x in here():
            if not over():
                break
            if not x["protected"]:
                wanted.remove(x)

    # 3. Save.
    for p in order:
        mine = sorted((x for x in wanted if x["plan"] is p), key=lambda x: x["day"])
        for i, x in enumerate(mine):
            db.session.add(StudySession(plan_id=p.id, user_id=user.id, day=x["day"].isoformat(), position=i,
                                        role=x["role"], minutes=x["minutes"]))
        p.updated_at = utcnow()
    db.session.flush()
    for p in active_plans(user.id):
        db.session.expire(p, ["sessions"])


def day_load(user) -> dict[str, int]:
    """Planned minutes per local day across every active plan (for the "busy day" note)."""
    rows = db.session.execute(select(StudySession.day, StudySession.minutes).join(StudyPlan)
                              .where(StudySession.user_id == user.id, StudyPlan.status == "active")).all()
    out: dict[str, int] = {}
    for day, minutes in rows:
        out[day] = out.get(day, 0) + minutes
    return out


def create(user, *, title: str, kind: str, exam_at: datetime | None, course=None, assignment=None, event=None,
           share: float | None = None, hints: dict | None = None, method: str | None = None) -> StudyPlan:
    plan = StudyPlan(user_id=user.id, course_id=getattr(course, "id", None), assignment_id=getattr(assignment, "id", None),
                     event_id=getattr(event, "id", None), title=title[:500], kind=kind if kind in KIND_LABELS else "test",
                     exam_at=exam_at, share=share, tier=tier_for(kind, share, hints),
                     method=method if method in METHODS else recommend_method(course), pacing="25_5")
    db.session.add(plan)
    db.session.flush()
    schedule(user)
    return plan


# ---------------------------------------------------------------- what the page shows


def readiness(plan: StudyPlan) -> dict:
    """How ready the student looks, from their own practice: the latest practice test if there is one,
    else the share of the plan's cards they've recalled at least once."""
    test = db.session.scalar(select(DeckTest).where(DeckTest.plan_id == plan.id).order_by(DeckTest.created_at.desc()))
    decks = db.session.scalars(select(Deck).where(Deck.plan_id == plan.id)).all()
    cards = [c for d in decks for c in d.cards]
    learned = sum(1 for c in cards if (c.repetitions or 0) >= 1)
    if test and test.total:
        return {"percent": round(100 * test.score / test.total), "source": "practice test", "cards": len(cards)}
    if cards:
        return {"percent": round(100 * learned / len(cards)), "source": "cards recalled", "cards": len(cards)}
    return {"percent": None, "source": None, "cards": 0}


def materials(plan: StudyPlan) -> dict:
    decks = db.session.scalars(select(Deck).where(Deck.plan_id == plan.id).order_by(Deck.created_at)).all()
    quizzes = db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.plan_id == plan.id)
                                 .order_by(PracticeQuiz.created_at)).all()
    other_decks, other_quizzes = [], []
    if plan.course_id:
        other_decks = db.session.scalars(select(Deck).where(Deck.user_id == plan.user_id, Deck.course_id == plan.course_id,
                                                            Deck.plan_id.is_(None))).all()
        other_quizzes = db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.user_id == plan.user_id,
                                                                      PracticeQuiz.course_id == plan.course_id,
                                                                      PracticeQuiz.plan_id.is_(None))).all()
    return {"decks": decks, "quizzes": quizzes, "other_decks": other_decks, "other_quizzes": other_quizzes}


def needed_score(plan: StudyPlan) -> dict | None:
    """"Keep your 87%: you need about 84% on this", from the course's own grade math."""
    from . import grades

    if not plan.assignment_id:
        return None
    a = db.session.get(Assignment, plan.assignment_id)
    if a is None or not a.points_possible or a.score is not None or a.course is None:
        return None
    course = a.course
    current = grades.compute(course.groups, course.assignments, weighted=course.group_weighting)["percent"]
    if current is None:
        return None
    goal = math.floor(current)
    points = grades.needed_on(course.groups, course.assignments, a.id, goal, weighted=course.group_weighting)
    if points is None:
        return {"current": round(current, 1), "needed": None}
    return {"current": round(current, 1), "needed": round(100 * points / a.points_possible)}
