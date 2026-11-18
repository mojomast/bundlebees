"""Enrolled owner authority for the feedback board (release gates E2/E5).

The release decision's gap: "First feedback write revalidates authority and expiry at the
writer boundary... Operator enrolls owner authority."

An owner authority is a locally-held key plus a public record that names what the owner may
do (``board``, ``feedback``) and until when. A writer validates a request by checking the
signature, the scope and the expiry — at the writer boundary, on every write, never once at
startup. Nothing here can mint authority on its own: enrollment is an explicit, audited act.

``expires never`` maps to the same far-future stamp the standing grants use.
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
from typing import Any, Iterable, Mapping

SCHEMA = "rizzstack.owner-authority/1"
NEVER = 253402300799.0  # 9999-12-31T23:59:59Z, matching the standing-grant convention
KEY_BYTES = 32
SCOPES = ("board", "feedback", "rail_clear", "approve")


class OwnerAuthorityError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    if not slug:
        raise OwnerAuthorityError("owner_name_invalid", "an owner authority needs a name",
                                  "Pass --name with a stable identifier.")
    return slug


def _dir(state_dir: str | os.PathLike) -> Path:
    path = Path(state_dir) / "owners"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_expiry(value: str | None) -> float:
    text = str(value or "never").strip().lower()
    if text in ("never", "none", "no-expiry", "no_expiry"):
        return NEVER
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OwnerAuthorityError("owner_expiry_invalid", f"expiry {value!r} is not ISO-8601",
                                  "Use YYYY-MM-DD or never.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    stamp = parsed.timestamp()
    if stamp <= time.time():
        raise OwnerAuthorityError("owner_expired", "the expiry is in the past",
                                  "Set a future date or never.")
    return stamp


def enroll(state_dir: str | os.PathLike, name: str, *, scope: Iterable[str],
           expires: str | None = None, key_path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Create (or adopt) an owner key and write its public authority record."""
    slug = _slug(name)
    wanted = sorted({str(item).strip() for item in scope or [] if str(item).strip()})
    if not wanted:
        raise OwnerAuthorityError("owner_scope_empty", "an owner authority needs at least one scope",
                                  f"Choose from: {', '.join(SCOPES)}.")
    unknown = [item for item in wanted if item not in SCOPES]
    if unknown:
        raise OwnerAuthorityError("owner_scope_unknown",
                                  f"unknown scope(s): {', '.join(unknown)}",
                                  f"Choose from: {', '.join(SCOPES)}.")
    expires_at = _parse_expiry(expires)
    key_file = Path(key_path) if key_path else _dir(state_dir) / f"{slug}.key"
    if key_file.exists():
        key = key_file.read_bytes().strip()
        created = None
    else:
        key = secrets.token_hex(KEY_BYTES).encode()
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key + b"\n")
        finally:
            os.close(fd)
        created = time.time()
    record = {
        "schema": SCHEMA,
        "name": str(name),
        "slug": slug,
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "fingerprint": hashlib.sha256(b"owner:" + key).hexdigest()[:32],
        "key_path": str(key_file),
        "scope": wanted,
        "expires_at": expires_at,
        "created_at": created,
        "enrolled_at": time.time(),
    }
    path = _dir(state_dir) / f"{slug}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return record


def load_owner(state_dir: str | os.PathLike, name: str) -> dict[str, Any] | None:
    path = _dir(state_dir) / f"{_slug(name)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sign(state_dir: str | os.PathLike, *, name: str, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Sign an owner action (e.g. a board write) with the enrolled key."""
    record = load_owner(state_dir, name)
    if record is None:
        raise OwnerAuthorityError("owner_not_enrolled", f"owner {name!r} is not enrolled",
                                  "Run `hermes bundlebees owner enroll` first.")
    body = {
        "schema": "rizzstack.owner-action/1",
        "owner": record["name"],
        "key_id": record["key_id"],
        "scope": record["scope"],
        "expires_at": record["expires_at"],
        "action": str(action),
        "payload_sha256": hashlib.sha256(json.dumps(dict(payload), sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest(),
        "at": time.time(),
    }
    key = Path(record["key_path"]).read_bytes()
    body["signature"] = hmac.new(key.strip(), json.dumps(body, sort_keys=True,
                                                         separators=(",", ":")).encode(),
                                 hashlib.sha256).hexdigest()
    return body


def verify(state_dir: str | os.PathLike, action: Mapping[str, Any], *, scope: str,
           now: float | None = None) -> dict[str, Any]:
    """Writer-boundary check: signature, scope and expiry, every time."""
    record = load_owner(state_dir, str(action.get("owner") or ""))
    if record is None:
        return {"valid": False, "reason": "owner is not enrolled"}
    if scope not in (record.get("scope") or []):
        return {"valid": False, "reason": f"owner has no {scope!r} scope"}
    expires_at = float(record.get("expires_at") or 0)
    if (now if now is not None else time.time()) >= expires_at:
        return {"valid": False, "reason": "owner authority has expired"}
    key = Path(record["key_path"]).read_bytes()
    body = {k: v for k, v in action.items() if k != "signature"}
    expected = hmac.new(key.strip(), json.dumps(body, sort_keys=True,
                                                separators=(",", ":")).encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(action.get("signature") or "")):
        return {"valid": False, "reason": "signature does not match the action"}
    return {"valid": True, "owner": record["name"], "key_id": record["key_id"], "scope": scope}
