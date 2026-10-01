"""The thin Composio client sends the same requests as Composio's SDK (checked against the real API)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.composio_rest import ComposioREST, MultipleConnectedAccounts


@pytest.fixture
def fake_composio():
    seen = []
    replies = {}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            seen.append({"method": self.command, "path": url.path, "query": parse_qs(url.query), "body": body,
                         "key": self.headers.get("x-api-key")})
            status, payload = replies.get((self.command, url.path), (200, {"items": []}))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = do_DELETE = _reply

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield ComposioREST("ak_test", base_url=f"http://127.0.0.1:{server.server_port}/api/v3.1"), seen, replies
    server.shutdown()


def test_requests_match_the_sdk(fake_composio):
    client, seen, replies = fake_composio
    replies[("GET", "/api/v3.1/auth_configs")] = (200, {"items": [{"id": "ac_1", "status": "ENABLED", "is_composio_managed": True}]})
    assert client.auth_configs.list(toolkit_slug="googlecalendar").items[0].id == "ac_1"
    client.connected_accounts.list(user_ids=["hh-1"], toolkit_slugs=["googledrive"], statuses=["ACTIVE"])
    replies[("POST", "/api/v3.1/connected_accounts/link")] = (200, {"redirect_url": "https://composio.test/go"})
    assert client.connected_accounts.link("hh-1", "ac_1", callback_url="https://hh.test/cb").redirect_url == "https://composio.test/go"
    replies[("POST", "/api/v3.1/tools/execute/GOOGLEDRIVE_FIND_FILE")] = (200, {"successful": True, "data": {"files": []}, "error": None})
    out = client.tools.execute("GOOGLEDRIVE_FIND_FILE", {"q": "x"}, user_id="hh-1", dangerously_skip_version_check=True)
    assert out["successful"] and out["data"] == {"files": []}

    assert all(r["key"] == "ak_test" for r in seen)
    assert seen[0]["path"] == "/api/v3.1/auth_configs" and seen[0]["query"] == {"toolkit_slug": ["googlecalendar"]}
    assert seen[1]["query"] == {"user_ids": ["hh-1"], "toolkit_slugs": ["googledrive"], "statuses": ["ACTIVE"]}
    assert seen[2]["query"] == {"user_ids": ["hh-1"], "statuses": ["ACTIVE"], "auth_config_ids": ["ac_1"]}, "link checks first"
    assert seen[3]["body"] == {"auth_config_id": "ac_1", "user_id": "hh-1", "callback_url": "https://hh.test/cb"}
    assert seen[4]["body"] == {"user_id": "hh-1", "arguments": {"q": "x"}, "version": "latest"}


def test_errors_look_like_the_sdk(fake_composio):
    client, seen, replies = fake_composio
    replies[("GET", "/api/v3.1/connected_accounts")] = (200, {"items": [{"id": "ca_1"}]})
    with pytest.raises(MultipleConnectedAccounts):  # integrations.connect_url treats this as "already connected"
        client.connected_accounts.link("hh-1", "ac_1")
    replies[("POST", "/api/v3.1/tools/execute/GOOGLECALENDAR_CREATE_EVENT")] = (
        404, {"error": {"message": "No connected account found for user ID hh-1"}})
    out = client.tools.execute("GOOGLECALENDAR_CREATE_EVENT", {}, user_id="hh-1")
    assert out["successful"] is False and "no connected account" in out["error"].lower()
    replies[("GET", "/api/v3.1/auth_configs")] = (500, {})
    with pytest.raises(Exception):
        client.auth_configs.list(toolkit_slug="googlecalendar")
    assert sum(r["path"] == "/api/v3.1/auth_configs" for r in seen) == 2, "reads retry once on a server error"
