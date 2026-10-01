from __future__ import annotations

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.orm import undefer_group

from .. import queries
from ..extensions import db
from ..models import Assignment, CanvasFile, Page, Summary, Upload
from ..services import ai, grades, study
from ..services.storage import get_storage

bp = Blueprint("courses", __name__, url_prefix="/courses")


@bp.route("/")
@login_required
def index():
    courses = queries.visible_courses(current_user.id, include_hidden=True)
    return render_template("courses/index.html", courses=courses)


@bp.route("/<int:course_id>")
@login_required
def detail(course_id: int):
    course = queries.owned_course(current_user.id, course_id)
    tab = request.args.get("tab", "overview")
    result = grades.compute(course.groups, course.assignments, weighted=course.group_weighting)
    base_url = course.account.base_url if course.account else None
    uploads = db.session.scalars(select(Upload).where(Upload.user_id == current_user.id, Upload.course_id == course.id)
                                 .order_by(Upload.name)).all() if tab == "files" else []
    return render_template("courses/detail.html", course=course, tab=tab, grade=result, base_url=base_url,
                           upcoming=queries.upcoming_for_course(course), uploads=uploads)


@bp.route("/<int:course_id>/visibility", methods=["POST"])
@login_required
def toggle_hidden(course_id: int):
    course = queries.owned_course(current_user.id, course_id)
    course.hidden = not course.hidden
    db.session.commit()
    flash(f"{course.name} is now {'hidden' if course.hidden else 'shown'}.", "info")
    return redirect(request.referrer or url_for("courses.index"))


@bp.route("/<int:course_id>/what-if", methods=["POST"])
@login_required
def what_if(course_id: int):
    course = queries.owned_course(current_user.id, course_id)
    payload = request.get_json(silent=True) or {}
    overrides = {}
    valid_ids = {a.id for a in course.assignments}
    for key, value in (payload.get("scores") or {}).items():
        try:
            aid = int(key)
            if aid in valid_ids and value not in (None, ""):
                overrides[aid] = float(value)
        except (TypeError, ValueError):
            continue
    result = grades.compute(course.groups, course.assignments, overrides, weighted=course.group_weighting)
    return jsonify({
        "percent": result["percent"],
        "groups": [{"name": g.name, "weight": g.weight, "percent": None if g.percent is None else round(g.percent, 2),
                    "dropped": g.dropped} for g in result["groups"]],
    })


@bp.route("/<int:course_id>/needed", methods=["POST"])
@login_required
def needed(course_id: int):
    course = queries.owned_course(current_user.id, course_id)
    payload = request.get_json(silent=True) or {}
    try:
        aid = int(payload.get("assignment_id"))
        goal = float(payload.get("goal"))
    except (TypeError, ValueError):
        return jsonify({"error": "Pick an assignment and a goal."}), 400
    score = grades.needed_on(course.groups, course.assignments, aid, goal, weighted=course.group_weighting)
    return jsonify({"needed": score})


@bp.route("/pages/<int:page_id>")
@login_required
def page(page_id: int):
    p = db.session.get(Page, page_id)
    if p is None:
        abort(404)
    course = queries.owned_course(current_user.id, p.course_id)
    summary = db.session.scalar(select(Summary).where(Summary.user_id == current_user.id,
                                                      Summary.source_type == "page", Summary.source_id == p.id))
    return render_template("courses/page.html", page=p, course=course, summary=summary,
                           base_url=course.account.base_url, render_markdown=study.render_markdown)


@bp.route("/assignments/<int:assignment_id>")
@login_required
def assignment(assignment_id: int):
    a = db.session.get(Assignment, assignment_id, options=[undefer_group("assignment_detail")])
    if a is None or a.course.user_id != current_user.id:
        abort(404)
    return render_template("courses/assignment.html", a=a, course=a.course, base_url=a.course.account.base_url)


@bp.route("/files/<int:file_id>")
@login_required
def file_detail(file_id: int):
    f = db.session.get(CanvasFile, file_id)
    if f is None or f.user_id != current_user.id:
        abort(404)
    summary = db.session.scalar(select(Summary).where(Summary.user_id == current_user.id,
                                                      Summary.source_type == "file", Summary.source_id == f.id))
    course = queries.owned_course(current_user.id, f.course_id) if f.course_id else None
    return render_template("courses/file.html", f=f, course=course, summary=summary,
                           render_markdown=study.render_markdown)


@bp.route("/files/<int:file_id>/download")
@login_required
def download(file_id: int):
    f = db.session.get(CanvasFile, file_id)
    if f is None or f.user_id != current_user.id or not f.storage_key:
        abort(404)
    inline = request.args.get("inline") == "1" and (f.content_type or "").startswith(("application/pdf", "image/"))
    storage = get_storage()
    # Object storage: redirect to a short-lived signed URL so the bytes don't pass through us.
    url = storage.signed_url(f.storage_key, f.name, f.content_type, inline, current_app.config["DOWNLOAD_URL_TTL"])
    if url:
        return redirect(url, code=302)
    return send_file(storage.open(f.storage_key), mimetype=f.content_type or "application/octet-stream",
                     as_attachment=not inline, download_name=f.name)


@bp.route("/files/<int:file_id>/transcribe", methods=["POST"])
@login_required
def transcribe(file_id: int):
    """Scanned PDFs have no text layer; let Claude read them so the tutor and generators can too."""
    f = db.session.get(CanvasFile, file_id)
    if f is None or f.user_id != current_user.id or not f.storage_key:
        abort(404)
    back = url_for("courses.file_detail", file_id=f.id)
    if not f.name.lower().endswith(".pdf") and f.content_type != "application/pdf":
        flash("Only PDFs can be read this way.", "error")
        return redirect(back)
    try:
        text = study.transcribe_pdf(current_user, get_storage().read(f.storage_key), f.name)
    except (study.MaterialError, ai.AIError) as exc:
        flash(str(exc), "error")
        return redirect(back)
    from ..services import retrieval

    f.text, f.text_status = text[:400_000], "ai"
    retrieval.rebuild_file_chunks(f)
    db.session.commit()
    flash("Done. The tutor, summaries and study-set generator can now read this file.", "success")
    return redirect(back)


@bp.route("/summarize/<kind>/<int:ident>", methods=["POST"])
@login_required
def summarize(kind: str, ident: int):
    if kind not in {"file", "page"}:
        abort(404)
    back = url_for("courses.file_detail", file_id=ident) if kind == "file" else url_for("courses.page", page_id=ident)
    existing = db.session.scalar(select(Summary).where(Summary.user_id == current_user.id,
                                                       Summary.source_type == kind, Summary.source_id == ident))
    try:
        material = study.gather_material(current_user, kind, ident)
        text = study.summarize(current_user, material)
    except (study.MaterialError, ai.AIError) as exc:
        flash(str(exc), "error")
        return redirect(back)
    if existing:
        existing.content = text
    else:
        db.session.add(Summary(user_id=current_user.id, source_type=kind, source_id=ident, content=text))
    db.session.commit()
    if material.truncated:
        flash("This source is very long, so the summary covers the first part of it.", "info")
    return redirect(back)
