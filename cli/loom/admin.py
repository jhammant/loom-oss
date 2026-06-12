"""`loom admin` — the local fleet console.

A single-user, localhost-only web page over the live fleet: see every app
(status/health/URLs), stop/start/remove with one click, and "warp in" new
apps by scanning a directory (default ~/dev) for deployable candidates —
dirs that already carry a fleet.app.yaml, or whose runtime Loom can infer
(Dockerfile/package.json/index.html/pyproject). Deploying a candidate with
no manifest writes a fleet.app.yaml next to the app first (the manifest
always lives with the app — that's the contract), then deploys it.

This is the LOCAL admin for a single fleet. It binds 127.0.0.1 and refuses
non-loopback callers; it is not the hosted multi-tenant dashboard (that is
a commercial-product concern, see OPEN-CORE.md).
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import dockercmd, gateway, harvester, library, registry
from .manifest import load_manifest
from .targets import get_target
from .util import LoomError, ok

SKIP_DIRS = {"node_modules", "dist", "build", "out", "venv", "__pycache__",
             ".git", ".venv", ".next", ".cache", "target"}


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

def make_handler(cfg: dict, default_root: Path):
    html = (Path(__file__).parent / "admin.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _loopback(self) -> bool:
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _send(self, code, obj, ctype="application/json"):
            body = (json.dumps(obj) if ctype == "application/json" else obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._loopback():
                return self._send(403, {"error": "loopback only"})
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
            if u.path == "/api/logs":
                try:
                    text = app_logs(cfg, q.get("app", [""])[0],
                                    int(q.get("tail", ["200"])[0]))
                    return self._send(200, {"logs": text})
                except LoomError as e:
                    return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._loopback():
                return self._send(403, {"error": "loopback only"})
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
    if host != "127.0.0.1":
        raise LoomError("loom admin is local-only; it binds 127.0.0.1")
    root = root or Path.home() / "dev"
    httpd = ThreadingHTTPServer((host, port), make_handler(cfg, root))
    url = f"http://{host}:{port}"
    ok(f"loom admin on {url}  (scanning {root})")
    if open_browser:
        import webbrowser
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
