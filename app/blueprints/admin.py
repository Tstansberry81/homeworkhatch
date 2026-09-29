from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_, select

from ..extensions import db
from ..models import (ActivityLog, AIUsage, ChatMessage, ChatReport, CoinTransaction, Course, SyncRun, User, utcnow)
from ..services import ai, billing, coins
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
        "tokens_month": db.session.scalar(select(func.coalesce(func.sum(AIUsage.input_tokens + AIUsage.output_tokens), 0))
                                          .where(AIUsage.created_at >= ai.month_start())),
        "coins": db.session.scalar(select(func.coalesce(func.sum(CoinTransaction.amount), 0))),
        "open_reports": db.session.scalar(select(func.count(ChatReport.id)).where(ChatReport.resolved.is_(False))),
    }
    plans = dict(db.session.execute(select(User.plan, func.count(User.id)).group_by(User.plan)).all())
    pending = db.session.scalars(select(User).where(User.is_approved.is_(False)).order_by(User.created_at)).all()
    return render_template("admin/overview.html", stats=stats, plans=plans, pending=pending)


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
                           balance=coins.balance(user.id), ai_used=ai.used_this_month(user.id),
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


@bp.route("/chat/<int:message_id>/delete", methods=["POST"])
def delete_message(message_id: int):
    m = db.session.get(ChatMessage, message_id)
    if m:
        m.deleted = True
        db.session.commit()
    return redirect(request.referrer or url_for("admin.reports"))
