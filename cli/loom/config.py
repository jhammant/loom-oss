"""Locating the Loom home directory and reading fleet/config.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .util import LoomError

DEFAULT_CONFIG = {
    "base_domain": "loom.localhost",
    "network": "loom",
    "http_port": 80,
    # HTTPS port. 443 is ideal, but some local stacks (e.g. OrbStack's domain
    # proxy) reserve it; 8443 is a safe default that needs no system changes.
    "https_port": 8443,
    "default_target": "local",
    # Public domain for externally-reachable apps. When set, PUBLIC apps also
    # get a router for <name>.<public_domain>, fronted by an external gateway
    # (see proxy.py / the gateway docs). Empty string = local-only.
    "public_domain": "",
    # The gateway exposes Loom beyond this machine (see gateway.py).
    "gateway": {
        # Native relay: a non-Docker listener the external edge proxies to
        # (OrbStack won't forward Docker-published ports off-loopback).
        "relay_port": 8444,
        # `gated` tier (forwardAuth SSO). auth_upstream = the auth server
        # host:port reachable from THIS machine; a native relay bridges the
        # Traefik container to it. Empty disables the gated tier.
        "auth_upstream": "",
        "auth_rd": "",            # SSO login redirect, e.g. https://auth.example.com
        "auth_relay_port": 19091,
        # Edge proxy push target for `loom gateway sync` (Proxmox host + LXC vmid).
        "edge_host": "",
        "edge_vmid": 0,
        # Tailscale node name; enables per-app tailnet serve for private apps.
        "tailnet_host": "",
        # First port used for private apps' tailnet (tailscale serve) URLs.
        "tailnet_base_port": 7100,
    },
}


def loom_home() -> Path:
    """Repo root that holds proxy/, fleet/, examples/.

    Overridable with $LOOM_HOME. Otherwise derived from this file's location
    (<root>/cli/loom/config.py), which is correct for an editable install.
    """
    env = os.environ.get("LOOM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


@dataclass
class Paths:
    root: Path

    @property
    def fleet(self) -> Path:
        return self.root / "fleet"

    @property
    def config_file(self) -> Path:
        return self.fleet / "config.json"

    @property
    def registry_file(self) -> Path:
        return self.fleet / "registry.json"

    @property
    def proxy(self) -> Path:
        return self.root / "proxy"

    @property
    def compose_file(self) -> Path:
        return self.proxy / "docker-compose.yml"

    @property
    def dynamic(self) -> Path:
        return self.proxy / "dynamic"

    @property
    def certs(self) -> Path:
        return self.proxy / "certs"


def paths() -> Paths:
    return Paths(root=loom_home())


def load_config() -> dict:
    """Read fleet/config.json, creating it with defaults if absent."""
    p = paths()
    cfg = dict(DEFAULT_CONFIG)
    if p.config_file.exists():
        try:
            cfg.update(json.loads(p.config_file.read_text()))
        except json.JSONDecodeError as e:
            raise LoomError(f"fleet/config.json is not valid JSON: {e}")
    else:
        p.fleet.mkdir(parents=True, exist_ok=True)
        p.config_file.write_text(json.dumps(cfg, indent=2) + "\n")
    return cfg


def app_url(cfg: dict, name: str, access: str, host_port: int | None = None) -> str:
    """The stable URL an app is reachable at."""
    if access == "private":
        return f"http://127.0.0.1:{host_port}"
    https_port = int(cfg["https_port"])
    base = f"https://{name}.{cfg['base_domain']}"
    return base if https_port == 443 else f"{base}:{https_port}"


def public_app_url(cfg: dict, name: str) -> str | None:
    """The externally-reachable URL for a public app, or None if no public
    domain is configured. The external gateway terminates TLS on 443."""
    domain = cfg.get("public_domain") or ""
    if not domain:
        return None
    return f"https://{name}.{domain}"
