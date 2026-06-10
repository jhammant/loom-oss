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
