"""Deploy-time harvester: turn a deployed app's contract (+ live probing) into a
flattened, searchable Library record.

Target-agnostic — it reads the registry entry and reaches the app at its recorded
URL (which is loopback-reachable for both routed apps via *.localhost and private
apps via 127.0.0.1). Probing is best-effort: an app that isn't ready yet still
yields a record from its declared contract.
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime, timezone

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _get_json(url: str, timeout: int = 5):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # local mkcert / self-signed
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _flatten_openapi(spec) -> list:
    ops = []
    if not isinstance(spec, dict):
        return ops
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS:
                continue
            op = op if isinstance(op, dict) else {}
            stem = path.strip("/").replace("/", "_") or "root"
            ops.append({
                "id": op.get("operationId") or f"{method.lower()}_{stem}",
                "kind": "openapi",
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary") or op.get("description") or "",
            })
    return ops


def harvest_app(cfg: dict, entry: dict) -> dict:
    """Build the Library record for one app."""
    contract = entry.get("contract") or {}
    meta = contract.get("metadata") or {}
    name = entry["name"]
    base = (entry.get("url") or "").rstrip("/")

    record = {
        "name": name,
        "description": meta.get("description", ""),
        "tags": meta.get("tags", []),
        "url": entry.get("url"),
        "public_url": entry.get("public_url"),
        "tailnet_url": entry.get("tailnet_url"),
        "access": entry.get("access"),
        "runtime": entry.get("runtime"),
        "health_status": contract.get("health_status", "unknown"),
        "operations": [{
            "id": "web", "kind": "web", "method": "GET", "path": "/",
            "summary": f"{name} web endpoint",
        }],
        "harvested_at": _now(),
    }
    for cap in contract.get("capabilities", []):
        if cap.get("kind") == "openapi" and base:
            ops = _flatten_openapi(_get_json(base + cap.get("path", "")))
            if ops:
                record["operations"].extend(ops)
                continue  # spec expanded; skip the raw fallback
        record["operations"].append({
            "id": cap["id"],
            "kind": cap.get("kind", "http"),
            "method": "GET",
            "path": cap.get("path", "/"),
            "summary": cap.get("description", ""),
            "input_schema": cap.get("input_schema"),
            "output_schema": cap.get("output_schema"),
        })
    # Datasets this app federates (discoverable alongside operations).
    record["datasets"] = ((contract.get("data") or {}).get("provides")) or []
    # Dedupe by (method, path) — a declared http capability and the same op from
    # a flattened OpenAPI spec collapse to one (the declared one wins; it carries
    # the richer schema).
    seen, deduped = set(), []
    for o in record["operations"]:
        key = (o.get("method", "GET"), o.get("path", "/"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(o)
    record["operations"] = deduped
    return record
