"""The fleet registry: fleet/registry.json — the record of every deployed app.

This is the seed of the future Library, so it is kept clean and explicit.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import paths


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load() -> dict:
    f = paths().registry_file
    if not f.exists():
        return {"version": 1, "apps": {}}
    data = json.loads(f.read_text())
    data.setdefault("version", 1)
    data.setdefault("apps", {})
    return data


def save(reg: dict) -> None:
    p = paths()
    p.fleet.mkdir(parents=True, exist_ok=True)
    p.registry_file.write_text(json.dumps(reg, indent=2, sort_keys=False) + "\n")


def get(name: str) -> dict | None:
    return load()["apps"].get(name)


def all_apps() -> list[dict]:
    return list(load()["apps"].values())


def upsert(entry: dict) -> None:
    reg = load()
    name = entry["name"]
    existing = reg["apps"].get(name)
    if existing:
        entry["created_at"] = existing.get("created_at", _now())
        # Carry forward semantic state the harvester/health-probe own, so a
        # redeploy doesn't blank a later harvest.
        if isinstance(entry.get("contract"), dict) and isinstance(existing.get("contract"), dict):
            for k in ("harvested_at", "health_status", "capability_index"):
                if k in existing["contract"]:
                    entry["contract"][k] = existing["contract"][k]
    else:
        entry["created_at"] = _now()
    entry["updated_at"] = _now()
    reg["apps"][name] = entry
    save(reg)


def set_status(name: str, status: str) -> None:
    reg = load()
    if name in reg["apps"]:
        reg["apps"][name]["status"] = status
        reg["apps"][name]["updated_at"] = _now()
        save(reg)


def set_health(name: str, status: str) -> None:
    reg = load()
    app = reg["apps"].get(name)
    if app and isinstance(app.get("contract"), dict):
        app["contract"]["health_status"] = status
        app["updated_at"] = _now()
        save(reg)


def remove(name: str) -> None:
    reg = load()
    if name in reg["apps"]:
        del reg["apps"][name]
        save(reg)
