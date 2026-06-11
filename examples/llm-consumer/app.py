"""llm-consumer: a public app that calls an LLM with NO API key of its own.

It declares `consumes: [llm]`; Loom injects LOOM_LLM_URL + LOOM_LLM_TOKEN and the
SDK does the rest. The app never sees a provider key. Also demonstrates
`identity()` — if this app were `access: gated`, the forward-auth headers would
populate the caller's user; for a public app it's simply unauthenticated.
"""
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
        if u.path == "/ask":
            prompt = q.get("q", ["Say hello in one short sentence."])[0]
            model = q.get("model", ["fast"])[0]
            who = loom_sdk.identity(self.headers)  # populated only on gated apps
            try:
                reply = loom_sdk.llm().chat(prompt, model=model)
            except loom_sdk.ServiceError as e:
                return self.send(503, {"error": str(e)})
            return self.send(200, {
                "you_asked": prompt,
                "model": model,
                "answer": reply.get("text", ""),
                "stub": reply.get("stub", False),
                "as_user": who.user or "(anonymous)",
            })
        return self.send(200, {"app": "llm-consumer",
                               "try": ["/ask?q=Summarise+Loom+in+a+line",
                                       "/ask?q=Name+three+colours&model=smart"]})


print(f"[llm-consumer] listening on :{PORT}")
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
