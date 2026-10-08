"""Class chat rooms and private direct messages between classmates.

**Who is in a class room.** A room is one real course at one school, keyed by the LMS's own identity
for it plus proof of being in it: "<host>:<course id>:<hash of the course's Canvas uuid>"
(Course.chat_key, set by sync from extension 1.5.2). Canvas shows a course's uuid only to people in
the course, so a hand-made sync can't land in a real class's room. Names play no part: renaming a
class on its Customize tab (or the teacher renaming it in Canvas) never moves anyone. Every student
whose synced classes include the course is in its room automatically; they can mute it, and hiding
the class takes it out of chat. Teachers, TAs, designers and observers (Canvas's enrollment role)
aren't in rooms, and neither are classes from calendar links.

**Who can message whom.** Only classmates: someone in a room you're in, met through a message they
posted there (there's no member list or user search). Messages stay within an age band (User.age_band:
adult or minor), so adults and students under 18 never message each other; birth dates can't be edited
after sign-up. Someone whose birth date can't tell which side of 18 they're on ("edge": a birth year
without a month, or the month of their 18th birthday) can't send or get direct messages until it can;
accounts missing the month can add it once on the Chat page. Age, blocks, closed accounts and turned-off requests all get the same refusal
(NOT_TAKING), so it never says which applies. A first message is a request; nothing more can be sent until the other
student accepts (replying accepts). Blocking stops messages both ways until undone. A student can
turn off new requests. Posting or messaging needs a one-time agreement to the chat rules. Messages
are moderated like class chat (and contact details are refused in rooms and in any conversation
with someone under 18), rate-limited together, and stored encrypted. A report copies the message and
a little of the conversation around it for site admins, so deleting it can't erase it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import CanvasAccount, Course, DirectMessage, DirectReport, DirectThread, User, UserBlock, utcnow
from . import moderation

NOT_TAKING = "This classmate isn't taking messages from you."  # the one wording for every "no"
NEW_REQUESTS_PER_DAY = 10
NEW_ACCOUNT_WAIT = timedelta(days=1)  # before a new account can send requests (slows block evasion)
CONTEXT_ON_REPORT = 5            # messages either side of a reported one that admins see


class DMError(ValueError):
    pass


def protected(user: User) -> bool:
    """Under 18, or possibly: their conversations refuse contact details."""
    return user.age_band != "adult"


# ---------------------------------------------------------------- class rooms


def _room_courses():
    """Courses that put their student in a class room."""
    return (select(Course).join(CanvasAccount, CanvasAccount.id == Course.account_id)
            .where(Course.active.is_(True), Course.hidden.is_(False), Course.chat_key.is_not(None),
                   CanvasAccount.lms != "ics", Course.enrollment_role == "student"))


def room_courses(user_id: int, include_muted: bool = True) -> list[Course]:
    stmt = _room_courses().where(Course.user_id == user_id)
    if not include_muted:
        stmt = stmt.where(Course.chat_muted.is_(False))
    return list(db.session.scalars(stmt.order_by(Course.name)))


def in_room(user_id: int, chat_key: str) -> bool:
    return db.session.scalar(_room_courses().with_only_columns(Course.id)
                             .where(Course.user_id == user_id, Course.chat_key == chat_key).limit(1)) is not None


def member_count(chat_key: str) -> int:
    return db.session.scalar(_room_courses().with_only_columns(func.count(func.distinct(Course.user_id)))
                             .join(User, User.id == Course.user_id)
                             .where(Course.chat_key == chat_key, User.active.is_(True))) or 0


def shared_room(a_id: int, b_id: int) -> str | None:
    """A class room both students are in, if any."""
    mine = {c.chat_key for c in room_courses(a_id)}
    if not mine:
        return None
    return db.session.scalar(_room_courses().with_only_columns(Course.chat_key)
                             .where(Course.user_id == b_id, Course.chat_key.in_(mine)).limit(1))


def room_status(user_id: int) -> str:
    """Why a student has no class rooms, for the Chat page to say so (never "connect Canvas" to someone
    who has):
    "none": nothing connected yet; "not_canvas": only Brightspace or calendar-link classes (rooms are
    Canvas only for now); "update": Canvas classes synced by an extension older than 1.5.2, which sends
    neither the course ID rooms need nor the student's role (the extension syncs as soon as Chrome
    updates it, so rooms then appear on their own); "no_proof": a newer extension synced but Canvas gave
    no course ID; "not_student": rooms exist for none of their classes (hidden, or not taken as a student)."""
    rows = db.session.execute(select(Course, CanvasAccount.lms).join(CanvasAccount, CanvasAccount.id == Course.account_id)
                              .where(Course.user_id == user_id, Course.active.is_(True))).all()
    if not rows:
        return "none"
    canvas = [c for c, lms in rows if lms == "canvas"]
    if not canvas:
        return "not_canvas"
    if any(c.chat_key for c in canvas):
        return "not_student"
    return "update" if all(c.enrollment_role is None for c in canvas) else "no_proof"


# ---------------------------------------------------------------- who can message whom


def is_new(user: User) -> bool:
    return user.created_at is not None and user.created_at > utcnow() - NEW_ACCOUNT_WAIT


def blocked_ids(user_id: int) -> set[int]:
    """People this student blocked."""
    return set(db.session.scalars(select(UserBlock.blocked_id).where(UserBlock.blocker_id == user_id)))


def blocked(a_id: int, b_id: int) -> bool:
    return db.session.scalar(select(UserBlock.id).where(
        or_((UserBlock.blocker_id == a_id) & (UserBlock.blocked_id == b_id),
            (UserBlock.blocker_id == b_id) & (UserBlock.blocked_id == a_id))).limit(1)) is not None


def refusal(sender: User, recipient: User, new_request: bool = True) -> str | None:
    """Why `sender` can't message `recipient`, or None. A new request needs a shared class room, checked
    first so non-classmates all hear the same thing. After that, age, blocks, closed accounts and
    turned-off requests all read the same (NOT_TAKING), so a refusal never says which applies.
    Conversations already under way outlive the class (new_request=False skips the room check)."""
    if sender.id == recipient.id:
        return "That's you."
    if not sender.chat_agreed_at:
        return "Agree to the chat rules first."
    if new_request and shared_room(sender.id, recipient.id) is None:
        return "You can only message classmates."
    if new_request and is_new(sender):
        return "New accounts can send message requests a day after signing up."
    bands = (sender.age_band, recipient.age_band)
    if not recipient.active or not sender.active or blocked(sender.id, recipient.id) \
            or bands[0] != bands[1] or "edge" in bands or (new_request and not recipient.allow_dms):
        return NOT_TAKING
    return None


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def thread_between(a_id: int, b_id: int) -> DirectThread | None:
    lo, hi = _pair(a_id, b_id)
    return db.session.scalar(select(DirectThread).where(DirectThread.user_a_id == lo, DirectThread.user_b_id == hi))


def _post(thread: DirectThread, sender: User, body: str) -> DirectMessage:
    moderation.check_rate(sender.id)
    body = moderation.clean(body)
    other = db.session.get(User, thread.other(sender.id))
    if protected(sender) or protected(other):
        moderation.check_contact(body)
    m = DirectMessage(thread_id=thread.id, sender_id=sender.id, body=body)
    db.session.add(m)
    thread.last_message_at = utcnow()
    thread.last_sender_id = sender.id
    mark_read(thread, sender.id)
    return m


def start(sender: User, recipient: User, body: str, room_key: str | None) -> DirectThread:
    """A first message to a classmate (a request), or a message in the thread the two already have."""
    existing = thread_between(sender.id, recipient.id)
    if existing is not None and not (existing.status == "declined" and existing.started_by != sender.id):
        send(sender, existing, body)
        return existing
    # A new request, or the student who declined one now reaching out themselves.
    why = refusal(sender, recipient)
    if why:
        raise DMError(why)
    today = db.session.scalar(select(func.count(DirectThread.id)).where(
        DirectThread.started_by == sender.id, DirectThread.created_at >= utcnow() - timedelta(days=1))) or 0
    if today >= NEW_REQUESTS_PER_DAY:
        raise DMError("You've sent a lot of new message requests today. Try again tomorrow.")
    lo, hi = _pair(sender.id, recipient.id)
    thread = existing or DirectThread(user_a_id=lo, user_b_id=hi)
    thread.started_by, thread.status, thread.created_at = sender.id, "request", utcnow()
    thread.room_key = room_key or shared_room(sender.id, recipient.id)
    db.session.add(thread)
    db.session.flush()
    _post(thread, sender, body)
    return thread


def send(sender: User, thread: DirectThread, body: str) -> DirectMessage:
    if sender.id not in (thread.user_a_id, thread.user_b_id):
        raise DMError("Not your conversation.")
    other = db.session.get(User, thread.other(sender.id))
    why = refusal(sender, other, new_request=False)
    if why:
        raise DMError(why)
    if thread.status == "declined":
        raise DMError("This request was declined.")
    if thread.status == "request":
        if sender.id == thread.started_by:
            raise DMError("Wait until they accept your request.")
        thread.status = "active"  # replying to a request accepts it
    return _post(thread, sender, body)


def accept(user: User, thread: DirectThread) -> None:
    if user.id == thread.started_by or thread.status != "request":
        raise DMError("Nothing to accept.")
    why = refusal(user, db.session.get(User, thread.other(user.id)), new_request=False)
    if why:
        raise DMError(why)
    thread.status = "active"


def decline(user: User, thread: DirectThread) -> None:
    if user.id == thread.started_by or thread.status != "request":
        raise DMError("Nothing to decline.")
    thread.status = "declined"


def block(user: User, other_id: int) -> None:
    if other_id == user.id:
        raise DMError("That's you.")
    if not db.session.scalar(select(UserBlock.id).where(UserBlock.blocker_id == user.id, UserBlock.blocked_id == other_id)):
        db.session.add(UserBlock(blocker_id=user.id, blocked_id=other_id))


def unblock(user: User, other_id: int) -> None:
    row = db.session.scalar(select(UserBlock).where(UserBlock.blocker_id == user.id, UserBlock.blocked_id == other_id))
    if row is not None:
        db.session.delete(row)


def blocked_people(user_id: int) -> list[User]:
    ids = db.session.scalars(select(UserBlock.blocked_id).where(UserBlock.blocker_id == user_id)).all()
    return list(db.session.scalars(select(User).where(User.id.in_(ids)))) if ids else []


def message_deleted(thread: DirectThread) -> None:
    """After a message is deleted: the thread's "last message" is the newest one left."""
    last = db.session.scalar(select(DirectMessage).where(DirectMessage.thread_id == thread.id, DirectMessage.deleted.is_(False))
                             .order_by(DirectMessage.id.desc()).limit(1))
    thread.last_sender_id = last.sender_id if last else None
    thread.last_message_at = last.created_at if last else thread.created_at


# ---------------------------------------------------------------- inbox


def mark_read(thread: DirectThread, user_id: int) -> None:
    if user_id == thread.user_a_id:
        thread.a_read_at = utcnow()
    elif user_id == thread.user_b_id:
        thread.b_read_at = utcnow()


def _read_at(thread: DirectThread, user_id: int):
    return thread.a_read_at if user_id == thread.user_a_id else thread.b_read_at


def is_unread(thread: DirectThread, user_id: int) -> bool:
    read = _read_at(thread, user_id)
    return thread.last_sender_id not in (None, user_id) and (read is None or thread.last_message_at > read)


def threads(user_id: int) -> list[DirectThread]:
    """The student's conversations, newest first: declined requests they sent stay visible to them;
    ones sent to them, and anything with someone they blocked, don't."""
    rows = db.session.scalars(select(DirectThread).where(
        or_(DirectThread.user_a_id == user_id, DirectThread.user_b_id == user_id))
        .order_by(DirectThread.last_message_at.desc())).all()
    hidden = blocked_ids(user_id)
    return [t for t in rows if t.other(user_id) not in hidden
            and not (t.status == "declined" and t.started_by != user_id)]


def unread_count(user_id: int) -> int:
    return sum(1 for t in threads(user_id) if is_unread(t, user_id) and t.status != "declined")


def messages(thread: DirectThread, after: int = 0, limit: int = 200) -> list[DirectMessage]:
    """Deleted messages included: the recipient sees "Message deleted" in their place and can still
    report it (the payload never carries a deleted message's text)."""
    stmt = select(DirectMessage).where(DirectMessage.thread_id == thread.id)
    if after:
        return list(db.session.scalars(stmt.where(DirectMessage.id > after).order_by(DirectMessage.id).limit(limit)))
    return list(reversed(db.session.scalars(stmt.order_by(DirectMessage.id.desc()).limit(limit)).all()))


def report(user: User, message: DirectMessage, reason: str | None) -> None:
    """Works on deleted messages and declined or blocked conversations too: the reporter was in it."""
    thread = db.session.get(DirectThread, message.thread_id)
    if thread is None or user.id not in (thread.user_a_id, thread.user_b_id) or message.sender_id == user.id:
        raise DMError("You can only report messages sent to you.")
    if not db.session.scalar(select(DirectReport.id).where(DirectReport.message_id == message.id,
                                                           DirectReport.reporter_id == user.id)):
        sender = db.session.get(User, message.sender_id)
        db.session.add(DirectReport(message_id=message.id, reporter_id=user.id, sender_id=message.sender_id,
                                    sender_name=sender.username if sender else None,
                                    sender_band=sender.age_band if sender else None,
                                    reason=(reason or "").strip()[:300] or None, snapshot=_context(message)))


def _context(m: DirectMessage) -> list[dict]:
    """The reported message and up to CONTEXT_ON_REPORT on either side (deleted ones included, marked)."""
    around = select(DirectMessage).where(DirectMessage.thread_id == m.thread_id)
    before = db.session.scalars(around.where(DirectMessage.id < m.id).order_by(DirectMessage.id.desc())
                                .limit(CONTEXT_ON_REPORT)).all()
    after = db.session.scalars(around.where(DirectMessage.id > m.id).order_by(DirectMessage.id)
                               .limit(CONTEXT_ON_REPORT)).all()
    return [{"id": x.id, "from": "sender" if x.sender_id == m.sender_id else "reporter", "body": x.body,
             "at": x.created_at.isoformat(), "deleted": bool(x.deleted), "reported": x.id == m.id}
            for x in list(reversed(before)) + [m] + list(after)]


def report_context(report: DirectReport) -> list[dict]:
    """What an admin sees for a report: the copy made when it was filed."""
    return report.snapshot or []
