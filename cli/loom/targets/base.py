"""The Target interface every deploy adapter implements.

The CLI is target-agnostic: it loads a manifest, hands it to a Target, and
records the returned entry in the registry. Lifecycle verbs (start/stop/
remove/logs) operate on a registry entry. Keeping this surface small is what
lets a cloud or Coolify target drop in later without touching the CLI.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Target(ABC):
    #: short identifier used in fleet/config.json (`default_target`) and entries
    name: str = "base"

    @abstractmethod
    def deploy(self, cfg: dict, app_dir: Path, manifest: dict) -> dict:
        """Build + run the app. Return a registry entry dict (incl. url, status)."""

    @abstractmethod
    def start(self, cfg: dict, entry: dict) -> None:
        ...

    @abstractmethod
    def stop(self, cfg: dict, entry: dict) -> None:
        ...

    @abstractmethod
    def remove(self, cfg: dict, entry: dict) -> None:
        """Tear the app down completely (container, route, etc.)."""

    @abstractmethod
    def logs(self, cfg: dict, entry: dict, follow: bool, tail: str) -> int:
        ...

    @abstractmethod
    def reconcile(self, cfg: dict, entries: list[dict]) -> dict:
        """Return {app_name: live_status} for the given entries."""

    def probe_health(self, cfg: dict, entry: dict) -> str:
        """App-level readiness from its declared health path: 'ok' | 'unready' |
        'down' | 'unknown'. Concrete (not abstract) and defaults to 'unknown' so
        targets opt in; never raises. Folded into reconcile in a later milestone."""
        return "unknown"
