"""The student's own files: uploaded from their computer or picked from Google Drive. They can
be filed under a class and used for flashcards, quizzes and tutor chats like Canvas files."""

from __future__ import annotations

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import queries
from ..extensions import db
from ..models import Upload
from ..services import gdrive, integrations, retrieval, sources, uploads
from ..services.storage import StorageError, TooLarge, get_storage

bp = Blueprint("uploads", __name__, url_prefix="/files")


def _upload(upload_id: int) -> Upload:
    row = db.session.get(Upload, upload_id)
    if row is None or row.user_id != current_user.id:
        abort(404)
    return row


def _course_id(value) -> int | None:
    try:
        course_id = int(value or 0)
    except (TypeError, ValueError):
        return None
    if not course_id:
        return None
    queries.owned_course(current_user.id, course_id)
    return course_id


def _wants_json() -> bool:
    return request.accept_mimetypes.best == "application/json" or request.headers.get("X-Requested-With") == "fetch"


@bp.route("/")
@login_required
def index():
    rows = db.session.scalars(select(Upload).where(Upload.user_id == current_user.id)
                              .order_by(Upload.created_at.desc())).all()
    return render_template("uploads/index.html", rows=rows, courses=queries.visible_courses(current_user.id),
                           drive_available=integrations.available(),
                           drive_connected=integrations.connected(current_user, "drive"))


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    course_id = _course_id(request.form.get("course_id"))
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        if _wants_json():
            return jsonify({"error": "Choose at least one file."}), 400
        flash("Choose at least one file.", "error")
        return redirect(url_for("uploads.index"))
    saved, errors = [], []
    for f in files[:20]:
        try:
            row = uploads.store(current_user, f.stream, f.filename, f.mimetype, course_id)
            saved.append(sources.describe(current_user, [f"upload:{row.id}"])[0].to_dict())
        except TooLarge:
            errors.append(f"{f.filename}: over the {current_app.config['MAX_FILE_MB']} MB limit")
        except StorageError:
            errors.append(f"{f.filename}: storage is unavailable, try again")
    if _wants_json():
        return jsonify({"sources": saved, "errors": errors}), 200 if saved else 400
    if saved:
        flash(f"Added {len(saved)} file{'s' if len(saved) != 1 else ''}. Its text is being read now.", "success")
    for e in errors:
        flash(e, "error")
    return redirect(request.form.get("next") or url_for("uploads.index"))


@bp.route("/<int:upload_id>/download")
@login_required
def download(upload_id: int):
    row = _upload(upload_id)
    if not row.storage_key:
        abort(404)
    inline = request.args.get("inline") == "1" and (row.content_type or "").startswith(("application/pdf", "image/"))
    storage = get_storage()
    url = storage.signed_url(row.storage_key, row.name, row.content_type, inline, current_app.config["DOWNLOAD_URL_TTL"])
    if url:
        return redirect(url, code=302)
    return send_file(storage.open(row.storage_key), mimetype=row.content_type or "application/octet-stream",
                     as_attachment=not inline, download_name=row.name)


@bp.route("/<int:upload_id>/course", methods=["POST"])
@login_required
def set_course(upload_id: int):
    row = _upload(upload_id)
    row.course_id = _course_id(request.form.get("course_id"))
    retrieval.rebuild_chunks_for(row)
    db.session.commit()
    flash(f"Moved “{row.name}” to {row.course.name if row.course else 'no class'}.", "success")
    return redirect(url_for("uploads.index"))


@bp.route("/<int:upload_id>/delete", methods=["POST"])
@login_required
def delete(upload_id: int):
    row = _upload(upload_id)
    name = row.name
    uploads.delete(row)
    flash(f"Deleted “{name}”.", "info")
    return redirect(url_for("uploads.index"))


# ---------------------------------------------------------------- Google Drive


@bp.route("/drive/search")
@login_required
def drive_search():
    if not integrations.available():
        return jsonify({"error": "Google Drive isn't set up on this server."}), 404
    if not integrations.connected(current_user, "drive"):
        return jsonify({"connected": False, "connect_url": url_for("settings.integration_connect", kind="drive")}), 200
    try:
        result = gdrive.search(current_user, request.args.get("q", ""), request.args.get("page") or None)
    except integrations.NotConnected:
        return jsonify({"connected": False, "connect_url": url_for("settings.integration_connect", kind="drive")}), 200
    except integrations.IntegrationError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"connected": True, **result})


@bp.route("/drive/import", methods=["POST"])
@login_required
def drive_import():
    body = request.get_json(silent=True) or {}
    try:
        course_id = _course_id(body.get("course_id"))
        row = gdrive.import_file(current_user, str(body.get("id") or ""), str(body.get("mimeType") or ""), course_id)
    except integrations.NotConnected:
        return jsonify({"error": "Connect Google Drive first.",
                        "connect_url": url_for("settings.integration_connect", kind="drive")}), 409
    except integrations.IntegrationError as exc:
        return jsonify({"error": str(exc)}), 502
    except TooLarge:
        return jsonify({"error": f"That file is over the {current_app.config['MAX_FILE_MB']} MB limit."}), 413
    return jsonify({"source": sources.describe(current_user, [f"upload:{row.id}"])[0].to_dict()})
