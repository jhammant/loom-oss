"""The LLM-addressable Library: fleet/library.json.

Derived from the registry + harvester, fully regenerable. `search()` is the
single seam an agent (and the future MCP server) queries — lexical now, a vector
store can swap in behind it later with no change to callers or the record shape.
"""
from __future__ import annotations

import json
import re

from .config import paths

_TOKEN = re.compile(r"[a-z0-9]+")


def _file():
    return paths().fleet / "library.json"


def load() -> dict:
    f = _file()
    if not f.exists():
        return {"version": 1, "apps": {}, "_index": {}}
    d = json.loads(f.read_text())
    d.setdefault("apps", {})
    d.setdefault("_index", {})
    return d


def _tokens(*parts) -> set:
    out: set = set()
    for p in parts:
        if isinstance(p, (list, tuple)):
            for x in p:
                out |= set(_TOKEN.findall(str(x).lower()))
        elif p:
            out |= set(_TOKEN.findall(str(p).lower()))
    return out


def _record_tokens(rec: dict) -> set:
    ops = rec.get("operations", [])
    return _tokens(rec.get("name"), rec.get("description"), rec.get("tags"),
                   [o.get("id") for o in ops], [o.get("summary") for o in ops])


def _build_index(apps: dict) -> dict:
    idx: dict = {}
    for name, rec in apps.items():
        for tok in _record_tokens(rec):
            idx.setdefault(tok, []).append(name)
    return idx


def save(lib: dict) -> None:
    p = paths()
    p.fleet.mkdir(parents=True, exist_ok=True)
    lib["_index"] = _build_index(lib.get("apps", {}))
    _file().write_text(json.dumps(lib, indent=2) + "\n")


def upsert(record: dict) -> None:
    lib = load()
    lib["apps"][record["name"]] = record
    save(lib)


def drop(name: str) -> None:
    lib = load()
    if name in lib["apps"]:
        del lib["apps"][name]
        save(lib)


def get(name: str):
    return load()["apps"].get(name)


def all_records() -> list:
    return list(load()["apps"].values())


def search(query: str, limit: int = 10) -> list:
    """Lexical ranked search over the Library. Name/tag hits weigh higher."""
    q = set(_TOKEN.findall(query.lower()))
    if not q:
        return []
    scored = []
    for rec in load()["apps"].values():
        score = len(q & _record_tokens(rec))
        if q & _tokens(rec.get("name"), rec.get("tags")):
            score += 2
        if score:
            scored.append((score, rec))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [r for _, r in scored[:limit]]


def reindex_from_registry(cfg: dict) -> int:
    """Self-heal: rebuild the entire Library from the registry (re-harvesting)."""
    from . import harvester, registry
    lib = {"version": 1, "apps": {}, "_index": {}}
    for entry in registry.all_apps():
        lib["apps"][entry["name"]] = harvester.harvest_app(cfg, entry)
    save(lib)
    return len(lib["apps"])
