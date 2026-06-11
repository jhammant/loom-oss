"""loom-llm: the no-key LLM gateway shared service.

Apps `consumes: [llm]` and call this (Loom injects the URL + an HMAC token); the
app never holds a provider key. The gateway verifies the caller, maps a small set
of model aliases, meters tokens against a PER-APP cap (the one guardrail that
matters for LLMs), and proxies to Anthropic — or returns a stub if no
ANTHROPIC_API_KEY is set, so the wiring works before you add a key. Stdlib only.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 8095))
SECRET = os.environ.get("LOOM_SERVICE_SECRET", "")
SERVICE = os.environ.get("LOOM_SERVICE", "llm")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CAP_TOKENS = int(os.environ.get("LOOM_LLM_CAP_TOKENS", "200000"))  # per-app, cumulative

# Curated model set (alias -> Anthropic model id). A full id is also accepted.
MODELS = {
    "fast": "claude-haiku-4-5-20251001",
    "smart": "claude-sonnet-4-6",
    "frontier": "claude-opus-4-8",
}

db = sqlite3.connect("/tmp/llm.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS usage(app TEXT PRIMARY KEY, tokens INTEGER NOT NULL DEFAULT 0)")
db.commit()


def caller(headers):
    auth = headers.get("Authorization", "")
    app = headers.get("X-Loom-App", "")
    if not SECRET or not app or not auth.startswith("Bearer "):
        return None
    expect = hmac.new(SECRET.encode(), f"{app}:{SERVICE}".encode(), hashlib.sha256).hexdigest()
    return app if hmac.compare_digest(auth[7:], expect) else None


def used(app):
    row = db.execute("SELECT tokens FROM usage WHERE app=?", (app,)).fetchone()
    return row[0] if row else 0


def add_usage(app, n):
    db.execute("INSERT INTO usage(app,tokens) VALUES(?,?) "
               "ON CONFLICT(app) DO UPDATE SET tokens=tokens+?", (app, n, n))
    db.commit()


def call_anthropic(model, messages, max_tokens):
    system = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    body = {"model": model, "max_tokens": max_tokens, "messages": convo}
    if system:
        body["system"] = system
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    u = out.get("usage", {})
    return text, {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send(200, {"status": "ok", "live": bool(API_KEY)})
        if u.path == "/v1/models":
            return self.send(200, {"models": list(MODELS) + ["<any anthropic model id>"]})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        who = caller(self.headers)
        if who is None:
            return self.send(401, {"error": "unauthorized"})
        if urlparse(self.path).path != "/v1/chat":
            return self.send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send(400, {"error": "invalid json"})
        if used(who) >= CAP_TOKENS:
            return self.send(402, {"error": "token_cap_exceeded", "used": used(who), "cap": CAP_TOKENS})
        model = MODELS.get(d.get("model", "fast"), d.get("model", "fast"))
        messages = d.get("messages") or []
        max_tokens = int(d.get("max_tokens", 1024))
        if not API_KEY:  # stub mode — wiring works before a key is added
            last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
            return self.send(200, {"model": model, "stub": True, "app": who,
                                   "text": f"[stub: set ANTHROPIC_API_KEY to go live] you said: {last}",
                                   "usage": {"input_tokens": 0, "output_tokens": 0}})
        try:
            text, usage = call_anthropic(model, messages, max_tokens)
        except urllib.error.HTTPError as e:
            return self.send(502, {"error": "provider_error", "status": e.code,
                                   "detail": e.read().decode("utf-8", "replace")[:300]})
        except Exception as e:
            return self.send(502, {"error": f"provider_unreachable: {e}"})
        add_usage(who, usage["input_tokens"] + usage["output_tokens"])
        return self.send(200, {"model": model, "stub": False, "app": who, "text": text,
                               "usage": usage, "total_tokens_used": used(who)})


print(f"[loom-llm] gateway on :{PORT} (live={bool(API_KEY)}, cap={CAP_TOKENS}/app)")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
