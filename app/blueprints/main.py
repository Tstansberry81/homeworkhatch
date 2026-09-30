from __future__ import annotations

import calendar as cal
import io
import zipfile
from datetime import date, timedelta

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import queries
from ..config import BASE_DIR
from ..extensions import db
from ..models import Assignment, CalendarEvent, Course, User, utcnow
from ..services import ics, planner
from ..utils import local_now, to_local
from .auth import valid_timezone

bp = Blueprint("main", __name__)


@bp.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("landing.html")


@bp.route("/health")
def health():
    """Render's health check: fast, and reports the deployed commit (no database call)."""
    return {"ok": True, "commit": current_app.config.get("GIT_COMMIT")}


@bp.route("/health/db")
def health_db():
    """Confirms the database answers. Reports the backend, never credentials."""
    from sqlalchemy import text

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
        year = f.get("birth_year", "").strip()
        if year:
            if not year.isdigit() or not 1900 < int(year) <= utcnow().year - 13:
                flash("Homework Hatch is for students 13 and older.", "error")
                return render_template("onboarding.html"), 400
            current_user.birth_year = int(year)
        current_user.onboarded = True
        db.session.commit()
        return redirect(url_for("settings.sync"))
    return render_template("onboarding.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    if not current_user.onboarded:
        return redirect(url_for("main.onboarding"))
    courses = queries.visible_courses(current_user.id)
    return render_template(
        "dashboard.html",
        upcoming=queries.upcoming(current_user.id),
        missing=queries.missing(current_user.id),
        classes=queries.class_rows(courses),
        announcements=queries.recent_announcements(current_user.id),
        accounts=queries.accounts(current_user.id),
        due_cards=queries.due_card_count(current_user.id),
        now=utcnow(),
    )


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


@bp.route("/calendar")
@login_required
def calendar_view():
    today = local_now(current_user).date()
    try:
        year = int(request.args.get("y", today.year))
        month = int(request.args.get("m", today.month))
        if not 1970 <= year <= 2100:
            raise ValueError("year out of range")
        first = date(year, month, 1)
    except ValueError:
        first = today.replace(day=1)
    weeks = cal.Calendar(firstweekday=6).monthdatescalendar(first.year, first.month)
    course_ids = [c.id for c in queries.visible_courses(current_user.id)]
    start, end = weeks[0][0] - timedelta(days=1), weeks[-1][-1] + timedelta(days=2)
    from datetime import datetime

    lo, hi = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())
    by_day: dict[date, list] = {}
    if course_ids:
        for a in db.session.scalars(select(Assignment).where(Assignment.course_id.in_(course_ids),
                                                             Assignment.due_at >= lo, Assignment.due_at < hi)):
            by_day.setdefault(to_local(a.due_at).date(), []).append(("assignment", a))
    for e in db.session.scalars(select(CalendarEvent).where(CalendarEvent.user_id == current_user.id,
                                                            CalendarEvent.start_at >= lo, CalendarEvent.start_at < hi)):
        by_day.setdefault(to_local(e.start_at).date(), []).append(("event", e))
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    feed_url = url_for("main.ics_feed", token=current_user.calendar_token, _external=True)
    return render_template("calendar.html", weeks=weeks, first=first, today=today, by_day=by_day,
                           prev_month=prev_month, next_month=next_month, feed_url=feed_url)


@bp.route("/calendar/<token>.ics")
def ics_feed(token: str):
    user = db.session.scalar(select(User).where(User.calendar_token == token))
    if user is None or not user.active:
        abort(404)
    course_ids = [c.id for c in queries.visible_courses(user.id)]
    since = utcnow() - timedelta(days=60)
    assignments = db.session.scalars(select(Assignment).where(Assignment.course_id.in_(course_ids),
                                                              Assignment.due_at >= since)).all() if course_ids else []
    events = db.session.scalars(select(CalendarEvent).where(CalendarEvent.user_id == user.id,
                                                            CalendarEvent.start_at >= since)).all()
    body = ics.build(assignments, events, request.host)
    return Response(body, mimetype="text/calendar", headers={"Content-Disposition": "inline; filename=homeworkhatch.ics"})


@bp.route("/calendar/reset-feed", methods=["POST"])
@login_required
def reset_feed():
    import secrets

    current_user.calendar_token = secrets.token_urlsafe(24)
    db.session.commit()
    flash("Calendar link reset. Re-subscribe with the new link; the old one stopped working.", "info")
    return redirect(url_for("main.calendar_view"))


# ---------------------------------------------------------------- planner


@bp.route("/planner", methods=["GET", "POST"])
@login_required
def study_planner():
    if request.method == "POST":
        try:
            minutes = int(request.form.get("minutes", current_user.study_minutes_per_day))
        except ValueError:
            minutes = current_user.study_minutes_per_day
        current_user.study_minutes_per_day = max(15, min(minutes, 720))
        db.session.commit()
        return redirect(url_for("main.study_planner"))
    work = queries.upcoming(current_user.id, days=planner.HORIZON_DAYS)
    plan = planner.build_plan(work, local_now(current_user).date(), current_user.study_minutes_per_day, current_user)
    return render_template("planner.html", plan=plan, today=local_now(current_user).date())


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
                z.write(path, f"homework-hatch-extension/{rel}")
    buf.seek(0)
    current_app.logger.info("extension download by user %s", current_user.id)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="homework-hatch-extension.zip")


@bp.app_template_global()
def course_by_id(course_id: int) -> Course | None:
    return db.session.get(Course, course_id)


@bp.app_template_global()
def timezones() -> list[str]:
    from zoneinfo import available_timezones

    return sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/", "posix/", "right/")))
