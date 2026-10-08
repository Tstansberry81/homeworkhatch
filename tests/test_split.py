"""Brain Grade (services/split.py): test grade vs everything else, missing work, the fix-it list."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import AssessmentChoice, Assignment, AssignmentGroup, CanvasAccount, Course, utcnow
from app.services import split

from .conftest import login, make_user


def _course(user, weighted=True, groups=(("hw", "Homework", 40), ("ex", "Exams", 60))) -> Course:
    acct = CanvasAccount(user_id=user.id, host="school.test", base_url="https://school.test", canvas_user_id="1")
    db.session.add(acct)
    db.session.flush()
    c = Course(user_id=user.id, account_id=acct.id, canvas_id="7", name="Chemistry", canvas_name="Chemistry",
               class_key="k", room_key="school.test:7", group_weighting=weighted)
    db.session.add(c)
    db.session.flush()
    for gid, name, weight in groups:
        db.session.add(AssignmentGroup(course_id=c.id, canvas_id=gid, name=name, weight=weight if weighted else None))
    db.session.commit()
    return c


def _a(course, cid, name, group, possible, score=None, status=None, **kw) -> Assignment:
    a = Assignment(course_id=course.id, canvas_id=cid, name=name, group_canvas_id=group, points_possible=possible,
                   score=score, status=status or ("graded" if score is not None else "upcoming"),
                   submission_types=kw.pop("submission_types", ["online_upload"]), **kw)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def chem(app):
    user = make_user()
    c = _course(user)
    _a(c, "1", "HW 1", "hw", 10, 10)
    _a(c, "2", "HW 2", "hw", 10, 9)
    zero = _a(c, "3", "HW 3", "hw", 10, 0, missing=True)
    _a(c, "4", "Exam 1", "ex", 100, 90, submission_types=["on_paper"])
    _a(c, "5", "Exam 2", "ex", 100, 80, submission_types=["on_paper"])
    return user, c, zero


def test_split_weighted_class(app, chem):
    user, c, zero = chem
    s = split.split(user.id, c)
    # Homework 19/30 = 63.33% (40%), exams 170/200 = 85% (60%): 76.33% overall.
    assert s.status == "ok" and s.canvas == pytest.approx(76.33, abs=0.01)
    assert s.tests == pytest.approx(85.0) and s.other == pytest.approx(63.33, abs=0.01)
    assert s.n_tests == 2 and s.test_share == pytest.approx(0.6, abs=0.01)
    # The zero at the student's usual 95% on homework: 28.5/30 = 95% -> 89% overall, 12.67 points back.
    assert s.lost_missing == pytest.approx(12.67, abs=0.01)
    assert [f.assignment.id for f in s.fixes] == [zero.id] and s.fixes[0].gain == pytest.approx(12.67, abs=0.01)
    assert s.gap == pytest.approx(8.67, abs=0.01) and "pulling the class grade down" in s.message


def test_what_counts_as_missing(app, chem):
    user, c, zero = chem
    # Canvas's missing-work policy gave partial credit: still missing work (it only shows up as a fix
    # when the student's usual work would beat the policy's score, which here it doesn't).
    _a(c, "6", "HW 4", "hw", 10, 8, missing=True)
    # Not graded yet: today's score leaves it out, so it "could cost".
    pending = _a(c, "7", "HW 5", "hw", 10, None, status="missing", missing=True)
    # Locked in Canvas: still missing, but not a fix.
    _a(c, "8", "HW 6", "hw", 10, 0, missing=True, lock_at=utcnow() - timedelta(days=1))
    # Marked done by the student (handed in on paper): never missing work.
    _a(c, "9", "HW 7", "hw", 10, 0, missing=True, user_done=True)
    s = split.split(user.id, c)
    ids = [f.assignment.id for f in s.fixes]
    assert set(ids) == {zero.id, pending.id}
    assert s.could_cost > 0 and s.n_missing == 4  # HW 3, HW 4, HW 5, HW 6
    assert next(f for f in s.fixes if f.assignment.id == pending.id).graded_zero is False


def test_late_penalties(app, chem):
    user, c, _zero = chem
    exam2 = db.session.scalar(select(Assignment).where(Assignment.name == "Exam 2"))
    exam2.points_deducted = 5.0
    exam2.late_policy_status = "late"
    db.session.commit()
    s = split.split(user.id, c)
    # 5 points back on a 200-point, 60% group: 0.6 * 2.5 = 1.5 points of the grade.
    assert s.lost_late == pytest.approx(1.5, abs=0.01)


def test_not_a_test_based_class(app):
    user = make_user()
    c = _course(user)
    _a(c, "1", "HW 1", "hw", 10, 9)
    _a(c, "2", "Exam 1", "ex", 100, 70, submission_types=["on_paper"])
    s = split.split(user.id, c)
    assert s.status == "na" and "Not a test-based class" in s.message
    empty = _course(make_user("bo"))
    assert split.split(empty.user_id, empty).status == "empty"


def test_points_based_class_and_missing_tests(app):
    user = make_user()
    c = _course(user, weighted=False, groups=(("all", "Assignments", None),))
    _a(c, "1", "Quiz 1", "all", 20, 18, is_quiz=True, submission_types=["online_quiz"])
    _a(c, "2", "Quiz 2", "all", 20, 16, is_quiz=True, submission_types=["online_quiz"])
    _a(c, "3", "Quiz 3", "all", 20, 0, missing=True, is_quiz=True, submission_types=["online_quiz"],
       lock_at=utcnow() - timedelta(days=2))
    _a(c, "4", "Essay", "all", 100, 85)
    s = split.split(user.id, c)
    # A quiz never taken isn't knowledge: the test grade is 34/40, the zero is missing work.
    assert s.tests == pytest.approx(85.0) and s.lost_missing > 0 and s.fixes == []


def test_the_students_answer_moves_an_item(app, chem, client):
    user, c, _zero = chem
    login(client, user)
    hw2 = db.session.scalar(select(Assignment).where(Assignment.name == "HW 2"))
    assert not split.split(user.id, c).labels[hw2.id].is_test
    r = client.post("/study/exams/choice", data={"item": f"a:{hw2.id}", "kind": "quiz",
                                                  "next": f"/courses/{c.id}?tab=grades"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/courses/{c.id}?tab=grades")
    assert db.session.scalar(select(AssessmentChoice).where(AssessmentChoice.item == f"a:{hw2.id}")).kind == "quiz"
    s = split.split(user.id, c)
    assert s.labels[hw2.id].is_test and s.labels[hw2.id].why() == "you said so" and s.n_tests == 3
    # An open redirect is ignored.
    r = client.post("/study/exams/choice", data={"item": f"a:{hw2.id}", "kind": "none", "next": "//evil.test/"})
    assert r.headers["Location"].endswith(f"/courses/{c.id}?tab=grades")
    # Someone else's assignment: 404.
    other = make_user("mallory")
    oc = app.test_client()
    login(oc, other)
    assert oc.post("/study/exams/choice", data={"item": f"a:{hw2.id}", "kind": "none"}).status_code == 404


def test_pages_show_it(app, chem, client):
    user, c, zero = chem
    login(client, user)
    page = client.get(f"/courses/{c.id}?tab=grades").get_data(as_text=True)
    assert "Your grade, split" in page and "85.0%" in page and "63.3%" in page and "−12.7%" in page
    assert "Points still on the table" in page and "up to +12.7%" in page and "data-brain-share" in page
    dash = client.get("/dashboard").get_data(as_text=True)
    assert "Points still on the table" in dash and "HW 3" in dash and "up to +12.7%" in dash


def test_letters():
    assert [split.letter(p) for p in (95, 91.2, 85, 72, 50, None)] == ["A", "A-", "B", "C-", "F", "—"]


def test_drops_are_chosen_once_on_the_whole_class(app):
    """A drop-lowest rule in a mixed group must not run again inside the test half."""
    user = make_user()
    c = _course(user, groups=(("mix", "Quizzes & Homework", 50), ("ex", "Exams", 50)))
    db.session.get(AssignmentGroup, db.session.scalar(select(AssignmentGroup.id).where(
        AssignmentGroup.canvas_id == "mix"))).drop_lowest = 2
    db.session.commit()
    for i, (name, score) in enumerate([("HW 1", 3), ("HW 2", 4), ("HW 3", 10), ("HW 4", 10)]):
        _a(c, f"h{i}", name, "mix", 10, score)
    for i, score in enumerate([6, 7, 9, 8]):
        _a(c, f"q{i}", f"Quiz {i + 1}", "mix", 10, score, is_quiz=True, submission_types=["online_quiz"])
    _a(c, "e1", "Exam 1", "ex", 100, 80, submission_types=["on_paper"])
    _a(c, "e2", "Exam 2", "ex", 100, 70, submission_types=["on_paper"])
    s = split.split(user.id, c)
    # Canvas drops HW 1 and HW 2; the quizzes all count: 30/40 and 150/200 -> 75.0, not 80.0.
    assert s.tests == pytest.approx(75.0) and s.n_tests == 6


def test_messages_match_the_numbers(app):
    user = make_user()
    c = _course(user)
    for i in range(10):
        _a(c, f"h{i}", f"HW {i + 1}", "hw", 10, 6)
    _a(c, "x", "Exit slip", "hw", 1, 0, missing=True, lock_at=utcnow() - timedelta(days=1))
    _a(c, "e1", "Exam 1", "ex", 100, 90, submission_types=["on_paper"])
    _a(c, "e2", "Exam 2", "ex", 100, 90, submission_types=["on_paper"])
    s = split.split(user.id, c)
    assert s.gap > 2 and s.lost < 1 and "pulling" not in s.message and "homework is carrying" not in s.message
    assert s.message == "Your tests are ahead of the rest of your graded work."


def test_ungraded_missing_work_is_a_risk_not_a_gain(app, chem, client):
    user, c, zero = chem
    pending = _a(c, "7", "HW 5", "hw", 10, None, status="missing", missing=True)
    s = split.split(user.id, c)
    risk = next(f for f in s.fixes if f.assignment.id == pending.id)
    assert not risk.graded_zero and s.fixes[0].assignment.id == zero.id, "real gains come first"
    login(client, user)
    page = client.get(f"/courses/{c.id}?tab=grades").get_data(as_text=True)
    assert "could cost −" in page and "Missing, not graded yet" in page
