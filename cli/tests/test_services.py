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
