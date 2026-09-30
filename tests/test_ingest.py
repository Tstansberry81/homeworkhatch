from sqlalchemy import func, select

from app.extensions import db
from app.models import Assignment, CanvasFile, CoinTransaction, ContentChunk, Course, Page
from app.services import coins

from .conftest import api_token, iso, make_user, sync


def test_first_sync_stores_everything_and_requests_files(app, client, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    body, uploaded = sync(client, token, snapshot, manifest)
    assert sorted(body["files_needed"]) == ["9001", "9002"]
    assert sorted(uploaded) == ["9001", "9002"]

    courses = db.session.scalars(select(Course).where(Course.user_id == user.id)).all()
    assert {c.canvas_id for c in courses} == {"101", "102", "103"}
    calc = next(c for c in courses if c.canvas_id == "101")
    assert calc.room_key == "school.instructure.com:101"
    assert calc.current_score == 91.5
    assert len(calc.assignments) == 5
    assert {g.name: g.drop_lowest for g in calc.groups} == {"Homework": 1, "Exams": 0}
    assert calc.pages[0].body_html.startswith("<h2>Limits")
    hw1 = next(a for a in calc.assignments if a.canvas_id == "1001")
    assert hw1.comments[0]["comment"] == "Nice" and hw1.rubric[0]["description"] == "Correct"
    # Placement course isn't on the Canvas dashboard, so it starts hidden.
    assert next(c for c in courses if c.canvas_id == "103").hidden is True

    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    assert f.is_stored and f.text_status == "ok" and f.course_id == calc.id


def test_resync_is_idempotent_and_skips_unchanged_files(app, client, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    sync(client, token, snapshot, manifest)
    first_ids = {a.canvas_id: a.id for a in db.session.scalars(select(Assignment))}
    balance = coins.balance(user.id)

    body, uploaded = sync(client, token, snapshot, manifest)
    assert body["files_needed"] == [] and uploaded == []
    assert {a.canvas_id: a.id for a in db.session.scalars(select(Assignment))} == first_ids, "row ids stay stable"
    assert coins.balance(user.id) == balance, "coins are never paid twice"

    manifest[1]["updated_at"] = iso(0)  # only the date moved (Canvas does this for settings edits)
    body, uploaded = sync(client, token, snapshot, manifest)
    assert body["files_needed"] == []

    manifest[1]["size"] = 121  # instructor replaced notes.txt
    body, uploaded = sync(client, token, snapshot, manifest)
    assert body["files_needed"] == ["9002"]


def test_changes_and_removals_propagate(app, client, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    sync(client, token, snapshot, manifest)
    calc = snapshot["courses"][0]
    calc["assignments"] = [a for a in calc["assignments"] if a["id"] != "1003"]  # assignment deleted in Canvas
    calc["assignments"][0]["name"] = "HW 1 (revised)"
    snapshot["courses"] = [c for c in snapshot["courses"] if c["id"] != "103"]  # dropped course
    sync(client, token, snapshot, manifest)
    names = {a.name for a in db.session.scalars(select(Assignment).join(Course).where(Course.canvas_id == "101"))}
    assert "HW 3" not in names and "HW 1 (revised)" in names
    dropped = db.session.scalar(select(Course).where(Course.canvas_id == "103"))
    assert dropped.active is False, "courses that leave Canvas are kept but marked inactive"


def test_coins_follow_the_original_rules(app, client, snapshot, manifest):
    user = make_user()
    sync(client, api_token(user), snapshot, manifest)
    rows = {t.reason: t.amount for t in db.session.scalars(select(CoinTransaction).where(CoinTransaction.user_id == user.id))}
    assert rows["Turned in: HW 1"] == 10                     # assignment base
    assert rows["Grade bonus: HW 1 (90%)"] == 9              # 90% of base
    assert rows["Turned in (late): HW 2"] == 5               # late work earns half
    assert rows["Grade bonus: HW 2 (40%)"] == 4
    assert not any("Placement" in r for r in rows), "hidden courses don't pay"
    assert not any("Quiz 1" in r for r in rows), "missing work doesn't pay"


def test_search_index_built_from_pages_syllabus_and_files(app, client, snapshot, manifest):
    user = make_user()
    sync(client, api_token(user), snapshot, manifest, {"9002": b"Integration by parts: u dv = uv - v du."})
    kinds = set(db.session.scalars(select(ContentChunk.source_type).where(ContentChunk.user_id == user.id)))
    assert {"syllabus", "page", "assignment", "announcement", "file"} <= kinds


def test_auth_cors_and_validation(app, client, snapshot, manifest):
    r = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest})
    assert r.status_code == 401
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    pre = client.open("/v1/snapshots", method="OPTIONS")
    assert pre.status_code == 204 and "Authorization" in pre.headers["Access-Control-Allow-Headers"]

    user = make_user()
    token = api_token(user)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/snapshots", json={"snapshot": {"schema_version": 2}}, headers=auth).status_code == 400
    bad = dict(snapshot, base_url="javascript:alert(1)")
    assert client.post("/v1/snapshots", json={"snapshot": bad, "files": []}, headers=auth).status_code == 400
    assert client.get("/v1/me", headers=auth).get_json()["username"] == "sam"


def test_users_cannot_touch_each_others_uploads(app, client, snapshot, manifest):
    alice, bob = make_user("alice"), make_user("bob")
    a_tok, b_tok = api_token(alice), api_token(bob)
    r = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers={"Authorization": f"Bearer {a_tok}"})
    run_id = r.get_json()["snapshot_id"]
    # Bob can't upload into Alice's sync run or complete it.
    put = client.put(f"/v1/files/9001?updated_at=x", data=b"evil",
                     headers={"Authorization": f"Bearer {b_tok}", "X-Snapshot-Id": run_id})
    assert put.status_code == 400
    assert client.post(f"/v1/snapshots/{run_id}/complete", json={}, headers={"Authorization": f"Bearer {b_tok}"}).status_code == 404
    # Files not in the manifest are refused.
    put = client.put("/v1/files/12345?updated_at=x", data=b"x",
                     headers={"Authorization": f"Bearer {a_tok}", "X-Snapshot-Id": run_id})
    assert put.status_code == 400


def test_revoked_token_stops_sync(app, client, snapshot, manifest):
    from app.models import ApiToken

    user = make_user()
    token = api_token(user)
    db.session.scalar(select(ApiToken)).revoked = True
    db.session.commit()
    r = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_page_html_is_sanitized_and_links_point_to_canvas(synced_user, client):
    page = db.session.scalar(select(Page))
    html = client.get(f"/courses/pages/{page.id}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert 'href="https://school.instructure.com/courses/101/files/9001"' in html


def test_two_students_share_a_room_key(app, client, snapshot, manifest):
    a, b = make_user("alice"), make_user("bob")
    sync(client, api_token(a), snapshot, manifest)
    snapshot["user"]["id"] = "777"
    sync(client, api_token(b), snapshot, manifest)
    keys = db.session.execute(select(Course.room_key, func.count(Course.id)).where(Course.canvas_id == "101")
                              .group_by(Course.room_key)).all()
    assert keys == [("school.instructure.com:101", 2)]


def test_failed_or_unknown_lists_never_delete_data(app, client, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    sync(client, token, snapshot, manifest)
    calc = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    hw1 = next(a for a in calc.assignments if a.canvas_id == "1001")
    hw1.user_done = True
    db.session.commit()
    counts = (len(calc.assignments), len(calc.pages), len(calc.modules))

    # New extension: a list it couldn't fetch is null.
    snapshot["courses"][0]["assignments"] = None
    snapshot["courses"][0]["modules"] = None
    sync(client, token, snapshot, manifest)
    # Old extension: the list is [] but the snapshot's errors say the fetch failed.
    snapshot["courses"][0]["assignments"] = []
    snapshot["courses"][0]["pages"] = []
    snapshot["errors"] = [{"endpoint": "assignments:101", "message": "Canvas 500"},
                          {"endpoint": "pages:101", "message": "Canvas 500"}]
    sync(client, token, snapshot, manifest)
    db.session.expire_all()
    calc = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    assert (len(calc.assignments), len(calc.pages), len(calc.modules)) == counts
    assert next(a for a in calc.assignments if a.canvas_id == "1001").user_done is True


def test_long_canvas_values_and_huge_files_dont_break_sync(app, client, snapshot, manifest):
    user = make_user()
    token = api_token(user)
    snapshot["courses"][0]["grade"]["current_grade"] = "Exceeds Expectations (with distinction)"
    snapshot["courses"][0]["assignments"][0]["submission"]["grade"] = "complete " * 20
    snapshot["courses"][0]["term"]["name"] = "T" * 400
    manifest[0]["size"] = 10 ** 10  # 10 GB: over the upload limit
    body, uploaded = sync(client, token, snapshot, manifest)
    assert "9001" not in body["files_needed"], "oversized files aren't requested"
    body, _ = sync(client, token, snapshot, manifest)
    assert body["files_needed"] == [], "...and not on later syncs either"
    calc = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    assert calc.current_grade == "Exceeds Expectations"[:20]


def test_one_canvas_identity_per_account(app, client, snapshot, manifest):
    alice, mallory = make_user("alice"), make_user("mallory")
    sync(client, api_token(alice), snapshot, manifest)
    r = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest},
                    headers={"Authorization": f"Bearer {api_token(mallory)}"})
    assert r.status_code == 409, "someone else can't claim Alice's Canvas identity"


def test_files_are_copied_only_for_classes_the_student_chose(app, client, snapshot, manifest):
    from app.services.storage import get_storage

    from .conftest import login

    student = make_user("ria", keep_all_files=None)  # hasn't chosen yet
    token = api_token(student)
    snapshot["courses"][0]["roster_ids"] = ["501", "502"]  # an old extension still sending a roster
    snapshot["courses"][0]["files"][0]["download_url"] = "https://canvas.test/files/9001/download?verifier=secret"
    _, uploaded = sync(client, token, snapshot, manifest)
    assert uploaded == [], "no course files until the student picks classes"
    archived = get_storage().read(f"u/{student.id}/snapshots/"
                                  f"{db.session.scalar(select(Course.account_id).limit(1))}-latest.json").decode()
    assert "verifier=secret" not in archived and "download_url" not in archived, "signed links aren't kept"
    assert all(c.sync_files is None for c in db.session.scalars(select(Course)))

    c = app.test_client()
    login(c, student)
    assert b"not chosen yet" in c.get("/dashboard", follow_redirects=True).data
    calc = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    c.post("/settings/files", data={"mode": "pick", "keep": [str(calc.id)]})
    _, uploaded = sync(client, token, snapshot, manifest)
    calc_files = {f.canvas_id for f in db.session.scalars(select(CanvasFile).where(CanvasFile.course_id == calc.id))}
    assert uploaded and set(uploaded) <= calc_files, "only the ticked class's files"

    # Unticking deletes the stored copies (and their text), keeping the names for later.
    c.post("/settings/files", data={"mode": "pick", "keep": []})
    rows = db.session.scalars(select(CanvasFile).where(CanvasFile.course_id == calc.id)).all()
    assert rows and all(r.storage_key is None and r.text is None for r in rows)
    assert db.session.scalar(select(func.count(ContentChunk.id)).where(ContentChunk.source_type == "file")) == 0
    _, uploaded = sync(client, token, snapshot, manifest)
    assert uploaded == []

    # "All my classes, including new ones": every current class, and classes that show up later.
    c.post("/settings/files", data={"mode": "all"})
    snapshot["courses"].append(dict(snapshot["courses"][0], id="777", name="New class", files=[]))
    sync(client, token, snapshot, manifest)
    assert db.session.scalar(select(Course.sync_files).where(Course.canvas_id == "777")) is True


# ---------------------------------------------------------------- duplicate files


def _add_file(snapshot, manifest, fid, name, ctype, size, updated_at=None):
    updated_at = updated_at or iso(-3)
    snapshot["courses"][0]["files"].append({"id": fid, "name": name, "content_type": ctype, "size": size, "updated_at": updated_at,
                                           "download_url": f"https://school.instructure.com/files/{fid}/download",
                                           "locked": False, "sources": ["files_tab"]})
    manifest.append({"id": fid, "updated_at": updated_at, "size": size, "name": name, "content_type": ctype,
                     "course_id": "101", "path": f"Calculus I/{name}"})


def _file(canvas_id):
    return db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == canvas_id))


def test_same_file_under_two_canvas_ids_is_uploaded_once(app, client, snapshot, manifest):
    """Same name, type and size = the same file: one download, both entries point at it."""
    _add_file(snapshot, manifest, "9003", "notes.txt", "text/plain", 120)  # 9002's twin
    token = api_token(make_user())
    body, uploaded = sync(client, token, snapshot, manifest, {"9002": b"x" * 120, "9003": b"x" * 120})
    assert sorted(uploaded) == ["9001", "9002"], "the second copy is not requested"
    a, b = _file("9002"), _file("9003")
    assert a.storage_key and a.storage_key == b.storage_key and b.is_stored
    assert b.text == a.text and b.text_status == "ok"
    course = db.session.get(Course, a.course_id)
    assert [f.name for f in course.listed_files].count("notes.txt") == 1, "listed once"

    # Next sync: nothing to download.
    body, uploaded = sync(client, token, snapshot, manifest)
    assert uploaded == [] and body["files_needed"] == []


def test_same_name_and_type_but_different_size_is_a_different_file(app, client, snapshot, manifest):
    _add_file(snapshot, manifest, "9003", "notes.txt", "text/plain", 999)
    _, uploaded = sync(client, api_token(make_user()), snapshot, manifest)
    assert sorted(uploaded) == ["9001", "9002", "9003"]


def test_settings_only_change_is_not_downloaded_again(app, client, snapshot, manifest):
    """Canvas bumps updated_at when a file is renamed back, re-published, moved... Same name,
    type and size means we still hold it."""
    token = api_token(make_user())
    sync(client, token, snapshot, manifest)
    key = _file("9002").storage_key
    snapshot["courses"][0]["files"][1]["updated_at"] = manifest[1]["updated_at"] = iso(-1)
    body, uploaded = sync(client, token, snapshot, manifest)
    assert uploaded == []
    f = _file("9002")
    assert f.is_stored and f.storage_key == key

    # A real edit changes the size: that one is downloaded.
    snapshot["courses"][0]["files"][1]["size"] = manifest[1]["size"] = 150
    snapshot["courses"][0]["files"][1]["updated_at"] = manifest[1]["updated_at"] = iso(0)
    _, uploaded = sync(client, token, snapshot, manifest, {"9002": b"y" * 150})
    assert uploaded == ["9002"] and _file("9002").storage_key != key


def test_shared_object_survives_when_one_copy_changes(app, client, snapshot, manifest):
    from app.services.storage import get_storage

    _add_file(snapshot, manifest, "9003", "notes.txt", "text/plain", 120)
    token = api_token(make_user())
    sync(client, token, snapshot, manifest, {"9002": b"x" * 120})
    shared_key = _file("9003").storage_key
    # 9002 gets a new version of its own; 9003 still uses the old object.
    snapshot["courses"][0]["files"][1]["size"] = manifest[1]["size"] = 130
    snapshot["courses"][0]["files"][1]["updated_at"] = manifest[1]["updated_at"] = iso(0)
    sync(client, token, snapshot, manifest, {"9002": b"z" * 130})
    assert _file("9002").storage_key != shared_key == _file("9003").storage_key
    assert get_storage().read(shared_key) == b"x" * 120, "not deleted while a copy uses it"


def test_files_stored_before_fingerprints_are_not_downloaded_again(app, client, snapshot, manifest):
    token = api_token(make_user())
    sync(client, token, snapshot, manifest)
    from app.models import CanvasAccount

    for f in db.session.scalars(select(CanvasFile)):
        f.stored_fingerprint = f.wanted_fingerprint = None  # as deployed before this change
    for a in db.session.scalars(select(CanvasAccount)):
        a.last_snapshot_hash = None  # ...which also predates skipping unchanged syncs
    db.session.commit()
    body, uploaded = sync(client, token, snapshot, manifest)
    assert uploaded == [] and all(f.stored_fingerprint for f in db.session.scalars(select(CanvasFile)) if f.storage_key)



def test_unchanged_sync_skips_the_classes_but_still_retries_missing_files(app, client, snapshot, manifest):
    from sqlalchemy import event

    from app.models import SyncRun

    token = api_token(make_user())
    auth = {"Authorization": f"Bearer {token}"}
    first = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    assert sorted(first["files_needed"]) == ["9001", "9002"]
    # 9002 uploads, 9001 fails this time.
    client.put(f"/v1/files/9002?updated_at={manifest[1]['updated_at']}", data=b"x" * 120,
               headers={**auth, "X-Snapshot-Id": first["snapshot_id"], "Content-Type": "text/plain"})

    statements = []
    listener = lambda *a: statements.append(a[2])
    event.listen(db.engine, "before_cursor_execute", listener)
    snapshot["synced_at"] = iso(0)  # an hour later, nothing else changed
    again = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    event.remove(db.engine, "before_cursor_execute", listener)
    assert again["files_needed"] == ["9001"], "the failed file is still requested"
    assert db.session.get(SyncRun, again["snapshot_id"]).stats["unchanged"] is True
    assert not any("assignment" in s.lower() and s.lstrip().upper().startswith("SELECT") for s in statements), \
        "classes aren't re-read"
    assert len(statements) < 15, statements

    # A real change goes through the full sync again.
    snapshot["courses"][0]["assignments"][0]["name"] = "HW 1 (renamed)"
    changed = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    assert not db.session.get(SyncRun, changed["snapshot_id"]).stats.get("unchanged")
    assert db.session.scalar(select(Assignment).where(Assignment.name == "HW 1 (renamed)"))
