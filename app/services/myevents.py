"""The student's own calendar items (UserEvent): study blocks and tasks the tutor suggested, or ones
added by hand. Free: nothing here calls the AI.

**From the tutor.** When an answer schedules something ("Mon 7-8pm: review chapter 3"), the tutor ends
it with a machine-readable block, in the same reply, so it costs nothing extra:

    <hh-calendar>
    [{"title": "Review chapter 3", "date": "2026-10-12", "start": "19:00", "end": "20:00", "class": "Calculus I"}]
    </hh-calendar>

split_answer() takes the block out of the text the student reads (and the stream hides it as it
arrives), and keeps the checked items on the TutorMessage. The student picks and edits them under the
answer, then adds them; nothing reaches the calendar without that tap. To plan with real dates, each
question goes with context(): the current date and time in the student's time zone, their classes,
what's due soon and what's already on their calendar.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select

from .. import queries
from ..extensions import db
from ..models import (Assignment, CalendarEvent, Course, StudyPlan, StudySession, TutorMessage, User, UserEvent,
                      utcnow)
from ..utils import local_now, to_local, user_zone

MAX_SUGGESTED = 30          # items kept from one answer
MAX_PER_ADD = 30            # items added in one go
MAX_ITEMS = 5000            # a student's items in all
DEFAULT_MINUTES = 60        # a timed item with no end
MAX_MINUTES = 12 * 60
TITLE_MAX, NOTES_MAX = 120, 500
DONE_STATUSES = {"done", "submitted", "submitted_late", "graded"}

# The block starts its own line (a mention of the tag mid-sentence isn't one). It may be wrapped in a
# code fence, taken only when the fences pair up around it, or cut off before it closes.
_BLOCK = re.compile(r"^[ \t]{0,10}```[\w-]{0,20}[ \t]{0,10}\n[ \t]{0,10}<hh-calendar>(.*?)</hh-calendar>[ \t]{0,10}\n[ \t]{0,10}```[ \t]{0,10}$"
                    r"|^[ \t]{0,10}<hh-calendar>(.*?)(?:</hh-calendar>|\Z)", re.S | re.I | re.M)
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s{0,2}([ap])?\.?(?:m\.?)?$", re.I)


class ItemError(ValueError):
    pass


# ---------------------------------------------------------------- reading the tutor's answer


def split_answer(text: str, today: date) -> tuple[str, list[dict]]:
    """(the answer without its calendar blocks, the blocks' valid items)."""
    found = list(_BLOCK.finditer(text or ""))
    if not found:
        return text, []
    items = [x for m in found for x in parse_items(m.group(1) if m.group(1) is not None else m.group(2), today)]
    clean = re.sub(r"\n{3,}", "\n\n", _BLOCK.sub("", text)).strip()
    return clean, items[:MAX_SUGGESTED]


def parse_items(raw: str, today: date) -> list[dict]:
    raw = re.sub(r"^\s*```[\w-]*\s*|\s*```\s*$", "", raw or "")
    try:
        data = json.loads(raw)
    except ValueError:  # one object per line also works
        data = []
        for line in raw.splitlines():
            try:
                data.append(json.loads(line.strip().rstrip(",")))
            except ValueError:
                continue
    if isinstance(data, dict):
        data = data.get("items") if isinstance(data.get("items"), list) else [data]
    if not isinstance(data, list):
        return []
    items = (clean_item(d, today - timedelta(days=1), today + timedelta(days=400)) for d in data[:MAX_SUGGESTED * 2])
    return [x for x in items if x][:MAX_SUGGESTED]


def _clock(value) -> str | None:
    """"19:00", "7:00 PM", "7pm" -> "19:00"; anything else None."""
    m = _TIME.match(str(value or "").strip())
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "p" else 0)
    if (hour, minute) == (24, 0):  # the end of the day
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def clean_item(d, earliest: date, latest: date) -> dict | None:
    """One suggested or submitted item, checked and tidied, or None."""
    if not isinstance(d, dict):
        return None
    title = " ".join(str(d.get("title") or "").split())[:TITLE_MAX]
    try:
        day = date.fromisoformat(str(d.get("date") or "")[:10])
    except ValueError:
        return None
    if not title or not earliest <= day <= latest:
        return None
    start = _clock(d.get("start"))
    end = _clock(d.get("end")) if start else None
    if end and not 0 < _minutes(start, end) <= MAX_MINUTES:
        end = None  # the same as the start, or over 12 hours: the default length instead
    cls = " ".join(str(d.get("class") or "").split())[:TITLE_MAX] or None
    notes = str(d.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:NOTES_MAX] or None
    return {"title": title, "date": day.isoformat(), "start": start, "end": end, "class": cls, "notes": notes}


def _minutes(start: str, end: str) -> int:
    """How long from start to end; an end at or before the start is the next day (22:00-00:30)."""
    (h1, m1), (h2, m2) = (map(int, start.split(":")), map(int, end.split(":")))
    return ((h2 * 60 + m2) - (h1 * 60 + m1)) % (24 * 60)


def for_history(message: TutorMessage) -> str:
    """A past message as the model sees it again: answers keep the items they suggested, so "move
    Wednesday's to Thursday" can send a corrected list."""
    if message.role == "assistant" and message.calendar:
        return f"{message.content}\n\n<hh-calendar>\n{json.dumps(message.calendar)}\n</hh-calendar>"
    return message.content


# ---------------------------------------------------------------- what the tutor knows about dates


def _span(e, zone, first: date, last: date) -> tuple[datetime, str] | None:
    """(sort time, "Fri Oct 9, 7:00 PM – 8:00 PM" / "Sat Oct 10 – Mon Oct 12, all day") for an event or
    item between first and last, placed on days exactly as the calendar page does."""
    from ..blueprints.main import _event_days  # the calendar's own rules (all-day dates, overnight spans)

    days = [d for d in _event_days(e, zone) if first <= d[0] <= last]
    if not days:
        return None
    (d0, when, short, long), d1 = days[0], days[-1][0]
    if d1 == d0:
        return when, f"{d0:%a %b %-d}, {'all day' if long == 'All day' else long}"
    return when, f"{d0:%a %b %-d} – {d1:%a %b %-d}, " + ("all day" if short == "All day" else f"from {short}")


def _name(text: str) -> str:
    return re.sub(r"[<>]", "", " ".join((text or "").split()))[:120]


def _when(dt: datetime, user: User) -> str:
    local = to_local(dt, user)
    return f"{local:%a %b %-d, %-I:%M %p}"


def context(user: User, course_ids: list[int]) -> str:
    """The <today> block that goes with each question: now, the student's classes, what's due in the
    next three weeks (in the chat's classes) and what's on their calendar for the next two."""
    now, zone = utcnow(), user_zone(user)
    local = local_now(user)
    lines = [f"Now: {local:%A, %B %-d, %Y, %-I:%M %p} ({zone.key})"]
    courses = queries.visible_courses(user.id)
    if courses:
        lines.append("Classes: " + "; ".join(_name(c.name) for c in courses[:25]))
    names = {c.id: _name(c.name) for c in courses}
    if course_ids:
        due = db.session.scalars(select(Assignment).where(
            Assignment.course_id.in_(course_ids), Assignment.due_at >= now - timedelta(hours=12),
            Assignment.due_at <= now + timedelta(days=21)).order_by(Assignment.due_at).limit(25)).all()
        if due:
            lines.append("Due soon:")
            lines += [f"- {_when(a.due_at, user)}: {_name(a.name)} ({names.get(a.course_id, 'class')})"
                      + (" - done" if a.effective_status in DONE_STATUSES else "") for a in due]
    plans = db.session.scalars(select(StudyPlan).where(
        StudyPlan.user_id == user.id, StudyPlan.status == "active", StudyPlan.exam_at > now)
        .order_by(StudyPlan.exam_at).limit(10)).all()
    if plans:
        lines.append("Tests they're preparing for:")
        lines += [f"- {_name(p.title)}" + (f" ({names[p.course_id]})" if p.course_id in names else "")
                  + f" on {_when(p.exam_at, user)}" for p in plans]
    soon = []
    first, last = local.date(), local.date() + timedelta(days=14)
    window = (now + timedelta(days=15), now - timedelta(days=1))
    for e in db.session.scalars(select(UserEvent).where(
            UserEvent.user_id == user.id, UserEvent.start_at < window[0],
            func.coalesce(UserEvent.end_at, UserEvent.start_at) >= window[1]).order_by(UserEvent.start_at).limit(40)):
        line = _span(e, zone, first, last)
        if line:
            soon.append((line[0], f"- {line[1]}: {_name(e.title)}" + (" - done" if e.done_at else "")))
    for s in db.session.scalars(select(StudySession).join(StudyPlan).where(
            StudySession.user_id == user.id, StudyPlan.status == "active", StudySession.day >= first.isoformat(),
            StudySession.day <= last.isoformat()).order_by(StudySession.day).limit(15)):
        day = date.fromisoformat(s.day)
        soon.append((_utc(day, "00:00", user), f"- {day:%a %b %-d}: study for {_name(s.plan.title)}, {s.minutes} min"
                     + (" - done" if s.done_at else "")))
    for e in db.session.scalars(select(CalendarEvent).where(
            CalendarEvent.user_id == user.id, CalendarEvent.start_at < window[0],
            func.coalesce(CalendarEvent.end_at, CalendarEvent.start_at) >= window[1]).order_by(CalendarEvent.start_at).limit(25)):
        line = _span(e, zone, first, last)
        if line:
            soon.append((line[0], f"- {line[1]}: {_name(e.title)}"))
    if soon:
        lines.append("Already on their calendar (next two weeks):")
        lines += [line for _, line in sorted(soon, key=lambda x: x[0])][:40]
    return "<today>\n" + "\n".join(lines) + "\n</today>"


# ---------------------------------------------------------------- saving


def _course_id(user: User, value) -> int | None:
    try:
        ident = int(value)
    except (TypeError, ValueError):
        return None
    course = db.session.get(Course, ident)
    return course.id if course is not None and course.user_id == user.id else None


def _utc(day: date, at: str, user: User) -> datetime:
    return (datetime.combine(day, time.fromisoformat(at), tzinfo=user_zone(user))
            .astimezone(timezone.utc).replace(tzinfo=None))


def apply(event: UserEvent, item: dict, user: User) -> None:
    """Set an item's fields from a checked dict (clean_item's shape plus course_id)."""
    day = date.fromisoformat(item["date"])
    event.title, event.notes = item["title"], item.get("notes")
    event.course_id = _course_id(user, item.get("course_id"))
    if item.get("start"):
        event.all_day, event.all_day_date = False, None
        event.start_at = _utc(day, item["start"], user)
        end = item.get("end")
        event.end_at = _utc(day + timedelta(days=1) if end and end <= item["start"] else day, end, user) if end else None
        if event.end_at is None or event.end_at <= event.start_at:  # no end, or a start in a clock change's gap
            event.end_at = event.start_at + timedelta(minutes=DEFAULT_MINUTES)
    else:
        event.all_day, event.all_day_date = True, day
        event.start_at = _utc(day, "00:00", user)
        event.end_at = None
    event.updated_at = utcnow()


def check(raw: dict, user: User) -> dict:
    """A submitted item (from the tutor's list or the calendar's form), or ItemError."""
    today = local_now(user).date()
    item = clean_item(raw, today - timedelta(days=366), today + timedelta(days=730))
    if item is None:
        raise ItemError("Each item needs a title and a date within the next two years.")
    if raw.get("end") and item["start"] and not item["end"]:
        raise ItemError(f"“{item['title']}” ends before it starts. Fix the end time, or leave it empty for an hour.")
    item["course_id"] = raw.get("course_id")
    return item


def room_for(user: User, n: int) -> None:
    have = db.session.scalar(select(func.count(UserEvent.id)).where(UserEvent.user_id == user.id)) or 0
    if have + n > MAX_ITEMS:
        raise ItemError("Your calendar is full of items already. Delete some old ones first.")


def _key(event: UserEvent) -> tuple:
    return event.title.casefold(), event.start_at, event.all_day_date


def _unsaved(item: dict, user: User) -> UserEvent:
    event = UserEvent(user_id=user.id)
    apply(event, dict(item, course_id=None), user)
    return event


def _keys_near(user: User, events: list[UserEvent]) -> set[tuple]:
    """Keys of the student's items around the given ones' times (the same title at the same time = the same item)."""
    if not events:
        return set()
    lo = min(e.start_at for e in events) - timedelta(days=1)
    hi = max(e.start_at for e in events) + timedelta(days=1)
    return {_key(e) for e in db.session.scalars(select(UserEvent).where(
        UserEvent.user_id == user.id, UserEvent.start_at >= lo, UserEvent.start_at <= hi))}


def mark_existing(user: User, items: list[dict] | None) -> list[dict]:
    """The tutor's suggestions, each with "have": already on the student's calendar (so a revised plan
    doesn't add the unchanged ones twice)."""
    if not items:
        return []
    events = [_unsaved(i, user) for i in items]
    keys = _keys_near(user, events)
    return [dict(i, have=_key(e) in keys) for i, e in zip(items, events)]


def add_from_tutor(user: User, message: TutorMessage, raw_items: list) -> tuple[list[UserEvent], int]:
    """Add the items the student kept (edited or not) from a tutor answer. Returns (added, skipped as
    already on their calendar)."""
    if not isinstance(raw_items, list) or not raw_items:
        raise ItemError("Pick at least one item.")
    if len(raw_items) > MAX_PER_ADD:
        raise ItemError(f"Add up to {MAX_PER_ADD} items at a time.")
    items = [check(d if isinstance(d, dict) else {}, user) for d in raw_items]
    room_for(user, len(items))
    events = []
    for item in items:
        event = UserEvent(user_id=user.id, source="tutor", message_id=message.id)
        apply(event, item, user)
        events.append(event)
    have = _keys_near(user, events)
    added = []
    for event in events:
        if _key(event) in have:
            continue
        have.add(_key(event))
        db.session.add(event)
        added.append(event)
    return added, len(events) - len(added)


def left_from_earlier(user: User, message: TutorMessage) -> list[UserEvent]:
    """Upcoming, unfinished items added from earlier answers in the same chat that this answer's plan
    doesn't have: when a plan is revised, the student is offered to remove what it replaced."""
    earlier = select(TutorMessage.id).where(TutorMessage.conversation_id == message.conversation_id,
                                            TutorMessage.id < message.id)
    rows = db.session.scalars(select(UserEvent).where(
        UserEvent.user_id == user.id, UserEvent.message_id.in_(earlier), UserEvent.done_at.is_(None),
        UserEvent.start_at >= utcnow()).order_by(UserEvent.start_at).limit(MAX_SUGGESTED)).all()
    if not rows:
        return []
    current = {_key(_unsaved(i, user)) for i in message.calendar or []}
    current |= {_key(e) for e in db.session.scalars(select(UserEvent).where(UserEvent.message_id == message.id))}
    return [e for e in rows if _key(e) not in current]


def label(event: UserEvent, user: User) -> str:
    """"Sat Oct 10" for an all-day item, "Fri Oct 9, 7:00 PM" otherwise."""
    if event.all_day and event.all_day_date:
        return f"{event.all_day_date:%a %b %-d}"
    return _when(event.start_at, user)


def local_day(event: UserEvent, user: User) -> date:
    return event.all_day_date if event.all_day and event.all_day_date else to_local(event.start_at, user).date()


def added_counts(message_ids: list[int]) -> dict[int, int]:
    if not message_ids:
        return {}
    return dict(db.session.execute(select(UserEvent.message_id, func.count(UserEvent.id))
                                   .where(UserEvent.message_id.in_(message_ids)).group_by(UserEvent.message_id)).all())
