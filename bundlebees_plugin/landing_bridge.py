"""Bridge a hyvv landing outbox into the bundlebees landing outbox.

hyvv (the former PC launcher) writes canonical ``rizzstack.bee.landed/1``
CloudEvents into a directory.  This module consumes that directory:

1. every source event is validated with the pinned workflow schema/validator;
2. when coordinator lease/job records exist, the event attempt, ``source_rev``,
   lease digest and ``assigned_files`` are checked against them and a mismatch
   fails closed before anything is recorded;
3. the canonical event is recorded in the bundlebees outbox idempotently;
4. ``deliver_pending`` runs and the source file is acknowledged/moved only
   after both the outbox record and delivery are durable;
5. a crash or restart replays the same file without duplicating the event, and
   a source file whose bytes conflict with an existing event is refused.

Only the standard library is used at runtime.  No source path or coordinator
secret is written into an event, delivery record or error message.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import bee_events
from .job import JobError, load_job
from .leases import LeaseError, load_lease

__all__ = [
    "ACK_SCHEMA",
    "ACK_DIR",
    "BridgeError",
    "consume_once",
    "consume",
    "run_once",
    "process_pending",
    "scan_pending",
    "verify_event_identity",
    "lease_sha256",
    "run",
    "main",
]

ACK_SCHEMA = "rizzstack.bee-landing-ack/1"
ACK_DIR = "acked"
_ACK_META_DIR = ".acks"
_ACK_SUFFIX = ".ack.json"
_MAX_SOURCE_BYTES = bee_events.MAX_EVENT_BYTES * 8
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class BridgeError(Exception):
    """A source event could not be correlated, recorded or acknowledged."""

    def __init__(self, code: str, error: str, fix: str = "") -> None:
        self.code = str(code)
        self.error = bee_events.scrub(str(error))
        self.fix = bee_events.scrub(str(fix)) if fix else ""
        super().__init__(f"{self.code}: {self.error}")

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.error, "fix": self.fix}


# ---------------------------------------------------------------------------
# Small durable-file helpers
# ---------------------------------------------------------------------------


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_regular(path: Path, *, limit: int = _MAX_SOURCE_BYTES) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError(
            "source_invalid",
            f"cannot read source {path.name}: {type(exc).__name__}",
            "Use a regular non-symlink source file.",
        ) from exc
    try:
        info = os.fstat(fd)
        if (info.st_mode & 0o170000) != 0o100000:
            raise BridgeError(
                "source_invalid",
                f"source {path.name} is not a regular file",
                "Use a regular non-symlink source file.",
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise BridgeError(
                "source_oversized",
                f"source {path.name} exceeds the size limit",
                "Keep each landing event below the event size limit.",
            )
        return raw
    finally:
        os.close(fd)


def _decode_json(raw: bytes, name: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "source_invalid",
            f"source {name} is not UTF-8 JSON",
            "Write one canonical landing event per JSON file or JSON line.",
        ) from exc


def _decode_source_events(raw: bytes, name: str) -> list[dict[str, Any]]:
    """Accept one event, a list of events, a wrapper object or JSON lines."""

    if not raw.strip():
        raise BridgeError("source_invalid", f"source {name} is empty", "Write one event.")
    try:
        loaded = _decode_json(raw, name)
    except BridgeError:
        loaded = None
        parsed_lines: list[Any] = []
        for line in raw.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed_lines.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise BridgeError(
                    "source_invalid",
                    f"source {name} is not valid JSON or JSON lines",
                    "Write one canonical landing event per JSON file or JSON line.",
                ) from exc
        if not parsed_lines:
            raise BridgeError("source_invalid", f"source {name} is empty", "Write one event.")
        loaded = parsed_lines

    if isinstance(loaded, Mapping) and isinstance(loaded.get("event"), Mapping):
        loaded = [loaded["event"]]
    elif isinstance(loaded, Mapping) and isinstance(loaded.get("events"), list):
        loaded = loaded["events"]
    elif isinstance(loaded, Mapping):
        loaded = [loaded]
    elif isinstance(loaded, list):
        pass
    else:
        raise BridgeError(
            "source_invalid",
            f"source {name} does not contain an event object",
            "Write a canonical CloudEvents JSON object.",
        )

    events: list[dict[str, Any]] = []
    for index, item in enumerate(loaded):
        if not isinstance(item, Mapping):
            raise BridgeError(
                "source_invalid",
                f"source {name} item {index} is not an event object",
                "Write a canonical CloudEvents JSON object.",
            )
        events.append(dict(item))
    if not events:
        raise BridgeError("source_invalid", f"source {name} has no events", "Write one event.")
    return events


def _canonical(value: Any) -> bytes:
    return bee_events._canonical(value)


def _canonical_events_sha256(events: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for event in events:
        digest.update(_canonical(event))
    return digest.hexdigest()


def _write_ack(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Create the ack once; identical identity is idempotent, other bytes conflict."""

    payload = _canonical(record)
    existing = _read_regular(path)
    if existing is not None:
        try:
            old = json.loads(existing.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(
                "ack_conflict",
                f"ack {path.name} is not valid JSON",
                "Inspect the acknowledgement directory; do not hand-edit acks.",
            ) from exc
        if not isinstance(old, Mapping):
            raise BridgeError("ack_conflict", f"ack {path.name} is not an object", "Inspect the ack.")
        same = (
            old.get("event_ids") == record.get("event_ids")
            and old.get("source_sha256") == record.get("source_sha256")
            and old.get("events_sha256") == record.get("events_sha256")
        )
        if not same:
            raise BridgeError(
                "source_conflict",
                f"source {record.get('source')} conflicts with an existing acknowledgement",
                "Keep the original source bytes; do not reuse a source name for another event.",
            )
        return dict(old)
    _ensure_private_dir(path.parent)
    try:
        bee_events._create_only_payload(path, payload)
    except OSError as exc:
        raise BridgeError(
            "ack_write_failed",
            f"acknowledgement write failed: {type(exc).__name__}",
            "Check the state directory is writable and retry.",
        ) from exc
    stored = _read_regular(path)
    if stored is None:
        raise BridgeError("ack_write_failed", "acknowledgement was not published", "Retry.")
    try:
        value = json.loads(stored.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive
        raise BridgeError("ack_write_failed", "acknowledgement is not JSON", "Inspect the ack.") from exc
    if not isinstance(value, Mapping):
        raise BridgeError("ack_write_failed", "acknowledgement is not an object", "Inspect the ack.")
    same = (
        value.get("event_ids") == record.get("event_ids")
        and value.get("source_sha256") == record.get("source_sha256")
        and value.get("events_sha256") == record.get("events_sha256")
    )
    if not same:
        raise BridgeError(
            "source_conflict",
            f"source {record.get('source')} conflicts with an existing acknowledgement",
            "Keep the original source bytes; do not reuse a source name for another event.",
        )
    return dict(value)


# ---------------------------------------------------------------------------
# Coordinator records and correlation
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    raw = _read_regular(path)
    if raw is None:
        return None
    value = _decode_json(raw, path.name)
    if not isinstance(value, Mapping):
        raise BridgeError(
            "identity_invalid",
            f"{path.name} is not a JSON object",
            "Use coordinator-written lease/job records.",
        )
    return dict(value)


def _safe_attempt(attempt_id: Any) -> str:
    text = attempt_id.strip() if isinstance(attempt_id, str) else ""
    if not text or ".." in text or not _ATTEMPT_RE.match(text):
        raise BridgeError(
            "identity_invalid",
            "event attempt_id is not a safe lease identity",
            "Emit the attempt id exactly as the coordinator recorded it.",
        )
    return text


def _safe_job(job_id: Any) -> str | None:
    if job_id is None:
        return None
    text = job_id.strip() if isinstance(job_id, str) else ""
    if not text or ".." in text or not _JOB_RE.match(text):
        raise BridgeError(
            "identity_invalid",
            "event job_id is not a safe job identity",
            "Emit the job id exactly as the coordinator recorded it.",
        )
    return text


def _load_lease_record(state_dir: Any, attempt_id: str) -> dict[str, Any] | None:
    path = Path(state_dir) / "leases" / f"{attempt_id}.json"
    raw = _read_regular(path)
    if raw is None:
        return None
    try:
        plain = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "lease_invalid",
            "the coordinator lease record is not valid JSON",
            "Restore the coordinator-written lease; do not hand-edit it.",
        ) from exc
    if not isinstance(plain, Mapping):
        raise BridgeError("lease_invalid", "the coordinator lease record is not an object", "Restore the lease.")
    if "mac" in plain or "seal" in plain:
        try:
            return load_lease(Path(state_dir), attempt_id)
        except LeaseError as exc:
            raise BridgeError(exc.code, exc.error, exc.fix) from exc
    return dict(plain)


def _load_job_record(state_dir: Any, job_id: str) -> dict[str, Any] | None:
    path = Path(state_dir) / "jobs" / f"{job_id}.json"
    raw = _read_regular(path)
    if raw is None:
        return None
    try:
        plain = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError(
            "job_invalid",
            "the coordinator job record is not valid JSON",
            "Restore the coordinator-written job; do not hand-edit it.",
        ) from exc
    if not isinstance(plain, Mapping):
        raise BridgeError("job_invalid", "the coordinator job record is not an object", "Restore the job.")
    if "mac" in plain or "seal" in plain or "body" in plain:
        try:
            return load_job(Path(state_dir), job_id)
        except JobError as exc:
            # A minimal, readable coordinator record may have no seal.  If it
            # looks like a full job document, an unsealed/missing-key failure is
            # a refusal rather than a reason to ignore its scope.
            if exc.code in {"job_missing"}:
                return None
            raise BridgeError(exc.code, str(exc), exc.fix) from exc
    return dict(plain)


def _digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) == 64 and all(ch in "0123456789abcdef" for ch in text):
        return text
    return None


def _nested(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _record_source_rev(record: Mapping[str, Any]) -> Any:
    value = _nested(record, "source_rev", "base_rev", "source")
    if value is not None:
        return value
    for nested_name in ("goal", "brief", "lease"):
        nested = record.get(nested_name)
        if isinstance(nested, Mapping):
            value = _nested(nested, "source_rev", "base_rev", "source")
            if value is not None:
                return value
    return None


def _record_assigned_files(record: Mapping[str, Any]) -> Any:
    value = _nested(record, "assigned_files", "lease_files")
    if value is not None:
        return value
    direct_lease = record.get("lease")
    if isinstance(direct_lease, (list, tuple)):
        return direct_lease
    for nested_name in ("lease", "scope", "brief"):
        nested = record.get(nested_name)
        if isinstance(nested, Mapping):
            value = _nested(nested, "assigned_files", "lease_files")
            if value is not None:
                return value
    return None


def _record_digest(record: Mapping[str, Any]) -> str | None:
    direct = _nested(record, "sha256", "lease_sha256", "brief_sha256")
    digest = _digest(direct)
    if digest is not None:
        return digest
    nested = record.get("lease")
    if isinstance(nested, Mapping):
        digest = _digest(nested.get("sha256"))
        if digest is not None:
            return digest
    brief = record.get("brief")
    if isinstance(brief, Mapping):
        return hashlib.sha256(_canonical(brief)).hexdigest()
    return None


def _record_digest_candidates(record: Mapping[str, Any]) -> set[str]:
    candidates: set[str] = set()
    primary = _record_digest(record)
    if primary is not None:
        candidates.add(primary)
    brief = record.get("brief")
    if isinstance(brief, Mapping):
        candidates.add(hashlib.sha256(_canonical(brief)).hexdigest())
    files = _record_assigned_files(record)
    source_rev = _record_source_rev(record)
    identity = {
        "attempt_id": record.get("attempt_id"),
        "assigned_files": list(files) if isinstance(files, (list, tuple)) else files,
        "source_rev": source_rev,
    }
    candidates.add(hashlib.sha256(_canonical(identity)).hexdigest())
    if isinstance(files, (list, tuple)):
        candidates.add(hashlib.sha256(_canonical(sorted(str(item) for item in files))).hexdigest())
    return candidates


def lease_sha256(record: Mapping[str, Any]) -> str:
    """Return the digest a canonical event should carry for *record*.

    Coordinator leases written by :mod:`bundlebees_plugin.leases` already carry
    ``brief_sha256``.  A record with an explicit ``sha256``/``lease_sha256`` is
    honoured too.  For a minimal record without any of those, the digest is the
    SHA-256 of the canonical ``attempt_id``/``source_rev``/``assigned_files``
    identity, which is deterministic for all writers.
    """

    existing = _record_digest(record)
    if existing is not None:
        return existing
    identity = {
        "attempt_id": record.get("attempt_id"),
        "assigned_files": _record_assigned_files(record) or [],
        "source_rev": _record_source_rev(record),
    }
    return hashlib.sha256(_canonical(identity)).hexdigest()


def _same_files(left: Any, right: Any) -> bool:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return False
    return sorted(str(item) for item in left) == sorted(str(item) for item in right)


def _same_source(left: Any, right: Any) -> bool:
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.strip().lower() == right.strip().lower()
    )


def _verify_against_record(
    record: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    where: str,
    require_attempt: bool = True,
) -> None:
    attempt = data.get("attempt_id")
    recorded_attempt = record.get("attempt_id")
    if require_attempt:
        if recorded_attempt is None:
            raise BridgeError(
                "identity_missing",
                f"{where} has no attempt identity",
                "Record the attempt lease before landing its event.",
            )
        if str(recorded_attempt) != str(attempt):
            raise BridgeError(
                "attempt_mismatch",
                f"event attempt {attempt!r} does not match {where} attempt {recorded_attempt!r}",
                "Land only the attempt the coordinator leased.",
            )

    source_rev = data.get("source_rev")
    recorded_rev = _record_source_rev(record)
    if recorded_rev is None:
        raise BridgeError(
            "identity_missing",
            f"{where} has no source_rev",
            "Restore the coordinator lease/job record before landing.",
        )
    if not _same_source(source_rev, recorded_rev):
        raise BridgeError(
            "source_rev_mismatch",
            "event source_rev does not match the coordinator record",
            "Land the event against the pinned source revision.",
        )

    recorded_files = _record_assigned_files(record)
    if recorded_files is None:
        raise BridgeError(
            "identity_missing",
            f"{where} has no assigned_files lease",
            "Restore the coordinator lease/job record before landing.",
        )
    event_files = data.get("lease", {}).get("assigned_files")
    if not _same_files(event_files, recorded_files):
        raise BridgeError(
            "lease_mismatch",
            "event lease.assigned_files does not match the coordinator record",
            "Land the event with exactly the coordinator-written lease.",
        )

    if _record_digest(record) is not None:
        event_digest = _digest(data.get("lease", {}).get("sha256"))
        if event_digest not in _record_digest_candidates(record):
            raise BridgeError(
                "lease_mismatch",
                "event lease.sha256 does not match the coordinator record",
                "Land the event with the coordinator-written lease digest.",
            )


def verify_event_identity(state_dir: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    """Correlate one canonical event with coordinator lease/job records.

    Returns a small summary of the records used.  No record at all is an
    ``identity_missing`` refusal: a landing must have an authoritative attempt
    identity.
    """

    data = event.get("data")
    if not isinstance(data, Mapping):
        raise BridgeError("event_invalid", "event data is not an object", "Use build_event.")
    attempt = _safe_attempt(data.get("attempt_id"))
    job_id = _safe_job(data.get("job_id"))
    task_id = data.get("task_id")

    lease = _load_lease_record(state_dir, attempt)
    job = _load_job_record(state_dir, job_id) if job_id is not None else None
    if lease is None and job is None:
        raise BridgeError(
            "identity_missing",
            f"no coordinator lease or job record exists for attempt {attempt}",
            "Record the attempt lease/job before bridging a landing event.",
        )

    records: list[str] = []
    if lease is not None:
        _verify_against_record(lease, data, where="coordinator lease")
        records.append("lease")

    if job is not None:
        recorded_job = job.get("job_id")
        if recorded_job is not None and str(recorded_job) != str(job_id):
            raise BridgeError(
                "job_mismatch",
                "event job_id does not match the coordinator job record",
                "Land the event with the coordinator-written job id.",
            )
        task = None
        if task_id is not None:
            for candidate in job.get("tasks") or []:
                if isinstance(candidate, Mapping) and str(candidate.get("task_id")) == str(task_id):
                    task = candidate
                    break
            if task is None:
                raise BridgeError(
                    "task_mismatch",
                    "event task_id is not present in the coordinator job record",
                    "Land the event with a task the coordinator admitted.",
                )
        if task is not None:
            attempts = task.get("attempts")
            if isinstance(attempts, (list, tuple)) and attempts:
                if attempt not in [str(item) for item in attempts]:
                    raise BridgeError(
                        "attempt_mismatch",
                        "event attempt_id is not recorded for this job task",
                        "Land only an attempt the coordinator dispatched for the task.",
                    )
            elif lease is None:
                raise BridgeError(
                    "identity_missing",
                    "the coordinator job task has no attempt identity",
                    "Record the attempt lease before landing its event.",
                )
            task_record = {
                "attempt_id": attempt if attempts else None,
                "source_rev": _record_source_rev(job),
                "assigned_files": _record_assigned_files(task),
                "sha256": _record_digest(task),
            }
            _verify_against_record(
                task_record,
                data,
                where="coordinator job task",
                require_attempt=bool(attempts),
            )
            records.append("job")
        else:
            goal_record = {
                "attempt_id": attempt,
                "source_rev": _record_source_rev(job),
            }
            recorded_rev = goal_record["source_rev"]
            if recorded_rev is not None and not _same_source(data.get("source_rev"), recorded_rev):
                raise BridgeError(
                    "source_rev_mismatch",
                    "event source_rev does not match the coordinator job base_rev",
                    "Land the event against the pinned source revision.",
                )
            records.append("job")

    return {"attempt_id": attempt, "job_id": job_id, "records": records}


# ---------------------------------------------------------------------------
# Source acknowledgement and moving
# ---------------------------------------------------------------------------


def _ack_dir(source_dir: Path) -> Path:
    return source_dir / ACK_DIR


def _ack_meta_path(source_dir: Path, name: str) -> Path:
    return _ack_dir(source_dir) / _ACK_META_DIR / f"{name}{_ACK_SUFFIX}"


def _ack_for(source_dir: Path, name: str) -> dict[str, Any] | None:
    path = _ack_meta_path(source_dir, name)
    raw = _read_regular(path)
    if raw is None:
        return None
    value = _decode_json(raw, path.name)
    if not isinstance(value, Mapping):
        raise BridgeError("ack_conflict", f"ack {path.name} is not an object", "Inspect the ack.")
    return dict(value)


def _move_source(source: Path, raw: bytes) -> None:
    acked = _ack_dir(source.parent)
    _ensure_private_dir(acked)
    destination = acked / source.name
    if destination.exists():
        existing = _read_regular(destination)
        if existing != raw:
            raise BridgeError(
                "source_conflict",
                f"source {source.name} conflicts with an already-acked file",
                "Keep distinct source names for distinct event bytes.",
            )
        try:
            source.unlink()
        except FileNotFoundError:
            pass
        _fsync_dir(source.parent)
        return
    try:
        os.replace(source, destination)
    except OSError as exc:
        raise BridgeError(
            "ack_write_failed",
            f"source move failed: {type(exc).__name__}",
            "Retry after the source directory is writable.",
        ) from exc
    _fsync_dir(source.parent)
    _fsync_dir(acked)


def _ack_record(
    name: str,
    raw: bytes,
    events: list[Mapping[str, Any]],
    delivery: Mapping[str, Any],
    now: Any = None,
) -> dict[str, Any]:
    return {
        "schema": ACK_SCHEMA,
        "source": name,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "event_ids": [event["id"] for event in events],
        "events_sha256": _canonical_events_sha256(events),
        "delivery": {
            "delivered": int(delivery.get("delivered") or 0),
            "skipped": int(delivery.get("skipped") or 0),
            "failed": int(delivery.get("failed") or 0),
            "pending": int(delivery.get("pending") or 0),
        },
        "acked_at": bee_events._iso(bee_events._clock(now), microseconds=True),
    }


# ---------------------------------------------------------------------------
# Consuming the source outbox
# ---------------------------------------------------------------------------


def _iter_source_files(source_dir: Path) -> list[Path]:
    if not source_dir.exists():
        return []
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise BridgeError(
            "source_invalid",
            "launcher outbox is not a directory",
            "Pass a real directory containing canonical landing events.",
        )
    files: list[Path] = []
    for item in sorted(source_dir.iterdir(), key=lambda path: path.name):
        if item.name in {ACK_DIR} or item.name.startswith("."):
            continue
        if item.name.endswith(_ACK_SUFFIX) or item.name.endswith(".tmp"):
            continue
        files.append(item)
    return files


def scan_pending(state_dir: Any, launcher_outbox: Any) -> list[str]:
    """Return source file names that have not been acknowledged yet."""

    source = Path(launcher_outbox)
    pending: list[str] = []
    for path in _iter_source_files(source):
        if _ack_for(source, path.name) is None:
            pending.append(path.name)
    return pending


def _process_file(
    state_dir: Path,
    source: Path,
    *,
    now: Any = None,
) -> dict[str, Any]:
    raw = _read_regular(source)
    if raw is None:
        return {"source": source.name, "state": "missing"}
    events = _decode_source_events(raw, source.name)
    ack = _ack_for(source.parent, source.name)
    if ack is not None:
        record = _ack_record(source.name, raw, events, {})
        same = (
            ack.get("source_sha256") == record["source_sha256"]
            and ack.get("event_ids") == record["event_ids"]
            and ack.get("events_sha256") == record["events_sha256"]
        )
        if not same:
            raise BridgeError(
                "source_conflict",
                f"source {source.name} conflicts with its durable acknowledgement",
                "Keep the original source bytes; do not reuse a source name.",
            )
        _move_source(source, raw)
        return {
            "source": source.name,
            "state": "duplicate",
            "event_ids": [event["id"] for event in events],
        }

    for event in events:
        bee_events.validate_event(event)

    # Validate and correlate the whole file before writing or acking any of it.
    identities: list[dict[str, Any]] = []
    for event in events:
        identities.append(verify_event_identity(state_dir, event))

    # Preflight the whole file so a later conflict never leaves an earlier
    # event from the same source recorded but unacknowledged.
    seen: dict[str, bytes] = {}
    for event in events:
        event_id = event["id"]
        canonical = _canonical(event)
        if event_id in seen and seen[event_id] != canonical:
            raise BridgeError(
                "event_conflict",
                f"source {source.name} contains conflicting bytes for {event_id}",
                "Keep one event per event id; do not rewrite a landing.",
            )
        seen[event_id] = canonical
        path = bee_events._outbox_path(state_dir, event_id)
        existing = bee_events._read_regular(path)
        if existing is not None and existing != canonical:
            raise BridgeError(
                "event_conflict",
                f"source {source.name} conflicts with the durable event {event_id}",
                "Keep the original landing bytes; do not rewrite a landing.",
            )

    for event in events:
        bee_events.record_landing(state_dir, event)

    delivery = bee_events.deliver_pending(state_dir)
    pending_ids = {item["event_id"] for item in bee_events.pending_deliveries(state_dir)}
    blocked = [event["id"] for event in events if event["id"] in pending_ids]
    if blocked:
        raise BridgeError(
            "delivery_pending",
            f"delivery is not durable for event(s): {', '.join(blocked)}",
            "Keep the source file; retry delivery before acknowledging it.",
        )

    record = _ack_record(source.name, raw, events, delivery, now)
    stored = _write_ack(_ack_meta_path(source.parent, source.name), record)
    _move_source(source, raw)
    return {
        "source": source.name,
        "state": "acked",
        "event_ids": stored["event_ids"],
        "delivery": stored["delivery"],
        "identities": identities,
    }


def consume_once(
    state_dir: Any,
    launcher_outbox: Any,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Process every source file once.  Safe to call again after a crash."""

    state = Path(state_dir)
    source = Path(launcher_outbox)
    processed: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for path in _iter_source_files(source):
        try:
            result = _process_file(state, path, now=now)
        except (BridgeError, bee_events.EventError) as exc:
            failures.append(
                {
                    "source": path.name,
                    "error": getattr(exc, "code", "bridge_failed"),
                    "message": getattr(exc, "error", str(exc)),
                }
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one source must not stop the scan
            failures.append(
                {
                    "source": path.name,
                    "error": "bridge_failed",
                    "message": bee_events.scrub(f"{type(exc).__name__}: {exc}"),
                }
            )
            continue
        if result.get("state") == "duplicate":
            duplicates.append(result)
        elif result.get("state") == "acked":
            processed.append(result)

    return {
        "processed": len(processed),
        "duplicates": len(duplicates),
        "failed": len(failures),
        "pending": len(scan_pending(state, source)),
        "sources": processed + duplicates,
        "failures": failures,
    }


#: Short aliases for embedders that name the one-shot pass differently.
consume = consume_once
run_once = consume_once


def process_pending(state_dir: Any, launcher_outbox: Any, *, now: Any = None) -> dict[str, Any]:
    """Compatibility alias for :func:`consume_once`."""

    return consume_once(state_dir, launcher_outbox, now=now)


def run(
    state_dir: Any,
    launcher_outbox: Any,
    *,
    once: bool = False,
    interval_s: float = 1.0,
    max_iterations: int | None = None,
) -> dict[str, Any]:
    """Consume once, or loop until interrupted."""

    if once:
        return consume_once(state_dir, launcher_outbox)
    iterations = 0
    last: dict[str, Any] = {
        "processed": 0,
        "duplicates": 0,
        "failed": 0,
        "pending": 0,
        "sources": [],
        "failures": [],
    }
    while True:
        last = consume_once(state_dir, launcher_outbox)
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return last
        time.sleep(max(0.0, float(interval_s)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bundlebees-landing-bridge",
        description="Bridge a hyvv canonical landing outbox into bundlebees.",
    )
    parser.add_argument("--state", required=True, help="bundlebees coordinator state directory")
    parser.add_argument("--launcher-outbox", required=True, help="hyvv canonical outbox directory")
    parser.add_argument("--once", action="store_true", help="process the current outbox and exit")
    parser.add_argument("--interval", type=float, default=1.0, help="polling interval in seconds")
    args = parser.parse_args(argv)

    if args.once:
        result = consume_once(args.state, args.launcher_outbox)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["failed"] == 0 else 1
    try:
        run(args.state, args.launcher_outbox, interval_s=args.interval)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
