"""Thin Composio REST client: the calls Homework Hatch makes, over keep-alive HTTP sessions.

Stands in for the parts of the `composio` SDK that integrations.py uses (same method names and
return shapes, checked against the real API), without the SDK's ~1 s / 140 MB import on every
worker after every restart, its extra schema request before each tool's first use, or its
telemetry calls. Timeouts are short (5 s to connect, 30 s to answer); reads retry once.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import requests
from requests.adapters import HTTPAdapter

BASE = "https://backend.composio.dev/api/v3.1"


class ComposioHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"{status}: {body[:300]}")
        self.status = status


class MultipleConnectedAccounts(ComposioHTTPError):  # name matched by integrations.connect_url
    pass


def _ns(obj):
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(v) for v in obj]
    return obj


class ComposioREST:
    def __init__(self, api_key: str, base_url: str = BASE, timeout=(5, 30), toolkit_versions: dict | None = None):
        self._base, self._timeout, self._versions = base_url.rstrip("/"), timeout, toolkit_versions or {}
        self._local = threading.local()  # requests.Session isn't guaranteed thread-safe; one per thread
        self._headers = {"x-api-key": api_key, "accept": "application/json"}
        self.auth_configs = SimpleNamespace(list=self._auth_list, create=self._auth_create)
        self.connected_accounts = SimpleNamespace(link=self._link, list=self._list, delete=self._delete,
                                                  revoke=self._revoke)
        self.tools = SimpleNamespace(execute=self._execute)

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = self._local.s = requests.Session()
            s.headers.update(self._headers)
            s.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=8, max_retries=0))
        return s

    def _call(self, method: str, path: str, *, params=None, json=None, idempotent=True):
        tries = 2 if idempotent else 1
        for attempt in range(tries):
            try:
                r = self._session().request(method, self._base + path, params=params, json=json, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt + 1 < tries:
                    continue
                raise
            if r.status_code >= 500 and attempt + 1 < tries:
                continue
            if r.status_code >= 400:
                raise ComposioHTTPError(r.status_code, r.text)
            return r.json() if r.content else {}

    # auth configs
    def _auth_list(self, toolkit_slug: str):
        return _ns(self._call("GET", "/auth_configs", params={"toolkit_slug": toolkit_slug}))

    def _auth_create(self, toolkit: str, options: dict):
        body = {"toolkit": {"slug": toolkit}, "auth_config": options}
        return _ns(self._call("POST", "/auth_configs", json=body, idempotent=False)).auth_config

    # connected accounts
    def _list(self, user_ids, toolkit_slugs, statuses):
        return _ns(self._call("GET", "/connected_accounts",
                              params={"user_ids": user_ids, "toolkit_slugs": toolkit_slugs, "statuses": statuses}))

    def _link(self, user_id: str, auth_config_id: str, callback_url: str | None = None):
        existing = self._call("GET", "/connected_accounts", params={"user_ids": [user_id], "statuses": ["ACTIVE"],
                                                                     "auth_config_ids": [auth_config_id]})
        if existing.get("items"):
            raise MultipleConnectedAccounts(409, "already connected")
        body = {"auth_config_id": auth_config_id, "user_id": user_id}
        if callback_url:
            body["callback_url"] = callback_url
        return _ns(self._call("POST", "/connected_accounts/link", json=body, idempotent=False))

    def _delete(self, account_id: str):
        return self._call("DELETE", f"/connected_accounts/{account_id}")

    def _revoke(self, account_id: str):
        return self._call("POST", f"/connected_accounts/{account_id}/revoke", idempotent=False)

    # tools
    def _execute(self, slug: str, arguments: dict, user_id: str | None = None, **_ignored):
        toolkit = slug.split("_", 1)[0].lower()
        body = {"user_id": user_id, "arguments": arguments, "version": self._versions.get(toolkit, "latest")}
        try:
            return self._call("POST", f"/tools/execute/{slug}", json=body, idempotent=False)
        except ComposioHTTPError as exc:  # the SDK returns HTTP 4xx tool errors as exceptions too
            return {"successful": False, "error": str(exc), "data": {}}
