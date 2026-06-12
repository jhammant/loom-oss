"""Loom shared-services SDK (Python).

Apps read the LOOM_<SERVICE>_URL / LOOM_<SERVICE>_TOKEN env vars Loom injects
from the contract `consumes:` block — zero config. Vendor this file into an app
(a pip package is a follow-up; a Node mirror is planned).

    from loom_sdk import wallet
    wallet().charge("alice", 100, idempotency_key="order-42")
"""
import json
import os
import urllib.error
import urllib.request


class ServiceError(Exception):
    pass


class Unauthorized(ServiceError):
    pass


class InsufficientCredits(ServiceError):
    pass


class _Client:
    def __init__(self, service: str):
        self.service = service
        self.url = os.environ.get(f"LOOM_{service.upper()}_URL")
        self.token = os.environ.get(f"LOOM_{service.upper()}_TOKEN")
        self.app = os.environ.get("LOOM_APP", "")
        if not self.url or not self.token:
            raise ServiceError(
                f"service '{service}' is not provisioned — add it to `consumes:` in fleet.app.yaml")

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "X-Loom-App": self.app,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Unauthorized(f"{self.service}: unauthorized")
            if e.code == 402:
                raise InsufficientCredits(f"{self.service}: insufficient credits")
            raise ServiceError(f"{self.service}: HTTP {e.code}")
        except Exception as e:
            raise ServiceError(f"{self.service}: {e}")


class Wallet(_Client):
    def __init__(self):
        super().__init__("wallet")

    def balance(self, account):
        return self._request("GET", f"/balance?account={account}")

    def credit(self, account, cents):
        return self._request("POST", "/credit", {"account": account, "amount": cents})

    def charge(self, account, cents, idempotency_key=None):
        return self._request("POST", "/charge",
                             {"account": account, "amount": cents, "idempotency_key": idempotency_key})


def wallet() -> Wallet:
    return Wallet()


class LLM(_Client):
    """No-key LLM access via the platform gateway. Declare `consumes: [llm]` and
    Loom injects the endpoint + token — the app never holds a provider key.

        from loom_sdk import llm
        reply = llm().chat("Summarise this in one line: ...", model="fast")
        print(reply["text"])
    """
    def __init__(self):
        super().__init__("llm")

    def chat(self, messages, model="fast", max_tokens=1024):
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        return self._request("POST", "/v1/chat",
                             {"model": model, "messages": messages, "max_tokens": max_tokens})


def llm() -> LLM:
    return LLM()


class Analytics(_Client):
    """Fleet event tracking. Declare `consumes: [analytics]` and Loom wires the
    endpoint + token. `track()` is fire-and-forget friendly: failures (service
    down, network blip) are swallowed and it returns False — never break the
    app over telemetry.

        from loom_sdk import analytics
        analytics().track("page_view", {"path": "/pricing"})
    """
    def __init__(self):
        super().__init__("analytics")

    def track(self, event, props=None) -> bool:
        try:
            self._request("POST", "/track", {"event": event, "props": props or {}})
            return True
        except ServiceError:
            return False

    def stats(self, app=None, since=None):
        q = "&".join(p for p in (f"app={app}" if app else "",
                                 f"since={int(since)}" if since else "") if p)
        return self._request("GET", "/stats" + (f"?{q}" if q else ""))


def analytics() -> Analytics:
    return Analytics()


# --- identity (zero-config; available to GATED apps via request headers) --------

class Identity:
    def __init__(self, user="", email="", name="", groups=()):
        self.user, self.email, self.name = user, email, name
        self.groups = list(groups)

    @property
    def is_authenticated(self) -> bool:
        return bool(self.user)

    def __repr__(self):
        return f"Identity(user={self.user!r}, email={self.email!r}, groups={self.groups})"


def identity(headers) -> Identity:
    """The authenticated user for a GATED request, from the headers the platform's
    forward-auth injects (Remote-User / Remote-Email / Remote-Name / Remote-Groups).
    Returns an empty Identity for unauthenticated (public) requests. `headers` is
    any case-insensitive mapping — e.g. a stdlib http.server `self.headers`, a
    WSGI/ASGI headers object, or a dict."""
    def h(*names):
        for n in names:
            try:
                v = headers.get(n)
            except Exception:
                v = None
            if v:
                return v
        return ""
    groups = h("Remote-Groups", "X-Forwarded-Groups")
    return Identity(
        user=h("Remote-User", "X-Forwarded-User", "X-Auth-Request-User"),
        email=h("Remote-Email", "X-Forwarded-Email", "X-Auth-Request-Email"),
        name=h("Remote-Name"),
        groups=[g.strip() for g in groups.split(",") if g.strip()],
    )
