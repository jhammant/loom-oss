"""Loading and validating fleet.app.yaml."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import contract
from .util import LoomError

MANIFEST_NAME = "fleet.app.yaml"
RUNTIMES = {"node", "python", "static", "docker"}
# public  = routed, reachable by anyone (local + public domain)
# gated   = routed + behind forwardAuth (SSO); public domain, auth required
# private = not routed publicly; local + (optionally) a tailnet-only URL
ACCESS = {"public", "gated", "private"}
# DNS label: lowercase alphanumeric + hyphen, not leading/trailing hyphen, <=63 chars.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def manifest_path(app_dir: Path) -> Path:
    return app_dir / MANIFEST_NAME


def load_manifest(app_dir: Path) -> dict:
    """Read and validate the manifest in app_dir. Returns a normalised dict."""
    path = manifest_path(app_dir)
    if not path.exists():
        raise LoomError(f"no {MANIFEST_NAME} found in {app_dir}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise LoomError(f"{path} is not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise LoomError(f"{path} must be a YAML mapping")

    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise LoomError(f"{MANIFEST_NAME}: 'name' is required")
    if not _NAME_RE.match(name):
        raise LoomError(
            f"{MANIFEST_NAME}: name '{name}' is not DNS-safe "
            "(use lowercase letters, digits and hyphens; no leading/trailing hyphen)"
        )

    runtime = raw.get("runtime")
    if runtime not in RUNTIMES:
        raise LoomError(
            f"{MANIFEST_NAME}: runtime must be one of {sorted(RUNTIMES)}, got {runtime!r}"
        )

    access = raw.get("access", "public")
    if access not in ACCESS:
        raise LoomError(f"{MANIFEST_NAME}: access must be one of {sorted(ACCESS)}, got {access!r}")

    # Static sites are served by nginx on 80; port is optional for them.
    port = raw.get("port", 80 if runtime == "static" else None)
    if runtime != "static" and port is None:
        raise LoomError(f"{MANIFEST_NAME}: 'port' is required for runtime '{runtime}'")
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise LoomError(f"{MANIFEST_NAME}: port must be an integer, got {port!r}")
        if not (1 <= port <= 65535):
            raise LoomError(f"{MANIFEST_NAME}: port must be 1-65535, got {port}")

    # v1 fields above; merge the (optional, defaulted) v2 contract fields.
    manifest = {"name": name, "runtime": runtime, "port": port, "access": access}
    manifest.update(contract.normalize(raw))
    return manifest
