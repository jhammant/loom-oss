"""Tests for the Loom Python SDK — the identity() header parser (no network)."""
import sys
from pathlib import Path

# The SDK lives at sdk/python/loom_sdk.py (vendored into apps); import it directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

import loom_sdk  # noqa: E402


def test_identity_from_forward_auth_headers():
    ident = loom_sdk.identity({
        "Remote-User": "alice",
        "Remote-Email": "alice@example.com",
        "Remote-Name": "Alice A.",
        "Remote-Groups": "admins, beta",
    })
    assert ident.is_authenticated
    assert ident.user == "alice"
    assert ident.email == "alice@example.com"
    assert ident.name == "Alice A."
    assert ident.groups == ["admins", "beta"]  # split + stripped


def test_identity_falls_back_to_x_forwarded():
    ident = loom_sdk.identity({
        "X-Forwarded-User": "bob",
        "X-Forwarded-Email": "bob@example.com",
        "X-Forwarded-Groups": "users",
    })
    assert ident.user == "bob"
    assert ident.email == "bob@example.com"
    assert ident.groups == ["users"]


def test_identity_empty_for_public_request():
    ident = loom_sdk.identity({})
    assert not ident.is_authenticated
    assert ident.user == "" and ident.email == "" and ident.groups == []


def test_identity_tolerates_objects_without_get():
    # a mapping whose .get raises must not blow up the parser
    class Weird:
        def get(self, _):
            raise RuntimeError("no")
    ident = loom_sdk.identity(Weird())
    assert not ident.is_authenticated
