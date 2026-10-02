from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from urllib.parse import quote

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from werkzeug.routing import BuildError
from werkzeug.utils import secure_filename

from .. import queries
from ..extensions import db
from ..models import Card, Deck, DeckTest, PracticeQuiz, QuizAttempt, StudyPlan, utcnow
from ..services import ai, cards_io, coins, learn as learn_service, sources, srs, study
from ..utils import local_now, to_local, user_zone

bp = Blueprint("study", __name__, url_prefix="/study")


def _deck(deck_id: int) -> Deck:
    deck = db.session.get(Deck, deck_id)
    if deck is None or deck.user_id != current_user.id:
        abort(404)
    return deck


def _quiz(quiz_id: int) -> PracticeQuiz:
    quiz = db.session.get(PracticeQuiz, quiz_id)
    if quiz is None or quiz.user_id != current_user.id:
        abort(404)
    return quiz


def _course_id(value) -> int | None:
    try:
        cid = int(value or 0)
    except ValueError:
        return None
    if not cid:
        return None
    queries.owned_course(current_user.id, cid)
    return cid


def _preset(args) -> dict:
    """What the generator opens with: ?course=&refs=file:1,page:2&output= (from a class's Files
    tab), or ?kind=file|page&ref= (the buttons on a file or page)."""
    refs = [r for r in (args.get("refs") or "").split(",") if r]
    if args.get("kind") in ("file", "page") and str(args.get("ref") or "").isdigit():
        refs.append(f"{args['kind']}:{args['ref']}")
    course_id = args.get("course", type=int) if hasattr(args, "getlist") else None
    if not course_id and refs:
        course_id = next((s.course_id for s in sources.describe(current_user, refs) if s.course_id), None)
    return {"course_id": course_id, "refs": refs, "output": args.get("output", "deck"),
            "mode": args.get("mode") if args.get("mode") in ("sources", "course", "paste") else "sources"}


# ---------------------------------------------------------------- hub


@bp.route("/")
@login_required
def index():
    decks = db.session.scalars(select(Deck).options(selectinload(Deck.course)).where(Deck.user_id == current_user.id).order_by(Deck.created_at.desc())).all()
    quizzes = db.session.scalars(select(PracticeQuiz).options(selectinload(PracticeQuiz.course)).where(PracticeQuiz.user_id == current_user.id)
                                 .order_by(PracticeQuiz.created_at.desc())).all()
    best = dict(db.session.execute(select(QuizAttempt.quiz_id, func.max(QuizAttempt.score * 100 / QuizAttempt.total))
                                   .where(QuizAttempt.user_id == current_user.id, QuizAttempt.total > 0)
                                   .group_by(QuizAttempt.quiz_id)).all())
    card_counts = dict(db.session.execute(select(Card.deck_id, func.count(Card.id)).join(Deck)
                                          .where(Deck.user_id == current_user.id).group_by(Card.deck_id)).all())
    return render_template("study/index.html", decks=decks, quizzes=quizzes, best=best, card_counts=card_counts)


# ---------------------------------------------------------------- AI generation


@bp.route("/sources")
@login_required
def sources_json():
    """The picker's list for one class (or the student's unfiled uploads with no course_id)."""
    raw = request.args.get("course_id", "")
    course_id = _course_id(raw) if raw else None
    return jsonify({"sources": [s.to_dict() for s in sources.for_course(current_user, course_id)]})


@bp.route("/generate", methods=["GET", "POST"])
@login_required
def generate():
    if request.method == "GET":
        preset = _preset(request.args)
        return _generate_page(preset)
    f = request.form
    mode = f.get("mode", "sources")
    output = f.get("output", "deck")
    preset = {"course_id": f.get("picker_course", type=int), "refs": f.getlist("refs"), "output": output, "mode": mode}
    try:
        count = int(f.get("count") or (15 if output == "deck" else 10))
        if mode == "course":
            material = study.gather_material(current_user, "course", f.get("course_id"), topic=f.get("topic"))
        elif mode == "paste":
            material = study.gather_material(current_user, "paste", pasted=f.get("pasted"))
        else:
            material = study.gather_sources(current_user, f.getlist("refs"))
        if output == "quiz":
            data = study.generate_quiz(current_user, material, count)
            quiz = PracticeQuiz(user_id=current_user.id, course_id=material.course_id, source="ai",
                                title=data["title"] or f"Quiz: {material.title}"[:200], questions=data["questions"],
                                from_course_files=mode != "paste")
            db.session.add(quiz)
            db.session.commit()
            target = url_for("study.take_quiz", quiz_id=quiz.id)
        else:
            data = study.generate_flashcards(current_user, material, count)
            deck = Deck(user_id=current_user.id, course_id=material.course_id, source="ai",
                        title=data["title"] or f"Cards: {material.title}"[:200],
                        description=f"Generated from {material.title}"[:1000])
            deck.cards = [Card(front=c["front"], back=c["back"], position=i) for i, c in enumerate(data["cards"])]
            db.session.add(deck)
            db.session.commit()
            target = url_for("study.deck", deck_id=deck.id)
    except (study.MaterialError, ai.AIError, ValueError) as exc:
        flash(str(exc), "error")
        return _generate_page(preset), 400
    if material.truncated:
        flash("The picked sources are long, so each one was trimmed to fit (every source still contributes).", "info")
    flash("Generated! Review the items and fix anything that looks off.", "success")
    return redirect(target)


def _generate_page(preset: dict):
    options = study.material_options(current_user, preset.get("course_id"))
    return render_template("study/generate.html", courses=options["courses"], preset=preset,
                           remaining=ai.remaining(current_user))


# ---------------------------------------------------------------- decks


def _parse_cards(text: str) -> list[tuple[str, str]]:
    """Pasted cards: "front :: back" per line, tab-separated, or a Quizlet / Anki / CSV export
    (see services/cards_io)."""
    return cards_io.parse_cards(text)[0]


@bp.route("/decks/new", methods=["GET", "POST"])
@login_required
def new_deck():
    courses = queries.visible_courses(current_user.id)
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("Give the deck a title.", "error")
            return render_template("study/deck_new.html", courses=courses), 400
        deck = Deck(user_id=current_user.id, title=title[:200], course_id=_course_id(request.form.get("course_id")),
                    description=request.form.get("description", "").strip()[:1000] or None)
        deck.cards = [Card(front=f, back=b, position=i) for i, (f, b) in enumerate(_parse_cards(request.form.get("cards")))]
        db.session.add(deck)
        db.session.commit()
        return redirect(url_for("study.deck", deck_id=deck.id))
    return render_template("study/deck_new.html", courses=courses)


@bp.route("/decks/<int:deck_id>", methods=["GET", "POST"])
@login_required
def deck(deck_id: int):
    d = _deck(deck_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            added = _parse_cards(request.form.get("cards"))
            start = len(d.cards)
            for i, (front, back) in enumerate(added):
                d.cards.append(Card(front=front, back=back, position=start + i))
            flash(f"Added {len(added)} card{'s' if len(added) != 1 else ''}.", "success")
        elif action == "edit":
            card = db.session.get(Card, int(request.form.get("card_id", 0)))
            if card and card.deck_id == d.id:
                card.front = request.form.get("front", card.front).strip()[:2000] or card.front
                card.back = request.form.get("back", card.back).strip()[:4000] or card.back
        elif action == "delete_card":
            card = db.session.get(Card, int(request.form.get("card_id", 0)))
            if card and card.deck_id == d.id:
                db.session.delete(card)
        elif action == "rename":
            d.title = request.form.get("title", d.title).strip()[:200] or d.title
        elif action == "plan":
            plan = _plan_from_form(request.form.get("plan_id"))
            d.plan_id = plan.id if plan else None
            flash(f"This deck is now for {plan.title}." if plan else "This deck isn't tied to an exam now.", "success")
        db.session.commit()
        return redirect(url_for("study.deck", deck_id=d.id))
    plans = _active_plans()
    if d.plan_id and all(p.id != d.plan_id for p in plans):
        current = db.session.get(StudyPlan, d.plan_id)
        if current is not None and current.user_id == current_user.id:
            plans.append(current)
    return render_template("study/deck.html", deck=d, plans=plans)


@bp.route("/decks/<int:deck_id>/delete", methods=["POST"])
@login_required
def delete_deck(deck_id: int):
    db.session.delete(_deck(deck_id))
    db.session.commit()
    flash("Deck deleted.", "info")
    return redirect(url_for("study.index"))


@bp.route("/decks/<int:deck_id>/review")
@login_required
def review(deck_id: int):
    """Flip through a deck: one card at a time, arrows to move, x / n underneath."""
    d = _deck(deck_id)
    cards = [{"id": c.id, "front": study.render_markdown(c.front), "back": study.render_markdown(c.back)} for c in d.cards]
    return render_template("study/review.html", deck=d, cards=cards)


@bp.route("/decks/<int:deck_id>/cram")
@login_required
def cram(deck_id: int):
    return redirect(url_for("study.review", deck_id=_deck(deck_id).id))  # old links


@bp.route("/decks/<int:deck_id>/studied", methods=["POST"])
@login_required
def studied(deck_id: int):
    """Sent when the student reaches the last card: a few coins, once a day."""
    d = _deck(deck_id)
    if len(d.cards) >= 5:
        today = local_now(current_user).date().isoformat()
        coins.award(current_user.id, 3, "Studied a flashcard deck", f"cards:{today}")
        db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- exams (plans) for decks


def _active_plans() -> list[StudyPlan]:
    """The student's active exam plans, soonest exam first (undated ones last)."""
    plans = db.session.scalars(select(StudyPlan).where(StudyPlan.user_id == current_user.id,
                                                       StudyPlan.status == "active")).all()
    return sorted(plans, key=lambda p: (p.exam_at is None, p.exam_at or datetime.max, p.id))


def _plan(plan_id: int) -> StudyPlan:
    plan = db.session.get(StudyPlan, plan_id)
    if plan is None or plan.user_id != current_user.id:
        abort(404)
    return plan


def _plan_from_form(value) -> StudyPlan | None:
    try:
        plan_id = int(value or 0)
    except ValueError:
        return None
    return _plan(plan_id) if plan_id else None


def _exam_badge(plan: StudyPlan | None) -> dict | None:
    """"Calc Midterm 2 · Thu · 64% ready" for a plan whose exam is still ahead."""
    if plan is None or plan.exam_at is None or plan.exam_at <= utcnow():
        return None
    local = to_local(plan.exam_at, current_user)
    days = (local.date() - local_now(current_user).date()).days
    when = "Today" if days <= 0 else "Tomorrow" if days == 1 else local.strftime("%a") if days < 7 \
        else local.strftime("%b %-d")
    reps = db.session.scalars(select(Card.repetitions).join(Deck, Deck.id == Card.deck_id).where(
        Deck.plan_id == plan.id, Deck.user_id == current_user.id)).all()
    ready = round(100 * sum(1 for r in reps if (r or 0) >= 1) / len(reps)) if reps else 0
    return {"title": plan.title, "when": when, "days": max(0, days), "ready": ready, "cards": len(reps)}


def _session_link(args) -> str | None:
    """Back to the planner's study session, when the planner exists in this app."""
    try:
        session_id = int(args.get("session") or 0)
    except ValueError:
        return None
    if not session_id or "planner.session" not in current_app.view_functions:
        return None
    try:
        return url_for("planner.session", session_id=session_id)
    except BuildError:
        return None


def _award_study_coins(answers: int) -> bool:
    """Learn rounds and tests count as studying: today's flashcard coins, once a day."""
    if answers < 5:
        return False
    today = local_now(current_user).date().isoformat()
    return coins.award(current_user.id, 3, "Studied flashcards", f"cards:{today}")


# ---------------------------------------------------------------- import / export / stars

MAX_IMPORT_CHARS = 5_000_000


@bp.route("/decks/import", methods=["GET", "POST"])
@login_required
def import_deck():
    """Paste a Quizlet "Copy text" export, Anki notes or a CSV; preview it live; save a deck."""
    courses = queries.visible_courses(current_user.id)
    plans = _active_plans()
    if request.method == "POST":
        f = request.form
        text = f.get("text", "")
        form = {"title": f.get("title", ""), "course_id": f.get("course_id", ""), "plan_id": f.get("plan_id", ""),
                "text": text[:200_000], "term_sep": f.get("term_sep", "auto"), "card_sep": f.get("card_sep", "auto"),
                "term_sep_custom": f.get("term_sep_custom", ""), "card_sep_custom": f.get("card_sep_custom", "")}
        if len(text) > MAX_IMPORT_CHARS:
            flash("That's more than we can import at once. Split it into a few decks.", "error")
            return render_template("study/import.html", courses=courses, plans=plans, form=form), 400
        cards, skipped, _ = cards_io.parse_cards(text, cards_io.separator_choice(f, "term_sep"),
                                                 cards_io.separator_choice(f, "card_sep"))
        title = form["title"].strip()
        if not title or not cards:
            flash("Give the deck a title." if not title else
                  "We couldn't find any cards in that. Check the separators, or see the examples below.", "error")
            return render_template("study/import.html", courses=courses, plans=plans, form=form), 400
        plan = _plan_from_form(f.get("plan_id"))
        course_id = _course_id(f.get("course_id")) or (plan.course_id if plan else None)
        deck = Deck(user_id=current_user.id, title=title[:200], course_id=course_id, source="import",
                    plan_id=plan.id if plan else None)
        deck.cards = [Card(front=front, back=back, position=i) for i, (front, back) in enumerate(cards)]
        db.session.add(deck)
        db.session.commit()
        note = f" {len(skipped)} line{'s' if len(skipped) != 1 else ''} couldn't be read." if skipped else ""
        flash(f"Imported {len(cards)} card{'s' if len(cards) != 1 else ''}.{note}", "success")
        return redirect(url_for("study.deck", deck_id=deck.id))
    form = {"plan_id": request.args.get("plan", ""), "course_id": request.args.get("course", "")}
    return render_template("study/import.html", courses=courses, plans=plans, form=form)


@bp.route("/decks/import/preview", methods=["POST"])
@login_required
def import_preview():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send JSON with a text field."}), 400
    if len(str(data.get("text") or "")) > MAX_IMPORT_CHARS:
        return jsonify({"error": "That's more than we can import at once."}), 413
    return jsonify(cards_io.preview(data, show=20))


@bp.route("/decks/<int:deck_id>/export.<fmt>")
@login_required
def export_deck(deck_id: int, fmt: str):
    """Download a deck: .csv for spreadsheets, .txt for Anki (File -> Import)."""
    if fmt not in ("csv", "txt"):
        abort(404)
    d = _deck(deck_id)
    if fmt == "csv":
        body, mimetype = "﻿" + cards_io.export_csv(d.cards), "text/csv; charset=utf-8"  # BOM: Excel reads UTF-8
    else:
        body, mimetype = cards_io.export_anki(d.cards), "text/plain; charset=utf-8"
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", d.title).strip() or "flashcards"
    ascii_name = secure_filename(name) or "flashcards"
    return Response(body, mimetype=mimetype, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}.{fmt}\"; filename*=UTF-8''{quote(name)}.{fmt}",
        "Cache-Control": "no-store"})


@bp.route("/cards/<int:card_id>/star", methods=["POST"])
@login_required
def star_card(card_id: int):
    """Toggle a card's star (or set it with {"starred": true|false})."""
    card = db.session.get(Card, card_id)
    if card is None or card.deck.user_id != current_user.id:
        abort(404)
    data = request.get_json(silent=True) or {}
    wanted = data.get("starred") if isinstance(data, dict) else None
    card.starred = wanted if isinstance(wanted, bool) else not card.starred
    db.session.commit()
    return jsonify({"ok": True, "starred": card.starred})


# ---------------------------------------------------------------- Learn and Test: which cards


def _ids(values) -> list[int]:
    out = []
    for value in values:
        for part in str(value).split(","):
            if part.strip().isdigit():
                out.append(int(part))
    return list(dict.fromkeys(out))


@dataclass
class StudySet:
    cards: list[Card]
    title: str
    plan: StudyPlan | None
    params: dict = field(default_factory=dict)  # the selection, for links to Learn / Test / Flip
    deck: Deck | None = None  # when the set is one deck
    subset: str = ""  # "starred" / "picked" when only some cards are in it


def _study_set(args) -> StudySet | None:
    """deck=<id> (repeatable), plan=<id> (every deck for that exam), card=<id> (repeatable: only
    those cards), starred=1. Everything is owner-checked; None when nothing was asked for."""
    deck_ids, card_ids = _ids(args.getlist("deck")), _ids(args.getlist("card"))
    try:
        plan_id = int(args.get("plan") or 0)
    except ValueError:
        plan_id = 0
    if not (deck_ids or card_ids or plan_id):
        return None
    plan = _plan(plan_id) if plan_id else None
    decks: list[Deck] = []
    if deck_ids:
        found = {d.id: d for d in db.session.scalars(select(Deck).options(selectinload(Deck.cards)).where(
            Deck.id.in_(deck_ids), Deck.user_id == current_user.id))}
        if not found:
            abort(404)
        decks = [found[i] for i in deck_ids if i in found]
    if plan:
        decks += [d for d in db.session.scalars(select(Deck).options(selectinload(Deck.cards)).where(
            Deck.plan_id == plan.id, Deck.user_id == current_user.id).order_by(Deck.created_at)) if d not in decks]
    if decks:
        cards = [c for d in decks for c in d.cards]
        if card_ids:
            wanted = set(card_ids)
            cards = [c for c in cards if c.id in wanted]
    elif card_ids:
        found_cards = {c.id: c for c in db.session.scalars(select(Card).join(Deck, Deck.id == Card.deck_id).options(
            selectinload(Card.deck)).where(Card.id.in_(card_ids), Deck.user_id == current_user.id))}
        if not found_cards:
            abort(404)
        cards = [found_cards[i] for i in card_ids if i in found_cards]
        decks = list(dict.fromkeys(c.deck for c in cards))
    else:
        cards = []  # a plan with no decks yet
    starred = args.get("starred") in ("1", "true", "on")
    if starred:
        cards = [c for c in cards if c.starred]
    if plan is None:
        plan_ids = {d.plan_id for d in decks}
        if len(plan_ids) == 1 and None not in plan_ids:
            candidate = db.session.get(StudyPlan, plan_ids.pop())
            plan = candidate if candidate is not None and candidate.user_id == current_user.id else None
    if plan_id and plan:
        title = plan.title
    elif len(decks) == 1:
        title = decks[0].title
    else:
        title = f"{len(decks)} decks"
    params = {k: v for k, v in (("deck", deck_ids), ("plan", plan_id or None), ("card", card_ids),
                                ("starred", 1 if starred else None)) if v}
    return StudySet(cards=cards, title=title, plan=plan, params=params,
                    deck=decks[0] if len(decks) == 1 else None,
                    subset="starred" if starred else "picked" if card_ids else "")


def _answer_with(args) -> str:
    return "term" if args.get("answer_with") == "term" else "definition"


def _reviewed_today() -> int:
    """Cards this student has answered since local midnight (they count toward the daily cap)."""
    midnight = datetime.combine(local_now(current_user).date(), dtime.min, tzinfo=user_zone(current_user))
    since = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    return db.session.scalar(select(func.count(Card.id)).join(Deck, Deck.id == Card.deck_id).where(
        Deck.user_id == current_user.id, Card.last_reviewed_at >= since)) or 0


# ---------------------------------------------------------------- Learn mode

LEARN_MAX = 1000  # cards sent to one Learn page
ROUND_SIZE = 7


@bp.route("/learn", methods=["GET", "POST"])
@login_required
def learn():
    """Learn mode: rounds of 7 from the spaced-repetition queue, each card asked as multiple
    choice and then typed, checked in the browser; one POST of results per round.
    POST shows the same page for selections too long for a URL (the flip viewer's "don't know")."""
    args = request.values
    sset = _study_set(args)
    if sset is None:
        flash("Pick a deck to learn.", "info")
        return redirect(url_for("study.index"))
    now = utcnow()
    cards = sset.cards
    if sset.subset:  # cards the student picked (or starred): study all of them now
        today, rest = cards, []
    else:
        today = srs.due_queue(cards, now, reviewed_today=_reviewed_today())
        chosen = {c.id for c in today}
        remaining = [c for c in cards if c.id not in chosen]
        rest = srs.due_queue(remaining, now, daily_cap=len(remaining)) + srs.not_due(remaining, now)
    ordered = (today + rest)[:LEARN_MAX]
    rng = random.Random()
    fronts, backs = [c.front for c in ordered], [c.back for c in ordered]
    front_keys = [learn_service.normalize(t) for t in fronts]
    back_keys = [learn_service.normalize(t) for t in backs]
    payload = [{"id": c.id, "t": c.front, "d": c.back, "th": study.render_markdown(c.front),
                "dh": study.render_markdown(c.back), "s": bool(c.starred), "n": srs.is_new(c),
                "o": learn_service.pick_options(i, backs, rng, 3, back_keys),
                "ot": learn_service.pick_options(i, fronts, rng, 3, front_keys)} for i, c in enumerate(ordered)]
    data = {"cards": payload, "today": min(len(today), len(ordered)), "roundSize": ROUND_SIZE,
            "answerWith": _answer_with(args), "total": len(cards),
            "urls": {"answers": url_for("study.learn_answers"), "star": url_for("study.star_card", card_id=0)}}
    return render_template("study/learn.html", sset=sset, data=data, exam=_exam_badge(sset.plan),
                           session_url=_session_link(args), session_id=args.get("session", ""),
                           answer_with=data["answerWith"])


@bp.route("/learn/answers", methods=["POST"])
@login_required
def learn_answers():
    """One Learn round's results: [{card_id, correct, almost}] -> SM-2 schedule for each card the
    student owns (others are ignored), plus today's study coins for a round of 5+ answers."""
    data = request.get_json(silent=True)
    items = data.get("answers") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return jsonify({"error": "Send {answers: [{card_id, correct, almost}]}."}), 400
    wanted = {}
    for item in items[:200]:
        if isinstance(item, dict) and str(item.get("card_id", "")).isdigit():
            wanted.setdefault(int(item["card_id"]), item)
    owned = db.session.scalars(select(Card).join(Deck, Deck.id == Card.deck_id).options(selectinload(Card.deck)).where(
        Card.id.in_(list(wanted)), Deck.user_id == current_user.id)).all() if wanted else []
    plan_ids = {c.deck.plan_id for c in owned if c.deck.plan_id}
    exams = dict(db.session.execute(select(StudyPlan.id, StudyPlan.exam_at).where(
        StudyPlan.id.in_(plan_ids), StudyPlan.user_id == current_user.id, StudyPlan.status == "active")).all()) \
        if plan_ids else {}
    now = utcnow()
    for card in owned:
        item = wanted[card.id]
        srs.grade(card, item.get("correct") is True, item.get("almost") is True, now, exams.get(card.deck.plan_id))
    paid = _award_study_coins(len(owned))
    db.session.commit()
    return jsonify({"ok": True, "graded": len(owned), "coins": paid})


# ---------------------------------------------------------------- Test mode

TEST_MAX_AGE = 12 * 3600


def _test_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt="hh-deck-test")


@bp.route("/test", methods=["GET", "POST"])
@login_required
def test_mode():
    """A timed practice test (multiple choice, typed, true/false) with no feedback until the
    end. What was asked travels signed in the form; scoring re-reads the real cards."""
    if request.method == "POST":
        return _grade_test()
    args = request.args
    sset = _study_set(args)
    if sset is None:
        flash("Pick a deck to test yourself on.", "info")
        return redirect(url_for("study.index"))
    try:
        n = max(1, min(int(args.get("n") or 20), 60))
    except ValueError:
        n = 20
    try:
        minutes = max(1, min(int(args.get("minutes") or 0), 180)) if args.get("minutes") else None
    except ValueError:
        minutes = None
    answer_with = _answer_with(args)
    if len(sset.cards) < 2:
        flash("A test needs at least 2 cards.", "info")
        return redirect(url_for("study.deck", deck_id=sset.deck.id) if sset.deck else url_for("study.index"))
    questions = learn_service.build_test(sset.cards, n, answer_with)
    by_id = {c.id: c for c in sset.cards}

    def html_of(token) -> str:
        if isinstance(token, int):
            return study.render_markdown(learn_service.answer_of(by_id[token], answer_with))
        return study.render_markdown(str(token))

    shown = []
    for q in questions:
        card = by_id[q["c"]]
        item = {"kind": q["k"], "prompt": study.render_markdown(learn_service.prompt_of(card, answer_with))}
        if q["k"] == "mc":
            item["options"] = [html_of(o) for o in q["o"]]
        elif q["k"] == "tf":
            item["shown"] = html_of(q["s"])
        shown.append(item)
    token = _test_signer().dumps({"u": current_user.id, "q": questions, "p": sset.plan.id if sset.plan else None,
                                  "a": answer_with, "t": int(time.time()), "m": minutes})
    return render_template("study/test.html", sset=sset, questions=shown, token=token, minutes=minutes,
                           answer_with=answer_with, session_url=_session_link(args),
                           session_id=args.get("session", ""), exam=_exam_badge(sset.plan))


def _grade_test():
    try:
        data = _test_signer().loads(request.form.get("token", ""), max_age=TEST_MAX_AGE)
    except BadSignature:
        data = None
    if not data or data.get("u") != current_user.id:
        flash("That test expired, so it wasn't scored. Start a new one from the deck.", "info")
        return redirect(url_for("study.index"))
    questions, answer_with = data.get("q") or [], data.get("a", "definition")
    ids = {q["c"] for q in questions} | {o for q in questions for o in q.get("o", []) if isinstance(o, int)} \
        | {q["s"] for q in questions if isinstance(q.get("s"), int)}
    cards_by_id = {c.id: c for c in db.session.scalars(select(Card).join(Deck, Deck.id == Card.deck_id).where(
        Card.id.in_(ids), Deck.user_id == current_user.id))} if ids else {}
    answers, score = [], 0
    for i, q in enumerate(questions):
        if q["c"] not in cards_by_id:
            continue  # deleted since (or not this student's): not part of the score
        correct, given, almost = learn_service.grade_question(q, request.form.get(f"a{i}"), cards_by_id, answer_with)
        score += correct
        entry = {"card_id": q["c"], "kind": q["k"], "correct": correct, "given": given[:500]}
        if almost:
            entry["almost"] = True
        answers.append(entry)
    if not answers:
        flash("Those cards were deleted, so there was nothing to score.", "info")
        return redirect(url_for("study.index"))
    plan_id = data.get("p")
    if plan_id:
        plan = db.session.get(StudyPlan, plan_id)
        plan_id = plan.id if plan is not None and plan.user_id == current_user.id else None
    seconds = max(0, min(int(time.time()) - int(data.get("t") or time.time()), TEST_MAX_AGE))
    test = DeckTest(user_id=current_user.id, plan_id=plan_id, score=score, total=len(answers), seconds=seconds,
                    answers=answers)
    db.session.add(test)
    if _award_study_coins(len(answers)):
        flash("+3 Buddy Coins for studying today.", "success")
    db.session.commit()
    return redirect(url_for("study.test_result", test_id=test.id, **_keep_selection(request.form, answer_with)))


def _keep_selection(src, answer_with: str | None = None) -> dict:
    keep = {k: src.getlist(k) for k in ("deck", "card") if src.getlist(k)}
    keep.update({k: src[k] for k in ("plan", "starred", "session") if src.get(k)})
    if (answer_with or src.get("answer_with")) == "term":
        keep["answer_with"] = "term"
    return keep


def _deck_test(test_id: int) -> DeckTest:
    test = db.session.get(DeckTest, test_id)
    if test is None or test.user_id != current_user.id:
        abort(404)
    return test


@bp.route("/tests/<int:test_id>")
@login_required
def test_result(test_id: int):
    test = _deck_test(test_id)
    answer_with = _answer_with(request.args)
    answers = test.answers or []
    ids = [a["card_id"] for a in answers]
    cards = {c.id: c for c in db.session.scalars(select(Card).join(Deck, Deck.id == Card.deck_id).where(
        Card.id.in_(ids), Deck.user_id == current_user.id))} if ids else {}
    misses = []
    for a in answers:
        if a.get("correct"):
            continue
        card = cards.get(a["card_id"])
        misses.append({"card_id": a["card_id"], "kind": a.get("kind"), "given": a.get("given") or "",
                       "starred": bool(card and card.starred),
                       "prompt": study.render_markdown(learn_service.prompt_of(card, answer_with)) if card else "",
                       "expected": study.render_markdown(learn_service.answer_of(card, answer_with)) if card
                       else "<p><em>(card deleted)</em></p>"})
    plan = db.session.get(StudyPlan, test.plan_id) if test.plan_id else None
    selection = {k: v for k, v in _keep_selection(request.args).items() if k != "session"}
    return render_template("study/test_result.html", test=test, misses=misses, selection=selection,
                           missed_ids=[m["card_id"] for m in misses if m["card_id"] in cards],
                           session_url=_session_link(request.args), session_id=request.args.get("session", ""),
                           exam=_exam_badge(plan if plan and plan.user_id == current_user.id else None),
                           answer_with=answer_with)


@bp.route("/tests/<int:test_id>/star", methods=["POST"])
@login_required
def star_missed(test_id: int):
    """"Star the ones I missed": stars every card the test marked wrong."""
    test = _deck_test(test_id)
    missed = {a["card_id"] for a in (test.answers or []) if not a.get("correct")}
    cards = db.session.scalars(select(Card).join(Deck, Deck.id == Card.deck_id).where(
        Card.id.in_(missed), Deck.user_id == current_user.id)).all() if missed else []
    for card in cards:
        card.starred = True
    db.session.commit()
    flash(f"Starred {len(cards)} card{'s' if len(cards) != 1 else ''}. Turn on \"Starred only\" in Learn to drill them.",
          "success")
    return redirect(url_for("study.test_result", test_id=test.id, **_keep_selection(request.form)))


# ---------------------------------------------------------------- quizzes

_Q = re.compile(r"^\s*Q\s*[:.)]\s*(.+)$", re.I)
_E = re.compile(r"^\s*E\s*[:.)]\s*(.+)$", re.I)


def parse_quiz_text(text: str) -> list[dict]:
    """Q: question / "- wrong" / "* right" / E: explanation. Any other non-blank line continues
    whatever came before it, so multi-line questions, choices and explanations survive editing."""
    questions, current, field = [], None, None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if m := _Q.match(line):
            if current:
                questions.append(current)
            current = {"question": m.group(1).strip(), "choices": [], "answer": -1, "explanation": ""}
            field = "question"
        elif current and stripped.startswith(("- ", "* ", "-\t", "*\t")) or (current and stripped in ("-", "*")):
            if stripped.startswith("*"):
                current["answer"] = len(current["choices"])
            current["choices"].append(stripped[1:].strip())
            field = "choice"
        elif current and (m := _E.match(line)):
            current["explanation"] = m.group(1).strip()
            field = "explanation"
        elif current and stripped:
            if field == "question":
                current["question"] += "\n" + stripped
            elif field == "choice":
                current["choices"][-1] += "\n" + stripped
            elif field == "explanation":
                current["explanation"] += "\n" + stripped
    if current:
        questions.append(current)
    return study.valid_questions(questions)


def quiz_to_text(quiz: PracticeQuiz) -> str:
    blocks = []
    for q in quiz.questions:
        # Continuation lines are indented so they can't be mistaken for new choices or questions.
        cont = lambda text: text.replace("\n", "\n  ")  # noqa: E731
        lines = [f"Q: {cont(q['question'])}"]
        lines += [f"{'*' if i == q['answer'] else '-'} {cont(c)}" for i, c in enumerate(q["choices"])]
        if q.get("explanation"):
            lines.append(f"E: {cont(q['explanation'])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


@bp.route("/quizzes/new", methods=["GET", "POST"])
@login_required
def new_quiz():
    courses = queries.visible_courses(current_user.id)
    if request.method == "POST":
        questions = parse_quiz_text(request.form.get("body"))
        title = request.form.get("title", "").strip()
        if not title or not questions:
            flash("Add a title and at least one complete question (mark the right answer with *).", "error")
            return render_template("study/quiz_edit.html", quiz=None, courses=courses, body=request.form.get("body", ""),
                                   title=title), 400
        quiz = PracticeQuiz(user_id=current_user.id, title=title[:200], questions=questions,
                            course_id=_course_id(request.form.get("course_id")))
        db.session.add(quiz)
        db.session.commit()
        return redirect(url_for("study.take_quiz", quiz_id=quiz.id))
    return render_template("study/quiz_edit.html", quiz=None, courses=courses, body="", title="")


@bp.route("/quizzes/<int:quiz_id>/edit", methods=["GET", "POST"])
@login_required
def edit_quiz(quiz_id: int):
    quiz = _quiz(quiz_id)
    courses = queries.visible_courses(current_user.id)
    if request.method == "POST":
        questions = parse_quiz_text(request.form.get("body"))
        if not questions:
            flash("Keep at least one complete question.", "error")
            return render_template("study/quiz_edit.html", quiz=quiz, courses=courses, body=request.form.get("body", ""),
                                   title=request.form.get("title", quiz.title)), 400
        quiz.title = request.form.get("title", quiz.title).strip()[:200] or quiz.title
        quiz.questions = questions
        try:
            quiz.seconds_per_question = max(5, min(int(request.form.get("seconds", 20)), 120))
        except ValueError:
            pass
        db.session.commit()
        flash("Quiz saved.", "success")
        return redirect(url_for("study.take_quiz", quiz_id=quiz.id))
    return render_template("study/quiz_edit.html", quiz=quiz, courses=courses, body=quiz_to_text(quiz), title=quiz.title)


@bp.route("/quizzes/<int:quiz_id>/delete", methods=["POST"])
@login_required
def delete_quiz(quiz_id: int):
    db.session.delete(_quiz(quiz_id))
    db.session.commit()
    flash("Quiz deleted.", "info")
    return redirect(url_for("study.index"))


@bp.route("/quizzes/<int:quiz_id>", methods=["GET", "POST"])
@login_required
def take_quiz(quiz_id: int):
    quiz = _quiz(quiz_id)
    if request.method == "POST":
        answers = []
        for i in range(len(quiz.questions)):
            value = request.form.get(f"q{i}")
            answers.append(int(value) if value is not None and value.isdigit() else None)
        score = sum(1 for q, a in zip(quiz.questions, answers) if a == q["answer"])
        attempt = QuizAttempt(quiz_id=quiz.id, user_id=current_user.id, answers=answers, score=score,
                              total=len(quiz.questions))
        db.session.add(attempt)
        # Coins need a real quiz (5+ questions), so one-question quizzes can't mint coins.
        if len(quiz.questions) >= 5 and score / len(quiz.questions) >= 0.8:
            today = local_now(current_user).date().isoformat()
            if coins.award(current_user.id, 5, f"Scored {score}/{len(quiz.questions)} on {quiz.title}"[:200],
                           f"quiz:{quiz.id}:{today}"):
                flash("+5 Buddy Coins for scoring 80% or better!", "success")
        db.session.commit()
        return render_template("study/quiz_result.html", quiz=quiz, attempt=attempt)
    return render_template("study/quiz_take.html", quiz=quiz)
