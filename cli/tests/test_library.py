"""Tests for the harvester + Library (C3)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loom import contract, harvester, library  # noqa: E402


def _entry(name, caps=(), desc="", tags=()):
    manifest = {"name": name, "metadata": {"description": desc, "tags": list(tags)},
                "capabilities": list(caps), "health": {"path": "/health"},
                "consumes": [], "data": {"provides": [], "consumes": []}}
    return {"name": name, "url": f"http://127.0.0.1:9/{name}", "access": "public",
            "runtime": "node", "contract": contract.snapshot(manifest)}


def test_harvest_synthesizes_web_and_keeps_http_capability():
    e = _entry("svc", caps=[{"id": "search", "kind": "http", "path": "/search"}], desc="finds things")
    rec = harvester.harvest_app({}, e)
    paths = [(o["method"], o["path"]) for o in rec["operations"]]
    assert ("GET", "/") in paths          # web endpoint always synthesized
    assert ("GET", "/search") in paths    # declared http capability present
    # no duplicate (method, path) pairs
    assert len(paths) == len(set(paths))


def test_library_upsert_search_drop_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    (tmp_path / "fleet").mkdir()
    library.upsert(harvester.harvest_app({}, _entry(
        "searchly", caps=[{"id": "query", "kind": "http", "path": "/q"}],
        desc="full text search engine", tags=["search"])))
    library.upsert(harvester.harvest_app({}, _entry(
        "mailer", desc="send transactional email", tags=["email"])))

    hits = [r["name"] for r in library.search("search")]
    assert hits and hits[0] == "searchly"      # name/tag/desc all match -> ranks first
    assert "mailer" not in hits                 # no lexical overlap with "search"

    assert library.get("mailer") is not None
    library.drop("mailer")
    assert library.get("mailer") is None


def test_inverted_index_is_built_on_save(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_HOME", str(tmp_path))
    (tmp_path / "fleet").mkdir()
    library.upsert(harvester.harvest_app({}, _entry("wallet", desc="credits ledger")))
    idx = library.load()["_index"]
    assert "wallet" in idx and "wallet" in idx["wallet"]
    assert "credits" in idx
