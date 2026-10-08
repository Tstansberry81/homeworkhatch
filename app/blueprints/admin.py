from __future__ import annotations

import json
import secrets
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import (ActivityLog, AIUsage, CalendarFeed, ChatMessage, ChatReport, CoinTransaction, Course, Deck, DirectReport,
                      DirectThread, LmsDiagnostic, PracticeQuiz, ShareReport, SyncRun, User, utcnow)
from ..services import ai, billing, coins, diagnostics, dms, sharing
from ..utils import admin_required, log_activity, parse_id

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@login_required
@admin_required
def guard():
    return None


@bp.route("/")
def overview():
    now = utcnow()
    stats = {
        "users": db.session.scalar(select(func.count(User.id))),
        "active_7d": db.session.scalar(select(func.count(User.id)).where(User.last_seen_at >= now - timedelta(days=7))),
        "pending": db.session.scalar(select(func.count(User.id)).where(User.is_approved.is_(False))),
        "syncs_24h": db.session.scalar(select(func.count(SyncRun.id)).where(SyncRun.received_at >= now - timedelta(days=1))),
        "courses": db.session.scalar(select(func.count(Course.id)).where(Course.active.is_(True))),
        "ai_month": db.session.scalar(select(func.count(AIUsage.id)).where(AIUsage.created_at >= ai.month_start())),
        "spend_month": db.session.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0))
                                         .where(AIUsage.created_at >= ai.month_start())),
        "coins": db.session.scalar(select(func.coalesce(func.sum(CoinTransaction.amount), 0))),
        "open_reports": (db.session.scalar(select(func.count(ChatReport.id)).where(ChatReport.resolved.is_(False))) or 0)
                        + (db.session.scalar(select(func.count(ShareReport.id)).where(ShareReport.resolved.is_(False))) or 0)
                        + (db.session.scalar(select(func.count(DirectReport.id)).where(DirectReport.resolved.is_(False))) or 0),
    }
    plans = dict(db.session.execute(select(User.plan, func.count(User.id)).group_by(User.plan)).all())
    pending = db.session.scalars(select(User).where(User.is_approved.is_(False)).order_by(User.created_at)).all()
    from ..services import crypto, encryption

    ring = crypto.keyring()
    enc = {"on": ring is not None, "strict": crypto.strict(), "keys": ring.checks() if ring else {},
           "active": ring.active.kid if ring else None, "pending": encryption.field_status() if ring else {},
           "last": encryption.state()}
    return render_template("admin/overview.html", stats=stats, plans=plans, pending=pending, enc=enc,
                           links=_calendar_links())


def _calendar_links(limit: int = 30) -> list[tuple]:
    """Students' calendar links with the shape of each one's last import (property names and counts,
    never text or the link), for writing better rules per LMS."""
    rows = []
    for f in db.session.scalars(select(CalendarFeed).order_by(CalendarFeed.created_at.desc()).limit(limit)):
        run = db.session.scalar(select(SyncRun).where(SyncRun.account_id == f.account_id)
                                .order_by(SyncRun.received_at.desc()).limit(1)) if f.account_id else None
        rows.append((f, ((run.stats or {}).get("shape") if run else None)))
    return rows


@bp.route("/ai")
def ai_spend():
    """What Claude costs: this month by feature, model and student."""
    since = ai.month_start()
    this_month = AIUsage.created_at >= since
    cost = func.coalesce(func.sum(AIUsage.cost_usd), 0)
    by = lambda col: db.session.execute(select(col, func.count(AIUsage.id), cost).where(this_month)  # noqa: E731
                                        .group_by(col).order_by(cost.desc())).all()
    totals = db.session.execute(select(func.count(AIUsage.id), cost, func.coalesce(func.sum(AIUsage.input_tokens), 0),
                                       func.coalesce(func.sum(AIUsage.output_tokens), 0),
                                       func.coalesce(func.sum(AIUsage.cache_read_tokens), 0)).where(this_month)).one()
    top = db.session.execute(select(User, func.count(AIUsage.id), cost).join(AIUsage, AIUsage.user_id == User.id)
                             .where(this_month).group_by(User.id).order_by(cost.desc()).limit(15)).all()
    return render_template("admin/ai.html", since=since, totals=totals, kinds=by(AIUsage.kind), models=by(AIUsage.model),
                           top=top, plans=billing.PLANS)


@bp.route("/users")
def users():
    q = (request.args.get("q") or "").strip()
    stmt = select(User).order_by(User.created_at.desc())
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(User.username).like(like),
                              func.lower(User.display_name).like(like)))
    return render_template("admin/users.html", users=db.session.scalars(stmt.limit(200)).all(), q=q)


def _user(user_id: int) -> User:
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return user


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_detail(user_id: int):
    user = _user(user_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "approve":
            user.is_approved = True
        elif action == "toggle_active" and user.id != current_user.id:
            user.active = not user.active
        elif action == "toggle_admin" and user.id != current_user.id:
            user.is_admin = not user.is_admin
        elif action == "toggle_sharing":
            if user.sharing_blocked:
                user.sharing_blocked = False  # their sets stay private until they share them again
            else:
                sharing.block_sharing(user)
        elif action == "plan":
            plan = request.form.get("plan")
            if plan in billing.PLANS:
                user.plan = plan
                user.plan_comped = plan != "free"  # admin-granted plans bypass payment
                user.plan_status = "comped" if plan != "free" else user.plan_status
        elif action == "coins":
            try:
                amount = int(request.form.get("amount", 0))
            except ValueError:
                amount = 0
            reason = (request.form.get("reason") or "Admin adjustment").strip()[:150]
            if amount > 0:
                coins.award(user.id, amount, f"Admin: {reason}")
            elif amount < 0:
                db.session.add(CoinTransaction(user_id=user.id, amount=amount, reason=f"Admin: {reason}"))
        elif action == "reset_password":
            temp = secrets.token_urlsafe(9)
            user.set_password(temp)
            log_activity(user.id, "password_reset_by_admin", current_user.username)
            db.session.commit()
            flash(f"Temporary password for {user.username}: {temp} — share it privately; they should change it.",
                  "warning")
            return redirect(url_for("admin.user_detail", user_id=user.id))
        log_activity(user.id, f"admin:{action}", current_user.username)
        db.session.commit()
        flash("Updated.", "success")
        return redirect(url_for("admin.user_detail", user_id=user.id))
    activity = db.session.scalars(select(ActivityLog).where(ActivityLog.user_id == user.id)
                                  .order_by(ActivityLog.created_at.desc()).limit(50)).all()
    return render_template("admin/user.html", user=user, activity=activity, plans=billing.PLANS,
                           balance=coins.balance(user.id), ai_used=ai.used(user), plan=billing.plan_for(user),
                           courses=db.session.scalar(select(func.count(Course.id)).where(Course.user_id == user.id)))


@bp.route("/reports", methods=["GET", "POST"])
def reports():
    if request.method == "POST" and request.form.get("kind") == "dm":
        rid = parse_id(request.form.get("report_id"))
        r = db.session.get(DirectReport, rid) if rid else None
        if r is not None:
            if request.form.get("action") == "remove" and r.message is not None:
                r.message.deleted = r.message.removed = True
                dms.message_deleted(db.session.get(DirectThread, r.message.thread_id))
            same = select(DirectReport).where(DirectReport.message_id == r.message_id) if r.message_id else None
            for other in (db.session.scalars(same) if same is not None else [r]):
                other.resolved = True
            if r.sender_id:
                log_activity(r.sender_id, f"admin:dm_{request.form.get('action')}", current_user.username)
            db.session.commit()
        return redirect(url_for("admin.reports"))
    if request.method == "POST" and request.form.get("kind") in ("deck", "quiz"):
        model = Deck if request.form["kind"] == "deck" else PracticeQuiz
        item_id = parse_id(request.form.get("item_id"))
        item = db.session.get(model, item_id) if item_id else None
        if item is not None:
            if request.form.get("action") == "take_down":
                removed = sharing.take_down(item)
                flash(f"Taken down{f', with {removed} saved copies' if removed else ''}. The owner now has "
                      f"{db.session.get(User, item.user_id).share_strikes} strike(s).", "success")
            elif request.form.get("action") == "dismiss":
                sharing.dismiss(item)
                flash("Dismissed; the set is visible again if its owner still shares it.", "info")
            log_activity(item.user_id, f"admin:share_{request.form.get('action')}", current_user.username)
            db.session.commit()
        return redirect(url_for("admin.reports"))
    if request.method == "POST":
        report = db.session.get(ChatReport, int(request.form.get("report_id", 0)))
        if report:
            if request.form.get("action") == "remove":
                report.message.deleted = True
            elif request.form.get("action") == "restore":
                report.message.deleted = False
            for r in db.session.scalars(select(ChatReport).where(ChatReport.message_id == report.message_id)):
                r.resolved = True
            db.session.commit()
        return redirect(url_for("admin.reports"))
    rows = db.session.scalars(select(ChatReport).where(ChatReport.resolved.is_(False))
                              .order_by(ChatReport.created_at.desc())).all()
    # Reported shared sets, one entry per set with all its open reports, oldest report first.
    sets: dict = {}
    for r in db.session.scalars(select(ShareReport).where(ShareReport.resolved.is_(False)).order_by(ShareReport.created_at)):
        item = r.deck or r.quiz
        key = ("deck" if r.deck_id else "quiz", item.id)
        entry = sets.setdefault(key, {"kind": key[0], "item": item, "reports": [], "owner": db.session.get(User, item.user_id),
                                      "copies": sharing.copy_count(item)})
        entry["reports"].append(r)
    for kind, item in sharing.hidden_without_reports():  # hidden, but every report went with its reporter
        sets.setdefault((kind, item.id), {"kind": kind, "item": item, "reports": [], "copies": sharing.copy_count(item),
                                          "owner": db.session.get(User, item.user_id)})
    for entry in sets.values():
        if entry["kind"] == "deck":
            entry["cards"] = sharing.shown_cards(entry["item"])
    dm_reports = [(r, dms.report_context(r), db.session.get(User, r.sender_id) if r.sender_id else None)
                  for r in db.session.scalars(select(DirectReport).where(DirectReport.resolved.is_(False))
                                              .order_by(DirectReport.created_at.desc()))]
    return render_template("admin/reports.html", reports=rows, sets=list(sets.values()), reasons=sharing.REASONS, dm_reports=dm_reports,
                           strikes_to_block=sharing.STRIKES_TO_BLOCK)


@bp.route("/diagnostics")
def diagnostics_list():
    """Brightspace (and later other LMS) checks students chose to send: shape only, no course data."""
    rows = db.session.execute(select(LmsDiagnostic, User.username).outerjoin(User, User.id == LmsDiagnostic.user_id)
                              .order_by(LmsDiagnostic.created_at.desc()).limit(200)).all()
    items = [{"row": row, "username": username, "answered": diagnostics.answered(row.payload)} for row, username in rows]
    return render_template("admin/diagnostics.html", items=items)


@bp.route("/diagnostics/<int:diagnostic_id>.json")
def diagnostic_json(diagnostic_id: int):
    row = db.session.get(LmsDiagnostic, diagnostic_id)
    if row is None:
        abort(404)
    body = json.dumps(row.payload, indent=2, sort_keys=False)
    name = f"{row.lms}-{row.host}-{row.created_at:%Y-%m-%d}-{row.id}.json"
    return Response(body, mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/chat/<int:message_id>/delete", methods=["POST"])
def delete_message(message_id: int):
    m = db.session.get(ChatMessage, message_id)
    if m:
        m.deleted = True
        db.session.commit()
    return redirect(request.referrer or url_for("admin.reports"))
