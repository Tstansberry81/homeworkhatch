"""Exam planner pages (Study > Exams): the tests found in Canvas, a study plan per test, and the
session player with its timer. Free on every plan: nothing here calls the AI."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import queries
from ..extensions import db
from ..models import AssessmentChoice, Assignment, CalendarEvent, Deck, DeckTest, PracticeQuiz, StudyPlan, StudySession, utcnow
from ..services import assessments, coins, planner
from ..utils import local_now, user_zone

bp = Blueprint("planner", __name__, url_prefix="/study/exams")

KINDS = ("final", "midterm", "test", "quiz")


def _plan(plan_id: int) -> StudyPlan:
    plan = db.session.get(StudyPlan, plan_id)
    if plan is None or plan.user_id != current_user.id:
        abort(404)
    return plan


def _session(session_id: int) -> StudySession:
    s = db.session.get(StudySession, session_id)
    if s is None or s.user_id != current_user.id:
        abort(404)
    return s


def _endpoint(name: str, **values) -> str | None:
    """A link to another part of the app, or None when that part isn't there (keeps pages working)."""
    return url_for(name, **values) if name in current_app.view_functions else None


def _planned_keys(plans) -> set[str]:
    keys = set()
    for p in plans:
        if p.assignment_id:
            keys.add(f"a:{p.assignment_id}")
        if p.event_id:
            keys.add(f"e:{p.event_id}")
    return keys


def _suggestions(found, plans) -> list:
    """Tests without a plan, one per real sitting: parts of one exam in the same hour are one test,
    a make-up sitting is dropped when its regular sitting is listed, and anything already over is
    left out."""
    planned = _planned_keys(plans)
    now = utcnow()
    regular = {(f.course.id, assessments.makeup_key(f.title)) for f in found if not f.hints.get("makeup")}
    out, seen = [], {}
    for f in found:
        if f.kind == "none" or f.key in planned or (f.event and f"e:{f.event.id}" in planned):
            continue
        if f.when is not None and f.when <= now:
            continue
        if f.hints.get("makeup") and (f.course.id, assessments.makeup_key(f.title)) in regular:
            continue
        sib = (f.course.id, f.kind, assessments.sibling_key(f.title))
        if sib in seen and f.when and seen[sib] and abs((f.when - seen[sib]).total_seconds()) <= 3600:
            continue
        seen[sib] = f.when
        out.append(f)
    return out


def _existing(found) -> StudyPlan | None:
    """The student's active plan for this test, if they already have one."""
    if found.assignment is not None:
        cond = StudyPlan.assignment_id == found.assignment.id
    else:
        cond = StudyPlan.event_id == found.event.id
    return db.session.scalar(select(StudyPlan).where(StudyPlan.user_id == current_user.id,
                                                     StudyPlan.status == "active", cond))


def _family_label(item: str) -> str:
    key = item[7:].split("|")[0].split("~")[0]
    return f"Everything like “{key.replace('#', 'N')}”"


@bp.app_template_global()
def day_label(day: str) -> str:
    """"Today", "Tomorrow", "Yesterday" or "Thu Oct 8" for a local "YYYY-MM-DD"."""
    d = date.fromisoformat(day)
    today = local_now(current_user).date()
    return {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((d - today).days, f"{d:%a %b %-d}")


def _from_local(day: str, at: str | None) -> datetime | None:
    try:
        d = date.fromisoformat(day)
        t = time.fromisoformat(at) if at else time(9, 0)
    except (TypeError, ValueError):
        return None
    if not 1970 <= d.year <= 2100:
        return None
    return datetime.combine(d, t, tzinfo=user_zone(current_user)).astimezone(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- the Exams page


@bp.route("/")
@login_required
def index():
    notes = planner.refresh(current_user)
    plans = planner.active_plans(current_user.id)
    found = assessments.find(current_user)
    suggestions = _suggestions(found, plans)
    today = local_now(current_user).date()
    week = [today + timedelta(days=i) for i in range(7)]
    sessions = db.session.scalars(select(StudySession).join(StudyPlan).where(
        StudySession.user_id == current_user.id, StudyPlan.status == "active",
        StudySession.day >= today.isoformat(), StudySession.day <= week[-1].isoformat())
        .order_by(StudySession.day, StudySession.position)).all()
    by_day: dict[str, list] = {}
    for s in sessions:
        by_day.setdefault(s.day, []).append(s)
    test_days: dict[str, list] = {}
    for p in plans:
        d = planner.local_day(p.exam_at, current_user)
        if d and today <= d <= week[-1]:
            test_days.setdefault(d.isoformat(), []).append(p)
    nope = db.session.scalars(select(AssessmentChoice).where(AssessmentChoice.user_id == current_user.id,
                                                             AssessmentChoice.kind == "none")
                              .order_by(AssessmentChoice.created_at.desc()).limit(20)).all()
    nope_titles = {}
    for c in nope:
        if c.item.startswith("a:"):
            a = db.session.get(Assignment, int(c.item[2:]))
            nope_titles[c.id] = a.name if a else "An assignment"
        elif c.item.startswith("e:"):
            e = db.session.get(CalendarEvent, int(c.item[2:]))
            nope_titles[c.id] = e.title if e else "A calendar event"
        else:
            nope_titles[c.id] = _family_label(c.item)
    past = db.session.scalars(select(StudyPlan).where(StudyPlan.user_id == current_user.id, StudyPlan.status == "done")
                              .order_by(StudyPlan.exam_at.desc()).limit(5)).all()
    return render_template(
        "planner/index.html", notes=notes, plans=plans, sure=[f for f in suggestions if f.confidence in ("high", "user")],
        ask=[f for f in suggestions if f.confidence == "medium"], by_day=by_day, test_days=test_days, week=week, today=today,
        load=planner.day_load(current_user), nope=nope, nope_titles=nope_titles, past=past,
        readiness={p.id: planner.readiness(p) for p in plans},
        courses=queries.visible_courses(current_user.id), why=assessments.why, roles=planner.ROLES)


@bp.route("/settings", methods=["POST"])
@login_required
def settings():
    try:
        minutes = int(request.form.get("minutes", 120))
    except ValueError:
        minutes = 120
    current_user.study_minutes_per_day = max(30, min(minutes, 480))
    planner.schedule(current_user)
    db.session.commit()
    flash(f"Up to {current_user.study_minutes_per_day} minutes a day. Plans rearranged.", "success")
    return redirect(url_for("planner.index"))


@bp.route("/plan", methods=["POST"])
@login_required
def create():
    item = request.form.get("item", "")
    if item:
        found = next((f for f in assessments.find(current_user) if f.key == item or
                      (f.event and f"e:{f.event.id}" == item)), None)
        if found is None:
            flash("That test isn't in your synced Canvas data any more.", "error")
            return redirect(url_for("planner.index"))
        existing = _existing(found)
        if existing:
            return redirect(url_for("planner.plan", plan_id=existing.id))
        if found.when is not None and found.when <= utcnow():
            flash(f"{found.title} has already happened.", "info")
            return redirect(url_for("planner.index"))
        plan = planner.create(current_user, title=found.title, kind=found.kind, exam_at=found.when, course=found.course,
                              assignment=found.assignment, event=found.event, share=found.share, hints=found.hints)
    else:  # by hand: a test that isn't in Canvas (or only in the syllabus)
        title = (request.form.get("title") or "").strip()
        exam_at = _from_local(request.form.get("date"), request.form.get("time"))
        if not title or exam_at is None:
            flash("Give the test a name and a date.", "error")
            return redirect(url_for("planner.index"))
        course = None
        if request.form.get("course_id"):
            try:
                course = queries.owned_course(current_user.id, int(request.form["course_id"]))
            except (ValueError, Exception):  # noqa: BLE001 - a bad id just means no course
                course = None
        kind = request.form.get("kind") if request.form.get("kind") in KINDS else "test"
        plan = planner.create(current_user, title=title, kind=kind, exam_at=exam_at, course=course)
    db.session.commit()
    return redirect(url_for("planner.plan", plan_id=plan.id))


@bp.route("/plan-all", methods=["POST"])
@login_required
def create_all():
    plans = planner.active_plans(current_user.id)
    made = 0
    for f in _suggestions(assessments.find(current_user), plans):
        if f.confidence in ("high", "user") and f.when and f.when > utcnow():
            planner.create(current_user, title=f.title, kind=f.kind, exam_at=f.when, course=f.course,
                           assignment=f.assignment, event=f.event, share=f.share, hints=f.hints)
            made += 1
    db.session.commit()
    flash(f"Made {made} study plan{'s' if made != 1 else ''}. Each one is yours to change." if made
          else "Nothing new to plan.", "success" if made else "info")
    return redirect(url_for("planner.index"))


@bp.route("/choice", methods=["POST"])
@login_required
def choice():
    """"Is this a test?" Yes (with a kind) or No, for one item or the whole series."""
    item = request.form.get("item", "")
    kind = request.form.get("kind", "none")
    if not (item.startswith(("a:", "e:")) and item[2:].isdigit()) or kind not in KINDS + ("none",):
        abort(400)
    found = next((f for f in assessments.find(current_user, include_none=True)
                  if f.key == item or (f.event and f"e:{f.event.id}" == item)), None)
    if found is None:
        abort(404)
    course_id = found.course.id
    key = f"family:{assessments.family_id(found.family, found.natural)}" if request.form.get("scope") == "family" else item
    row = db.session.scalar(select(AssessmentChoice).where(AssessmentChoice.user_id == current_user.id,
                                                           AssessmentChoice.course_id == course_id,
                                                           AssessmentChoice.item == key))
    if row is None:
        row = AssessmentChoice(user_id=current_user.id, course_id=course_id, item=key, kind=kind)
        db.session.add(row)
    row.kind = kind
    db.session.commit()
    if kind != "none" and request.form.get("plan"):
        existing = _existing(found)
        if existing:
            return redirect(url_for("planner.plan", plan_id=existing.id))
        if found.when is not None and found.when <= utcnow():
            flash(f"{found.title} has already happened.", "info")
            return redirect(url_for("planner.index"))
        plan = planner.create(current_user, title=found.title, kind=kind, exam_at=found.when, course=found.course,
                              assignment=found.assignment, event=found.event, share=found.share, hints=found.hints)
        db.session.commit()
        return redirect(url_for("planner.plan", plan_id=plan.id))
    if kind == "none":
        flash(f"Got it: {found.title} isn't a test.", "info")
    return redirect(url_for("planner.index"))


@bp.route("/choice/<int:choice_id>/delete", methods=["POST"])
@login_required
def undo_choice(choice_id: int):
    row = db.session.get(AssessmentChoice, choice_id)
    if row is None or row.user_id != current_user.id:
        abort(404)
    db.session.delete(row)
    db.session.commit()
    return redirect(url_for("planner.index"))


# ---------------------------------------------------------------- one plan


@bp.route("/<int:plan_id>", methods=["GET", "POST"])
@login_required
def plan(plan_id: int):
    p = _plan(plan_id)
    if request.method == "POST":
        action = request.form.get("action")
        reschedule = False
        if action == "method" and request.form.get("method") in planner.METHODS:
            p.method = request.form["method"]
            reschedule = True
        elif action == "pacing" and request.form.get("pacing") in planner.PACINGS:
            p.pacing = request.form["pacing"]
        elif action == "scope":
            p.scope = (request.form.get("scope") or "").strip()[:2000] or None
        elif action == "date":
            when = _from_local(request.form.get("date"), request.form.get("time"))
            if when:
                p.exam_at = when
                if p.status == "done" and when > utcnow():
                    p.status = "active"
                reschedule = True
        elif action == "tier" and request.form.get("tier") in planner.TIERS:
            p.tier = request.form["tier"]
            reschedule = True
        elif action in ("link_deck", "unlink_deck"):
            deck = db.session.get(Deck, int(request.form.get("deck_id") or 0))
            if deck and deck.user_id == current_user.id:
                deck.plan_id = p.id if action == "link_deck" else None
        elif action in ("link_quiz", "unlink_quiz"):
            quiz = db.session.get(PracticeQuiz, int(request.form.get("quiz_id") or 0))
            if quiz and quiz.user_id == current_user.id:
                quiz.plan_id = p.id if action == "link_quiz" else None
        if reschedule:
            planner.schedule(current_user)
        p.updated_at = utcnow()
        db.session.commit()
        back = request.form.get("next") or ""
        if back.startswith("/study/exams/session/") and "//" not in back:  # only back to a session page
            return redirect(back)
        return redirect(url_for("planner.plan", plan_id=p.id) + (request.form.get("anchor") or ""))
    zone = user_zone(current_user)
    exam_local = p.exam_at.replace(tzinfo=timezone.utc).astimezone(zone) if p.exam_at else None
    return render_template(
        "planner/plan.html", plan=p, methods=planner.METHODS, pacings=planner.PACINGS, roles=planner.ROLES,
        tiers=planner.TIER_LABELS, kinds=planner.KIND_LABELS, today=local_now(current_user).date(),
        exam_local=exam_local, readiness=planner.readiness(p), materials=planner.materials(p),
        upcoming=bool(p.exam_at and p.exam_at > utcnow()),
        needed=planner.needed_score(p), recommended=planner.recommend_method(p.course),
        generate_deck=_endpoint("study.generate", course=p.course_id, output="deck", mode="course", plan=p.id)
        if p.course_id else None,
        import_url=_endpoint("study.import_deck", plan=p.id), new_deck_url=_endpoint("study.new_deck", plan=p.id),
        new_quiz_url=_endpoint("study.new_quiz"))


@bp.route("/<int:plan_id>/delete", methods=["POST"])
@login_required
def delete(plan_id: int):
    p = _plan(plan_id)
    title = p.title
    db.session.delete(p)
    db.session.flush()
    planner.schedule(current_user)
    db.session.commit()
    flash(f"Removed the plan for {title}. Your cards and quizzes are still in Study.", "info")
    return redirect(url_for("planner.index"))


# ---------------------------------------------------------------- a study session


def _tools(p: StudyPlan, s: StudySession) -> dict:
    """What a session's buttons open. Learn and Test work on the decks linked to the plan."""
    decks = [d for d in db.session.scalars(select(Deck).where(Deck.plan_id == p.id)) if d.cards]
    quizzes = db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.plan_id == p.id)).all()
    test_minutes = planner.TEST_MINUTES.get(p.kind, 50)
    tools = {"decks": decks, "quizzes": quizzes}
    if decks:
        tools["learn"] = _endpoint("study.learn", plan=p.id, session=s.id)
        tools["learn_starred"] = _endpoint("study.learn", plan=p.id, session=s.id, starred=1)
        last = db.session.scalar(select(DeckTest).where(DeckTest.plan_id == p.id).order_by(DeckTest.created_at.desc()))
        missed = [a.get("card_id") for a in (last.answers or []) if not a.get("correct")] if last else []
        missed = [int(c) for c in missed if isinstance(c, int) or str(c).isdigit()][:60]
        if missed:
            tools["learn_missed"] = _endpoint("study.learn", card=missed, session=s.id)
            tools["missed_count"] = len(missed)
        # The opening check is ungraded ("guessing is fine"): it never counts as readiness.
        tools["pretest"] = _endpoint("study.test_mode", plan=p.id, session=s.id, n=12, check=1)
        tools["test"] = _endpoint("study.test_mode", plan=p.id, session=s.id, n=10 if p.kind == "quiz" else 30,
                                  minutes=test_minutes)
    if quizzes:
        tools["quiz"] = _endpoint("study.take_quiz", quiz_id=quizzes[0].id)
    return tools


@bp.route("/session/<int:session_id>")
@login_required
def session(session_id: int):
    s = _session(session_id)
    p = s.plan
    pacing = planner.PACINGS.get(p.pacing, planner.PACINGS["25_5"])
    return render_template("planner/session.html", s=s, plan=p, role=planner.ROLES.get(s.role, (s.role, "")),
                           method=planner.METHODS.get(p.method, planner.METHODS["spaced"]), pacing_key=p.pacing,
                           pacing=pacing, pacings=planner.PACINGS, tools=_tools(p, s), today=local_now(current_user).date(),
                           plan_url=url_for("planner.plan", plan_id=p.id))


def _daily_award(amount: int, reason: str, kind: str, session_id: int, per_day: int = 3) -> None:
    """Coins for studying: once per session, and at most per_day sessions a day (plans can be made
    and deleted freely, so per-session alone could be farmed)."""
    from sqlalchemy import func

    from ..models import CoinTransaction

    today = local_now(current_user).date().isoformat()
    paid_today = db.session.scalar(select(func.count(CoinTransaction.id)).where(
        CoinTransaction.user_id == current_user.id, CoinTransaction.ref.like(f"{kind}:{today}:%")))
    if paid_today < per_day:
        coins.award(current_user.id, amount, reason, f"{kind}:{today}:{session_id}")


@bp.route("/session/<int:session_id>/start", methods=["POST"])
@login_required
def start(session_id: int):
    s = _session(session_id)
    if s.started_at is None:
        s.started_at = utcnow()
        # Starting is the hard part: a coin for showing up (a few times a day at most).
        _daily_award(1, "Started a study session", "session-start", s.id)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/session/<int:session_id>/notes", methods=["POST"])
@login_required
def notes(session_id: int):
    s = _session(session_id)
    payload = request.get_json(silent=True) or request.form  # a beacon sends a form when the page closes
    s.notes = str(payload.get("notes") or "")[:20000] or None
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/session/<int:session_id>/done", methods=["POST"])
@login_required
def done(session_id: int):
    s = _session(session_id)
    payload = request.get_json(silent=True) or request.form
    try:
        minutes = int(payload.get("minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if request.form.get("undo") or payload.get("undo"):
        s.done_at = None
    else:
        s.done_at = s.done_at or utcnow()
        s.started_at = s.started_at or s.done_at
        s.minutes_done = max(s.minutes_done or 0, min(max(minutes, 0), 600))
        notes = payload.get("notes")
        if notes is not None:  # the blank page's last words, sent with "I'm done"
            s.notes = str(notes)[:20000] or None
        if s.minutes_done >= 10 or (s.done_at - s.started_at) >= timedelta(minutes=10):  # some real studying
            _daily_award(3, "Finished a study session", "session-done", s.id)
    db.session.commit()
    if request.is_json:
        return jsonify({"ok": True, "next": url_for("planner.plan", plan_id=s.plan_id)})
    flash("Session done. Nice." if s.done_at else "Marked as not done.", "success")
    return redirect(url_for("planner.plan", plan_id=s.plan_id))
