"""First tests in the repo: the app contract (manifest v1 compat + v2)."""
import sys
from pathlib import Path

import pytest

# Make the `loom` package importable without relying on an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import contract, manifest  # noqa: E402
from loom.util import LoomError  # noqa: E402


def write(tmp_path, text: str) -> Path:
    (tmp_path / "fleet.app.yaml").write_text(text)
    return tmp_path


# --- v1 backward compatibility -------------------------------------------------

def test_v1_manifest_normalizes_with_contract_defaults(tmp_path):
    d = write(tmp_path, "name: legacy\nruntime: static\naccess: public\n")
    m = manifest.load_manifest(d)
    assert m["name"] == "legacy"
    assert m["manifest_version"] == 1
    assert m["capabilities"] == []
    assert m["health"]["path"] == "/health"
    assert m["consumes"] == []
    assert m["data"] == {"provides": [], "consumes": []}


def test_snapshot_shape_for_v1():
    snap = contract.snapshot(contract.normalize({}) | {"name": "x"})
    assert snap["harvested_at"] is None
    assert snap["health_status"] == "unknown"
    assert snap["capability_index"] == []
    assert snap["capabilities"] == []


# --- v2 round-trip -------------------------------------------------------------

def test_v2_round_trip(tmp_path):
    d = write(tmp_path, """
name: demo
runtime: node
port: 3000
access: public
manifest_version: 2
metadata:
  description: A demo.
  tags: [Search, search, DEMO]
health:
  path: /healthz
capabilities:
  - id: search
    kind: http
    path: /search
    output_schema: { type: object }
consumes:
  - service: wallet
""")
    m = manifest.load_manifest(d)
    assert m["manifest_version"] == 2
    assert m["metadata"]["description"] == "A demo."
    assert m["metadata"]["tags"] == ["demo", "search"]  # deduped + lowercased + sorted
    assert m["health"]["path"] == "/healthz"
    assert [c["id"] for c in m["capabilities"]] == ["search"]
    assert m["capabilities"][0]["kind"] == "http"
    assert m["consumes"][0]["service"] == "wallet"


# --- validation ----------------------------------------------------------------

def test_duplicate_capability_id_raises():
    with pytest.raises(LoomError, match="duplicate capability id"):
        contract.parse_capabilities({"capabilities": [
            {"id": "a", "kind": "http", "path": "/a"},
            {"id": "a", "kind": "http", "path": "/b"},
        ]})


def test_bad_capability_kind_raises():
    with pytest.raises(LoomError, match="kind must be one of"):
        contract.parse_capabilities({"capabilities": [{"id": "a", "kind": "grpc", "path": "/a"}]})


def test_http_capability_missing_path_raises():
    with pytest.raises(LoomError, match="requires a 'path'"):
        contract.parse_capabilities({"capabilities": [{"id": "a", "kind": "http"}]})


def test_non_int_port_raises(tmp_path):
    d = write(tmp_path, "name: x\nruntime: node\nport: web\naccess: public\n")
    with pytest.raises(LoomError, match="port must be an integer"):
        manifest.load_manifest(d)


def test_unknown_top_level_key_is_ignored(tmp_path):
    d = write(tmp_path, "name: x\nruntime: static\naccess: public\nfuture_thing: 42\n")
    m = manifest.load_manifest(d)  # must not raise
    assert m["name"] == "x"


def test_unknown_consumes_service_warns_not_raises():
    # not a KNOWN_SERVICE -> warns but is allowed (does not raise)
    out = contract.parse_consumes({"consumes": [{"service": "weather"}]})
    assert out[0]["service"] == "weather"


def test_newer_manifest_version_is_accepted():
    out = contract.normalize({"manifest_version": 99})  # warns, does not raise
    assert out["manifest_version"] == 99
