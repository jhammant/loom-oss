"""loom-wallet: a credit ledger shared service for the fleet.

Verifies app-to-app calls via the HMAC token Loom provisions (no external deps).
Endpoints: GET /health, GET /balance?account=, POST /credit, POST /charge.
"""
import hashlib
import hmac
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", 8080))
SECRET = os.environ.get("LOOM_SERVICE_SECRET", "")
SERVICE = os.environ.get("LOOM_SERVICE", "wallet")

db = sqlite3.connect("/tmp/wallet.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS balances(account TEXT PRIMARY KEY, cents INTEGER NOT NULL DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS idem(key TEXT PRIMARY KEY, result TEXT)")
db.commit()


def caller(headers):
    """Return the verified calling app, or None."""
    auth = headers.get("Authorization", "")
    app = headers.get("X-Loom-App", "")
    if not SECRET or not app or not auth.startswith("Bearer "):
        return None
    expect = hmac.new(SECRET.encode(), f"{app}:{SERVICE}".encode(), hashlib.sha256).hexdigest()
    return app if hmac.compare_digest(auth[7:], expect) else None


def balance(account):
    row = db.execute("SELECT cents FROM balances WHERE account=?", (account,)).fetchone()
    return row[0] if row else 0


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
        if u.path == "/balance":
            acct = parse_qs(u.query).get("account", [""])[0]
            return self.send(200, {"account": acct, "cents": balance(acct)})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        who = caller(self.headers)
        if who is None:
            return self.send(401, {"error": "unauthorized"})
        d = self.json_body()
        acct = d.get("account", "")
        amount = int(d.get("amount", 0))
        if u.path == "/credit":
            db.execute("INSERT INTO balances(account,cents) VALUES(?,?) "
                       "ON CONFLICT(account) DO UPDATE SET cents=cents+?", (acct, amount, amount))
            db.commit()
            return self.send(200, {"account": acct, "cents": balance(acct), "by": who})
        if u.path == "/charge":
            key = d.get("idempotency_key") or ""
            if key:
                row = db.execute("SELECT result FROM idem WHERE key=?", (key,)).fetchone()
                if row:
                    return self.send(200, json.loads(row[0]))
            if balance(acct) < amount:
                return self.send(402, {"error": "insufficient_credits",
                                       "account": acct, "cents": balance(acct)})
            db.execute("UPDATE balances SET cents=cents-? WHERE account=?", (amount, acct))
            db.commit()
            res = {"account": acct, "charged": amount, "cents": balance(acct), "by": who}
            if key:
                db.execute("INSERT OR IGNORE INTO idem(key,result) VALUES(?,?)", (key, json.dumps(res)))
                db.commit()
            return self.send(200, res)
        return self.send(404, {"error": "not found"})


print(f"[loom-wallet] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
