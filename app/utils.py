from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import nh3
from flask import abort, flash, redirect, url_for
from flask_login import current_user

from .extensions import db
from .models import ActivityLog, utcnow

# ---------------------------------------------------------------- time


def parse_ts(value) -> datetime | None:
    """ISO-8601 (as Canvas sends it) -> naive UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def user_zone(user=None) -> ZoneInfo:
    user = user or (current_user if current_user and current_user.is_authenticated else None)
    try:
        return ZoneInfo(getattr(user, "timezone", None) or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def to_local(dt: datetime | None, user=None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(user_zone(user))


def local_now(user=None) -> datetime:
    return to_local(utcnow(), user)


def fmt_dt(dt: datetime | None, style: str = "short") -> str:
    local = to_local(dt)
    if local is None:
        return "—"
    if style == "date":
        return local.strftime("%a, %b %-d")
    if style == "time":
        return local.strftime("%-I:%M %p")
    if style == "long":
        return local.strftime("%A, %B %-d, %Y at %-I:%M %p")
    return local.strftime("%a %b %-d, %-I:%M %p")


def relative(dt: datetime | None) -> str:
    if dt is None:
        return ""
    seconds = (dt - utcnow()).total_seconds()
    future = seconds > 0
    s = abs(seconds)
    if s < 60:
        text = "just now" if not future else "in under a minute"
        return text
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= size:
            n = int(s // size)
            label = f"{n} {unit}{'s' if n != 1 else ''}"
            return f"in {label}" if future else f"{label} ago"
    return ""


# ---------------------------------------------------------------- Canvas HTML

_ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "col", "colgroup", "dd", "div", "dl", "dt", "em",
    "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "s",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "*": {"class"},
}


def safe_canvas_html(raw: str | None, base_url: str | None = None) -> str:
    """Sanitize instructor-authored Canvas HTML for display inside our app.

    Relative links ("/courses/1/files/2") are made absolute to the student's Canvas so
    they open there instead of 404ing on our domain.
    """
    if not raw:
        return ""
    if base_url:
        raw = re.sub(
            r'(\s(?:href|src)=")(/[^"]*)"',
            lambda m: f'{m.group(1)}{urljoin(base_url, m.group(2))}"',
            raw,
        )
    return nh3.clean(
        raw,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )


_BLOCK_TAGS = re.compile(r"</?(p|div|br|li|tr|h[1-6]|table|ul|ol|section|article|blockquote|pre)[^>]*>", re.I)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BLOCK_TAGS.sub("\n", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- access


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def adult_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_adult:
            flash("This feature is only available to users 18 and older.", "warning")
            return redirect(url_for("main.dashboard"))
        return view(*args, **kwargs)

    return wrapper


def body_limit(max_bytes: int, message: str, json: bool = False):
    """Cap a route's request body at max_bytes. The app's `limit_request_body` hook enforces it
    before anything reads the body (CSRF reads forms before the view runs): a bigger body is
    answered without being read (JSON for `json` routes, else a flash and a redirect back), and
    a body sent without a Content-Length is cut off at the cap. There's no app-wide cap: file
    uploads send many files in one request."""

    def decorate(view):
        view.max_body = (max_bytes, message, json)
        return view

    return decorate


_ID = re.compile(r"[0-9]{1,18}")


def parse_id(value) -> int | None:
    """A database id from a form, a query string or JSON: plain ASCII digits only ("²" and "٣"
    pass str.isdigit() but aren't ids), else None."""
    if isinstance(value, bool):
        return None
    text = str(value).strip() if value is not None else ""
    return int(text) if _ID.fullmatch(text) else None


def log_activity(user_id: int, event: str, detail: str | None = None) -> None:
    db.session.add(ActivityLog(user_id=user_id, event=event, detail=(detail or "")[:500] or None))


def clamp(value, low, high):
    return max(low, min(high, value))


# Class colors: fills that read on paper and on ink, paired with an ink outline in the CSS.
CLASS_COLORS = ("#ffc629", "#ff8a65", "#7b9bff", "#3ddc8a", "#c3a6ff", "#5fd3e0", "#ff7eb6", "#b8e05a")


def course_color(course_id: int | None) -> str:
    return CLASS_COLORS[(course_id or 0) * 5 % len(CLASS_COLORS)]


def countdown(dt: datetime | None) -> tuple[str, str]:
    """("5h", "urgent") style label and urgency class for a due date."""
    if dt is None:
        return "", ""
    seconds = (dt - utcnow()).total_seconds()
    hours = abs(seconds) / 3600
    if seconds < 0:
        return (f"{max(1, round(hours))}h late" if hours < 36 else f"{round(hours / 24)}d late"), "late"
    if hours < 1:
        return f"{max(1, round(seconds / 60))}m", "urgent"
    if hours < 24:
        return f"{round(hours)}h", "urgent"
    if hours < 72:
        return f"{round(hours / 24)}d", "soon"
    return f"{round(hours / 24)}d", ""
