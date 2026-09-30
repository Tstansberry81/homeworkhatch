"""Picking sources for flashcards/quizzes/tutor, uploads, and Google Calendar/Drive through a fake Composio."""

import io
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import (Assignment, CalendarPush, CanvasFile, Course, Deck, Integration, Page, TutorConversation, Upload,
                        utcnow)
from app.services import gdrive

from .conftest import api_token, login, make_user, sync


class FakeComposio:
    """Just enough of the Composio SDK: auth configs, connect links, connections and tools."""

    def __init__(self):
        self.calls = []
        self.connected = set()  # (composio user id, toolkit)
        self.events = {}
        self.auth_configs = SimpleNamespace(list=lambda **kw: SimpleNamespace(items=[]),
                                            create=lambda toolkit, options: SimpleNamespace(id=f"ac_{toolkit}"))
        self.connected_accounts = SimpleNamespace(link=self._link, list=self._list, delete=self._delete,
                                                  revoke=lambda account_id: None)
        self.tools = SimpleNamespace(execute=self._execute)

    def connect(self, user, toolkit):
        self.connected.add((f"hh-{user.id}", toolkit))

    def _link(self, user_id, auth_config_id, callback_url=None):
        self.callback = callback_url
        return SimpleNamespace(redirect_url=f"https://connect.composio.test/{auth_config_id}?u={user_id}")

    def _list(self, user_ids, toolkit_slugs, statuses):
        return SimpleNamespace(items=[SimpleNamespace(id=f"ca_{u}_{t}") for u in user_ids for t in toolkit_slugs
                                      if (u, t) in self.connected])

    def _delete(self, account_id):
        self.connected = {(u, t) for u, t in self.connected if f"ca_{u}_{t}" != account_id}

    def _execute(self, slug, arguments, user_id=None, **kwargs):
        self.calls.append((slug, dict(arguments)))
        toolkit = "googlecalendar" if slug.startswith("GOOGLECALENDAR") else "googledrive"
        if (user_id, toolkit) not in self.connected:
            return {"successful": False, "error": "No connected account found for user", "data": {}}
        if slug == "GOOGLECALENDAR_CREATE_EVENT":
            event_id = f"ev{len(self.calls)}"
            self.events[event_id] = dict(arguments)
            return {"successful": True, "error": None, "data": {"response_data": {"id": event_id}}}
        if slug in ("GOOGLECALENDAR_PATCH_EVENT", "GOOGLECALENDAR_DELETE_EVENT"):
            if arguments["event_id"] not in self.events:
                return {"successful": False, "error": "Not Found", "data": {}}
            if slug.endswith("DELETE_EVENT"):
                self.events.pop(arguments["event_id"])
            else:
                self.events[arguments["event_id"]].update(arguments)
            return {"successful": True, "error": None, "data": {}}
        if slug == "GOOGLEDRIVE_FIND_FILE":
            return {"successful": True, "error": None, "data": {"files": [
                {"id": "doc1", "name": "Lecture 3 notes", "mimeType": "application/vnd.google-apps.document"},
                {"id": "form1", "name": "A form", "mimeType": "application/vnd.google-apps.form"},
                {"id": "pdf1", "name": "Reading.pdf", "mimeType": "application/pdf", "size": "2048"}]}}
        if slug == "GOOGLEDRIVE_DOWNLOAD_FILE":
            return {"successful": True, "error": None, "data": {"name": "Lecture 3 notes", "downloaded_file_content": {
                "name": "Lecture 3 notes", "s3url": "https://s3.test/doc1", "mimetype": "text/plain"}}}
        raise AssertionError(f"unexpected tool {slug}")


@pytest.fixture
def composio(app, monkeypatch):
    fake = FakeComposio()
    app.extensions["hh_composio"] = fake

    class Download:
        raw = io.BytesIO(b"Lecture 3: the chain rule, worked examples, and implicit differentiation.")
        def raise_for_status(self): pass
        def close(self): pass

    monkeypatch.setattr(gdrive.requests, "get", lambda url, **kw: Download())
    return fake


def _upload(client, name, body, course_id=""):
    return client.post("/files/upload", data={"files": (io.BytesIO(body), name), "course_id": str(course_id)},
                       content_type="multipart/form-data", headers={"Accept": "application/json"})


# ---------------------------------------------------------------- sources, uploads, generation


def test_generate_from_several_picked_sources(synced_user, client, fake_ai):
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    r = _upload(client, "my-notes.txt", b"My own notes on related rates and optimization problems.", course.id)
    assert r.status_code == 200, r.get_json()
    upload = db.session.scalar(select(Upload))
    assert upload.text_status == "ok" and upload.course_id == course.id and "related rates" in upload.text

    listed = client.get(f"/study/sources?course_id={course.id}").get_json()["sources"]
    by_ref = {s["ref"]: s for s in listed}
    notes = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    slides = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9001"))
    page = db.session.scalar(select(Page).where(Page.course_id == course.id))
    assert by_ref[f"file:{notes.id}"]["status"] == "ready"
    assert by_ref[f"file:{slides.id}"]["status"] == "unreadable", "fake PDF bytes have no text"
    assert f"upload:{upload.id}" in by_ref and f"page:{page.id}" in by_ref

    refs = [f"file:{notes.id}", f"upload:{upload.id}", f"page:{page.id}"]
    r = client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": refs})
    assert r.status_code == 302
    prompt = fake_ai.calls[-1]["messages"][0]["content"]
    assert all(f"=== {t} ===" in prompt for t in (notes.name, upload.name, page.title))
    assert "combines 3 sources" in prompt
    assert db.session.scalar(select(Deck)).course_id == course.id


def test_sources_belong_to_their_owner(synced_user, client):
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    notes = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    other = make_user("intruder")
    client.post("/logout")
    login(client, other)
    assert client.get(f"/study/sources?course_id={course.id}").status_code == 404
    r = client.post("/study/generate", data={"output": "deck", "mode": "sources", "refs": [f"file:{notes.id}"]})
    assert r.status_code == 400 and b"readable text" in r.data


def test_uploads_can_be_filed_and_deleted(synced_user, client):
    from app.services.storage import get_storage

    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    _upload(client, "guide.txt", b"A study guide about limits and continuity.")
    u = db.session.scalar(select(Upload))
    assert u.course_id is None and client.get("/study/sources?course_id=").get_json()["sources"][0]["ref"] == f"upload:{u.id}"
    assert client.post(f"/files/{u.id}/course", data={"course_id": course.id}).status_code == 302
    assert db.session.get(Upload, u.id).course_id == course.id
    key = u.storage_key
    assert client.get("/files/").status_code == 200
    assert client.post(f"/files/{u.id}/delete").status_code == 302
    assert db.session.get(Upload, u.id) is None
    with pytest.raises(Exception):
        get_storage().read(key)


def test_tutor_uses_attached_files(synced_user, client, fake_ai):
    fake_ai.close_session_before_streaming = True  # as in production: the answer outlives the request
    _upload(client, "essay-draft.txt", b"Draft thesis: the Missouri Compromise delayed but did not prevent conflict.")
    u = db.session.scalar(select(Upload))
    client.post("/tutor/new")
    conv = db.session.scalar(select(TutorConversation))
    r = client.post(f"/tutor/{conv.id}/attachments", json={"refs": [f"upload:{u.id}", "file:99999"]})
    assert [s["ref"] for s in r.get_json()["sources"]] == [f"upload:{u.id}"], "unknown refs are dropped"
    r = client.post(f"/tutor/{conv.id}/message", json={"text": "Is my thesis arguable?"})
    body = r.get_data(as_text=True)
    assert "event: done" in body
    call = fake_ai.calls[-1]
    system = call["system"]
    assert isinstance(system, list) and "Missouri Compromise" in system[1]["text"]
    assert system[1]["cache_control"] == {"type": "ephemeral"}, "attachments are cached across turns"
    done = json.loads(body.split("event: done\ndata: ", 1)[1].split("\n", 1)[0])
    assert done["sources"][0]["title"] == "essay-draft.txt", "[S1] is the attached file"


def test_college_odds_is_gone(synced_user, client):
    assert client.get("/college/").status_code == 404


# ---------------------------------------------------------------- Google Calendar


def _upcoming(user):
    now = utcnow()
    return [a for a in db.session.scalars(select(Assignment).join(Course).where(Course.user_id == user.id))
            if a.due_at and now - timedelta(hours=12) <= a.due_at <= now + timedelta(days=60) and not a.course.hidden]


def test_google_calendar_gets_due_dates_once_and_stays_in_step(synced_user, client, composio):
    assert client.get("/settings/integrations").status_code == 200
    # Turning it on sends the student to connect Google first.
    r = client.post("/settings/integrations/calendar/toggle", data={"enabled": "1", "next": "/calendar"})
    assert r.status_code == 302 and "/settings/integrations/calendar/connect" in r.headers["Location"]
    r = client.get(r.headers["Location"])
    assert r.headers["Location"].startswith("https://connect.composio.test/ac_googlecalendar")
    assert composio.callback.endswith("/settings/integrations/calendar/callback")
    composio.connect(synced_user, "googlecalendar")
    r = client.get("/settings/integrations/calendar/callback?status=success")
    assert r.status_code == 302 and r.headers["Location"].endswith("/calendar")

    upcoming = _upcoming(synced_user)
    assert upcoming, "the fixture has upcoming assignments"
    creates = [a for s, a in composio.calls if s == "GOOGLECALENDAR_CREATE_EVENT"]
    assert len(creates) == len(upcoming) == db.session.query(CalendarPush).count()
    ev = creates[0]
    assert ev["summary"].startswith("Due: ") and ev["create_meeting_room"] is False and ev["send_updates"] == "none"
    assert ev["timezone"] == synced_user.timezone and "Open in Homework Hatch" in ev["description"]

    # Nothing changed: nothing is sent again.
    composio.calls.clear()
    client.post("/settings/integrations/calendar/sync")
    assert not [s for s, _ in composio.calls if s != "GOOGLECALENDAR_EVENTS_LIST"]

    # A due date moves: that event is updated, not duplicated.
    moved = upcoming[0]
    moved.due_at += timedelta(days=1)
    db.session.commit()
    client.post("/settings/integrations/calendar/sync")
    assert [s for s, _ in composio.calls] == ["GOOGLECALENDAR_PATCH_EVENT"]

    # Deleted in Canvas: the event is removed from Google.
    composio.calls.clear()
    db.session.delete(db.session.get(Assignment, moved.id))
    db.session.commit()
    client.post("/settings/integrations/calendar/sync")
    assert [s for s, _ in composio.calls] == ["GOOGLECALENDAR_DELETE_EVENT"]
    assert len(composio.events) == len(upcoming) - 1

    # The Calendar page shows the toggle; turning it off removes the upcoming events.
    assert b"Google Calendar" in client.get("/calendar").data
    client.post("/settings/integrations/calendar/toggle", data={"enabled": "0", "remove_events": "1"})
    assert composio.events == {} and db.session.query(CalendarPush).count() == 0
    assert not db.session.scalar(select(Integration).where(Integration.kind == "calendar")).settings["enabled"]


def test_canvas_sync_updates_google_calendar(app, client, composio, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    sync(client, token, snapshot, manifest)
    composio.connect(user, "googlecalendar")
    db.session.add(Integration(user_id=user.id, kind="calendar", connected=True, settings={"enabled": True}))
    db.session.commit()
    composio.calls.clear()
    snapshot["courses"][0]["assignments"].append({**snapshot["courses"][0]["assignments"][0], "id": "1999", "name": "New quiz",
                                                  "due_at": (utcnow() + timedelta(days=3)).isoformat() + "Z"})
    sync(client, token, snapshot, manifest)
    names = [a["summary"] for s, a in composio.calls if s == "GOOGLECALENDAR_CREATE_EVENT"]
    assert any("New quiz" in n for n in names)


def test_lost_google_access_is_reported_not_crashed(synced_user, client, composio):
    db.session.add(Integration(user_id=synced_user.id, kind="calendar", connected=True, settings={"enabled": True}))
    db.session.commit()  # connected here, but Composio no longer has the account
    client.post("/settings/integrations/calendar/sync")
    row = db.session.scalar(select(Integration).where(Integration.kind == "calendar"))
    assert row.connected is False and row.last_error
    assert b"Connect Google Calendar" in client.get("/calendar").data


# ---------------------------------------------------------------- Google Drive


def test_drive_search_and_import(synced_user, client, composio):
    r = client.get("/files/drive/search?q=notes").get_json()
    assert r["connected"] is False and "/settings/integrations/drive/connect" in r["connect_url"]
    composio.connect(synced_user, "googledrive")
    db.session.add(Integration(user_id=synced_user.id, kind="drive", connected=True, settings={}))
    db.session.commit()

    r = client.get("/files/drive/search?q=notes").get_json()
    assert [f["id"] for f in r["files"]] == ["doc1", "pdf1"], "forms and other non-documents are hidden"
    q = next(a for s, a in composio.calls if s == "GOOGLEDRIVE_FIND_FILE")["q"]
    assert "name contains 'notes'" in q and "trashed = false" in q

    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    body = {"id": "doc1", "mimeType": "application/vnd.google-apps.document", "course_id": course.id}
    r = client.post("/files/drive/import", json=body)
    assert r.status_code == 200, r.get_json()
    u = db.session.scalar(select(Upload))
    assert u.source == "drive" and u.external_id == "doc1" and u.name == "Lecture 3 notes.txt"
    assert u.text_status == "ok" and "chain rule" in u.text and u.course_id == course.id
    download = next(a for s, a in composio.calls if s == "GOOGLEDRIVE_DOWNLOAD_FILE")
    assert download == {"fileId": "doc1", "mime_type": "text/plain"}, "Google Docs are exported as text"

    # Picking it again reuses the copy.
    client.post("/files/drive/import", json=body)
    assert db.session.query(Upload).count() == 1
    assert sum(1 for s, _ in composio.calls if s == "GOOGLEDRIVE_DOWNLOAD_FILE") == 1
