from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlparse
from zoneinfo import available_timezones

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, select

from ..extensions import db
from ..models import User, utcnow
from ..utils import log_activity

bp = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIMEZONES = None


def valid_timezone(tz: str | None) -> str:
    global _TIMEZONES
    if _TIMEZONES is None:
        _TIMEZONES = available_timezones()
    return tz if tz in _TIMEZONES else "UTC"


MAX_FAILED_LOGINS = 10
LOCKOUT = timedelta(minutes=15)


def _recent_failures(user_id: int) -> int:
    from ..models import ActivityLog

    since = utcnow() - LOCKOUT
    last_ok = db.session.scalar(select(func.max(ActivityLog.created_at)).where(
        ActivityLog.user_id == user_id, ActivityLog.event == "login"))
    if last_ok and last_ok > since:
        since = last_ok
    return db.session.scalar(select(func.count(ActivityLog.id)).where(
        ActivityLog.user_id == user_id, ActivityLog.event == "login_failed", ActivityLog.created_at >= since)) or 0


def _safe_next(target: str | None) -> str | None:
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = request.form
    if request.method == "POST":
        email = form.get("email", "").strip().lower()
        username = form.get("username", "").strip()
        password = form.get("password", "")
        errors = []
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if not USERNAME_RE.match(username):
            errors.append("Usernames are 3–30 letters, numbers, dots, dashes or underscores.")
        if len(password) < 8:
            errors.append("Use a password of at least 8 characters.")
        if not form.get("terms"):
            errors.append("You need to accept the Terms of Service.")
        if db.session.scalar(select(User.id).where(User.email == email)):
            errors.append("An account with that email already exists.")
        if db.session.scalar(select(User.id).where(func.lower(User.username) == username.lower())):
            errors.append("That username is taken.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("auth/register.html", form=form), 400
        # Admin rights: in production only for ADMIN_EMAIL (or via `flask create-admin`), so a
        # stranger can't claim a fresh public deploy by registering first. Locally, the first
        # account is the admin for convenience.
        admin_email = (current_app.config.get("ADMIN_EMAIL") or "").strip().lower()
        if current_app.config["ENV_NAME"] == "production":
            make_admin = bool(admin_email) and email == admin_email
        else:
            make_admin = not db.session.scalar(select(User.id).limit(1)) or (bool(admin_email) and email == admin_email)
        user = User(email=email, username=username, display_name=username,
                    timezone=valid_timezone(form.get("timezone")), accepted_terms_at=utcnow(),
                    is_admin=make_admin,
                    is_approved=make_admin or not current_app.config["REQUIRE_APPROVAL"])
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        log_activity(user.id, "register")
        db.session.commit()
        if not user.is_approved:
            return render_template("auth/pending.html")
        login_user(user, remember=True)
        return redirect(url_for("main.onboarding"))
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip()
        user = db.session.scalar(select(User).where(
            (User.email == ident.lower()) | (func.lower(User.username) == ident.lower())))
        if user is not None and _recent_failures(user.id) >= MAX_FAILED_LOGINS:
            flash("Too many failed sign-ins. Wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", identifier=ident), 429
        if user is None or not user.check_password(request.form.get("password", "")):
            if user is not None:
                log_activity(user.id, "login_failed", request.remote_addr)
                db.session.commit()
            flash("Wrong email/username or password.", "error")
            return render_template("auth/login.html", identifier=ident), 401
        if not user.active:
            flash("This account has been deactivated.", "error")
            return render_template("auth/login.html", identifier=ident), 403
        if not user.is_approved:
            return render_template("auth/pending.html"), 403
        login_user(user, remember=bool(request.form.get("remember")))
        log_activity(user.id, "login")
        db.session.commit()
        return redirect(_safe_next(request.args.get("next")) or url_for("main.dashboard"))
    return render_template("auth/login.html", identifier="")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("main.landing"))
