from __future__ import annotations

import calendar as cal
import functools
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from flask_login import current_user, login_required, logout_user
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .. import queries
from ..config import BASE_DIR
from ..extensions import csrf, db
from ..models import (Assignment, CalendarEvent, Course, StudyPlan, StudySession, User, UserEvent, calendar_token_hash,
                      utcnow)
from ..services import cards_io, feeds, ics, integrations, planner, split
from ..utils import lasting_url, local_now, log_activity, to_local, user_zone
from .auth import valid_timezone

bp = Blueprint("main", __name__)


@bp.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("landing.html")


@bp.route("/health")
def health():
    """Render's health check and the keep-alive ping (every 5 minutes): fast, and reports the deployed
    commit (no database call). It also starts the sweep that keeps calendar links fresh for students
    who don't open a page (feeds.tick: at most every 5 minutes, on its own thread; returns at once and
    never raises)."""
    from ..services import crypto

    feeds.tick(current_app._get_current_object())
    return {"ok": True, "commit": current_app.config.get("GIT_COMMIT"),
            "encryption": "on" if crypto.keyring() else "off"}


@bp.route("/health/db")
def health_db():
    """Confirms the database answers. Reports the backend, never credentials. The backup keep-alive
    (GitHub Actions) pings this one, so it starts the calendar-link sweep too (see health)."""
    from sqlalchemy import text

    feeds.tick(current_app._get_current_object())
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:  # report, don't crash the probe
        current_app.logger.error("database health check failed: %s", exc)
        return {"ok": False, "database": db.engine.dialect.name}, 503
    return {"ok": True, "database": db.engine.dialect.name}


@bp.route("/terms")
def terms():
    return render_template("legal/terms.html")


@bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")


# The page only shows 50 cards; the browser sends the start of a longer paste (import.js).
FREE_PREVIEW_MAX_BYTES = 30 * 1024


@bp.route("/free-learn")
def free_learn():
    """Public page: Learn mode free, with your own Quizlet or Anki sets (paste, preview, sign up)."""
    if current_user.is_authenticated:
        return redirect(url_for("study.import_deck"))
    return render_template("free_learn.html", preview_max=FREE_PREVIEW_MAX_BYTES)


@bp.route("/free-learn/preview", methods=["POST"])
@csrf.exempt  # read-only: parses the posted text and answers; no session, account or storage involved
def free_learn_preview():
    """Parse pasted cards for the public page. Nothing is stored; at most 30 KB in, 50 cards out.
    JSON only: another site's form (or a "simple" cross-site fetch) can't send that type."""
    if (request.content_length or 0) > FREE_PREVIEW_MAX_BYTES:
        return jsonify({"error": "That's a big set! Sign up to import all of it."}), 413
    if not request.is_json:
        return jsonify({"error": "Send JSON with a text field."}), 415
    raw = request.stream.read(FREE_PREVIEW_MAX_BYTES + 1)
    if len(raw) > FREE_PREVIEW_MAX_BYTES:
        return jsonify({"error": "That's a big set! Sign up to import all of it."}), 413
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return jsonify({"error": "Send JSON with a text field."}), 400
    if not isinstance(data, dict):
        return jsonify({"error": "Send JSON with a text field."}), 400
    return jsonify(cards_io.preview(data, show=50))


@bp.route("/support")
def support():
    return render_template("legal/support.html")


@bp.route("/copyright")
def copyright():
    return render_template("legal/copyright.html")


@bp.route("/age", methods=["GET", "POST"])
@login_required
def age():
    """The sign-up age check, for accounts created before sign-up asked for it."""
    from .auth import MIN_AGE, _age

    if current_user.birth_year is not None:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        years = _age(request.form.get("birth_month"), request.form.get("birth_year"))
        if years is None:
            flash("Enter your birth month and year.", "error")
            return render_template("age.html"), 400
        if years < MIN_AGE:
            current_user.active = False  # an admin reviews and deletes the account
            log_activity(current_user.id, "age_blocked")
            db.session.commit()
            logout_user()
            return render_template("age.html", blocked=True), 403
        current_user.birth_year = int(request.form["birth_year"])
        current_user.birth_month = int(request.form["birth_month"])
        db.session.commit()
        nxt = request.args.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.dashboard"))
    return render_template("age.html")


@bp.route("/welcome", methods=["GET", "POST"])
@login_required
def onboarding():
    if request.method == "POST":
        f = request.form
        from ..services import moderation

        try:
            current_user.display_name = moderation.clean_name(f.get("display_name") or current_user.username)[:80]
        except moderation.Rejected as exc:
            flash(str(exc), "error")
            return render_template("onboarding.html"), 400
        current_user.grade_level = (f.get("grade_level") or "").strip()[:40] or None
        current_user.timezone = valid_timezone(f.get("timezone"))
        current_user.onboarded = True
        db.session.commit()
        shared_next = session.pop("shared_next", None)  # signed up from a shared set's link
        if isinstance(shared_next, str) and shared_next.startswith("/s/") and "//" not in shared_next:
            return redirect(shared_next)
        return redirect(url_for("settings.sync"))
    return render_template("onboarding.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    if not current_user.onboarded:
        return redirect(url_for("main.onboarding"))
    feeds.refresh_due(current_user)  # calendar links older than an hour (in the background)
    planner.refresh(current_user)
    courses = queries.visible_courses(current_user.id)
    upcoming = queries.upcoming(current_user.id)
    today = local_now(current_user).date()
    by_day: dict[date, list] = {}
    for a in upcoming:
        by_day.setdefault(to_local(a.due_at).date(), []).append(a)
    week = [{"date": today + timedelta(days=i), "items": by_day.get(today + timedelta(days=i), [])} for i in range(7)]
    agenda = []
    overdue = [a for d, items in by_day.items() if d < today for a in items]
    if overdue:
        agenda.append(("Overdue", overdue))
    for d in sorted(k for k in by_day if k >= today):
        label = "Today" if d == today else "Tomorrow" if d == today + timedelta(days=1) else d.strftime("%a · %b %-d")
        agenda.append((label, by_day[d]))
    accounts = queries.accounts(current_user.id)
    # The pill tracks the extension's Canvas sync: an hourly calendar link must not hide a stale
    # one. Calendar links count only for students who have nothing else.
    canvas = [a for a in accounts if a.lms != "ics"]
    pill = (canvas or accounts)[0] if accounts else None
    last_sync = pill.last_sync_at if pill else None
    soon = [p for p in planner.active_plans(current_user.id) if p.exam_at and p.exam_at <= utcnow() + timedelta(days=21)]
    today_iso = today.isoformat()
    zone = user_zone(current_user)
    day_start = datetime.combine(today, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    planned_today = [e for e in db.session.scalars(select(UserEvent).options(selectinload(UserEvent.course)).where(
        UserEvent.user_id == current_user.id, UserEvent.start_at >= day_start - timedelta(days=1),
        UserEvent.start_at < day_start + timedelta(days=2)).order_by(UserEvent.start_at))
        if (e.all_day_date if e.all_day and e.all_day_date else to_local(e.start_at).date()) == today]
    table = split.points_on_the_table(current_user.id)
    return render_template(
        "dashboard.html", week=week, agenda=agenda, upcoming=upcoming, today=local_now(current_user),
        table=table, table_ids=[f.assignment.id for _c, f in table],
        exam_plans=soon, planned_today=planned_today, study_today={p.id: [s for s in p.sessions if s.day == today_iso and p.exam_at > utcnow()]
                                      for p in soon},
        roles=planner.ROLES,
        missing=queries.missing(current_user.id), classes=queries.class_rows(courses),
        announcements=queries.recent_announcements(current_user.id), accounts=accounts, pill=pill,
        stale=last_sync is None or (utcnow() - last_sync) > timedelta(hours=3),
        files_undecided=sum(1 for c in courses if c.sync_files is None and c.account.lms != "ics"),
    )


@bp.route("/account")
@login_required
def account():
    return redirect(url_for("settings.profile"))


@bp.route("/assignments/<int:assignment_id>/done", methods=["POST"])
@login_required
def toggle_done(assignment_id: int):
    a = db.session.get(Assignment, assignment_id)
    if a is None or a.course.user_id != current_user.id:
        abort(404)
    a.user_done = not a.user_done
    db.session.commit()
    if request.accept_mimetypes.best == "application/json":
        return {"done": a.user_done, "status": a.effective_status}
    return redirect(request.referrer or url_for("main.dashboard"))


# ---------------------------------------------------------------- calendar


CALENDAR_VIEWS = ("month", "week", "day")
DONE_STATUSES = {"done", "submitted", "submitted_late", "graded"}


def _calendar_anchor(today: date) -> date:
    """The day the calendar is showing: ?d=YYYY-MM-DD, or the older ?y=&m= month links, else today."""
    try:
        if request.args.get("d"):
            anchor = date.fromisoformat(request.args["d"])
        elif request.args.get("y") or request.args.get("m"):
            anchor = date(int(request.args.get("y", today.year)), int(request.args.get("m", today.month)), 1)
        else:
            return today
    except (TypeError, ValueError, OverflowError):
        return today
    return anchor if 1970 <= anchor.year <= 2100 else today


@dataclass
class DayEvent:
    """A Canvas event as one day shows it: multi-day events appear on every day they cover."""

    event: CalendarEvent
    short: str  # "All day", "9:00 AM", "Until 5:00 PM"
    long: str  # with the end time: "9:00 AM – 5:00 PM", "9:00 AM – Thu 5:00 PM"

    def __getattr__(self, name):
        return getattr(self.event, name)


def _event_days(e: CalendarEvent, zone) -> list[tuple[date, datetime, str, str]]:
    """(local day, sort time in UTC, short label, long label) for each day the event covers."""
    def utc_midnight(d):
        return datetime.combine(d, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)

    if e.all_day and e.all_day_date:  # Canvas's own all-day events are a date, not a moment
        return [(e.all_day_date, utc_midnight(e.all_day_date), "All day", "All day")]
    start = to_local(e.start_at).astimezone(zone)
    end = to_local(e.end_at).astimezone(zone) if e.end_at and e.end_at > e.start_at else None
    if start.time() == time.min and (end is None or (end.time() == time.min and end.date() > start.date())):
        last = end.date() - timedelta(days=1) if end else start.date()  # midnight to midnight: all day
        return [(start.date() + timedelta(days=i), utc_midnight(start.date() + timedelta(days=i)), "All day", "All day")
                for i in range((last - start.date()).days + 1)]
    t = lambda d: d.strftime("%-I:%M %p")  # noqa: E731
    if end is None or end.date() == start.date():
        return [(start.date(), e.start_at, t(start), f"{t(start)} – {t(end)}" if end else t(start))]
    last = end.date() - timedelta(days=1) if end.time() == time.min else end.date()
    days = [(start.date(), e.start_at, t(start), f"{t(start)} – {end:%a} {t(end)}")]
    for i in range(1, (last - start.date()).days + 1):
        d = start.date() + timedelta(days=i)
        until = f"Until {t(end)}" if d == end.date() else "All day"
        days.append((d, utc_midnight(d), until, until))
    return days


def _calendar_items(first: date, last: date) -> dict[date, list]:
    """Assignments due, Canvas events, planned study sessions and the student's own items on each local
    day from first to last, in time order."""
    course_ids = [c.id for c in queries.visible_courses(current_user.id)]
    zone = user_zone(current_user)
    # Stored times are UTC; a day of padding each side covers every time zone, then each item is
    # filed under its local date.
    lo = datetime.combine(first - timedelta(days=1), time.min)
    hi = datetime.combine(last + timedelta(days=2), time.min)
    by_day: dict[date, list] = {}

    def add(day, kind, item, when):
        if first <= day <= last:
            by_day.setdefault(day, []).append((kind, item, when))

    if course_ids:
        for a in db.session.scalars(select(Assignment).options(selectinload(Assignment.course)).where(
                Assignment.course_id.in_(course_ids), Assignment.due_at >= lo, Assignment.due_at < hi)):
            add(to_local(a.due_at).date(), "assignment", a, a.due_at)
    # Events that overlap the window, including ones that started before it and are still going.
    for e in db.session.scalars(select(CalendarEvent).where(
            CalendarEvent.user_id == current_user.id, CalendarEvent.start_at < hi,
            func.coalesce(CalendarEvent.end_at, CalendarEvent.start_at) >= lo)):
        for day, when, short, long in _event_days(e, zone):
            add(day, "event", DayEvent(e, short, long), when)
    # The student's own items (from the tutor or added by hand), laid out like events.
    for e in db.session.scalars(select(UserEvent).options(selectinload(UserEvent.course)).where(
            UserEvent.user_id == current_user.id, UserEvent.start_at < hi,
            func.coalesce(UserEvent.end_at, UserEvent.start_at) >= lo)):
        for day, when, short, long in _event_days(e, zone):
            add(day, "mine", DayEvent(e, short, long), when)
    # Planned study sessions, first thing on their day.
    for s in db.session.scalars(select(StudySession).join(StudyPlan).options(selectinload(StudySession.plan)).where(
            StudySession.user_id == current_user.id, StudyPlan.status == "active",
            StudySession.day >= first.isoformat(), StudySession.day <= last.isoformat())):
        day = date.fromisoformat(s.day)
        start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        add(day, "study", s, start - timedelta(seconds=1) + timedelta(microseconds=s.position))
    return {d: [(kind, item) for kind, item, _ in sorted(items, key=lambda x: x[2])] for d, items in by_day.items()}


@bp.route("/calendar")
@login_required
def calendar_view():
    feeds.refresh_due(current_user)
    today = local_now(current_user).date()
    view = request.args.get("view")
    if view in CALENDAR_VIEWS:
        session["calendar_view"] = view
    elif not request.args.get("d") and (request.args.get("y") or request.args.get("m")):
        view = "month"  # an old month link names a month
    else:  # the view you used last
        view = session.get("calendar_view") if session.get("calendar_view") in CALENDAR_VIEWS else "month"
    anchor = _calendar_anchor(today)
    if view == "month":
        first = anchor.replace(day=1)
        weeks = cal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
        prev, nxt = (first - timedelta(days=1)).replace(day=1), (first + timedelta(days=32)).replace(day=1)
        title = first.strftime("%B %Y")
    elif view == "week":
        first = anchor - timedelta(days=(anchor.weekday() + 1) % 7)  # weeks start on Sunday, like the month grid
        weeks = [[first + timedelta(days=i) for i in range(7)]]
        prev, nxt = first - timedelta(days=7), first + timedelta(days=7)
        last = weeks[0][-1]
        title = (f"{first:%b %-d} – {last:%-d, %Y}" if first.month == last.month
                 else f"{first:%b %-d} – {last:%b %-d, %Y}" if first.year == last.year
                 else f"{first:%b %-d, %Y} – {last:%b %-d, %Y}")
    else:
        first = anchor
        # The day's week too, for the strip of days above the list.
        sunday = anchor - timedelta(days=(anchor.weekday() + 1) % 7)
        weeks = [[sunday + timedelta(days=i) for i in range(7)]]
        prev, nxt = anchor - timedelta(days=1), anchor + timedelta(days=1)
        title = anchor.strftime("%A, %B %-d") + ("" if anchor.year == today.year else anchor.strftime(", %Y"))
    by_day = _calendar_items(weeks[0][0], weeks[-1][-1])
    # The counts cover what the view is about: the day, the week, or the month without its neighbours' days.
    shown = [anchor] if view == "day" else [d for week in weeks for d in week if view == "week" or d.month == first.month]
    assignments = [item for d in shown for kind, item in by_day.get(d, []) if kind == "assignment"]
    feed_url = lasting_url("main.ics_feed", token=current_user.calendar_token)
    return render_template("calendar.html", view=view, title=title, weeks=weeks, anchor=anchor, first=first,
                           today=today, by_day=by_day, prev=prev, nxt=nxt,
                           to_do=sum(1 for a in assignments if not a.excused and queries.still_to_do(a, utcnow())),
                           done=sum(1 for a in assignments if a.effective_status in DONE_STATUSES),
                           feed_url=feed_url, study_roles=planner.ROLES,
                           gcal=integrations.get(current_user, "calendar") if integrations.available() else None)


@bp.route("/calendar/<token>.ics")
def ics_feed(token: str):
    user = db.session.scalar(select(User).where(User.calendar_token_hash == calendar_token_hash(token)))
    if user is None or not user.active:
        abort(404)
    course_ids = [c.id for c in queries.visible_courses(user.id)]
    since = utcnow() - timedelta(days=60)
    assignments = db.session.scalars(select(Assignment).options(selectinload(Assignment.course)).where(Assignment.course_id.in_(course_ids),
                                                              Assignment.due_at >= since)).all() if course_ids else []
    events = db.session.scalars(select(CalendarEvent).where(CalendarEvent.user_id == user.id,
                                                            CalendarEvent.start_at >= since)).all()
    mine = db.session.scalars(select(UserEvent).options(selectinload(UserEvent.course)).where(
        UserEvent.user_id == user.id, UserEvent.start_at >= since)).all()
    body = ics.build(assignments, events, request.host, mine)
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "inline; filename=homeworkhatch.ics"})


@bp.route("/calendar/reset-feed", methods=["POST"])
@login_required
def reset_feed():
    import secrets

    current_user.calendar_token = secrets.token_urlsafe(24)
    db.session.commit()
    flash("Calendar link reset. Re-subscribe with the new link; the old one stopped working.", "info")
    return redirect(url_for("main.calendar_view"))


# ---------------------------------------------------------------- extension download


@bp.route("/extension.zip")
@login_required
def extension_zip():
    root = BASE_DIR / "extension"
    if not (root / "manifest.json").exists():
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if path.is_file() and not any(p in {"node_modules", "tests", ".chrome"} for p in rel.parts) \
                    and rel.name not in {"package.json", "package-lock.json"} and rel.suffix != ".md":
                z.write(path, rel.as_posix())  # flat: "Extract All" gives a folder with manifest.json in it
    buf.seek(0)
    current_app.logger.info("extension download by user %s", current_user.id)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="homework-hatch-extension.zip")


@bp.app_template_global()
def course_by_id(course_id: int) -> Course | None:
    return db.session.get(Course, course_id)


@bp.app_template_global()
@functools.cache
def timezones() -> list[str]:
    from zoneinfo import available_timezones

    return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
