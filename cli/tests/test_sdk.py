"""Tests for the Loom Python SDK — the identity() header parser (no network)
and the analytics client (stub HTTP server, no real fleet)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# The SDK lives at sdk/python/loom_sdk.py (vendored into apps); import it directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

import loom_sdk  # noqa: E402


def test_identity_from_forward_auth_headers():
    ident = loom_sdk.identity({
        "Remote-User": "alice",
        "Remote-Email": "alice@example.com",
        "Remote-Name": "Alice A.",
        "Remote-Groups": "admins, beta",
    })
    assert ident.is_authenticated
    assert ident.user == "alice"
    assert ident.email == "alice@example.com"
    assert ident.name == "Alice A."
    assert ident.groups == ["admins", "beta"]  # split + stripped


def test_identity_falls_back_to_x_forwarded():
    ident = loom_sdk.identity({
        "X-Forwarded-User": "bob",
        "X-Forwarded-Email": "bob@example.com",
        "X-Forwarded-Groups": "users",
    })
    assert ident.user == "bob"
    assert ident.email == "bob@example.com"
    assert ident.groups == ["users"]


def test_identity_empty_for_public_request():
    ident = loom_sdk.identity({})
    assert not ident.is_authenticated
    assert ident.user == "" and ident.email == "" and ident.groups == []


def test_identity_tolerates_objects_without_get():
    # a mapping whose .get raises must not blow up the parser
    class Weird:
        def get(self, _):
            raise RuntimeError("no")
    ident = loom_sdk.identity(Weird())
    assert not ident.is_authenticated


def _stub_server(record):
    """A one-shot analytics stub that records what the client sent."""
    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            record["path"] = self.path
            record["body"] = json.loads(self.rfile.read(n))
            record["authorization"] = self.headers.get("Authorization")
            record["x_loom_app"] = self.headers.get("X-Loom-App")
            b = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _provision_analytics(monkeypatch, url):
    monkeypatch.setenv("LOOM_ANALYTICS_URL", url)
    monkeypatch.setenv("LOOM_ANALYTICS_TOKEN", "tok-123")
    monkeypatch.setenv("LOOM_APP", "myapp")


def test_analytics_track_posts_event_with_auth_headers(monkeypatch):
    record = {}
    srv = _stub_server(record)
    _provision_analytics(monkeypatch, f"http://127.0.0.1:{srv.server_port}")
    try:
        assert loom_sdk.analytics().track("page_view", {"path": "/x"}) is True
    finally:
        srv.shutdown()
    assert record["path"] == "/track"
    assert record["body"] == {"event": "page_view", "props": {"path": "/x"}}
    assert record["authorization"] == "Bearer tok-123"
    assert record["x_loom_app"] == "myapp"


def test_analytics_track_swallows_connection_failure(monkeypatch):
    # nothing listening on this port — fire-and-forget must not raise
    _provision_analytics(monkeypatch, "http://127.0.0.1:1")
    assert loom_sdk.analytics().track("page_view") is False
