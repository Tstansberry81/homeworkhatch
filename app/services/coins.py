"""Buddy Coins: an append-only ledger with idempotent awards, plus achievements.

Earning rules (from the original app): completing work pays a base amount by type —
assignment 10, quiz 20, test/exam 30 — and a graded result adds a bonus proportional to
the grade. Late work earns half the base. Studying (flashcards, practice quizzes, live
quizzes) and daily check-ins earn smaller amounts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Assignment, Card, CoinTransaction, Deck, QuizAttempt, User

BASE_BY_TYPE = {"assignment": 10, "quiz": 20, "test": 30}
_TEST_RE = re.compile(r"\b(exam|test|midterm|final)s?\b", re.I)
_QUIZ_RE = re.compile(r"\bquiz(zes)?\b", re.I)


class InsufficientCoins(Exception):
    pass


def work_type(assignment: Assignment) -> str:
    if _TEST_RE.search(assignment.name or ""):
        return "test"
    if assignment.is_quiz or _QUIZ_RE.search(assignment.name or ""):
        return "quiz"
    return "assignment"


def balance(user_id: int) -> int:
    return int(db.session.scalar(select(func.coalesce(func.sum(CoinTransaction.amount), 0))
                                 .where(CoinTransaction.user_id == user_id)) or 0)


def lifetime_earned(user_id: int) -> int:
    return int(db.session.scalar(select(func.coalesce(func.sum(CoinTransaction.amount), 0))
                                 .where(CoinTransaction.user_id == user_id, CoinTransaction.amount > 0)) or 0)


def award(user_id: int, amount: int, reason: str, ref: str | None = None) -> bool:
    """Credit coins. With a ref, the same award is only ever paid once. Returns True if paid."""
    if amount <= 0:
        return False
    if ref and db.session.scalar(select(CoinTransaction.id).where(
            CoinTransaction.user_id == user_id, CoinTransaction.ref == ref)):
        return False
    try:
        with db.session.begin_nested():
            db.session.add(CoinTransaction(user_id=user_id, amount=int(amount), reason=reason[:200], ref=ref))
    except IntegrityError:  # lost a race with an identical award
        return False
    return True


def spend(user_id: int, amount: int, reason: str) -> None:
    if amount <= 0:
        return
    if balance(user_id) < amount:
        raise InsufficientCoins(f"You need {amount} coins for that.")
    db.session.add(CoinTransaction(user_id=user_id, amount=-int(amount), reason=reason[:200]))


def award_for_assignment(user_id: int, account_host: str, a: Assignment) -> int:
    """Pay for submitted/graded work found in a Canvas sync. Safe to call on every sync."""
    if a.excused:
        return 0
    base = BASE_BY_TYPE[work_type(a)]
    key = f"{account_host}:{a.course.canvas_id}:{a.canvas_id}"
    paid = 0
    submitted = a.submitted_at is not None or a.status in {"submitted", "submitted_late", "graded"}
    if submitted:
        late = a.late or a.status == "submitted_late"
        amount = base // 2 if late else base
        label = "Turned in (late)" if late else "Turned in"
        if award(user_id, amount, f"{label}: {a.name}", f"submit:{key}"):
            paid += amount
    if a.status == "graded" and a.percent is not None:
        bonus = int(max(0.0, min(a.percent, 100.0)) / 100 * base)
        if award(user_id, bonus, f"Grade bonus: {a.name} ({a.percent:g}%)", f"grade:{key}"):
            paid += bonus
    return paid


# ---------------------------------------------------------------- achievements


@dataclass
class Achievement:
    key: str
    name: str
    description: str
    icon: str
    earned: bool
    progress: str = ""


def achievements(user: User) -> list[Achievement]:
    earned = lifetime_earned(user.id)
    on_time = db.session.scalar(select(func.count(CoinTransaction.id)).where(
        CoinTransaction.user_id == user.id, CoinTransaction.ref.like("submit:%"),
        ~CoinTransaction.reason.like("%(late)%"))) or 0
    reviews = db.session.scalar(select(func.coalesce(func.sum(Card.review_count), 0))
                                .join(Deck, Deck.id == Card.deck_id).where(Deck.user_id == user.id)) or 0
    perfect = db.session.scalar(select(func.count(QuizAttempt.id)).where(
        QuizAttempt.user_id == user.id, QuizAttempt.score == QuizAttempt.total, QuizAttempt.total > 0)) or 0
    live_wins = db.session.scalar(select(func.count(CoinTransaction.id)).where(
        CoinTransaction.user_id == user.id, CoinTransaction.ref.like("live:%:1"))) or 0
    streak = user.streak_days or 0

    def a(key, name, desc, icon, value, target):
        return Achievement(key, name, desc, icon, value >= target, f"{min(value, target)}/{target}")

    return [
        a("first_coin", "First Coin", "Earn your first Buddy Coin", "🪙", earned, 1),
        a("collector", "Coin Collector", "Earn 500 coins", "💰", earned, 500),
        a("tycoon", "Tycoon", "Earn 5,000 coins", "🏦", earned, 5000),
        a("on_time_10", "On It", "Turn in 10 assignments", "✅", on_time, 10),
        a("on_time_50", "Machine", "Turn in 50 assignments", "⚙️", on_time, 50),
        a("cards_100", "Card Shark", "Review 100 flashcards", "🃏", reviews, 100),
        a("cards_1000", "Memory Palace", "Review 1,000 flashcards", "🏛️", reviews, 1000),
        a("perfect_quiz", "Flawless", "Score 100% on a practice quiz", "🎯", perfect, 1),
        a("live_win", "Champion", "Win a live quiz", "🏆", live_wins, 1),
        a("streak_7", "Week Streak", "Study 7 days in a row", "🔥", streak, 7),
        a("streak_30", "Unstoppable", "Study 30 days in a row", "☄️", streak, 30),
    ]
