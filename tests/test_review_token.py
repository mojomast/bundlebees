"""Approved acceptance tests for HR1: human-review tokens (written first, RED).

Contract: docs/tasks/HR1_HUMAN_REVIEW_TOKEN.md (AC-001 .. AC-006 here;
the CLI and report criteria are in tests/test_cli_review_token.py).

A human review is accepted only with an Ed25519-signed token minted by the
human, bound to the reviewer label, contract id and hash, the approved test
revision (``source_rev``), the evidence chain head, an expiry and a nonce.
Every key in these tests is throwaway and generated in memory.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _crypto():
    if os.environ.get("CI"):
        import cryptography  # noqa: F401  (required in CI; a skip would hide a gap)
    else:
        pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey, serialization


@pytest.fixture()
def rt():
    from bundlebees_plugin import review_token

    return review_token


def _keypair(tmp_path, name="kyle"):
    Ed25519PrivateKey, serialization = _crypto()
    private = Ed25519PrivateKey.generate()
    priv_path = tmp_path / f"{name}.pem"
    priv_path.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(priv_path, 0o600)
    pub_path = tmp_path / f"{name}.pub.pem"
    pub_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv_path, pub_path, "ed25519:" + base64.b64encode(raw).decode()


BINDING = {
    "contract_id": "HR1-demo",
    "contract_sha256": "a" * 64,
    "test_rev": "b" * 40,
    "chain_head": "c" * 64,
}
NOW = 1_800_000_000


def _mint(rt, priv, **over):
    args = dict(reviewer="kyle", ttl_s=900, now=NOW, **BINDING)
    args.update(over)
    return rt.mint(rt.load_private_key(priv), **args)


def _verify(rt, token, pub, **over):
    args = dict(BINDING, now=NOW + 60)
    args.update(over)
    return rt.verify(token, rt.load_public_key(str(pub)), **args)


# AC-001: the token binds every required claim, and mint refuses gaps.
def test_mint_binds_reviewer_contract_test_rev_chain_head_expiry_and_nonce(rt, tmp_path):
    priv, pub, _ = _keypair(tmp_path)
    token = _mint(rt, priv)
    assert token.startswith("bbrt1.") and token.count(".") == 2
    claims = rt.decode_unverified(token)
    assert claims["typ"] == "bundlebees.review-token/v1"
    assert claims["reviewer"] == "kyle"
    assert claims["contract_id"] == BINDING["contract_id"]
    assert claims["contract_sha256"] == BINDING["contract_sha256"]
    assert claims["test_rev"] == BINDING["test_rev"]
    assert claims["chain_head"] == BINDING["chain_head"]
    assert claims["iat"] == NOW and claims["exp"] == NOW + 900
    assert len(claims["nonce"]) == 32 and int(claims["nonce"], 16) >= 0
    assert claims["kid"] == rt.key_id(rt.load_public_key(str(pub)))
    assert claims["verdict"] == "approve"
    assert _mint(rt, priv) != token  # fresh nonce every time


def test_mint_refuses_bad_or_missing_binding(rt, tmp_path):
    priv, _, _ = _keypair(tmp_path)
    for bad in ({"reviewer": ""}, {"contract_sha256": "nothex"}, {"test_rev": "short"},
                {"chain_head": ""}, {"ttl_s": 0}, {"ttl_s": rt.MAX_TTL_S + 1}, {"contract_id": "../x"}):
        with pytest.raises(rt.ReviewTokenError) as exc:
            _mint(rt, priv, **bad)
        assert exc.value.code == "token_invalid_request", bad


# AC-002: a valid token bound to the exact binding verifies.
def test_valid_token_verifies_with_pem_or_inline_public_key(rt, tmp_path):
    priv, pub, inline = _keypair(tmp_path)
    token = _mint(rt, priv)
    for key in (pub, inline):
        claims = _verify(rt, token, key)
        assert claims["reviewer"] == "kyle" and claims["chain_head"] == BINDING["chain_head"]


# AC-003: anything else fails closed with a named reason.
@pytest.mark.parametrize("over,code", [
    ({"contract_id": "HR1-other"}, "token_binding_mismatch"),
    ({"contract_sha256": "d" * 64}, "token_binding_mismatch"),
    ({"test_rev": "e" * 40}, "token_binding_mismatch"),
    ({"chain_head": "f" * 64}, "token_binding_mismatch"),
    ({"now": NOW + 901 + 120}, "token_expired"),
    ({"now": NOW - 3600}, "token_not_yet_valid"),
])
def test_wrong_binding_or_time_is_refused(rt, tmp_path, over, code):
    priv, pub, _ = _keypair(tmp_path)
    with pytest.raises(rt.ReviewTokenError) as exc:
        _verify(rt, _mint(rt, priv), pub, **over)
    assert exc.value.code == code


def test_wrong_key_tampered_or_malformed_token_is_refused(rt, tmp_path):
    priv, pub, _ = _keypair(tmp_path)
    _, other_pub, _ = _keypair(tmp_path, "mallory")
    token = _mint(rt, priv)
    with pytest.raises(rt.ReviewTokenError) as exc:
        _verify(rt, token, other_pub)
    assert exc.value.code == "token_bad_signature"
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["reviewer"] = "mallory"
    forged = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()).decode().rstrip("=")
    with pytest.raises(rt.ReviewTokenError) as exc:
        _verify(rt, f"{head}.{forged}.{sig}", pub)
    assert exc.value.code == "token_bad_signature"
    for junk in ("", "bbrt1", "bbrt0.a.b", "bbrt1.!!.??", token + ".x", "x" * 5000):
        with pytest.raises(rt.ReviewTokenError) as exc:
            _verify(rt, junk, pub)
        assert exc.value.code == "token_malformed", junk


def test_signed_token_with_overlong_lifetime_is_refused(rt, tmp_path):
    priv, pub, _ = _keypair(tmp_path)
    key = rt.load_private_key(priv)
    claims = rt.decode_unverified(_mint(rt, priv))
    claims["exp"] = claims["iat"] + rt.MAX_TTL_S + 3600
    token = rt._encode_signed(key, claims)
    with pytest.raises(rt.ReviewTokenError) as exc:
        _verify(rt, token, pub)
    assert exc.value.code == "token_invalid_claims"


# AC-004: a token is single-use (nonce store), so it cannot be replayed.
def test_consumed_token_cannot_be_replayed(rt, tmp_path):
    priv, pub, _ = _keypair(tmp_path)
    state = tmp_path / "state"
    token = _mint(rt, priv)
    claims = _verify(rt, token, pub)
    rt.consume(state, claims, token)
    with pytest.raises(rt.ReviewTokenError) as exc:
        rt.consume(state, claims, token)
    assert exc.value.code == "token_replayed"
    store = state / "review-tokens"
    assert oct(store.stat().st_mode & 0o777) == "0o700"
    assert len(list(store.iterdir())) == 1


# AC-005: no configured public key, or an unusable one, fails closed.
def test_missing_or_bad_public_key_fails_closed(rt, tmp_path):
    for value in ("", None, "ed25519:notbase64!", str(tmp_path / "absent.pem"), "rsa:abc"):
        with pytest.raises(rt.ReviewTokenError) as exc:
            rt.load_public_key(value)
        assert exc.value.code in ("review_key_missing", "review_key_invalid"), value


def test_private_key_file_must_be_owner_only(rt, tmp_path):
    priv, _, _ = _keypair(tmp_path)
    if os.name == "posix":
        os.chmod(priv, 0o644)
        with pytest.raises(rt.ReviewTokenError) as exc:
            rt.load_private_key(priv)
        assert exc.value.code == "private_key_unsafe"


# AC-006: the repository never carries a private key; mint never prints one.
def test_repository_tracks_no_private_key():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    # A real PEM block: the BEGIN line followed by base64 key material (a scanner's
    # regex that merely names the marker, like the one in cli.py, is not a key).
    pem = re.compile(r"-----BEGIN [A-Z ]*PRIVATE " + r"KEY-----\s*\n[A-Za-z0-9+/=]{40,}")
    for name in files:
        path = ROOT / name
        if path.is_file() and path.stat().st_size < 2_000_000:
            assert not pem.search(path.read_text(encoding="utf-8", errors="ignore")), name


def test_standalone_mint_prints_only_the_token(rt, tmp_path):
    _crypto()
    from tests.test_cli_acdd import _frozen

    priv, pub, _ = _keypair(tmp_path)
    frozen = _frozen(BINDING["test_rev"])
    contract = tmp_path / "frozen.json"
    contract.write_text(json.dumps(frozen))
    out = subprocess.run(
        [sys.executable, "-m", "bundlebees_plugin.review_token", "mint", "--private-key", str(priv),
         "--contract", str(contract), "--chain-head", BINDING["chain_head"], "--reviewer", "kyle", "--ttl", "600"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("bbrt1.")
    assert "PRIVATE" not in out.stdout + out.stderr
    claims = rt.verify(lines[0], rt.load_public_key(str(pub)), contract_id=frozen["id"],
                       contract_sha256=frozen["review"]["contract_sha256"], test_rev=BINDING["test_rev"],
                       chain_head=BINDING["chain_head"])
    assert claims["exp"] - claims["iat"] == 600
    pub_out = subprocess.run([sys.executable, "-m", "bundlebees_plugin.review_token", "pubkey",
                              "--private-key", str(priv)], cwd=ROOT, capture_output=True, text=True)
    assert pub_out.returncode == 0 and pub_out.stdout.strip().startswith("ed25519:")
    assert "PRIVATE" not in pub_out.stdout
