"""Google Calendar and Google Drive through Composio.

Composio runs the Google sign-in and holds the Google tokens; the app keeps only whether a
student is connected (Integration rows) and calls Google through Composio tools. Each
student is the Composio user "hh-<user id>". Needs COMPOSIO_API_KEY; without it the
integrations are hidden.

The auth configs (Composio's per-app login setups) are found or created on first use with
Composio-managed Google credentials, unless COMPOSIO_AUTH_CONFIGS names them.
"""

from __future__ import annotations

from flask import current_app
from sqlalchemy import select

from ..extensions import db
from ..models import Integration, User

TOOLKITS = {"calendar": "googlecalendar", "drive": "googledrive"}
LABELS = {"calendar": "Google Calendar", "drive": "Google Drive"}


class IntegrationError(Exception):
    pass


class NotConnected(IntegrationError):
    pass


def available() -> bool:
    return bool(current_app.config.get("COMPOSIO_API_KEY")) or "hh_composio" in current_app.extensions


def client():
    app = current_app
    composio = app.extensions.get("hh_composio")
    if composio is None:
        from composio import Composio

        versions = app.config.get("COMPOSIO_TOOLKIT_VERSIONS") or None
        composio = Composio(api_key=app.config["COMPOSIO_API_KEY"], toolkit_versions=versions)
        app.extensions["hh_composio"] = composio
    return composio


def composio_user(user: User) -> str:
    return f"hh-{user.id}"


def get(user: User, kind: str, create: bool = False) -> Integration | None:
    row = db.session.scalar(select(Integration).where(Integration.user_id == user.id, Integration.kind == kind))
    if row is None and create:
        row = Integration(user_id=user.id, kind=kind, connected=False, settings={})
        db.session.add(row)
        db.session.flush()
    return row


def connected(user: User, kind: str) -> bool:
    row = get(user, kind)
    return bool(row and row.connected)


def auth_config_id(kind: str) -> str:
    toolkit = TOOLKITS[kind]
    configured = (current_app.config.get("COMPOSIO_AUTH_CONFIGS") or {}).get(toolkit)
    if configured:
        return configured
    cache = current_app.extensions.setdefault("hh_composio_auth", {})
    if toolkit not in cache:
        try:
            items = list(getattr(client().auth_configs.list(toolkit_slug=toolkit), "items", []) or [])
            enabled = [a for a in items if str(getattr(a, "status", "ENABLED")).upper() != "DISABLED"]
            found = next((a for a in enabled if getattr(a, "is_composio_managed", False)), None) or \
                (enabled[0] if enabled else None)
            if found is None:
                found = client().auth_configs.create(
                    toolkit, {"type": "use_composio_managed_auth", "name": f"Homework Hatch {LABELS[kind]}"})
        except Exception as exc:
            raise IntegrationError(f"Couldn't set up {LABELS[kind]} with Composio: {exc}") from exc
        cache[toolkit] = found.id
    return cache[toolkit]


def _active_accounts(user: User, kind: str) -> list:
    result = client().connected_accounts.list(user_ids=[composio_user(user)], toolkit_slugs=[TOOLKITS[kind]],
                                              statuses=["ACTIVE"])
    return list(getattr(result, "items", []) or [])


def connect_url(user: User, kind: str, callback_url: str) -> str | None:
    """Where to send the student to connect Google; None when they're already connected."""
    try:
        request = client().connected_accounts.link(composio_user(user), auth_config_id(kind), callback_url=callback_url)
    except IntegrationError:
        raise
    except Exception as exc:
        if "MultipleConnectedAccounts" in type(exc).__name__:
            refresh(user, kind)
            return None
        raise IntegrationError(f"Couldn't start connecting {LABELS[kind]}: {exc}") from exc
    return request.redirect_url


def refresh(user: User, kind: str) -> bool:
    """Ask Composio whether the student is connected, and remember the answer."""
    try:
        is_connected = bool(_active_accounts(user, kind))
    except Exception as exc:
        raise IntegrationError(f"Couldn't reach Composio: {exc}") from exc
    row = get(user, kind, create=True)
    row.connected = is_connected
    if is_connected:
        row.last_error = None
    db.session.commit()
    return is_connected


def disconnect(user: User, kind: str) -> None:
    try:
        for account in _active_accounts(user, kind):
            try:
                client().connected_accounts.revoke(account.id)  # also revokes the Google tokens
            except Exception:
                pass
            client().connected_accounts.delete(account.id)
    except Exception as exc:
        raise IntegrationError(f"Couldn't disconnect {LABELS[kind]}: {exc}") from exc
    row = get(user, kind)
    if row:
        row.connected = False
        db.session.commit()


def execute(user: User, kind: str, slug: str, arguments: dict) -> dict:
    """Run one Composio tool as the student; returns the tool's `data`."""
    kwargs = {"user_id": composio_user(user)}
    if not current_app.config.get("COMPOSIO_TOOLKIT_VERSIONS"):
        kwargs["dangerously_skip_version_check"] = True  # latest tool versions
    try:
        response = client().tools.execute(slug, arguments, **kwargs)
    except Exception as exc:
        if "connected account" in str(exc).lower() or "ConnectedAccountNotFound" in type(exc).__name__:
            _mark_disconnected(user, kind, str(exc))
            raise NotConnected(f"{LABELS[kind]} isn't connected.") from exc
        raise IntegrationError(f"{LABELS[kind]} request failed: {exc}") from exc
    response = response if isinstance(response, dict) else getattr(response, "__dict__", {})
    if not response.get("successful"):
        error = str(response.get("error") or "unknown error")
        if any(s in error.lower() for s in ("no connected account", "not connected", "invalid_grant", "unauthorized")):
            _mark_disconnected(user, kind, error)
            raise NotConnected(f"{LABELS[kind]} needs to be connected again.")
        raise IntegrationError(f"{LABELS[kind]}: {error[:300]}")
    return response.get("data") or {}


def _mark_disconnected(user: User, kind: str, error: str) -> None:
    row = get(user, kind)
    if row:
        row.connected = False
        row.last_error = error[:500]
        db.session.commit()
