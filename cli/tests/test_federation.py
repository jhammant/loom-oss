"""Tests for data federation provisioning (C7)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import registry, services  # noqa: E402

GATEWAY = {"name": "loom-fed", "service_port": 8090,
           "contract": {"provides_service": "federation"}}
PROVIDER = {"name": "data-provider", "service_port": 8100,
            "contract": {"data": {"provides": [{"name": "items", "path": "/items"}], "consumes": []}}}


def test_mint_data_token_roundtrip():
    t = services.mint_data_token("sek", "appA", "items")
    assert t == services.mint_data_token("sek", "appA", "items")
    assert t != services.mint_data_token("sek", "appB", "items")   # caller-bound
    assert t != services.mint_data_token("sek", "appA", "orders")  # dataset-bound


def test_data_provision_resolves_gateway(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [GATEWAY, PROVIDER])
    cfg = {"service_secret": "sek"}
    manifest = {"name": "shop", "data": {"consumes": [{"name": "items"}], "provides": []}}
    env, grants = services.provision_data_env(cfg, manifest)
    assert env["LOOM_DATA_ITEMS_URL"] == "http://loom-loom-fed:8090/fed/items"
    assert env["LOOM_DATA_ITEMS_TOKEN"] == services.mint_data_token("sek", "shop", "items")
    assert grants == [{"dataset": "items", "provider": "data-provider"}]


def test_data_provision_without_gateway_injects_nothing(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [PROVIDER])  # no gateway deployed
    env, grants = services.provision_data_env(
        {"service_secret": "sek"}, {"name": "shop", "data": {"consumes": [{"name": "items"}], "provides": []}})
    assert env == {} and grants == []


def test_find_dataset_provider(monkeypatch):
    monkeypatch.setattr(registry, "all_apps", lambda: [GATEWAY, PROVIDER])
    entry, ds = services.find_dataset_provider("items")
    assert entry["name"] == "data-provider" and ds["path"] == "/items"
    assert services.find_dataset_provider("nope") == (None, None)
