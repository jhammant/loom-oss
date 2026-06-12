"""analytics-consumer: a public app that tracks a page_view event in the shared
analytics service on every request, then shows the running stats. Demonstrates
fire-and-forget `consumes:` telemetry with zero config in the app."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        try:
            a = loom_sdk.analytics()
        except loom_sdk.ServiceError as e:
            return self.send(503, {"error": str(e)})
        tracked = a.track("page_view", {"path": u.path})  # never raises
        try:
            stats = a.stats()
        except loom_sdk.ServiceError as e:
            return self.send(503, {"error": str(e)})
        return self.send(200, {"app": "analytics-consumer",
                               "tracked": tracked, "stats": stats})


print(f"[analytics-consumer] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
