"""loom-analytics: an event-tracking shared service for the fleet.

Verifies app-to-app calls via the HMAC token Loom provisions (no external deps).
Endpoints: GET /health, POST /track, GET /stats?app=&since=.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8090))
SECRET = os.environ.get("LOOM_SERVICE_SECRET", "")
SERVICE = os.environ.get("LOOM_SERVICE", "analytics")
DB_PATH = "/data/analytics.db" if os.path.isdir("/data") else "./analytics.db"

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS events("
           "app TEXT NOT NULL, event TEXT NOT NULL, props TEXT, ts INTEGER NOT NULL)")
db.commit()


def caller(headers):
    """Return the verified calling app, or None."""
    app = headers.get("X-Loom-App", "")
    auth = headers.get("Authorization", "")
    token = headers.get("X-Loom-Token", "") or (auth[7:] if auth.startswith("Bearer ") else "")
    if not SECRET or not app or not token:
        return None
    expect = hmac.new(SECRET.encode(), f"{app}:{SERVICE}".encode(), hashlib.sha256).hexdigest()
    return app if hmac.compare_digest(token, expect) else None


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

    def json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        if caller(self.headers) is None:
            return self.send(401, {"error": "unauthorized"})
        if u.path == "/stats":
            q = parse_qs(u.query)
            app = q.get("app", [""])[0]
            since = int(q.get("since", ["0"])[0] or 0)
            where, args = "ts>=?", [since]
            if app:
                where, args = "app=? AND ts>=?", [app, since]
            events = {e: n for e, n in db.execute(
                f"SELECT event, COUNT(*) FROM events WHERE {where} GROUP BY event", args)}
            apps = {a: n for a, n in db.execute(
                f"SELECT app, COUNT(*) FROM events WHERE {where} GROUP BY app", args)}
            return self.send(200, {"events": events, "apps": apps,
                                   "total": sum(events.values())})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        who = caller(self.headers)
        if who is None:
            return self.send(401, {"error": "unauthorized"})
        if u.path == "/track":
            d = self.json_body()
            event = d.get("event", "")
            if not event:
                return self.send(400, {"error": "missing event"})
            ts = int(d.get("ts") or time.time())
            db.execute("INSERT INTO events(app,event,props,ts) VALUES(?,?,?,?)",
                       (who, event, json.dumps(d.get("props") or {}), ts))
            db.commit()
            return self.send(200, {"ok": True, "app": who, "event": event, "ts": ts})
        return self.send(404, {"error": "not found"})


print(f"[loom-analytics] listening on :{PORT} (db: {DB_PATH})")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
