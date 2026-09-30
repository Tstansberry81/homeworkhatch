"""Buddy Coins wallet, achievements, leaderboards, and the 18+ Probability Lab."""

from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func, select

from ..extensions import db
from ..models import CoinTransaction, User, utcnow
from ..services import coins
from ..utils import adult_required

bp = Blueprint("coins", __name__)


@bp.route("/coins")
@login_required
def wallet():
    history = db.session.scalars(select(CoinTransaction).where(CoinTransaction.user_id == current_user.id)
                                 .order_by(CoinTransaction.created_at.desc()).limit(60)).all()
    earned = func.sum(CoinTransaction.amount)
    leaders = db.session.execute(
        select(User.display_name, earned.label("earned")).join(CoinTransaction, CoinTransaction.user_id == User.id)
        .where(User.show_on_leaderboards.is_(True), User.active.is_(True), CoinTransaction.amount > 0)
        .group_by(User.id).order_by(earned.desc()).limit(15)).all()
    return render_template("coins/wallet.html", history=history, achievements=coins.achievements(current_user),
                           leaders=leaders, lifetime=coins.lifetime_earned(current_user.id))


# ---------------------------------------------------------------- Probability Lab (18+)
# Optional, age-gated simulations from the original terms of service: virtual coins only,
# no money or prizes, odds and expected value always shown.

MAX_WAGER = 50
DAILY_PLAYS = 20
HOUSE_EDGE = 0.05


def _lab_enabled():
    if not current_app.config["FEATURE_SIMULATIONS"]:
        abort(404)


def dice_odds(target: int) -> float:
    """P(sum of two dice >= target)."""
    wins = sum(1 for a in range(1, 7) for b in range(1, 7) if a + b >= target)
    return wins / 36


@bp.route("/lab")
@login_required
@adult_required
def lab():
    _lab_enabled()
    table = [{"target": t, "p": dice_odds(t), "payout": round((1 - HOUSE_EDGE) / dice_odds(t), 2)} for t in range(3, 13)]
    return render_template("coins/lab.html", table=table, max_wager=MAX_WAGER, edge=HOUSE_EDGE)


@bp.route("/lab/roll", methods=["POST"])
@login_required
@adult_required
def lab_roll():
    _lab_enabled()
    data = request.get_json(silent=True) or {}
    try:
        wager, target = int(data.get("wager")), int(data.get("target"))
    except (TypeError, ValueError):
        return jsonify({"error": "Pick a wager and a target."}), 400
    if not 1 <= wager <= MAX_WAGER or not 3 <= target <= 12:
        return jsonify({"error": f"Wager 1–{MAX_WAGER} coins on a target from 3 to 12."}), 400
    plays = db.session.scalar(select(func.count(CoinTransaction.id)).where(
        CoinTransaction.user_id == current_user.id, CoinTransaction.reason.like("Probability Lab%"),
        CoinTransaction.created_at >= utcnow() - timedelta(days=1), CoinTransaction.amount < 0)) or 0
    if plays >= DAILY_PLAYS:
        return jsonify({"error": "That's the daily limit for the lab. Come back tomorrow."}), 429
    try:
        coins.spend(current_user.id, wager, f"Probability Lab: wager on ≥{target}")
    except coins.InsufficientCoins as exc:
        return jsonify({"error": str(exc)}), 402
    d1, d2 = secrets.randbelow(6) + 1, secrets.randbelow(6) + 1
    p = dice_odds(target)
    won = d1 + d2 >= target
    payout = int(wager * (1 - HOUSE_EDGE) / p) if won else 0
    if payout:
        db.session.add(CoinTransaction(user_id=current_user.id, amount=payout, reason=f"Probability Lab: payout on ≥{target}"))
    db.session.commit()
    return jsonify({"dice": [d1, d2], "won": won, "payout": payout, "p": p,
                    "expected_value": round(wager * (1 - HOUSE_EDGE) - wager, 2), "balance": coins.balance(current_user.id)})
