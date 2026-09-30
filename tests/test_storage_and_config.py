"""Object storage (Supabase/S3 via a local S3 emulator) and Supabase/Render configuration."""

from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from sqlalchemy import select

from app import create_app
from app.config import engine_options, load_config, normalize_database_url, validate_production
from app.extensions import db
from app.models import CanvasFile

from .conftest import FakeAI, api_token, login, make_user, sync


@pytest.fixture(scope="module")
def s3_server():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def s3_app(tmp_path, s3_server, monkeypatch):
    import boto3

    bucket = f"hh-{tmp_path.name.lower().replace('_', '-')}"[:60]
    boto3.client("s3", endpoint_url=s3_server, region_name="us-east-1", aws_access_key_id="k",
                 aws_secret_access_key="s").create_bucket(Bucket=bucket)
    app = create_app("test", {"STORAGE_BACKEND": "supabase", "S3_BUCKET": bucket, "S3_ENDPOINT_URL": s3_server,
                              "S3_REGION": "us-east-1", "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s",
                              "SERVER_NAME": "hatch.test"})
    app.extensions["hh_ai"] = FakeAI()

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


def test_s3_storage_roundtrip_signed_urls_and_prefix_delete(s3_app):
    import io

    from app.services.storage import get_storage, safe_key_part

    st = get_storage()
    st.put_file("u/1/files/a/notes.pdf", io.BytesIO(b"%PDF one"), "application/pdf")
    st.put_file("u/12/files/b/other.pdf", io.BytesIO(b"%PDF two"), "application/pdf")
    st.put_bytes("u/1/snapshots/latest.json", b"{}", "application/json")
    assert st.read("u/1/files/a/notes.pdf") == b"%PDF one"

    url = st.signed_url("u/1/files/a/notes.pdf", "Week 1 — notes.pdf", "application/pdf", True, 60)
    q = parse_qs(urlsplit(url).query)
    assert "inline" in q["response-content-disposition"][0] and "X-Amz-Signature" in q
    r = requests.get(url, timeout=10)
    assert r.status_code == 200 and r.content == b"%PDF one"
    assert r.headers["Content-Type"] == "application/pdf"

    st.delete_prefix("u/1/")
    with pytest.raises(Exception):
        st.read("u/1/files/a/notes.pdf")
    assert st.read("u/12/files/b/other.pdf") == b"%PDF two", "deleting u/1/ must not touch u/12/"
    with pytest.raises(ValueError):
        st.delete_prefix("/")
    assert safe_key_part("a/b\\c?.pdf") == "a_b_c_.pdf"


def test_sync_upload_and_download_through_object_storage(s3_app, snapshot, manifest):
    client = s3_app.test_client()
    user = make_user()
    sync(client, api_token(user), snapshot, manifest, {"9002": b"The chain rule, stored in object storage."})
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    assert f.storage_key.endswith("/notes.txt") and f.text_status == "ok" and f.sha256
    login(client, user)
    r = client.get(f"/courses/files/{f.id}/download")
    assert r.status_code == 302, "downloads redirect to a signed URL instead of streaming through the app"
    assert requests.get(r.headers["Location"], timeout=10).content == b"The chain rule, stored in object storage."

    # A newer version replaces the old object.
    old_key = f.storage_key
    manifest[1]["updated_at"], manifest[1]["size"] = "2030-01-01T00:00:00Z", 2
    sync(client, api_token(user, raw="hh_second_token_" + "y" * 20), snapshot, manifest, {"9002": b"v2"})
    db.session.refresh(f)
    assert f.storage_key != old_key
    from app.services.storage import get_storage

    with pytest.raises(Exception):
        get_storage().read(old_key)


def test_oversized_upload_is_rejected_cleanly(s3_app, snapshot, manifest):
    s3_app.config["MAX_FILE_MB"] = 1
    client = s3_app.test_client()
    user = make_user()
    token = api_token(user)
    auth = {"Authorization": f"Bearer {token}"}
    run = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    r = client.put(f"/v1/files/9002?updated_at={manifest[1]['updated_at']}", data=b"x" * (2 * 1024 * 1024),
                   headers={**auth, "X-Snapshot-Id": run["snapshot_id"]})
    assert r.status_code == 413


def test_direct_upload_to_storage_then_confirm(s3_app, snapshot, manifest):
    """The extension PUTs bytes straight to storage with a presigned URL; the app only confirms."""
    import hashlib

    from app.services.storage import get_storage

    client = s3_app.test_client()
    user = make_user()
    auth = {"Authorization": f"Bearer {api_token(user)}"}
    body = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    assert set(body["upload_urls"]) == set(body["files_needed"]) == {"9001", "9002"}
    target = body["upload_urls"]["9002"]
    assert target["headers"] == {"Content-Type": "text/plain"}
    q = f"?updated_at={manifest[1]['updated_at']}"
    hdrs = {**auth, "X-Snapshot-Id": body["snapshot_id"]}

    # Confirming before the bytes arrived is refused.
    assert client.post(f"/v1/files/9002/uploaded{q}", headers=hdrs).status_code == 400

    data = b"Direct to storage: the chain rule."
    assert requests.put(target["url"], data=data, headers=target["headers"], timeout=10).status_code == 200
    r = client.post(f"/v1/files/9002/uploaded{q}", headers=hdrs)
    assert r.status_code == 200, r.get_json()
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    assert f.is_stored and f.size == len(data)
    assert f.text_status == "ok" and "chain rule" in f.text, "text read back from storage"
    assert f.sha256 == hashlib.sha256(data).hexdigest()
    assert get_storage().read(f.storage_key) == data
    from app.models import SyncRun

    assert db.session.get(SyncRun, body["snapshot_id"]).files_uploaded == 1

    # Over the size limit: the object is removed and the version isn't requested again.
    s3_app.config["MAX_FILE_MB"] = 0
    assert requests.put(body["upload_urls"]["9001"]["url"], data=b"%PDF big", timeout=10,
                        headers=body["upload_urls"]["9001"]["headers"]).status_code == 200
    r = client.post(f"/v1/files/9001/uploaded?updated_at={manifest[0]['updated_at']}", headers=hdrs)
    assert r.status_code == 413
    big = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9001"))
    assert big.text_status == "too_large" and big.storage_key is None
    again = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest}, headers=auth).get_json()
    assert "9001" not in again["files_needed"], "a rejected file isn't requested every sync"


def test_local_storage_has_no_upload_urls(client, snapshot, manifest):
    user = make_user()
    body = client.post("/v1/snapshots", json={"snapshot": snapshot, "files": manifest},
                       headers={"Authorization": f"Bearer {api_token(user)}"}).get_json()
    assert body["files_needed"] and body["upload_urls"] == {}


def test_text_is_read_in_a_background_thread(tmp_path, snapshot, manifest):
    import time

    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/bg.db", "STORAGE_DIR": str(tmp_path / "st"),
                              "EXTRACT_INLINE": False})
    with app.app_context():
        db.create_all()
        user = make_user()
        body, _ = sync(app.test_client(), api_token(user), snapshot, manifest, {"9002": b"Read me later, in the background."})
        f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
        for _ in range(100):
            db.session.expire_all()
            if f.text_status == "ok":
                break
            time.sleep(0.05)
        assert f.text_status == "ok" and "background" in f.text and f.sha256 and f.text_started_at is None
        db.session.remove()
        db.drop_all()


def test_stale_text_claims_are_retried(app, snapshot, manifest):
    from datetime import timedelta

    from app.models import utcnow
    from app.services import textjobs

    user = make_user()
    sync(app.test_client(), api_token(user), snapshot, manifest, {"9002": b"Retry me."})
    f = db.session.scalar(select(CanvasFile).where(CanvasFile.canvas_id == "9002"))
    f.text, f.text_status, f.text_started_at = None, "extracting", utcnow()  # a reader is on it
    db.session.commit()
    assert textjobs.run_pending() == 0
    f.text_started_at = utcnow() - timedelta(minutes=30)  # ...and died with a restart
    db.session.commit()
    assert textjobs.run_pending() == 1
    db.session.refresh(f)
    assert f.text_status == "ok" and f.text == "Retry me."


# ---------------------------------------------------------------- configuration


def test_database_urls_from_supabase_and_render():
    pooler = "postgresql://postgres.abcd:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
    url = normalize_database_url(pooler)
    assert url.startswith("postgresql+psycopg://postgres.abcd:pw@") and url.endswith("?sslmode=require")
    assert normalize_database_url("postgres://u:p@host:5432/db") == "postgresql+psycopg://u:p@host:5432/db"
    assert "sslmode=verify-full" in normalize_database_url(pooler + "?sslmode=verify-full")
    assert "sslmode=require" not in normalize_database_url("postgres://u:p@localhost:5432/db"), "only Supabase hosts"
    assert normalize_database_url("").startswith("sqlite:///")

    session = engine_options(normalize_database_url(pooler))
    assert session["pool_pre_ping"] and session["pool_size"] == 3 and "connect_args" not in session
    transaction = engine_options(normalize_database_url(pooler.replace(":5432/", ":6543/")))
    assert transaction["connect_args"] == {"prepare_threshold": None}, "Supavisor transaction mode can't prepare"
    assert transaction["poolclass"].__name__ == "NullPool"
    assert engine_options("sqlite://") == {} and engine_options("sqlite:///x.db")["connect_args"]["timeout"] == 20


def test_supabase_storage_endpoint_and_production_checks(monkeypatch):
    for key in ("S3_ENDPOINT_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "SECRET_KEY", "MAX_FILE_MB"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "supabase")
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefgh.supabase.co")
    cfg = load_config("production")
    assert cfg["S3_ENDPOINT_URL"] == "https://abcdefgh.storage.supabase.co/storage/v1/s3"
    assert cfg["MAX_FILE_MB"] == 50, "Supabase Free caps files at 50 MB"
    problems = " ".join(validate_production(cfg))
    for needle in ("SECRET_KEY", "DATABASE_URL", "S3_BUCKET", "S3_ACCESS_KEY_ID"):
        assert needle in problems
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.abcd:pw@aws-1-us-east-1.pooler.supabase.com:5432/postgres")
    monkeypatch.setenv("SUPABASE_BUCKET", "canvas-files")
    monkeypatch.setenv("SUPABASE_S3_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("SUPABASE_S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("SUPABASE_S3_REGION", "us-east-1")
    assert validate_production(load_config("production")) == []
    with pytest.raises(RuntimeError):
        monkeypatch.setenv("SECRET_KEY", "dev-insecure-change-me")
        create_app("production")


def test_health_endpoints(client, app):
    assert client.get("/health").get_json()["ok"] is True
    body = client.get("/health/db").get_json()
    assert body["ok"] is True and body["database"] in {"sqlite", "postgresql"}
