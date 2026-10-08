"""iCalendar feed of due dates, Canvas events and the student's own calendar items, for
Google/Apple/Outlook calendars."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import utcnow


def _esc(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")  # a textarea's CRLF; a bare CR would break the line
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _dt(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")  # stored values are naive UTC


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on lines starting with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > (75 if not out else 74):
            out.append(current.decode("utf-8"))
            current = b""
        current += b
    out.append(current.decode("utf-8"))
    return "\r\n ".join(out)


def build(assignments, events, host: str, mine=()) -> str:
    stamp = _dt(utcnow())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Homework Hatch//Due dates//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Homework Hatch", "X-PUBLISHED-TTL:PT1H"]
    for a in assignments:
        if not a.due_at:
            continue
        lines += ["BEGIN:VEVENT", f"UID:assignment-{a.id}@{host}", f"DTSTAMP:{stamp}",
                  f"DTSTART:{_dt(a.due_at - timedelta(minutes=30))}", f"DTEND:{_dt(a.due_at)}",
                  f"SUMMARY:{_esc(f'Due: {a.name} ({a.course.name})')}"]
        if a.html_url:
            lines.append(f"URL:{a.html_url}")
        lines.append(f"DESCRIPTION:{_esc(f'Status: {a.effective_status}')}")
        lines.append("END:VEVENT")
    for e in events:
        if not e.start_at:
            continue
        end = e.end_at if e.end_at and e.end_at > e.start_at else e.start_at + timedelta(hours=1)
        lines += ["BEGIN:VEVENT", f"UID:event-{e.id}@{host}", f"DTSTAMP:{stamp}", f"DTSTART:{_dt(e.start_at)}",
                  f"DTEND:{_dt(end)}", f"SUMMARY:{_esc(e.title)}"]
        if e.location:
            lines.append(f"LOCATION:{_esc(e.location)}")
        if e.html_url:
            lines.append(f"URL:{e.html_url}")
        lines.append("END:VEVENT")
    for e in mine:  # items the student added (from the tutor or by hand)
        if e.all_day and e.all_day_date:
            span = [f"DTSTART;VALUE=DATE:{e.all_day_date:%Y%m%d}", f"DTEND;VALUE=DATE:{e.all_day_date + timedelta(days=1):%Y%m%d}"]
        else:
            end = e.end_at if e.end_at and e.end_at > e.start_at else e.start_at + timedelta(hours=1)
            span = [f"DTSTART:{_dt(e.start_at)}", f"DTEND:{_dt(end)}"]
        summary = ("✓ " if e.done_at else "") + (f"{e.title} ({e.course.name})" if e.course else e.title)
        lines += ["BEGIN:VEVENT", f"UID:mine-{e.id}@{host}", f"DTSTAMP:{stamp}", *span, f"SUMMARY:{_esc(summary)}"]
        if e.notes:
            lines.append(f"DESCRIPTION:{_esc(e.notes)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
