from __future__ import annotations

import re
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
        first_user = not db.session.scalar(select(User.id).limit(1))
        user = User(email=email, username=username, display_name=username,
                    timezone=valid_timezone(form.get("timezone")), accepted_terms_at=utcnow(),
                    # The very first account runs the place.
                    is_admin=first_user,
                    is_approved=first_user or not current_app.config["REQUIRE_APPROVAL"])
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
        if user is None or not user.check_password(request.form.get("password", "")):
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
