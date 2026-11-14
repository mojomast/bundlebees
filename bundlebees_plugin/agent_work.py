"""Durable agent work queue for recovery requests (M4.3 lane A).

A recovery request is durable, but it is not a task: nothing owns *when* an
agent should look at it, and the mechanical tick must never wait for one. This
module publishes exactly one work item per canonical request, and gives a
separate consumer process a small, crash-safe claim protocol:

``pending -> claimed -> completed | refused``

The item is deliberately agent-agnostic. It names the request and its digest,
optionally hints which adapter should answer it, and records the claim and the
result. It never carries a prompt, an argv, a transcript or an instruction for
a particular agent; those belong to the adapter's own durable records.

Concurrency rules:

* ``publish`` is create-only. Replaying the same request returns the stored
  item; a different digest at the same path is refused, never overwritten.
* ``claim`` serialises read-modify-write under ``flock`` and allows exactly one
  active claim. A claim is adopted when its pid is dead or its TTL has expired;
  a live claim is never stolen because a file looks old.
* ``complete`` / ``refuse`` are terminal and idempotent. ``release`` returns an
  item to ``pending`` for a bounded retry by the consumer.

The clock is injected (``now``) in every mutating call so tests can prove the
TTL and stale-pid paths without sleeping.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock has no claim protocol
    fcntl = None  # type: ignore[assignment]

from . import recovery

SCHEMA = "rizzstack.agent-work/1"
STATES: tuple[str, ...] = ("pending", "claimed", "completed", "refused")

#: The consumer may invoke an adapter at most this many times for one item.
#: ``attempts`` counts claims; a claim beyond the budget is refused rather than
#: being allowed to call a model again.
MAX_ATTEMPTS = 2

DEFAULT_CLAIM_TTL_S = 900

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX = (
    "Inspect the work item and its recovery request; a work item is a durable "
    "queue record and is never overwritten to hide a second decision."
)


class WorkError(Exception):
    """A work item was refused: missing, corrupt, conflicting or not owned."""

    def __init__(self, code: str, message: str, fix: str = _FIX) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Identifiers, paths and time
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise WorkError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 96 characters.",
        )
    return text


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    epoch = _epoch(value)
    if epoch is None:
        raise WorkError(
            "clock_invalid",
            "now must be epoch seconds, an ISO timestamp or a callable",
            "Pass a clock the queue can read.",
        )
    return epoch


def work_id_for(request: Mapping[str, Any]) -> str:
    """``aw_`` + the first 16 hex chars of sha256(request_id ":" digest)."""

    request_id = str(request.get("request_id") or "").strip()
    digest = str(request.get("request_digest") or "").strip()
    if not request_id or not digest:
        raise WorkError(
            "request_incomplete",
            "a work item needs a request_id and a request_digest",
            "Publish the canonical request returned by recovery.build_request().",
        )
    return "aw_" + hashlib.sha256(f"{request_id}:{digest}".encode("utf-8")).hexdigest()[:16]


def _ordinal(state_dir: Path, request: Mapping[str, Any]) -> int:
    request_id = str(request.get("request_id") or "")
    number = recovery.request_ordinal(Path(state_dir), request_id)
    if number > 0:
        return number
    job_id = _safe_id(request.get("job_id"), "job_id")
    task_id = _safe_id(request.get("task_id"), "task_id")
    prefix = f"{job_id}.{task_id}."
    suffix = request_id[len(prefix):] if request_id.startswith(prefix) else ""
    if suffix.isdigit() and int(suffix) > 0:
        return int(suffix)
    raise WorkError(
        "request_incomplete",
        f"request {request_id!r} does not name a positive ordinal and is not on disk",
        "Publish only a durable canonical request; the work path mirrors its request path.",
    )


def work_path(state_dir: Path, request: Mapping[str, Any]) -> Path:
    """``recovery/<job>/<task>.<n>.work.json`` for the request's ordinal."""

    job_id = _safe_id(request.get("job_id"), "job_id")
    task_id = _safe_id(request.get("task_id"), "task_id")
    number = _ordinal(Path(state_dir), request)
    return recovery.recovery_dir(Path(state_dir), job_id) / f"{task_id}.{number}.work.json"


def lock_path(state_dir: Path, work_id: str) -> Path:
    """The sidecar lock for one work id; never deleted, so inodes stay stable."""

    _path, _item = _locate(Path(state_dir), work_id)
    return _path.with_suffix(".work.lock")


# ---------------------------------------------------------------------------
# Durable JSON
# ---------------------------------------------------------------------------


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


def _payload(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"


def _create_only_json(path: Path, value: Mapping[str, Any]) -> bool:
    """Publish JSON at *path* exactly once, atomically, mode 0600."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = _payload(value)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        _fsync_dir(path.parent)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace a work item; the caller holds its lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(value)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_item(path: Path) -> dict[str, Any] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkError("work_unreadable", f"work item {Path(path).name} cannot be read: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WorkError(
            "work_corrupt",
            f"work item {Path(path).name} is not valid JSON: {exc}",
            "Inspect it by hand; a corrupt work item is not permission to decide.",
        ) from exc
    if not isinstance(data, dict):
        raise WorkError("work_corrupt", f"work item {Path(path).name} is not an object")
    if str(data.get("schema") or "") != SCHEMA:
        raise WorkError(
            "work_schema",
            f"work item {Path(path).name} is schema {data.get('schema')!r}, not {SCHEMA!r}",
        )
    if not str(data.get("work_id") or "").startswith("aw_"):
        raise WorkError("work_corrupt", f"work item {Path(path).name} has no work_id")
    data.setdefault("state", "pending")
    data.setdefault("claim", None)
    data.setdefault("attempts", 0)
    data.setdefault("claims", int(data.get("attempts") or 0))
    data.setdefault("last_error", "")
    data.setdefault("result", {"proposal": None, "decision": None, "decision_digest": ""})
    data.setdefault("history", [])
    data.setdefault("created_at", "")
    data.setdefault("updated_at", data.get("created_at") or "")
    data.setdefault("adapter", "")
    return data


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialise read-modify-write for one item with a kernel-held lock."""

    if fcntl is None:  # pragma: no cover - the queue has no portable fallback
        raise WorkError(
            "locking_unavailable",
            "this platform has no flock, so a single active claim cannot be proven",
            "Run the consumer on POSIX (Linux/macOS); an unlocked claim is not a claim.",
        )
    lock = path.with_suffix(".work.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock.parent, 0o700)
    except OSError:
        pass
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _holder_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _claim_expired(claim: Mapping[str, Any], clock: float) -> bool:
    expires = _epoch(claim.get("expires_at"))
    if expires is not None and clock >= expires:
        return True
    return not _holder_alive(claim.get("pid"))


def _event(
    item: dict[str, Any],
    name: str,
    *,
    at: str,
    consumer: str = "",
    pid: int = 0,
    detail: str = "",
) -> None:
    history = item.setdefault("history", [])
    if not isinstance(history, builtins.list):
        history = []
        item["history"] = history
    history.append({"at": at, "event": str(name), "consumer": str(consumer), "pid": int(pid), "detail": str(detail)[:500]})


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish(
    state_dir: Path,
    request: Mapping[str, Any],
    *,
    adapter: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Publish one work item for a canonical request, create-only.

    An identical replay (same request id and digest) returns the stored item
    unchanged. A different digest at the same durable path is refused: a work
    item is never rewritten to point at another request.
    """

    if not isinstance(request, Mapping):
        raise WorkError("request_invalid", "publish() needs a recovery request mapping")
    request_id = _safe_id(request.get("request_id"), "request_id")
    digest = str(request.get("request_digest") or "").strip()
    if not digest:
        raise WorkError("request_incomplete", f"request {request_id!r} carries no request_digest")
    actual = recovery.request_digest(request)
    if digest != actual:
        raise WorkError(
            "request_tampered",
            f"request {request_id!r} hashes to {actual[:12]}, not its recorded {digest[:12]}",
        )
    job_id = _safe_id(request.get("job_id"), "job_id")
    task_id = _safe_id(request.get("task_id"), "task_id")
    work_id = work_id_for(request)
    path = work_path(Path(state_dir), request)

    existing = _read_item(path)
    if existing is not None:
        if existing.get("work_id") == work_id and str(existing.get("request_digest") or "") == digest:
            return existing
        raise WorkError(
            "work_conflict",
            f"a different work item already exists at {path.name}",
            "The request ordinal is taken; inspect recovery/<job>/ and do not overwrite it.",
        )

    stamp = _iso(_clock(now))
    item: dict[str, Any] = {
        "schema": SCHEMA,
        "work_id": work_id,
        "request_id": request_id,
        "request_digest": digest,
        "job_id": job_id,
        "task_id": task_id,
        "attempt_id": str(request.get("attempt_id") or ""),
        "adapter": str(adapter or ""),
        "state": "pending",
        "claim": None,
        "attempts": 0,
        "claims": 0,
        "last_error": "",
        "result": {"proposal": None, "decision": None, "decision_digest": ""},
        "created_at": stamp,
        "updated_at": stamp,
        "history": [],
    }
    _event(item, "published", at=stamp)
    if _create_only_json(path, item):
        return item

    existing = _read_item(path)
    if existing is not None and existing.get("work_id") == work_id and str(
        existing.get("request_digest") or ""
    ) == digest:
        return existing
    raise WorkError(
        "work_conflict",
        f"a different work item was published concurrently at {path.name}",
        "Inspect the durable item; a conflicting publication is never overwritten.",
    )


def _newest_requests(state_dir: Path, job: Mapping[str, Any]) -> builtins.list[dict[str, Any]]:
    """The newest undecided canonical request for every rejected task."""

    state = Path(state_dir)
    job_id = str(job.get("job_id") or "")
    if not job_id:
        return []
    all_requests = recovery.list_requests(state, job_id)
    by_task: dict[str, builtins.list[dict[str, Any]]] = {}
    for request in all_requests:
        by_task.setdefault(str(request.get("task_id") or ""), []).append(request)
    found: builtins.list[dict[str, Any]] = []
    for task in job.get("tasks") or []:
        if str(task.get("state") or "") != "rejected":
            continue
        task_id = str(task.get("task_id") or "")
        requests = by_task.get(task_id) or []
        if not requests:
            continue
        request = requests[-1]
        if recovery.read_decision(state, request) is None:
            found.append(request)
    return found


def publish_outstanding(
    state_dir: Path,
    job: Mapping[str, Any],
    *,
    adapter: str | None = None,
    now: Any = None,
) -> builtins.list[dict[str, Any]]:
    """Publish every outstanding request for *job*; safe to call every tick."""

    return [
        publish(state_dir, request, adapter=adapter, now=now)
        for request in _newest_requests(Path(state_dir), job)
    ]


# ---------------------------------------------------------------------------
# Load / list
# ---------------------------------------------------------------------------


def _locate(state_dir: Path, work_id: str) -> tuple[Path, dict[str, Any]]:
    wanted = str(work_id or "").strip()
    if not wanted.startswith("aw_"):
        raise WorkError("work_id_invalid", f"{wanted!r} is not a work id")
    root = Path(state_dir) / recovery.RECOVERY_DIR
    if not root.is_dir():
        raise WorkError("work_missing", f"no work item {wanted!r} exists")
    for path in sorted(root.glob("*/*.work.json")):
        item = _read_item(path)
        if item is not None and str(item.get("work_id") or "") == wanted:
            return path, item
    raise WorkError("work_missing", f"no work item {wanted!r} exists")


def load(state_dir: Path, work_id: str) -> dict[str, Any]:
    """Read one work item by id; a missing item is a refusal, not an empty one."""

    _path, item = _locate(Path(state_dir), work_id)
    return item


def list(
    state_dir: Path,
    *,
    job_id: str | None = None,
    state: str | None = None,
) -> builtins.list[dict[str, Any]]:
    """Every work item, optionally restricted to one job and/or one state."""

    root = Path(state_dir) / recovery.RECOVERY_DIR
    if job_id is not None:
        root = root / _safe_id(job_id, "job_id")
        pattern = "*.work.json"
    else:
        pattern = "*/*.work.json"
    if not root.is_dir():
        return []
    if state is not None and state not in STATES:
        raise WorkError("state_invalid", f"{state!r} is not one of {', '.join(STATES)}")
    found: builtins.list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(root.glob(pattern)):
        item = _read_item(path)
        if item is None:
            continue
        if state is not None and str(item.get("state") or "") != state:
            continue
        found.append((path, item))
    found.sort(key=lambda pair: (str(pair[1].get("created_at") or ""), str(pair[1].get("work_id") or ""), str(pair[0])))
    return [item for _path, item in found]


# ---------------------------------------------------------------------------
# Claim / complete / refuse / release
# ---------------------------------------------------------------------------


def claim(
    state_dir: Path,
    work_id: str,
    *,
    consumer: str,
    pid: int,
    ttl_s: float = DEFAULT_CLAIM_TTL_S,
    now: Any = None,
) -> dict[str, Any] | None:
    """Claim one item; ``None`` means another live consumer owns it or it is done.

    A stale claim (dead pid or elapsed TTL) is adopted and the new claimant is
    recorded. ``attempts`` counts claims so the consumer can bound retries.
    """

    name = str(consumer or "").strip()
    if not name:
        raise WorkError("consumer_invalid", "a claim must name the consumer")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise WorkError("pid_invalid", "a claim must name a positive process id")
    ttl = float(ttl_s)
    if ttl <= 0:
        raise WorkError("ttl_invalid", "the claim TTL must be positive")
    clock = _clock(now)
    path, _item = _locate(Path(state_dir), work_id)
    with _locked(path):
        item = _read_item(path)
        if item is None:
            raise WorkError("work_missing", f"no work item {work_id!r} exists")
        state = str(item.get("state") or "")
        if state in {"completed", "refused"}:
            return None
        if state not in {"pending", "claimed"}:
            raise WorkError("work_corrupt", f"work item {work_id!r} has unknown state {state!r}")

        current = item.get("claim")
        event = "claimed"
        detail = ""
        if state == "claimed" and isinstance(current, Mapping):
            same = str(current.get("consumer") or "") == name and current.get("pid") == pid
            expired = _claim_expired(current, clock)
            if same and not expired:
                return item
            if not expired:
                return None
            event = "adopted_stale_claim"
            detail = f"previous claim by {current.get('consumer')!r} pid {current.get('pid')}"
        elif state == "claimed":
            event = "recovered_missing_claim"

        attempts = int(item.get("attempts") or 0) + 1
        claims = int(item.get("claims") or attempts - 1) + 1
        stamp = _iso(clock)
        item["state"] = "claimed"
        item["claim"] = {
            "consumer": name,
            "pid": pid,
            "claimed_at": stamp,
            "expires_at": _iso(clock + ttl),
        }
        # ``attempts`` is the bounded adapter-work budget; ``claims`` records
        # every claim (including crash adoption) so a third claim can refuse
        # without ever pretending it has a third attempt to spend.
        item["attempts"] = min(attempts, MAX_ATTEMPTS)
        item["claims"] = claims
        item["updated_at"] = stamp
        item["last_error"] = ""
        _event(item, event, at=stamp, consumer=name, pid=pid, detail=detail)
        _write_json(path, item)
        return item


def complete(
    state_dir: Path,
    work_id: str,
    *,
    consumer: str,
    pid: int,
    proposal: Any = None,
    decision: Mapping[str, Any] | None = None,
    decision_digest: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Record the adapter's result and close the item, idempotently.

    Repeating the same completion returns the stored item. A completion that
    disagrees with an already-recorded result is refused.
    """

    name = str(consumer or "").strip()
    if not name:
        raise WorkError("consumer_invalid", "a completion must name the consumer")
    path, _item = _locate(Path(state_dir), work_id)
    clock = _clock(now)
    stamp = _iso(clock)
    with _locked(path):
        item = _read_item(path)
        if item is None:
            raise WorkError("work_missing", f"no work item {work_id!r} exists")
        state = str(item.get("state") or "")
        if state == "completed":
            stored = item.get("result") if isinstance(item.get("result"), Mapping) else {}
            same_decision = (
                decision is None
                or not stored.get("decision")
                or recovery.canonical_bytes(stored.get("decision")) == recovery.canonical_bytes(dict(decision))
            )
            if same_decision:
                return item
            raise WorkError(
                "work_conflict",
                f"work item {work_id!r} is already completed with a different decision",
            )
        if state == "refused":
            raise WorkError("work_terminal", f"work item {work_id!r} was refused; it cannot complete")
        held = item.get("claim")
        if not isinstance(held, Mapping) or str(held.get("consumer") or "") != name or held.get("pid") != pid:
            raise WorkError(
                "claim_not_held",
                f"work item {work_id!r} is not claimed by {name!r} pid {pid}",
                "Claim the item (a stale claim is adoptable) before completing it.",
            )

        result = dict(item.get("result") or {})
        if proposal is not None:
            result["proposal"] = str(proposal)
        if decision is not None:
            result["decision"] = dict(decision)
            if decision_digest is None:
                decision_digest = recovery.decision_digest(decision)
        if decision_digest is not None:
            result["decision_digest"] = str(decision_digest)
        item["result"] = {
            "proposal": result.get("proposal"),
            "decision": result.get("decision"),
            "decision_digest": str(result.get("decision_digest") or ""),
        }
        item["state"] = "completed"
        item["claim"] = None
        item["last_error"] = ""
        item["updated_at"] = stamp
        _event(item, "completed", at=stamp, consumer=name, pid=pid)
        _write_json(path, item)
        return item


def refuse(
    state_dir: Path,
    work_id: str,
    *,
    consumer: str,
    pid: int,
    error: str = "",
    reason: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Close one item as refused. Repeating a refusal returns the stored item."""

    name = str(consumer or "").strip()
    if not name:
        raise WorkError("consumer_invalid", "a refusal must name the consumer")
    text = str(error if reason is None else reason)
    path, _item = _locate(Path(state_dir), work_id)
    stamp = _iso(_clock(now))
    with _locked(path):
        item = _read_item(path)
        if item is None:
            raise WorkError("work_missing", f"no work item {work_id!r} exists")
        state = str(item.get("state") or "")
        if state == "refused":
            return item
        if state == "completed":
            raise WorkError("work_terminal", f"work item {work_id!r} is completed; it cannot be refused")
        held = item.get("claim")
        if not isinstance(held, Mapping) or str(held.get("consumer") or "") != name or held.get("pid") != pid:
            raise WorkError(
                "claim_not_held",
                f"work item {work_id!r} is not claimed by {name!r} pid {pid}",
                "Claim the item before refusing it.",
            )
        item["state"] = "refused"
        item["claim"] = None
        item["last_error"] = text[:2000]
        item["updated_at"] = stamp
        _event(item, "refused", at=stamp, consumer=name, pid=pid, detail=text[:500])
        _write_json(path, item)
        return item


def release(
    state_dir: Path,
    work_id: str,
    *,
    consumer: str,
    pid: int,
    error: str = "",
    reason: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Return an item to ``pending`` for a bounded retry by another claim."""

    name = str(consumer or "").strip()
    if not name:
        raise WorkError("consumer_invalid", "a release must name the consumer")
    text = str(error if reason is None else reason)
    path, _item = _locate(Path(state_dir), work_id)
    stamp = _iso(_clock(now))
    with _locked(path):
        item = _read_item(path)
        if item is None:
            raise WorkError("work_missing", f"no work item {work_id!r} exists")
        state = str(item.get("state") or "")
        if state == "pending" and not item.get("claim"):
            return item
        if state in {"completed", "refused"}:
            raise WorkError("work_terminal", f"work item {work_id!r} is {state}; it cannot be released")
        held = item.get("claim")
        if not isinstance(held, Mapping) or str(held.get("consumer") or "") != name or held.get("pid") != pid:
            raise WorkError(
                "claim_not_held",
                f"work item {work_id!r} is not claimed by {name!r} pid {pid}",
                "Only the active claimant may release an item.",
            )
        item["state"] = "pending"
        item["claim"] = None
        item["last_error"] = text[:2000]
        item["updated_at"] = stamp
        _event(item, "released", at=stamp, consumer=name, pid=pid, detail=text[:500])
        _write_json(path, item)
        return item


__all__ = [
    "DEFAULT_CLAIM_TTL_S",
    "MAX_ATTEMPTS",
    "SCHEMA",
    "STATES",
    "WorkError",
    "claim",
    "complete",
    "list",
    "list_items",
    "load",
    "lock_path",
    "path",
    "publish",
    "publish_outstanding",
    "refuse",
    "release",
    "work_id_for",
    "work_path",
]

#: Compatibility name for callers that prefer an explicit verb.
list_items = list

#: The path helper's short name, mirroring the interface description.
path = work_path
