"""The extension's Brightspace check (extension/d2l.js) and where it lands: POST /v1/diagnostics and
the admin Diagnostics page. Nothing personal may be stored, even from a modified client."""

import copy
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import LmsDiagnostic
from app.services import diagnostics

from .conftest import api_token, login, make_user

ROOT = Path(__file__).resolve().parent.parent

REPORT = {
    "lms": "brightspace", "schema": 1, "host": "school.brightspace.com", "extension_version": "1.5.0",
    "ran_at": "2026-10-03T16:00:00.000Z", "transport": "worker",
    "versions": {"lp": "1.63", "le": "1.99"}, "signed_in": True,
    "endpoints": [
        {"name": "versions", "path": "/d2l/api/versions/", "status": 200, "ms": 9, "count": 3,
         "shape": {"[]": {"LatestVersion": "str", "ProductCode": "str", "SupportedVersions": {"[]": "str", "len": 6}}, "len": 3}},
        {"name": "whoami", "path": "/d2l/api/lp/1.63/users/whoami", "status": 200, "ms": 1, "count": None, "shape": None},
        {"name": "dropbox_folders", "path": "/d2l/api/le/1.99/{ou}/dropbox/folders/", "status": 200, "ms": 4, "count": 2,
         "shape": {"[]": {"Availability": {"EndDate": "date", "{or}": "null"}, "DueDate": "date|null", "Name": "str",
                          "SubmissionType": "num"}, "len": 2},
         "course": 1, "enums": {"SubmissionType": [0, 2]}},
        {"name": "my_items_due", "path": "/d2l/api/le/1.99/content/myItems/due/?orgUnitIdsCSV={ou}", "status": 403, "ms": 2,
         "count": None, "shape": None},
        {"name": "grade_setup", "path": "/d2l/api/le/1.99/{ou}/grades/setup/", "status": 429, "ms": 3, "count": None,
         "shape": None, "course": 1, "retry_after": 30},
    ],
    "enums": {"SubmissionType": [0, 2], "AssociatedEntityType": ["D2L.LE.Dropbox.Dropbox"], "GradingSystem": ["Weighted"]},
    "notes": ["rate_limited"],
}


def _post(client, token, body, raw=None):
    data = raw if raw is not None else json.dumps(body)
    return client.post("/v1/diagnostics", data=data, content_type="application/json",
                       headers={"Authorization": f"Bearer {token}"} if token else {})


def test_a_report_is_stored_as_sent(app, client):
    user = make_user()
    token = api_token(user)
    r = _post(client, token, REPORT)
    assert r.status_code == 201, r.data
    assert r.headers["Access-Control-Allow-Origin"] == "*", "the extension calls from chrome-extension://"
    row = db.session.get(LmsDiagnostic, r.get_json()["id"])
    assert (row.user_id, row.lms, row.host, row.extension_version) == (user.id, "brightspace", "school.brightspace.com", "1.5.0")
    assert row.payload == REPORT, "a clean report passes through unchanged"


def test_auth_is_the_sync_token(app, client):
    user = make_user()
    assert _post(client, None, REPORT).status_code == 401
    assert _post(client, "hh_not_a_real_token_xxxxxxxxxxxx", REPORT).status_code == 401
    token = api_token(user)
    user.active = False
    db.session.commit()
    assert _post(client, token, REPORT).status_code == 403
    assert db.session.scalar(select(LmsDiagnostic)) is None
    pre = client.open("/v1/diagnostics", method="OPTIONS")
    assert pre.status_code == 204 and "Authorization" in pre.headers["Access-Control-Allow-Headers"]


def test_size_limit_and_bad_bodies(app, client):
    token = api_token(make_user())
    big = copy.deepcopy(REPORT)
    big["padding"] = "x" * (256 * 1024)
    assert _post(client, token, big).status_code == 413
    assert _post(client, token, None, raw="{not json").status_code == 400
    assert _post(client, token, None, raw="[]").status_code == 400
    for patch in ({"lms": "canvas"}, {"schema": 2}, {"schema": True}, {"host": "Pat Example's school"},
                  {"host": "https://school.brightspace.com/d2l"}, {"host": None}):
        assert _post(client, token, {**REPORT, **patch}).status_code == 400, patch
    assert db.session.scalar(select(LmsDiagnostic)) is None


def test_server_drops_anything_personal_a_modified_client_smuggles_in(app, client):
    token = api_token(make_user())
    evil = copy.deepcopy(REPORT)
    evil["student_name"] = "Pat Example"                                   # unknown top-level key
    evil["notes"] = ["rate_limited", "Pat Example got 87%", "signed_out"]  # free-text note
    evil["extension_version"] = "1.5.0 Pat Example"
    evil["ran_at"] = "yesterday, by Pat"
    evil["transport"] = "Pat's laptop"
    evil["versions"] = {"lp": "1.63", "le": "Pat", "product_build": "20.26.10", "whoami": "Pat Example"}
    evil["enums"] = {"SubmissionType": [0, "File", "Pat Example's essay: 87%", 3.75, True],
                     "Name": ["Pat Example"],                                # not an enum field
                     "Status": ["Submitted", "x" * 40]}
    ep = evil["endpoints"][2]
    ep["shape"]["[]"]["Name"] = "Pat Example"                             # a value instead of a type
    ep["shape"]["[]"]["Pat Example's grade"] = "num"                      # a key outside the key pattern? (apostrophe)
    ep["shape"]["[]"]["DueDate"] = "date|B+"
    ep["shape"]["[]"]["Availability"]["{or}"] = "null|87%"
    ep["shape"]["[]"]["Grades"] = ["Pat", "Example"]                      # lists aren't shapes
    ep["shape"]["[]"]["Score"] = 87.5
    ep["shape"]["len"] = 2
    ep["path"] = "/d2l/api/le/1.99/6606/dropbox/folders/"                # a real org unit id
    ep["display_name"] = "Pat Example"
    ep["enums"] = {"EntityType": ["User"], "DisplayName": ["Pat Example"]}
    ep["error"] = "Pat Example"
    evil["endpoints"].append({"name": "Pat Example", "status": 200, "path": "/d2l/api/x", "shape": "str"})
    evil["endpoints"].append({"name": "news", "status": "200", "shape": "str"})

    r = _post(client, token, evil)
    assert r.status_code == 201, r.data
    stored = db.session.get(LmsDiagnostic, r.get_json()["id"]).payload
    text = json.dumps(stored)
    for s in ("Pat", "87", "B+", "yesterday", "laptop", "6606", "x" * 40):
        assert s not in text, f"{s!r} was stored: {text}"
    assert "student_name" not in stored
    assert stored["notes"] == ["rate_limited", "signed_out"]
    assert stored["extension_version"] is None and stored["ran_at"] is None and stored["transport"] is None
    assert stored["versions"] == {"lp": "1.63", "product_build": "20.26.10"}
    assert stored["enums"] == {"SubmissionType": [0, "File"], "Status": ["Submitted"]}
    folders = stored["endpoints"][2]
    assert folders["path"] is None and "display_name" not in folders and "error" not in folders
    assert folders["enums"] == {"EntityType": ["User"]}
    item = folders["shape"]["[]"]
    assert set(item) == {"Availability", "SubmissionType"}, item
    assert item["Availability"] == {"EndDate": "date"}
    assert [e["name"] for e in stored["endpoints"]] == ["versions", "whoami", "dropbox_folders", "my_items_due", "grade_setup"]


def test_shape_cleaner_vocabulary():
    clean = diagnostics.clean_shape
    assert clean("date|null") == "date|null"
    assert clean("str|str") is None and clean("text") is None and clean("") is None
    assert clean({"len": 0}) == {"len": 0}
    assert clean({"len": True}) == {}, "a bool isn't a length, so this is an (empty) object"
    assert clean({"[]": {"A": "num"}, "len": 3, "{or}": "null", "extra": "str"}) == {"[]": {"A": "num"}, "len": 3, "{or}": "null"}
    assert clean({"@odata.context": "url", "{id}": {"x": "bool"}, "a b": "num"}) == \
        {"@odata.context": "url", "{id}": {"x": "bool"}}, "a key with a space could be a name: dropped"
    assert clean({"k" * 81: "str", "ok": "str"}) == {"ok": "str"}
    deep = "num"
    for _ in range(20):
        deep = {"a": deep}
    assert "num" not in json.dumps(clean(deep)), "depth is capped"


def test_ten_reports_a_day(app, client):
    user = make_user()
    token = api_token(user)
    for _ in range(10):
        assert _post(client, token, REPORT).status_code == 201
    r = _post(client, token, REPORT)
    assert r.status_code == 429 and "10" in r.get_json()["error"]
    # Another student isn't affected, and a day later it's allowed again.
    assert _post(client, api_token(make_user("lee")), REPORT).status_code == 201
    for row in db.session.scalars(select(LmsDiagnostic).where(LmsDiagnostic.user_id == user.id)):
        row.created_at -= timedelta(days=1, minutes=1)
    db.session.commit()
    assert _post(client, token, REPORT).status_code == 201


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_extensions_real_report_passes_validation_unchanged(app, client):
    out = subprocess.run(["node", str(ROOT / "tests/js/d2l_report.mjs")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["signed_in"] and len(report["endpoints"]) == 30
    r = _post(client, api_token(make_user()), report)
    assert r.status_code == 201, r.data
    assert db.session.get(LmsDiagnostic, r.get_json()["id"]).payload == report


def test_server_lists_match_the_extension():
    """d2l.js and services/diagnostics.py must agree on routes, enum fields and notes."""
    js = (ROOT / "extension/d2l.js").read_text()
    enum_block = re.search(r"ENUM_FIELDS = new Set\(\[(.*?)\]\)", js, re.S).group(1)
    assert set(re.findall(r'"(\w+)"', enum_block)) == diagnostics.ENUM_FIELDS
    notes_block = re.search(r"NOTES = Object\.freeze\(\{(.*?)\}\);", js, re.S).group(1)
    assert set(re.findall(r"^\s*(\w+):", notes_block, re.M)) == diagnostics.NOTES
    routes_block = re.search(r"ROUTES = \{(.*?)\};", js, re.S).group(1)
    assert dict(re.findall(r'^\s*(\w+): "([^"]+)"', routes_block, re.M)) == diagnostics.ROUTES
    assert '/^[A-Za-z][A-Za-z ]{0,30}$/' in js and diagnostics.ENUM_VALUE_RE.pattern == r"^[A-Za-z][A-Za-z ]{0,30}$"


def test_admin_diagnostics_page(app, client):
    admin = make_user("boss", is_admin=True)
    student = make_user("kid")
    _post(client, api_token(student), REPORT)
    _post(client, api_token(student, raw="hh_second_token_" + "y" * 20), {**REPORT, "signed_in": False, "endpoints": REPORT["endpoints"][:2],
                                                                          "notes": ["signed_out"]})
    c = app.test_client()
    login(c, student)
    assert c.get("/admin/diagnostics").status_code == 403
    row = db.session.scalar(select(LmsDiagnostic))
    assert c.get(f"/admin/diagnostics/{row.id}.json").status_code == 403

    login(client, admin)
    assert 'href="/admin/diagnostics"' in client.get("/admin/").get_data(as_text=True), "linked from the admin overview"
    page = client.get("/admin/diagnostics").get_data(as_text=True)
    assert "kid" in page and "school.brightspace.com" in page and "1.5.0" in page
    assert "3/5" in page, "endpoints answered / total"
    assert "2/2" in page and "signed out" in page
    r = client.get(f"/admin/diagnostics/{row.id}.json")
    assert r.status_code == 200 and r.mimetype == "application/json"
    assert "attachment" in r.headers["Content-Disposition"] and "brightspace-school.brightspace.com" in r.headers["Content-Disposition"]
    assert json.loads(r.data) == row.payload
    assert client.get("/admin/diagnostics/999999.json").status_code == 404


def test_field_names_with_spaces_are_dropped():
    from app.services import diagnostics

    assert diagnostics.KEY_RE.match("SubmissionType") and diagnostics.KEY_RE.match("{ou}")
    assert not diagnostics.KEY_RE.match("Pat Example")


def test_deleting_the_account_deletes_its_diagnostics(app, client):
    student = make_user("sam")
    _post(client, api_token(student), REPORT)
    assert db.session.query(LmsDiagnostic).count() == 1
    login(client, student)
    client.post("/settings/data/delete", data={"confirm": "sam", "password": "password123"})
    db.session.expire_all()
    assert db.session.query(LmsDiagnostic).count() == 0
