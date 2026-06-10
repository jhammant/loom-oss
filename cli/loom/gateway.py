"""The Loom gateway: exposing the local fleet beyond this machine.

Concerns, all optional and driven by fleet/config.json's `gateway` block:

  * relay   — a NATIVE (non-Docker) TCP listener the external edge proxy forwards
              to. OrbStack only forwards Docker-published ports on loopback, so a
              native relay is what makes Loom reachable over the LAN / tailnet.
  * auth    — a forwardAuth middleware (e.g. Authelia) for the `gated` tier. The
              Traefik container can't reach the LAN, so a second native relay
              bridges container -> host.docker.internal -> Authelia.
  * tailnet — per-app `tailscale serve` for PRIVATE apps: a stable tailnet-only
              URL without exposing them publicly.

Relays run as launchd agents so they survive reboots. The external edge route
itself lives off-box; see proxy/gateway/edge-loom.yml.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import paths
from .util import LoomError, info, ok, warn

RELAY_LABEL = "dev.loom.gateway-relay"
AUTH_RELAY_LABEL = "dev.loom.auth-relay"
AUTH_MIDDLEWARE = "loom-authelia"
AUTH_FILE = "loom-auth.yml"


def _gw(cfg: dict) -> dict:
    return cfg.get("gateway") or {}


def _socat() -> str:
    for c in ("socat", "/opt/homebrew/bin/socat", "/usr/local/bin/socat"):
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    raise LoomError("socat not found — install with `brew install socat` for the gateway relay")


# --- native relays (launchd) ---------------------------------------------------

def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _write_relay_plist(label: str, listen_port: int, target: str) -> None:
    socat = _socat()
    p = _plist_path(label)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{socat}</string>
    <string>TCP-LISTEN:{listen_port},fork,reuseaddr</string>
    <string>TCP-CONNECT:{target}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/{label}.log</string>
  <key>StandardErrorPath</key><string>/tmp/{label}.log</string>
</dict>
</plist>
""")


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def _agent_running(label: str) -> bool:
    r = _launchctl("list")
    return any(line.endswith(label) for line in (r.stdout or "").splitlines())


def _relay_load(label: str, listen_port: int, target: str) -> None:
    _write_relay_plist(label, listen_port, target)
    _launchctl("unload", str(_plist_path(label)))
    r = _launchctl("load", str(_plist_path(label)))
    if r.returncode != 0:
        raise LoomError(f"failed to load {label}: {r.stderr.strip()}")


def _relay_unload(label: str) -> None:
    _launchctl("unload", str(_plist_path(label)))


def relay_running() -> bool:
    return _agent_running(RELAY_LABEL)


def relay_up(cfg: dict) -> None:
    port = _gw(cfg).get("relay_port", 8444)
    _relay_load(RELAY_LABEL, port, f"127.0.0.1:{cfg['https_port']}")
    ok(f"relay up (:{port} → Loom :{cfg['https_port']})")


def relay_down(cfg: dict) -> None:
    _relay_unload(RELAY_LABEL)
    ok("relay down")


# --- gated tier: gate at the EDGE -----------------------------------------------
# The Loom Traefik container can't reach the SSO server's LAN IP (OrbStack), and
# a launchd relay can't either (macOS Local Network privacy). The edge proxy,
# however, already reaches the SSO server and gates ~dozens of services. So gated
# apps are gated AT THE EDGE: Loom emits a higher-priority per-gated-app router
# (with the SSO forwardAuth middleware) for the edge's dynamic config.

EDGE_GATED_FILE = "edge-loom-gated.yml"


def lan_ip() -> str:
    """This machine's primary LAN IP (what the edge proxies to). Auto-detected so
    a DHCP change doesn't silently break the public path — re-run `loom gateway sync`."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def write_edge_main(cfg: dict):
    """(Re)generate the edge wildcard route file with the CURRENT LAN IP."""
    domain = cfg.get("public_domain") or ""
    if not domain:
        return None
    ip = lan_ip()
    port = _gw(cfg).get("relay_port", 8444)
    esc = domain.replace(".", "\\.")
    rule = "HostRegexp(`^[a-z0-9-]+\\." + esc + "$`)"
    out = paths().proxy / "gateway" / "edge-loom.yml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Generated by Loom — edge wildcard route. Deploy to the edge as\n"
        "# /etc/traefik/dynamic/loom.yml (or `loom gateway sync`).\n"
        "http:\n"
        "  routers:\n"
        "    loom:\n"
        f"      rule: '{rule}'\n"
        "      entryPoints: [websecure]\n"
        "      service: loom\n"
        "      tls:\n"
        "        certResolver: letsencrypt\n"
        "        domains:\n"
        f'          - main: "{domain}"\n'
        f'            sans: ["*.{domain}"]\n'
        "  services:\n"
        "    loom:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        f'          - url: "https://{ip}:{port}"\n'
        "        serversTransport: loom-insecure\n"
        "        passHostHeader: true\n"
        "  serversTransports:\n"
        "    loom-insecure:\n"
        "      insecureSkipVerify: true\n"
    )
    return out


def sync(cfg: dict) -> None:
    """Regenerate the edge config (current LAN IP + gated apps) and push it to the
    edge proxy. The one-command fix for IP drift / gated changes."""
    import subprocess
    from . import registry
    main = write_edge_main(cfg)
    gated_names = [a["name"] for a in registry.all_apps() if a.get("access") == "gated"]
    gated = write_edge_gated(cfg, gated_names)
    host = _gw(cfg).get("edge_host")
    vmid = _gw(cfg).get("edge_vmid")
    if not (host and vmid):
        warn("gateway.edge_host/edge_vmid not set — generated proxy/gateway/*.yml; "
             "push them to the edge yourself")
        return
    key = str(Path.home() / ".ssh" / "id_ed25519")
    for f, dest in [(main, "loom.yml"), (gated, "loom-gated.yml")]:
        if f is None:
            continue
        subprocess.run(["scp", "-i", key, "-o", "BatchMode=yes", str(f), f"{host}:/tmp/{dest}"],
                       check=True, capture_output=True)
        subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", host,
                        f"pct push {vmid} /tmp/{dest} /etc/traefik/dynamic/{dest} && rm /tmp/{dest}"],
                       check=True, capture_output=True)
    ok(f"edge synced → loom @ {lan_ip()}:{_gw(cfg).get('relay_port', 8444)}")


def auth_enabled(cfg: dict) -> bool:
    gw = _gw(cfg)
    return bool(gw.get("auth_upstream") and gw.get("auth_rd"))


def edge_gated_path():
    return paths().proxy / "gateway" / EDGE_GATED_FILE


def write_edge_gated(cfg: dict, gated_names: list) -> "Path | None":
    """(Re)generate the edge dynamic-config for gated apps. Returns the path to
    deploy to the edge (as /etc/traefik/dynamic/loom-gated.yml), or None."""
    out = edge_gated_path()
    if not (auth_enabled(cfg) and gated_names and cfg.get("public_domain")):
        if out.exists():
            out.unlink()
        return None
    gw = _gw(cfg)
    domain = cfg["public_domain"]
    routers = ""
    for n in sorted(gated_names):
        routers += (
            f"    loom-gated-{n}:\n"
            f'      rule: "Host(`{n}.{domain}`)"\n'
            "      priority: 1000\n"
            "      entryPoints: [websecure]\n"
            "      service: loom\n"
            f"      middlewares: [{AUTH_MIDDLEWARE}]\n"
            "      tls:\n"
            "        certResolver: letsencrypt\n"
            "        domains:\n"
            f'          - main: "{domain}"\n'
            f'            sans: ["*.{domain}"]\n'
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Generated by Loom — gated-tier routers for the EDGE proxy.\n"
        "# Deploy to the edge as /etc/traefik/dynamic/loom-gated.yml.\n"
        "http:\n"
        "  middlewares:\n"
        f"    {AUTH_MIDDLEWARE}:\n"
        "      forwardAuth:\n"
        f'        address: "http://{gw["auth_upstream"]}/api/verify?rd={gw["auth_rd"]}"\n'
        "        trustForwardHeader: true\n"
        "        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Name, Remote-Email]\n"
        "  routers:\n"
        + routers
    )
    return out


# --- tailnet serve (private tier) ----------------------------------------------

def tailnet_enabled(cfg: dict) -> bool:
    return bool(_gw(cfg).get("tailnet_host")) and shutil.which("tailscale") is not None


def _tailscale(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tailscale", *args], text=True, capture_output=True, check=False)


def tailnet_serve(cfg: dict, tnport: int, local_port: int) -> str:
    r = _tailscale("serve", "--bg", f"--https={tnport}", f"http://127.0.0.1:{local_port}")
    if r.returncode != 0:
        raise LoomError(f"tailscale serve failed: {(r.stderr or r.stdout).strip()}")
    return f"https://{_gw(cfg)['tailnet_host']}:{tnport}"


def tailnet_unserve(tnport: int) -> None:
    _tailscale("serve", "--https", str(tnport), "off")


def next_tailnet_port(cfg: dict, used: set) -> int:
    p = int(_gw(cfg).get("tailnet_base_port", 7100))
    while p in used:
        p += 1
    return p


# --- lifecycle / status --------------------------------------------------------

def ensure(cfg: dict) -> None:
    """Bring up configured gateway pieces (idempotent)."""
    stale = paths().dynamic / AUTH_FILE  # remove the abandoned loom-side auth file
    if stale.exists():
        stale.unlink()
    if not relay_running():
        relay_up(cfg)


def status(cfg: dict) -> None:
    gw = _gw(cfg)
    if relay_running():
        ok(f"relay running (:{gw.get('relay_port', 8444)} → Loom :{cfg['https_port']})")
    else:
        warn("relay not running — run `loom gateway up`")
    if cfg.get("public_domain"):
        info(f"public domain: *.{cfg['public_domain']} (edge route: proxy/gateway/edge-loom.yml)")
    if auth_enabled(cfg):
        print(f"  gated tier: enabled (gated at edge via {gw['auth_upstream']}; "
              f"deploy proxy/gateway/{EDGE_GATED_FILE} to the edge)")
    else:
        print("  gated tier: disabled (set gateway.auth_upstream + auth_rd)")
    print(f"  tailnet serve: {'enabled' if tailnet_enabled(cfg) else 'disabled (set gateway.tailnet_host)'}")
