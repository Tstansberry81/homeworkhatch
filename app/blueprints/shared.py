"""Shared decks and quizzes (services/sharing.py): turning sharing on, the shared link, saving a copy,
and reporting."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Deck, PracticeQuiz
from ..services import sharing

bp = Blueprint("shared", __name__)


def _owned(kind: str, item_id: int):
    model = {"deck": Deck, "quiz": PracticeQuiz}.get(kind)
    item = db.session.get(model, item_id) if model else None
    if item is None or item.user_id != current_user.id:
        abort(404)
    return item


def _back(kind: str, item) -> str:
    return url_for("study.deck", deck_id=item.id) if kind == "deck" else url_for("study.take_quiz", quiz_id=item.id)


@bp.route("/study/<kind>/<int:item_id>/share", methods=["POST"])
@login_required
def share(kind: str, item_id: int):
    item = _owned(kind, item_id)
    mode = request.form.get("mode", "")
    try:
        sharing.set_mode(current_user, item, mode, request.form.get("own_work") == "1")
    except sharing.ShareError as exc:
        db.session.commit()  # keep the check's results, so the cards show why they're held back
        flash(str(exc), "error")
        return redirect(_back(kind, item) + "#share")
    db.session.commit()
    if mode == "private":
        flash("Sharing is off. The link no longer works.", "info")
    else:
        flash("Shared! Anyone with the link can see it" + (", and it's listed for your classmates." if mode == "class" else "."),
              "success")
    return redirect(_back(kind, item) + "#share")


def _shared(token: str):
    item = sharing.find(token)
    if item is None:
        abort(404)
    return item


@bp.route("/s/<token>")
def view(token: str):
    item = _shared(token)
    is_deck = isinstance(item, Deck)
    signed_in = current_user.is_authenticated
    own = signed_in and item.user_id == current_user.id
    if is_deck:
        shown = sharing.shown_cards(item) if signed_in else sharing.preview_cards(item, sharing.PREVIEW_CARDS)
        total = len(shown) if signed_in else sharing.shown_count(item)
    else:
        shown = item.questions if signed_in else item.questions[:sharing.PREVIEW_QUESTIONS]
        total = len(item.questions)
    if not signed_in:
        session["shared_next"] = url_for("shared.view", token=token)  # back here after signing up
    resp = render_template("shared/view.html", item=item, is_deck=is_deck, shown=shown, total=total, own=own,
                           token=token, signed_in=signed_in, description=sharing.public_description(item))
    return resp, 200, {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"}


@bp.route("/s/<token>/copy", methods=["POST"])
@login_required
def copy(token: str):
    item = _shared(token)
    if item.user_id == current_user.id:
        return redirect(_back("deck" if isinstance(item, Deck) else "quiz", item))
    new = sharing.copy_to(current_user, item)
    db.session.commit()
    if isinstance(new, Deck):
        flash(f"Saved to your flashcards: {len(new.cards)} cards. Study it, edit it or play it live; it's yours to "
              "share only once you've rewritten it in your own words.", "success")
        return redirect(url_for("study.deck", deck_id=new.id))
    flash("Saved to your quizzes.", "success")
    return redirect(url_for("study.take_quiz", quiz_id=new.id))


@bp.route("/s/<token>/report", methods=["GET", "POST"])
@login_required
def report(token: str):
    item = _shared(token)
    if item.user_id == current_user.id:
        abort(404)
    if request.method == "POST":
        try:
            sharing.report(current_user, item, request.form.get("reason", ""), request.form.get("details"))
        except sharing.ShareError as exc:
            flash(str(exc), "error")
            return render_template("shared/report.html", item=item, token=token, reasons=sharing.REASONS), 400
        db.session.commit()
        flash("Thanks. We'll review it" + (" (it's hidden until we do)." if item.share_hidden else "."), "success")
        return redirect(url_for("study.index") if item.share_hidden else url_for("shared.view", token=token))
    return render_template("shared/report.html", item=item, token=token, reasons=sharing.REASONS)
