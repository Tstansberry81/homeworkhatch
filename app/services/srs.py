"""SM-2 spaced repetition (the algorithm behind Anki/SuperMemo) for Learn mode's right/wrong answers.

Learn mode only knows whether an answer was right, so the grade maps to an SM-2 quality:
right -> 4, right after an "almost" (one typo, or "I was right") -> 3, wrong -> 1.

* A miss brings the card back in 10 minutes. It only counts as a lapse (and lowers the ease)
  when the card had been learned in an earlier session; failing a brand-new card or one
  studied minutes ago is just learning.
* Intervals go 1 day -> 3 days -> previous x ease; ease never drops below 1.3.
* Answering the same card right again within a few hours (Learn asks multiple choice, then
  typed) is practice: it's counted, but it doesn't stretch the interval a second time.
* So is answering a learned card right before it's due (starred-only or picked-card Learn,
  "Keep going"): drilling a card every few hours must not push it out for years. A miss
  still counts and brings the card back.
* Exam clamp: when the card's deck belongs to a plan with a future exam, the next interval is
  at most max(1, days_left // 2), so every card comes due again before the exam.
* No interval is ever longer than MAX_INTERVAL_DAYS (ten years).

`due_queue` picks what to study today: cards that are due (most overdue first), then new ones,
never more than the daily cap. A backlog after days off is spread over the next days instead of
becoming one big pile, and the app never shows an overdue count.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Card, utcnow

QUALITY_RIGHT, QUALITY_ALMOST, QUALITY_WRONG = 4, 3, 1
EASE_FLOOR = 1.3
RELEARN = timedelta(minutes=10)
SAME_SESSION = timedelta(hours=6)
DAILY_CAP = 150
MAX_INTERVAL_DAYS = 3650


def quality(correct: bool, almost: bool = False) -> int:
    if not correct:
        return QUALITY_WRONG
    return QUALITY_ALMOST if almost else QUALITY_RIGHT


def _ease_after(ease: float, q: int) -> float:
    return max(EASE_FLOOR, ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))


def exam_cap(exam_at: datetime | None, now: datetime) -> int | None:
    """The longest interval (days) that still brings a card back before the exam, or None."""
    if exam_at is None or exam_at <= now:
        return None
    days_left = (exam_at - now).days
    return max(1, days_left // 2)


def grade(card: Card, correct: bool, almost: bool = False, now: datetime | None = None,
          exam_at: datetime | None = None) -> Card:
    """Apply one Learn/Test answer to a card's schedule."""
    now = now or utcnow()
    q = quality(correct, almost)
    last = card.last_reviewed_at
    same_session = last is not None and timedelta(0) <= now - last < SAME_SESSION
    reps = card.repetitions or 0
    ease = card.ease or 2.5
    card.review_count = (card.review_count or 0) + 1
    card.last_reviewed_at = now

    if q < 3:
        if reps >= 1 and not same_session:  # forgot something learned before: a real lapse
            card.lapses = (card.lapses or 0) + 1
            card.ease = _ease_after(ease, q)
        card.repetitions = 0
        card.interval_days = 0
        card.due_at = now + RELEARN
        return card

    cap = exam_cap(exam_at, now)
    if same_session and reps >= 1:
        return card  # practice inside one session: counted, schedule unchanged
    if reps >= 1 and card.due_at is not None and now < card.due_at:
        # Reviewed before it's due: practice too. The schedule stays, except that an exam
        # added since still brings the card back in time.
        if cap is not None and card.due_at > now + timedelta(days=cap):
            card.interval_days = min(card.interval_days or cap, cap)
            card.due_at = now + timedelta(days=cap)
        return card

    card.ease = ease = _ease_after(ease, q)
    reps += 1
    card.repetitions = reps
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 3
    else:
        interval = max(1, round(min(card.interval_days or 1, MAX_INTERVAL_DAYS) * ease))
    if cap is not None:
        interval = min(interval, cap)
    interval = min(interval, MAX_INTERVAL_DAYS)  # now + timedelta(days=...) can never overflow
    card.interval_days = interval
    card.due_at = now + timedelta(days=interval)
    return card


def is_new(card: Card) -> bool:
    return not (card.review_count or card.last_reviewed_at)


def due_queue(cards: list[Card], now: datetime | None = None, daily_cap: int = DAILY_CAP,
              new_per_day: int | None = None, reviewed_today: int = 0) -> list[Card]:
    """Today's cards, in study order: missed-recently cards, then due reviews (most overdue
    first), then new cards in deck order. At most `daily_cap` minus what was already reviewed
    today; whatever doesn't fit waits for tomorrow (a backlog is spread out, never piled up)."""
    now = now or utcnow()
    room = max(0, daily_cap - max(0, reviewed_today))
    due = [c for c in cards if not is_new(c) and (c.due_at or now) <= now]
    due.sort(key=lambda c: ((c.repetitions or 0) > 0, c.due_at or now, c.position or 0))
    picked = due[:room]
    room -= len(picked)
    new = [c for c in cards if is_new(c)]
    if new_per_day is not None:
        new = new[:max(0, new_per_day)]
    return picked + new[:room]


def not_due(cards: list[Card], now: datetime | None = None) -> list[Card]:
    """Cards studied before and not due yet, soonest first (for "keep going" past today)."""
    now = now or utcnow()
    rest = [c for c in cards if not is_new(c) and (c.due_at or now) > now]
    return sorted(rest, key=lambda c: (c.due_at, c.position or 0))


def readiness(cards: list[Card]) -> int:
    """Percent of cards known: learned at least once and not missed in their last review
    (a miss resets repetitions to 0, so repetitions >= 1 says both)."""
    if not cards:
        return 0
    return round(100 * sum(1 for c in cards if (c.repetitions or 0) >= 1) / len(cards))
