"""Shared-services provisioning (C6).

An app declares which platform services it CONSUMES (contract `consumes:`); a
service-backend app declares the one it PROVIDES (`provides_service:`). At deploy
time Loom resolves consume-grants against the live providers and injects into the
consumer container:

    LOOM_<SERVICE>_URL    — the provider's in-network address (loom network)
    LOOM_<SERVICE>_TOKEN  — an HMAC the provider verifies (app-to-app identity)

Provider apps get LOOM_SERVICE + LOOM_SERVICE_SECRET so they can verify callers.
The SDK (sdk/python/loom_sdk.py) reads exactly these env vars. Resolution is
best-effort: a consume with no deployed provider warns and injects nothing
(never blocks deploy) — redeploy once the provider exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from . import registry
from .config import paths
from .util import warn


def service_secret(cfg: dict) -> str:
    """A stable platform secret used to sign app-to-app tokens. Generated once
    and persisted to fleet/config.json (gitignored)."""
    s = cfg.get("service_secret")
    if s:
        return s
    s = secrets.token_hex(32)
    f = paths().config_file
    data = json.loads(f.read_text()) if f.exists() else {}
    data["service_secret"] = s
    f.write_text(json.dumps(data, indent=2) + "\n")
    cfg["service_secret"] = s
    return s


def mint_token(secret: str, app: str, service: str) -> str:
    return hmac.new(secret.encode(), f"{app}:{service}".encode(), hashlib.sha256).hexdigest()


def verify_token(secret: str, token: str, app: str, service: str) -> bool:
    return hmac.compare_digest(token, mint_token(secret, app, service))


def find_provider(service: str):
    for e in registry.all_apps():
        if (e.get("contract") or {}).get("provides_service") == service:
            return e
    return None


def _internal_url(entry: dict) -> str:
    port = entry.get("service_port") or entry.get("port") or 80
    return f"http://loom-{entry['name']}:{port}"  # reachable over the loom network


def provider_env(cfg: dict, manifest: dict) -> dict:
    """Provider side: secret + service name so the backend can verify callers."""
    svc = manifest.get("provides_service") or ""
    if not svc:
        return {}
    return {"LOOM_SERVICE": svc, "LOOM_SERVICE_SECRET": service_secret(cfg)}


def mint_data_token(secret: str, app: str, dataset: str) -> str:
    return hmac.new(secret.encode(), f"data:{app}:{dataset}".encode(), hashlib.sha256).hexdigest()


def find_federation_gateway():
    for e in registry.all_apps():
        if (e.get("contract") or {}).get("provides_service") == "federation":
            return e
    return None


def find_dataset_provider(dataset: str):
    for e in registry.all_apps():
        for ds in ((e.get("contract") or {}).get("data") or {}).get("provides", []):
            if ds.get("name") == dataset:
                return e, ds
    return None, None


def provision_data_env(cfg: dict, manifest: dict) -> tuple[dict, list]:
    """Consumer side of data federation: point each declared data-consume at the
    federation gateway with a scoped token, and record the grant. Deny-by-default
    — only declared consumes get a token, and the gateway re-checks the live grant."""
    env: dict = {}
    grants: list = []
    consumes = (manifest.get("data") or {}).get("consumes", [])
    if not consumes:
        return env, grants
    gw = find_federation_gateway()
    if not gw:
        warn(f"app '{manifest['name']}' consumes data but no federation gateway "
             "(provides_service: federation) is deployed (unresolved)")
        return env, grants
    secret = service_secret(cfg)
    gw_url = _internal_url(gw)
    for c in consumes:
        ds = c["name"]
        env[f"LOOM_DATA_{ds.upper()}_URL"] = f"{gw_url}/fed/{ds}"
        env[f"LOOM_DATA_{ds.upper()}_TOKEN"] = mint_data_token(secret, manifest["name"], ds)
        prov, _ = find_dataset_provider(ds)
        grants.append({"dataset": ds, "provider": prov["name"] if prov else None})
    return env, grants


def provision_env(cfg: dict, manifest: dict) -> tuple[dict, list]:
    """Consumer side: env to inject + the grants that were resolved."""
    env: dict = {}
    grants: list = []
    name = manifest["name"]
    secret = None
    for c in manifest.get("consumes", []):
        svc = c["service"]
        prov = find_provider(svc)
        if not prov:
            warn(f"app '{name}' consumes '{svc}' but no provider is deployed "
                 "(unresolved; redeploy after deploying the provider)")
            continue
        secret = secret or service_secret(cfg)
        env[f"LOOM_{svc.upper()}_URL"] = _internal_url(prov)
        env[f"LOOM_{svc.upper()}_TOKEN"] = mint_token(secret, name, svc)
        grants.append({"service": svc, "provider": prov["name"], "scope": c.get("scope", "")})
    return env, grants
