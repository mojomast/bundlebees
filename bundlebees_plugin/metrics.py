"""Append-only, tamper-evident per-run metrics for bundlebees.

Threat model: the metrics log is written from the coordinator process and may
live in a directory other local processes can reach. A run's history must not
be silently rewritten, reordered or truncated in the middle. Each event is one
JSON object on its own line with a monotonic ``seq`` and ``prev_sha256`` holding
the SHA-256 of the previous raw line. Readers recompute the chain, so editing or
deleting an earlier line is detected. The log is opened with ``O_NOFOLLOW`` and
created ``0600`` inside an ``0700`` directory, and cooperating writers hold an
exclusive ``flock`` while reading the last line and appending.

Limits: this is an unkeyed hash chain, not a signature. A process that can
replace the whole file can forge a fresh chain, and deleting the newest line is
not detectable from the file alone. It also is not a substitute for keeping the
state directory private. All free text is passed through
``bundlebees_plugin.security.scrub`` before it is written, so a token, URL,
address or private key in a note never lands on disk verbatim.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping

from .security import scrub

try:  # pragma: no cover - the fallback is for platforms without fcntl
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

EVENTS = ("dispatch", "verify", "integrate", "intervention", "recovery")

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_ZERO_SHA = "0" * 64
_ALLOWED_FIELDS = frozenset(
    {
        "attempt_id",
        "wave_id",
        "ok",
        "code",
        "first_pass",
        "tokens",
        "cost_usd",
        "attempts",
        "regressions",
        "note",
        "retry_of",
        "sha",
        "duration_s",
    }
)
_VERBATIM_FIELDS = frozenset({"attempt_id", "wave_id", "retry_of", "sha"})
_DEFAULT_ERRORS = {
    "invalid_run_id": (
        "run id is not a safe file name",
        "Use 1-64 characters from A-Z a-z 0-9 . _ - starting with a letter or digit.",
    ),
    "invalid_event": (
        "event is not a known metrics event",
        "Use one of: " + ", ".join(EVENTS) + ".",
    ),
    "invalid_field": (
        "metrics field is not allowed",
        "Use only the documented metrics fields and JSON-safe values.",
    ),
    "metrics_tampered": (
        "the metrics hash chain does not verify",
        "Treat this run's history as untrusted; do not repair or rewrite the log.",
    ),
    "metrics_io": (
        "the metrics log could not be read or written",
        "Check that the state directory is writable and not a symlink, then retry.",
    ),
}
_DEFAULT_FIX = "Check the metrics state directory and retry."


class MetricsError(Exception):
    """A metrics failure with a stable ``code``, scrubbed ``error`` and ``fix``."""

    def __init__(
        self,
        code: str,
        error: str | None = None,
        fix: str | None = None,
    ) -> None:
        default_error, default_fix = _DEFAULT_ERRORS.get(
            code, (f"metrics error: {code}", _DEFAULT_FIX)
        )
        self.code = str(code)
        self.error = str(scrub(str(error if error is not None else default_error)))
        self.fix = str(scrub(str(fix if fix is not None else default_fix)))
        super().__init__(self.error)


# ---------------------------------------------------------------------------
# Validation and scrubbing helpers
# ---------------------------------------------------------------------------


def _check_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise MetricsError("invalid_run_id")
    return run_id


def _check_event(event: Any) -> str:
    if not isinstance(event, str) or event not in EVENTS:
        raise MetricsError("invalid_event")
    return event


def _clean_value(key: str, value: Any) -> Any:
    if key in _VERBATIM_FIELDS and isinstance(value, str):
        pattern = _SHA_RE if key == "sha" else _ID_RE
        if pattern.fullmatch(value):
            return value
    return scrub(value)


def _clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or key not in _ALLOWED_FIELDS:
            raise MetricsError(
                "invalid_field",
                f"metrics field is not allowed: {key!r}",
            )
        clean[key] = _clean_value(key, value)
    return clean


def _utc_now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# File plumbing
# ---------------------------------------------------------------------------


def _metrics_dir(state_dir: Path) -> Path:
    directory = Path(state_dir) / "metrics"
    try:
        if directory.is_symlink():
            raise MetricsError("metrics_io", "the metrics directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise MetricsError("metrics_io", "the metrics path is not a directory")
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        return directory
    except MetricsError:
        raise
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics directory could not be created: {exc}",
        ) from exc


def _run_path(state_dir: Path, run_id: Any, *, create: bool) -> tuple[Path, str]:
    checked = _check_run_id(run_id)
    directory = _metrics_dir(state_dir) if create else Path(state_dir) / "metrics"
    return directory / f"{checked}.jsonl", checked


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_NOFOLLOW", 0)


def _lock(fd: int) -> bool:
    if fcntl is None:
        return False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return True
        except InterruptedError:
            continue
        except OSError:
            return False


def _unlock(fd: int, locked: bool) -> None:
    if fcntl is None or not locked:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_last_line(path: Path) -> bytes | None:
    """Return the final raw line (no trailing newline) or ``None`` when empty."""
    flags = _open_flags(os.O_RDONLY)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return None
        position = size
        data = b""
        while position > 0:
            step = min(65536, position)
            position -= step
            os.lseek(fd, position, os.SEEK_SET)
            data = os.read(fd, step) + data
            complete = data[:-1] if data.endswith(b"\n") else data
            if b"\n" in complete:
                break
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        return lines[-1] if lines else None
    finally:
        os.close(fd)


def _previous_state(path: Path) -> tuple[int, str]:
    last = _read_last_line(path)
    if last is None:
        return 1, _ZERO_SHA
    try:
        record = json.loads(last.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MetricsError("metrics_tampered") from None
    if not isinstance(record, dict):
        raise MetricsError("metrics_tampered")
    seq = record.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise MetricsError("metrics_tampered")
    return seq + 1, hashlib.sha256(last).hexdigest()


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to the metrics log")
        view = view[written:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_event(
    state_dir: Path,
    run_id: str,
    event: str,
    *,
    now: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Append one scrubbed, chained event and return the stored record.

    The file is never rewritten or truncated. The lock is held while the last
    line is read to pick the next sequence number and while the new line is
    appended.
    """
    checked_event = _check_event(event)
    clean_fields = _clean_fields(fields)
    path, checked_run = _run_path(state_dir, run_id, create=True)
    at = str(now) if now is not None else _utc_now()

    flags = _open_flags(os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics log could not be opened: {exc}",
        ) from exc

    locked = False
    try:
        locked = _lock(fd)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
        except OSError:
            pass
        seq, previous_sha = _previous_state(path)
        record: dict[str, Any] = {
            "seq": seq,
            "at": at,
            "event": checked_event,
            "run_id": checked_run,
        }
        record.update(clean_fields)
        record["prev_sha256"] = previous_sha
        try:
            line = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise MetricsError(
                "invalid_field",
                f"metrics field value is not JSON-serializable: {exc}",
            ) from exc
        _write_all(fd, line + b"\n")
        try:
            os.fsync(fd)
        except OSError:
            pass
        return record
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics log could not be written: {exc}",
        ) from exc
    finally:
        _unlock(fd, locked)
        os.close(fd)


def read_events(state_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Read and verify a run's log. A missing file is an empty history."""
    path, _checked_run = _run_path(state_dir, run_id, create=False)
    flags = _open_flags(os.O_RDONLY)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics log could not be read: {exc}",
        ) from exc

    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics log could not be read: {exc}",
        ) from exc

    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    events: list[dict[str, Any]] = []
    expected_sha = _ZERO_SHA
    for index, raw in enumerate(lines, start=1):
        if not raw:
            raise MetricsError("metrics_tampered", f"blank metrics line at {index}")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MetricsError("metrics_tampered", f"invalid metrics line at {index}") from None
        if not isinstance(record, dict):
            raise MetricsError("metrics_tampered", f"invalid metrics line at {index}")
        if record.get("seq") != index:
            raise MetricsError("metrics_tampered", f"sequence gap at metrics line {index}")
        if record.get("prev_sha256") != expected_sha:
            raise MetricsError("metrics_tampered", f"hash chain break at metrics line {index}")
        events.append(record)
        expected_sha = hashlib.sha256(raw).hexdigest()
    return events


def list_runs(state_dir: Path) -> list[str]:
    """Return the run ids with a metrics log, sorted."""
    directory = Path(state_dir) / "metrics"
    try:
        entries = list(os.scandir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        raise MetricsError(
            "metrics_io",
            f"the metrics directory could not be listed: {exc}",
        ) from exc

    runs: list[str] = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        name = entry.name
        if not name.endswith(".jsonl"):
            continue
        run_id = name[: -len(".jsonl")]
        if RUN_ID_RE.fullmatch(run_id):
            runs.append(run_id)
    return sorted(runs)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _ordered(events: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    items = list(events)
    if all(isinstance(item.get("seq"), int) for item in items):
        return sorted(items, key=lambda item: item["seq"])
    return items


def _timestamp(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _tidy(value: float) -> int | float:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _plan_to_accepted(
    events: list[Mapping[str, Any]], accepted_ids: set[str]
) -> dict[str, float | None]:
    dispatched: dict[str, _dt.datetime] = {}
    accepted_at: dict[str, _dt.datetime] = {}
    for event in events:
        attempt_id = event.get("attempt_id")
        if not isinstance(attempt_id, str):
            continue
        at = _timestamp(event.get("at"))
        if at is None:
            continue
        if event.get("event") == "dispatch" and attempt_id not in dispatched:
            dispatched[attempt_id] = at
        elif event.get("event") == "integrate" and bool(event.get("ok")):
            if attempt_id in accepted_ids and attempt_id not in accepted_at:
                accepted_at[attempt_id] = at

    durations: list[float] = []
    for attempt_id in accepted_ids:
        started = dispatched.get(attempt_id)
        finished = accepted_at.get(attempt_id)
        if started is None or finished is None:
            continue
        seconds = (finished - started).total_seconds()
        if seconds >= 0:
            durations.append(float(seconds))
    if not durations:
        return {"median": None, "max": None}
    return {"median": float(statistics.median(durations)), "max": float(max(durations))}


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a run's verified events into outcome metrics."""
    ordered = _ordered(events)
    run_id = next((item.get("run_id") for item in ordered), None)
    if not isinstance(run_id, str):
        run_id = None

    dispatched = 0
    verified_attempts: set[str] = set()
    first_verify: dict[str, bool] = {}
    accepted_ids: set[str] = set()
    tokens_total = 0
    cost_total = 0.0
    tokens_known = False
    cost_known = False
    interventions = 0
    regressions = 0
    accepted_waves: set[str] = set()
    rejected_waves: set[str] = set()
    verify_ok: dict[str, list[bool]] = {}
    recoveries: list[str] = []

    for index, event in enumerate(ordered):
        name = event.get("event")
        attempt_id = event.get("attempt_id")
        attempt_key = attempt_id if isinstance(attempt_id, str) else None
        tokens = _number(event.get("tokens"))
        cost = _number(event.get("cost_usd"))
        if tokens is not None:
            tokens_total += tokens
            tokens_known = True
        if cost is not None:
            cost_total += cost
            cost_known = True

        if name == "dispatch":
            dispatched += 1
        elif name == "verify":
            if attempt_key is not None:
                verified_attempts.add(attempt_key)
                first_verify.setdefault(attempt_key, bool(event.get("ok")))
                verify_ok.setdefault(attempt_key, []).append(bool(event.get("ok")))
        elif name == "integrate":
            if bool(event.get("ok")):
                if attempt_key is not None:
                    accepted_ids.add(attempt_key)
                wave = event.get("wave_id")
                accepted_waves.add(str(wave) if isinstance(wave, str) else f"#{index}")
            else:
                wave = event.get("wave_id")
                rejected_waves.add(str(wave) if isinstance(wave, str) else f"#{index}")
            count = _number(event.get("regressions"))
            if count is not None:
                regressions += count
        elif name == "intervention":
            interventions += 1
        elif name == "recovery":
            retry_of = event.get("retry_of")
            if isinstance(retry_of, str):
                recoveries.append(retry_of)

    failed_recoveries = 0
    for attempt_id in recoveries:
        results = verify_ok.get(attempt_id)
        if not results:
            failed_recoveries += 1
        elif not all(results):
            failed_recoveries += 1

    attempted = len(first_verify)
    first_pass = sum(1 for ok in first_verify.values() if ok)
    accepted = len(accepted_ids)
    tokens_per_accepted = _rate(tokens_total, accepted) if tokens_known else None
    cost_per_accepted = _rate(cost_total, accepted) if cost_known else None

    return {
        "run_id": run_id,
        "attempts_dispatched": dispatched,
        "attempts_verified": len(verified_attempts),
        "first_pass_rate": _rate(first_pass, attempted),
        "accepted_changes": accepted,
        "tokens_total": _tidy(tokens_total),
        "tokens_per_accepted": tokens_per_accepted,
        "cost_usd_total": cost_total,
        "cost_per_accepted": cost_per_accepted,
        "human_interventions": interventions,
        "plan_to_accepted_s": _plan_to_accepted(ordered, accepted_ids),
        "regressions_found": _tidy(regressions),
        "failed_worker_recoveries": failed_recoveries,
        "waves_accepted": len(accepted_waves),
        "waves_rejected": len(rejected_waves),
    }


def format_summary(summary: Mapping[str, Any]) -> str:
    """Render a compact one-metric-per-line summary; ``None`` becomes ``n/a``."""

    def render(value: Any) -> str:
        return "n/a" if value is None else str(value)

    plan = summary.get("plan_to_accepted_s")
    plan_map = plan if isinstance(plan, Mapping) else {}
    lines = [
        f"run_id: {render(summary.get('run_id'))}",
        f"attempts_dispatched: {render(summary.get('attempts_dispatched'))}",
        f"attempts_verified: {render(summary.get('attempts_verified'))}",
        f"first_pass_rate: {render(summary.get('first_pass_rate'))}",
        f"accepted_changes: {render(summary.get('accepted_changes'))}",
        f"tokens_total: {render(summary.get('tokens_total'))}",
        f"tokens_per_accepted: {render(summary.get('tokens_per_accepted'))}",
        f"cost_usd_total: {render(summary.get('cost_usd_total'))}",
        f"cost_per_accepted: {render(summary.get('cost_per_accepted'))}",
        f"human_interventions: {render(summary.get('human_interventions'))}",
        "plan_to_accepted_s: median="
        f"{render(plan_map.get('median'))} max={render(plan_map.get('max'))}",
        f"regressions_found: {render(summary.get('regressions_found'))}",
        f"failed_worker_recoveries: {render(summary.get('failed_worker_recoveries'))}",
        f"waves_accepted: {render(summary.get('waves_accepted'))}",
        f"waves_rejected: {render(summary.get('waves_rejected'))}",
    ]
    return "\n".join(lines)
