"""`loom admin` — the local fleet console.

A single-user, localhost-only web page over the live fleet: see every app
(status/health/URLs), stop/start/remove with one click, and "warp in" new
apps by scanning a directory (default ~/dev) for deployable candidates —
dirs that already carry a fleet.app.yaml, or whose runtime Loom can infer
(Dockerfile/package.json/index.html/pyproject). Deploying a candidate with
no manifest writes a fleet.app.yaml next to the app first (the manifest
always lives with the app — that's the contract), then deploys it.

This is the LOCAL admin for a single fleet — not the hosted multi-tenant
dashboard (a commercial-product concern, see OPEN-CORE.md). Security model:

  * No credentials configured → binds 127.0.0.1 and refuses non-loopback
    callers. `--host` beyond loopback is refused.
  * `loom admin --set-password` stores username + PBKDF2-SHA256 password
    hash in fleet/secrets.json (gitignored). From then on every request
    needs HTTP Basic auth (timing-safe verify, delay on failure), and
    `--host` may bind beyond loopback (LAN/tailnet) — front it with TLS
    (e.g. tailscale serve / your edge proxy) if the network is untrusted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets as pysecrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import dockercmd, gateway, harvester, library, registry
from .manifest import load_manifest
from .targets import get_target
from .util import LoomError, info, ok

SKIP_DIRS = {"node_modules", "dist", "build", "out", "venv", "__pycache__",
             ".git", ".venv", ".next", ".cache", "target"}

PBKDF2_ITERATIONS = 600_000


# --- credentials -----------------------------------------------------------------

def _secrets_file() -> Path:
    from .config import paths
    return paths().fleet / "secrets.json"


def hash_password(password: str, salt: str | None = None,
                  iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = salt or pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), iterations)
    return {"salt": salt, "iterations": iterations, "hash": digest.hex()}


def verify_password(password: str, rec: dict) -> bool:
    got = hashlib.pbkdf2_hmac("sha256", password.encode(),
                              bytes.fromhex(rec["salt"]), int(rec["iterations"]))
    return hmac.compare_digest(got.hex(), rec["hash"])


def set_credentials(username: str, password: str) -> None:
    """Store admin credentials (PBKDF2 hash, never the password) in
    fleet/secrets.json — gitignored, server-side only."""
    if not username or not password:
        raise LoomError("username and password must be non-empty")
    if len(password) < 8:
        raise LoomError("password must be at least 8 characters")
    f = _secrets_file()
    store = json.loads(f.read_text()) if f.exists() else {}
    store["ADMIN_AUTH"] = {"username": username, **hash_password(password)}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(store, indent=2) + "\n")
    try:
        f.chmod(0o600)
    except OSError:
        pass


def load_credentials() -> dict | None:
    f = _secrets_file()
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text()).get("ADMIN_AUTH")
    except Exception:
        return None


def check_basic_auth(header: str | None, creds: dict) -> bool:
    """Validate an Authorization header against stored credentials.
    Timing-safe on both username and password."""
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    user_ok = hmac.compare_digest(user, creds["username"])
    return verify_password(password, creds) and user_ok


# --- fleet operations (the same primitives the CLI verbs use) -------------------

def deploy_dir(cfg: dict, app_dir: Path) -> dict:
    manifest = load_manifest(app_dir)
    target = get_target(cfg["default_target"])
    entry = target.deploy(cfg, app_dir, manifest)
    registry.upsert(entry)
    gated = [a["name"] for a in registry.all_apps() if a.get("access") == "gated"]
    gateway.write_edge_gated(cfg, gated)
    library.upsert(harvester.harvest_app(cfg, entry))
    return entry


def write_manifest(app_dir: Path, manifest: dict) -> None:
    f = app_dir / "fleet.app.yaml"
    if f.exists():
        raise LoomError(f"{f} already exists")
    f.write_text(yaml.safe_dump(manifest, sort_keys=False))


def stop_app(cfg: dict, name: str) -> None:
    entry = _entry(name)
    get_target(entry.get("target", "local")).stop(cfg, entry)
    registry.set_status(name, "exited")


def start_app(cfg: dict, name: str) -> None:
    entry = _entry(name)
    get_target(entry.get("target", "local")).start(cfg, entry)
    registry.set_status(name, "running")


def remove_app(cfg: dict, name: str) -> None:
    entry = _entry(name)
    get_target(entry.get("target", "local")).remove(cfg, entry)
    registry.remove(name)
    gated = [a["name"] for a in registry.all_apps() if a.get("access") == "gated"]
    gateway.write_edge_gated(cfg, gated)
    library.drop(name)


def app_logs(cfg: dict, name: str, tail: int = 200) -> str:
    entry = _entry(name)
    container = entry.get("container")
    if not container:
        raise LoomError(f"'{name}' has no local container (target '{entry.get('target')}')")
    r = dockercmd.run(["logs", "--tail", str(tail), container], capture=True, check=False)
    return (r.stdout or "") + (r.stderr or "")


def _entry(name: str) -> dict:
    entry = registry.get(name)
    if entry is None:
        raise LoomError(f"no app named '{name}' in the fleet")
    return entry


def config_view(cfg: dict) -> dict:
    """The config as shown in the console: values from fleet/config.json with
    secret material masked, plus where to edit it."""
    from .config import paths
    shown = {k: ("•••" if k == "service_secret" and v else v)
             for k, v in cfg.items()}
    secrets_file = paths().fleet / "secrets.json"
    secret_names = []
    if secrets_file.exists():
        try:
            secret_names = sorted(json.loads(secrets_file.read_text()))
        except Exception:
            pass
    return {"config": shown,
            "config_file": str(paths().config_file),
            "secrets_file": str(secrets_file),
            "secret_names": secret_names}  # names only — values never leave disk


def import_repo(url: str, root: Path) -> dict:
    """Clone a git repo into the scan root so it shows up as a candidate.
    Accepts https/ssh git URLs and GitHub shorthand (user/repo)."""
    import subprocess
    url = url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url):
        url = f"https://github.com/{url}.git"
    if not re.match(r"^(https://|git@|ssh://)", url):
        raise LoomError(f"not a git URL: {url}")
    name = re.sub(r"\.git$", "", url.rstrip("/").rsplit("/", 1)[-1])
    dest = root / name
    if dest.exists():
        raise LoomError(f"{dest} already exists")
    r = subprocess.run(["git", "clone", "--depth", "1", url, str(dest)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise LoomError(f"git clone failed: {(r.stderr or '').strip()[-300:]}")
    return {"path": str(dest), "dir": name}


def fleet_snapshot(cfg: dict) -> list[dict]:
    """Registry entries with live status reconciled, shaped for the UI."""
    entries = registry.all_apps()
    by_target: dict[str, list[dict]] = {}
    for e in entries:
        by_target.setdefault(e.get("target", "local"), []).append(e)
    live: dict[str, str] = {}
    for tname, items in by_target.items():
        try:
            live.update(get_target(tname).reconcile(cfg, items))
        except LoomError:
            pass  # a dead target must not take the console down
    out = []
    for e in sorted(entries, key=lambda x: x["name"]):
        status = live.get(e["name"], e.get("status", "unknown"))
        if e.get("status") != status:
            registry.set_status(e["name"], status)
        out.append({
            "name": e["name"], "status": status,
            "health": (e.get("contract") or {}).get("health_status", "unknown"),
            "url": e.get("url"), "public_url": e.get("public_url"),
            "runtime": e.get("runtime"), "access": e.get("access"),
            "target": e.get("target", "local"),
            "source_path": e.get("source_path"),
        })
    return out


def app_detail(cfg: dict, name: str) -> dict:
    """Everything the console shows when you click an app: the registry
    entry's operational fields + contract, and the Library's harvested
    operations. Grants carry no token material — safe to surface."""
    entry = _entry(name)
    rec = library.get(name) or {}
    contract = entry.get("contract") or {}
    return {
        "name": name,
        "status": entry.get("status"),
        "runtime": entry.get("runtime"), "target": entry.get("target", "local"),
        "access": entry.get("access"),
        "urls": {"local": entry.get("url"), "public": entry.get("public_url"),
                 "tailnet": entry.get("tailnet_url"),
                 "custom": entry.get("custom_url")},
        "source_path": entry.get("source_path"),
        "image": entry.get("image"),
        "description": (contract.get("metadata") or {}).get("description", ""),
        "tags": (contract.get("metadata") or {}).get("tags", []),
        "health_path": (contract.get("health") or {}).get("path"),
        "health_status": contract.get("health_status", "unknown"),
        "capabilities": contract.get("capabilities") or [],
        "operations": rec.get("operations") or [],
        "provides_service": contract.get("provides_service") or None,
        "consumes": contract.get("consumes") or [],
        "grants": entry.get("grants") or [],
        "data": contract.get("data") or {"provides": [], "consumes": []},
        "data_grants": entry.get("data_grants") or [],
        "secrets": contract.get("secrets") or [],  # names only, by design
    }


def fleet_stats(cfg: dict) -> dict:
    """Live per-app usage from `docker stats` (local target): CPU%, memory,
    net I/O. One docker call for the whole fleet; apps on other targets are
    simply absent."""
    r = dockercmd.run(["stats", "--no-stream", "--format", "{{json .}}"],
                      capture=True, check=False)
    out = {}
    for line in (r.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = d.get("Name", "")
        if name.startswith("loom-"):
            out[name[5:]] = {"cpu": d.get("CPUPerc", ""),
                             "mem": d.get("MemUsage", ""),
                             "mem_pct": d.get("MemPerc", ""),
                             "net": d.get("NetIO", "")}
    return out


def services_snapshot(cfg: dict) -> dict:
    """The shared-services fabric: every service with its live provider and
    consumers (resolved grants AND unresolved consumes), plus the data-
    federation grants."""
    entries = registry.all_apps()
    services: dict[str, dict] = {}

    def svc(name: str) -> dict:
        return services.setdefault(name, {"service": name, "provider": None,
                                           "consumers": []})

    for e in entries:
        provided = (e.get("contract") or {}).get("provides_service")
        if provided:
            svc(provided)["provider"] = e["name"]
    for e in entries:
        granted = {g["service"] for g in e.get("grants") or []}
        for g in e.get("grants") or []:
            svc(g["service"])["consumers"].append(
                {"app": e["name"], "scope": g.get("scope", ""), "resolved": True})
        for c in (e.get("contract") or {}).get("consumes") or []:
            if c.get("service") and c["service"] not in granted:
                svc(c["service"])["consumers"].append(
                    {"app": e["name"], "scope": c.get("scope", ""), "resolved": False})

    data = []
    for e in entries:
        for g in e.get("data_grants") or []:
            data.append({"consumer": e["name"], "dataset": g.get("dataset"),
                         "provider": g.get("provider")})
    return {"services": sorted(services.values(), key=lambda s: s["service"]),
            "data_grants": data}


# --- the ~/dev scanner -----------------------------------------------------------

def _dns_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return name or "app"


def detect_runtime(d: Path) -> dict | None:
    """Infer how a directory could run on Loom. Returns a suggested v1
    manifest, or None if nothing recognizable is there."""
    if (d / "Dockerfile").exists():
        return {"runtime": "docker", "port": 8080}
    if (d / "package.json").exists():
        return {"runtime": "node", "port": 3000}
    if any((d / f).exists() for f in ("pyproject.toml", "requirements.txt",
                                      "app.py", "main.py")):
        return {"runtime": "python", "port": 8000}
    if (d / "index.html").exists():
        return {"runtime": "static", "port": None}
    return None


def scan(root: Path) -> list[dict]:
    """One level deep: every child dir that has a manifest or an inferable
    runtime. Dirs already in the fleet are marked deployed."""
    deployed = {e.get("source_path") for e in registry.all_apps()}
    found = []
    candidates = [root] + sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS
    ) if root.is_dir() else []
    for d in candidates:
        manifest_file = d / "fleet.app.yaml"
        if manifest_file.exists():
            try:
                m = yaml.safe_load(manifest_file.read_text()) or {}
            except yaml.YAMLError:
                m = {}
            found.append({"path": str(d), "dir": d.name, "ready": True,
                          "name": m.get("name", _dns_name(d.name)),
                          "runtime": m.get("runtime"), "port": m.get("port"),
                          "access": m.get("access", "private"),
                          "deployed": str(d) in deployed})
        elif d != root:
            hint = detect_runtime(d)
            if hint:
                found.append({"path": str(d), "dir": d.name, "ready": False,
                              "name": _dns_name(d.name), "runtime": hint["runtime"],
                              "port": hint["port"], "access": "private",
                              "deployed": str(d) in deployed})
    return found


# --- HTTP ------------------------------------------------------------------------

def make_handler(cfg: dict, default_root: Path, creds: dict | None = None,
                 allow_remote: bool = False):
    html = (Path(__file__).parent / "admin.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _loopback(self) -> bool:
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _send(self, code, obj, ctype="application/json", headers=None):
            body = (json.dumps(obj) if ctype == "application/json" else obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _gate(self) -> bool:
            """Both checks, in order: network (loopback unless explicitly
            opened) then credentials (always, once configured)."""
            if not allow_remote and not self._loopback():
                self._send(403, {"error": "loopback only"})
                return False
            if creds and not check_basic_auth(self.headers.get("Authorization"), creds):
                time.sleep(0.4)  # blunt brute-force throttle
                self._send(401, {"error": "authentication required"},
                           headers={"WWW-Authenticate": 'Basic realm="loom admin"'})
                return False
            return True

        def do_GET(self):
            if not self._gate():
                return
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/admin"):
                return self._send(200, html, ctype="text/html; charset=utf-8")
            if u.path == "/api/fleet":
                return self._send(200, {"apps": fleet_snapshot(cfg),
                                        "base_domain": cfg.get("base_domain"),
                                        "public_domain": cfg.get("public_domain") or None,
                                        "target": cfg.get("default_target")})
            if u.path == "/api/scan":
                root = Path(q.get("root", [str(default_root)])[0]).expanduser()
                return self._send(200, {"root": str(root), "candidates": scan(root)})
            if u.path == "/api/config":
                return self._send(200, config_view(cfg))
            if u.path == "/api/app":
                try:
                    return self._send(200, app_detail(cfg, q.get("name", [""])[0]))
                except LoomError as e:
                    return self._send(404, {"error": str(e)})
            if u.path == "/api/services":
                return self._send(200, services_snapshot(cfg))
            if u.path == "/api/stats":
                return self._send(200, fleet_stats(cfg))
            if u.path == "/api/logs":
                try:
                    text = app_logs(cfg, q.get("app", [""])[0],
                                    int(q.get("tail", ["200"])[0]))
                    return self._send(200, {"logs": text})
                except LoomError as e:
                    return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._gate():
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, {"error": "invalid json"})
            try:
                if self.path == "/api/deploy":
                    app_dir = Path(payload["path"]).expanduser().resolve()
                    if not app_dir.is_dir():
                        raise LoomError(f"not a directory: {app_dir}")
                    if payload.get("manifest"):
                        m = payload["manifest"]
                        doc = {"name": _dns_name(str(m.get("name", app_dir.name))),
                               "runtime": m["runtime"],
                               "access": m.get("access", "private")}
                        if m.get("port"):
                            doc["port"] = int(m["port"])
                        write_manifest(app_dir, doc)
                    entry = deploy_dir(cfg, app_dir)
                    return self._send(200, {"ok": True, "name": entry["name"],
                                            "url": entry["url"],
                                            "public_url": entry.get("public_url")})
                if self.path == "/api/import":
                    root = Path(payload.get("root") or default_root).expanduser()
                    return self._send(200, {"ok": True,
                                            **import_repo(payload["url"], root)})
                if self.path in ("/api/stop", "/api/start", "/api/remove"):
                    name = payload["name"]
                    {"/api/stop": stop_app, "/api/start": start_app,
                     "/api/remove": remove_app}[self.path](cfg, name)
                    return self._send(200, {"ok": True})
            except (LoomError, KeyError, ValueError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

    return Handler


def serve(cfg: dict, host: str = "127.0.0.1", port: int = 7879,
          root: Path | None = None, open_browser: bool = True) -> None:
    creds = load_credentials()
    remote = host not in ("127.0.0.1", "localhost")
    if remote and not creds:
        raise LoomError(
            "binding beyond loopback needs credentials: run "
            "`loom admin --set-password` first"
        )
    root = root or Path.home() / "dev"
    httpd = ThreadingHTTPServer((host, port),
                                make_handler(cfg, root, creds, allow_remote=remote))
    url = f"http://{host}:{port}"
    guard = f"basic auth as '{creds['username']}'" if creds else "loopback-only, no auth"
    ok(f"loom admin on {url}  (scanning {root}; {guard})")
    if remote:
        info("remote bind: plain HTTP — front with TLS (tailscale serve / edge proxy) "
             "on untrusted networks")
    if open_browser:
        import webbrowser
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
