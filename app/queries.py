"""Common read queries shared by several pages."""

from __future__ import annotations

from datetime import timedelta

from flask import abort
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .extensions import db
from .models import Announcement, Assignment, CanvasAccount, Course, Deck, utcnow

UPCOMING_STATUSES = {"upcoming", "past_due", "missing"}


def visible_courses(user_id: int, include_hidden: bool = False) -> list[Course]:
    stmt = select(Course).where(Course.user_id == user_id, Course.active.is_(True))
    if not include_hidden:
        stmt = stmt.where(Course.hidden.is_(False))
    return list(db.session.scalars(stmt.order_by(Course.name)))


def owned_course(user_id: int, course_id: int) -> Course:
    course = db.session.get(Course, course_id)
    if course is None or course.user_id != user_id:
        abort(404)
    return course


def upcoming(user_id: int, days: int = 14, back_days: int = 7) -> list[Assignment]:
    """Work still to do, due between `back_days` ago and `days` ahead (the popup's logic)."""
    now = utcnow()
    course_ids = [c.id for c in visible_courses(user_id)]
    if not course_ids:
        return []
    rows = db.session.scalars(
        select(Assignment).options(selectinload(Assignment.course))
        .where(Assignment.course_id.in_(course_ids), Assignment.due_at.is_not(None),
               Assignment.due_at <= now + timedelta(days=days), Assignment.due_at >= now - timedelta(days=back_days))
        .order_by(Assignment.due_at)).all()
    return [a for a in rows if still_to_do(a, now)]


def still_to_do(a: Assignment, now) -> bool:
    status = a.effective_status
    if status == "no_submission":
        # In-class items (exams, checkpoints) show while upcoming and worth points.
        return a.due_at >= now and (a.points_possible or 0) > 0
    return status in UPCOMING_STATUSES


def upcoming_for_course(course: Course, days: int = 14, back_days: int = 7) -> list[Assignment]:
    """Same window as upcoming(), for one course, whether or not it's hidden."""
    now = utcnow()
    return [a for a in course.assignments
            if a.due_at and now - timedelta(days=back_days) <= a.due_at <= now + timedelta(days=days)
            and still_to_do(a, now)]


def missing(user_id: int) -> list[Assignment]:
    course_ids = [c.id for c in visible_courses(user_id)]
    if not course_ids:
        return []
    rows = db.session.scalars(select(Assignment).options(selectinload(Assignment.course)).where(
        Assignment.course_id.in_(course_ids), Assignment.missing.is_(True)).order_by(Assignment.due_at)).all()
    return [a for a in rows if not a.user_done]


def class_rows(courses: list[Course]) -> list[Course]:
    """One row per class: sections sharing a class_key merge; the one with a grade wins."""
    chosen: dict[str, Course] = {}
    for c in courses:
        prev = chosen.get(c.class_key)
        if prev is None or (prev.current_score is None and c.current_score is not None):
            chosen[c.class_key] = c
    return list(chosen.values())


def recent_announcements(user_id: int, days: int = 10, limit: int = 6) -> list[Announcement]:
    course_ids = [c.id for c in visible_courses(user_id)]
    if not course_ids:
        return []
    return list(db.session.scalars(
        select(Announcement).where(Announcement.course_id.in_(course_ids),
                                   Announcement.posted_at >= utcnow() - timedelta(days=days))
        .order_by(Announcement.posted_at.desc()).limit(limit)))


def accounts(user_id: int) -> list[CanvasAccount]:
    return list(db.session.scalars(select(CanvasAccount).where(CanvasAccount.user_id == user_id)
                                   .order_by(CanvasAccount.last_sync_at.desc())))


def deck_count(user_id: int) -> int:
    return db.session.scalar(select(func.count(Deck.id)).where(Deck.user_id == user_id)) or 0
