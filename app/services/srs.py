"""SM-2 spaced repetition (the algorithm behind Anki/SuperMemo), used for flashcards."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import Card, utcnow

# Button -> SM-2 quality grade.
RATINGS = {"again": 1, "hard": 3, "good": 4, "easy": 5}


def review(card: Card, rating: str, now: datetime | None = None) -> Card:
    if rating not in RATINGS:
        raise ValueError(f"unknown rating {rating!r}")
    now = now or utcnow()
    q = RATINGS[rating]
    card.review_count = (card.review_count or 0) + 1
    card.last_reviewed_at = now
    card.ease = max(1.3, (card.ease or 2.5) + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
    if q < 3:
        card.repetitions = 0
        card.lapses = (card.lapses or 0) + 1
        card.interval_days = 0
        card.due_at = now + timedelta(minutes=10)  # see it again this session
        return card
    card.repetitions = (card.repetitions or 0) + 1
    if card.repetitions == 1:
        interval = 1
    elif card.repetitions == 2:
        interval = 6 if q >= 4 else 3
    else:
        interval = round((card.interval_days or 1) * card.ease)
    if q == 3:
        interval = max(1, round(interval * 0.8))
    if q == 5:
        interval = round(interval * 1.3) or 1
    card.interval_days = max(1, interval)
    card.due_at = now + timedelta(days=card.interval_days)
    return card


def due_cards(cards: list[Card], now: datetime | None = None) -> list[Card]:
    now = now or utcnow()
    return sorted((c for c in cards if c.due_at is None or c.due_at <= now), key=lambda c: (c.due_at or now, c.position))
