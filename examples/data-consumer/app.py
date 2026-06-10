"""data-consumer: reads the `items` dataset through the federation gateway,
using only the LOOM_DATA_ITEMS_URL/_TOKEN env Loom provisions from `consumes:`."""
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 3000))


def fetch_items():
    url = os.environ.get("LOOM_DATA_ITEMS_URL")
    token = os.environ.get("LOOM_DATA_ITEMS_TOKEN")
    app = os.environ.get("LOOM_APP", "")
    if not url or not token:
        return {"error": "items dataset not provisioned (declare it in consumes:)"}
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "X-Loom-Consumer": app})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"federation {e.code}", "detail": e.read().decode("utf-8", "replace")}


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
            return self.send(200, fetch_items())
        return self.send(200, {"app": "data-consumer", "try": ["/items"]})


print(f"[data-consumer] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
