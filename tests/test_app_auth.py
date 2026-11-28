"""GitHub App identities for the coordinator and the reviewer (program: Phase 3).

The coordinator's GitHub calls move to installation tokens minted from the coordinator app's
private key; the verdict poster runs as its own OS user and is the only reader of the
reviewer app's key. A key file that is group/other readable is refused, the token never
carries the key material back to the caller, and tokens are reused until 60 s before expiry.

Run: python -m pytest tests/test_app_auth.py -q
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


NOW = 1_800_000_000


def _key(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "app.pem"
    # Assembled at runtime so the fixture never carries a key-shaped literal in history.
    header = "-----BEGIN " + "PRIVATE KEY-----"
    footer = "-----END " + "PRIVATE KEY-----"
    path.write_text(header + "\nfake\n" + footer + "\n")
    os.chmod(path, mode)
    return path


def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.app_auth")


def test_claims_are_short_lived_and_issued_for_the_app():
    m = _load()
    claims = m.build_claims(1234, NOW)
    assert claims["iss"] == 1234
    assert claims["iat"] == NOW - 60
    assert claims["exp"] == NOW + 540


def test_installation_token_is_cached_until_just_before_expiry():
    m = _load()
    calls = []

    def signer(claims):
        return "jwt"

    def http(url, headers, body):
        calls.append((url, body))
        return {"token": "ghs_abc", "expires_at": "2026-11-27T21:00:00Z"}

    cache: dict = {}
    first = m.installation_token(1234, 99, signer, http, cache, now=NOW)
    second = m.installation_token(1234, 99, signer, http, cache, now=NOW + 30)
    assert first["token"] == second["token"] == "ghs_abc"
    assert len(calls) == 1
    third = m.installation_token(1234, 99, signer, http, cache, now=NOW + 7200)
    assert len(calls) == 2
    assert third["token"] == "ghs_abc"


def test_group_readable_key_is_refused(tmp_path: Path):
    m = _load()
    path = _key(tmp_path, 0o644)
    with pytest.raises(m.AppAuthError) as caught:
        m.load_private_key(path)
    assert caught.value.code == "app_key_permissions"


def test_token_for_reads_the_configured_app_without_leaking_the_key(tmp_path: Path):
    m = _load()
    config = {
        "github_apps": {
            "coordinator": {
                "app_id": 1234,
                "installation_id": 99,
                "key_path": str(_key(tmp_path)),
            }
        }
    }
    result = m.token_for(config, lambda claims: "jwt",
                       lambda url, headers, body: {"token": "ghs_abc", "expires_at": "2026-11-27T21:00:00Z"},
                       now=NOW)
    assert result["token"] == "ghs_abc"
    assert "private" not in str(result).lower()
    with pytest.raises(m.AppAuthError) as caught:
        m.token_for({}, lambda c: "jwt", lambda *a: {}, now=NOW)
    assert caught.value.code == "app_not_configured"


def test_reviewer_key_is_only_readable_by_the_poster_user(tmp_path: Path):
    m = _load()
    path = _key(tmp_path, 0o600)
    config = {"github_apps": {"reviewer": {"key_path": str(path), "poster_user": "rizzstack-reviewer"}}}
    assert m.reviewer_key_path(config) == path
    os.chmod(path, 0o640)
    with pytest.raises(m.AppAuthError) as caught:
        m.reviewer_key_path(config)
    assert caught.value.code == "app_key_permissions"
