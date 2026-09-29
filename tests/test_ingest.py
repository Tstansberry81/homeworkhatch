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

    manifest[1]["updated_at"] = iso(0)  # instructor replaced notes.txt
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
