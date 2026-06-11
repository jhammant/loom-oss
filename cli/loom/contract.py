"""The Loom app contract (manifest v2).

A backward-compatible, additive extension of the v1 manifest (name/runtime/port/
access). Every new field is OPTIONAL and defaulted, so a v1 manifest deploys
unchanged. This is the machine-readable surface the harvester reads to build the
LLM-addressable Library, and that shared services + data federation key off later.

Reserved for the future: `semantics:` on capabilities and datasets is accepted
and stored but not yet acted on — forward-compat for taxilang/Orbital semantic
types so apps can declare meaning today; the federation engine arrives later.
"""
from __future__ import annotations

import re

from .util import LoomError, warn

MANIFEST_VERSION = 2
CAPABILITY_KINDS = {"http", "openapi", "mcp"}
KNOWN_SERVICES = {"auth", "email", "billing", "wallet", "llm"}
DATA_APIS = {"rest", "graphql", "event"}
DEFAULT_HEALTH_PATH = "/health"
_ID_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _err(msg: str):
    raise LoomError(f"fleet.app.yaml: {msg}")


def _as_list(v, field: str) -> list:
    if v is None:
        return []
    if not isinstance(v, list):
        _err(f"{field} must be a list")
    return v


def _as_dict(v, field: str) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        _err(f"{field} must be a mapping")
    return v


def parse_metadata(raw: dict) -> dict:
    m = _as_dict(raw.get("metadata"), "metadata")
    tags = _as_list(m.get("tags"), "metadata.tags")
    return {
        "description": str(m.get("description", "") or ""),
        "tags": sorted({str(t).lower() for t in tags}),
        "owner": str(m.get("owner", "") or ""),
    }


def parse_health(raw: dict) -> dict:
    h = _as_dict(raw.get("health"), "health")
    path = h.get("path", DEFAULT_HEALTH_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        _err("health.path must be a string starting with '/'")
    return {"path": path}


def parse_capabilities(raw: dict) -> list:
    caps = _as_list(raw.get("capabilities"), "capabilities")
    out, seen = [], set()
    for i, c in enumerate(caps):
        if not isinstance(c, dict):
            _err(f"capabilities[{i}] must be a mapping")
        cid = c.get("id")
        if not cid or not isinstance(cid, str) or not _ID_RE.match(cid):
            _err(f"capabilities[{i}].id must be a DNS-safe identifier (got {cid!r})")
        if cid in seen:
            _err(f"duplicate capability id '{cid}'")
        seen.add(cid)
        kind = c.get("kind")
        if kind not in CAPABILITY_KINDS:
            _err(f"capability '{cid}' kind must be one of {sorted(CAPABILITY_KINDS)}, got {kind!r}")
        if not c.get("path"):
            _err(f"capability '{cid}' ({kind}) requires a 'path'")
        cap = {
            "id": cid,
            "kind": kind,
            "path": str(c["path"]),
            "description": str(c.get("description", "") or ""),
        }
        for sk in ("input_schema", "output_schema"):
            if sk in c:
                if not isinstance(c[sk], dict):
                    _err(f"capability '{cid}'.{sk} must be a JSON-schema mapping")
                cap[sk] = c[sk]
        if "semantics" in c:  # reserved (taxilang); stored, not yet acted on
            cap["semantics"] = c["semantics"]
        out.append(cap)
    return out


def parse_consumes(raw: dict) -> list:
    out = []
    for i, c in enumerate(_as_list(raw.get("consumes"), "consumes")):
        if isinstance(c, str):
            c = {"service": c}
        if not isinstance(c, dict):
            _err(f"consumes[{i}] must be a service name or mapping")
        svc = c.get("service")
        if not svc or not isinstance(svc, str):
            _err(f"consumes[{i}].service is required")
        if svc not in KNOWN_SERVICES:
            warn(f"fleet.app.yaml: consumes '{svc}' is not a known service "
                 f"{sorted(KNOWN_SERVICES)} (allowed; will be unresolved for now)")
        out.append({"service": svc, "scope": str(c.get("scope", "") or "")})
    return out


def parse_data(raw: dict) -> dict:
    d = _as_dict(raw.get("data"), "data")

    def datasets(key: str) -> list:
        res = []
        for i, it in enumerate(_as_list(d.get(key), f"data.{key}")):
            if not isinstance(it, dict):
                _err(f"data.{key}[{i}] must be a mapping")
            name = it.get("name")
            if not name or not isinstance(name, str):
                _err(f"data.{key}[{i}].name is required")
            api = it.get("api", "rest")
            if api not in DATA_APIS:
                _err(f"data.{key}[{i}].api must be one of {sorted(DATA_APIS)}")
            res.append({"name": name, "api": api, "path": str(it.get("path", "")),
                        "semantics": it.get("semantics")})  # reserved (taxilang)
        return res

    return {"provides": datasets("provides"), "consumes": datasets("consumes")}


def normalize(raw: dict) -> dict:
    """The v2 fields, normalized and defaulted, to merge onto the v1 manifest."""
    mv = raw.get("manifest_version", 1)
    if not isinstance(mv, int):
        _err("manifest_version must be an integer")
    if mv > MANIFEST_VERSION:
        warn(f"fleet.app.yaml: manifest_version {mv} is newer than this CLI "
             f"supports ({MANIFEST_VERSION}); proceeding best-effort")
    provides = raw.get("provides_service") or ""
    if provides and not isinstance(provides, str):
        _err("provides_service must be a string (the service name this app backs)")
    secrets = raw.get("secrets") or []
    if not isinstance(secrets, list) or not all(isinstance(s, str) for s in secrets):
        _err("secrets must be a list of environment-variable names")
    return {
        "manifest_version": mv,
        "metadata": parse_metadata(raw),
        "health": parse_health(raw),
        "capabilities": parse_capabilities(raw),
        "consumes": parse_consumes(raw),
        "data": parse_data(raw),
        "provides_service": provides,
        "secrets": secrets,
    }


def snapshot(manifest: dict) -> dict:
    """The registry entry['contract'] block — the semantic record of an app,
    kept separate from operational fields. harvested_at/health_status/
    capability_index are populated later (health probe, harvester)."""
    return {
        "manifest_version": manifest.get("manifest_version", 1),
        "metadata": manifest.get("metadata", parse_metadata({})),
        "health": manifest.get("health", {"path": DEFAULT_HEALTH_PATH}),
        "capabilities": manifest.get("capabilities", []),
        "consumes": manifest.get("consumes", []),
        "data": manifest.get("data", {"provides": [], "consumes": []}),
        "provides_service": manifest.get("provides_service", ""),
        "secrets": manifest.get("secrets", []),
        "harvested_at": None,
        "health_status": "unknown",
        "capability_index": [],
    }
