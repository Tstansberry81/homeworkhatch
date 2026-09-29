"""Pure-logic tests: grades, spaced repetition, planner, college odds, citations, moderation, iCal."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Assignment, AssignmentGroup, Card, utcnow
from app.services import citations, college, grades, ics, moderation, planner, srs


def A(id, score, possible, group="g", status="graded", **kw):
    return Assignment(id=id, canvas_id=str(id), name=kw.pop("name", f"A{id}"), score=score, points_possible=possible,
                      group_canvas_id=group, status=status, excused=kw.pop("excused", False), **kw)


def G(cid, weight=None, drop_lowest=0, drop_highest=0, name=None):
    return AssignmentGroup(canvas_id=cid, name=name or cid, weight=weight, drop_lowest=drop_lowest, drop_highest=drop_highest)


# ---------------------------------------------------------------- grades

def test_unweighted_is_total_points():
    r = grades.compute([G("g")], [A(1, 8, 10), A(2, 45, 50), A(3, None, 10, status="upcoming")])
    assert r["percent"] == pytest.approx(100 * 53 / 60, abs=0.01)
    assert not r["weighted"]


def test_weighted_renormalizes_over_groups_with_grades():
    groups = [G("hw", 40), G("exam", 60)]
    r = grades.compute(groups, [A(1, 9, 10, "hw"), A(2, None, 100, "exam", status="upcoming")])
    assert r["percent"] == 90.0, "an empty exam group doesn't drag the grade down"
    r = grades.compute(groups, [A(1, 9, 10, "hw"), A(2, 70, 100, "exam")])
    assert r["percent"] == pytest.approx(0.4 * 90 + 0.6 * 70)


def test_drop_lowest_picks_the_best_combination_not_the_lowest_percent():
    # Dropping the 0/1 (lowest %) leaves 50/100 = 50%; dropping the 50/100 leaves 10/10+0/1 = 90.9%.
    items = [A(1, 0, 1), A(2, 50, 100), A(3, 10, 10)]
    r = grades.compute([G("g", drop_lowest=1)], items)
    assert r["percent"] == pytest.approx(100 * 10 / 11, abs=0.01)
    assert r["groups"][0].dropped == [2]


def test_excused_ignored_and_extra_credit_counts():
    r = grades.compute([G("g")], [A(1, 10, 10), A(2, None, 10, excused=True, status="graded"), A(3, 2, 0)])
    assert r["percent"] == 120.0


def test_what_if_and_needed():
    groups = [G("hw", 40), G("exam", 60)]
    items = [A(1, 9, 10, "hw"), A(2, None, 100, "exam", status="upcoming")]
    assert grades.compute(groups, items, {2: 80})["percent"] == pytest.approx(0.4 * 90 + 0.6 * 80)
    need = grades.needed_on(groups, items, 2, 85)
    assert need == pytest.approx(81.67, abs=0.05)
    assert grades.needed_on(groups, items, 2, 200) is None


# ---------------------------------------------------------------- spaced repetition

def test_sm2_intervals_grow_and_lapses_reset():
    now = datetime(2026, 9, 1, 12)
    c = Card(front="f", back="b", ease=2.5, interval_days=0, repetitions=0, lapses=0, review_count=0, due_at=now)
    srs.review(c, "good", now)
    assert c.interval_days == 1
    srs.review(c, "good", now)
    assert c.interval_days == 6
    srs.review(c, "good", now)
    assert c.interval_days >= 14
    srs.review(c, "again", now)
    assert c.repetitions == 0 and c.lapses == 1 and c.due_at == now + timedelta(minutes=10)
    assert c.ease >= 1.3 and c.review_count == 4
    with pytest.raises(ValueError):
        srs.review(c, "meh", now)


# ---------------------------------------------------------------- planner

def _planned(name, due, points=10, status="upcoming", is_quiz=False):
    a = Assignment(name=name, due_at=due, points_possible=points, status=status, is_quiz=is_quiz, user_done=False)
    a.id = hash(name) % 100000
    return a


def test_planner_spreads_work_and_flags_overflow():
    user = SimpleNamespace(timezone="UTC")
    today = date(2026, 9, 1)
    at = lambda d, h=23: datetime(2026, 9, d, h, 59)
    work = [_planned("Essay", at(5), points=100), _planned("Final Exam", at(2)), _planned("Done", at(3), status="graded"),
            _planned("Midterm Exam", at(9), points=100, status="no_submission"),
            _planned("Week 6 participation", at(4), points=3, status="no_submission")]
    plan = planner.build_plan(work, today, 60, user)
    names = {i.assignment.name for d in plan["days"] for i in d.items}
    assert names == {"Essay", "Final Exam", "Midterm Exam"}, "in-class exams get prep time; participation doesn't"
    assert all(d.used <= 60 for d in plan["days"])
    assert any(r["assignment"].name == "Final Exam" for r in plan["at_risk"]), "180 min of exam prep can't fit in 2 days at 60/day"
    essay_days = [d.day for d in plan["days"] for i in d.items if i.assignment.name == "Essay"]
    assert max(essay_days) <= date(2026, 9, 5)


# ---------------------------------------------------------------- college

def school(**kw):
    base = dict(admit_rate=0.5, oos_admit_rate=None, public=False, state="VA", sat25=1200, sat75=1400, act25=None, act75=None)
    base.update(kw)
    return SimpleNamespace(**base)


def profile(**kw):
    base = dict(gpa=3.8, gpa_scale=4.0, sat=1300, act=None, home_state="VA")
    base.update(kw)
    return SimpleNamespace(**base)


def test_college_estimate_moves_with_scores_and_caps_selective_schools():
    low, mid, high = (college.estimate(profile(sat=s), school()).chance for s in (1100, 1300, 1550))
    assert low < mid < high
    elite = college.estimate(profile(sat=1600, gpa=4.0), school(admit_rate=0.05, sat25=1500, sat75=1570))
    assert elite.label == "Reach" and elite.chance <= 0.15
    safe = college.estimate(profile(sat=1500), school(admit_rate=0.8, sat25=1000, sat75=1200))
    assert safe.label == "Safety"


def test_out_of_state_rate_used_for_public_schools():
    s = school(public=True, state="VA", admit_rate=0.24, oos_admit_rate=0.16)
    in_state = college.estimate(profile(home_state="VA"), s).chance
    out = college.estimate(profile(home_state="MD"), s)
    assert out.chance < in_state and any("out-of-state" in r for r in out.reasons)
    assert college.estimate(profile(), school(admit_rate=None)).chance is None


# ---------------------------------------------------------------- citations

def test_citations_three_styles():
    web = citations.Source(kind="website", authors=["Jane Q. Smith", "Bo Li"], title="How Photosynthesis Works",
                           container="Science Daily", year=2024, month=3, day=5, url="https://sci.example/photo",
                           accessed=date(2026, 9, 1))
    assert citations.mla(web) == ("Smith, Jane Q., and Bo Li. “How Photosynthesis Works.” <i>Science Daily</i>, 5 Mar. 2024, "
                                  "sci.example/photo. Accessed 1 Sept. 2026.")
    assert citations.apa(web) == ("Smith, J. Q., &amp; Li, B. (2024, March 5). <i>How Photosynthesis Works</i>. Science Daily. "
                                  "https://sci.example/photo")
    book = citations.Source(kind="book", authors=["Toni Morrison"], title="Beloved", publisher="Knopf", year=1987, city="New York")
    assert citations.chicago(book) == "Morrison, Toni. <i>Beloved</i>. New York: Knopf, 1987."
    art = citations.Source(kind="article", authors=["A B", "C D", "E F"], title="Deep Results", container="Nature",
                           volume="5", issue="2", pages="10-20", year=2020, doi="10.1/xyz")
    assert citations.mla(art).startswith("B, A, et al. “Deep Results.” <i>Nature</i>, vol. 5, no. 2, 2020, pp. 10-20.")
    assert "<i>Nature</i>, <i>5</i>(2), 10-20. https://doi.org/10.1/xyz" in citations.apa(art)
    evil = citations.Source(kind="website", title="<script>x</script>")
    assert "<script>" not in citations.mla(evil)


# ---------------------------------------------------------------- moderation

def test_moderation_masks_blocks_and_spares_academic_words():
    assert moderation.clean("this is shit") == "this is s***"
    assert moderation.clean("Philip K. Dick wrote it; the retarded potential is in chapter 9") .startswith("Philip K. Dick")
    for bad in ("kys", "go kill yourself", "I will kill you"):
        with pytest.raises(moderation.Rejected):
            moderation.clean(bad)
    with pytest.raises(moderation.Rejected):
        moderation.clean("   ")
    with pytest.raises(moderation.Rejected):
        moderation.clean("x" * 1001)


# ---------------------------------------------------------------- iCal

def test_ics_feed_is_valid_and_folded():
    course = SimpleNamespace(name="Calculus, I; honors")
    a = SimpleNamespace(id=1, name="HW " + "x" * 120, due_at=datetime(2026, 9, 3, 3, 59), course=course,
                        html_url="https://c/a/1", effective_status="upcoming")
    e = SimpleNamespace(id=2, title="Review", start_at=datetime(2026, 9, 4, 18), end_at=None, location="Room 1", html_url=None)
    body = ics.build([a], [e], "hatch.test")
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))
    unfolded = body.replace("\r\n ", "")  # RFC 5545 line folding
    assert "DTEND:20260903T035900Z" in unfolded and "Calculus\\, I\\; honors" in unfolded
    assert "DTEND:20260904T190000Z" in body, "events without an end get an hour"
