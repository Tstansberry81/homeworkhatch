from __future__ import annotations

import re
from datetime import timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select

from .. import queries
from ..extensions import db
from ..models import Card, Deck, PracticeQuiz, QuizAttempt, utcnow
from ..services import ai, coins, srs, study
from ..utils import local_now

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


def _preset_course(preset: dict) -> int | None:
    """The class that owns the preselected file/page, so it shows even when hidden."""
    from ..models import CanvasFile, Page

    try:
        ref = int(preset.get("ref") or 0)
    except ValueError:
        return None
    if preset.get("kind") == "file":
        f = db.session.get(CanvasFile, ref)
        return f.course_id if f and f.user_id == current_user.id else None
    if preset.get("kind") == "page":
        p = db.session.get(Page, ref)
        return p.course_id if p else None
    return None


# ---------------------------------------------------------------- hub


@bp.route("/")
@login_required
def index():
    decks = db.session.scalars(select(Deck).where(Deck.user_id == current_user.id).order_by(Deck.created_at.desc())).all()
    now = utcnow()
    due = {d.id: sum(1 for c in d.cards if c.due_at <= now) for d in decks}
    quizzes = db.session.scalars(select(PracticeQuiz).where(PracticeQuiz.user_id == current_user.id)
                                 .order_by(PracticeQuiz.created_at.desc())).all()
    best = dict(db.session.execute(select(QuizAttempt.quiz_id, func.max(QuizAttempt.score * 100 / QuizAttempt.total))
                                   .where(QuizAttempt.user_id == current_user.id, QuizAttempt.total > 0)
                                   .group_by(QuizAttempt.quiz_id)).all())
    return render_template("study/index.html", decks=decks, due=due, quizzes=quizzes, best=best)


# ---------------------------------------------------------------- AI generation


@bp.route("/generate", methods=["GET", "POST"])
@login_required
def generate():
    preset = {"kind": request.args.get("kind", "file"), "ref": request.args.get("ref", ""),
              "output": request.args.get("output", "deck")}
    options = study.material_options(current_user, _preset_course(preset))
    if request.method == "GET":
        return render_template("study/generate.html", options=options, preset=preset,
                               remaining=ai.remaining(current_user))
    f = request.form
    kind = f.get("kind", "file")
    ref = {"file": f.get("file_id"), "page": f.get("page_id"), "course": f.get("course_id")}.get(kind)
    output = f.get("output", "deck")
    try:
        count = int(f.get("count") or (15 if output == "deck" else 10))
        material = study.gather_material(current_user, kind, ref, topic=f.get("topic"), pasted=f.get("pasted"))
        if output == "quiz":
            data = study.generate_quiz(current_user, material, count)
            quiz = PracticeQuiz(user_id=current_user.id, course_id=material.course_id, source="ai",
                                title=data["title"] or f"Quiz: {material.title}"[:200], questions=data["questions"])
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
        return render_template("study/generate.html", options=options, preset={"kind": kind, "ref": ref or "",
                                                                                "output": output},
                               remaining=ai.remaining(current_user)), 400
    if material.truncated:
        flash("That source is very long, so only its first part was used.", "info")
    flash("Generated! Review the items and fix anything that looks off.", "success")
    return redirect(target)


# ---------------------------------------------------------------- decks


def _parse_cards(text: str) -> list[tuple[str, str]]:
    """One card per line: "front :: back" (or tab-separated, as pasted from a spreadsheet)."""
    cards = []
    for line in (text or "").splitlines():
        if "::" in line:
            front, back = line.split("::", 1)
        elif "\t" in line:
            front, back = line.split("\t", 1)
        else:
            continue
        if front.strip() and back.strip():
            cards.append((front.strip()[:2000], back.strip()[:4000]))
    return cards


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
        elif action == "reset":
            for c in d.cards:
                c.ease, c.interval_days, c.repetitions, c.lapses, c.due_at = 2.5, 0, 0, 0, utcnow()
            flash("Progress reset: every card is due again.", "info")
        db.session.commit()
        return redirect(url_for("study.deck", deck_id=d.id))
    now = utcnow()
    return render_template("study/deck.html", deck=d, due=sum(1 for c in d.cards if c.due_at <= now))


@bp.route("/decks/<int:deck_id>/delete", methods=["POST"])
@login_required
def delete_deck(deck_id: int):
    db.session.delete(_deck(deck_id))
    db.session.commit()
    flash("Deck deleted.", "info")
    return redirect(url_for("study.index"))


@bp.route("/decks/<int:deck_id>/review", methods=["GET", "POST"])
@login_required
def review(deck_id: int):
    d = _deck(deck_id)
    if request.method == "POST":
        card = db.session.get(Card, int(request.form.get("card_id", 0) or 0))
        rating = request.form.get("rating")
        # A double-click or key repeat submits the same card twice: only the first counts.
        fresh = card is None or card.last_reviewed_at is None or (utcnow() - card.last_reviewed_at).total_seconds() > 3
        if card and card.deck_id == d.id and rating in srs.RATINGS and fresh:
            srs.review(card, rating)
            local = local_now(current_user)
            today = local.date().isoformat()
            local_midnight_utc = (local.replace(hour=0, minute=0, second=0, microsecond=0)
                                  .astimezone(timezone.utc).replace(tzinfo=None))
            reviewed_today = db.session.scalar(select(func.count(Card.id)).join(Deck).where(
                Deck.user_id == current_user.id, Card.last_reviewed_at >= local_midnight_utc))
            if reviewed_today and reviewed_today >= 10:
                coins.award(current_user.id, 3, "Reviewed 10 flashcards", f"cards:{today}")
            db.session.commit()
        return redirect(url_for("study.review", deck_id=d.id))
    queue = srs.due_cards(d.cards)
    return render_template("study/review.html", deck=d, card=queue[0] if queue else None, remaining=len(queue))


@bp.route("/decks/<int:deck_id>/cram")
@login_required
def cram(deck_id: int):
    d = _deck(deck_id)
    return render_template("study/cram.html", deck=d, cards=[{"front": c.front, "back": c.back} for c in d.cards])


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
