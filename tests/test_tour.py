"""The new-user walkthrough and the Get set up checklist (services/tour.py, js/tour.js)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import Course, User
from app.services import tour

from .conftest import login, make_user


def _config(page: str) -> dict | None:
    if 'id="hh-tour"' not in page:
        return None
    return json.loads(page.split('id="hh-tour">', 1)[1].split("</script>", 1)[0])


def test_new_students_start_the_tour_after_welcome(app, client):
    r = client.post("/register", data={"email": "new@example.com", "username": "newbie", "password": "longenough",
                                       "birth_month": "3", "birth_year": "2005", "terms": "1", "timezone": "America/New_York"})
    assert r.status_code == 302
    user = db.session.scalar(select(User).where(User.username == "newbie"))
    user.is_approved = True
    db.session.commit()
    r = client.post("/welcome", data={"display_name": "Newbie", "timezone": "America/New_York"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard?tour=start")
    assert db.session.get(User, user.id).tour_step == "welcome"
    assert _config(client.get("/welcome").get_data(as_text=True)) is None, "never on the welcome form itself"
    cfg = _config(client.get("/dashboard").get_data(as_text=True))
    ids = [s["id"] for s in cfg["steps"]]
    assert cfg["current"] == "welcome" and ids[0] == "welcome" and ids[-1] == "finish"
    assert "canvas" in ids and "calendar_link" in ids and "phone" in ids
    assert "google" not in ids, "no Google step when Google isn't offered"
    assert "files" not in ids, "no files step before there are Canvas classes"
    canvas = next(s for s in cfg["steps"] if s["id"] == "canvas")
    assert canvas["url"] == "/settings/sync" and canvas["check"] == "school" and canvas["done"] is False
    # A second visit to /welcome doesn't restart a tour they finished.
    client.post("/tour/end", json={})
    client.post("/welcome", data={"display_name": "Newbie", "timezone": "America/New_York"})
    assert db.session.get(User, user.id).tour_step is None


def test_moving_through_ending_and_restarting(synced_user, client):
    tour.start(synced_user)
    db.session.commit()
    ids = [s["id"] for s in _config(client.get("/dashboard").get_data(as_text=True))["steps"]]
    assert "files" in ids, "a student with Canvas classes picks which files to keep"
    assert client.post("/tour/step", json={"step": "calendar"}).get_json() == {"ok": True}
    assert db.session.get(User, synced_user.id).tour_step == "calendar"
    assert _config(client.get("/courses/").get_data(as_text=True))["current"] == "calendar", "the tour waits on any page"
    assert client.post("/tour/step", json={"step": "nope"}).status_code == 400
    assert client.post("/tour/step", json=["x"]).status_code == 400
    assert client.post("/tour/step", json={"step": ["welcome"]}).status_code == 400
    assert client.post("/tour/mark", json=["phone_calendar"]).status_code == 400
    client.post("/tour/end", json={})
    user = db.session.get(User, synced_user.id)
    assert user.tour_step is None and user.tour_done_at is not None
    assert _config(client.get("/dashboard").get_data(as_text=True)) is None
    page = client.get("/settings/").get_data(as_text=True)
    assert "Restart the tour" in page
    r = client.post("/tour/start")
    assert r.headers["Location"].endswith("/dashboard?tour=start"), "tour.js forgets an earlier 'hide the tour'"
    assert db.session.get(User, synced_user.id).tour_step == "welcome"


def test_setup_checklist_and_status(synced_user, client):
    synced_user.keep_all_files = None  # a student who hasn't chosen which classes' files to keep
    for c in db.session.scalars(select(Course).where(Course.user_id == synced_user.id)):
        c.sync_files = None
    db.session.commit()
    status = client.get("/tour/status").get_json()
    assert status["school"] is True and status["phone"] is False and status["study"] is False
    assert status["files"] is False and not any(k.startswith("_") for k in status)
    dash = client.get("/dashboard").get_data(as_text=True)
    assert 'id="setup-card"' in dash and "Connect your school" in dash and "Choose which classes" in dash
    assert "Take the tour" in dash, "students who never saw the tour are invited"
    # The phone calendar can't be seen by the server: the student says so.
    client.post("/tour/mark", data={"key": "phone_calendar"})
    assert client.get("/tour/status").get_json()["phone"] is True
    assert client.post("/tour/mark", json={"key": "admin"}).status_code == 400
    # Choosing files checks that item off; a new class from a later sync needs a choice again.
    client.post("/settings/files", data={"mode": "some", "keep": [str(c.id) for c in db.session.scalars(select(Course))]})
    assert client.get("/tour/status").get_json()["files"] is True
    db.session.scalars(select(Course)).first().sync_files = None
    db.session.commit()
    assert client.get("/tour/status").get_json()["files"] is False
    # Hiding the checklist; declining the invitation.
    client.post("/tour/mark", data={"key": "hide_setup"})
    dash = client.get("/dashboard").get_data(as_text=True)
    assert 'id="setup-card"' not in dash and 'id="tour-invite"' in dash
    client.post("/tour/decline")
    assert 'id="tour-invite"' not in client.get("/dashboard").get_data(as_text=True)


def test_every_page_renders_during_the_tour(app, synced_user, client):
    tour.start(synced_user)
    db.session.commit()
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    for path in ["/dashboard", "/courses/", f"/courses/{course.id}", "/calendar?view=week", "/study/", "/study/generate",
                 "/study/exams/", "/tutor/", "/chat/", "/files/", "/settings/", "/settings/sync", "/coins"]:
        r = client.get(path)
        assert r.status_code == 200 and "js/tour.js" in r.get_data(as_text=True), path
    # Every step's page has what the step points at.
    anchors = {"#ext": "/settings/sync", "#calendar-links": "/settings/sync", "#files": "/settings/sync",
               "#subscribe-card": "/calendar?view=week", "#gen-modes": "/study/generate", "#rooms-card": "/chat/",
               "#upload-form": "/files/", "#setup-card": "/dashboard"}
    for anchor, path in anchors.items():
        assert f'id="{anchor[1:]}"' in client.get(path).get_data(as_text=True), anchor
    assert 'data-nav="main.dashboard"' in client.get("/dashboard").get_data(as_text=True)


def test_google_step_only_when_google_is_offered(app, synced_user, client):
    app.extensions["hh_composio"] = object()  # Google offered (no calls are made here)
    tour.start(synced_user)
    db.session.commit()
    dash = client.get("/dashboard")
    assert dash.status_code == 200 and "Connect Google (optional)" in dash.get_data(as_text=True)
    cfg = _config(dash.get_data(as_text=True))
    google = next(s for s in cfg["steps"] if s["id"] == "google")
    assert google["url"] == "/settings/integrations" and client.get(google["url"]).status_code == 200
    assert "Google Drive" in next(s for s in cfg["steps"] if s["id"] == "myfiles")["body"]


def test_without_ai_the_tour_skips_ai_and_the_checklist_can_finish(app, synced_user, client, monkeypatch):
    from app.services import ai

    monkeypatch.setattr(ai, "available", lambda: False)
    tour.start(synced_user)
    db.session.commit()
    cfg = _config(client.get("/dashboard").get_data(as_text=True))
    ids = [s["id"] for s in cfg["steps"]]
    assert "generate" not in ids and "tutor" not in ids
    assert "Google Drive" not in next(s for s in cfg["steps"] if s["id"] == "myfiles")["body"]
    items = {i["key"]: i for i in tour.checklist(db.session.get(User, synced_user.id))}
    assert "tutor" not in items and items["study"]["url"].endswith("/study/decks/import")
    # Hidden checklist: the last step doesn't point at it.
    client.post("/tour/mark", data={"key": "hide_setup"})
    finish = next(s for s in _config(client.get("/dashboard").get_data(as_text=True))["steps"] if s["id"] == "finish")
    assert finish["target"] == "" and "checklist" not in finish["body"]
    client.post("/tour/start")
    assert "hide_setup" not in (db.session.get(User, synced_user.id).tour_marks or []), "restarting brings the checklist back"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_tour_script_parses():
    script = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "tour.js"
    out = subprocess.run([shutil.which("node"), "--check", str(script)], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
