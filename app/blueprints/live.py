"""Live quizzes: a host runs one of their quizzes; signed-in players join with a code and nickname.

Only quizzes the host wrote or made from their own pasted notes can go live: a quiz generated from
course files carries instructors' and publishers' material, which isn't the host's to hand out.
State lives in the database and clients poll once a second, so it works on any number
of server workers without WebSockets. Faster correct answers earn more points (up to 1000).
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from flask import Blueprint, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import LiveAnswer, LivePlayer, LiveSession, PracticeQuiz, utcnow
from ..services import coins, moderation

bp = Blueprint("live", __name__, url_prefix="/live")

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PRIZES = [20, 10, 5]
SESSION_HOURS = 6  # a game nobody finished closes on its own


def _session(code: str) -> LiveSession:
    s = db.session.scalar(select(LiveSession).where(LiveSession.code == code.upper()))
    if s is None:
        abort(404)
    if s.state != "finished" and utcnow() - s.created_at > timedelta(hours=SESSION_HOURS):
        s.state = "finished"
        db.session.commit()
    return s


def _player(s: LiveSession) -> LivePlayer | None:
    token = request.cookies.get(f"hh_live_{s.code}")
    if not token:
        return None
    p = db.session.scalar(select(LivePlayer).where(LivePlayer.token == token, LivePlayer.session_id == s.id))
    return p if p is not None and p.user_id == current_user.id else None


def _elapsed(s: LiveSession) -> float:
    return (utcnow() - s.question_started_at).total_seconds() if s.question_started_at else 0.0


def _advance_if_due(s: LiveSession) -> None:
    """Close a question when time is up or everyone has answered."""
    if s.state != "question":
        return
    answered = db.session.scalar(select(func.count(LiveAnswer.id)).where(
        LiveAnswer.session_id == s.id, LiveAnswer.question_index == s.question_index)) or 0
    if _elapsed(s) >= s.quiz.seconds_per_question or (s.players and answered >= len(s.players)):
        s.state = "reveal"
        db.session.commit()


def _leaderboard(s: LiveSession, limit: int = 10) -> list[dict]:
    players = sorted(s.players, key=lambda p: (-p.score, p.joined_at))
    return [{"nickname": p.nickname, "score": p.score} for p in players[:limit]]


def _finish(s: LiveSession) -> None:
    s.state = "finished"
    if not s.rewarded:
        ranked, seen = [], set()
        for p in sorted(s.players, key=lambda p: (-p.score, p.joined_at)):
            if p.user_id and p.user_id != s.host_id and p.score > 0 and p.user_id not in seen:
                seen.add(p.user_id)
                ranked.append(p)
        # Prizes only when at least two signed-in players competed.
        if len(ranked) >= 2:
            for place, (p, prize) in enumerate(zip(ranked, PRIZES), start=1):
                coins.award(p.user_id, prize, f"Live quiz #{place} in {s.quiz.title}"[:200], f"live:{s.id}:{place}")
        s.rewarded = True
    db.session.commit()


# ---------------------------------------------------------------- host


@bp.route("/host/<int:quiz_id>", methods=["POST"])
@login_required
def create(quiz_id: int):
    quiz = db.session.get(PracticeQuiz, quiz_id)
    if quiz is None or quiz.user_id != current_user.id or not quiz.questions:
        abort(404)
    if quiz.from_course_files:
        flash("Quizzes made from course files stay private to you, so they can't be hosted live. "
              "Host a quiz you wrote, or one made from your own pasted notes.", "info")
        return redirect(url_for("study.take_quiz", quiz_id=quiz.id))
    if request.form.get("own_material") != "1":  # the host confirms it's their own material
        abort(400)
    for _ in range(10):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.session.scalar(select(LiveSession.id).where(LiveSession.code == code)):
            break
    s = LiveSession(code=code, host_id=current_user.id, quiz_id=quiz.id)
    db.session.add(s)
    db.session.commit()
    return redirect(url_for("live.host", code=s.code))


@bp.route("/<code>/host")
@login_required
def host(code: str):
    s = _session(code)
    if s.host_id != current_user.id:
        abort(403)
    return render_template("live/host.html", s=s, join_url=url_for("live.join", code=s.code, _external=True))


@bp.route("/<code>/control", methods=["POST"])
@login_required
def control(code: str):
    s = _session(code)
    if s.host_id != current_user.id:
        abort(403)
    action = (request.get_json(silent=True) or {}).get("action")
    total = len(s.quiz.questions)
    if s.state == "finished":
        return jsonify({"ok": True})  # a finished game can't be restarted
    if action in ("start", "next"):
        if s.question_index + 1 >= total:
            _finish(s)
        else:
            s.question_index += 1
            s.state = "question"
            s.question_started_at = utcnow()
            db.session.commit()
    elif action == "reveal" and s.state == "question":
        s.state = "reveal"
        db.session.commit()
    elif action == "finish":
        _finish(s)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- players


@bp.route("/join", methods=["GET", "POST"])
@login_required
def join():
    code = (request.values.get("code") or "").strip().upper()
    if request.method == "POST":
        s = db.session.scalar(select(LiveSession).where(LiveSession.code == code))
        if s is not None:
            s = _session(s.code)  # closes a stale game
        nickname = (request.form.get("nickname") or "").strip()[:40]
        if s is None or s.state == "finished":
            flash("No live quiz with that code.", "error")
            return render_template("live/join.html", code=code), 404
        if not nickname:
            flash("Pick a nickname.", "error")
            return render_template("live/join.html", code=code), 400
        try:
            nickname = moderation.clean_name(nickname)
        except moderation.Rejected as exc:
            flash(str(exc), "error")
            return render_template("live/join.html", code=code), 400
        existing = _player(s)
        if existing is None:
            # One seat per account: rejoining (another tab/device) reuses the same player.
            existing = db.session.scalar(select(LivePlayer).where(LivePlayer.session_id == s.id,
                                                                  LivePlayer.user_id == current_user.id))
        if existing is None:
            p = LivePlayer(session_id=s.id, nickname=nickname, user_id=current_user.id)
            db.session.add(p)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("That nickname is taken in this game.", "error")
                return render_template("live/join.html", code=code), 400
            existing = p
        resp = make_response(redirect(url_for("live.play", code=s.code)))
        resp.set_cookie(f"hh_live_{s.code}", existing.token, max_age=6 * 3600, httponly=True, samesite="Lax",
                        secure=request.is_secure)
        return resp
    return render_template("live/join.html", code=code)


@bp.route("/<code>/play")
@login_required
def play(code: str):
    s = _session(code)
    if _player(s) is None:
        return redirect(url_for("live.join", code=s.code))
    return render_template("live/play.html", s=s)


@bp.route("/<code>/answer", methods=["POST"])
@login_required
def answer(code: str):
    s = _session(code)
    p = _player(s)
    if p is None:
        abort(403)
    _advance_if_due(s)
    if s.state != "question":
        return jsonify({"error": "Time's up!"}), 409
    try:
        choice = int((request.get_json(silent=True) or {}).get("choice"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad choice"}), 400
    q = s.quiz.questions[s.question_index]
    if not 0 <= choice < len(q["choices"]):
        return jsonify({"error": "bad choice"}), 400
    limit = s.quiz.seconds_per_question
    correct = choice == q["answer"]
    points = round(1000 * (1 - 0.5 * min(_elapsed(s), limit) / limit)) if correct else 0
    db.session.add(LiveAnswer(session_id=s.id, player_id=p.id, question_index=s.question_index, choice=choice,
                              correct=correct, points=points))
    p.score += points
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Already answered"}), 409
    return jsonify({"ok": True})


@bp.route("/<code>/state")
@login_required
def state(code: str):
    s = _session(code)
    is_host = current_user.id == s.host_id
    player = None if is_host else _player(s)
    if not is_host and player is None:
        abort(403)
    _advance_if_due(s)
    quiz = s.quiz
    out = {"state": s.state, "index": s.question_index, "total": len(quiz.questions), "players": len(s.players),
           "title": quiz.title, "leaderboard": _leaderboard(s), "limit": quiz.seconds_per_question}
    if s.state in ("question", "reveal") and 0 <= s.question_index < len(quiz.questions):
        q = quiz.questions[s.question_index]
        out["question"] = {"text": q["question"], "choices": q["choices"]}
        out["time_left"] = max(0, round(quiz.seconds_per_question - _elapsed(s), 1)) if s.state == "question" else 0
        answers = db.session.scalars(select(LiveAnswer).where(LiveAnswer.session_id == s.id,
                                                              LiveAnswer.question_index == s.question_index)).all()
        out["answered"] = len(answers)
        if s.state == "reveal":
            out["correct"] = q["answer"]
            out["explanation"] = q.get("explanation", "")
            counts = [0] * len(q["choices"])
            for a in answers:
                if 0 <= a.choice < len(counts):
                    counts[a.choice] += 1
            out["distribution"] = counts
        if player is not None:
            mine = next((a for a in answers if a.player_id == player.id), None)
            out["you"] = {"answered": mine is not None, "choice": mine.choice if mine else None,
                          "correct": mine.correct if mine and s.state == "reveal" else None,
                          "points": mine.points if mine and s.state == "reveal" else None}
    if player is not None:
        ranked = sorted(s.players, key=lambda x: (-x.score, x.joined_at))
        out["me"] = {"nickname": player.nickname, "score": player.score,
                     "rank": next(i for i, x in enumerate(ranked, 1) if x.id == player.id)}
    if is_host and s.state == "lobby":
        out["names"] = [p.nickname for p in s.players]
    return jsonify(out)
