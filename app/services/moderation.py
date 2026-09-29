"""Class-chat moderation: masks profanity, blocks slurs and threats, rate-limits spam."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select

from ..extensions import db
from ..models import ChatMessage, utcnow

MAX_LENGTH = 1000
RATE_WINDOW = timedelta(seconds=15)
RATE_LIMIT = 6

# Masked (the message still posts).
# Words with innocent classroom uses ("Philip K. Dick", "damn" in a quoted text) are left alone.
_PROFANITY = ["fuck", "fucking", "shit", "bitch", "bastard", "pussy", "cunt", "asshole", "motherfucker", "bullshit",
              "slut", "whore"]
# Blocked outright: hate slurs and violent threats have no place in a class room.
# (Terms with common academic meanings — "retarded potential", "chink in the armor" — are not listed.)
_BLOCKED = [r"n[i1!]gg(?:er|a)", r"\bf[a@]gg?(?:ot)?s?\b", r"\bk[i1]ke\b", r"\bsp[i1]c\b", r"\btr[a@]nny\b",
            r"\bkill (?:yo)?urself\b", r"\bkys\b", r"\bi(?:'ll| will) (?:kill|shoot|stab) (?:you|u)\b"]

_profanity_re = re.compile(r"\b(" + "|".join(re.escape(w) for w in _PROFANITY) + r")(?:s|ed|ing|er)?\b", re.I)
_blocked_re = re.compile("|".join(_BLOCKED), re.I)


class Rejected(ValueError):
    pass


def clean(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise Rejected("Message is empty.")
    if len(body) > MAX_LENGTH:
        raise Rejected(f"Keep messages under {MAX_LENGTH} characters.")
    if _blocked_re.search(body):
        raise Rejected("That message breaks the community rules and wasn't sent.")
    return _profanity_re.sub(lambda m: m.group(0)[0] + "*" * (len(m.group(0)) - 1), body)


def check_rate(user_id: int) -> None:
    recent = db.session.scalar(select(func.count(ChatMessage.id)).where(
        ChatMessage.user_id == user_id, ChatMessage.created_at >= utcnow() - RATE_WINDOW)) or 0
    if recent >= RATE_LIMIT:
        raise Rejected("Slow down a little — you're sending messages too fast.")
