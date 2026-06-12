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


# --- secrets + llm service -----------------------------------------------------

def test_secrets_parse_and_snapshot(tmp_path):
    d = write(tmp_path, """
name: loom-llm
runtime: python
port: 8095
access: private
manifest_version: 2
provides_service: llm
secrets:
  - ANTHROPIC_API_KEY
""")
    m = manifest.load_manifest(d)
    assert m["provides_service"] == "llm"
    assert m["secrets"] == ["ANTHROPIC_API_KEY"]
    assert contract.snapshot(m)["secrets"] == ["ANTHROPIC_API_KEY"]


def test_secrets_must_be_list_of_strings():
    with pytest.raises(LoomError, match="secrets must be a list"):
        contract.normalize({"secrets": "ANTHROPIC_API_KEY"})  # str, not a list
    with pytest.raises(LoomError, match="secrets must be a list"):
        contract.normalize({"secrets": [123]})  # non-string entry


def test_secrets_default_empty():
    assert contract.normalize({})["secrets"] == []


def test_llm_is_a_known_service():
    # consumes: [llm] must NOT warn (it's a first-class service now)
    out = contract.parse_consumes({"consumes": [{"service": "llm"}]})
    assert out[0]["service"] == "llm"
    assert "llm" in contract.KNOWN_SERVICES


def test_allow_parses_users_and_groups():
    from loom import contract
    out = contract.parse_allow({"access": "gated",
                                "allow": {"users": ["jon"], "groups": ["family", "admins"]}})
    assert out == {"users": ["jon"], "groups": ["family", "admins"]}
    assert contract.parse_allow({}) == {"users": [], "groups": []}


def test_allow_rejects_rule_injection():
    import pytest
    from loom import contract
    from loom.util import LoomError
    with pytest.raises(LoomError, match="quotes/backticks"):
        contract.parse_allow({"access": "gated",
                              "allow": {"users": ["jo`) || Host(`evil"]}})


def test_route_file_with_allow_writes_authz_routers(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    from loom import proxy
    cfg = {"base_domain": "loom.localhost", "public_domain": "loom.example.com"}
    proxy.write_route(cfg, "fam", "loom-fam", 80,
                      allow={"users": ["jon"], "groups": ["family"]})
    text = (tmp_path / "proxy" / "dynamic" / "app-fam.yml").read_text()
    assert "Header(`Remote-User`, `jon`)" in text
    assert "HeaderRegexp(`Remote-Groups`" in text and "family" in text
    assert "fam-deny" in text and "255.255.255.255/32" in text
    # local hostname router carries no matcher (operator + health probe path)
    assert 'rule: "Host(`fam.loom.localhost`)"' in text


def test_route_file_without_allow_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    from loom import proxy
    cfg = {"base_domain": "loom.localhost", "public_domain": ""}
    proxy.write_route(cfg, "plain", "loom-plain", 80)
    text = (tmp_path / "proxy" / "dynamic" / "app-plain.yml").read_text()
    assert "deny" not in text and "priority" not in text
