from __future__ import annotations

import json
import secrets
from datetime import timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import (ActivityLog, AIUsage, ChatMessage, ChatReport, CoinTransaction, Course, LmsDiagnostic, SyncRun, User,
                      utcnow)
from ..services import ai, billing, coins, diagnostics
from ..utils import admin_required, log_activity

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
        "open_reports": db.session.scalar(select(func.count(ChatReport.id)).where(ChatReport.resolved.is_(False))),
    }
    plans = dict(db.session.execute(select(User.plan, func.count(User.id)).group_by(User.plan)).all())
    pending = db.session.scalars(select(User).where(User.is_approved.is_(False)).order_by(User.created_at)).all()
    from ..services import crypto, encryption

    ring = crypto.keyring()
    enc = {"on": ring is not None, "strict": crypto.strict(), "keys": ring.checks() if ring else {},
           "active": ring.active.kid if ring else None, "pending": encryption.field_status() if ring else {},
           "last": encryption.state()}
    return render_template("admin/overview.html", stats=stats, plans=plans, pending=pending, enc=enc)


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
    return render_template("admin/reports.html", reports=rows)


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
