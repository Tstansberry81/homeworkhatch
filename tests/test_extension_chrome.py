"""The shipped extension in a real Chrome against a live server with S3-style storage.

Opt-in: needs Chrome for Testing (branded Chrome ignores --load-extension) and the extension's
dev dependencies:

    cd extension && npm install
    CHROME_PATH=".../Google Chrome for Testing" pytest tests/test_extension_chrome.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db
from app.models import Card, CanvasFile, Deck

from .conftest import FakeAI, make_user

ROOT = Path(__file__).resolve().parent.parent
CHROME = os.environ.get("CHROME_PATH")

pytestmark = pytest.mark.skipif(
    not CHROME or shutil.which("node") is None or not (ROOT / "extension/node_modules/puppeteer-core").exists(),
    reason="set CHROME_PATH to Chrome for Testing and run npm install in extension/")


def test_extension_in_chrome(tmp_path):
    import boto3
    from moto.server import ThreadedMotoServer

    s3 = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    s3.start()
    host, port = s3.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1", aws_access_key_id="k",
                 aws_secret_access_key="s").create_bucket(Bucket="e2e")
    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'e2e.db'}", "STORAGE_BACKEND": "s3",
                              "S3_BUCKET": "e2e", "S3_ENDPOINT_URL": endpoint, "S3_REGION": "us-east-1",
                              "S3_ACCESS_KEY_ID": "k", "S3_SECRET_ACCESS_KEY": "s"})
    app.extensions["hh_ai"] = FakeAI()
    hits = Counter()

    @app.before_request
    def _count():
        from flask import request

        hits[f"{request.method} {request.url_rule.rule if request.url_rule else request.path}"] += 1

    # Chrome gives an unpacked extension an id derived from its folder's path.
    ext_dir = tmp_path / "extension"
    app.config["EXTENSION_IDS"] = [
        "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(p.encode()).hexdigest()[:32])
        for p in {str(ext_dir), os.path.realpath(ext_dir)}]
    with app.app_context():
        db.create_all()
        user = make_user("chrome")
        deck = Deck(user_id=user.id, title="Review check", cards=[Card(front=f"q{i}", back=f"a{i}", position=i) for i in range(3)])
        db.session.add(deck)
        db.session.commit()
        deck_id = deck.id
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run = subprocess.run(["node", str(ROOT / "tests/js/extension_chrome.mjs"), f"http://127.0.0.1:{server.server_port}",
                              "chrome", "password123", CHROME, str(ext_dir), str(deck_id)], capture_output=True, text=True, timeout=240)
        assert run.returncode == 0, run.stderr[-3000:]
        r = json.loads(run.stdout.strip().splitlines()[-1])
    finally:
        server.shutdown()
        s3.stop()

    assert r["opened_setup_page"], "first install opens the token page"
    server_url = f"http://127.0.0.1:{server.server_port}"
    assert r["default_server"] == server_url, "the hosted server is the built-in default"
    assert r["linked"] == {"server": server_url, "token_looks_right": True}, "the site linked the extension by itself"
    assert "Linked and syncing from 127.0.0.1" in r["site_status"] and "last sync" in r["site_status"], r["site_status"]
    assert r["alarm_minutes"] == 60, "hourly auto-sync is scheduled"

    # Mock Canvas: 5 distinct files, plus notes.pdf posted again under another Canvas id.
    assert r["first"] == {"state": "ok", "error": None, "pushError": None, "uploaded": 5, "skipped": 1, "failed": 0}, r["first"]
    assert r["second"]["uploaded"] == 0 and r["second"]["skipped"] == 6, "nothing is downloaded twice"
    assert "no new files (all 6 already on Homework Hatch)" in r["popup"]["summary"]
    assert "every 60 min" in r["popup"]["auto"] and "next at" in r["popup"]["auto"]
    assert r["popup"]["download"].startswith("Download 5 files (.zip, ~"), r["popup"]
    assert r["popup"]["downloadAll"] is None, "no 'all' button while nothing was downloaded yet"

    assert len(r["zip1"]) == 5 and sum(p.endswith("/notes.pdf") for p in r["zip1"]) == 1, r["zip1"]
    assert r["after_zip"] == {"download": "No new files to download", "downloadAll": "Download all 5", "disabled": True}

    assert r["third"]["uploaded"] == 1 and r["third"]["skipped"] == 6, r["third"]
    assert r["new_download_label"].startswith("Download 1 new file (.zip, ~"), r["new_download_label"]
    assert [p.rsplit("/", 1)[-1] for p in r["zip2"]] == ["week2.pdf"]
    assert len(r["zip_all"]) == 6

    # Flashcards: flip, next and previous (wrapping around), and the x / n counter.
    assert r["study"] == {"start": "1 / 3", "flipped": True, "after_next": "2 / 3", "flipped_after_next": False,
                          "wrapped": "3 / 3", "back_text": "a2"}, r["study"]

    # Bytes went straight to storage: the app only confirmed them.
    assert hits["POST /v1/files/<file_id>/uploaded"] == 6 and hits["PUT /v1/files/<file_id>"] == 0, hits
    with app.app_context():
        files = db.session.scalars(select(CanvasFile)).all()
        assert all(f.is_stored for f in files if f.canvas_id in {"1", "2", "3", "4", "5", "77", "78"})
        notes = [f for f in files if f.name == "notes.pdf"]
        assert len(notes) == 2 and notes[0].storage_key == notes[1].storage_key, "one stored copy for both ids"
