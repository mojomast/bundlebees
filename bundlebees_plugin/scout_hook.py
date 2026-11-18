"""Interim scout -> factory-driver hook.

The 2-minute rizz-scout already observes the launcher's attempt list.  When it
sees an attempt reach a terminal state, this module lets it trigger *one*
ordinary factory-driver pass immediately instead of waiting for the 30-minute
heartbeat.

The hook is intentionally small and composable:

* :func:`run_hook` takes the attempts the scout observed and an injected
  ``run_pass`` callable.  It never builds a driver itself, so the CLI and tests
  can reuse exactly the same detection/watermark/lock contract.
* A durable watermark under the plugin state directory records the terminal
  attempt ids already handled.  Re-running over unchanged state is a no-op.
* A non-blocking ``flock`` serialises overlapping scout invocations.  The
  loser reports ``coalesced`` and does not run a second pass.
* A pass exception leaves the watermark unadvanced and is surfaced as a
  :class:`ScoutHookError`, so the next run retries instead of silently losing
  the completion.

This module does not read or edit the live scout script or any cron entry.
Wiring it into the live 2-minute scout is an owner action documented in
``docs/scout-hook.md``.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:  # POSIX advisory locks; Linux is the target platform.
    import fcntl
except ImportError:  # pragma: no cover - no lock protocol on this platform
    fcntl = None  # type: ignore[assignment]

from .beat_reconciliation import SUCCESS_STATES
from .security import scrub

__all__ = [
    "COMPLETED_STATES",
    "EXIT_ERROR",
    "EXIT_OK",
    "SCHEMA",
    "WATERMARK_SCHEMA",
    "ScoutHookError",
    "completed_attempt_ids",
    "load_attempts_file",
    "lock_path",
    "run_hook",
    "run_scout_hook",
    "watermark_path",
]

SCHEMA = "bundlebees.scout-hook/1"
WATERMARK_SCHEMA = "bundlebees.scout-hook-watermark/1"

EXIT_OK = 0
EXIT_ERROR = 2

#: A "completed" attempt is terminal, not necessarily successful.  Failures
#: still need the driver's reconciliation/landing path, and waiting for the
#: heartbeat to notice them is the same latency bug.  Success names come from
#: the reconciliation sweep so the two views cannot drift.
_COMPLETION_FAILURES = frozenset(
    {
        "failed",
        "failure",
        "cancelled",
        "canceled",
        "error",
        "errored",
        "timed_out",
        "timeout",
        "rejected",
        "aborted",
    }
)
COMPLETED_STATES = frozenset(SUCCESS_STATES) | _COMPLETION_FAILURES

DEFAULT_LOCK_WAIT_S = 0.0
_LOCK_POLL_S = 0.05
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_DEFAULT_ERRORS = {
    "factory_pass_failed": (
        "the factory-driver pass failed",
        "Inspect the driver output and retry; the attempt watermark was not advanced.",
    ),
    "lock_io": (
        "the scout-hook lock could not be used",
        "Check that the plugin state directory is writable and retry.",
    ),
    "unsupported_platform": (
        "this platform has no advisory file locks",
        "Run the scout hook on a POSIX host with flock support.",
    ),
    "watermark_corrupt": (
        "the scout-hook watermark is unreadable or malformed",
        "Inspect factory/scout-hook-watermark.json; do not silently delete a completed attempt's marker.",
    ),
    "watermark_io": (
        "the scout-hook watermark could not be written",
        "Check that the plugin state directory is writable and retry.",
    ),
    "attempts_unreadable": (
        "the scout attempt list could not be read",
        "Check the attempts file path and permissions, then retry.",
    ),
    "attempts_invalid": (
        "the scout attempt list is not valid JSON attempts data",
        "Pass a JSON array of attempts or an object with an 'attempts' list.",
    ),
}


class ScoutHookError(Exception):
    """A hook failure with a stable ``code``, scrubbed ``error`` and ``fix``."""

    def __init__(
        self,
        code: str,
        error: str | None = None,
        fix: str | None = None,
    ) -> None:
        default_error, default_fix = _DEFAULT_ERRORS.get(
            code, (f"scout hook error: {code}", "Check the hook state and retry.")
        )
        self.code = str(code)
        self.error = str(scrub(str(error if error is not None else default_error)))
        self.fix = str(scrub(str(fix if fix is not None else default_fix)))
        super().__init__(f"{self.code}: {self.error}")


# ---------------------------------------------------------------------------
# Durable paths and watermark
# ---------------------------------------------------------------------------


def watermark_path(state_dir: Any) -> Path:
    return Path(state_dir) / "factory" / "scout-hook-watermark.json"


def lock_path(state_dir: Any) -> Path:
    return Path(state_dir) / "factory" / "scout-hook.lock"


def _iso(now: Any = None) -> str:
    if now is None:
        epoch = time.time()
    elif callable(now):
        epoch = now()
    else:
        epoch = now
    if isinstance(epoch, str) and epoch.strip():
        return epoch.strip()
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        value = time.time()
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _seen_from_body(body: Any) -> set[str]:
    if not isinstance(body, Mapping):
        raise ScoutHookError("watermark_corrupt", "the watermark body is not an object")
    raw = body.get("seen", body.get("completed_attempts"))
    if isinstance(raw, Mapping):
        raw = list(raw)
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ScoutHookError(
            "watermark_corrupt", "the watermark 'seen' value is not a list"
        )
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ScoutHookError(
                "watermark_corrupt", "the watermark contains a non-string attempt id"
            )
        value = item.strip()
        if value:
            seen.add(value)
    return seen


def _read_watermark(state_dir: Any) -> set[str]:
    path = watermark_path(state_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise ScoutHookError(
            "watermark_corrupt", f"the watermark could not be read: {exc}"
        ) from exc
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ScoutHookError(
            "watermark_corrupt", f"the watermark is not valid JSON: {exc}"
        ) from exc
    return _seen_from_body(body)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _write_watermark(state_dir: Any, seen: set[str], now: Any) -> Path:
    path = watermark_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = {
            "schema": WATERMARK_SCHEMA,
            "updated_at": _iso(now),
            "seen": sorted(seen),
        }
        payload = (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(
            prefix=".scout-hook-watermark-", dir=str(path.parent)
        )
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        try:
            directory = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
        return path
    except OSError as exc:
        try:
            if "temporary" in locals():
                os.unlink(temporary)
        except OSError:
            pass
        raise ScoutHookError(
            "watermark_io", f"the watermark could not be written: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


def _acquire_lock(state_dir: Any, wait_s: float) -> int | None:
    if fcntl is None:
        raise ScoutHookError("unsupported_platform")
    path = lock_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    except OSError as exc:
        raise ScoutHookError("lock_io", f"the lock file could not be opened: {exc}") from exc

    deadline = time.monotonic() + max(0.0, float(wait_s or 0.0))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                os.close(fd)
                raise ScoutHookError(
                    "lock_io", f"the lock could not be taken: {exc}"
                ) from exc
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(_LOCK_POLL_S)


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Attempt classification
# ---------------------------------------------------------------------------


def _attempt_value(entry: Any, *names: str) -> Any:
    if isinstance(entry, Mapping):
        for name in names:
            if name in entry:
                return entry.get(name)
        return None
    for name in names:
        if hasattr(entry, name):
            return getattr(entry, name)
    return None


def _iter_attempts(attempts: Any) -> Iterable[Any]:
    if isinstance(attempts, Mapping):
        for key in ("attempts", "items"):
            if key in attempts:
                attempts = attempts.get(key)
                break
        else:
            raise ScoutHookError(
                "attempts_invalid", "the attempts object has no 'attempts' list"
            )
    if isinstance(attempts, (str, bytes)) or attempts is None:
        raise ScoutHookError(
            "attempts_invalid", "attempts must be a sequence of attempt records"
        )
    try:
        return list(attempts)
    except TypeError as exc:
        raise ScoutHookError(
            "attempts_invalid", "attempts must be a sequence of attempt records"
        ) from exc


def completed_attempt_ids(attempts: Any) -> list[str]:
    """Terminal attempt ids in input order, de-duplicated.

    ``state`` (launcher) and ``status`` (some adapters) are both accepted.
    Running, queued and unknown states are ignored; a malformed record without
    an id is skipped rather than guessed at.
    """

    found: list[str] = []
    seen: set[str] = set()
    for entry in _iter_attempts(attempts):
        attempt_id = _attempt_value(entry, "attempt_id", "id")
        if not isinstance(attempt_id, str):
            continue
        name = attempt_id.strip()
        if not name or name in seen:
            continue
        state = _attempt_value(entry, "state", "status")
        if not isinstance(state, str):
            continue
        if state.strip().lower() not in COMPLETED_STATES:
            continue
        seen.add(name)
        found.append(name)
    return found


def load_attempts_file(path: Any) -> Any:
    """Read an attempts document from ``path`` (``-`` means stdin)."""

    try:
        if str(path) == "-":
            import sys

            text = sys.stdin.read()
        else:
            text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise ScoutHookError(
            "attempts_unreadable", f"the attempts file could not be read: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ScoutHookError(
            "attempts_invalid", f"the attempts file is not valid JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def _result(
    status: str,
    *,
    state_dir: Any,
    completed: list[str] | None = None,
    new: list[str] | None = None,
    pass_result: Any = None,
) -> dict[str, Any]:
    passes = 1 if status == "pass" else 0
    return {
        "schema": SCHEMA,
        "status": status,
        "passes": passes,
        "completed_attempts": list(completed or []),
        "new_attempts": list(new or []),
        "watermark": str(watermark_path(state_dir)),
        "pass": pass_result,
    }


def run_hook(
    state_dir: Any,
    attempts: Any,
    *,
    run_pass: Callable[[], Any],
    now: Any = None,
    lock_wait_s: float = DEFAULT_LOCK_WAIT_S,
) -> dict[str, Any]:
    """Run at most one factory pass for newly observed completed attempts.

    ``attempts`` may be the scout's list/document or a zero-argument callable
    that returns it.  A callable is evaluated while the single-flight lock is
    held, so the decision is made against the latest observation.

    Returns a JSON-safe result with ``status`` one of ``pass``, ``noop`` or
    ``coalesced``.  A real failure raises :class:`ScoutHookError`; in
    particular the watermark is not advanced when ``run_pass`` fails.
    """

    state = Path(state_dir)
    fd = _acquire_lock(state, lock_wait_s)
    if fd is None:
        return _result("coalesced", state_dir=state)

    try:
        seen = _read_watermark(state)
        observed = attempts() if callable(attempts) else attempts
        completed = completed_attempt_ids(observed)
        new = [attempt_id for attempt_id in completed if attempt_id not in seen]
        if not new:
            return _result("noop", state_dir=state, completed=completed)

        try:
            pass_result = run_pass()
        except ScoutHookError:
            raise
        except Exception as exc:  # noqa: BLE001 - the hook must fail closed
            raise ScoutHookError(
                "factory_pass_failed", f"{type(exc).__name__}: {exc}"
            ) from exc

        _write_watermark(state, seen | set(completed), now)
        return _result(
            "pass",
            state_dir=state,
            completed=completed,
            new=new,
            pass_result=pass_result,
        )
    finally:
        _release_lock(fd)


run_scout_hook = run_hook
