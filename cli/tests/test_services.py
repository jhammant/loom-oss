"""Tests for shared-services provisioning (C6)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import registry, services  # noqa: E402


def test_mint_verify_roundtrip():
    s = "sekret"
    t = services.mint_token(s, "appA", "wallet")
    assert services.verify_token(s, t, "appA", "wallet")
    assert not services.verify_token(s, t, "appB", "wallet")   # wrong caller
    assert not services.verify_token(s, t, "appA", "email")    # wrong service


def test_provision_resolves_provider(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [
        {"name": "loom-wallet", "service_port": 8080, "contract": {"provides_service": "wallet"}},
    ])
    cfg = {"service_secret": "sekret"}
    manifest = {"name": "shop", "consumes": [{"service": "wallet", "scope": "charge"}]}
    env, grants = services.provision_env(cfg, manifest)
    assert env["LOOM_WALLET_URL"] == "http://loom-loom-wallet:8080"
    assert env["LOOM_WALLET_TOKEN"] == services.mint_token("sekret", "shop", "wallet")
    assert grants == [{"service": "wallet", "provider": "loom-wallet", "scope": "charge"}]


def test_provision_unresolved_injects_nothing(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [])
    env, grants = services.provision_env(
        {"service_secret": "sekret"}, {"name": "shop", "consumes": [{"service": "wallet"}]})
    assert env == {} and grants == []


def test_provider_env_carries_secret_and_service():
    env = services.provider_env(
        {"service_secret": "sekret"}, {"name": "loom-wallet", "provides_service": "wallet"})
    assert env == {"LOOM_SERVICE": "wallet", "LOOM_SERVICE_SECRET": "sekret"}


def test_non_provider_gets_no_env():
    assert services.provider_env({"service_secret": "x"}, {"name": "plain"}) == {}


def test_provision_resolves_llm_provider(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [
        {"name": "loom-llm", "service_port": 8095, "contract": {"provides_service": "llm"}},
    ])
    cfg = {"service_secret": "sekret"}
    manifest = {"name": "chatapp", "consumes": [{"service": "llm", "scope": ""}]}
    env, grants = services.provision_env(cfg, manifest)
    assert env["LOOM_LLM_URL"] == "http://loom-loom-llm:8095"
    assert env["LOOM_LLM_TOKEN"] == services.mint_token("sekret", "chatapp", "llm")
    assert grants == [{"service": "llm", "provider": "loom-llm", "scope": ""}]


def test_secret_env_injects_declared_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "secrets.json").write_text('{"ANTHROPIC_API_KEY": "sk-test-123"}')
    env = services.secret_env({"name": "loom-llm", "secrets": ["ANTHROPIC_API_KEY"]})
    assert env == {"ANTHROPIC_API_KEY": "sk-test-123"}


def test_secret_env_skips_missing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    (tmp_path / "fleet").mkdir()  # no secrets.json at all
    env = services.secret_env({"name": "loom-llm", "secrets": ["ANTHROPIC_API_KEY"]})
    assert env == {}  # missing secret warns + is skipped, never blocks deploy


def test_secret_env_no_declared_secrets_is_noop(monkeypatch):
    # no filesystem access at all when nothing is declared
    monkeypatch.setattr(services, "paths", lambda: (_ for _ in ()).throw(AssertionError("touched fs")))
    assert services.secret_env({"name": "plain"}) == {}
