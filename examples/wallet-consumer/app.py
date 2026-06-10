"""wallet-consumer: a public app that uses the Loom SDK to charge the shared
wallet service. Demonstrates contract `consumes:` -> auto-provisioned, authed
service calls with zero config in the app."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import loom_sdk

PORT = int(os.environ.get("PORT", 3000))


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
        q = parse_qs(u.query)
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        if u.path in ("/balance", "/credit", "/buy"):
            acct = q.get("account", ["alice"])[0]
            cents = int(q.get("cents", ["100"])[0])
            try:
                w = loom_sdk.wallet()
                if u.path == "/balance":
                    return self.send(200, w.balance(acct))
                if u.path == "/credit":
                    return self.send(200, w.credit(acct, cents))
                return self.send(200, w.charge(acct, cents, idempotency_key=q.get("key", [None])[0]))
            except loom_sdk.InsufficientCredits:
                return self.send(402, {"error": "insufficient credits"})
            except loom_sdk.ServiceError as e:
                return self.send(503, {"error": str(e)})
        return self.send(200, {"app": "wallet-consumer",
                               "try": ["/credit?account=alice&cents=500",
                                       "/buy?account=alice&cents=100",
                                       "/balance?account=alice"]})


print(f"[wallet-consumer] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
