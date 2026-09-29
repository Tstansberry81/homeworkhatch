"""The real extension JavaScript (canvas.js + upload.js) talking to a live Flask server."""

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from sqlalchemy import select
from werkzeug.serving import make_server

from app import create_app
from app.extensions import db
from app.models import CanvasFile, Course, User

from .conftest import FakeAI, api_token, make_user

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_extension_uploads_to_flask(tmp_path):
    # A file-backed database, because the server runs on another thread.
    app = create_app("test", {"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'e2e.db'}",
                              "STORAGE_DIR": str(tmp_path / "storage")})
    app.extensions["hh_ai"] = FakeAI()
    with app.app_context():
        db.create_all()
        token = api_token(make_user("node"))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        out = subprocess.run(["node", str(ROOT / "tests/js/extension_roundtrip.mjs"),
                              f"http://127.0.0.1:{server.server_port}", token],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        server.shutdown()

    assert result["planned"] >= 4
    assert result["first"]["uploaded"] == result["planned"] and result["first"]["failed"] == []
    assert result["second"]["uploaded"] == 0 and result["second"]["skipped"] == result["planned"], \
        "an unchanged re-sync uploads nothing"
    with app.app_context():
        user = db.session.scalar(select(User).where(User.username == "node"))
        courses = db.session.scalars(select(Course).where(Course.user_id == user.id)).all()
        assert len(courses) == result["courses"]
        stored = db.session.scalars(select(CanvasFile).where(CanvasFile.user_id == user.id,
                                                             CanvasFile.storage_key.is_not(None))).all()
        assert len(stored) == result["planned"]
        assert all(f.is_stored for f in stored)
