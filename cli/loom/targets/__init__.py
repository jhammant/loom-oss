"""Deploy-target adapters.

Iteration 1 ships only the `local` Docker target, but the CLI talks to targets
exclusively through the `Target` interface (see base.py). A future target —
Coolify on a home box, a scale-to-zero cloud host — slots in here by
implementing the same interface and registering below. Nothing else changes.
"""
from __future__ import annotations

from .base import Target
from .local import LocalDockerTarget
from ..util import LoomError

_REGISTRY = {
    "local": LocalDockerTarget,
}


def get_target(name: str) -> Target:
    cls = _REGISTRY.get(name)
    if cls is None:
        raise LoomError(
            f"unknown target '{name}'. Available: {sorted(_REGISTRY)}"
        )
    return cls()


def available() -> list[str]:
    return sorted(_REGISTRY)
