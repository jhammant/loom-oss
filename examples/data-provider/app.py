"""data-provider: exposes an `items` dataset to the fleet (read via the
federation gateway, not directly)."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 8100))
ITEMS = [{"id": 1, "name": "widget", "price": 100},
         {"id": 2, "name": "gadget", "price": 250}]


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
            return self.send(200, {"status": "ok"})
        if u.path == "/items":
            return self.send(200, {"items": ITEMS, "served_to": self.headers.get("X-Loom-Consumer", "?")})
        return self.send(404, {"error": "not found"})


print(f"[data-provider] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
