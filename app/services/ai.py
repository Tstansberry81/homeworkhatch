"""Claude access for every AI feature, with per-plan monthly quotas.

All calls go through `complete()` (one response, optionally JSON-schema constrained) or
`stream()` (tutor chat). Requests opt into server-side refusal fallbacks
(`fallbacks="default"`): if a safety classifier declines, the API re-runs the request on
Anthropic's recommended fallback model instead of failing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Protocol

from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models import AIUsage, User, utcnow
from . import billing

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(Exception):
    """Shown to the student as-is."""


class AIUnavailable(AIError):
    pass


class QuotaExceeded(AIError):
    pass


@dataclass
class AIResult:
    text: str
    input_tokens: int = 0       # uncached input
    output_tokens: int = 0      # includes thinking
    model: str | None = None
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


# USD per million tokens: (input, output, cache read). Cache writes (5-minute TTL) cost 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
}


def cost_usd(model: str | None, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    name = (model or "").split("@")[0]
    price = next((p for key, p in PRICES.items() if name.startswith(key)), PRICES["claude-opus-5-5"])  # ids may carry a date
    return (input_tokens * price[0] + output_tokens * price[1] + cache_write * price[0] * 1.25
            + cache_read * price[2]) / 1e6


@dataclass
class StreamHandle:
    """Iterate for text deltas; `result` is filled in once the stream ends."""

    chunks: Iterator[str]
    result: AIResult | None = field(default=None)

    def __iter__(self):
        return self.chunks


class Provider(Protocol):
    def complete(self, *, system, messages: list[dict], max_tokens: int, effort: str,
                 schema: dict | None = None, model: str | None = None) -> AIResult: ...

    def stream(self, *, system, messages: list[dict], max_tokens: int, effort: str,
               model: str | None = None) -> StreamHandle: ...


class AnthropicProvider:
    def __init__(self, model: str):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def _params(self, system, messages, max_tokens, effort, schema=None, model=None) -> dict:
        model = model or self.model
        haiku = model.startswith("claude-haiku")
        output_config: dict = {} if haiku else {"effort": effort}  # Haiku 4.5 has no effort setting
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        params = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if output_config:
            params["output_config"] = output_config
        if not haiku:  # refusal fallbacks exist for the current Opus/Sonnet/Fable models
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        return params

    @staticmethod
    def _result(message, text: str) -> AIResult:
        u = message.usage
        return AIResult(text, u.input_tokens, u.output_tokens, message.model,
                        getattr(u, "cache_creation_input_tokens", 0) or 0, getattr(u, "cache_read_input_tokens", 0) or 0)

    def _translate(self, exc: Exception) -> AIError:
        a = self.anthropic
        if isinstance(exc, a.AuthenticationError):
            return AIUnavailable("The AI service isn't configured correctly (authentication failed).")
        if isinstance(exc, a.RateLimitError):
            return AIError("The AI is busy right now. Try again in a minute.")
        if isinstance(exc, a.BadRequestError):
            current_app.logger.warning("AI bad request: %s", exc)
            return AIError("That request was too large or malformed for the AI. Try less material.")
        if isinstance(exc, a.APIStatusError):
            return AIError("The AI service had a problem. Try again shortly.")
        if isinstance(exc, a.APIConnectionError):
            return AIError("Couldn't reach the AI service. Check the server's network.")
        return AIError("Something went wrong talking to the AI.")

    @staticmethod
    def _text(message) -> str:
        return "".join(block.text for block in message.content if block.type == "text")

    def complete(self, *, system, messages, max_tokens, effort, schema=None, model=None) -> AIResult:
        try:
            message = self.client.beta.messages.create(**self._params(system, messages, max_tokens, effort, schema, model))
        except self.anthropic.APIError as exc:
            raise self._translate(exc) from exc
        if message.stop_reason == "refusal":
            raise AIError("The AI declined this request. Try rephrasing it.")
        if message.stop_reason == "max_tokens" and schema is not None:
            raise AIError("The response was cut off. Try asking for fewer items.")
        return self._result(message, self._text(message))

    def stream(self, *, system, messages, max_tokens, effort, model=None) -> StreamHandle:
        handle = StreamHandle(chunks=iter(()))

        def run():
            try:
                with self.client.beta.messages.stream(**self._params(system, messages, max_tokens, effort, model=model)) as s:
                    for text in s.text_stream:
                        yield text
                    final = s.get_final_message()
            except self.anthropic.APIError as exc:
                raise self._translate(exc) from exc
            if final.stop_reason == "refusal":
                raise AIError("The AI declined to answer that. Try rephrasing.")
            handle.result = self._result(final, self._text(final))

        handle.chunks = run()
        return handle


# ---------------------------------------------------------------- selection & quotas


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()  # `ant auth login` profile (local dev)


def available() -> bool:
    app = current_app
    if "hh_ai" in app.extensions:
        return True
    return bool(app.config.get("AI_ENABLED")) and _has_credentials()


def provider() -> Provider:
    app = current_app
    if "hh_ai" in app.extensions:  # injected (tests, or a custom provider)
        return app.extensions["hh_ai"]
    if not available():
        raise AIUnavailable("AI features are turned off on this server (no ANTHROPIC_API_KEY).")
    cached = app.extensions.get("hh_ai_default")
    if cached is None:
        cached = AnthropicProvider(app.config["AI_MODEL"])
        app.extensions["hh_ai_default"] = cached
    return cached


def model_for(kind: str) -> str:
    """The model each feature uses (config AI_MODELS, falling back to AI_MODEL)."""
    return (current_app.config.get("AI_MODELS") or {}).get(kind) or current_app.config["AI_MODEL"]


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def used_this_month(user_id: int) -> int:
    return db.session.scalar(select(func.count(AIUsage.id)).where(
        AIUsage.user_id == user_id, AIUsage.created_at >= month_start())) or 0


def used_ever(user_id: int) -> int:
    return db.session.scalar(select(func.count(AIUsage.id)).where(AIUsage.user_id == user_id)) or 0


def used(user: User) -> int:
    """AI actions counted against the user's plan: this month, or ever for the free trial."""
    return used_this_month(user.id) if billing.plan_for(user).monthly else used_ever(user.id)


def remaining(user: User) -> int | None:
    """None means unlimited."""
    if user.is_admin:
        return None
    return max(0, billing.plan_for(user).ai_actions - used(user))


def _over_limit_message(plan: billing.Plan) -> str:
    if not plan.monthly:
        return (f"You've used your {plan.ai_actions} free AI actions. A Semester Pass or Plus gives you "
                f"{billing.PLANS['plus'].ai_actions} a month. Everything that doesn't use AI stays free.")
    return f"You've used all {plan.ai_actions} AI actions this month. They reset on the 1st."


def reserve(user: User, kind: str) -> AIUsage:
    """Claim one AI action before calling the model.

    The row is written first and the limit checked after, so concurrent requests can't all
    pass a check-then-record window; an over-limit request removes its own claim. Claims are
    kept for aborted or failed-mid-way answers (they cost tokens) and released only when the
    call fails before producing anything.
    """
    row = AIUsage(user_id=user.id, kind=kind)
    db.session.add(row)
    db.session.flush()
    if not user.is_admin:
        plan = billing.plan_for(user)
        if used(user) > plan.ai_actions:
            db.session.delete(row)
            db.session.commit()
            raise QuotaExceeded(_over_limit_message(plan))
    db.session.commit()
    return row


def finish(row: AIUsage, result: AIResult | None) -> None:
    if result is not None:
        row.model, row.input_tokens, row.output_tokens = result.model, result.input_tokens, result.output_tokens
        row.cache_write_tokens, row.cache_read_tokens = result.cache_write_tokens, result.cache_read_tokens
        row.cost_usd = cost_usd(result.model, result.input_tokens, result.output_tokens,
                                result.cache_write_tokens, result.cache_read_tokens)
    db.session.commit()


def release(row: AIUsage) -> None:
    db.session.delete(row)
    db.session.commit()


def complete(user: User, kind: str, *, system: str, prompt: str, max_tokens: int = 16000, effort: str = "medium",
             schema: dict | None = None) -> AIResult:
    row = reserve(user, kind)
    try:
        result = provider().complete(system=system, messages=[{"role": "user", "content": prompt}],
                                     max_tokens=max_tokens, effort=effort, schema=schema, model=model_for(kind))
    except AIError:
        release(row)
        raise
    finish(row, result)
    return result


def complete_json(user: User, kind: str, *, system: str, prompt: str, schema: dict, effort: str = "medium",
                  max_tokens: int = 16000, validate: Callable[[dict], dict] | None = None) -> dict:
    result = complete(user, kind, system=system, prompt=prompt, schema=schema, effort=effort, max_tokens=max_tokens)
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise AIError("The AI returned something unreadable. Try again.") from exc
    return validate(data) if validate else data
