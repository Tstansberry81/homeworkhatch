"""The walkthrough's endpoints (services/tour.py): where the student is in it, finishing or skipping
it, live setup status for the steps that check themselves off, and marks for what the server can't see."""

from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..services import tour

bp = Blueprint("tour", __name__, url_prefix="/tour")


def _json() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.route("/start", methods=["POST"])
@login_required
def start():
    """Start (or restart) the tour, from the dashboard's invitation or the profile."""
    tour.start(current_user)
    db.session.commit()
    return redirect(url_for("main.dashboard", tour="start"))  # tour.js forgets an earlier "hide the tour"


@bp.route("/decline", methods=["POST"])
@login_required
def decline():
    tour.finish(current_user)
    db.session.commit()
    return redirect(request.referrer or url_for("main.dashboard"))


@bp.route("/step", methods=["POST"])
@login_required
def step():
    ident = _json().get("step")
    if not isinstance(ident, str) or ident not in {s.id for s in tour.steps(current_user)}:
        return jsonify({"error": "Unknown step."}), 400
    current_user.tour_step = ident
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/end", methods=["POST"])
@login_required
def end():
    tour.finish(current_user)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/status")
@login_required
def status_json():
    return jsonify({k: v for k, v in tour.status(current_user).items() if not k.startswith("_")})


@bp.route("/mark", methods=["POST"])
@login_required
def mark():
    data = _json() if request.is_json else request.form
    key = data.get("key")
    if not isinstance(key, str) or key not in tour.MARKS:
        return jsonify({"error": "Unknown mark."}), 400
    tour.set_mark(current_user, key, data.get("on", True) not in (False, "0", "false"))
    db.session.commit()
    if not request.is_json:
        return redirect(request.referrer or url_for("main.dashboard"))
    return jsonify({"ok": True})
