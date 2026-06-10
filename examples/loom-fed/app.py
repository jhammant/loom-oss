"""loom-fed: the data-federation gateway.

Mediates cross-app dataset reads: GET /fed/<dataset>. It (1) verifies the
consumer's HMAC token, (2) re-checks the LIVE registry grant (deny-by-default +
prompt revocation — the registry is bind-mounted read-only), (3) resolves the
dataset's provider and proxies the read over the loom network. No external deps.
"""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", 8090))
SECRET = os.environ.get("LOOM_SERVICE_SECRET", "")
REGISTRY = "/registry.json"


def apps():
    try:
        with open(REGISTRY) as f:
            return json.load(f).get("apps", {})
    except Exception:
        return {}


def data_token(app, dataset):
    return hmac.new(SECRET.encode(), f"data:{app}:{dataset}".encode(), hashlib.sha256).hexdigest()


def find_provider(registry, dataset):
    for name, e in registry.items():
        for ds in ((e.get("contract") or {}).get("data") or {}).get("provides", []):
            if ds.get("name") == dataset:
                return name, e, ds
    return None, None, None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, extra=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send(200, {"status": "ok"})
        if not u.path.startswith("/fed/"):
            return self.send(404, {"error": "not found"})
        dataset = u.path[len("/fed/"):].split("/", 1)[0]
        consumer = self.headers.get("X-Loom-Consumer", "")
        auth = self.headers.get("Authorization", "")
        if not SECRET or not consumer or not auth.startswith("Bearer "):
            return self.send(401, {"error": "unauthorized"})
        if not hmac.compare_digest(auth[7:], data_token(consumer, dataset)):
            return self.send(401, {"error": "bad token"})
        registry = apps()
        grants = [g.get("dataset") for g in (registry.get(consumer, {}).get("data_grants") or [])]
        if dataset not in grants:  # deny-by-default + prompt revocation
            return self.send(403, {"error": "no grant", "consumer": consumer, "dataset": dataset})
        pname, pentry, ds = find_provider(registry, dataset)
        if not pname:
            return self.send(404, {"error": "no provider for dataset", "dataset": dataset})
        port = pentry.get("service_port") or pentry.get("port") or 80
        target = f"http://loom-{pname}:{port}{ds.get('path', '/')}"
        req = urllib.request.Request(target, headers={"X-Loom-Consumer": consumer})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                body, code = r.read(), r.getcode()
        except urllib.error.HTTPError as e:
            body, code = e.read(), e.code
        except Exception as e:
            return self.send(502, {"error": f"provider unreachable: {e}"})
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Loom-Federated-From", pname)
        self.end_headers()
        self.wfile.write(body)


print(f"[loom-fed] federation gateway on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
