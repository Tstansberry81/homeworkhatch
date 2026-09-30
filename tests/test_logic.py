"""Pure-logic tests: grades, citations, moderation, iCal."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models import Assignment, AssignmentGroup, Card, utcnow
from app.services import citations, grades, ics, moderation


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


def test_canvas_grading_settings():
    # Weights over 100% aren't scaled down (Canvas lets extra-credit groups push past 100%).
    groups = [G("hw", 60), G("exam", 60)]
    r = grades.compute(groups, [A(1, 10, 10, "hw"), A(2, 50, 100, "exam")])
    assert r["percent"] == pytest.approx((0.6 * 100 + 0.6 * 50))
    # Canvas weighting turned off: group weights are ignored, total points used.
    r = grades.compute([G("hw", 90), G("exam", 10)], [A(1, 10, 10, "hw"), A(2, 50, 100, "exam")], weighted=False)
    assert r["percent"] == pytest.approx(100 * 60 / 110, abs=0.01)
    # Omitted assignments and never-drop rules.
    omitted = A(3, 0, 100, "hw", omit_from_final_grade=True)
    assert grades.compute([G("hw")], [A(1, 10, 10, "hw"), omitted])["percent"] == 100.0
    g = G("g", drop_lowest=1)
    g.never_drop = ["1"]
    r = grades.compute([g], [A(1, 0, 10), A(2, 5, 10), A(3, 9, 10)])
    assert r["groups"][0].dropped == [2], "the never-drop assignment can't be the one dropped"


def test_drop_lowest_stays_fast_on_big_groups():
    import time

    items = [A(i, (i * 7) % 10, 10) for i in range(1, 41)]
    t = time.time()
    grades.needed_on([G("g", drop_lowest=12)], items + [A(99, None, 10, status="upcoming")], 99, 60)
    assert time.time() - t < 5
