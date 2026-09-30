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

from .config import BASE_DIR, load_config, validate_production
from .extensions import csrf, db, login_manager, migrate
from .models import User, utcnow

load_dotenv(BASE_DIR / ".env")


def create_app(env_name: str | None = None, overrides: dict | None = None) -> Flask:
    env_name = env_name or os.environ.get("HH_ENV", "development")
    app = Flask(__name__, instance_path=str(BASE_DIR / "instance"))
    app.config.update(load_config(env_name))
    if overrides:
        app.config.update(overrides)
    # Hard cap on any request body (also enforced for chunked uploads with no Content-Length).
    app.config.setdefault("MAX_CONTENT_LENGTH",
                          (max(app.config["MAX_FILE_MB"], app.config["MAX_SNAPSHOT_MB"]) + 1) * 1024 * 1024)
    if env_name == "production":
        problems = validate_production(app.config)
        if problems and os.environ.get("HH_ALLOW_UNSAFE_CONFIG") != "1":
            raise RuntimeError("Unsafe production config:\n- " + "\n- ".join(problems))
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

    from .blueprints import (admin, api, auth, billing, chat, coins, courses, live, main, settings, study, tools, tutor,
                             uploads)

    for bp in (main.bp, auth.bp, courses.bp, api.bp, study.bp, live.bp, tutor.bp, chat.bp, coins.bp, tools.bp,
               billing.bp, settings.bp, admin.bp, uploads.bp):
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
    # "<id>:<session version>"; a password change bumps the version and ends old sessions.
    ident, _, version = str(user_id).partition(":")
    if not ident.isdigit():
        return None
    user = db.session.get(User, int(ident))
    if user is None or str(user.session_version or 0) != (version or "0"):
        return None
    return user


def _register_template_helpers(app: Flask) -> None:
    from .services import ai as ai_service
    from .services import integrations as integration_service
    from .services import coins as coin_service
    from .utils import countdown, course_color, fmt_dt, relative, safe_canvas_html, to_local

    app.jinja_env.filters["dt"] = fmt_dt
    app.jinja_env.filters["ago"] = relative
    app.jinja_env.filters["local"] = to_local
    app.jinja_env.filters["canvas_html"] = safe_canvas_html
    app.jinja_env.filters["countdown"] = countdown
    app.jinja_env.globals["course_color"] = course_color
    from .services.study import render_markdown
    app.jinja_env.filters["md"] = render_markdown  # AI text: `code`, **bold**, lists; math stays for KaTeX
    app.jinja_env.globals["now_utc"] = utcnow

    @app.context_processor
    def inject():
        ctx = {"ai_available": ai_service.available(), "app_name": "Homework Hatch",
               "feature_simulations": app.config["FEATURE_SIMULATIONS"],
               "integrations_available": integration_service.available()}
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
                        onboarded=True, timezone="America/New_York", keep_all_files=True)  # synthetic files only
            user.set_password("demo12345")
            db.session.add(user)
            db.session.commit()
        seed(user)
        click.echo("Demo account ready: demo / demo12345")

    @app.cli.command("check-deploy")
    def check_deploy():
        """Verify a deployment end to end: config, database + migrations, file storage, AI."""
        import sys
        import uuid

        import requests
        from sqlalchemy import text

        from .services import ai as ai_service
        from .services.storage import get_storage

        failures = 0

        def report(ok: bool, label: str, detail: str = ""):
            nonlocal failures
            failures += 0 if ok else 1
            click.echo(f"{'✓' if ok else '✗'} {label}{': ' + detail if detail else ''}")

        problems = validate_production(app.config)
        report(not problems, "production config", "; ".join(problems) or "ok")
        try:
            db.session.execute(text("SELECT 1"))
            report(True, "database", db.engine.dialect.name)
            from alembic.migration import MigrationContext
            from alembic.script import ScriptDirectory
            from flask_migrate import Migrate  # noqa: F401  (ensures extension config is loaded)

            script = ScriptDirectory.from_config(app.extensions["migrate"].migrate.get_config())
            with db.engine.connect() as conn:
                current = MigrationContext.configure(conn).get_current_revision()
            head = script.get_current_head()
            report(current == head, "migrations", f"at {current}, head {head}")
            if db.engine.dialect.name == "postgresql":
                exposed = db.session.execute(text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND NOT rowsecurity")).scalar()
                report(exposed == 0, "row level security", f"{exposed} public tables without RLS")
        except Exception as exc:
            report(False, "database", str(exc).splitlines()[0])
        try:
            st = get_storage()
            key = f"healthcheck/{uuid.uuid4().hex}/probe.txt"
            st.put_bytes(key, b"homework hatch storage probe", "text/plain")
            round_trip = st.read(key) == b"homework hatch storage probe"
            url = st.signed_url(key, "probe.txt", "text/plain", True, 60)
            signed_ok = url is None or requests.get(url, timeout=15).content == b"homework hatch storage probe"
            st.delete_prefix(key)
            report(round_trip and signed_ok, "file storage", f"{app.config['STORAGE_BACKEND']} "
                   f"(write/read {'ok' if round_trip else 'FAILED'}, signed URL {'ok' if signed_ok else 'FAILED'})")
        except Exception as exc:
            report(False, "file storage", str(exc).splitlines()[0])
        if ai_service.available():
            report(True, "AI", app.config["AI_MODEL"])
        else:
            click.echo("! AI: off (no ANTHROPIC_API_KEY) — AI features are hidden until it's set")
        sys.exit(1 if failures else 0)

    @app.cli.command("init-db")
    def init_db():
        """Create tables directly (development shortcut; production uses `flask db upgrade`)."""
        db.create_all()
        click.echo("Tables created.")
