"""Human-review tokens: Ed25519 approvals that only the human can mint (HR1).

A REVIEW counts as a *human* review only with a token the human signed
locally with a private key that never leaves them. bundlebees holds only the
public key (setting ``review_public_key``) and checks that the token:

* is signed by that key (``token_bad_signature`` otherwise);
* binds this contract id and sha256, its approved test revision
  (``source_rev``) and the current evidence chain head
  (``token_binding_mismatch``);
* is inside its validity window, with a short maximum lifetime
  (``token_expired``, ``token_not_yet_valid``, ``token_invalid_claims``);
* has not been used before (``token_replayed``; nonces are consumed in an
  owner-only store under the plugin state directory).

Token format: ``bbrt1.<base64url(canonical JSON claims)>.<base64url(sig)>``,
where the signature covers the ASCII bytes ``bbrt1.<claims part>``.

Ed25519 comes from the ``cryptography`` package (extra ``review``). Without
it nothing verifies: every check fails closed. Mint is meant to run on the
human's own machine: ``python -m bundlebees_plugin.review_token mint``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping

PREFIX = "bbrt1"
TOKEN_TYPE = "bundlebees.review-token/v1"
MAX_TTL_S = 24 * 3600
DEFAULT_TTL_S = 3600
CLOCK_SKEW_S = 120
MAX_TOKEN_LEN = 4096

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REV = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_CID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@:-]{0,63}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_CLAIMS = ("typ", "kid", "reviewer", "contract_id", "contract_sha256", "test_rev",
           "chain_head", "verdict", "iat", "exp", "nonce")


class ReviewTokenError(Exception):
    def __init__(self, code: str, message: str, fix: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix or "Mint a fresh token with `review-token mint` for the current chain head."


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except Exception as exc:  # noqa: BLE001 - optional dependency
        raise ReviewTokenError(
            "review_key_invalid",
            "Ed25519 support (the cryptography package) is not installed",
            "Install bundlebees with the `review` extra; until then no human review verifies.",
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, serialization, InvalidSignature


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    if not _B64URL.match(text or ""):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(claims: Mapping[str, Any]) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


# ----------------------------------------------------------------- keys


def load_public_key(value: Any):
    """A PEM file path, or ``ed25519:<base64 raw 32 bytes>``. Fails closed."""
    _, Ed25519PublicKey, serialization, _ = _crypto()
    text = str(value or "").strip()
    if not text:
        raise ReviewTokenError("review_key_missing", "no review_public_key is configured",
                               "Set review_public_key to the public key Kyle printed with `review-token pubkey`.")
    if text.startswith("ed25519:"):
        try:
            raw = base64.b64decode(text[len("ed25519:"):], validate=True)
            if len(raw) != 32:
                raise ValueError("wrong length")
            return Ed25519PublicKey.from_public_bytes(raw)
        except (ValueError, binascii.Error) as exc:
            raise ReviewTokenError("review_key_invalid", "review_public_key is not a valid Ed25519 key") from exc
    if re.match(r"^[a-z0-9]+:", text) and not os.path.exists(text):
        raise ReviewTokenError("review_key_invalid", "review_public_key must be ed25519:<base64> or a PEM file")
    path = Path(text).expanduser()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ReviewTokenError("review_key_missing", "review_public_key file is not readable") from exc
    if b"PRIVATE KEY" in data:
        raise ReviewTokenError("review_key_invalid",
                               "review_public_key points at a private key; configure only the public key")
    try:
        key = serialization.load_pem_public_key(data)
    except Exception as exc:  # noqa: BLE001
        raise ReviewTokenError("review_key_invalid", "review_public_key is not a PEM public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ReviewTokenError("review_key_invalid", "review_public_key is not an Ed25519 key")
    return key


def load_private_key(path: Any):
    """Load the human's PEM (PKCS8) Ed25519 private key; owner-only file required."""
    Ed25519PrivateKey, _, serialization, _ = _crypto()
    p = Path(str(path or "")).expanduser()
    try:
        st = os.stat(p)
    except OSError as exc:
        raise ReviewTokenError("private_key_unsafe", "private key file is not readable", "Pass --private-key PATH.") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ReviewTokenError("private_key_unsafe", "private key path is not a regular file", "Pass --private-key PATH.")
    if os.name == "posix" and st.st_mode & 0o077:
        raise ReviewTokenError("private_key_unsafe", "private key file is readable by others",
                               "Run `chmod 600` on the private key file.")
    try:
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    except TypeError as exc:
        raise ReviewTokenError("private_key_unsafe", "encrypted private keys are not supported yet",
                               "Use an unencrypted PKCS8 key kept on an owner-only, encrypted disk.") from exc
    except Exception as exc:  # noqa: BLE001
        raise ReviewTokenError("private_key_unsafe", "private key file is not a PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ReviewTokenError("private_key_unsafe", "private key is not Ed25519")
    return key


def _raw_public(public_key) -> bytes:
    _, _, serialization, _ = _crypto()
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(public_key) -> str:
    """First 16 hex chars of sha256(raw public key)."""
    return hashlib.sha256(_raw_public(public_key)).hexdigest()[:16]


def public_key_line(public_key) -> str:
    return "ed25519:" + base64.b64encode(_raw_public(public_key)).decode("ascii")


# ----------------------------------------------------------------- mint / verify


def _check_binding(reviewer: Any, contract_id: Any, contract_sha256: Any, test_rev: Any,
                   chain_head: Any, code: str) -> None:
    if not isinstance(reviewer, str) or not _LABEL.match(reviewer):
        raise ReviewTokenError(code, "reviewer must be a short label (letters, digits, . _ @ : -)")
    if not isinstance(contract_id, str) or not _CID.match(contract_id):
        raise ReviewTokenError(code, "contract_id is not a contract id")
    if not isinstance(contract_sha256, str) or not _HEX64.match(contract_sha256):
        raise ReviewTokenError(code, "contract_sha256 must be 64 lowercase hex")
    if not isinstance(test_rev, str) or not _REV.match(test_rev):
        raise ReviewTokenError(code, "test_rev must be a full commit sha")
    if not isinstance(chain_head, str) or not _HEX64.match(chain_head):
        raise ReviewTokenError(code, "chain_head must be 64 lowercase hex (from `tdd report`)")


def _encode_signed(private_key, claims: Mapping[str, Any]) -> str:
    body = _b64e(_canonical(claims))
    signature = private_key.sign(f"{PREFIX}.{body}".encode("ascii"))
    return f"{PREFIX}.{body}.{_b64e(signature)}"


def mint(private_key, *, reviewer: str, contract_id: str, contract_sha256: str, test_rev: str,
         chain_head: str, ttl_s: int = DEFAULT_TTL_S, now: int | None = None) -> str:
    _check_binding(reviewer, contract_id, contract_sha256, test_rev, chain_head, "token_invalid_request")
    if not isinstance(ttl_s, int) or isinstance(ttl_s, bool) or not 0 < ttl_s <= MAX_TTL_S:
        raise ReviewTokenError("token_invalid_request", f"ttl must be 1..{MAX_TTL_S} seconds")
    issued = int(time.time() if now is None else now)
    claims = {
        "typ": TOKEN_TYPE,
        "kid": key_id(private_key.public_key()),
        "reviewer": reviewer,
        "contract_id": contract_id,
        "contract_sha256": contract_sha256,
        "test_rev": test_rev,
        "chain_head": chain_head,
        "verdict": "approve",
        "iat": issued,
        "exp": issued + ttl_s,
        "nonce": secrets.token_hex(16),
    }
    return _encode_signed(private_key, claims)


def _split(token: Any) -> tuple[str, dict, bytes]:
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
        raise ReviewTokenError("token_malformed", "token is empty or too long")
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise ReviewTokenError("token_malformed", "token is not bbrt1.<claims>.<signature>")
    try:
        claims = json.loads(_b64d(parts[1]).decode("ascii"))
        signature = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ReviewTokenError("token_malformed", "token parts are not valid base64url JSON") from exc
    if not isinstance(claims, dict):
        raise ReviewTokenError("token_malformed", "token claims are not an object")
    return parts[1], claims, signature


def decode_unverified(token: str) -> dict:
    """Claims without any check. For display only; never for decisions."""
    return _split(token)[1]


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.strip().encode("ascii", "replace")).hexdigest()


def verify(token: str, public_key, *, contract_id: str, contract_sha256: str, test_rev: str,
           chain_head: str, now: float | None = None) -> dict:
    """Return the verified claims, or raise ReviewTokenError. Never partial."""
    _, _, _, InvalidSignature = _crypto()
    body, claims, signature = _split(token)
    try:
        public_key.verify(signature, f"{PREFIX}.{body}".encode("ascii"))
    except InvalidSignature as exc:
        raise ReviewTokenError("token_bad_signature", "token is not signed by the configured review key") from exc
    if set(claims) != set(_CLAIMS) or claims.get("typ") != TOKEN_TYPE or claims.get("verdict") != "approve":
        raise ReviewTokenError("token_invalid_claims", "token claims are not bundlebees.review-token/v1")
    if claims.get("kid") != key_id(public_key):
        raise ReviewTokenError("token_invalid_claims", "token key id does not match the configured key")
    _check_binding(claims["reviewer"], claims["contract_id"], claims["contract_sha256"],
                   claims["test_rev"], claims["chain_head"], "token_invalid_claims")
    iat, exp = claims.get("iat"), claims.get("exp")
    if (not all(isinstance(v, int) and not isinstance(v, bool) for v in (iat, exp))
            or not 0 < exp - iat <= MAX_TTL_S or not _NONCE.match(str(claims.get("nonce")))):
        raise ReviewTokenError("token_invalid_claims", "token lifetime or nonce is invalid")
    current = time.time() if now is None else now
    if current + CLOCK_SKEW_S < iat:
        raise ReviewTokenError("token_not_yet_valid", "token is not valid yet (check the clock)")
    if current - CLOCK_SKEW_S > exp:
        raise ReviewTokenError("token_expired", "token has expired")
    expected = {"contract_id": contract_id, "contract_sha256": contract_sha256,
                "test_rev": test_rev, "chain_head": chain_head}
    for name, value in expected.items():
        if claims[name] != value:
            raise ReviewTokenError("token_binding_mismatch", f"token {name} does not match this review")
    return dict(claims)


def consume(state_dir: os.PathLike[str] | str, claims: Mapping[str, Any], token: str) -> Path:
    """Record the nonce as used (O_EXCL). A second use is ``token_replayed``."""
    nonce = str(claims.get("nonce") or "")
    if not _NONCE.match(nonce):
        raise ReviewTokenError("token_invalid_claims", "token nonce is invalid")
    store = Path(state_dir) / "review-tokens"
    try:
        store.mkdir(mode=0o700, parents=True, exist_ok=True)
        if store.is_symlink() or not store.is_dir():
            raise ReviewTokenError("token_store_unsafe", "review-token store is not a private directory")
        os.chmod(store, 0o700)
    except OSError as exc:
        raise ReviewTokenError("token_store_unsafe", f"could not prepare review-token store: {exc}") from exc
    path = store / f"{nonce}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ReviewTokenError("token_replayed", "this review token was already used") from exc
    except OSError as exc:
        raise ReviewTokenError("token_store_unsafe", f"could not record the token nonce: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"kid": claims.get("kid"), "contract_id": claims.get("contract_id"),
                   "chain_head": claims.get("chain_head"), "token_sha256": token_sha256(token),
                   "used_at": int(time.time())}, handle, sort_keys=True)
    return path


def read_token_file(path: Any) -> str:
    p = Path(str(path or "")).expanduser()
    try:
        if p.stat().st_size > MAX_TOKEN_LEN + 16:
            raise ReviewTokenError("token_malformed", "token file is too large")
        return p.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewTokenError("token_malformed", "token file is not readable") from exc


# ----------------------------------------------------------------- CLI (runs on the human's machine)


def _contract_binding(path: Any) -> tuple[str, str, str]:
    from . import contract as acdd

    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewTokenError("token_invalid_request", "contract file is not readable JSON") from exc
    try:
        acdd.validate_contract(data)
        sha = acdd.check_frozen(data)
    except Exception as exc:  # noqa: BLE001 - any contract problem refuses the mint
        raise ReviewTokenError("token_invalid_request", f"contract is not a valid frozen contract: {exc}") from exc
    return str(data.get("id") or ""), str(sha), str(data.get("source_rev") or "")


def add_parsers(subparsers: Any) -> None:
    rt = subparsers.add_parser("review-token", help="Mint a signed human-review token (run on the reviewer's own machine).")
    sub = rt.add_subparsers(dest="review_token_command", required=True)
    mint_p = sub.add_parser("mint", help="Print a single-use token approving one contract at one chain head.")
    mint_p.add_argument("--private-key", required=True, help="Your Ed25519 PEM private key (owner-only file).")
    mint_p.add_argument("--contract", required=True, help="The frozen contract JSON you reviewed.")
    mint_p.add_argument("--chain-head", required=True, help="chain_head from `tdd report --json`.")
    mint_p.add_argument("--reviewer", required=True, help="Your reviewer label, e.g. kyle.")
    mint_p.add_argument("--ttl", type=int, default=DEFAULT_TTL_S, help=f"Lifetime in seconds (max {MAX_TTL_S}).")
    pub_p = sub.add_parser("pubkey", help="Print the public key line for review_public_key.")
    pub_p.add_argument("--private-key", required=True, help="Your Ed25519 PEM private key.")
    rt.set_defaults(command="review-token")


def run(args: Any) -> int:
    sub = getattr(args, "review_token_command", None)
    key = load_private_key(args.private_key)
    if sub == "pubkey":
        print(public_key_line(key.public_key()))
        return 0
    contract_id, sha, test_rev = _contract_binding(args.contract)
    print(mint(key, reviewer=str(args.reviewer), contract_id=contract_id, contract_sha256=sha,
               test_rev=test_rev, chain_head=str(args.chain_head).strip().lower(), ttl_s=int(args.ttl)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bundlebees_plugin.review_token")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parsers(subparsers)
    args = parser.parse_args(["review-token", *(sys.argv[1:] if argv is None else argv)])
    try:
        return run(args)
    except ReviewTokenError as exc:
        print(f"error: {exc.code}: {exc.message}\nfix: {exc.fix}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
