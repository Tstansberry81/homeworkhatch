"""The exam study planner: finding tests, sizing and spacing plans, the daily cap, missed sessions
rolling forward, and the pages."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.extensions import db
from app.models import (AssessmentChoice, Assignment, CoinTransaction, Course, Deck, StudyPlan, StudySession, User,
                        utcnow)
from app.services import assessments, planner
from app.utils import local_now

from .conftest import login, make_user

USER = SimpleNamespace(id=0, timezone="America/New_York", study_minutes_per_day=120)
TODAY = date(2026, 10, 2)


def _plan(days: int, tier="medium", kind="test", method="spaced") -> SimpleNamespace:
    exam = datetime(TODAY.year, TODAY.month, TODAY.day, 18) + timedelta(days=days)  # 2 PM New York
    return SimpleNamespace(exam_at=exam, tier=tier, kind=kind, method=method)


def test_stakes_set_the_size_of_a_plan():
    assert planner.tier_for("quiz", 0.02) == "low"
    assert planner.tier_for("quiz", 0.005) == "micro"
    assert planner.tier_for("quiz", 0.12) == "high", "a quiz worth 12% gets a real plan"
    assert planner.tier_for("quiz", 0.30) == "high", "...but quizzes stop at high"
    assert planner.tier_for("midterm", 0.25) == "major"
    assert planner.tier_for("midterm", None) == "high"
    assert planner.tier_for("final", 0.30) == "final"
    assert planner.tier_for("final", 0.02) == "high", "finals are never small"
    assert planner.tier_for("test", 0.20, {"retake": True}) == "high"
    assert planner.tier_for("test", 0.20, {"optional": True}) == "low"


def test_sessions_are_spaced_out_and_end_with_a_light_review():
    for tier, (w, n, _m) in planner.TIERS.items():
        for days in (0, 1, 2, 3, 5, 7, 14, 30):
            sessions = planner.ideal_sessions(_plan(days, tier), USER, TODAY)
            exam_day = TODAY + timedelta(days=days)
            assert sessions, (tier, days)
            assert all(s["day"] >= TODAY for s in sessions), "never in the past"
            assert all(s["day"] < exam_day for s in sessions) or days == 0, "never on test day (unless it's today)"
            assert len({s["day"] for s in sessions}) == len(sessions), "one session a day per test"
            assert len(sessions) <= n
            assert all(15 <= s["minutes"] <= 120 for s in sessions)
            if days >= w:
                assert (exam_day - sessions[0]["day"]).days == w, "the plan starts W days out"
    big = planner.ideal_sessions(_plan(14, "major"), USER, TODAY)
    roles = [s["role"] for s in big]
    assert roles[0] == "pretest" and roles[-1] == "misses"
    test_day = big[roles.index("practice_test")]["day"]
    assert 2 <= (TODAY + timedelta(days=14) - test_day).days <= 4, "the practice test lands 2-4 days out"
    assert roles[roles.index("practice_test") + 1] == "review", "and its misses get fixed next"
    assert big[-1]["minutes"] <= 30, "the night before stays light"
    gaps = [(b["day"] - a["day"]).days for a, b in zip(big, big[1:])]
    assert gaps[0] >= gaps[-1], "wide gaps early, tight near the test"
    small = planner.ideal_sessions(_plan(7, "low", "quiz"), USER, TODAY)
    assert [s["role"] for s in small] == ["learn", "misses"], "a small quiz gets no warm-up check"
    assert [s["role"] for s in planner.ideal_sessions(_plan(1, "high"), USER, TODAY)] == ["cram"]
    assert planner.ideal_sessions(_plan(1, "high"), USER, TODAY)[0]["minutes"] <= 60


def test_methods_change_what_happens_in_sessions():
    roles = lambda m: [s["role"] for s in planner.ideal_sessions(_plan(14, "major", method=m), USER, TODAY)]  # noqa: E731
    assert "blurt" in roles("blurt") and "pretest" not in roles("blurt")
    assert "explain" in roles("explain") and "learn" in roles("explain")
    assert "mixed" in roles("mixed")
    assert roles("practice").count("practice_test") >= 2
    assert planner.recommend_method(SimpleNamespace(course_code="MATH 101", name="Calculus I")) == "mixed"
    assert planner.recommend_method(SimpleNamespace(course_code="HIST 2150", name="US History")) == "spaced"


def _exam(user, course, title, days, kind="midterm", tier=None, **kw):
    exam_at = utcnow() + timedelta(days=days)
    return planner.create(user, title=title, kind=kind, exam_at=exam_at, course=course, **kw) if tier is None else \
        _with_tier(planner.create(user, title=title, kind=kind, exam_at=exam_at, course=course, **kw), tier, user)


def _with_tier(plan, tier, user):
    plan.tier = tier
    planner.schedule(user)
    return plan


def test_daily_cap_across_plans_and_missed_sessions_roll_forward(app):
    user = make_user(study_minutes_per_day=60)
    a = _exam(user, None, "Bio midterm", 8, tier="major")
    b = _exam(user, None, "Chem midterm", 9, tier="major")
    db.session.commit()
    load = planner.day_load(user)
    over = {d: m for d, m in load.items() if m > 60}
    protected = {s.day for p in (a, b) for s in p.sessions if s.role in ("practice_test", "misses", "cram")}
    assert set(over) <= protected, f"only protected sessions may go over the cap: {over}"

    # Miss yesterday's session: it disappears from the past and its time rides along with the next one.
    first = a.sessions[0]
    yesterday = (local_now(user).date() - timedelta(days=1)).isoformat()
    first.day = yesterday
    db.session.commit()
    notes = planner.refresh(user)
    assert notes and "Moved" in notes[0]
    assert not db.session.scalars(select(StudySession).where(StudySession.user_id == user.id, StudySession.day < local_now(user).date().isoformat(),
                                                             StudySession.done_at.is_(None))).all(), "no overdue pile"

    # Done sessions are kept, and being ahead drops a later session instead of adding one.
    s = a.sessions[0]
    s.done_at, s.minutes_done = utcnow(), 40
    db.session.commit()
    before = len(a.sessions)
    planner.schedule(user)
    db.session.commit()
    db.session.refresh(a)
    assert s in a.sessions and s.done_at is not None
    assert len(a.sessions) <= before

    # A test that has passed closes its plan.
    a.exam_at = utcnow() - timedelta(days=1)
    db.session.commit()
    planner.refresh(user)
    assert db.session.get(StudyPlan, a.id).status == "done"


def test_finds_the_midterm_but_not_the_homework(app, synced_user):
    found = assessments.find(synced_user)
    names = {f.title: f for f in found}
    assert "Midterm Exam" in names and names["Midterm Exam"].kind == "midterm"
    assert names["Midterm Exam"].confidence == "high" and names["Midterm Exam"].share
    assert "HW 3" not in names
    assert "Review session" not in names, "a review session is not the test"
    # "Not a test" for the whole series hides it; undoing brings it back.
    course = db.session.scalar(select(Course).where(Course.user_id == synced_user.id, Course.course_code == "MATH 101-01"))
    db.session.add(AssessmentChoice(user_id=synced_user.id, course_id=course.id,
                                    item=f"family:{assessments.family_id(assessments.stem('Midterm Exam'), 'midterm')}",
                                    kind="none"))
    db.session.commit()
    assert "Midterm Exam" not in {f.title for f in assessments.find(synced_user)}


def test_exam_pages_plan_sessions_and_coins(app, synced_user, client):
    page = client.get("/study/exams/").get_data(as_text=True)
    assert "Midterm Exam" in page and "Plan it" in page and "HW 3" not in page
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    r = client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    assert r.status_code == 302
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.user_id == synced_user.id))
    assert plan.assignment_id == midterm.id and plan.kind == "midterm" and plan.sessions
    assert plan.method == "mixed", "a math class gets mixed practice"
    page = client.get(f"/study/exams/{plan.id}").get_data(as_text=True)
    assert "Your sessions" in page and "How you'll study" in page and "Worth" in page
    # Same test again: no second plan.
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    assert db.session.query(StudyPlan).count() == 1
    # Switch methods and timers.
    client.post(f"/study/exams/{plan.id}", data={"action": "method", "method": "blurt"})
    client.post(f"/study/exams/{plan.id}", data={"action": "pacing", "pacing": "flow"})
    db.session.refresh(plan)
    assert plan.method == "blurt" and plan.pacing == "flow" and any(s.role == "blurt" for s in plan.sessions)
    # A session: start (1 coin), write on the blank page (encrypted), finish (3 coins).
    s = next(s for s in plan.sessions if s.role == "blurt")
    page = client.get(f"/study/exams/session/{s.id}").get_data(as_text=True)
    assert "Blank page" in page and "Flowtime" in page
    client.post(f"/study/exams/session/{s.id}/start")
    client.post(f"/study/exams/session/{s.id}/notes", json={"notes": "Chain rule: outer derivative times inner"})
    r = client.post(f"/study/exams/session/{s.id}/done", data={"minutes": "27"})
    assert r.status_code == 302
    db.session.refresh(s)
    assert s.done_at and s.minutes_done == 27 and "Chain rule" in s.notes
    raw = db.session.execute(db.text("SELECT notes FROM study_session WHERE id = :i"), {"i": s.id}).scalar()
    assert raw.startswith("enc1:"), "what the student writes is encrypted"
    earned = db.session.scalars(select(CoinTransaction.amount).where(CoinTransaction.user_id == synced_user.id,
                                                                     CoinTransaction.reason.like("%study session%"))).all()
    assert sorted(earned) == [1, 3]
    client.post(f"/study/exams/session/{s.id}/start")
    client.post(f"/study/exams/session/{s.id}/done", data={"minutes": "5"})
    assert db.session.query(CoinTransaction).filter(CoinTransaction.reason.like("%study session%")).count() == 2, "once each"
    # It shows on the calendar and the dashboard.
    upcoming = next(x for x in plan.sessions if not x.done_at)
    cal = client.get(f"/calendar?view=day&d={upcoming.day}").get_data(as_text=True)
    assert f"/study/exams/session/{upcoming.id}" in cal
    assert "Exams coming up" in client.get("/dashboard").get_data(as_text=True)
    # Another student can't see or touch it.
    other = make_user("riley", "riley@example.com")
    client.post("/logout")
    login(client, other)
    assert client.get(f"/study/exams/{plan.id}").status_code == 404
    assert client.get(f"/study/exams/session/{s.id}").status_code == 404
    assert client.post(f"/study/exams/session/{s.id}/done", data={"minutes": "1"}).status_code == 404


def test_tests_by_hand_questions_and_settings(app, synced_user, client):
    day = (local_now(synced_user).date() + timedelta(days=10)).isoformat()
    r = client.post("/study/exams/plan", data={"title": "Syllabus-only quiz", "date": day, "time": "10:00", "kind": "quiz"})
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.title == "Syllabus-only quiz"))
    assert r.status_code == 302 and plan.kind == "quiz" and plan.assignment_id is None and plan.sessions
    assert client.post("/study/exams/plan", data={"title": "", "date": day}).status_code == 302
    assert db.session.query(StudyPlan).count() == 1, "a name and a date are required"
    client.post("/study/exams/settings", data={"minutes": "45"})
    assert db.session.get(User, synced_user.id).study_minutes_per_day == 45
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    client.post("/study/exams/choice", data={"item": f"a:{midterm.id}", "kind": "none"})
    page = client.get("/study/exams/").get_data(as_text=True)
    assert "Things you said aren't tests" in page
    choice = db.session.scalar(select(AssessmentChoice).where(AssessmentChoice.user_id == synced_user.id))
    client.post(f"/study/exams/choice/{choice.id}/delete")
    assert "Plan it" in client.get("/study/exams/").get_data(as_text=True)
    assert client.post("/study/exams/choice", data={"item": "x:1", "kind": "none"}).status_code == 400
    client.post(f"/study/exams/{plan.id}/delete")
    assert db.session.get(StudyPlan, plan.id) is None


def test_a_moved_exam_moves_its_plan(app, synced_user, client):
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.user_id == synced_user.id))
    last_before = max(s.day for s in plan.sessions)
    midterm.due_at += timedelta(days=4)
    db.session.commit()
    page = client.get("/study/exams/").get_data(as_text=True)
    assert "moved to" in page
    db.session.refresh(plan)
    assert plan.exam_at == midterm.due_at and max(s.day for s in plan.sessions) > last_before
    # Decks linked to the plan show up as its materials.
    deck = Deck(user_id=synced_user.id, title="Derivatives", plan_id=plan.id)
    db.session.add(deck)
    db.session.commit()
    assert "Derivatives" in client.get(f"/study/exams/{plan.id}").get_data(as_text=True)


def test_export_includes_study_plans(app, synced_user, client):
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    data = client.get("/settings/data/export").get_json(force=True)
    assert data["study_plans"][0]["title"] == "Midterm Exam" and data["study_plans"][0]["sessions"]
    assert "practice_tests" in data



def test_review_fixes_for_the_scheduler(app):
    """Nothing after the test; others get 30 minutes on a big test's day; reviews follow their practice
    test; a small quiz keeps its only session; early sessions cancel the matching planned one."""
    user = make_user(study_minutes_per_day=60)
    final = _exam(user, None, "Final", 5, kind="final", tier="final")
    quiz = _exam(user, None, "Tiny quiz", 4, kind="quiz", tier="micro")
    db.session.commit()
    assert quiz.sessions, "a plan's only session is never dropped"
    for p in (final, quiz):
        exam_day = planner.local_day(p.exam_at, user).isoformat()
        assert all(s.day < exam_day for s in p.sessions)
        roles = [s.role for s in p.sessions]
        for i, r in enumerate(roles):
            if r == "review":
                assert "practice_test" in roles[:i], roles
    # The 30-minute rule: on a big test's day, other tests get at most 30 minutes.
    user2 = make_user("kai", "kai@example.com", study_minutes_per_day=120)
    midterm = _exam(user2, None, "Midterm", 10, tier="high")
    test = _exam(user2, None, "Test", 12, kind="test", tier="medium")
    db.session.commit()
    big_day = planner.local_day(midterm.exam_at, user2).isoformat()
    assert sum(s.minutes for s in test.sessions if s.day == big_day) <= 30
    # A test that already started gets nothing new.
    user3 = make_user("ola", "ola@example.com")
    now_test = _exam(user3, None, "Started", 0, kind="quiz", tier="low")
    now_test.exam_at = utcnow() - timedelta(hours=1)
    db.session.commit()
    planner.schedule(user3)
    db.session.commit()
    assert not db.session.scalars(select(StudySession).where(StudySession.plan_id == now_test.id)).all()


def test_a_date_set_by_hand_sticks_until_canvas_changes(app, synced_user, client):
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.user_id == synced_user.id))
    day = (local_now(synced_user).date() + timedelta(days=12)).isoformat()
    client.post(f"/study/exams/{plan.id}", data={"action": "date", "date": day, "time": "10:00"})
    client.get("/dashboard")
    client.get("/study/exams/")
    db.session.refresh(plan)
    assert planner.local_day(plan.exam_at, synced_user).isoformat() == day, "the student's date stays"
    midterm.due_at += timedelta(days=1)  # Canvas itself moves it: follow Canvas again
    db.session.commit()
    client.get("/study/exams/")
    db.session.refresh(plan)
    assert plan.exam_at == midterm.due_at


def test_choices_and_suggestions_after_review(app, synced_user, client):
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    # A double tap on "Yes, plan it" makes one plan.
    for _ in range(2):
        client.post("/study/exams/choice", data={"item": f"a:{midterm.id}", "kind": "midterm", "plan": "1"})
    assert db.session.query(StudyPlan).count() == 1
    # Past tests aren't offered or planned again.
    plan = db.session.scalar(select(StudyPlan))
    db.session.delete(plan)
    midterm.due_at = utcnow() - timedelta(hours=8)
    db.session.commit()
    assert "Plan it" not in client.get("/study/exams/").get_data(as_text=True)
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    assert db.session.query(StudyPlan).count() == 0
    # Sibling and make-up keys.
    A = assessments
    assert A.sibling_key("Exam 1 Part A") == A.sibling_key("Exam 1 Part B")
    assert A.sibling_key("Chapter 4 Quiz") != A.sibling_key("Chapter 5 Quiz")
    assert A.sibling_key("Week 9: Exam") != A.sibling_key("Week 9: Concept Check")
    assert A.makeup_key("Exam 2 (Make-up)") == A.makeup_key("Exam 2") != A.makeup_key("Exam 3")
    assert len(A.family_id("x" * 400, "quiz")) <= 150
    # A quiz never swallows an exam event with the same number.
    q, e = A.Item(name="Quiz 2", is_quiz=True, submission_types=["online_quiz"], points_possible=10,
                  due_at=utcnow() + timedelta(days=6)), A.Item(name="Exam 2", is_event=True,
                                                               start_at=utcnow() + timedelta(days=7),
                                                               end_at=utcnow() + timedelta(days=7, minutes=75))
    merged = A.merge([(q, A.classify(q))], [(e, A.classify(e))])
    assert (None, e) in merged and (q, None) in merged


def test_session_coins_are_capped_per_day(app, synced_user, client):
    day = (local_now(synced_user).date() + timedelta(days=20)).isoformat()
    client.post("/study/exams/plan", data={"title": "Farm", "date": day, "kind": "final"})
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.title == "Farm"))
    for s in list(plan.sessions):
        client.post(f"/study/exams/session/{s.id}/start")
        client.post(f"/study/exams/session/{s.id}/done", data={"minutes": "30"})
    rows = db.session.scalars(select(CoinTransaction).where(CoinTransaction.user_id == synced_user.id,
                                                            CoinTransaction.reason.like("%study session%"))).all()
    assert sum(1 for r in rows if r.amount == 3) == 3 and sum(1 for r in rows if r.amount == 1) == 3
    client.post(f"/study/exams/session/{plan.sessions[-1].id}/done", data={"minutes": "0", "undo": "1"})


def test_after_the_exam_nothing_to_start_and_repeat_answers_are_fine(app, synced_user, client):
    midterm = db.session.scalar(select(Assignment).where(Assignment.name == "Midterm Exam"))
    client.post("/study/exams/plan", data={"item": f"a:{midterm.id}"})
    plan = db.session.scalar(select(StudyPlan).where(StudyPlan.user_id == synced_user.id))
    today = local_now(synced_user).date().isoformat()
    db.session.add(StudySession(plan_id=plan.id, user_id=synced_user.id, day=today, role="cram", minutes=45))
    midterm.due_at = plan.exam_at = plan.source_at = utcnow() - timedelta(hours=2)  # under way / just over
    db.session.commit()
    assert "today: Quick pass" not in client.get("/dashboard").get_data(as_text=True)
    # "Not a test" twice (a double tap) is fine.
    db.session.delete(plan)
    midterm.due_at = utcnow() + timedelta(days=5)
    db.session.commit()
    for _ in range(2):
        assert client.post("/study/exams/choice", data={"item": f"a:{midterm.id}", "kind": "none"}).status_code == 302
