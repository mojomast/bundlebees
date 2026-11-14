"""Create-only receipts for cacheable local test results.

This module is evidence reuse, not a scheduler.  A caller hashes everything
that can change a local test outcome into ``key_inputs``, looks the key up, and
only if it is absent runs the tests and records the result.  A hit returns the
stored receipt with ``reused=True``; nothing is executed here.

The key covers exactly the inputs the caller supplies: the source, patch and
test-source digests, the command, dependencies, stack and environment.  The
key is a SHA-256 over canonical JSON, and the receipt file stores only that
digest plus the result.  Raw commands, environments and paths are never
persisted.  Receipts live under ``<state_dir>/receipts/<key>.json`` and are
written to a private temporary file, fsynced, and linked into place with
create-only semantics, so an existing receipt is never overwritten.

Live or external checks are not reproducible evidence and must never be
cached: callers pass ``cacheable=False`` and :func:`record_receipt` refuses
before creating any directory or file.  A receipt whose JSON, schema, key or
integrity digest does not verify is refused rather than repaired.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .security import scrub

__all__ = [
    "ReceiptError",
    "REQUIRED_KEY_INPUTS",
    "canonical_key",
    "receipt_key",
    "receipt_path",
    "record_receipt",
    "lookup_receipt",
]

_FORMAT_VERSION = 1
_KEY_DOMAIN = b"bundlebees-test-receipt:key:v1\x00"
_RECEIPT_DOMAIN = b"bundlebees-test-receipt:receipt:v1\x00"
_REQUIRED_KEY_INPUTS = (
    "source_tree_sha256",
    "patch_sha256",
    "test_sources_sha256",
    "command",
    "dependencies",
    "stack",
    "environment",
)
REQUIRED_KEY_INPUTS = _REQUIRED_KEY_INPUTS
_DIGEST_FIELDS = _REQUIRED_KEY_INPUTS[:3]
_RECEIPT_FIELDS = frozenset(
    {"version", "key", "result", "duration_s", "cacheable", "receipt_sha256"}
)
_HEX_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

_DEFAULT_ERRORS = {
    "receipt_invalid": (
        "test receipt input is invalid",
        "Pass JSON-safe key inputs with all required fields and a non-negative "
        "finite duration.",
    ),
    "receipt_not_cacheable": (
        "live or external checks cannot be cached",
        "Record only reproducible local test results; run live checks every time "
        "and do not call record_receipt for them.",
    ),
    "receipt_corrupt": (
        "the stored test receipt is corrupt or was modified",
        "Treat the receipt as untrusted; remove it only if you intend to rerun "
        "the tests, never repair or overwrite it in place.",
    ),
    "receipt_io": (
        "the test receipt store could not be read or written",
        "Check that the state directory is writable and that receipts/ is a "
        "real private directory, then retry.",
    ),
}


class ReceiptError(Exception):
    """A receipt failure with a stable ``code``, scrubbed ``error`` and ``fix``."""

    def __init__(
        self,
        code: str,
        error: str | None = None,
        fix: str | None = None,
    ) -> None:
        default_error, default_fix = _DEFAULT_ERRORS.get(
            code, (f"test receipt error: {code}", "Check the receipt store and retry.")
        )
        self.code = str(code)
        self.error = str(scrub(str(error if error is not None else default_error)))
        self.fix = str(scrub(str(fix if fix is not None else default_fix)))
        super().__init__(f"{self.code}: {self.error}")


# ---------------------------------------------------------------------------
# Canonical data
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReceiptError(
            "receipt_invalid", "test receipt data is not canonical JSON"
        ) from exc


def _normalize_key_inputs(key_inputs: Any) -> dict[str, Any]:
    if not isinstance(key_inputs, Mapping):
        raise ReceiptError("receipt_invalid", "key_inputs must be a mapping")
    if not all(isinstance(name, str) for name in key_inputs):
        raise ReceiptError("receipt_invalid", "key_inputs keys must be strings")
    missing = [name for name in _REQUIRED_KEY_INPUTS if name not in key_inputs]
    if missing:
        raise ReceiptError(
            "receipt_invalid",
            "key_inputs is missing required fields: " + ", ".join(missing),
        )
    try:
        normalized = json.loads(_canonical(dict(key_inputs)).decode("utf-8"))
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise ReceiptError(
            "receipt_invalid", "test receipt data is not valid UTF-8"
        ) from exc
    if not isinstance(normalized, dict):  # pragma: no cover - defensive
        raise ReceiptError("receipt_invalid", "key_inputs must be an object")
    for name in _DIGEST_FIELDS:
        value = normalized.get(name)
        if not isinstance(value, str) or not value:
            raise ReceiptError(
                "receipt_invalid", f"key_inputs {name} must be a non-empty string"
            )
        if _HEX_DIGEST_RE.fullmatch(value):
            normalized[name] = value.lower()
    return normalized


def canonical_key(key_inputs: Any) -> str:
    """Return the canonical SHA-256 key for exactly these test inputs."""
    normalized = _normalize_key_inputs(key_inputs)
    return hashlib.sha256(_KEY_DOMAIN + _canonical(normalized)).hexdigest()


receipt_key = canonical_key


def receipt_path(state_dir: Any, key_inputs: Any) -> Path:
    """Return the receipt path for ``key_inputs`` without touching the disk."""
    return Path(state_dir) / "receipts" / f"{canonical_key(key_inputs)}.json"


# ---------------------------------------------------------------------------
# Receipt bodies
# ---------------------------------------------------------------------------


def _receipt_digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(_RECEIPT_DOMAIN + _canonical(dict(body))).hexdigest()


def _check_duration(duration_s: Any) -> float:
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        raise ReceiptError("receipt_invalid", "duration_s must be a number")
    value = float(duration_s)
    if not math.isfinite(value) or value < 0:
        raise ReceiptError(
            "receipt_invalid", "duration_s must be finite and non-negative"
        )
    return value


def _check_cacheable(cacheable: Any) -> None:
    if not isinstance(cacheable, bool):
        raise ReceiptError("receipt_invalid", "cacheable must be a boolean")
    if not cacheable:
        raise ReceiptError("receipt_not_cacheable")


def _make_receipt(key: str, result: Any, duration_s: float) -> dict[str, Any]:
    body = {
        "version": _FORMAT_VERSION,
        "key": key,
        "result": scrub(result),
        "duration_s": duration_s,
        "cacheable": True,
    }
    body["receipt_sha256"] = _receipt_digest(body)
    return body


# ---------------------------------------------------------------------------
# File plumbing
# ---------------------------------------------------------------------------


def _receipts_dir(state_dir: Any, *, create: bool) -> Path:
    try:
        state = Path(state_dir)
    except TypeError as exc:
        raise ReceiptError("receipt_invalid", "state_dir is not a path") from exc

    if state.is_symlink():
        raise ReceiptError("receipt_io", "the state directory must not be a symlink")
    directory = state / "receipts"
    if directory.is_symlink():
        raise ReceiptError("receipt_io", "receipts/ must not be a symlink")
    if not create:
        return directory

    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise ReceiptError("receipt_io", "receipts/ must be a real directory")
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        return directory
    except ReceiptError:
        raise
    except OSError as exc:
        raise ReceiptError(
            "receipt_io", f"the receipt directory could not be created: {exc}"
        ) from exc


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to the test receipt")
        view = view[written:]


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | _O_DIRECTORY | _NOFOLLOW
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_only(path: Path, payload: bytes) -> bool:
    """Atomically create ``path`` with ``payload``; return False if it exists."""
    directory = path.parent
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".receipt-", suffix=".tmp", dir=str(directory)
        )
    except OSError as exc:
        raise ReceiptError(
            "receipt_io", f"the receipt temporary file could not be created: {exc}"
        ) from exc

    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            raise
        _fsync_directory(directory)
        return True
    except ReceiptError:
        raise
    except OSError as exc:
        raise ReceiptError(
            "receipt_io", f"the test receipt could not be written: {exc}"
        ) from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _load_receipt(path: Path, expected_key: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | _NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ReceiptError("receipt_corrupt", "a receipt must not be a symlink") from exc
        raise ReceiptError(
            "receipt_io", f"the test receipt could not be opened: {exc}"
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ReceiptError("receipt_corrupt", "a receipt must be a regular file")
        if info.st_size > _MAX_RECEIPT_BYTES:
            raise ReceiptError("receipt_corrupt", "the receipt is unreasonably large")
        chunks: list[bytes] = []
        remaining = _MAX_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except ReceiptError:
        raise
    except OSError as exc:
        raise ReceiptError(
            "receipt_io", f"the test receipt could not be read: {exc}"
        ) from exc
    finally:
        os.close(fd)

    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReceiptError("receipt_corrupt", "the receipt is not valid JSON") from exc
    if not isinstance(receipt, dict):
        raise ReceiptError("receipt_corrupt", "the receipt must be a JSON object")
    if set(receipt) != _RECEIPT_FIELDS:
        raise ReceiptError("receipt_corrupt", "the receipt has unexpected fields")
    if receipt.get("version") != _FORMAT_VERSION:
        raise ReceiptError("receipt_corrupt", "the receipt version is not supported")
    if receipt.get("key") != expected_key:
        raise ReceiptError("receipt_corrupt", "the receipt key does not match")
    if receipt.get("cacheable") is not True:
        raise ReceiptError("receipt_corrupt", "a stored receipt must be cacheable")
    if "result" not in receipt:
        raise ReceiptError("receipt_corrupt", "the receipt result is missing")
    duration = receipt.get("duration_s")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or float(duration) < 0
    ):
        raise ReceiptError("receipt_corrupt", "the receipt duration is invalid")
    certificate = receipt.get("receipt_sha256")
    if not isinstance(certificate, str):
        raise ReceiptError("receipt_corrupt", "the receipt integrity digest is missing")
    body = {name: value for name, value in receipt.items() if name != "receipt_sha256"}
    if not hmac.compare_digest(_receipt_digest(body), certificate):
        raise ReceiptError("receipt_corrupt", "the receipt integrity digest does not match")

    verified = dict(receipt)
    verified["reused"] = True
    return verified


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def lookup_receipt(
    state_dir: Any,
    key_inputs: Any,
    *,
    cacheable: bool = True,
) -> dict[str, Any] | None:
    """Return the exact-key receipt with ``reused=True``, or ``None`` on a miss.

    A non-cacheable lookup always misses without touching the receipt store.
    """
    if not isinstance(cacheable, bool):
        raise ReceiptError("receipt_invalid", "cacheable must be a boolean")
    if not cacheable:
        return None
    key = canonical_key(key_inputs)
    path = _receipts_dir(state_dir, create=False) / f"{key}.json"
    return _load_receipt(path, key)


def record_receipt(
    state_dir: Any,
    key_inputs: Any,
    result: Any,
    duration_s: Any,
    *,
    cacheable: bool = True,
) -> dict[str, Any]:
    """Record a cacheable local test result, or return an existing exact match.

    The first call for a key creates the receipt with ``reused=False``.  Later
    calls with the same key return the stored receipt unchanged with
    ``reused=True`` and do not overwrite it.  Any changed input yields a new
    key and therefore a miss/new receipt.
    """
    _check_cacheable(cacheable)
    key = canonical_key(key_inputs)
    duration = _check_duration(duration_s)
    body = _make_receipt(key, result, duration)
    payload = _canonical(body) + b"\n"

    directory = _receipts_dir(state_dir, create=True)
    path = directory / f"{key}.json"
    for _attempt in range(3):
        if _create_only(path, payload):
            receipt = dict(body)
            receipt["reused"] = False
            return receipt
        existing = _load_receipt(path, key)
        if existing is not None:
            return existing
    raise ReceiptError(
        "receipt_io", "the receipt store changed while the receipt was being written"
    )
