"""Class chat: one opt-in room per Canvas course, for classmates who use Homework Hatch.

You can join a room if your synced courses include that course at that school. Enrollment isn't
independently confirmed (Homework Hatch doesn't collect class rosters), and the room says so.
No teacher or school admin is involved; moderation is automatic plus student reports reviewed by
site admins.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select

from .. import queries
from ..extensions import db
from ..models import ChatMessage, ChatReport, Course, User, utcnow
from ..services import moderation
from ..utils import fmt_dt

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _member_count(room_key: str) -> int:
    return db.session.scalar(select(func.count(func.distinct(Course.user_id))).join(User, User.id == Course.user_id)
                             .where(Course.room_key == room_key, Course.active.is_(True), Course.chat_joined.is_(True),
                                    User.active.is_(True))) or 0


def _course(course_id: int) -> Course:
    course = queries.owned_course(current_user.id, course_id)
    if course.account.lms == "ics":  # calendar-link classes have no classmates to chat with
        abort(404)
    if not course.active:
        abort(404)
    return course


def _room(course_id: int) -> Course:
    """A room the student has joined; reading and posting need an explicit join first."""
    course = _course(course_id)
    if not course.chat_joined:
        abort(403)
    return course


def _payload(m: ChatMessage) -> dict:
    return {"id": m.id, "author": m.user.display_name, "mine": m.user_id == current_user.id,
            "body": m.body, "time": fmt_dt(m.created_at, "time"), "date": fmt_dt(m.created_at, "date")}


@bp.route("/")
@login_required
def index():
    # Class chat is for classmates found through Canvas; a calendar link's classes have no classmates.
    courses = [c for c in queries.visible_courses(current_user.id) if c.account.lms != "ics"]
    keys = [c.room_key for c in courses]
    counts, last = {}, {}
    if keys:
        counts = dict(db.session.execute(select(Course.room_key, func.count(func.distinct(Course.user_id)))
                                         .where(Course.room_key.in_(keys), Course.active.is_(True),
                                                Course.chat_joined.is_(True))
                                         .group_by(Course.room_key)).all())
        last = dict(db.session.execute(select(ChatMessage.room_key, func.max(ChatMessage.created_at))
                                       .where(ChatMessage.room_key.in_(keys), ChatMessage.deleted.is_(False))
                                       .group_by(ChatMessage.room_key)).all())
    return render_template("chat/index.html", courses=courses, counts=counts, last=last)


@bp.route("/course/<int:course_id>")
@login_required
def room(course_id: int):
    course = _course(course_id)
    return render_template("chat/room.html", course=course, members=_member_count(course.room_key))


@bp.route("/course/<int:course_id>/join", methods=["POST"])
@login_required
def join(course_id: int):
    course = _course(course_id)
    course.chat_joined = request.form.get("leave") != "1"
    db.session.commit()
    return redirect(url_for("chat.room", course_id=course.id))


@bp.route("/course/<int:course_id>/messages")
@login_required
def messages(course_id: int):
    course = _room(course_id)
    after = request.args.get("after", type=int) or 0
    stmt = select(ChatMessage).where(ChatMessage.room_key == course.room_key, ChatMessage.deleted.is_(False))
    if after:
        rows = db.session.scalars(stmt.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(200)).all()
    else:
        rows = list(reversed(db.session.scalars(stmt.order_by(ChatMessage.id.desc()).limit(100)).all()))
    # Messages deleted or hidden since the client loaded them, so open windows can drop them.
    deleted = db.session.scalars(select(ChatMessage.id).where(
        ChatMessage.room_key == course.room_key, ChatMessage.deleted.is_(True),
        ChatMessage.created_at >= utcnow() - timedelta(days=7))).all()
    return jsonify({"messages": [_payload(m) for m in rows], "deleted": deleted})


@bp.route("/course/<int:course_id>/messages", methods=["POST"])
@login_required
def post(course_id: int):
    course = _room(course_id)
    try:
        moderation.check_rate(current_user.id)
        body = moderation.clean((request.get_json(silent=True) or {}).get("body"))
    except moderation.Rejected as exc:
        return jsonify({"error": str(exc)}), 400
    m = ChatMessage(room_key=course.room_key, user_id=current_user.id, body=body)
    db.session.add(m)
    db.session.commit()
    return jsonify({"message": _payload(m)})


def _message_in_my_rooms(message_id: int) -> ChatMessage:
    m = db.session.get(ChatMessage, message_id)
    if m is None or m.deleted:
        abort(404)
    mine = db.session.scalar(select(Course.id).where(Course.user_id == current_user.id, Course.room_key == m.room_key,
                                                     Course.chat_joined.is_(True)).limit(1))
    if not current_user.is_admin and not mine:
        abort(404)
    return m


@bp.route("/messages/<int:message_id>/report", methods=["POST"])
@login_required
def report(message_id: int):
    m = _message_in_my_rooms(message_id)
    if m.user_id == current_user.id:
        return jsonify({"error": "You can't report your own message."}), 400
    exists = db.session.scalar(select(ChatReport.id).where(ChatReport.message_id == m.id,
                                                           ChatReport.reporter_id == current_user.id))
    if not exists:
        reason = ((request.get_json(silent=True) or {}).get("reason") or "")[:300] or None
        db.session.add(ChatReport(message_id=m.id, reporter_id=current_user.id, reason=reason))
        db.session.flush()
        # Three open reports within a day hide a message until an admin looks.
        recent = db.session.scalar(select(func.count(ChatReport.id)).where(
            ChatReport.message_id == m.id, ChatReport.resolved.is_(False),
            ChatReport.created_at >= utcnow() - timedelta(days=1))) or 0
        if recent >= 3:
            m.deleted = True
        db.session.commit()
    return jsonify({"ok": True})


@bp.route("/messages/<int:message_id>/delete", methods=["POST"])
@login_required
def delete(message_id: int):
    m = _message_in_my_rooms(message_id)
    if m.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    m.deleted = True
    db.session.commit()
    return jsonify({"ok": True})
