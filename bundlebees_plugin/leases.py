"""Immutable coordinator-written file-lease records.

Threat model
============

A worker's result is untrusted: it can claim any ``assigned_files`` set, echo a
fake lease object, or edit files outside the brief. File-lease authority
therefore comes *only* from a record the coordinator writes at dispatch time,
under ``<state_dir>/leases/<attempt_id>.json``. The record is protected four
ways:

* The directory is private (0700), the record is created exclusively
  (``O_CREAT|O_EXCL|O_NOFOLLOW``, mode 0600) and then made read-only (0400), so
  a second dispatch can never overwrite an earlier lease.
* Every field is covered by an HMAC-SHA256 keyed by ``<state_dir>/lease.key``,
  a 32-byte random secret created once with 0600 permissions. Editing the JSON
  (including just the brief) invalidates the MAC. Symlinks, non-regular files,
  and group/world-readable or foreign-owned keys are refused.
* The brief digest is recomputed from the stored brief, so even a MAC-key
  compromise that rewrites one field is caught by the second check.
* Worker-returned metadata is never consulted for scope. Any worker claim that
  differs from the verified record is reported by
  :func:`ignored_worker_claims` for a warning only; it can never widen the
  lease.

Error text is passed through :func:`bundlebees_plugin.security.scrub`. The key
itself is never stored in a record, returned, logged, or embedded in an error.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .security import scrub

__all__ = [
    "LeaseError",
    "record_lease",
    "load_lease",
    "lease_files",
    "ignored_worker_claims",
]

_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_REV_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_KEY_BYTES = 32
_KEY_NAME = "lease.key"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_GROUP_WORLD_BITS = stat.S_IRWXG | stat.S_IRWXO

_FIX_MISSING = (
    "Dispatch through bundlebees_dispatch or record the brief with "
    "`hermes bundlebees lease <attempt_id> --brief <file>`."
)
_FIX_TAMPERED = (
    "Treat this attempt as untrusted and re-dispatch the brief through "
    "bundlebees_dispatch."
)
_FIX_KEY = (
    "Do not edit the lease key. Remove an unsafe key file only by re-dispatching "
    "in a fresh state directory; existing leases will no longer verify."
)
_FIX_BRIEF = (
    "Pass a brief that already carries source_rev (40 or 64 hex), a non-empty "
    "assigned_files list, and acceptance."
)


class LeaseError(Exception):
    """A lease could not be recorded or verified.

    ``code`` is one of ``lease_missing``, ``lease_tampered``, ``lease_exists``
    or ``lease_invalid``. ``error`` and ``fix`` are scrubbed before exposure.
    """

    def __init__(self, code: str, error: str, fix: str) -> None:
        self.code = code
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        super().__init__(f"{self.code}: {self.error}")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _validate_attempt_id(attempt_id: Any) -> str:
    text = attempt_id if isinstance(attempt_id, str) else ""
    if not text or ".." in text or not _ATTEMPT_ID_RE.match(text):
        raise LeaseError(
            "lease_invalid",
            "attempt id is not a safe lease file name",
            "Use the attempt id exactly as returned by bundlebees_dispatch.",
        )
    return text


def _canonical(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise LeaseError(
            "lease_invalid",
            "value is not JSON-serializable",
            _FIX_BRIEF,
        ) from exc
    return text.encode("utf-8")


def _sign(body: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, _canonical(dict(body)), hashlib.sha256).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _acceptance_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return bool(value)
    return bool(value)


def _normalize_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(brief, Mapping):
        raise LeaseError("lease_invalid", "brief must be a mapping", _FIX_BRIEF)
    try:
        normalized = json.loads(_canonical(dict(brief)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError(
            "lease_invalid", "brief is not valid JSON data", _FIX_BRIEF
        ) from exc
    if not isinstance(normalized, dict):
        raise LeaseError("lease_invalid", "brief must be an object", _FIX_BRIEF)

    source_rev = normalized.get("source_rev")
    if not isinstance(source_rev, str) or not _SOURCE_REV_RE.match(source_rev):
        raise LeaseError(
            "lease_invalid",
            "brief source_rev must be a 40 or 64 character hex commit id",
            _FIX_BRIEF,
        )

    files = normalized.get("assigned_files")
    if (
        not isinstance(files, list)
        or not files
        or not all(isinstance(item, str) and item.strip() for item in files)
    ):
        raise LeaseError(
            "lease_invalid",
            "brief assigned_files must be a non-empty list of strings",
            _FIX_BRIEF,
        )

    if "acceptance" not in normalized or not _acceptance_present(
        normalized.get("acceptance")
    ):
        raise LeaseError(
            "lease_invalid",
            "brief acceptance is required",
            _FIX_BRIEF,
        )
    return normalized


# ---------------------------------------------------------------------------
# Directory and key handling
# ---------------------------------------------------------------------------


def _ensure_leases_dir(state_dir: Path) -> Path:
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        leases = state_dir / "leases"
        leases.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(leases, 0o700)
    except OSError as exc:
        raise LeaseError(
            "lease_invalid",
            "the lease directory could not be created safely",
            "Use a writable plugin state directory and retry the dispatch.",
        ) from exc
    return leases


def _read_existing_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        raise
    except OSError as exc:
        code = "lease_invalid"
        if exc.errno == errno.ELOOP:
            message = "the lease key is a symlink"
        else:
            message = "the lease key could not be opened safely"
        raise LeaseError(code, message, _FIX_KEY) from exc

    keep_fd = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LeaseError(
                "lease_invalid", "the lease key is not a regular file", _FIX_KEY
            )
        if stat.S_IMODE(info.st_mode) & _GROUP_WORLD_BITS:
            raise LeaseError(
                "lease_invalid",
                "the lease key is group- or world-accessible",
                "Make the existing lease key mode 0600, then retry.",
            )
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise LeaseError(
                "lease_invalid", "the lease key is owned by another user", _FIX_KEY
            )
        with os.fdopen(fd, "rb") as handle:
            keep_fd = False
            data = handle.read(_KEY_BYTES + 1)
    finally:
        if keep_fd:
            os.close(fd)

    if len(data) != _KEY_BYTES:
        raise LeaseError(
            "lease_invalid", "the lease key is not 32 bytes", _FIX_KEY
        )
    return data


def _ensure_key(state_dir: Path) -> bytes:
    """Return the lease key, creating it atomically on first use.

    The key is written in full to a private temporary file and then published
    with ``os.link``, which fails if the key already exists. Concurrent first
    dispatches therefore either publish the key or read a complete one; none
    can observe a half-written key file.
    """
    path = state_dir / _KEY_NAME
    try:
        return _read_existing_key(path)
    except FileNotFoundError:
        pass

    tmp = state_dir / f".{_KEY_NAME}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise LeaseError(
            "lease_invalid",
            "the lease key could not be created safely",
            _FIX_KEY,
        ) from exc

    try:
        keep_fd = True
        try:
            key = os.urandom(_KEY_BYTES)
            with os.fdopen(fd, "wb") as handle:
                keep_fd = False
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
        finally:
            if keep_fd:
                os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_existing_key(path)
        except OSError as exc:
            raise LeaseError(
                "lease_invalid",
                "the lease key could not be created safely",
                _FIX_KEY,
            ) from exc
        return key
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _load_key(state_dir: Path) -> bytes:
    try:
        return _read_existing_key(state_dir / _KEY_NAME)
    except FileNotFoundError as exc:
        raise LeaseError(
            "lease_tampered",
            "the lease key is missing",
            _FIX_TAMPERED,
        ) from exc


# ---------------------------------------------------------------------------
# Record I/O
# ---------------------------------------------------------------------------


def _read_record_bytes(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError as exc:
        raise LeaseError("lease_missing", "no lease record exists", _FIX_MISSING) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            message = "the lease record is a symlink"
        else:
            message = "the lease record could not be opened safely"
        raise LeaseError("lease_tampered", message, _FIX_TAMPERED) from exc

    keep_fd = True
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise LeaseError(
                "lease_tampered",
                "the lease record is not a regular file",
                _FIX_TAMPERED,
            )
        with os.fdopen(fd, "rb") as handle:
            keep_fd = False
            return handle.read()
    finally:
        if keep_fd:
            os.close(fd)


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    payload = _canonical(record) + b"\n"
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
            0o600,
        )
    except FileExistsError as exc:
        raise LeaseError(
            "lease_exists",
            "a lease record already exists for this attempt id",
            "Use a new attempt id; recorded leases are immutable.",
        ) from exc
    except OSError as exc:
        raise LeaseError(
            "lease_invalid",
            "the lease record could not be created safely",
            _FIX_TAMPERED,
        ) from exc

    keep_fd = True
    try:
        with os.fdopen(fd, "wb") as handle:
            keep_fd = False
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o400)
    finally:
        if keep_fd:
            os.close(fd)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_lease(
    state_dir: Path,
    attempt_id: str,
    brief: Mapping[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Write the immutable lease record for ``attempt_id`` and return it.

    The brief must already carry ``source_rev`` (40 or 64 hex), a non-empty
    ``assigned_files`` list, and ``acceptance``. An existing record is never
    overwritten: it raises :class:`LeaseError` with ``code="lease_exists"``.
    """
    attempt = _validate_attempt_id(attempt_id)
    normalized = _normalize_brief(brief)
    state = Path(state_dir)
    leases = _ensure_leases_dir(state)
    key = _ensure_key(state)

    acceptance = normalized.get("acceptance")
    acceptance_tests = (
        acceptance.get("tests") if isinstance(acceptance, Mapping) else None
    )
    record: dict[str, Any] = {
        "version": 1,
        "attempt_id": attempt,
        "created_at": now if now is not None else _utc_now(),
        "source_rev": normalized["source_rev"],
        "branch": normalized.get("branch") or None,
        "assigned_files": list(normalized["assigned_files"]),
        "acceptance": acceptance,
        "tests": normalized.get("tests") or acceptance_tests or [],
        "run_id": normalized.get("run_id") or "default",
        "wave": normalized.get("wave") or None,
        "retry_of": normalized.get("retry_of") or None,
        "brief_sha256": hashlib.sha256(_canonical(normalized)).hexdigest(),
        "brief": normalized,
    }
    record["mac"] = _sign(record, key)
    _write_record(leases / f"{attempt}.json", record)
    return record


def load_lease(state_dir: Path, attempt_id: str) -> dict[str, Any]:
    """Load and verify the lease record for ``attempt_id``.

    Missing records raise ``lease_missing``; symlinks, non-regular files, bad
    JSON, an attempt-id mismatch, a missing key, a MAC mismatch, or a brief
    digest mismatch raise ``lease_tampered``.
    """
    attempt = _validate_attempt_id(attempt_id)
    path = Path(state_dir) / "leases" / f"{attempt}.json"
    raw = _read_record_bytes(path)

    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LeaseError(
            "lease_tampered", "the lease record is not valid JSON", _FIX_TAMPERED
        ) from exc
    if not isinstance(record, dict):
        raise LeaseError(
            "lease_tampered", "the lease record is not an object", _FIX_TAMPERED
        )
    if record.get("attempt_id") != attempt:
        raise LeaseError(
            "lease_tampered", "the lease record names a different attempt", _FIX_TAMPERED
        )

    key = _load_key(Path(state_dir))
    stored_mac = record.get("mac")
    if not isinstance(stored_mac, str) or not stored_mac:
        raise LeaseError(
            "lease_tampered", "the lease record has no MAC", _FIX_TAMPERED
        )
    body = {name: value for name, value in record.items() if name != "mac"}
    expected_mac = _sign(body, key)
    if not hmac.compare_digest(stored_mac, expected_mac):
        raise LeaseError(
            "lease_tampered", "the lease record MAC does not match", _FIX_TAMPERED
        )

    if "brief" not in record:
        raise LeaseError(
            "lease_tampered", "the lease record has no brief", _FIX_TAMPERED
        )
    stored_digest = record.get("brief_sha256")
    if not isinstance(stored_digest, str):
        raise LeaseError(
            "lease_tampered", "the lease record has no brief digest", _FIX_TAMPERED
        )
    recomputed = hashlib.sha256(_canonical(record["brief"])).hexdigest()
    if not hmac.compare_digest(stored_digest, recomputed):
        raise LeaseError(
            "lease_tampered",
            "the lease record brief digest does not match",
            _FIX_TAMPERED,
        )
    return record


def lease_files(record: Mapping[str, Any]) -> list[str]:
    """Return the ``assigned_files`` of a verified lease record."""
    files = record.get("assigned_files") if isinstance(record, Mapping) else None
    if (
        not isinstance(files, (list, tuple))
        or not files
        or not all(isinstance(item, str) and item for item in files)
    ):
        raise LeaseError(
            "lease_invalid",
            "the lease record has no usable assigned_files list",
            "Use the verified record returned by load_lease.",
        )
    return list(files)


# ---------------------------------------------------------------------------
# Worker claims are evidence only
# ---------------------------------------------------------------------------


def _file_list(value: Any) -> list[str] | None:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return None
    if not items or not all(isinstance(item, str) and item for item in items):
        return None
    return items


def _same_scope(value: Any, files: list[str]) -> bool:
    candidate = _file_list(value)
    if candidate is None:
        return False
    return sorted(candidate) == sorted(files)


def _lease_matches(value: Any, files: list[str], record: Mapping[str, Any]) -> bool:
    if isinstance(value, Mapping):
        if "assigned_files" in value:
            return _same_scope(value.get("assigned_files"), files)
        return value == record
    return _same_scope(value, files)


def ignored_worker_claims(
    record: Mapping[str, Any], result: Mapping[str, Any]
) -> list[str]:
    """Name worker-returned keys that tried to declare a different scope.

    The caller may show these as a warning. They are never used to widen the
    lease returned by :func:`lease_files`.
    """
    files = lease_files(record)
    if not isinstance(result, Mapping):
        return []

    claims: list[str] = []
    if "assigned_files" in result and not _same_scope(
        result.get("assigned_files"), files
    ):
        claims.append("assigned_files")

    brief = result.get("brief")
    if (
        isinstance(brief, Mapping)
        and "assigned_files" in brief
        and not _same_scope(brief.get("assigned_files"), files)
    ):
        claims.append("brief.assigned_files")

    observed = result.get("observed")
    if (
        isinstance(observed, Mapping)
        and "assigned_files" in observed
        and not _same_scope(observed.get("assigned_files"), files)
    ):
        claims.append("observed.assigned_files")

    if "lease" in result and not _lease_matches(result.get("lease"), files, record):
        claims.append("lease")

    return claims


_EVIDENCE_KEY_LABEL = b"bundlebees/tdd-evidence/1"


def evidence_key(state_dir: Path | str) -> bytes:
    """32-byte HMAC key for signing ``tdd-evidence/1`` records.

    Derived from the coordinator's private lease key with a fixed label, so
    evidence and leases share one ``0600`` secret but never the same key.
    Workers never see either: both live only in the coordinator state dir.
    """
    path = Path(state_dir)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise LeaseError(
            "lease_invalid",
            "the plugin state directory could not be created safely",
            "Use a writable plugin state directory and retry.",
        ) from exc
    return hmac.new(_ensure_key(path), _EVIDENCE_KEY_LABEL, hashlib.sha256).digest()
