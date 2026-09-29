"""Homework Hatch: an AI study platform built on each student's own Canvas data."""

from __future__ import annotations

import logging
import os
from datetime import timedelta

import click
from dotenv import load_dotenv
from flask import Flask, render_template, request
from flask_login import current_user
from sqlalchemy import select

from .config import BASE_DIR, config_for
from .extensions import csrf, db, login_manager, migrate
from .models import User, utcnow

load_dotenv(BASE_DIR / ".env")


def create_app(env_name: str | None = None, overrides: dict | None = None) -> Flask:
    env_name = env_name or os.environ.get("HH_ENV", "development")
    app = Flask(__name__, instance_path=str(BASE_DIR / "instance"))
    app.config.from_object(config_for(env_name))
    if overrides:
        app.config.update(overrides)
    if env_name == "production" and app.config["SECRET_KEY"] == "dev-insecure-change-me":
        raise RuntimeError("Set SECRET_KEY in production.")
    os.makedirs(app.instance_path, exist_ok=True)
    logging.basicConfig(level=logging.INFO)

    # Behind Render/other proxies, trust X-Forwarded-* so url_for builds https URLs.
    if env_name == "production":
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)  # batch mode lets SQLite handle ALTERs
    login_manager.init_app(app)
    csrf.init_app(app)

    from .blueprints import (admin, api, auth, billing, chat, coins, college, courses, live, main, settings, study,
                             tools, tutor)

    for bp in (main.bp, auth.bp, courses.bp, api.bp, study.bp, live.bp, tutor.bp, chat.bp, coins.bp, college.bp,
               tools.bp, billing.bp, settings.bp, admin.bp):
        app.register_blueprint(bp)
    # Token-authenticated / signature-verified endpoints don't use browser CSRF tokens.
    csrf.exempt(api.bp)
    csrf.exempt(billing.webhook)

    _register_template_helpers(app)
    _register_hooks(app)
    _register_errors(app)
    _register_cli(app)
    return app


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


def _register_template_helpers(app: Flask) -> None:
    from .services import ai as ai_service
    from .services import coins as coin_service
    from .utils import fmt_dt, relative, safe_canvas_html, to_local

    app.jinja_env.filters["dt"] = fmt_dt
    app.jinja_env.filters["ago"] = relative
    app.jinja_env.filters["local"] = to_local
    app.jinja_env.filters["canvas_html"] = safe_canvas_html
    app.jinja_env.globals["now_utc"] = utcnow

    @app.context_processor
    def inject():
        ctx = {"ai_available": ai_service.available(), "app_name": "Homework Hatch",
               "feature_simulations": app.config["FEATURE_SIMULATIONS"]}
        if current_user.is_authenticated:
            ctx["coin_balance"] = coin_service.balance(current_user.id)
        return ctx


def _register_hooks(app: Flask) -> None:
    from .services import coins as coin_service
    from .utils import local_now

    @app.before_request
    def track_activity():
        if not current_user.is_authenticated or request.endpoint in (None, "static"):
            return
        now = utcnow()
        if current_user.last_seen_at and now - current_user.last_seen_at < timedelta(minutes=5):
            return
        current_user.last_seen_at = now
        today = local_now(current_user).date()
        today_s = today.isoformat()
        if current_user.last_active_date != today_s:
            yesterday = (today - timedelta(days=1)).isoformat()
            current_user.streak_days = (current_user.streak_days or 0) + 1 \
                if current_user.last_active_date == yesterday else 1
            current_user.last_active_date = today_s
            coin_service.award(current_user.id, 1, "Daily check-in", f"daily:{today_s}")
        db.session.commit()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        return response


def _register_errors(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("errors/error.html", code=403, message="You don't have access to that."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("errors/error.html", code=404, message="That page doesn't exist."), 404

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return render_template("errors/error.html", code=500, message="Something broke on our end."), 500


def _register_cli(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--username", prompt=True)
    @click.password_option()
    def create_admin(email, username, password):
        """Create an admin account (or promote an existing one with that email)."""
        user = db.session.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            user = User(email=email.lower(), username=username, display_name=username, accepted_terms_at=utcnow(),
                        onboarded=True)
            user.set_password(password)
            db.session.add(user)
        user.is_admin = True
        user.is_approved = True
        db.session.commit()
        click.echo(f"Admin ready: {user.email}")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Create the demo / demo12345 account filled with realistic sample Canvas data."""
        from .demo import seed

        user = db.session.scalar(select(User).where(User.username == "demo"))
        if user is None:
            user = User(email="demo@example.com", username="demo", display_name="Demo", accepted_terms_at=utcnow(),
                        onboarded=True, timezone="America/New_York")
            user.set_password("demo12345")
            db.session.add(user)
            db.session.commit()
        seed(user)
        click.echo("Demo account ready: demo / demo12345")

    @app.cli.command("init-db")
    def init_db():
        """Create tables directly (development shortcut; production uses `flask db upgrade`)."""
        db.create_all()
        click.echo("Tables created.")
