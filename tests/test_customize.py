"""A student's own name, short code and color for a class (Customize tab)."""

from __future__ import annotations

from sqlalchemy import select

from app.extensions import db
from app.models import Course, Deck

from .conftest import api_token, sync


def _calc(user) -> Course:
    return db.session.scalar(select(Course).where(Course.user_id == user.id, Course.canvas_id == "101"))


def test_customize_shows_everywhere_and_survives_sync(app, synced_user, client, snapshot, manifest):
    course = _calc(synced_user)
    assert course.canvas_name == "Calculus I" and course.name == "Calculus I"
    deck = Deck(user_id=synced_user.id, title="Limits", course_id=course.id)
    db.session.add(deck)
    db.session.commit()

    r = client.post(f"/courses/{course.id}/customize", data={"name": "  Calc   with Dr. K ", "code": "CALC",
                                                             "color": "#8FF0BD", "hidden": "0"})
    assert r.status_code == 302
    course = _calc(synced_user)
    assert (course.name, course.course_code, course.color) == ("Calc with Dr. K", "CALC", "#8ff0bd")
    for path in ("/dashboard", "/calendar", "/study/", f"/study/decks/{deck.id}", "/courses/"):
        page = client.get(path).get_data(as_text=True)
        assert "Calc with Dr. K" in page or "CALC" in page, path
        assert "MATH 101-01" not in page, path  # (the discussion section, MATH 101-D02, keeps its name)
    assert "#8ff0bd" in client.get("/courses/").get_data(as_text=True)

    # The LMS renaming the class doesn't undo the student's name; it updates what "reset" goes back to.
    snapshot["courses"][0]["name"] = "Calculus I (Fall)"
    sync(app.test_client(), api_token(synced_user, raw="hh_test_token_again_" + "z" * 20), snapshot, manifest)
    course = _calc(synced_user)
    assert course.name == "Calc with Dr. K" and course.canvas_name == "Calculus I (Fall)"

    client.post(f"/courses/{course.id}/customize", data={"action": "reset"})
    course = _calc(synced_user)
    assert (course.name, course.course_code, course.color) == ("Calculus I (Fall)", "MATH 101-01", None)


def test_customize_rejects_odd_colors_and_strangers(app, synced_user, client):
    course = _calc(synced_user)
    client.post(f"/courses/{course.id}/customize", data={"name": "", "code": "", "color": "#000000"})
    course = _calc(synced_user)
    assert course.color is None and course.name == "Calculus I", "only palette colors; blank keeps the school's name"
    from .conftest import login, make_user

    other = app.test_client()
    login(other, make_user("eve"))
    assert other.post(f"/courses/{course.id}/customize", data={"name": "pwned"}).status_code == 404
    assert _calc(synced_user).name == "Calculus I"


def test_lasting_links_use_the_canonical_address(app, synced_user, client):
    app.config["CANONICAL_URL"] = "https://homeworkhatch.com"
    page = client.get("/calendar").get_data(as_text=True)
    assert "https://homeworkhatch.com/calendar/" in page and "hatch.test/calendar/" not in page
