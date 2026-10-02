from __future__ import annotations

import json
import re
import secrets
from urllib.parse import urlsplit

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required, logout_user
from sqlalchemy import delete, func, select

from .. import queries
from ..extensions import db
from ..models import (ApiToken, ChatMessage, CoinTransaction, Course, Deck, DeckTest, LivePlayer, PracticeQuiz,
                      StudyPlan, SyncRun, TutorConversation, User, utcnow)
from ..services import gcal, integrations
from ..services.storage import get_storage
from .api import hash_token
from .auth import USERNAME_RE, valid_timezone

EXTENSION_ID_RE = re.compile(r"[a-p]{32}")

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route("/", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        f = request.form
        action = f.get("action", "profile")
        if action == "password":
            if not current_user.check_password(f.get("current", "")):
                flash("Your current password is wrong.", "error")
            elif len(f.get("new", "")) < 8:
                flash("Use at least 8 characters.", "error")
            else:
                current_user.set_password(f["new"])
                db.session.commit()
                flash("Password changed.", "success")
            return redirect(url_for("settings.profile"))
        username = f.get("username", current_user.username).strip()
        if username != current_user.username:
            taken = db.session.scalar(select(User.id).where(func.lower(User.username) == username.lower(),
                                                            User.id != current_user.id))
            if not USERNAME_RE.match(username) or taken:
                flash("That username isn't available.", "error")
                return redirect(url_for("settings.profile"))
            current_user.username = username
        from ..services import moderation

        try:
            current_user.display_name = moderation.clean_name(f.get("display_name") or current_user.display_name)[:80]
        except moderation.Rejected as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings.profile"))
        current_user.grade_level = (f.get("grade_level") or "").strip()[:40] or None
        current_user.timezone = valid_timezone(f.get("timezone"))
        current_user.show_on_leaderboards = bool(f.get("show_on_leaderboards"))
        year = (f.get("birth_year") or "").strip()
        if year.isdigit() and 1900 < int(year) <= utcnow().year - 13:
            current_user.birth_year = int(year)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings.profile"))
    return render_template("settings/profile.html")


# ---------------------------------------------------------------- Canvas connection


def server_url() -> str:
    return current_app.config.get("PUBLIC_URL") or request.host_url.rstrip("/")


@bp.route("/sync")
@login_required
def sync():
    tokens = db.session.scalars(select(ApiToken).where(ApiToken.user_id == current_user.id,
                                                       ApiToken.revoked.is_(False))
                                .order_by(ApiToken.created_at.desc())).all()
    runs = db.session.scalars(select(SyncRun).where(SyncRun.user_id == current_user.id)
                              .order_by(SyncRun.received_at.desc()).limit(10)).all()
    return render_template("settings/sync.html", tokens=tokens, runs=runs, accounts=queries.accounts(current_user.id),
                           server_url=server_url(), new_token=request.args.get("new_token_shown"),
                           courses=queries.visible_courses(current_user.id, include_hidden=True),
                           past_courses=_past_courses())


def _all_courses() -> list[Course]:
    return list(db.session.scalars(select(Course).where(Course.user_id == current_user.id).order_by(Course.name)))


def _past_courses() -> list[Course]:
    """Classes no longer active in Canvas (ended or dropped) that still hold stored files."""
    return [c for c in _all_courses() if not c.active and c.sync_files]


@bp.route("/files", methods=["POST"])
@login_required
def files():
    """The student chooses which classes' files are copied here; unticked classes lose their copies."""
    from ..services import ingest

    keep_all = request.form.get("mode") == "all"
    keep = {int(x) for x in request.form.getlist("keep") if x.isdigit()}
    current_user.keep_all_files = keep_all
    removed = 0
    for course in _all_courses():
        # Past classes aren't offered for keeping; they're only listed so they can be unticked.
        wanted = course.id in keep or (keep_all and course.active)
        if course.sync_files and not wanted:
            try:
                removed += ingest.forget_course_files(course)
            except Exception as exc:
                db.session.rollback()
                current_app.logger.error("could not delete files of course %s: %s", course.id, exc)
                flash("We couldn't delete some stored files just now, so nothing was changed. Try again in a few minutes.",
                      "error")
                return redirect(url_for("settings.sync") + "#files")
        course.sync_files = wanted
    db.session.commit()
    msg = "Saved. New files arrive with the next sync (use Sync now to start it)."
    if removed:
        msg += f" Deleted {removed} stored file{'s' if removed != 1 else ''} from classes you unticked."
    flash(msg, "success")
    return redirect(url_for("settings.sync") + "#files")


@bp.route("/tokens", methods=["POST"])
@login_required
def create_token():
    token = "hh_" + secrets.token_urlsafe(32)
    name = (request.form.get("name") or "Browser extension").strip()[:80]
    db.session.add(ApiToken(user_id=current_user.id, name=name, token_hash=hash_token(token), prefix=token[:10]))
    db.session.commit()
    # Shown exactly once; only the hash is stored.
    return render_template("settings/token_created.html", token=token, server_url=server_url())


EXTENSION_TOKEN_NAME = "Chrome extension (linked automatically)"


@bp.route("/extension-token", methods=["POST"])
@login_required
def extension_token():
    """A sync token the Connect Canvas page hands straight to the installed extension, so
    nobody copies a server address or token. The extension only accepts it from this site
    (externally_connectable) and syncs to the origin that sent it."""
    token = "hh_" + secrets.token_urlsafe(32)
    db.session.add(ApiToken(user_id=current_user.id, name=EXTENSION_TOKEN_NAME, token_hash=hash_token(token),
                            prefix=token[:10]))
    # A copy loaded from the zip has its own ID: remember it, so the page finds it on every address.
    ext = str((request.get_json(silent=True) or {}).get("ext") or "")
    if EXTENSION_ID_RE.fullmatch(ext) and ext not in current_app.config["EXTENSION_IDS"]:
        current_user.extension_ids = [ext] + [i for i in (current_user.extension_ids or []) if i != ext][:4]
    db.session.commit()
    return jsonify({"token": token})


@bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
@login_required
def revoke_token(token_id: int):
    token = db.session.get(ApiToken, token_id)
    if token is None or token.user_id != current_user.id:
        abort(404)
    token.revoked = True
    db.session.commit()
    flash("Token revoked. Any extension using it will stop syncing.", "info")
    return redirect(url_for("settings.sync"))


# ---------------------------------------------------------------- your data


@bp.route("/data")
@login_required
def data():
    return render_template("settings/data.html")


@bp.route("/data/export")
@login_required
def export():
    u = current_user
    courses = _all_courses()  # past classes too
    payload = {
        "exported_at": utcnow().isoformat() + "Z",
        "profile": {"email": u.email, "username": u.username, "display_name": u.display_name, "plan": u.plan,
                    "timezone": u.timezone, "grade_level": u.grade_level},
        "courses": [{
            "name": c.name, "code": c.course_code, "term": c.term_name, "current_score": c.current_score,
            "assignments": [{"name": a.name, "due_at": a.due_at.isoformat() if a.due_at else None, "status": a.status,
                             "score": a.score, "points_possible": a.points_possible} for a in c.assignments],
        } for c in courses],
        "decks": [{"title": d.title, "cards": [{"front": c.front, "back": c.back} for c in d.cards]}
                  for d in db.session.scalars(select(Deck).where(Deck.user_id == u.id))],
        "quizzes": [{"title": q.title, "questions": q.questions}
                    for q in db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.user_id == u.id))],
        "study_plans": [{
            "title": p.title, "kind": p.kind, "exam_at": p.exam_at.isoformat() if p.exam_at else None, "method": p.method,
            "pacing": p.pacing, "status": p.status, "scope": p.scope,
            "sessions": [{"day": x.day, "role": x.role, "minutes": x.minutes, "minutes_done": x.minutes_done,
                          "done": x.done_at is not None, "notes": x.notes} for x in p.sessions],
        } for p in db.session.scalars(select(StudyPlan).where(StudyPlan.user_id == u.id))],
        "practice_tests": [{"score": t.score, "total": t.total, "at": t.created_at.isoformat()}
                           for t in db.session.scalars(select(DeckTest).where(DeckTest.user_id == u.id))],
        "tutor": [{"title": t.title, "messages": [{"role": m.role, "content": m.content} for m in t.messages]}
                  for t in db.session.scalars(select(TutorConversation).where(TutorConversation.user_id == u.id))],
        "chat_messages": [{"body": m.body, "at": m.created_at.isoformat()}
                          for m in db.session.scalars(select(ChatMessage).where(ChatMessage.user_id == u.id))],
        "coins": [{"amount": t.amount, "reason": t.reason, "at": t.created_at.isoformat()}
                  for t in db.session.scalars(select(CoinTransaction).where(CoinTransaction.user_id == u.id))],
    }
    return Response(json.dumps(payload, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=homeworkhatch-export.json"})


@bp.route("/data/delete", methods=["POST"])
@login_required
def delete_account():
    if request.form.get("confirm") != current_user.username or not current_user.check_password(request.form.get("password", "")):
        flash("Type your username and password exactly to delete your account.", "error")
        return redirect(url_for("settings.data"))
    user = db.session.get(User, current_user.id)
    from ..services import billing

    # Files first: if storage can't be cleared, delete nothing, so we never keep files for an
    # account that no longer exists.
    try:
        get_storage().delete_prefix(f"u/{user.id}/")  # trailing slash: never touch u/{id}0...
    except Exception as exc:
        current_app.logger.error("storage cleanup failed for user %s: %s", user.id, exc)
        flash("We couldn't delete your stored files just now, so nothing was deleted. Try again in a few minutes.",
              "error")
        return redirect(url_for("settings.data"))
    try:
        billing.cancel_subscription(user)
    except Exception as exc:  # never keep an account the user asked to delete because Stripe hiccuped
        current_app.logger.error("could not cancel Stripe subscription for user %s: %s", user.id, exc)
    if integrations.available():
        for kind in integrations.LABELS:
            try:  # whatever our own row says: Composio may still hold a live connection
                integrations.disconnect(user, kind)  # revokes the Google tokens too
            except Exception as exc:
                current_app.logger.error("could not disconnect %s for user %s: %s", kind, user.id, exc)
    # Live-quiz seats in other people's games keep only a SET NULL link otherwise; remove them.
    db.session.execute(delete(LivePlayer).where(LivePlayer.user_id == user.id))
    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash("Your account and all of its data were deleted.", "info")
    return redirect(url_for("main.landing"))


# ---------------------------------------------------------------- Google (through Composio)


def _kind(kind: str) -> str:
    if kind not in integrations.TOOLKITS or not integrations.available():
        abort(404)
    return kind


def _safe_next(target: str | None) -> str | None:
    return target if target and target.startswith("/") and not target.startswith("//") else None


@bp.route("/integrations")
@login_required
def integrations_page():
    if not integrations.available():
        abort(404)
    return render_template("settings/integrations.html", calendar=integrations.get(current_user, "calendar"),
                           drive=integrations.get(current_user, "drive"))


@bp.route("/integrations/<kind>/connect")
@login_required
def integration_connect(kind: str):
    kind = _kind(kind)
    ref = urlsplit(request.referrer or "")
    back = ref.path + (f"?{ref.query}" if ref.query else "") if ref.netloc == request.host else None
    session["integration_next"] = _safe_next(request.args.get("next")) or _safe_next(back)
    try:
        url = integrations.connect_url(current_user, kind,
                                       url_for("settings.integration_callback", kind=kind, _external=True))
    except integrations.IntegrationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings.integrations_page"))
    if url is None:  # already connected
        return redirect(url_for("settings.integration_callback", kind=kind))
    return redirect(url)


@bp.route("/integrations/<kind>/callback")
@login_required
def integration_callback(kind: str):
    kind = _kind(kind)
    try:
        ok = integrations.refresh(current_user, kind)
    except integrations.IntegrationError as exc:
        flash(str(exc), "error")
        ok = False
    if ok:
        flash(f"{integrations.LABELS[kind]} connected.", "success")
        if kind == "calendar":
            row = integrations.get(current_user, "calendar")
            if "enabled" not in (row.settings or {}):  # first connection: turn syncing on
                row.settings = {**(row.settings or {}), "enabled": True}
                db.session.commit()
            gcal.kick(current_user.id)
    elif request.args.get("status") not in (None, "success"):
        flash(f"{integrations.LABELS[kind]} wasn't connected.", "error")
    return redirect(session.pop("integration_next", None) or url_for("settings.integrations_page"))


@bp.route("/integrations/<kind>/disconnect", methods=["POST"])
@login_required
def integration_disconnect(kind: str):
    kind = _kind(kind)
    try:
        if kind == "calendar" and gcal.enabled(current_user) and request.form.get("remove_events"):
            row = integrations.get(current_user, kind)
            row.connected = False
            row.settings = {**(row.settings or {}), "enabled": False}
            db.session.commit()
            gcal.remove_in_background(current_user.id, disconnect=True)
            flash("Google Calendar disconnected. Your upcoming due-date events are being removed.", "info")
        else:
            integrations.disconnect(current_user, kind)
            flash(f"{integrations.LABELS[kind]} disconnected.", "info")
    except integrations.IntegrationError as exc:
        flash(str(exc), "error")
    return redirect(_safe_next(request.form.get("next")) or url_for("settings.integrations_page"))


@bp.route("/integrations/calendar/toggle", methods=["POST"])
@login_required
def calendar_toggle():
    _kind("calendar")
    row = integrations.get(current_user, "calendar", create=True)
    turn_on = request.form.get("enabled") == "1"
    row.settings = {**(row.settings or {}), "enabled": turn_on}
    db.session.commit()
    if turn_on:
        if not row.connected:
            return redirect(url_for("settings.integration_connect", kind="calendar", next=request.form.get("next")))
        gcal.kick(current_user.id)
        flash("Adding your due dates to Google Calendar. This takes a minute the first time.", "success")
    else:
        if request.form.get("remove_events"):
            gcal.remove_in_background(current_user.id)
        flash("Stopped adding due dates to Google Calendar." +
              (" Your upcoming due-date events are being removed." if request.form.get("remove_events") else ""), "info")
    return redirect(_safe_next(request.form.get("next")) or url_for("settings.integrations_page"))


@bp.route("/integrations/calendar/sync", methods=["POST"])
@login_required
def calendar_sync_now():
    _kind("calendar")
    if not gcal.enabled(current_user):
        flash("Turn on Google Calendar first.", "error")
    else:
        gcal.kick(current_user.id)
        flash("Syncing your due dates to Google Calendar…", "info")
    return redirect(_safe_next(request.form.get("next")) or url_for("settings.integrations_page"))

