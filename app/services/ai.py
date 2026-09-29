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
    input_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


@dataclass
class StreamHandle:
    """Iterate for text deltas; `result` is filled in once the stream ends."""

    chunks: Iterator[str]
    result: AIResult | None = field(default=None)

    def __iter__(self):
        return self.chunks


class Provider(Protocol):
    def complete(self, *, system: str, messages: list[dict], max_tokens: int, effort: str,
                 schema: dict | None = None) -> AIResult: ...

    def stream(self, *, system: str, messages: list[dict], max_tokens: int, effort: str) -> StreamHandle: ...


class AnthropicProvider:
    def __init__(self, model: str):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def _params(self, system, messages, max_tokens, effort, schema=None) -> dict:
        output_config: dict = {"effort": effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        return dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                    output_config=output_config, betas=[FALLBACK_BETA], fallbacks="default")

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

    def complete(self, *, system, messages, max_tokens, effort, schema=None) -> AIResult:
        try:
            message = self.client.beta.messages.create(**self._params(system, messages, max_tokens, effort, schema))
        except self.anthropic.APIError as exc:
            raise self._translate(exc) from exc
        if message.stop_reason == "refusal":
            raise AIError("The AI declined this request. Try rephrasing it.")
        if message.stop_reason == "max_tokens" and schema is not None:
            raise AIError("The response was cut off. Try asking for fewer items.")
        return AIResult(self._text(message), message.usage.input_tokens, message.usage.output_tokens, message.model)

    def stream(self, *, system, messages, max_tokens, effort) -> StreamHandle:
        handle = StreamHandle(chunks=iter(()))

        def run():
            try:
                with self.client.beta.messages.stream(**self._params(system, messages, max_tokens, effort)) as s:
                    for text in s.text_stream:
                        yield text
                    final = s.get_final_message()
            except self.anthropic.APIError as exc:
                raise self._translate(exc) from exc
            if final.stop_reason == "refusal":
                raise AIError("The AI declined to answer that. Try rephrasing.")
            handle.result = AIResult(self._text(final), final.usage.input_tokens, final.usage.output_tokens, final.model)

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


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def used_this_month(user_id: int) -> int:
    return db.session.scalar(select(func.count(AIUsage.id)).where(
        AIUsage.user_id == user_id, AIUsage.created_at >= month_start())) or 0


def remaining(user: User) -> int | None:
    """None means unlimited."""
    if user.is_admin:
        return None
    return max(0, billing.plan_for(user).ai_monthly - used_this_month(user.id))


def check_quota(user: User) -> None:
    left = remaining(user)
    if left is not None and left <= 0:
        raise QuotaExceeded(f"You've used all {billing.plan_for(user).ai_monthly} AI actions on your plan this month. "
                            "Upgrade for more, or wait until next month.")


def record(user: User, kind: str, result: AIResult | None) -> None:
    db.session.add(AIUsage(user_id=user.id, kind=kind, model=result.model if result else None,
                           input_tokens=result.input_tokens if result else 0,
                           output_tokens=result.output_tokens if result else 0))


def complete(user: User, kind: str, *, system: str, prompt: str, max_tokens: int = 16000, effort: str = "medium",
             schema: dict | None = None) -> AIResult:
    check_quota(user)
    result = provider().complete(system=system, messages=[{"role": "user", "content": prompt}],
                                 max_tokens=max_tokens, effort=effort, schema=schema)
    record(user, kind, result)
    db.session.commit()
    return result


def complete_json(user: User, kind: str, *, system: str, prompt: str, schema: dict, effort: str = "medium",
                  max_tokens: int = 16000, validate: Callable[[dict], dict] | None = None) -> dict:
    result = complete(user, kind, system=system, prompt=prompt, schema=schema, effort=effort, max_tokens=max_tokens)
    try:
        data = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise AIError("The AI returned something unreadable. Try again.") from exc
    return validate(data) if validate else data
