"""Chat: a room for each real class (automatic for every student who synced it) and private direct
messages between classmates. The rules live in services/dms.py.

Rooms are keyed by the LMS's own identity for the course plus proof of being in it (Course.chat_key,
"<host>:<course id>:<hash of the course's Canvas uuid>"), never by its name, so a class renamed on its
Customize tab stays in the same room. ChatMessage.room_key and DirectThread.room_key hold that key.
We don't collect rosters, so membership means "synced this course from their own Canvas". No teacher or
school admin is involved; moderation is automatic plus student reports reviewed by site admins.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select

from .. import queries
from ..extensions import db
from ..models import ChatMessage, ChatReport, Course, DirectMessage, DirectThread, User, utcnow
from ..services import dms, moderation
from ..utils import fmt_dt, parse_id

bp = Blueprint("chat", __name__, url_prefix="/chat")


def _course(course_id: int) -> Course:
    """One of the student's own classes that has a room (a Canvas class they're a student in)."""
    course = queries.owned_course(current_user.id, course_id)
    if course.id not in {c.id for c in dms.room_courses(current_user.id)}:
        abort(404)
    return course


def _json_text(key: str) -> str:
    """A string field from the JSON body ("" when it's missing or not a string)."""
    data = request.get_json(silent=True)
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _payload(m: ChatMessage) -> dict:
    mine = m.user_id == current_user.id
    # "Message" shows on everyone else's posts alike; whether it works is decided (and worded the same
    # way for every "no") on the next page, so the button never hints at someone's age.
    return {"id": m.id, "author": m.user.display_name, "mine": mine, "dm": not mine,
            "body": m.body, "time": fmt_dt(m.created_at, "time"), "date": fmt_dt(m.created_at, "date")}


@bp.route("/")
@login_required
def index():
    courses = dms.room_courses(current_user.id)
    keys = [c.chat_key for c in courses]
    counts, last = {}, {}
    if keys:
        counts = {k: dms.member_count(k) for k in keys}
        last = dict(db.session.execute(select(ChatMessage.room_key, func.max(ChatMessage.created_at))
                                       .where(ChatMessage.room_key.in_(keys), ChatMessage.deleted.is_(False))
                                       .group_by(ChatMessage.room_key)).all())
    convos = dms.threads(current_user.id)
    blocked = dms.blocked_people(current_user.id)
    people = {u.id: u for u in db.session.scalars(select(User).where(User.id.in_([t.other(current_user.id) for t in convos])))} if convos else {}
    return render_template("chat/index.html", courses=[c for c in courses if not c.chat_muted],
                           muted=[c for c in courses if c.chat_muted], counts=counts, last=last, convos=convos,
                           people=people, is_unread=dms.is_unread, blocked=blocked,
                           blocked_threads={u.id: getattr(dms.thread_between(current_user.id, u.id), "id", None) for u in blocked},
                           room_status=None if courses else dms.room_status(current_user.id))


@bp.route("/rules", methods=["POST"])
@login_required
def agree():
    """The one-time agreement to the chat rules that posting and messaging need."""
    current_user.chat_agreed_at = current_user.chat_agreed_at or utcnow()
    db.session.commit()
    return redirect(request.form.get("next") if (request.form.get("next") or "").startswith("/chat/") else url_for("chat.index"))


@bp.route("/birth-month", methods=["POST"])
@login_required
def birth_month():
    """Accounts made before sign-up kept the birth month can add it once, so messages can tell which
    side of 18 they're on (see dms.py)."""
    month = request.form.get("birth_month", type=int)
    if current_user.birth_month is None and current_user.birth_year is not None and month in range(1, 13):
        current_user.birth_month = month
        db.session.commit()
        flash("Saved.", "info")
    return redirect(url_for("chat.index"))


@bp.route("/settings", methods=["POST"])
@login_required
def settings():
    current_user.allow_dms = request.form.get("allow_dms") == "1"
    db.session.commit()
    flash("Classmates can send you message requests." if current_user.allow_dms
          else "New message requests are off. Conversations you already have stay open.", "info")
    return redirect(url_for("chat.index"))


# ---------------------------------------------------------------- class rooms


@bp.route("/course/<int:course_id>")
@login_required
def room(course_id: int):
    course = _course(course_id)
    return render_template("chat/room.html", course=course, members=dms.member_count(course.chat_key))


@bp.route("/course/<int:course_id>/mute", methods=["POST"])
@login_required
def mute(course_id: int):
    course = _course(course_id)
    course.chat_muted = request.form.get("mute") == "1"
    db.session.commit()
    return redirect(url_for("chat.room", course_id=course.id))


@bp.route("/course/<int:course_id>/messages")
@login_required
def messages(course_id: int):
    course = _course(course_id)
    after = request.args.get("after", type=int) or 0
    stmt = select(ChatMessage).where(ChatMessage.room_key == course.chat_key, ChatMessage.deleted.is_(False))
    hidden = dms.blocked_ids(current_user.id)
    if hidden:  # people you blocked: their room posts are hidden from you too
        stmt = stmt.where(ChatMessage.user_id.not_in(hidden))
    if after:
        rows = db.session.scalars(stmt.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(200)).all()
    else:
        rows = list(reversed(db.session.scalars(stmt.order_by(ChatMessage.id.desc()).limit(100)).all()))
    # Messages deleted or hidden since the client loaded them, so open windows can drop them.
    deleted = db.session.scalars(select(ChatMessage.id).where(
        ChatMessage.room_key == course.chat_key, ChatMessage.deleted.is_(True),
        ChatMessage.created_at >= utcnow() - timedelta(days=7))).all()
    return jsonify({"messages": [_payload(m) for m in rows], "deleted": deleted})


@bp.route("/course/<int:course_id>/messages", methods=["POST"])
@login_required
def post(course_id: int):
    course = _course(course_id)
    if not current_user.chat_agreed_at:
        return jsonify({"error": "Agree to the chat rules first."}), 403
    try:
        moderation.check_rate(current_user.id)
        body = moderation.clean(_json_text("body"))
        moderation.check_contact(body)  # rooms mix ages: no contact details or invite links
    except moderation.Rejected as exc:
        return jsonify({"error": str(exc)}), 400
    m = ChatMessage(room_key=course.chat_key, user_id=current_user.id, body=body)
    db.session.add(m)
    db.session.commit()
    return jsonify({"message": _payload(m)})


def _message_in_my_rooms(message_id: int) -> ChatMessage:
    m = db.session.get(ChatMessage, message_id)
    if m is None or m.deleted:
        abort(404)
    if not current_user.is_admin and not dms.in_room(current_user.id, m.room_key):
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
        reason = _json_text("reason").strip()[:300] or None
        db.session.add(ChatReport(message_id=m.id, reporter_id=current_user.id, reason=reason))
        db.session.flush()
        # Three open reports within a day hide a message until an admin looks. Accounts less than a
        # day old don't count toward that, so a few throwaway accounts can't hide someone's posts.
        recent = db.session.scalar(select(func.count(ChatReport.id)).join(User, User.id == ChatReport.reporter_id).where(
            ChatReport.message_id == m.id, ChatReport.resolved.is_(False),
            ChatReport.created_at >= utcnow() - timedelta(days=1),
            User.created_at <= utcnow() - dms.NEW_ACCOUNT_WAIT)) or 0
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


# ---------------------------------------------------------------- direct messages


def _thread(thread_id: int) -> DirectThread:
    """A conversation the student is in. Someone who blocked the other person can still open it (read
    only) to delete their own messages or report theirs; it just leaves their inbox."""
    t = db.session.get(DirectThread, thread_id)
    if t is None or current_user.id not in (t.user_a_id, t.user_b_id):
        abort(404)
    if t.status == "declined" and t.started_by != current_user.id:
        abort(404)
    return t


def _dm_payload(m: DirectMessage) -> dict:
    # A deleted message keeps its place (and its report link) for the recipient, never its text.
    return {"id": m.id, "mine": m.sender_id == current_user.id, "body": "" if m.deleted else m.body,
            "deleted": bool(m.deleted), "removed": bool(m.removed),
            "time": fmt_dt(m.created_at, "time"), "date": fmt_dt(m.created_at, "date")}


@bp.route("/dm/from/<int:message_id>", methods=["GET", "POST"])
@login_required
def dm_from_message(message_id: int):
    """Message the author of a class-chat message (the only way to start a conversation)."""
    m = _message_in_my_rooms(message_id)
    other = m.user
    if other.id in dms.blocked_ids(current_user.id):
        return render_template("chat/dm_new.html", other=other, message=m, course=None,
                               why=f"You blocked {other.display_name}. Unblock them on the Chat page to message them.")
    existing = dms.thread_between(current_user.id, other.id)
    if existing is not None and (existing.status != "declined" or existing.started_by == current_user.id):
        return redirect(url_for("chat.dm", thread_id=existing.id))
    why = dms.refusal(current_user, other)
    course = db.session.scalar(select(Course).where(Course.user_id == current_user.id, Course.chat_key == m.room_key).limit(1))
    if request.method == "POST":
        try:
            if why:
                raise dms.DMError(why)
            thread = dms.start(current_user, other, request.form.get("body") or "", m.room_key)
        except (dms.DMError, moderation.Rejected) as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template("chat/dm_new.html", other=other, message=m, why=None, course=course), 400
        db.session.commit()
        return redirect(url_for("chat.dm", thread_id=thread.id))
    return render_template("chat/dm_new.html", other=other, message=m, why=why, course=course)


@bp.route("/dm/<int:thread_id>")
@login_required
def dm(thread_id: int):
    t = _thread(thread_id)
    other = db.session.get(User, t.other(current_user.id))
    dms.mark_read(t, current_user.id)
    db.session.commit()
    course = db.session.scalar(select(Course).where(Course.user_id == current_user.id, Course.chat_key == t.room_key).limit(1)) \
        if t.room_key else None
    they_sent = db.session.scalar(select(DirectMessage.id).where(
        DirectMessage.thread_id == t.id, DirectMessage.sender_id == other.id).limit(1)) is not None
    return render_template("chat/dm.html", thread=t, other=other, course=course, they_sent=they_sent,
                           blocked_them=other.id in dms.blocked_ids(current_user.id))


@bp.route("/dm/<int:thread_id>/messages")
@login_required
def dm_messages(thread_id: int):
    t = _thread(thread_id)
    rows = dms.messages(t, after=request.args.get("after", type=int) or 0)
    dms.mark_read(t, current_user.id)
    db.session.commit()
    # Deleted since the window loaded: from the oldest message it holds (the client sends it), or
    # the last week of messages.
    since = request.args.get("since", type=int)
    gone = db.session.execute(select(DirectMessage.id, DirectMessage.removed).where(
        DirectMessage.thread_id == t.id, DirectMessage.deleted.is_(True),
        DirectMessage.id >= since if since else DirectMessage.created_at >= utcnow() - timedelta(days=7))).all()
    return jsonify({"messages": [_dm_payload(m) for m in rows], "deleted": [i for i, _ in gone],
                    "removed": [i for i, r in gone if r], "status": t.status,
                    "waiting": t.status == "request" and t.started_by == current_user.id})


@bp.route("/dm/<int:thread_id>/messages", methods=["POST"])
@login_required
def dm_send(thread_id: int):
    t = _thread(thread_id)
    try:
        m = dms.send(current_user, t, _json_text("body"))
    except (dms.DMError, moderation.Rejected) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify({"message": _dm_payload(m)})


@bp.route("/dm/<int:thread_id>/respond", methods=["POST"])
@login_required
def dm_respond(thread_id: int):
    t = _thread(thread_id)
    action = request.form.get("action")
    reported = False
    try:
        if action == "accept":
            dms.accept(current_user, t)
        elif action == "decline":
            dms.decline(current_user, t)
        elif action in ("block", "block_report"):
            if action == "block_report":  # report what they sent, then block
                theirs = db.session.scalar(select(DirectMessage).where(
                    DirectMessage.thread_id == t.id, DirectMessage.sender_id == t.other(current_user.id))
                    .order_by(DirectMessage.id.desc()).limit(1))
                if theirs is not None:
                    dms.report(current_user, theirs, "Reported with a block")
                    reported = True
            dms.block(current_user, t.other(current_user.id))
        else:
            abort(400)
    except dms.DMError as exc:
        flash(str(exc), "error")
        return redirect(url_for("chat.dm", thread_id=t.id))
    db.session.commit()
    if action in ("decline", "block", "block_report"):
        flash("Declined. They can't message you in this conversation." if action == "decline" else
              "Blocked. They can't message you, and you won't see their messages here or in class chat. "
              "You can unblock them on the Chat page."
              + (" Thanks for reporting; a moderator will look." if reported else ""), "info")
        return redirect(url_for("chat.index"))
    return redirect(url_for("chat.dm", thread_id=t.id))


def _my_dm(message_id) -> DirectMessage:
    """A message the student sent (deletable even after they blocked or were declined)."""
    ident = parse_id(message_id)
    m = db.session.get(DirectMessage, ident) if ident else None
    if m is None or m.deleted or m.sender_id != current_user.id:
        abort(404)
    return m


@bp.route("/dm/messages/<int:message_id>/report", methods=["POST"])
@login_required
def dm_report(message_id: int):
    # Any participant can report what they were sent, even after deleting, declining or blocking.
    m = db.session.get(DirectMessage, message_id)
    t = db.session.get(DirectThread, m.thread_id) if m is not None else None
    if t is None or current_user.id not in (t.user_a_id, t.user_b_id):
        abort(404)
    try:
        dms.report(current_user, m, _json_text("reason"))
    except dms.DMError as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/dm/messages/<int:message_id>/delete", methods=["POST"])
@login_required
def dm_delete(message_id: int):
    m = _my_dm(message_id)
    m.deleted = True
    dms.message_deleted(db.session.get(DirectThread, m.thread_id))
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/blocks/<int:user_id>/unblock", methods=["POST"])
@login_required
def unblock(user_id: int):
    dms.unblock(current_user, user_id)
    db.session.commit()
    flash("Unblocked.", "info")
    return redirect(url_for("chat.index"))
