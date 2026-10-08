from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import ApiToken, User, utcnow
from app.services.ai import AIResult, StreamHandle


class FakeAI:
    """Stands in for Claude: deterministic output shaped by the requested schema."""

    def __init__(self):
        self.calls = []
        self.fail_with = None
        # A streamed *response* (the tutor) outlives the request's database session in production,
        # but not in the test client; tests of streamed responses turn this on to match.
        self.close_session_before_streaming = False
        self.answer = "The chain rule multiplies derivatives [S1]. Try it on $\\sin(x^2)$."  # what stream() says

    def complete(self, *, system, messages, max_tokens, effort, schema=None, model=None):
        self.calls.append({"system": system, "messages": messages, "effort": effort, "schema": schema, "model": model})
        if self.fail_with:
            raise self.fail_with
        props = (schema or {}).get("properties", {})
        if "cards" in props:
            text = json.dumps({"title": "Derivatives", "cards": [
                {"front": "d/dx sin x", "back": "cos x"}, {"front": "Power rule", "back": "d/dx x^n = n x^(n-1)"},
                {"front": "  ", "back": "blank fronts are dropped"}]})
        elif "questions" in props:
            text = json.dumps({"title": "Derivative check", "questions": [
                {"question": "d/dx x^2?", "choices": ["x", "2x", "x^2", "2"], "answer": 1, "explanation": "Power rule."},
                {"question": "d/dx sin x?", "choices": ["cos x", "-cos x", "sin x", "tan x"], "answer": 0, "explanation": ""},
                {"question": "broken", "choices": ["only one"], "answer": 0, "explanation": ""}]})
        else:
            text = "## Overview\nA **summary** with math $x^2$.\n\n- point one\n- point two"
        return AIResult(text, 1200, 300, "fake-model")

    def stream(self, *, system, messages, max_tokens, effort, model=None):
        self.calls.append({"system": system, "messages": messages, "effort": effort, "stream": True, "model": model})
        handle = StreamHandle(chunks=iter(()))
        answer = self.answer

        def gen():
            if self.close_session_before_streaming:
                from app.extensions import db

                db.session.remove()  # what Flask's teardown has done by now in production
            for piece in re.findall(r".{1,12}", answer, re.S):
                yield piece
            handle.result = AIResult(answer, 900, 60, "fake-model")

        handle.chunks = gen()
        return handle


@pytest.fixture
def app(tmp_path):
    app = create_app("test", {"STORAGE_DIR": str(tmp_path / "storage"), "SERVER_NAME": "hatch.test"})
    app.extensions["hh_ai"] = FakeAI()

    # Tests hold one app context open (so they can query the database directly), and Flask
    # reuses it for test-client requests. Flask-Login caches the user on `g`, so clear it per
    # request; otherwise one simulated student would inherit another's identity.
    @app.before_request
    def _fresh_login_user():
        from flask import g

        g.pop("_login_user", None)

    app.before_request_funcs[None].insert(0, app.before_request_funcs[None].pop())
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def fake_ai(app) -> FakeAI:
    return app.extensions["hh_ai"]


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(username="sam", email=None, password="password123", **fields) -> User:
    fields.setdefault("keep_all_files", True)  # a student who chose to keep every class's files
    fields.setdefault("birth_year", 2004)
    user = User(email=email or f"{username}@example.com", username=username, display_name=username.title(),
                accepted_terms_at=utcnow(), onboarded=True, timezone="America/New_York", **fields)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def login(client, user, password="password123"):
    resp = client.post("/login", data={"identifier": user.username, "password": password})
    assert resp.status_code == 302, resp.data[:500]
    return resp


def api_token(user, raw="hh_test_token_" + "x" * 20) -> str:
    from app.blueprints.api import hash_token

    db.session.add(ApiToken(user_id=user.id, name="test", token_hash=hash_token(raw + user.username),
                            prefix=raw[:10]))
    db.session.commit()
    return raw + user.username


def iso(delta_days: float) -> str:
    return (utcnow() + timedelta(days=delta_days)).replace(microsecond=0).isoformat() + "Z"


BASE_SNAPSHOT = {
    "schema_version": 1,
    "base_url": "https://school.instructure.com",
    "user": {"id": "501", "name": "Sam Student", "short_name": "Sam"},
    "courses": [
        {
            "id": "101", "name": "Calculus I", "course_code": "MATH 101-01", "class_key": "9::calculus i",
            "on_dashboard": True, "term": {"id": "9", "name": "Fall"},
            "grade": {"current_score": 91.5, "current_grade": "A-", "final_score": 60.0, "final_grade": "D"},
            "html_url": "https://school.instructure.com/courses/101",
            "syllabus_html": "<p>Exams are 60% of the grade. Late work loses 10% per day.</p>",
            "files_tab_hidden": False,
            "assignment_groups": [
                {"id": "1", "name": "Homework", "weight": 40, "position": 1, "drop_lowest": 1, "drop_highest": 0},
                {"id": "2", "name": "Exams", "weight": 60, "position": 2, "drop_lowest": 0, "drop_highest": 0},
            ],
            "assignments": [
                {"id": "1001", "name": "HW 1", "group_id": "1", "due_at": iso(-20), "points_possible": 10,
                 "submission_types": ["online_upload"], "status": "graded", "html_url": "https://school.instructure.com/courses/101/assignments/1001",
                 "description_html": "<p>Practice the chain rule on problems 1-10.</p>",
                 "submission": {"submitted_at": iso(-21), "score": 9, "grade": "9", "late": False, "missing": False,
                                "workflow_state": "graded", "comments": [{"author": "Prof", "created_at": iso(-18), "comment": "Nice"}]},
                 "rubric": [{"id": "r1", "description": "Correct", "points": 10, "ratings": []}]},
                {"id": "1002", "name": "HW 2", "group_id": "1", "due_at": iso(-10), "points_possible": 10,
                 "submission_types": ["online_upload"], "status": "graded",
                 "submission": {"submitted_at": iso(-9), "score": 4, "late": True, "workflow_state": "graded"}},
                {"id": "1003", "name": "HW 3", "group_id": "1", "due_at": iso(2), "points_possible": 10,
                 "submission_types": ["online_upload"], "status": "upcoming", "submission": {"workflow_state": "unsubmitted"}},
                {"id": "1004", "name": "Midterm Exam", "group_id": "2", "due_at": iso(5), "points_possible": 100,
                 "submission_types": ["on_paper"], "status": "no_submission", "submission": {"workflow_state": "unsubmitted"}},
                {"id": "1005", "name": "Quiz 1", "group_id": "2", "due_at": iso(-3), "points_possible": 20, "is_quiz": True,
                 "submission_types": ["online_quiz"], "status": "missing", "submission": {"missing": True, "workflow_state": "unsubmitted"}},
            ],
            "modules": [{"id": "m1", "name": "Week 1", "position": 1, "items": [
                {"id": "i1", "title": "Limits notes", "type": "Page", "page_url": "limits-notes", "content_id": None},
                {"id": "i2", "title": "Slides", "type": "File", "content_id": "9001"},
                {"id": "i3", "title": "HW 1", "type": "Assignment", "content_id": "1001"},
            ]}],
            "pages": [{"url": "limits-notes", "title": "Limits notes", "updated_at": iso(-15),
                       "html_url": "https://school.instructure.com/courses/101/pages/limits-notes",
                       "body_html": "<h2>Limits</h2><p>The chain rule: derivative of f(g(x)) is f'(g(x)) g'(x).</p>"
                                    "<p><a href=\"/courses/101/files/9001\">slides</a><script>alert(1)</script></p>"}],
            "files": [
                {"id": "9001", "name": "week1-slides.pdf", "content_type": "application/pdf", "size": 2048, "updated_at": iso(-15),
                 "download_url": "https://school.instructure.com/files/9001/download?verifier=x", "locked": False, "sources": ["files_tab", "module"]},
                {"id": "9002", "name": "notes.txt", "content_type": "text/plain", "size": 120, "updated_at": iso(-14),
                 "download_url": "https://school.instructure.com/files/9002/download", "locked": False, "sources": ["files_tab"]},
            ],
            "discussions": [],
            "quizzes": [],
            "announcements": [{"id": "a1", "title": "Exam moved", "posted_at": iso(-1), "author": "Prof",
                               "message_html": "<p>The midterm is now in room 317.</p>", "html_url": None}],
        },
        {
            "id": "102", "name": "Calculus I", "course_code": "MATH 101-D02", "class_key": "9::calculus i",
            "on_dashboard": True, "term": {"id": "9", "name": "Fall"}, "grade": {}, "assignment_groups": [],
            "assignments": [], "modules": [], "pages": [], "files": None, "files_tab_hidden": True,
            "discussions": [], "quizzes": [], "announcements": [],
        },
        {
            "id": "103", "name": "Placement Test", "course_code": "PLACE", "class_key": "1::placement test",
            "on_dashboard": False, "term": {"id": "1", "name": "Default Term"}, "grade": {}, "assignment_groups": [],
            "assignments": [{"id": "3001", "name": "Placement", "due_at": iso(-30), "points_possible": 1,
                             "submission_types": ["online_quiz"], "status": "graded", "submission": {"submitted_at": iso(-30), "score": 1, "workflow_state": "graded"}}],
            "modules": [], "pages": [], "files": [], "discussions": [], "quizzes": [], "announcements": [],
        },
    ],
    "planner": [],
    "calendar_events": [{"id": "e1", "title": "Review session", "start_at": iso(4), "end_at": iso(4.05),
                         "course_id": "101", "location": "Room 317", "html_url": None}],
    "missing": [{"id": "1005", "course_id": "101", "name": "Quiz 1", "due_at": iso(-3)}],
    "restricted": [{"endpoint": "/courses/102/files", "status": 403}],
    "errors": [],
}

# What extension 1.5.2+ sends for each class: the enrollment role, and the hashed Canvas uuid that
# keys class chat rooms (services/dms.py).
for _c in BASE_SNAPSHOT["courses"]:
    _c.setdefault("enrollment_role", "student")
    _c.setdefault("uuid_hash", hashlib.sha256(f"uuid-of-{_c['id']}".encode()).hexdigest())

BASE_MANIFEST = [
    {"id": "9001", "updated_at": BASE_SNAPSHOT["courses"][0]["files"][0]["updated_at"], "size": 2048,
     "name": "week1-slides.pdf", "content_type": "application/pdf", "course_id": "101", "path": "Calculus I/week1-slides.pdf"},
    {"id": "9002", "updated_at": BASE_SNAPSHOT["courses"][0]["files"][1]["updated_at"], "size": 120,
     "name": "notes.txt", "content_type": "text/plain", "course_id": "101", "path": "Calculus I/notes.txt"},
]


@pytest.fixture
def snapshot():
    return copy.deepcopy(BASE_SNAPSHOT)


@pytest.fixture
def manifest():
    return copy.deepcopy(BASE_MANIFEST)


def sync(client, token, snapshot, manifest, files: dict[str, bytes] | None = None):
    """Run the extension's upload protocol against the app."""
    auth = {"Authorization": f"Bearer {token}"}
    r = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    uploaded = []
    for fid in body["files_needed"]:
        meta = next(m for m in manifest if m["id"] == fid)
        data = (files or {}).get(fid, f"contents of {fid}".encode())
        put = client.put(f"/v1/files/{fid}?updated_at={meta['updated_at']}", data=data,
                         headers={**auth, "X-Snapshot-Id": body["snapshot_id"], "Content-Type": meta["content_type"]})
        assert put.status_code == 200, put.get_json()
        uploaded.append(fid)
    done = client.post(f"/v1/snapshots/{body['snapshot_id']}/complete", json={"uploaded": len(uploaded), "failed": []},
                       headers=auth)
    assert done.status_code == 200
    return body, uploaded


@pytest.fixture
def synced_user(app, client, snapshot, manifest):
    """A logged-in student whose Canvas has been synced once."""
    user = make_user()
    token = api_token(user)
    sync(client, token, snapshot, manifest, {"9002": b"The chain rule says multiply the outer derivative by the inner."})
    login(client, user)
    return user
