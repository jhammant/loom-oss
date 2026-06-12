"""Tests for the local admin console (scanner + fleet ops + HTTP surface)."""
import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import admin, registry  # noqa: E402
from loom.util import LoomError  # noqa: E402


# --- scanner ---------------------------------------------------------------------

def test_detect_runtime_priorities(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert admin.detect_runtime(tmp_path)["runtime"] == "node"
    (tmp_path / "Dockerfile").write_text("FROM scratch")
    assert admin.detect_runtime(tmp_path)["runtime"] == "docker"


def test_detect_runtime_python_and_static(tmp_path):
    py = tmp_path / "py"; py.mkdir(); (py / "app.py").write_text("")
    st = tmp_path / "st"; st.mkdir(); (st / "index.html").write_text("")
    nothing = tmp_path / "n"; nothing.mkdir()
    assert admin.detect_runtime(py)["runtime"] == "python"
    assert admin.detect_runtime(st)["runtime"] == "static"
    assert admin.detect_runtime(nothing) is None


def test_scan_finds_manifests_and_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "home"))
    ready = tmp_path / "has-manifest"; ready.mkdir()
    ready_yaml = "name: shiny\nruntime: node\nport: 3000\naccess: public\n"
    (ready / "fleet.app.yaml").write_text(ready_yaml)
    raw = tmp_path / "My_NodeApp"; raw.mkdir()
    (raw / "package.json").write_text("{}")
    skip = tmp_path / "node_modules"; skip.mkdir()
    (skip / "package.json").write_text("{}")
    boring = tmp_path / "no-runtime"; boring.mkdir()

    found = {c["dir"]: c for c in admin.scan(tmp_path)}
    assert found["has-manifest"]["ready"] is True
    assert found["has-manifest"]["name"] == "shiny"
    assert found["My_NodeApp"]["ready"] is False
    assert found["My_NodeApp"]["name"] == "my-nodeapp"  # DNS-safe suggestion
    assert found["My_NodeApp"]["runtime"] == "node"
    assert "node_modules" not in found and "no-runtime" not in found


def test_scan_marks_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "home"))
    d = tmp_path / "app"; d.mkdir()
    (d / "fleet.app.yaml").write_text("name: app\nruntime: python\nport: 80\naccess: private\n")
    monkeypatch.setattr(registry, "all_apps",
                        lambda: [{"name": "app", "source_path": str(d)}])
    assert admin.scan(tmp_path)[0]["deployed"] is True


def test_write_manifest_refuses_overwrite(tmp_path):
    admin.write_manifest(tmp_path, {"name": "x", "runtime": "static", "access": "private"})
    assert (tmp_path / "fleet.app.yaml").exists()
    with pytest.raises(LoomError):
        admin.write_manifest(tmp_path, {"name": "y"})


# --- HTTP surface ------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "home"))
    deployed = []

    def fake_deploy(cfg, app_dir):
        deployed.append(app_dir)
        return {"name": app_dir.name, "url": f"https://{app_dir.name}.test", "access": "private"}

    monkeypatch.setattr(admin, "deploy_dir", fake_deploy)
    monkeypatch.setattr(admin, "fleet_snapshot", lambda cfg: [
        {"name": "a", "status": "running", "health": "ok", "url": "https://a.test",
         "public_url": None, "runtime": "node", "access": "public",
         "target": "local", "source_path": "/x"}])
    cfg = {"base_domain": "loom.localhost", "default_target": "local", "public_domain": ""}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), admin.make_handler(cfg, tmp_path))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", deployed, tmp_path
    httpd.shutdown()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.getcode(), r.read()


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode(), json.loads(r.read())


def test_admin_serves_page_and_fleet(server):
    base, _, _ = server
    code, body = _get(base + "/")
    assert code == 200 and b"fleet console" in body
    code, body = _get(base + "/api/fleet")
    data = json.loads(body)
    assert code == 200 and data["apps"][0]["name"] == "a"


def test_admin_deploy_writes_manifest_then_deploys(server):
    base, deployed, tmp_path = server
    app = tmp_path / "newapp"; app.mkdir()
    (app / "package.json").write_text("{}")
    code, resp = _post(base + "/api/deploy", {
        "path": str(app),
        "manifest": {"name": "NewApp!", "runtime": "node", "port": 3000},
    })
    assert code == 200 and resp["ok"]
    import yaml
    doc = yaml.safe_load((app / "fleet.app.yaml").read_text())
    assert doc == {"name": "newapp", "runtime": "node", "access": "private", "port": 3000}
    assert deployed == [app]


def test_admin_deploy_rejects_bad_path(server):
    base, _, _ = server
    try:
        _post(base + "/api/deploy", {"path": "/nope/missing"})
        assert False, "expected 400"
    except urllib.error.HTTPError as e:
        assert e.code == 400


# --- config view + git import -------------------------------------------------

def test_config_view_masks_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "secrets.json").write_text('{"ANTHROPIC_API_KEY": "sk-real"}')
    view = admin.config_view({"base_domain": "x", "service_secret": "topsecret"})
    assert view["config"]["service_secret"] == "•••"
    assert view["secret_names"] == ["ANTHROPIC_API_KEY"]
    assert "sk-real" not in json.dumps(view)


def test_import_repo_expands_shorthand_and_validates(tmp_path, monkeypatch):
    import subprocess
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R: returncode = 0; stderr = ""
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = admin.import_repo("someuser/cool-app", tmp_path)
    assert out["path"] == str(tmp_path / "cool-app")
    assert calls[0][:4] == ["git", "clone", "--depth", "1"]
    assert calls[0][4] == "https://github.com/someuser/cool-app.git"
    with pytest.raises(LoomError, match="not a git URL"):
        admin.import_repo("ftp://nope", tmp_path)
    (tmp_path / "exists").mkdir()
    with pytest.raises(LoomError, match="already exists"):
        admin.import_repo("https://github.com/x/exists.git", tmp_path)


# --- auth -----------------------------------------------------------------------

def _basic(user, pw):
    import base64
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_password_hash_roundtrip():
    rec = admin.hash_password("hunter2hunter2", iterations=1000)
    assert admin.verify_password("hunter2hunter2", rec)
    assert not admin.verify_password("wrong-password", rec)


def test_set_credentials_validates_and_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    with pytest.raises(LoomError, match="at least 8"):
        admin.set_credentials("jon", "short")
    admin.set_credentials("jon", "longenough")
    creds = admin.load_credentials()
    assert creds["username"] == "jon"
    assert "longenough" not in (tmp_path / "fleet" / "secrets.json").read_text()
    assert admin.check_basic_auth(_basic("jon", "longenough"), creds)
    assert not admin.check_basic_auth(_basic("jon", "nope-nope"), creds)
    assert not admin.check_basic_auth(_basic("eve", "longenough"), creds)
    assert not admin.check_basic_auth(None, creds)


def test_server_enforces_basic_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(admin, "fleet_snapshot", lambda cfg: [])
    creds = {"username": "jon", **admin.hash_password("longenough", iterations=1000)}
    cfg = {"base_domain": "x", "default_target": "local", "public_domain": ""}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                admin.make_handler(cfg, tmp_path, creds))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        try:
            _get(base + "/api/fleet")
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
            assert e.headers["WWW-Authenticate"].startswith("Basic")
        req = urllib.request.Request(base + "/api/fleet",
                                     headers={"Authorization": _basic("jon", "longenough")})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.getcode() == 200
    finally:
        httpd.shutdown()


def test_remote_bind_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    with pytest.raises(LoomError, match="set-password"):
        admin.serve({"base_domain": "x"}, host="0.0.0.0", open_browser=False)


# --- app detail + services fabric + stats ----------------------------------------

def test_app_detail_shapes_contract(monkeypatch):
    from loom import library
    monkeypatch.setattr(registry, "get", lambda n: {
        "name": "shop", "status": "running", "runtime": "python", "access": "public",
        "url": "https://shop.test", "public_url": None, "source_path": "/x",
        "image": "loom/shop", "grants": [{"service": "wallet", "provider": "loom-wallet",
                                          "scope": "charge"}],
        "data_grants": [],
        "contract": {"metadata": {"description": "a shop", "tags": ["demo"]},
                     "health": {"path": "/health"}, "health_status": "ok",
                     "capabilities": [{"id": "buy", "kind": "http", "path": "/buy"}],
                     "consumes": [{"service": "wallet", "scope": "charge"}],
                     "provides_service": "", "secrets": ["STRIPE_KEY"],
                     "data": {"provides": [], "consumes": []}},
    })
    monkeypatch.setattr(library, "get", lambda n: {"operations": [
        {"id": "buy", "method": "POST", "path": "/buy"}]})
    d = admin.app_detail({}, "shop")
    assert d["description"] == "a shop"
    assert d["operations"][0]["method"] == "POST"
    assert d["grants"][0]["provider"] == "loom-wallet"
    assert d["secrets"] == ["STRIPE_KEY"]
    assert d["provides_service"] is None


def test_services_snapshot_resolved_and_unresolved(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [
        {"name": "loom-wallet", "contract": {"provides_service": "wallet"}},
        {"name": "shop", "grants": [{"service": "wallet", "provider": "loom-wallet",
                                     "scope": "charge"}],
         "contract": {"consumes": [{"service": "wallet", "scope": "charge"}]}},
        {"name": "wisher", "grants": [],
         "contract": {"consumes": [{"service": "email", "scope": ""}]}},
        {"name": "feddy", "data_grants": [{"dataset": "orders", "provider": "shop"}]},
    ])
    snap = admin.services_snapshot({})
    by = {s["service"]: s for s in snap["services"]}
    assert by["wallet"]["provider"] == "loom-wallet"
    assert by["wallet"]["consumers"] == [{"app": "shop", "scope": "charge",
                                          "resolved": True}]
    assert by["email"]["provider"] is None
    assert by["email"]["consumers"][0] == {"app": "wisher", "scope": "",
                                           "resolved": False}
    assert snap["data_grants"] == [{"consumer": "feddy", "dataset": "orders",
                                    "provider": "shop"}]


def test_fleet_stats_parses_docker_lines(monkeypatch):
    from loom import dockercmd

    class R:
        stdout = ('{"Name":"loom-shop","CPUPerc":"1.2%","MemUsage":"30MiB / 8GiB",'
                  '"MemPerc":"0.4%","NetIO":"1kB / 2kB"}\n'
                  '{"Name":"unrelated","CPUPerc":"9%"}\n'
                  'not-json\n')

    monkeypatch.setattr(dockercmd, "run", lambda *a, **k: R())
    s = admin.fleet_stats({})
    assert s == {"shop": {"cpu": "1.2%", "mem": "30MiB / 8GiB",
                          "mem_pct": "0.4%", "net": "1kB / 2kB"}}
