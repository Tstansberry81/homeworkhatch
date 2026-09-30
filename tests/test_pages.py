import hashlib
import hmac
import io
import json
import time
import zipfile

import pytest
from sqlalchemy import select

from app.extensions import db
from app.models import (Assignment, CanvasFile, CoinTransaction, Course, Deck, Page, PracticeQuiz, User)
from app.services import coins

from .conftest import login, make_user


def test_public_pages(client):
    for path in ("/", "/login", "/register", "/terms", "/privacy", "/support", "/live/join", "/health"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/dashboard").status_code == 302, "login required"
    assert client.get("/nope").status_code == 404
    assert client.get("/arcade").status_code == 404, "the arcade was removed"


def test_every_student_page_renders(app, synced_user, client):
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    a = db.session.scalar(select(Assignment).where(Assignment.canvas_id == "1001"))
    page = db.session.scalar(select(Page))
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    deck = Deck(user_id=synced_user.id, title="D")
    quiz = PracticeQuiz(user_id=synced_user.id, title="Q", questions=[{"question": "q", "choices": ["a", "b"], "answer": 0, "explanation": ""}])
    db.session.add_all([deck, quiz])
    db.session.commit()
    paths = ["/dashboard", "/welcome", "/courses/", "/calendar", "/calendar?y=2026&m=2", "/study/",
             "/study/generate", "/study/decks/new", f"/study/decks/{deck.id}", f"/study/decks/{deck.id}/review",
             "/study/quizzes/new", f"/study/quizzes/{quiz.id}",
             f"/study/quizzes/{quiz.id}/edit", "/tutor/", "/chat/", f"/chat/course/{course.id}", "/coins", "/tools/citations", "/billing/",
             "/settings/", "/settings/sync", "/settings/data", f"/courses/assignments/{a.id}", f"/courses/pages/{page.id}",
             f"/courses/files/{f.id}", "/live/join"]
    paths += [f"/courses/{course.id}?tab={t}" for t in ("overview", "assignments", "grades", "modules", "pages", "files", "announcements")]
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"
    dash = client.get("/dashboard").get_data(as_text=True)
    assert "HW 3" in dash and "Quiz 1" in dash and "Midterm Exam" in dash
    assert "Placement" not in dash, "hidden course stays off the dashboard"
    grades_tab = client.get(f"/courses/{course.id}?tab=grades").get_data(as_text=True)
    assert "What-if calculator" in grades_tab


def test_downloads_are_owner_only(app, synced_user, client):
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    r = client.get(f"/courses/files/{f.id}/download")
    assert r.status_code == 200 and b"chain rule" in r.data
    other = make_user("mallory")
    c2 = app.test_client()
    login(c2, other)
    assert c2.get(f"/courses/files/{f.id}/download").status_code == 404
    assert c2.get(f"/courses/files/{f.id}").status_code == 404


def test_what_if_endpoint(synced_user, client):
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    midterm = db.session.scalar(select(Assignment).where(Assignment.canvas_id == "1004"))
    base = client.post(f"/courses/{course.id}/what-if", json={"scores": {}}).get_json()["percent"]
    better = client.post(f"/courses/{course.id}/what-if", json={"scores": {str(midterm.id): 100}}).get_json()["percent"]
    assert better > base
    need = client.post(f"/courses/{course.id}/needed", json={"assignment_id": midterm.id, "goal": 80}).get_json()
    assert need["needed"] is not None


def test_mark_done_and_hide_course(synced_user, client):
    quiz1 = db.session.scalar(select(Assignment).where(Assignment.canvas_id == "1005"))
    client.post(f"/assignments/{quiz1.id}/done")
    db.session.refresh(quiz1)
    assert quiz1.effective_status == "done"
    assert "Quiz 1" not in client.get("/dashboard").get_data(as_text=True).split("Up next")[1].split("Grades")[0]
    course = db.session.scalar(select(Course).where(Course.canvas_id == "101"))
    client.post(f"/courses/{course.id}/visibility")
    assert "HW 3" not in client.get("/dashboard").get_data(as_text=True)


def test_calendar_feed(app, synced_user, client):
    r = client.get(f"/calendar/{synced_user.calendar_token}.ics")
    assert r.mimetype == "text/calendar"
    body = r.get_data(as_text=True).replace("\r\n ", "")
    assert "SUMMARY:Due: HW 3 (Calculus I)" in body and "SUMMARY:Review session" in body
    assert client.get("/calendar/wrong-token.ics").status_code == 404


def test_extension_download_zip(app, synced_user, client):
    r = client.get("/extension.zip")
    if r.status_code == 404:
        pytest.skip("extension folder not present")
    names = zipfile.ZipFile(io.BytesIO(r.data)).namelist()
    assert "homework-hatch-extension/manifest.json" in names
    assert not any("/tests/" in n or "node_modules" in n for n in names)


# ---------------------------------------------------------------- accounts


def test_register_first_user_is_admin_then_login(client):
    r = client.post("/register", data={"email": "first@example.com", "username": "first", "password": "longenough",
                                       "terms": "on", "timezone": "America/Chicago"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/welcome")
    user = db.session.scalar(select(User))
    assert user.is_admin and user.timezone == "America/Chicago"
    bad = client.post("/register", data={"email": "x", "username": "a", "password": "short"})
    assert bad.status_code == 400 or bad.status_code == 302


def test_registration_validation_and_approval(app):
    client = app.test_client()
    make_user("existing")
    r = client.post("/register", data={"email": "existing@example.com", "username": "Existing", "password": "longenough", "terms": "on"})
    assert r.status_code == 400 and b"already exists" in r.data and b"taken" in r.data
    app.config["REQUIRE_APPROVAL"] = True
    r = client.post("/register", data={"email": "new@example.com", "username": "newbie", "password": "longenough", "terms": "on"})
    assert b"on the list" in r.data
    r = client.post("/login", data={"identifier": "newbie", "password": "longenough"})
    assert r.status_code == 403


def test_login_rejects_bad_password_and_open_redirects(client):
    user = make_user()
    assert client.post("/login", data={"identifier": "sam", "password": "wrong"}).status_code == 401
    r = client.post("/login?next=https://evil.example/", data={"identifier": "sam", "password": "password123"})
    assert r.headers["Location"].endswith("/dashboard")
    user.active = False
    db.session.commit()


def test_export_and_delete_account(app, synced_user, client):
    data = json.loads(client.get("/settings/data/export").data)
    assert data["profile"]["username"] == "sam" and data["courses"]
    r = client.post("/settings/data/delete", data={"confirm": "sam", "password": "nope"})
    assert db.session.get(User, synced_user.id) is not None
    uid = synced_user.id
    r = client.post("/settings/data/delete", data={"confirm": "sam", "password": "password123"})
    assert r.status_code == 302
    db.session.expire_all()
    assert db.session.get(User, uid) is None
    assert db.session.scalar(select(Course).where(Course.user_id == uid)) is None, "cascade removes synced data"


def test_token_lifecycle(app, client):
    user = make_user()
    login(client, user)
    r = client.post("/settings/tokens", data={"name": "Laptop"})
    token = r.get_data(as_text=True).split('id="tok"')[1].split(">")[1].split("<")[0]
    assert token.startswith("hh_")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# ---------------------------------------------------------------- billing


def _signed(payload: dict, secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


def test_stripe_webhook_updates_plan(app, client):
    app.config.update(STRIPE_WEBHOOK_SECRET="whsec_test", STRIPE_SECRET_KEY="sk_test_x", STRIPE_PRICE_PREMIUM="price_prem")
    user = make_user()
    body, sig = _signed({"id": "evt_1", "object": "event", "type": "checkout.session.completed",
                         "data": {"object": {"client_reference_id": str(user.id), "customer": "cus_1", "subscription": "sub_1",
                                             "metadata": {"plan": "premium"}}}}, "whsec_test")
    r = client.post("/billing/webhook", data=body, headers={"Stripe-Signature": sig, "Content-Type": "application/json"})
    assert r.status_code == 200
    db.session.refresh(user)
    assert user.plan == "premium" and user.stripe_customer_id == "cus_1"

    body, sig = _signed({"id": "evt_2", "object": "event", "type": "customer.subscription.deleted",
                         "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled", "items": {"data": []}}}},
                        "whsec_test")
    client.post("/billing/webhook", data=body, headers={"Stripe-Signature": sig, "Content-Type": "application/json"})
    db.session.refresh(user)
    assert user.plan == "free"

    forged, _ = _signed({"type": "checkout.session.completed", "data": {"object": {}}}, "whsec_test")
    assert client.post("/billing/webhook", data=forged, headers={"Stripe-Signature": "t=1,v1=bad"}).status_code == 400


def test_plan_quota_limits(app):
    from app.services import ai, billing

    user = make_user()
    assert billing.plan_for(user).key == "free" and ai.remaining(user) == 25
    user.plan, user.plan_status = "pro", "active"
    assert ai.remaining(user) == 2000
    user.plan_status = "canceled"
    assert billing.plan_for(user).key == "free", "a lapsed subscription falls back to free"
    user.plan_comped = True
    assert billing.plan_for(user).key == "pro", "admin-comped plans don't need Stripe"


# ---------------------------------------------------------------- admin


def test_admin_area(app, client):
    admin = make_user("boss", is_admin=True)
    student = make_user("kid")
    c = app.test_client()
    login(c, student)
    assert c.get("/admin/").status_code == 403
    login(client, admin)
    for path in ("/admin/", "/admin/users", "/admin/users?q=kid", f"/admin/users/{student.id}", "/admin/reports"):
        assert client.get(path).status_code == 200, path
    before = coins.balance(student.id)  # includes today's +1 check-in
    client.post(f"/admin/users/{student.id}", data={"action": "coins", "amount": "50", "reason": "contest"})
    assert coins.balance(student.id) == before + 50
    client.post(f"/admin/users/{student.id}", data={"action": "plan", "plan": "pro"})
    db.session.refresh(student)
    assert student.plan == "pro" and student.plan_comped
    client.post(f"/admin/users/{student.id}", data={"action": "toggle_active"})
    db.session.refresh(student)
    assert student.active is False
    reasons = db.session.scalars(select(CoinTransaction.reason).where(CoinTransaction.user_id == student.id)).all()
    assert "Admin: contest" in reasons


# ---------------------------------------------------------------- probability lab


def test_probability_lab_is_adult_and_flag_gated(app, synced_user, client):
    assert client.get("/lab").status_code == 302, "no birth year -> not adult"
    synced_user.birth_year = 2000
    db.session.commit()
    assert client.get("/lab").status_code == 404, "feature flag off by default"
    app.config["FEATURE_SIMULATIONS"] = True
    assert client.get("/lab").status_code == 200
    coins.award(synced_user.id, 100, "seed", "seed2")
    db.session.commit()
    r = client.post("/lab/roll", json={"wager": 10, "target": 7}).get_json()
    assert len(r["dice"]) == 2 and r["expected_value"] < 0
    assert client.post("/lab/roll", json={"wager": 500, "target": 7}).status_code == 400


def test_seed_demo_command(app):
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert "Demo account ready" in result.output, result.output
    demo = db.session.scalar(select(User).where(User.username == "demo"))
    courses = db.session.scalars(select(Course).where(Course.user_id == demo.id)).all()
    assert len(courses) == 4
    assert db.session.scalars(select(CanvasFile).where(CanvasFile.user_id == demo.id, CanvasFile.text_status == "ok")).all()
    again = app.test_cli_runner().invoke(args=["seed-demo"])  # re-running is safe
    assert "Demo account ready" in again.output


def test_supabase_lockdown_enables_rls_and_revokes_api_roles(app):
    """On Postgres: every table gets RLS, and Supabase's API roles can't read anything."""
    from sqlalchemy import text

    from app.services.dbsecurity import lock_down_public_schema

    if db.engine.dialect.name != "postgresql":
        pytest.skip("Postgres-only (run with TEST_DATABASE_URL)")
    with db.engine.begin() as conn:
        for role in ("anon", "authenticated"):
            conn.execute(text(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                              f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$;"))
        conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon, authenticated"))
        lock_down_public_schema(conn)
        lock_down_public_schema(conn)  # idempotent
        no_rls = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                                   "AND NOT rowsecurity")).scalars().all()
        leaks = conn.execute(text("SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace "
                                  "AND c.relkind = 'r' AND has_table_privilege('anon', c.oid, 'SELECT')")).scalars().all()
    assert no_rls == [] and leaks == []
    # The app (table owner) still reads and writes normally.
    make_user("owner")
    assert db.session.scalar(select(User.username).where(User.username == "owner")) == "owner"


def test_stripe_ignores_other_subscriptions_and_blocks_double_checkout(app, client):
    app.config.update(STRIPE_WEBHOOK_SECRET="whsec_test", STRIPE_SECRET_KEY="sk_test_x", STRIPE_PRICE_PRO="price_pro")
    user = make_user()
    user.plan, user.plan_status, user.stripe_customer_id, user.stripe_subscription_id = "pro", "active", "cus_1", "sub_live"
    db.session.commit()
    body, sig = _signed({"id": "evt_3", "object": "event", "type": "customer.subscription.deleted",
                         "data": {"object": {"id": "sub_old", "customer": "cus_1", "status": "canceled",
                                             "items": {"data": []}}}}, "whsec_test")
    client.post("/billing/webhook", data=body, headers={"Stripe-Signature": sig, "Content-Type": "application/json"})
    db.session.refresh(user)
    assert user.plan == "pro", "an old subscription's cancellation doesn't downgrade the paying user"
    login(client, user)
    r = client.post("/billing/checkout/pro")
    assert r.status_code == 302 and r.headers["Location"].endswith("/billing/portal"), "no second subscription"


def test_login_lockout_and_password_change_ends_sessions(app):
    user = make_user("locky")
    c = app.test_client()
    for _ in range(10):
        c.post("/login", data={"identifier": "locky", "password": "wrong"})
    r = c.post("/login", data={"identifier": "locky", "password": "password123"})
    assert r.status_code == 429, "locked after 10 failures, even with the right password"

    other = make_user("sess")
    a, b = app.test_client(), app.test_client()
    login(a, other), login(b, other)
    assert a.post("/settings/", data={"action": "password", "current": "password123", "new": "newpassword1"}).status_code == 302
    assert b.get("/dashboard").status_code == 302, "the other session was signed out by the password change"


def test_no_automatic_admin_in_production(app):
    app.config["ENV_NAME"] = "production"
    c = app.test_client()
    c.post("/register", data={"email": "first@example.com", "username": "firstone", "password": "longenough", "terms": "on"})
    assert db.session.scalar(select(User).where(User.username == "firstone")).is_admin is False
    app.config["ADMIN_EMAIL"] = "boss@example.com"
    app.test_client().post("/register", data={"email": "boss@example.com", "username": "bossy", "password": "longenough", "terms": "on"})
    assert db.session.scalar(select(User).where(User.username == "bossy")).is_admin is True
