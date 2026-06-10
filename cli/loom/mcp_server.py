"""Project the Loom Library as a callable surface for agents.

Run with `loom mcp`. It serves three things off the live Library
(fleet/library.json) + registry:

  * MCP (Streamable HTTP / JSON-RPC 2.0) at  POST /mcp  — tools
    loom_search_apps / loom_describe_app / loom_invoke, and loom://app/{name}
    resources. Progressive disclosure: a few stable meta-tools over the whole
    fleet, not one tool per operation (which explodes context).
  * OpenAPI 3.1 at  GET /openapi.json  — for non-MCP HTTP agents.
  * A plain REST surface (GET /apps, /apps/{name}, /search, POST /invoke).

`loom_invoke` only proxies REGISTERED apps (never arbitrary URLs) and refuses
PRIVATE apps — the SSRF / tier-leak guard.
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import library, registry
from .util import LoomError, ok

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "loom_search_apps",
        "description": "Search the Loom fleet for apps/capabilities matching a query.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
            "required": ["query"],
        },
    },
    {
        "name": "loom_describe_app",
        "description": "Get an app's callable operations (method, path, schemas).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "loom_invoke",
        "description": "Call an operation on a fleet app. Only registered, non-private apps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {"type": "string"},
                "path": {"type": "string", "description": "operation path, e.g. /search?q=loom"},
                "method": {"type": "string", "default": "GET"},
                "body": {"type": "object"},
            },
            "required": ["app", "path"],
        },
    },
]


# --- the operations ------------------------------------------------------------

def _search(query: str, limit: int = 10) -> list:
    return [_compact(r) for r in library.search(query, limit=limit)]


def _compact(rec: dict) -> dict:
    return {
        "name": rec["name"], "description": rec.get("description", ""),
        "tags": rec.get("tags", []), "url": rec.get("public_url") or rec.get("url"),
        "access": rec.get("access"), "health": rec.get("health_status"),
        "operations": [o.get("id") for o in rec.get("operations", [])],
    }


def _describe(name: str) -> dict:
    rec = library.get(name)
    if rec is None:
        raise LoomError(f"unknown app '{name}'")
    return rec


def _invoke(app: str, path: str, method: str = "GET", body=None) -> dict:
    rec = library.get(app) or {}
    entry = registry.get(app)
    if not rec or not entry:
        raise LoomError(f"unknown app '{app}'")
    if entry.get("access") == "private":
        raise LoomError(f"'{app}' is private and cannot be invoked through the Library")
    base = (rec.get("url") or "").rstrip("/")
    if not base:
        raise LoomError(f"'{app}' has no reachable URL")
    if not path.startswith("/"):
        path = "/" + path
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(base + path, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.getcode()
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        raise LoomError(f"invoke failed: {e}")
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = raw
    return {"app": app, "path": path, "status": code, "body": parsed}


def _call_tool(name: str, args: dict):
    if name == "loom_search_apps":
        return _search(args["query"], int(args.get("limit", 10)))
    if name == "loom_describe_app":
        return _describe(args["name"])
    if name == "loom_invoke":
        return _invoke(args["app"], args["path"], args.get("method", "GET"), args.get("body"))
    raise LoomError(f"unknown tool '{name}'")


def _openapi(base_url: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Loom Library", "version": "1.0.0",
                 "description": "Discover and invoke apps on the Loom fleet."},
        "servers": [{"url": base_url}],
        "paths": {
            "/apps": {"get": {"operationId": "listApps", "summary": "List all fleet apps",
                              "responses": {"200": {"description": "apps"}}}},
            "/apps/{name}": {"get": {"operationId": "describeApp", "summary": "Describe an app",
                                     "parameters": [{"name": "name", "in": "path", "required": True,
                                                     "schema": {"type": "string"}}],
                                     "responses": {"200": {"description": "app"}}}},
            "/search": {"get": {"operationId": "searchApps", "summary": "Search the fleet",
                                "parameters": [{"name": "q", "in": "query", "required": True,
                                                "schema": {"type": "string"}}],
                                "responses": {"200": {"description": "matches"}}}},
            "/invoke": {"post": {"operationId": "invoke", "summary": "Call an app operation",
                                 "responses": {"200": {"description": "result"}}}},
        },
    }


# --- HTTP / JSON-RPC plumbing ---------------------------------------------------

def _rpc(req: dict) -> dict | None:
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    def result(r):
        return {"jsonrpc": "2.0", "id": mid, "result": r}

    if method == "initialize":
        return result({"protocolVersion": PROTOCOL_VERSION,
                       "capabilities": {"tools": {}, "resources": {}},
                       "serverInfo": {"name": "loom-library", "version": "1.0.0"}})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notification, no response
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        try:
            out = _call_tool(params.get("name"), params.get("arguments") or {})
            return result({"content": [{"type": "text", "text": json.dumps(out)}]})
        except LoomError as e:
            return result({"isError": True, "content": [{"type": "text", "text": str(e)}]})
    if method == "resources/list":
        res = [{"uri": f"loom://app/{r['name']}", "name": r["name"],
                "description": r.get("description", ""), "mimeType": "application/json"}
               for r in library.all_records()]
        return result({"resources": res})
    if method == "resources/read":
        uri = params.get("uri", "")
        name = uri.split("loom://app/", 1)[-1]
        rec = library.get(name)
        if rec is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown resource"}}
        return result({"contents": [{"uri": uri, "mimeType": "application/json",
                                     "text": json.dumps(rec)}]})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def make_handler(base_url: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # quiet

        def _send(self, code, obj, ctype="application/json"):
            body = (json.dumps(obj) if ctype == "application/json" else obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/openapi.json":
                return self._send(200, _openapi(base_url))
            if u.path == "/apps":
                return self._send(200, [_compact(r) for r in library.all_records()])
            if u.path.startswith("/apps/"):
                try:
                    return self._send(200, _describe(u.path.split("/apps/", 1)[1]))
                except LoomError as e:
                    return self._send(404, {"error": str(e)})
            if u.path == "/search":
                return self._send(200, _search(q.get("q", [""])[0], int(q.get("limit", [10])[0])))
            if u.path in ("/", "/healthz", "/health"):
                return self._send(200, {"status": "ok", "service": "loom-library"})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            if self.path == "/invoke":
                try:
                    return self._send(200, _invoke(payload["app"], payload["path"],
                                                   payload.get("method", "GET"), payload.get("body")))
                except (LoomError, KeyError) as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/mcp":
                resp = _rpc(payload)
                if resp is None:
                    return self._send(202, {})  # notification accepted
                return self._send(200, resp)
            return self._send(404, {"error": "not found"})

    return Handler


def serve(cfg: dict, host: str = "127.0.0.1", port: int = 7878) -> None:
    base_url = f"http://{host}:{port}"
    httpd = ThreadingHTTPServer((host, port), make_handler(base_url))
    ok(f"loom-library MCP + OpenAPI on {base_url}  (POST /mcp · GET /openapi.json)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
