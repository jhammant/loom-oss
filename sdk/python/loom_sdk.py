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
