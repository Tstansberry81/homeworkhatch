"""Endpoints the browser extension uploads to (protocol in extension/upload.js).

Authenticated with a per-user bearer token created on the "Connect Canvas" page. The
extension calls from a chrome-extension:// origin, so these routes answer CORS.
"""

from __future__ import annotations

import hashlib
import json

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select

from ..extensions import db
from ..models import ApiToken, SyncRun, User, utcnow
from ..services import gcal, ingest, integrations, textjobs
from ..services.storage import StorageError, TooLarge

bp = Blueprint("api", __name__, url_prefix="/v1")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Snapshot-Id",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Max-Age": "600",
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _error(status: int, message: str):
    return jsonify({"error": message}), status


@bp.after_request
def cors(response):
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response


@bp.before_request
def authenticate():
    if request.method == "OPTIONS":
        return current_app.response_class(status=204)
    if request.endpoint == "api.health":
        return None
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    row = db.session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token))) if token else None
    if row is None or row.revoked:
        return _error(401, "invalid or revoked token")
    user = db.session.get(User, row.user_id)
    if user is None or not user.active or not user.is_approved:
        return _error(403, "account disabled")
    # Record use at most once a minute, in its own short transaction: holding that row write
    # for a whole file upload would serialize (and on SQLite, lock) parallel uploads.
    now = utcnow()
    if row.last_used_at is None or (now - row.last_used_at).total_seconds() > 60:
        row.last_used_at = now
        db.session.commit()
    g.api_user = user
    return None


@bp.route("/health")
def health():
    return jsonify({"ok": True, "service": "homework-hatch"})


@bp.route("/me")
def me():
    user = g.api_user
    return jsonify({"id": user.id, "username": user.username, "display_name": user.display_name})


@bp.route("/snapshots", methods=["POST"])
def post_snapshot():
    limit = current_app.config["MAX_SNAPSHOT_MB"] * 1024 * 1024
    if (request.content_length or 0) > limit:
        return _error(413, "snapshot too large")
    try:
        body = json.loads(request.get_data(cache=False) or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "body is not JSON")
    try:
        run, needed = ingest.ingest_snapshot(g.api_user, body.get("snapshot"), body.get("files") or [])
    except ingest.IngestError as exc:
        db.session.rollback()
        return _error(exc.status, str(exc))
    try:
        targets = ingest.upload_targets(run, needed)
    except Exception as exc:  # without presigned URLs the extension uploads through us instead
        current_app.logger.warning("could not presign uploads: %s", exc)
        targets = {}
    textjobs.kick()  # resumes reading any files left pending by a restart
    unchanged = (run.stats or {}).get("unchanged")
    if integrations.available() and gcal.enabled(g.api_user) and (not unchanged or gcal.due_for_refresh(g.api_user)):
        gcal.kick(g.api_user.id)  # new or changed due dates go to Google Calendar
    return jsonify({"snapshot_id": run.id, "files_needed": needed, "upload_urls": targets})


def _run_for(snapshot_id: str | None) -> SyncRun | None:
    if not snapshot_id:
        return None
    run = db.session.get(SyncRun, snapshot_id)
    return run if run and run.user_id == g.api_user.id else None


def _file_request(file_id: str):
    run = _run_for(request.headers.get("X-Snapshot-Id"))
    if run is None:
        return None, _error(400, "unknown snapshot")
    if not file_id.isdigit() and not file_id.replace("-", "").isalnum():
        return None, _error(400, "bad file id")
    return run, None


def _store(run: SyncRun, file_id: str, action):
    updated_at = request.args.get("updated_at")
    try:
        row = action(updated_at)
    except ingest.IngestError as exc:
        db.session.rollback()
        return _error(400, str(exc))
    except TooLarge as exc:
        db.session.rollback()
        ingest.mark_too_large(run, file_id, updated_at)
        return _error(413, str(exc))
    except StorageError as exc:
        db.session.rollback()
        current_app.logger.error("file storage failed: %s", exc)
        return _error(502, "file storage is unavailable; the file will be retried next sync")
    return jsonify({"ok": True, "size": row.size, "sha256": row.sha256, "text": row.text_status})


@bp.route("/files/<file_id>", methods=["PUT"])
def put_file(file_id: str):
    """The file's bytes, sent through the app."""
    run, error = _file_request(file_id)
    if error:
        return error
    if (request.content_length or 0) > current_app.config["MAX_FILE_MB"] * 1024 * 1024:
        return _error(413, "file too large")
    return _store(run, file_id, lambda updated_at: ingest.store_file(
        g.api_user, run, file_id, updated_at, request.stream, request.headers.get("Content-Type")))


@bp.route("/files/<file_id>/uploaded", methods=["POST"])
def file_uploaded(file_id: str):
    """The extension PUT the file straight to storage using a URL from upload_urls."""
    run, error = _file_request(file_id)
    if error:
        return error
    return _store(run, file_id, lambda updated_at: ingest.confirm_upload(g.api_user, run, file_id, updated_at))


@bp.route("/snapshots/<snapshot_id>/complete", methods=["POST"])
def complete(snapshot_id: str):
    run = _run_for(snapshot_id)
    if run is None:
        return _error(404, "unknown snapshot")
    ingest.complete_run(run, request.get_json(silent=True) or {})
    return jsonify({"ok": True})
