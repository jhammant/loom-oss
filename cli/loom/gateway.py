"""The Loom gateway: exposing the local fleet beyond this machine.

Concerns, all optional and driven by fleet/config.json's `gateway` block:

  * relay   — a NATIVE (non-Docker) TCP listener the external edge proxy forwards
              to. Some Docker setups (e.g. OrbStack) only forward published ports
              on loopback, so a native relay is what makes Loom reachable over the
              LAN / tailnet. It is supervised cross-platform: launchd (macOS),
              a systemd --user unit (Linux), or a plain background process.
  * gated   — the `gated` tier is enforced at the edge proxy via a forwardAuth
              (SSO) middleware Loom generates (see write_edge_gated).
  * tailnet — per-app `tailscale serve` for PRIVATE apps: a stable tailnet-only
              URL without exposing them publicly.

The external edge route itself lives off-box; see proxy/gateway/edge-loom.yml.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import paths
from .util import LoomError, info, ok, warn

RELAY_LABEL = "dev.loom.gateway-relay"
AUTH_MIDDLEWARE = "loom-authelia"
AUTH_FILE = "loom-auth.yml"


def _gw(cfg: dict) -> dict:
    return cfg.get("gateway") or {}


def _socat() -> str:
    for c in ("socat", "/opt/homebrew/bin/socat", "/usr/local/bin/socat"):
        p = shutil.which(c) or (c if Path(c).exists() else None)
        if p:
            return p
    raise LoomError("socat not found — install it (e.g. `brew install socat` or "
                    "`apt-get install socat`) for the gateway relay")


# --- native relay (cross-platform supervision) ---------------------------------
# Survives reboots via launchd (macOS) or a systemd --user unit (Linux); on hosts
# with neither it runs as a supervised background process (no reboot persistence).
# Override the choice with gateway.relay_supervisor = launchd | systemd | process.

def _state_dir() -> Path:
    d = Path.home() / ".loom"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _relay_args(cfg: dict) -> list:
    port = _gw(cfg).get("relay_port", 8444)
    return [_socat(), f"TCP-LISTEN:{port},fork,reuseaddr",
            f"TCP-CONNECT:127.0.0.1:{cfg['https_port']}"]


def _systemd_user_ok() -> bool:
    return bool(shutil.which("systemctl")) and subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0


def _backend(cfg: dict) -> str:
    forced = _gw(cfg).get("relay_supervisor")
    if forced in ("launchd", "systemd", "process"):
        return forced
    if sys.platform == "darwin":
        return "launchd"
    return "systemd" if _systemd_user_ok() else "process"


# launchd (macOS)
def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def _launchd_up(label: str, args: list) -> None:
    p = _plist_path(label)
    p.parent.mkdir(parents=True, exist_ok=True)
    prog = "".join(f"    <string>{a}</string>\n" for a in args)
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{label}</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n"
        f"{prog}  </array>\n"
        "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n"
        f"  <key>StandardOutPath</key><string>/tmp/{label}.log</string>\n"
        f"  <key>StandardErrorPath</key><string>/tmp/{label}.log</string>\n"
        "</dict></plist>\n"
    )
    _launchctl("unload", str(p))
    r = _launchctl("load", str(p))
    if r.returncode != 0:
        raise LoomError(f"failed to load {label}: {r.stderr.strip()}")


def _launchd_down(label: str) -> None:
    _launchctl("unload", str(_plist_path(label)))


def _launchd_running(label: str) -> bool:
    r = _launchctl("list")
    return any(line.endswith(label) for line in (r.stdout or "").splitlines())


# systemd --user (Linux)
def _unit_path(label: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{label}.service"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 1, "", "systemctl not found")


def _systemd_up(label: str, args: list) -> None:
    u = _unit_path(label)
    u.parent.mkdir(parents=True, exist_ok=True)
    u.write_text(
        "[Unit]\nDescription=Loom gateway relay\n\n"
        f"[Service]\nExecStart={' '.join(args)}\nRestart=always\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", f"{label}.service")
    if r.returncode != 0:
        raise LoomError(f"failed to start {label}: {(r.stderr or r.stdout).strip()}")


def _systemd_down(label: str) -> None:
    _systemctl("disable", "--now", f"{label}.service")


def _systemd_running(label: str) -> bool:
    return _systemctl("is-active", "--quiet", f"{label}.service").returncode == 0


# supervised process (any OS; no reboot persistence)
def _pidfile(label: str) -> Path:
    return _state_dir() / f"{label}.pid"


def _proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _process_running(label: str) -> bool:
    f = _pidfile(label)
    if not f.exists():
        return False
    try:
        return _proc_alive(int(f.read_text().strip()))
    except Exception:
        return False


def _process_up(label: str, args: list) -> None:
    if _process_running(label):
        return
    log = open(_state_dir() / f"{label}.log", "ab")
    proc = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    _pidfile(label).write_text(str(proc.pid))


def _process_down(label: str) -> None:
    f = _pidfile(label)
    if not f.exists():
        return
    try:
        os.kill(int(f.read_text().strip()), signal.SIGTERM)
    except Exception:
        pass
    f.unlink(missing_ok=True)


# dispatch over the chosen backend
def relay_running(cfg: dict) -> bool:
    return {"launchd": _launchd_running, "systemd": _systemd_running,
            "process": _process_running}[_backend(cfg)](RELAY_LABEL)


def relay_up(cfg: dict) -> None:
    b = _backend(cfg)
    {"launchd": _launchd_up, "systemd": _systemd_up, "process": _process_up}[b](
        RELAY_LABEL, _relay_args(cfg))
    ok(f"relay up (:{_gw(cfg).get('relay_port', 8444)} → Loom :{cfg['https_port']}, via {b})")


def relay_down(cfg: dict) -> None:
    b = _backend(cfg)
    {"launchd": _launchd_down, "systemd": _systemd_down, "process": _process_down}[b](RELAY_LABEL)
    ok("relay down")


# --- gated tier: gate at the EDGE -----------------------------------------------
# The Loom Traefik container can't reach the SSO server's LAN IP (OrbStack), and
# a launchd relay can't either (macOS Local Network privacy). The edge proxy,
# however, already reaches the SSO server and gates ~dozens of services. So gated
# apps are gated AT THE EDGE: Loom emits a higher-priority per-gated-app router
# (with the SSO forwardAuth middleware) for the edge's dynamic config.

EDGE_GATED_FILE = "edge-loom-gated.yml"


def lan_ip(cfg: dict | None = None) -> str:
    """This machine's IP on the interface that reaches the edge (what the edge
    proxies back to). Probes the route to the edge host if known (else a public
    IP) so it picks the real LAN interface, not a Docker/VPN virtual one. Auto-
    detected so a DHCP change doesn't silently break the public path."""
    import socket
    target = "8.8.8.8"
    host = (_gw(cfg or {}).get("edge_host") or "").split("@")[-1].split(":")[0].strip()
    if host:
        target = host
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))
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
    ip = lan_ip(cfg)
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
    ok(f"edge synced → loom @ {lan_ip(cfg)}:{_gw(cfg).get('relay_port', 8444)}")


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
    if not relay_running(cfg):
        relay_up(cfg)


def status(cfg: dict) -> None:
    gw = _gw(cfg)
    if relay_running(cfg):
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
