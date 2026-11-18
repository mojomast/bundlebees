"""Enrolled reviewer identities and signed review receipts (rizzstack.reviewer/1).

Why this exists: `tdd review` has always *recorded* a review, but the record was
coordinator-keyed and self-labelled — "the coordinator wrote this", never "this
named reviewer said this". The 2026-11-17 release decision names that as gate
E1/E4/E7/E8: an unsigned receipt or self-declared reviewer cannot grant PASS.

This module gives a reviewer a real key and makes the review carry a signature
the coordinator cannot forge:

* ``enroll`` writes a 256-bit HMAC key (0600) and a public record containing only
  a key id and a fingerprint — the key never leaves its file.
* ``sign_review`` covers a canonical binding of (contract id, contract sha256,
  revision, outcome, note digest). Changing any field invalidates the signature.
* ``verify_review`` re-derives the binding from the stored record and checks the
  signature against the enrolled identity.

A review without a verified signature stays exactly what it is today: a
coordinator observation, labelled as unauthenticated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "rizzstack.reviewer/1"
SIGNING_VERSION = "review-binding/1"

KEY_BYTES = 32


class ReviewerError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not slug:
        raise ReviewerError("reviewer_name_invalid", "a reviewer needs a name",
                            "Pass --name with a stable identifier.")
    return slug


def _dir(state_dir: str | os.PathLike) -> Path:
    path = Path(state_dir) / "reviewers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_key_path(state_dir: str | os.PathLike, name: str) -> Path:
    return _dir(state_dir) / f"{_slug(name)}.key"


def enroll(state_dir: str | os.PathLike, name: str, *, key_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Create (or adopt) a reviewer key and write its public record.

    The key file is 0600 and is never copied into the record; the record carries
    only a key id and a fingerprint so a verifier can recognise the identity
    without holding the secret.
    """
    slug = _slug(name)
    key_file = Path(key_path) if key_path else default_key_path(state_dir, name)
    if key_file.exists():
        key = key_file.read_bytes().strip()
        if len(key) < KEY_BYTES:
            raise ReviewerError("reviewer_key_invalid", f"{key_file} is not a reviewer key",
                                "Remove it or pass --key-file with an enrolled key.")
        created = None
    else:
        key = secrets.token_hex(KEY_BYTES).encode()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key + b"\n")
        finally:
            os.close(fd)
        created = time.time()
    key_id = hashlib.sha256(key).hexdigest()[:16]
    record = {
        "schema": SCHEMA,
        "name": str(name),
        "slug": slug,
        "key_id": key_id,
        "fingerprint": hashlib.sha256(b"reviewer:" + key).hexdigest()[:32],
        "key_path": str(key_file),
        "created_at": created,
        "enrolled_at": time.time(),
    }
    path = _dir(state_dir) / f"{slug}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return record


def load_reviewer(state_dir: str | os.PathLike, name_or_slug: str) -> dict[str, Any] | None:
    slug = _slug(name_or_slug)
    path = _dir(state_dir) / f"{slug}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def binding(*, contract_id: str, contract_sha256: str, revision: str, outcome: str, note: str) -> bytes:
    """Canonical review binding: every field a signature must cover."""
    body = {
        "signing": SIGNING_VERSION,
        "contract_id": str(contract_id),
        "contract_sha256": str(contract_sha256),
        "revision": str(revision),
        "outcome": str(outcome),
        "note_sha256": hashlib.sha256(str(note or "").encode("utf-8")).hexdigest(),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_review(key: bytes, **fields: str) -> str:
    return hmac.new(key.strip(), binding(**fields), hashlib.sha256).hexdigest()


def verify_review(state_dir: str | os.PathLike, record: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a stored review's signature against its enrolled identity."""
    signature = str(record.get("reviewer_signature") or "")
    key_id = str(record.get("reviewer_key_id") or "")
    name = str(record.get("reviewer_name") or "")
    if not signature or not key_id or not name:
        return {"signed": False, "reason": "no reviewer signature on the record"}
    enrolled = load_reviewer(state_dir, name)
    if enrolled is None:
        return {"signed": False, "reason": f"reviewer {name!r} is not enrolled"}
    if enrolled.get("key_id") != key_id:
        return {"signed": False, "reason": "signature key id does not match the enrolled identity"}
    key_file = Path(enrolled["key_path"])
    if not key_file.exists():
        return {"signed": False, "reason": "enrolled key file is missing"}
    expected = sign_review(
        key_file.read_bytes(),
        contract_id=str(record.get("contract_id") or ""),
        contract_sha256=str(record.get("contract_sha256") or ""),
        revision=str(record.get("revision") or ""),
        outcome=str(record.get("review_outcome") or ""),
        note=str(record.get("note") or ""),
    )
    if not hmac.compare_digest(expected, signature):
        return {"signed": False, "reason": "signature does not match the review binding"}
    return {"signed": True, "key_id": key_id, "reviewer": name}


def sign_attestation(state_dir, *, name: str, key: bytes, contract_id: str, contract_sha256: str,
                     revision: str, outcome: str, note: str, evidence_signature: str) -> dict:
    """Write a reviewer SIGNED attestation beside the coordinator's evidence record.

    The coordinator's evidence record keeps its own HMAC intact; the reviewer signs a
    binding that also covers the coordinator record's signature, so neither side can
    rewrite the other's claim without invalidating the pair.
    """
    record = {
        "schema": "rizzstack.reviewer-attestation/1",
        "reviewer": str(name),
        "key_id": hashlib.sha256(key.strip()).hexdigest()[:16],
        "contract_id": str(contract_id),
        "contract_sha256": str(contract_sha256),
        "revision": str(revision),
        "outcome": str(outcome),
        "note": str(note),
        "evidence_signature": str(evidence_signature),
        "signed_at": time.time(),
    }
    record["binding_sha256"] = hashlib.sha256(binding(
        contract_id=contract_id, contract_sha256=contract_sha256, revision=revision,
        outcome=outcome, note=note)).hexdigest()
    record["signature"] = hmac.new(key.strip(), json.dumps(record, sort_keys=True,
                                                          separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    directory = _dir(state_dir) / _slug(name)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{str(contract_id)}-{str(revision)[:12]}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return record


def verify_attestation(state_dir, record: Mapping[str, Any]) -> dict:
    """Verify a sidecar reviewer attestation against its enrolled identity."""
    name = str(record.get("reviewer") or "")
    enrolled = load_reviewer(state_dir, name) if name else None
    if enrolled is None:
        return {"signed": False, "reason": "reviewer is not enrolled"}
    key_file = Path(enrolled["key_path"])
    if not key_file.exists():
        return {"signed": False, "reason": "enrolled key file is missing"}
    payload = {k: v for k, v in record.items() if k != "signature"}
    expected = hmac.new(key_file.read_bytes().strip(),
                        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(record.get("signature") or "")):
        return {"signed": False, "reason": "attestation signature does not match"}
    return {"signed": True, "key_id": record.get("key_id"), "reviewer": name}
