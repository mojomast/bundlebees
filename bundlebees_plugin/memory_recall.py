"""Lease-time recall from the ``rizzstack.memory/1`` vault (default off).

The coordinator asks the configured vault query program for a small, capped
memory context before it signs a lease.  The returned mapping is attached to
the lease *record*, never to the brief: ``brief_sha256`` and the existing MAC
input therefore stay untouched.  With ``MEMORY_ENABLED`` not exactly ``1`` this
module touches no subprocess and no file.

The query engine is not imported: it runs as the operator-configured program in
``BUNDLEBEES_MEMORY_QUERY`` with a minimal, exact-name child environment.  A
missing command, refusal, timeout, invalid JSON or wrong contract is a logged,
scrubbed miss; recall never raises into dispatch and never invents memory.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .security import child_env, scrub

__all__ = [
    "MEMORY_CONTRACT",
    "FLAG_ENV",
    "QUERY_ENV",
    "CAPS",
    "MISS_SCHEMA",
    "recall",
    "enabled",
    "miss_path",
]

#: Contract name and version for the vault query result.
MEMORY_CONTRACT = "rizzstack.memory/1"
#: The feature is dark unless this is exactly ``1``.
FLAG_ENV = "MEMORY_ENABLED"
#: Program (optionally with arguments) that answers a vault query.
QUERY_ENV = "BUNDLEBEES_MEMORY_QUERY"

MAX_ITEMS = 8
MAX_EXCERPT_CHARS = 480
MAX_TOTAL_CHARS = 4000
CAPS = {
    "max_items": MAX_ITEMS,
    "max_excerpt_chars": MAX_EXCERPT_CHARS,
    "max_total_chars": MAX_TOTAL_CHARS,
}

#: Bounded wait for the query program.  A hung engine is a miss, not a stalled
#: dispatch.
QUERY_TIMEOUT_S = 5.0

MISS_SCHEMA = "rizzstack.memory-recall-miss/1"
MISS_DIRNAME = "memory"
MISS_FILENAME = "misses.jsonl"

_ITEM_FIELDS = ("id", "type", "sha", "path", "title", "excerpt")
_QUERY_FIELDS = ("objective", "title", "summary", "description")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class _QueryError(Exception):
    """Internal, fixed-code query failure; the text is never logged."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# Flag, paths and request shape
# ---------------------------------------------------------------------------


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the operator set ``MEMORY_ENABLED`` to exactly ``1``."""

    return str(_env(environ).get(FLAG_ENV, "")).strip() == "1"


def miss_path(state_dir: Any) -> Path:
    return Path(state_dir) / MISS_DIRNAME / MISS_FILENAME


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _timestamp(now: Any) -> str:
    if isinstance(now, str) and now:
        return now
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    return _utc_now()


def _query_text(brief: Mapping[str, Any]) -> str:
    if not isinstance(brief, Mapping):
        return ""
    for name in _QUERY_FIELDS:
        value = brief.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Miss log (append-only, no payload, no host)
# ---------------------------------------------------------------------------


def _record_miss(state_dir: Any, code: str, now: Any) -> None:
    """Append one fixed-shape miss record.  Any failure here stays silent."""

    try:
        directory = Path(state_dir) / MISS_DIRNAME
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        record = {
            "schema": MISS_SCHEMA,
            "at": _timestamp(now),
            "contract": MEMORY_CONTRACT,
            "code": str(code),
        }
        payload = (
            json.dumps(scrub(record), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        fd = os.open(
            directory / MISS_FILENAME,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW,
            0o600,
        )
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        # A miss must never stop a lease from being written.
        return


# ---------------------------------------------------------------------------
# Query subprocess
# ---------------------------------------------------------------------------


def _run_query(command: str, query: str) -> str:
    try:
        argv = shlex.split(str(command), posix=True)
    except ValueError:
        raise _QueryError("memory_query_unconfigured") from None
    if not argv:
        raise _QueryError("memory_query_unconfigured")

    request = {
        "contract": MEMORY_CONTRACT,
        "query": query,
        "caps": dict(CAPS),
    }
    payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    try:
        completed = subprocess.run(
            [*argv, query],
            input=payload,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_S,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise _QueryError("memory_query_timeout") from None
    except (OSError, subprocess.SubprocessError):
        raise _QueryError("memory_query_failed") from None
    except UnicodeError:
        raise _QueryError("memory_query_invalid_output") from None
    if completed.returncode != 0:
        raise _QueryError("memory_query_failed")
    return completed.stdout


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


def _clean_item(value: Any) -> dict[str, str] | None:
    """Reduce one vault item to the six contract fields, or drop it."""

    if not isinstance(value, Mapping):
        return None
    item: dict[str, str] = {}
    for field in _ITEM_FIELDS:
        text = value.get(field)
        if not isinstance(text, str):
            return None
        item[field] = text
    if len(item["excerpt"]) > MAX_EXCERPT_CHARS:
        item["excerpt"] = item["excerpt"][:MAX_EXCERPT_CHARS]
    return item


def _cap_items(items: list[Any]) -> list[dict[str, str]]:
    """Apply item, excerpt and total caps, dropping rather than inventing."""

    capped: list[dict[str, str]] = []
    total = 0
    for value in items:
        if len(capped) >= MAX_ITEMS:
            break
        item = _clean_item(value)
        if item is None:
            continue
        cost = sum(len(item[field]) for field in _ITEM_FIELDS)
        if total + cost > MAX_TOTAL_CHARS:
            break
        capped.append(item)
        total += cost
    return capped


def _parse_result(stdout: str) -> dict[str, Any]:
    try:
        result = json.loads(stdout)
    except (TypeError, ValueError):
        raise _QueryError("memory_query_invalid_json") from None
    if not isinstance(result, Mapping):
        raise _QueryError("memory_query_invalid_json")
    if result.get("contract") != MEMORY_CONTRACT:
        raise _QueryError("memory_query_wrong_contract")
    items = result.get("items")
    if not isinstance(items, list):
        raise _QueryError("memory_query_invalid_result")
    vault_rev = result.get("vault_rev")
    if vault_rev is not None and not isinstance(vault_rev, str):
        raise _QueryError("memory_query_invalid_result")
    return {
        "contract": MEMORY_CONTRACT,
        "caps": dict(CAPS),
        "vault_rev": vault_rev,
        "items": _cap_items(items),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def recall(
    state_dir: Any,
    brief: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    now: Any = None,
) -> dict[str, Any] | None:
    """Return a capped ``rizzstack.memory/1`` context, or ``None``.

    Disabled means no subprocess and no file.  Enabled but unavailable means
    one scrubbed miss record and ``None``; it never raises and never fabricates
    an item.
    """

    if not enabled(environ):
        return None

    command = str(_env(environ).get(QUERY_ENV, "") or "").strip()
    if not command:
        _record_miss(state_dir, "memory_query_unconfigured", now)
        return None

    try:
        stdout = _run_query(command, _query_text(brief))
        return _parse_result(stdout)
    except _QueryError as exc:
        _record_miss(state_dir, exc.code, now)
        return None
    except Exception:
        # Recall is never allowed to break dispatch, and unknown internals are
        # summarized by a fixed code rather than echoing any text.
        _record_miss(state_dir, "memory_query_failed", now)
        return None
