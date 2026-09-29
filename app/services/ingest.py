"""Turns a snapshot from the browser extension into database rows.

The protocol (see extension/upload.js):
  1. POST /v1/snapshots {snapshot, files}   -> ingest_snapshot(): upsert everything and
     answer with the file ids whose current version we don't hold yet.
  2. PUT /v1/files/<id>?updated_at=...     -> store_file(): one request per needed file.
  3. POST /v1/snapshots/<id>/complete      -> complete_run().

Everything is keyed by (Canvas host, Canvas IDs), so this works for any school.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy import select

from ..extensions import db
from ..models import (Announcement, Assignment, AssignmentGroup, CalendarEvent, CanvasAccount, CanvasFile, Course,
                      Discussion, Module, Page, SyncRun, User, utcnow)
from ..utils import log_activity, parse_ts
from . import coins, retrieval
from .extract import extract_text
from .storage import get_storage


class IngestError(ValueError):
    pass


def version_key(updated_at) -> str:
    """Stable file-version id derived from Canvas's updated_at (same scheme as the extension docs)."""
    if not updated_at:
        return "v0"
    return hashlib.sha1(str(updated_at).encode()).hexdigest()[:16]


def _str(v) -> str | None:
    return None if v is None else str(v)


def _float(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _sync_rows(model, parent: dict, items, key_attr: str, key_fn, apply_fn, delete_missing: bool = True):
    """Upsert children by natural key; optionally delete the ones that disappeared."""
    filters = [getattr(model, k) == v for k, v in parent.items()]
    existing = {getattr(r, key_attr): r for r in db.session.scalars(select(model).where(*filters))}
    seen = set()
    for item in items or []:
        key = key_fn(item)
        if key is None or key in seen:
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = model(**parent, **{key_attr: key})
            db.session.add(row)
        apply_fn(row, item)
    if delete_missing:
        for key, row in existing.items():
            if key not in seen:
                db.session.delete(row)


# ---------------------------------------------------------------- appliers


def _apply_group(row: AssignmentGroup, g: dict):
    row.name = (g.get("name") or "Group")[:300]
    row.weight = _float(g.get("weight"))
    row.position = g.get("position")
    row.drop_lowest = int(g.get("drop_lowest") or 0)
    row.drop_highest = int(g.get("drop_highest") or 0)


def _apply_assignment(row: Assignment, a: dict):
    sub = a.get("submission") or {}
    row.group_canvas_id = _str(a.get("group_id"))
    row.name = (a.get("name") or "Untitled")[:500]
    row.due_at = parse_ts(a.get("due_at"))
    row.unlock_at = parse_ts(a.get("unlock_at"))
    row.lock_at = parse_ts(a.get("lock_at"))
    row.points_possible = _float(a.get("points_possible"))
    row.grading_type = a.get("grading_type")
    row.submission_types = a.get("submission_types") or []
    row.is_quiz = bool(a.get("is_quiz"))
    row.html_url = a.get("html_url")
    row.description_html = a.get("description_html")
    row.status = a.get("status") or "upcoming"
    row.submitted_at = parse_ts(sub.get("submitted_at"))
    row.score = _float(sub.get("score"))
    row.grade = _str(sub.get("grade"))
    row.late = bool(sub.get("late"))
    row.missing = bool(sub.get("missing"))
    row.excused = bool(sub.get("excused"))
    row.workflow_state = sub.get("workflow_state")
    row.rubric = a.get("rubric") or None
    row.comments = sub.get("comments") or None
    row.attachments = sub.get("attachments") or None
    row.rubric_assessment = sub.get("rubric_assessment") or None


def _apply_module(row: Module, m: dict):
    row.name = (m.get("name") or "Module")[:300]
    row.position = m.get("position")
    row.unlock_at = parse_ts(m.get("unlock_at"))
    row.state = m.get("state")
    row.items = m.get("items") or []


def _apply_page(row: Page, p: dict):
    row.title = (p.get("title") or p.get("url") or "Page")[:300]
    # Keep the last known body if this sync couldn't fetch it.
    if p.get("body_html") is not None:
        row.body_html = p.get("body_html")
    row.html_url = p.get("html_url")
    row.canvas_updated_at = parse_ts(p.get("updated_at"))


def _apply_announcement(row: Announcement, a: dict):
    row.title = (a.get("title") or "Announcement")[:500]
    row.message_html = a.get("message_html")
    row.author = a.get("author")
    row.posted_at = parse_ts(a.get("posted_at"))
    row.html_url = a.get("html_url")


def _apply_discussion(row: Discussion, d: dict):
    row.title = (d.get("title") or "Discussion")[:500]
    row.message_html = d.get("message_html")
    row.posted_at = parse_ts(d.get("posted_at"))
    row.due_at = parse_ts(d.get("due_at"))
    row.html_url = d.get("html_url")


# ---------------------------------------------------------------- snapshot


def ingest_snapshot(user: User, snapshot: dict, manifest: list[dict]) -> tuple[SyncRun, list[str]]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise IngestError("unsupported snapshot schema")
    base_url = snapshot.get("base_url") or ""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise IngestError("snapshot.base_url is not a URL")
    canvas_user = snapshot.get("user") or {}
    canvas_user_id = _str(canvas_user.get("id"))
    if not canvas_user_id:
        raise IngestError("snapshot.user.id missing")
    host = parsed.netloc.lower()

    account = db.session.scalar(select(CanvasAccount).where(
        CanvasAccount.user_id == user.id, CanvasAccount.host == host, CanvasAccount.canvas_user_id == canvas_user_id))
    if account is None:
        account = CanvasAccount(user_id=user.id, host=host, canvas_user_id=canvas_user_id, base_url=f"{parsed.scheme}://{host}")
        db.session.add(account)
        db.session.flush()
    account.base_url = f"{parsed.scheme}://{host}"
    account.canvas_name = canvas_user.get("name")
    account.last_sync_at = parse_ts(snapshot.get("synced_at")) or utcnow()
    account.restricted = snapshot.get("restricted") or []

    courses_by_canvas_id: dict[str, Course] = {}
    existing_courses = {c.canvas_id: c for c in db.session.scalars(select(Course).where(Course.account_id == account.id))}
    seen_courses = set()
    for c in snapshot.get("courses") or []:
        cid = _str(c.get("id"))
        if not cid or cid in seen_courses:
            continue
        seen_courses.add(cid)
        course = existing_courses.get(cid)
        if course is None:
            course = Course(user_id=user.id, account_id=account.id, canvas_id=cid,
                            # First time we see it: follow the student's Canvas dashboard.
                            hidden=c.get("on_dashboard") is False)
            db.session.add(course)
        term = c.get("term") or {}
        grade = c.get("grade") or {}
        course.name = (c.get("name") or "Course")[:300]
        course.course_code = (c.get("course_code") or "")[:200] or None
        course.term_id = _str(term.get("id"))
        course.term_name = term.get("name")
        course.class_key = (c.get("class_key") or f"{course.term_id}::{course.name.lower()}")[:400]
        course.room_key = f"{host}:{cid}"
        course.on_dashboard = c.get("on_dashboard")
        course.active = True
        course.current_score = _float(grade.get("current_score"))
        course.current_grade = grade.get("current_grade")
        course.final_score = _float(grade.get("final_score"))
        course.final_grade = grade.get("final_grade")
        course.html_url = c.get("html_url")
        course.syllabus_html = c.get("syllabus_html")
        course.files_tab_hidden = bool(c.get("files_tab_hidden"))
        db.session.flush()
        courses_by_canvas_id[cid] = course

        parent = {"course_id": course.id}
        _sync_rows(AssignmentGroup, parent, c.get("assignment_groups"), "canvas_id", lambda g: _str(g.get("id")), _apply_group)
        _sync_rows(Assignment, parent, c.get("assignments"), "canvas_id", lambda a: _str(a.get("id")), _apply_assignment)
        _sync_rows(Module, parent, c.get("modules"), "canvas_id", lambda m: _str(m.get("id")), _apply_module)
        _sync_rows(Page, parent, c.get("pages"), "slug", lambda p: (p.get("url") or "")[:300] or None, _apply_page)
        _sync_rows(Announcement, parent, c.get("announcements"), "canvas_id", lambda a: _str(a.get("id")), _apply_announcement)
        _sync_rows(Discussion, parent, c.get("discussions"), "canvas_id", lambda d: _str(d.get("id")), _apply_discussion)

    # Courses that dropped out of the student's active enrollments keep their data.
    for cid, course in existing_courses.items():
        if cid not in seen_courses:
            course.active = False
    db.session.flush()

    # File metadata from every course; the manifest says which ones the extension will send.
    manifest_by_id = {str(f.get("id")): f for f in manifest or [] if f.get("id") is not None}
    files = {f.canvas_id: f for f in db.session.scalars(select(CanvasFile).where(CanvasFile.account_id == account.id))}
    for cid, c in ((str(c.get("id")), c) for c in snapshot.get("courses") or []):
        course = courses_by_canvas_id.get(cid)
        for meta in c.get("files") or []:
            fid = _str(meta.get("id"))
            if not fid:
                continue
            row = files.get(fid)
            if row is None:
                row = CanvasFile(user_id=user.id, account_id=account.id, canvas_id=fid,
                                 course_id=course.id if course else None, name="file")
                db.session.add(row)
                files[fid] = row
            row.name = (meta.get("name") or "file")[:500]
            row.content_type = meta.get("content_type")
            row.size = meta.get("size")
            row.canvas_updated_at = _str(meta.get("updated_at"))
            if row.course_id is None and course:
                row.course_id = course.id
    needed: list[str] = []
    for fid, m in manifest_by_id.items():
        row = files.get(fid)
        if row is None:  # in the manifest but not in any course listing: accept its metadata
            course = courses_by_canvas_id.get(_str(m.get("course_id")))
            row = CanvasFile(user_id=user.id, account_id=account.id, canvas_id=fid,
                             course_id=course.id if course else None, name=(m.get("name") or "file")[:500])
            db.session.add(row)
            files[fid] = row
        row.path = (m.get("path") or "")[:800] or None
        row.wanted_version = version_key(m.get("updated_at"))
        if row.stored_version != row.wanted_version:
            needed.append(fid)

    events = snapshot.get("calendar_events") or []
    _sync_rows(CalendarEvent, {"account_id": account.id, "user_id": user.id}, events, "canvas_id",
               lambda e: _str(e.get("id")), lambda row, e: _apply_event(row, e, courses_by_canvas_id))

    db.session.flush()
    host_key = host
    paid = 0
    for course in courses_by_canvas_id.values():
        if course.hidden:
            continue
        for a in course.assignments:
            paid += coins.award_for_assignment(user.id, host_key, a)
        retrieval.rebuild_course_chunks(course)

    run = SyncRun(id=f"{utcnow():%Y%m%dT%H%M%S}-{secrets.token_hex(4)}", user_id=user.id, account_id=account.id,
                  synced_at=parse_ts(snapshot.get("synced_at")), files_needed=len(needed),
                  stats={"courses": len(courses_by_canvas_id), "coins": paid,
                         "errors": len(snapshot.get("errors") or []), "restricted": len(snapshot.get("restricted") or [])})
    db.session.add(run)
    account.last_snapshot_id = run.id
    log_activity(user.id, "sync", f"{len(courses_by_canvas_id)} courses from {host}, {len(needed)} files needed")

    # Keep the latest raw snapshot for debugging and re-processing.
    try:
        get_storage().put_bytes(f"u/{user.id}/snapshots/{account.id}-latest.json",
                                json.dumps({"snapshot": snapshot, "files": manifest}).encode())
    except Exception as exc:  # storage trouble must not lose the sync itself
        current_app.logger.warning("could not store raw snapshot: %s", exc)

    db.session.commit()
    return run, needed


def _apply_event(row: CalendarEvent, e: dict, courses: dict[str, Course]):
    row.title = (e.get("title") or "Event")[:500]
    row.start_at = parse_ts(e.get("start_at"))
    row.end_at = parse_ts(e.get("end_at"))
    row.location = e.get("location")
    row.html_url = e.get("html_url")
    course = courses.get(_str(e.get("course_id")))
    row.course_id = course.id if course else None


# ---------------------------------------------------------------- files


def store_file(user: User, run: SyncRun, canvas_file_id: str, updated_at: str | None, stream,
               content_type: str | None) -> CanvasFile:
    row = db.session.scalar(select(CanvasFile).where(CanvasFile.account_id == run.account_id,
                                                     CanvasFile.canvas_id == canvas_file_id))
    if row is None or row.user_id != user.id or row.wanted_version is None:
        raise IngestError("file is not part of this sync")
    version = version_key(updated_at)
    cfg = current_app.config
    key = f"u/{user.id}/files/{row.account_id}/{row.canvas_id}/{version}"
    size, digest = get_storage().put_stream(key, stream, cfg["MAX_FILE_MB"] * 1024 * 1024)
    row.storage_key = key
    row.stored_version = version
    row.sha256 = digest
    row.size = size
    row.stored_at = utcnow()
    if content_type and content_type != "application/octet-stream":
        row.content_type = content_type
    if size <= cfg["MAX_EXTRACT_MB"] * 1024 * 1024:
        text, status = extract_text(get_storage().read(key), row.name, row.content_type)
    else:
        text, status = None, "too_large"
    row.text, row.text_status = text, status
    retrieval.rebuild_file_chunks(row)
    run.files_uploaded = (run.files_uploaded or 0) + 1
    db.session.commit()
    return row


def complete_run(run: SyncRun, result: dict) -> None:
    run.completed_at = utcnow()
    run.failed = (result.get("failed") or [])[:500]
    db.session.commit()
