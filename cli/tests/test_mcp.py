"""Tests for the MCP/OpenAPI surface (C5)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import library, mcp_server, registry  # noqa: E402
from loom.util import LoomError  # noqa: E402


def test_rpc_initialize_and_tools_list():
    init = mcp_server._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "loom-library"
    tools = mcp_server._rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in tools["result"]["tools"]]
    assert names == ["loom_search_apps", "loom_describe_app", "loom_invoke"]


def test_initialized_notification_returns_no_response():
    assert mcp_server._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_invoke_refuses_private_app(monkeypatch):
    monkeypatch.setattr(library, "get", lambda n: {"name": n, "url": "http://x"})
    monkeypatch.setattr(registry, "get", lambda n: {"name": n, "access": "private"})
    with pytest.raises(LoomError, match="private"):
        mcp_server._invoke("secret", "/")


def test_invoke_unknown_app_raises(monkeypatch):
    monkeypatch.setattr(library, "get", lambda n: None)
    monkeypatch.setattr(registry, "get", lambda n: None)
    with pytest.raises(LoomError, match="unknown app"):
        mcp_server._invoke("nope", "/")


# --- admin tools (loom mcp --admin) ----------------------------------------------

def test_admin_tools_hidden_without_flag():
    from loom import mcp_server
    resp = mcp_server._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "loom_deploy" not in names
    resp = mcp_server._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           cfg={}, admin=True)
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"loom_fleet", "loom_deploy", "loom_stop", "loom_start",
            "loom_remove", "loom_services", "loom_traffic"} <= names


def test_admin_call_refused_without_flag():
    from loom import mcp_server
    resp = mcp_server._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "loom_remove",
                                       "arguments": {"name": "x"}}})
    assert resp["result"]["isError"] is True
    assert "--admin" in resp["result"]["content"][0]["text"]


def test_admin_call_dispatches(monkeypatch):
    from loom import admin, mcp_server
    monkeypatch.setattr(admin, "stop_app", lambda cfg, n: None)
    resp = mcp_server._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "loom_stop",
                                       "arguments": {"name": "shop"}}},
                           cfg={}, admin=True)
    import json as j
    assert j.loads(resp["result"]["content"][0]["text"]) == {"ok": True}


def test_admin_serve_refuses_remote_host():
    import pytest
    from loom import mcp_server
    from loom.util import LoomError
    with pytest.raises(LoomError, match="loopback-only"):
        mcp_server.serve({}, host="0.0.0.0", admin=True)
