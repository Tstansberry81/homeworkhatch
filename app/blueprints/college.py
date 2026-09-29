from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from ..extensions import db
from ..models import SavedCollege
from ..services import college

bp = Blueprint("college", __name__, url_prefix="/college")

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
             "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR").split()


def _num(value, kind=float, low=None, high=None):
    try:
        v = kind(value)
    except (TypeError, ValueError):
        return None
    if (low is not None and v < low) or (high is not None and v > high):
        return None
    return v


def _rate(value):
    """Accepts 0.24, 24 or 24% -> 0.24."""
    if value in (None, ""):
        return None
    v = _num(str(value).rstrip("%"), float, 0, 100)
    if v is None:
        return None
    return v / 100 if v > 1 else v


@bp.route("/")
@login_required
def index():
    schools = db.session.scalars(select(SavedCollege).where(SavedCollege.user_id == current_user.id)
                                 .order_by(SavedCollege.name)).all()
    rows = [(s, college.estimate(current_user, s)) for s in schools]
    order = {"Safety": 0, "Target": 1, "Reach": 2, "Unknown": 3}
    rows.sort(key=lambda r: (order.get(r[1].label, 9), -(r[1].chance or 0)))
    return render_template("college/index.html", rows=rows, states=US_STATES,
                           search_enabled=college.scorecard_enabled())


@bp.route("/profile", methods=["POST"])
@login_required
def profile():
    f = request.form
    current_user.gpa = _num(f.get("gpa"), float, 0, 20)
    current_user.gpa_scale = _num(f.get("gpa_scale"), float, 1, 20) or 4.0
    current_user.sat = _num(f.get("sat"), int, 400, 1600)
    current_user.act = _num(f.get("act"), int, 1, 36)
    state = (f.get("home_state") or "").upper()
    current_user.home_state = state if state in US_STATES else None
    db.session.commit()
    flash("Academic profile saved.", "success")
    return redirect(url_for("college.index"))


@bp.route("/search")
@login_required
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"results": []})
    try:
        return jsonify({"results": college.search(q)})
    except college.CollegeDataError as exc:
        return jsonify({"error": str(exc)}), 503


@bp.route("/save", methods=["POST"])
@login_required
def save():
    f = request.form
    name = (f.get("name") or "").strip()
    if not name:
        flash("A school needs a name.", "error")
        return redirect(url_for("college.index"))
    state = (f.get("state") or "").upper()
    school = SavedCollege(
        user_id=current_user.id, name=name[:300], scorecard_id=(f.get("scorecard_id") or None),
        city=(f.get("city") or "")[:120] or None, state=state if state in US_STATES else None,
        public={"1": True, "true": True, "0": False, "false": False}.get((f.get("public") or "").lower()),
        admit_rate=_rate(f.get("admit_rate")), oos_admit_rate=_rate(f.get("oos_admit_rate")),
        sat25=_num(f.get("sat25"), int, 400, 1600), sat75=_num(f.get("sat75"), int, 400, 1600),
        act25=_num(f.get("act25"), int, 1, 36), act75=_num(f.get("act75"), int, 1, 36))
    db.session.add(school)
    db.session.commit()
    flash(f"Added {school.name}.", "success")
    return redirect(url_for("college.index"))


@bp.route("/<int:school_id>/update", methods=["POST"])
@login_required
def update(school_id: int):
    school = db.session.get(SavedCollege, school_id)
    if school is None or school.user_id != current_user.id:
        abort(404)
    school.oos_admit_rate = _rate(request.form.get("oos_admit_rate"))
    if request.form.get("admit_rate"):
        school.admit_rate = _rate(request.form.get("admit_rate"))
    db.session.commit()
    return redirect(url_for("college.index"))


@bp.route("/<int:school_id>/delete", methods=["POST"])
@login_required
def delete(school_id: int):
    school = db.session.get(SavedCollege, school_id)
    if school is None or school.user_id != current_user.id:
        abort(404)
    db.session.delete(school)
    db.session.commit()
    return redirect(url_for("college.index"))
