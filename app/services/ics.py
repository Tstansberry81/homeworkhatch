"""iCalendar feed of due dates and Canvas events, for Google/Apple/Outlook calendars."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import utcnow


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


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


def build(assignments, events, host: str) -> str:
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
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
