"""Turns a snapshot from the browser extension into database rows.

The protocol (see extension/upload.js):
  1. POST /v1/snapshots {snapshot, files}   -> ingest_snapshot(): upsert everything and
     answer with the file ids whose current version we don't hold yet, plus presigned
     storage URLs for them (upload_targets) when storage supports direct uploads.
  2. Per needed file, either
     PUT <presigned url>, then POST /v1/files/<id>/uploaded?updated_at=... -> confirm_upload()
     or PUT /v1/files/<id>?updated_at=... with the bytes                    -> store_file().
  3. POST /v1/snapshots/<id>/complete      -> complete_run().
Text is read from stored files afterwards, in the background (services/textjobs.py).

Everything is keyed by (Canvas host, Canvas IDs), so this works for any school.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy import exists, func, or_, select

from ..extensions import db
from ..models import (Announcement, Assignment, AssignmentGroup, CalendarEvent, CanvasAccount, CanvasFile, Course,
                      Discussion, Module, Page, SyncRun, User, utcnow)
from ..utils import log_activity, parse_ts
from . import coins, retrieval, textjobs
from .storage import TooLarge, get_storage, safe_key_part


class IngestError(ValueError):
    status = 400


class IngestConflict(IngestError):
    status = 409


def _clip(value, length: int):
    """Canvas strings have no length limit; our columns do (Postgres enforces them)."""
    if value is None:
        return None
    value = str(value)
    return value[:length] if len(value) > length else value


def version_key(updated_at) -> str:
    """Stable file-version id derived from Canvas's updated_at (same scheme as the extension docs)."""
    if not updated_at:
        return "v0"
    return hashlib.sha1(str(updated_at).encode()).hexdigest()[:16]


def snapshot_digest(snapshot: dict, manifest: list) -> str:
    """What the sync says, minus when it was taken. Equal digests mean nothing changed in Canvas
    (assignment statuses are part of the snapshot, so a due time passing counts as a change)."""
    body = {k: v for k, v in snapshot.items() if k != "synced_at"}
    raw = json.dumps({"snapshot": body, "files": manifest}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def content_fingerprint(name, content_type, size) -> str | None:
    """A file's identity before we download it: name, type and size. Name and type alone
    aren't enough; courses reuse names like "solution.py" for different files."""
    if not name or not isinstance(size, int):
        return None
    ident = f"{str(name).strip().lower()}\0{(content_type or '').strip().lower()}\0{size}"
    return hashlib.sha1(ident.encode()).hexdigest()


def share_stored(row: CanvasFile, twin: CanvasFile) -> None:
    """Mark `row` as held using `twin`'s stored object (twin may be row itself)."""
    if twin is not row:
        row.storage_key, row.size, row.sha256 = twin.storage_key, twin.size, twin.sha256
        row.text, row.text_status, row.text_started_at = twin.text, twin.text_status, None
        row.stored_at = utcnow()
    row.stored_version = row.wanted_version
    row.stored_fingerprint = row.wanted_fingerprint


def forget_course_files(course: Course) -> int:
    """Delete the stored copies of a class's files (the student stopped keeping them). Objects a kept
    row in another class still points at stay; so do the file names, so turning the class back on
    re-requests them. Storage goes first: if it fails, nothing changes and the caller can say so."""
    rows = db.session.scalars(select(CanvasFile).where(CanvasFile.course_id == course.id,
                                                       CanvasFile.storage_key.is_not(None))).all()
    keys = {r.storage_key for r in rows}
    used_elsewhere = set(db.session.scalars(select(CanvasFile.storage_key).where(
        CanvasFile.storage_key.in_(keys), CanvasFile.course_id != course.id))) if keys else set()
    for key in keys - used_elsewhere:
        get_storage().delete_prefix(key)
    for r in rows:
        r.storage_key = r.sha256 = r.stored_version = r.stored_fingerprint = r.stored_at = None
        r.text, r.text_status, r.text_started_at = None, None, None
        retrieval.rebuild_file_chunks(r)
    return len(rows)


def _str(v) -> str | None:
    return None if v is None else str(v)


def _float(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _sync_rows(model, parent: dict, items, key_attr: str, key_fn, apply_fn, delete_missing: bool = True):
    """Upsert children by natural key; optionally delete the ones that disappeared.

    `items` of None means "unknown" (the extension couldn't fetch the list): keep everything.
    """
    if items is None:
        return
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
    row.never_drop = [str(x) for x in g.get("never_drop") or []] or None


def _apply_assignment(row: Assignment, a: dict):
    sub = a.get("submission") or {}
    row.group_canvas_id = _str(a.get("group_id"))
    row.name = (a.get("name") or "Untitled")[:500]
    row.due_at = parse_ts(a.get("due_at"))
    row.unlock_at = parse_ts(a.get("unlock_at"))
    row.lock_at = parse_ts(a.get("lock_at"))
    row.points_possible = _float(a.get("points_possible"))
    row.grading_type = _clip(a.get("grading_type"), 40)
    row.omit_from_final_grade = bool(a.get("omit_from_final_grade"))
    row.submission_types = a.get("submission_types") or []
    row.is_quiz = bool(a.get("is_quiz"))
    row.html_url = _clip(a.get("html_url"), 500)
    row.description_html = a.get("description_html")
    row.status = _clip(a.get("status") or "upcoming", 30)
    row.submitted_at = parse_ts(sub.get("submitted_at"))
    row.score = _float(sub.get("score"))
    row.grade = _clip(sub.get("grade"), 40)
    row.late = bool(sub.get("late"))
    row.missing = bool(sub.get("missing"))
    row.excused = bool(sub.get("excused"))
    row.workflow_state = _clip(sub.get("workflow_state"), 40)
    row.rubric = a.get("rubric") or None
    row.comments = sub.get("comments") or None
    # Only what the page shows: Canvas's signed download links work like passwords, so they're not kept.
    row.attachments = [{k: f.get(k) for k in ("id", "name", "content_type", "size")}
                       for f in sub.get("attachments") or [] if isinstance(f, dict)] or None
    row.rubric_assessment = sub.get("rubric_assessment") or None


def _apply_module(row: Module, m: dict):
    row.name = (m.get("name") or "Module")[:300]
    row.position = m.get("position")
    row.unlock_at = parse_ts(m.get("unlock_at"))
    row.state = _clip(m.get("state"), 40)
    row.items = m.get("items") or []


def _apply_page(row: Page, p: dict):
    row.title = (p.get("title") or p.get("url") or "Page")[:300]
    # Keep the last known body if this sync couldn't fetch it.
    if p.get("body_html") is not None:
        row.body_html = p.get("body_html")
    row.html_url = _clip(p.get("html_url"), 500)
    row.canvas_updated_at = parse_ts(p.get("updated_at"))


def _apply_announcement(row: Announcement, a: dict):
    row.title = (a.get("title") or "Announcement")[:500]
    row.message_html = a.get("message_html")
    row.author = _clip(a.get("author"), 200)
    row.posted_at = parse_ts(a.get("posted_at"))
    row.html_url = _clip(a.get("html_url"), 500)


def _apply_discussion(row: Discussion, d: dict):
    row.title = (d.get("title") or "Discussion")[:500]
    row.message_html = None  # the text is often a classmate's post, and nothing here uses it
    row.posted_at = parse_ts(d.get("posted_at"))
    row.due_at = parse_ts(d.get("due_at"))
    row.html_url = _clip(d.get("html_url"), 500)


# ---------------------------------------------------------------- snapshot


def scrub_snapshot(snapshot: dict) -> dict:
    """A copy without what we don't keep, whatever the extension version sent: class rosters
    (older extensions), Canvas's signed file links, and discussion posts' text."""
    clean = json.loads(json.dumps(snapshot, default=str))
    for c in clean.get("courses") or []:
        if not isinstance(c, dict):
            continue
        c.pop("roster_ids", None)
        for f in c.get("files") or []:
            if isinstance(f, dict):
                f.pop("download_url", None)
        for a in c.get("assignments") or []:
            sub = a.get("submission") if isinstance(a, dict) else None
            for f in (sub or {}).get("attachments") or []:
                if isinstance(f, dict):
                    f.pop("download_url", None)
                    f.pop("url", None)
        for d in c.get("discussions") or []:
            if isinstance(d, dict):
                d.pop("message_html", None)
    return clean


MEDIA_EXTENSIONS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac")


def is_media(name: str | None, content_type: str | None) -> bool:
    """Audio and video (often lecture recordings, which UVA policy protects more strictly) aren't copied."""
    return (content_type or "").startswith(("audio/", "video/")) or (name or "").lower().endswith(MEDIA_EXTENSIONS)


def _kept_courses(account_id: int):
    return select(Course.id).where(Course.account_id == account_id, Course.sync_files.is_(True))


def ingest_snapshot(user: User, snapshot: dict, manifest: list[dict]) -> tuple[SyncRun, list[str]]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise IngestError("unsupported snapshot schema")
    snapshot = scrub_snapshot(snapshot)
    base_url = snapshot.get("base_url") or ""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise IngestError("snapshot.base_url is not a URL")
    canvas_user = snapshot.get("user") or {}
    canvas_user_id = _str(canvas_user.get("id"))
    if not canvas_user_id:
        raise IngestError("snapshot.user.id missing")
    host = parsed.netloc.lower()
    canvas_user_id = _clip(canvas_user_id, 64)
    if len(host) > 200:
        raise IngestError("snapshot.base_url host is too long")
    claimed = db.session.scalar(select(CanvasAccount.user_id).where(
        CanvasAccount.host == host, CanvasAccount.canvas_user_id == canvas_user_id, CanvasAccount.user_id != user.id))
    if claimed:
        # One Canvas identity per Homework Hatch account: stops one Canvas login from feeding
        # several Homework Hatch accounts.
        raise IngestConflict("This Canvas account is already linked to a different Homework Hatch account.")

    account = db.session.scalar(select(CanvasAccount).where(
        CanvasAccount.user_id == user.id, CanvasAccount.host == host, CanvasAccount.canvas_user_id == canvas_user_id))
    if account is None:
        account = CanvasAccount(user_id=user.id, host=host, canvas_user_id=canvas_user_id, base_url=f"{parsed.scheme}://{host}")
        db.session.add(account)
        db.session.flush()
    account.base_url = f"{parsed.scheme}://{host}"
    account.canvas_name = _clip(canvas_user.get("name"), 200)
    account.last_sync_at = parse_ts(snapshot.get("synced_at")) or utcnow()
    account.restricted = snapshot.get("restricted") or []
    digest = snapshot_digest(snapshot, manifest or [])
    if digest == account.last_snapshot_hash and account.last_snapshot_id:
        return _unchanged_run(user, account, snapshot, manifest or [])

    # Endpoints the extension reported as failed: e.g. {"endpoint": "assignments:123"}.
    failed = set()
    for err in snapshot.get("errors") or []:
        kind, _, course_ref = str((err or {}).get("endpoint", "")).partition(":")
        if course_ref:
            failed.add((kind, course_ref))
    failed_announcements = any(str((e or {}).get("endpoint", "")) == "announcements" for e in snapshot.get("errors") or [])
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
                            hidden=c.get("on_dashboard") is False,
                            # Files only if the student chose "all my classes"; otherwise they decide.
                            sync_files=True if user.keep_all_files else None)
            db.session.add(course)
        term = c.get("term") or {}
        grade = c.get("grade") or {}
        course.name = (c.get("name") or "Course")[:300]
        course.course_code = (c.get("course_code") or "")[:200] or None
        course.term_id = _clip(term.get("id"), 64)
        course.term_name = _clip(term.get("name"), 200)
        course.class_key = (c.get("class_key") or f"{course.term_id}::{course.name.lower()}")[:400]
        course.room_key = f"{host}:{cid}"
        course.on_dashboard = c.get("on_dashboard")
        course.active = True
        course.current_score = _float(grade.get("current_score"))
        course.current_grade = _clip(grade.get("current_grade"), 20)
        course.final_score = _float(grade.get("final_score"))
        course.final_grade = _clip(grade.get("final_grade"), 20)
        course.html_url = _clip(c.get("html_url"), 500)
        course.syllabus_html = c.get("syllabus_html")
        course.files_tab_hidden = bool(c.get("files_tab_hidden"))
        if "group_weighting" in c:
            course.group_weighting = c["group_weighting"] if isinstance(c["group_weighting"], bool) else None
        db.session.flush()
        courses_by_canvas_id[cid] = course

        parent = {"course_id": course.id}

        def items(kind):
            # Old extensions send [] when a fetch failed; the snapshot's errors say which.
            return None if (kind, cid) in failed else c.get(kind)

        _sync_rows(AssignmentGroup, parent, items("assignment_groups"), "canvas_id", lambda g: _str(g.get("id")), _apply_group)
        _sync_rows(Assignment, parent, items("assignments"), "canvas_id", lambda a: _str(a.get("id")), _apply_assignment)
        _sync_rows(Module, parent, items("modules"), "canvas_id", lambda m: _str(m.get("id")), _apply_module)
        _sync_rows(Page, parent, items("pages"), "slug", lambda p: (p.get("url") or "")[:300] or None, _apply_page,
                   delete_missing=not c.get("pages_partial") and ("pages", cid) not in failed)
        _sync_rows(Announcement, parent, None if failed_announcements else c.get("announcements"), "canvas_id",
                   lambda a: _str(a.get("id")), _apply_announcement)
        _sync_rows(Discussion, parent, items("discussions"), "canvas_id", lambda d: _str(d.get("id")), _apply_discussion)

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
            row.content_type = _clip(meta.get("content_type"), 200)
            row.size = meta.get("size") if isinstance(meta.get("size"), int) else None
            row.canvas_updated_at = _clip(meta.get("updated_at"), 64)
            if row.course_id is None and course:
                row.course_id = course.id
    needed: list[str] = []
    max_bytes = current_app.config["MAX_FILE_MB"] * 1024 * 1024
    announced = []
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
        row.wanted_fingerprint = content_fingerprint(m.get("name") or row.name, m.get("content_type") or row.content_type,
                                                     m.get("size"))
        if row.storage_key and row.stored_fingerprint is None and row.stored_version == row.wanted_version:
            row.stored_fingerprint = row.wanted_fingerprint  # stored before fingerprints existed
        announced.append((fid, row, isinstance(m.get("size"), int) and m["size"] > max_bytes))

    # Only files we don't already hold get downloaded. "Hold" means an object with the same name,
    # type and size, whatever its Canvas ID or updated_at: Canvas bumps updated_at for settings
    # changes, and instructors post the same file in several places.
    held = {r.stored_fingerprint: r for r in files.values() if r.storage_key and r.stored_fingerprint}
    requested: set[str] = set()
    shared: list[CanvasFile] = []
    already_held = duplicates = 0
    keeping = {c.id for c in courses_by_canvas_id.values() if c.sync_files}
    for fid, row, too_big in announced:
        if row.course_id not in keeping or is_media(row.name, row.content_type):
            continue  # not a class the student chose to keep, or a recording
        if row.stored_version == row.wanted_version and row.stored_fingerprint == row.wanted_fingerprint:
            already_held += 1
            continue
        fp = row.wanted_fingerprint
        twin = held.get(fp) if fp else None
        if twin is not None:
            share_stored(row, twin)
            if twin is row:
                already_held += 1
            else:
                duplicates += 1
                shared.append(row)
            continue
        if too_big:
            # Over the storage limit: don't ask for it (it would be rejected every hour forever).
            row.stored_version, row.stored_fingerprint = row.wanted_version, row.wanted_fingerprint
            row.text_status = "too_large"
            continue
        if fp in requested:  # the same file twice in this sync: upload one copy, the other links to it
            duplicates += 1
            continue
        if fp:
            requested.add(fp)
        needed.append(fid)

    events = snapshot.get("calendar_events") or []
    _sync_rows(CalendarEvent, {"account_id": account.id, "user_id": user.id}, events, "canvas_id",
               lambda e: _str(e.get("id")), lambda row, e: _apply_event(row, e, courses_by_canvas_id))

    db.session.flush()
    for row in shared:
        retrieval.rebuild_file_chunks(row)
    paid = 0
    already = coins.paid_refs(user.id, f"%:{host}:%")
    for course in courses_by_canvas_id.values():
        if course.hidden:
            continue
        for a in course.assignments:
            paid += coins.award_for_assignment(user.id, host, a, already)
        retrieval.rebuild_course_chunks(course)

    run = SyncRun(id=f"{utcnow():%Y%m%dT%H%M%S}-{secrets.token_hex(4)}", user_id=user.id, account_id=account.id,
                  synced_at=parse_ts(snapshot.get("synced_at")), files_needed=len(needed),
                  stats={"courses": len(courses_by_canvas_id), "coins": paid, "files_already": already_held,
                         "files_duplicate": duplicates,
                         "errors": len(snapshot.get("errors") or []), "restricted": len(snapshot.get("restricted") or [])})
    db.session.add(run)
    account.last_snapshot_id = run.id
    account.last_snapshot_hash = digest
    log_activity(user.id, "sync", f"{len(courses_by_canvas_id)} courses from {host}, {len(needed)} files needed")

    db.session.commit()
    return run, needed


def _unchanged_run(user: User, account: CanvasAccount, snapshot: dict, manifest: list) -> tuple[SyncRun, list[str]]:
    """Canvas hasn't changed since the last sync: record the sync and say which files are still
    missing (a failed upload is retried), without touching the classes. Same no-duplicates rules
    as a full sync: a copy of a stored file is linked to it, and one file is requested once."""
    announced = {str(m.get("id")) for m in manifest if m.get("id") is not None}
    keeping = set(db.session.scalars(select(Course.id).where(Course.account_id == account.id,
                                                             Course.sync_files.is_(True))))
    rows = [r for r in db.session.scalars(select(CanvasFile).where(CanvasFile.account_id == account.id)
                                          .order_by(CanvasFile.id))
            if r.canvas_id in announced and r.course_id in keeping and not is_media(r.name, r.content_type)]
    held = {r.stored_fingerprint: r for r in rows if r.storage_key and r.stored_fingerprint}
    needed, requested = [], set()
    for row in rows:
        if row.wanted_version is None or (row.stored_version == row.wanted_version
                                          and row.stored_fingerprint == row.wanted_fingerprint):
            continue
        fp = row.wanted_fingerprint
        if fp and fp in held:
            share_stored(row, held[fp])
            continue
        if fp in requested:
            continue
        if fp:
            requested.add(fp)
        needed.append(row.canvas_id)
    run = SyncRun(id=f"{utcnow():%Y%m%dT%H%M%S}-{secrets.token_hex(4)}", user_id=user.id, account_id=account.id,
                  synced_at=parse_ts(snapshot.get("synced_at")), files_needed=len(needed),
                  stats={"courses": len(snapshot.get("courses") or []), "coins": 0, "unchanged": True,
                         "errors": len(snapshot.get("errors") or []), "restricted": len(snapshot.get("restricted") or [])})
    db.session.add(run)
    account.last_snapshot_id = run.id
    db.session.commit()
    return run, needed


def _apply_event(row: CalendarEvent, e: dict, courses: dict[str, Course]):
    row.title = (e.get("title") or "Event")[:500]
    row.start_at = parse_ts(e.get("start_at"))
    row.end_at = parse_ts(e.get("end_at"))
    row.location = _clip(e.get("location"), 300)
    row.html_url = _clip(e.get("html_url"), 500)
    course = courses.get(_str(e.get("course_id")))
    row.course_id = course.id if course else None


# ---------------------------------------------------------------- files


def _spool(stream, limit: int):
    """Copy the request body to a temp file, enforcing the size limit and hashing as we go."""
    spool = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)  # bigger uploads wait on disk, not in RAM
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(1024 * 256)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            spool.close()
            raise TooLarge(f"file exceeds the {limit // (1024 * 1024)} MB limit")
        digest.update(chunk)
        spool.write(chunk)
    spool.seek(0)
    return spool, size, digest.hexdigest()


def file_key(row: CanvasFile, version: str) -> str:
    return f"u/{row.user_id}/files/{row.account_id}/{safe_key_part(row.canvas_id)}/{version}/{safe_key_part(row.name)}"


def _upload_type(row: CanvasFile) -> str:
    return row.content_type or "application/octet-stream"


def upload_targets(run: SyncRun, needed: list[str]) -> dict[str, dict]:
    """Presigned PUT URLs for the needed files, or {} when storage can't take direct uploads."""
    if not needed:
        return {}
    storage = get_storage()
    ttl = current_app.config["UPLOAD_URL_TTL"]
    rows = db.session.scalars(select(CanvasFile).where(CanvasFile.account_id == run.account_id,
                                                       CanvasFile.canvas_id.in_(needed)))
    targets = {}
    for row in rows:
        url = storage.presign_put(file_key(row, row.wanted_version), _upload_type(row), ttl)
        if url is None:
            return {}
        targets[row.canvas_id] = {"url": url, "headers": {"Content-Type": _upload_type(row)}}
    return targets


class NotKept(IngestError):
    def __init__(self, row: CanvasFile):
        super().__init__("this class's files aren't being kept")
        self.row = row


def _file_row(user: User, run: SyncRun, canvas_file_id: str) -> CanvasFile:
    row = db.session.scalar(select(CanvasFile).where(CanvasFile.account_id == run.account_id,
                                                     CanvasFile.canvas_id == canvas_file_id))
    if row is None or row.user_id != user.id or row.wanted_version is None:
        raise IngestError("file is not part of this sync")
    if row.course_id not in set(db.session.scalars(_kept_courses(row.account_id))) or is_media(row.name, row.content_type):
        raise NotKept(row)  # e.g. the student unticked the class while an upload was on its way
    return row


def _record_stored(row: CanvasFile, run: SyncRun, key: str, version: str, size: int, digest: str | None) -> None:
    old_key = row.storage_key
    row.storage_key = key
    row.stored_version = version
    row.stored_fingerprint = row.wanted_fingerprint if version == row.wanted_version else None
    row.size = size
    row.sha256 = digest  # filled in by the text reader for direct uploads
    row.stored_at = utcnow()
    # The previous version's text stays searchable until the new text is read.
    row.text_status, row.text_started_at = "pending", None
    # Incremented in SQL: parallel uploads each loaded the same count, so += lost updates.
    run.files_uploaded = func.coalesce(SyncRun.files_uploaded, 0) + 1
    # Copies of this file announced in the same sync were not requested; they share this upload.
    if row.stored_fingerprint:
        waiting = db.session.scalars(select(CanvasFile).where(
            CanvasFile.account_id == row.account_id, CanvasFile.id != row.id,
            CanvasFile.course_id.in_(_kept_courses(row.account_id)),
            CanvasFile.wanted_fingerprint == row.stored_fingerprint,
            or_(CanvasFile.stored_version.is_(None), CanvasFile.stored_version != CanvasFile.wanted_version)))
        for twin in waiting:
            share_stored(twin, row)
    db.session.commit()
    if old_key and old_key != key:  # an older version of this file is no longer needed...
        in_use = db.session.scalar(select(exists().where(CanvasFile.storage_key == old_key)))
        if not in_use:  # ...unless a copy elsewhere still points at it
            try:
                get_storage().delete_prefix(old_key)
            except Exception as exc:
                current_app.logger.warning("could not delete old file version %s: %s", old_key, exc)
    textjobs.kick()


def store_file(user: User, run: SyncRun, canvas_file_id: str, updated_at: str | None, stream,
               content_type: str | None) -> CanvasFile:
    """The file's bytes came through the app (local storage, or an extension without direct uploads)."""
    row = _file_row(user, run, canvas_file_id)
    version = version_key(updated_at)
    if content_type and content_type != "application/octet-stream":
        row.content_type = content_type
    spool, size, digest = _spool(stream, current_app.config["MAX_FILE_MB"] * 1024 * 1024)
    key = file_key(row, version)
    try:
        get_storage().put_file(key, spool, row.content_type)
    finally:
        spool.close()
    _record_stored(row, run, key, version, size, digest)
    return row


def confirm_upload(user: User, run: SyncRun, canvas_file_id: str, updated_at: str | None) -> CanvasFile:
    """The extension PUT the file straight to storage with a URL from upload_targets()."""
    version = version_key(updated_at)
    try:
        row = _file_row(user, run, canvas_file_id)
    except NotKept as exc:
        get_storage().delete_prefix(file_key(exc.row, version))  # the bytes already landed; don't keep them
        raise
    key = file_key(row, version)
    storage = get_storage()
    size = storage.size(key)
    if size is None:
        raise IngestError("the file never reached storage")
    limit = current_app.config["MAX_FILE_MB"] * 1024 * 1024
    if size > limit:
        storage.delete_prefix(key)
        raise TooLarge(f"file exceeds the {limit // (1024 * 1024)} MB limit")
    _record_stored(row, run, key, version, size, None)
    return row


def mark_too_large(run: SyncRun, canvas_file_id: str, updated_at: str | None) -> None:
    """Remember that this version was rejected for size, so it isn't requested every sync."""
    row = db.session.scalar(select(CanvasFile).where(CanvasFile.account_id == run.account_id,
                                                     CanvasFile.canvas_id == canvas_file_id))
    if row is not None and row.user_id == run.user_id:
        row.stored_version = version_key(updated_at)
        row.stored_fingerprint = row.wanted_fingerprint
        row.text_status = "too_large"
        db.session.commit()


def complete_run(run: SyncRun, result: dict) -> None:
    run.completed_at = utcnow()
    run.failed = (result.get("failed") or [])[:500]
    db.session.commit()
