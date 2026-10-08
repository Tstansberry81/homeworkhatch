"""The student's own calendar items (models.UserEvent): add by hand, edit, mark done, delete. Items
the tutor suggested arrive through tutor.add_to_calendar; the rules live in services/myevents.py."""

from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import queries
from ..extensions import db
from ..models import Course, UserEvent, utcnow
from ..services import myevents
from ..utils import local_now, parse_id, to_local

bp = Blueprint("myevents", __name__, url_prefix="/calendar/items")


def _event(event_id: int) -> UserEvent:
    e = db.session.get(UserEvent, event_id)
    if e is None or e.user_id != current_user.id:
        abort(404)
    return e


def _courses(event: UserEvent | None = None) -> list[Course]:
    """The class menu: visible classes, plus the item's own class if it's hidden or over now (so saving
    doesn't drop it)."""
    courses = queries.visible_courses(current_user.id)
    if event is not None and event.course is not None and event.course not in courses:
        courses.append(event.course)
    return courses


def _form_item() -> dict:
    f = request.form
    timed = not f.get("all_day") and f.get("start")
    return {"title": f.get("title"), "date": f.get("date"), "start": f.get("start") if timed else None,
            "end": f.get("end") if timed else None, "course_id": f.get("course_id"), "notes": f.get("notes")}


def _back(event: UserEvent):
    return redirect(url_for("main.calendar_view", view="day", d=myevents.local_day(event, current_user).isoformat()))


def _values(event: UserEvent | None) -> dict:
    """What the form shows: the item's own values, or a new one's defaults (the day the calendar was on)."""
    if event is None:
        day = request.args.get("d") or local_now(current_user).date().isoformat()
        return {"title": "", "date": day[:10], "all_day": False, "start": "", "end": "",
                "course_id": parse_id(request.args.get("course_id")), "notes": ""}
    start, end = to_local(event.start_at, current_user), to_local(event.end_at, current_user)
    return {"title": event.title, "date": myevents.local_day(event, current_user).isoformat(), "all_day": event.all_day,
            "start": "" if event.all_day else f"{start:%H:%M}", "end": "" if event.all_day or end is None else f"{end:%H:%M}",
            "course_id": event.course_id, "notes": event.notes or ""}


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            item = myevents.check(_form_item(), current_user)
            myevents.room_for(current_user, 1)
        except myevents.ItemError as exc:
            flash(str(exc), "error")
            return render_template("calendar_item.html", event=None, values=dict(_form_item(), all_day=bool(request.form.get("all_day"))),
                                   courses=_courses()), 400
        event = UserEvent(user_id=current_user.id, source="manual")
        myevents.apply(event, item, current_user)
        db.session.add(event)
        db.session.commit()
        flash("Added to your calendar.", "success")
        return _back(event)
    return render_template("calendar_item.html", event=None, values=_values(None), courses=_courses())


@bp.route("/<int:event_id>", methods=["GET", "POST"])
@login_required
def edit(event_id: int):
    event = _event(event_id)
    if request.method == "POST":
        try:
            item = myevents.check(_form_item(), current_user)
        except myevents.ItemError as exc:
            flash(str(exc), "error")
            return render_template("calendar_item.html", event=event, values=dict(_form_item(), all_day=bool(request.form.get("all_day"))),
                                   courses=_courses(event)), 400
        myevents.apply(event, item, current_user)
        db.session.commit()
        flash("Saved.", "success")
        return _back(event)
    return render_template("calendar_item.html", event=event, values=_values(event), courses=_courses(event))


@bp.route("/<int:event_id>/done", methods=["POST"])
@login_required
def done(event_id: int):
    event = _event(event_id)
    event.done_at = None if event.done_at else utcnow()
    db.session.commit()
    if request.accept_mimetypes.best == "application/json":
        return {"done": event.done_at is not None}
    return redirect(request.referrer or url_for("main.calendar_view"))


@bp.route("/remove", methods=["POST"])
@login_required
def remove():
    """Remove several items at once (what a revised tutor plan replaced). JSON {"ids": [...]}."""
    data = request.get_json(silent=True)
    ids = [i for i in (parse_id(x) for x in (data.get("ids") if isinstance(data, dict) and isinstance(data.get("ids"), list) else [])[:100]) if i]
    rows = db.session.scalars(select(UserEvent).where(UserEvent.user_id == current_user.id, UserEvent.id.in_(ids))).all() if ids else []
    for e in rows:
        db.session.delete(e)
    db.session.commit()
    return jsonify({"removed": len(rows)})


@bp.route("/<int:event_id>/delete", methods=["POST"])
@login_required
def delete(event_id: int):
    event = _event(event_id)
    back = _back(event)
    db.session.delete(event)
    db.session.commit()
    flash("Deleted.", "info")
    return back
