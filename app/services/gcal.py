"""Put the student's upcoming Canvas due dates on their Google Calendar (through Composio).

With the toggle on, every sync (and the "Sync now" button) makes Google match Homework Hatch:
one event per upcoming assignment, ending at the due time, updated when the title or due
date changes and removed when the assignment is deleted, hidden or its due date is dropped.
Past events are left alone. CalendarPush rows remember which event is which, so nothing is
ever added twice.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app, url_for
from sqlalchemy import select

from ..extensions import db
from ..models import Assignment, CalendarPush, Course, User, utcnow
from . import integrations

LOOKAHEAD = timedelta(days=60)
EVENT_MINUTES = 30
_running: set[int] = set()
_lock = threading.Lock()


def enabled(user: User) -> bool:
    row = integrations.get(user, "calendar")
    return bool(row and row.connected and (row.settings or {}).get("enabled"))


def due_for_refresh(user: User, hours: int = 12) -> bool:
    """Even when Canvas hasn't changed, assignments move into the 60-day window over time."""
    row = integrations.get(user, "calendar")
    return row is None or row.last_sync_at is None or utcnow() - row.last_sync_at > timedelta(hours=hours)


def _tz(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _event(a: Assignment, tz: ZoneInfo) -> dict:
    course = a.course
    label = course.course_code or course.name
    done = a.user_done or a.submitted_at is not None or a.workflow_state in ("submitted", "graded", "pending_review")
    summary = f"{'✓ ' if done else ''}Due: {a.name} · {label}"[:250]
    lines = [f"{course.name}"]
    if a.points_possible:
        lines.append(f"{a.points_possible:g} points")
    if a.html_url:
        lines.append(f'<a href="{a.html_url}">Open in Canvas</a>')
    lines.append(f'<a href="{url_for("courses.assignment", assignment_id=a.id, _external=True)}">Open in Homework Hatch</a>')
    due = a.due_at.replace(tzinfo=timezone.utc).astimezone(tz)
    return {"summary": summary, "description": "<br>".join(lines), "end": due,
            "start": due - timedelta(minutes=EVENT_MINUTES)}


def _fingerprint(ev: dict) -> str:
    return hashlib.sha1(f"{ev['summary']}|{ev['end'].isoformat()}|{ev['description']}".encode()).hexdigest()


def _event_id(data: dict) -> str | None:
    body = data.get("response_data") if isinstance(data.get("response_data"), dict) else data
    return body.get("id")


def _wanted(user: User) -> list[Assignment]:
    now = utcnow()
    return db.session.scalars(
        select(Assignment).join(Course).where(Course.user_id == user.id, Course.hidden.is_(False),
                                              Course.active.is_(True), Assignment.due_at.is_not(None),
                                              Assignment.due_at >= now - timedelta(hours=12),
                                              Assignment.due_at <= now + LOOKAHEAD)
        .order_by(Assignment.due_at)).all()


def sync(user: User) -> dict:
    """Bring Google Calendar in line; returns counts. Raises IntegrationError when Google/Composio fails."""
    row = integrations.get(user, "calendar")
    if not enabled(user):
        return {"skipped": True}
    calendar_id = (row.settings or {}).get("calendar_id") or "primary"
    tz = _tz(user)
    now = utcnow()
    wanted = {a.id: a for a in _wanted(user)}
    pushes = db.session.scalars(select(CalendarPush).where(CalendarPush.user_id == user.id)).all()
    by_assignment = {p.assignment_id: p for p in pushes if p.assignment_id is not None}
    stats = {"created": 0, "updated": 0, "removed": 0, "unchanged": 0}

    for p in pushes:
        # Deleted, hidden, undated or pushed past the window while still in the future: remove it.
        gone = p.assignment_id is None or (p.assignment_id not in wanted and (p.due_at is None or p.due_at > now))
        if gone:
            try:
                integrations.execute(user, "calendar", "GOOGLECALENDAR_DELETE_EVENT",
                                     {"event_id": p.event_id, "calendar_id": p.calendar_id, "send_updates": "none"})
            except integrations.NotConnected:
                raise
            except integrations.IntegrationError:
                pass  # already deleted in Google
            db.session.delete(p)
            stats["removed"] += 1
        elif p.assignment_id not in wanted and p.due_at and p.due_at < now - timedelta(days=30):
            db.session.delete(p)  # an old event: keep it in Google, stop tracking it

    for a in wanted.values():
        ev = _event(a, tz)
        fp = _fingerprint(ev)
        p = by_assignment.get(a.id)
        if p is not None and p.fingerprint == fp:
            stats["unchanged"] += 1
            continue
        if p is not None:
            try:
                integrations.execute(user, "calendar", "GOOGLECALENDAR_PATCH_EVENT", {
                    "calendar_id": p.calendar_id, "event_id": p.event_id, "summary": ev["summary"],
                    "description": ev["description"], "start_time": ev["start"].isoformat(),
                    "end_time": ev["end"].isoformat(), "timezone": str(tz), "send_updates": "none"})
                p.fingerprint, p.due_at, p.updated_at = fp, a.due_at, utcnow()
                stats["updated"] += 1
                db.session.commit()
                continue
            except integrations.NotConnected:
                raise
            except integrations.IntegrationError:
                db.session.delete(p)  # the student deleted it in Google: add it back
        data = integrations.execute(user, "calendar", "GOOGLECALENDAR_CREATE_EVENT", {
            "calendar_id": calendar_id, "summary": ev["summary"], "description": ev["description"],
            "start_datetime": ev["start"].replace(tzinfo=None).isoformat(timespec="seconds"),
            "end_datetime": ev["end"].replace(tzinfo=None).isoformat(timespec="seconds"),
            "timezone": str(tz), "create_meeting_room": False, "exclude_organizer": True, "send_updates": "none",
            "transparency": "transparent"})
        event_id = _event_id(data)
        if event_id:
            db.session.add(CalendarPush(user_id=user.id, assignment_id=a.id, event_id=event_id, calendar_id=calendar_id,
                                        fingerprint=fp, due_at=a.due_at))
            stats["created"] += 1
        db.session.commit()

    row.last_sync_at, row.last_error = utcnow(), None
    row.settings = {**(row.settings or {}), "last_stats": stats}
    db.session.commit()
    return stats


def remove_all(user: User) -> int:
    """Delete the upcoming events we created (turning the toggle off)."""
    now = utcnow()
    removed = 0
    for p in db.session.scalars(select(CalendarPush).where(CalendarPush.user_id == user.id)).all():
        if p.due_at is None or p.due_at > now:
            try:
                integrations.execute(user, "calendar", "GOOGLECALENDAR_DELETE_EVENT",
                                     {"event_id": p.event_id, "calendar_id": p.calendar_id, "send_updates": "none"})
                removed += 1
            except integrations.IntegrationError:
                pass
        db.session.delete(p)
    db.session.commit()
    return removed


def kick(user_id: int) -> None:
    """Sync in the background (after a Canvas sync); one run per student at a time."""
    app = current_app._get_current_object()
    if app.config.get("EXTRACT_INLINE"):  # tests: run inline
        _run(user_id)
        return
    with _lock:
        if user_id in _running:
            return
        _running.add(user_id)

    def work():
        # Links in event descriptions need a request context to build absolute URLs.
        with app.app_context(), app.test_request_context(base_url=app.config.get("PUBLIC_URL") or "http://localhost"):
            try:
                _run(user_id)
            finally:
                db.session.remove()
                with _lock:
                    _running.discard(user_id)

    threading.Thread(target=work, name="gcal-sync", daemon=True).start()


def _run(user_id: int) -> None:
    user = db.session.get(User, user_id)
    if user is None or not enabled(user):
        return
    try:
        sync(user)
    except integrations.IntegrationError as exc:
        db.session.rollback()
        row = integrations.get(user, "calendar")
        if row:
            row.last_error = str(exc)[:500]
            db.session.commit()
        current_app.logger.warning("google calendar sync for user %s failed: %s", user_id, exc)
