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
