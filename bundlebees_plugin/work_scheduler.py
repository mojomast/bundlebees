"""Agent-agnostic, work-conserving coordinator scheduler.

The coordinator keeps one durable queue at ``<state>/coordinator/work.json``.
An item names *what* should happen (``kind``), never *who* should do it and
never *how*: there is no prompt, argv, model id or adapter in a record. The
scheduler only decides which item is eligible, who currently owns it, and which
safe checkpoint a landing event may preempt. It never calls a model, dispatches
a bee, or touches approval, scope or budget state.

Item shape::

    {
      "work_id": "ws_plan_api",
      "title": "Plan the API",
      "kind": "research|spec|tests|docs|integration-plan|triage|feature",
      "priority": 0..9,                 # 0 is most urgent
      "status": "pending|blocked|claimed|done",
      "depends_on": ["ws_other"],
      "owner": "",
      "lease_files": ["src/api.py"],
      "worktree": "",
      "checkpoint": {"note": "...", "evidence": [], "at": "..."} | null,
      "created_at": "...",
      "updated_at": "..."
    }

Concurrency rules:

* ``add_item`` is create-only. Replaying the same item returns the stored
  record; a different record at the same ``work_id`` is refused, never
  overwritten.
* Every mutation re-reads the queue, mutates it and atomically replaces it
  while holding ``coordinator/work.lock`` (``flock``). Claims are therefore
  serialised across processes, not merely threads.
* ``claim`` enforces one active item per owner, a bounded number of active
  claims, and one reserved slot that planning may never consume: a landing
  event always finds room without waiting for a planning claim to finish.
* ``checkpoint`` fsyncs the record before returning, so a crash after the
  return can never lose an acknowledged checkpoint.
* ``preempt_for_event`` is a *query*. It returns an advisory checkpoint action
  or ``"none"`` and never changes the queue, so it can never interrupt a
  non-idempotent operation: only an item already sitting on a durable
  checkpoint is ever proposed for preemption.

The clock is injectable (``now``) in every mutating call, so ordering and
timestamps are deterministic under test.
"""

from __future__ import annotations

import json
import os
import posixpath
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot prove a single claim
    fcntl = None  # type: ignore[assignment]

SCHEMA = "bundlebees.work-scheduler/1"

KINDS: tuple[str, ...] = (
    "research",
    "spec",
    "tests",
    "docs",
    "integration-plan",
    "triage",
    "feature",
)
STATUSES: tuple[str, ...] = ("pending", "blocked", "claimed", "done")

#: Kinds that only plan. They may never consume the slot reserved for landing
#: events, and they are the only kinds a landing event may preempt.
PLANNING_KINDS: tuple[str, ...] = (
    "research",
    "spec",
    "docs",
    "integration-plan",
    "triage",
)
#: Kinds that deliver change. They may use the reserved landing slot.
DELIVERY_KINDS: tuple[str, ...] = ("tests", "feature")

#: Bounded concurrency: at most this many items are claimed at once.
MAX_ACTIVE = 4
#: Slots planning may not consume, so a landing event always has capacity.
RESERVED_FOR_LANDING = 1

_LANDING_EVENTS = frozenset({"landing", "land", "merge", "integration"})

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX = (
    "Inspect the durable queue at coordinator/work.json; a scheduler item is a "
    "record of intent and is never overwritten to hide a second decision."
)


class SchedulerError(Exception):
    """A scheduler request was refused: missing, corrupt, conflicting or not owned."""

    def __init__(self, code: str, message: str, fix: str = _FIX) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Identifiers, time and small normalisers
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise SchedulerError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 96 characters.",
        )
    return text


def _now_iso(now: Any) -> str:
    epoch = now() if callable(now) else now
    if epoch is None:
        epoch = datetime.now(tz=timezone.utc).timestamp()
    if isinstance(epoch, str):
        return _normalise_iso(epoch)
    try:
        value = float(epoch)
    except (TypeError, ValueError) as exc:
        raise SchedulerError(
            "clock_invalid",
            f"now {epoch!r} is not epoch seconds or an ISO timestamp",
            "Pass a clock the scheduler can read.",
        ) from exc
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalise_iso(value: str) -> str:
    text = value.strip()
    if not text:
        raise SchedulerError("clock_invalid", "an ISO timestamp may not be empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SchedulerError(
            "clock_invalid", f"{text!r} is not an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _lease_key(path: Any) -> str:
    text = str(path if path is not None else "").strip().replace("\\", "/")
    if not text or text in {".", "/"}:
        raise SchedulerError(
            "lease_invalid",
            f"lease file {text!r} is empty",
            "Name a repository-relative file or directory to lease.",
        )
    normalised = posixpath.normpath(text)
    if normalised == ".." or normalised.startswith("../") or "/../" in normalised:
        raise SchedulerError(
            "lease_invalid",
            f"lease file {text!r} escapes the worktree",
            "Lease paths inside the worktree only.",
        )
    return normalised


def _lease_set(paths: Iterable[Any] | None) -> set[str]:
    return {_lease_key(path) for path in (paths or ())}


def _ids(values: Iterable[Any] | None, what: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        text = _safe_id(value, what)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _priority(value: Any) -> int:
    if isinstance(value, bool):
        raise SchedulerError("priority_invalid", "priority must be an integer 0..9")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SchedulerError(
            "priority_invalid", f"priority {value!r} must be an integer 0..9"
        ) from exc
    if number < 0 or number > 9:
        raise SchedulerError(
            "priority_invalid", f"priority {value!r} must be an integer 0..9"
        )
    return number


def _evidence(values: Iterable[Any] | None) -> list[Any]:
    evidence = list(values or ())
    try:
        json.dumps(evidence)
    except (TypeError, ValueError) as exc:
        raise SchedulerError(
            "evidence_invalid",
            f"checkpoint evidence is not JSON-serialisable: {exc}",
            "Pass strings or plain JSON values as evidence.",
        ) from exc
    return evidence


# ---------------------------------------------------------------------------
# Paths, locking and durable JSON
# ---------------------------------------------------------------------------


def queue_path(state: Any) -> Path:
    """The one durable queue path for a coordinator state directory."""

    return Path(state) / "coordinator" / "work.json"


def _lock_path(state: Any) -> Path:
    return Path(state) / "coordinator" / "work.lock"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _payload(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode(
            "utf-8"
        )
        + b"\n"
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace *path* and fsync the file and its directory."""

    _private_dir(path.parent)
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


def _read_queue(state: Any) -> list[dict[str, Any]]:
    path = queue_path(state)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SchedulerError(
            "queue_unreadable", f"scheduler queue {path} cannot be read: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SchedulerError(
            "queue_corrupt",
            f"scheduler queue {path} is not valid JSON: {exc}",
            "Inspect it by hand; a corrupt queue is not permission to decide.",
        ) from exc
    if not isinstance(data, dict) or str(data.get("schema") or "") != SCHEMA:
        raise SchedulerError(
            "queue_corrupt",
            f"scheduler queue {path} is not schema {SCHEMA!r}",
            "Restore the queue from its last durable copy.",
        )
    items = data.get("items")
    if not isinstance(items, list):
        raise SchedulerError("queue_corrupt", f"scheduler queue {path} has no items list")
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise SchedulerError("queue_corrupt", f"scheduler queue {path} has a non-object item")
        result.append(_normalise_item(item))
    return result


def _write_queue(state: Any, items: Sequence[Mapping[str, Any]]) -> None:
    ordered = sorted((dict(item) for item in items), key=lambda item: str(item["work_id"]))
    _write_json(queue_path(state), {"schema": SCHEMA, "items": ordered})


@contextmanager
def _locked(state: Any) -> Iterator[None]:
    """Serialise read-modify-write of the whole queue with a kernel-held lock."""

    if fcntl is None:  # pragma: no cover - no portable fallback for a proof of one claim
        raise SchedulerError(
            "locking_unavailable",
            "this platform has no flock, so a single active claim cannot be proven",
            "Run the coordinator on POSIX (Linux/macOS); an unlocked claim is not a claim.",
        )
    lock = _lock_path(state)
    _private_dir(lock.parent)
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


# ---------------------------------------------------------------------------
# Item validation
# ---------------------------------------------------------------------------


def _normalise_item(item: Mapping[str, Any]) -> dict[str, Any]:
    work_id = _safe_id(item.get("work_id"), "work_id")
    kind = str(item.get("kind") or "")
    if kind not in KINDS:
        raise SchedulerError(
            "kind_invalid", f"item {work_id!r} has kind {kind!r}, not one of {KINDS}"
        )
    status = str(item.get("status") or "pending")
    if status not in STATUSES:
        raise SchedulerError(
            "status_invalid", f"item {work_id!r} has status {status!r}, not one of {STATUSES}"
        )
    checkpoint = item.get("checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, Mapping):
            raise SchedulerError("checkpoint_invalid", f"item {work_id!r} has a non-object checkpoint")
        checkpoint = {
            "note": str(checkpoint.get("note") or ""),
            "evidence": list(checkpoint.get("evidence") or ()),
            "at": str(checkpoint.get("at") or ""),
        }
    return {
        "work_id": work_id,
        "title": str(item.get("title") or ""),
        "kind": kind,
        "priority": _priority(item.get("priority", 5)),
        "status": status,
        "depends_on": _ids(item.get("depends_on"), "depends_on"),
        "owner": str(item.get("owner") or ""),
        "lease_files": sorted(_lease_set(item.get("lease_files"))),
        "worktree": str(item.get("worktree") or ""),
        "checkpoint": checkpoint,
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or item.get("created_at") or ""),
    }


def _copy(item: Mapping[str, Any]) -> dict[str, Any]:
    copied = dict(item)
    copied["depends_on"] = list(item.get("depends_on") or ())
    copied["lease_files"] = list(item.get("lease_files") or ())
    checkpoint = item.get("checkpoint")
    copied["checkpoint"] = dict(checkpoint) if isinstance(checkpoint, Mapping) else None
    return copied


def _find(items: Sequence[Mapping[str, Any]], work_id: str) -> tuple[int, dict[str, Any]]:
    for index, item in enumerate(items):
        if item["work_id"] == work_id:
            return index, dict(item)
    raise SchedulerError(
        "work_not_found",
        f"no scheduler item {work_id!r}",
        "List the queue and use an exact work_id.",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_item(
    state: Any,
    *,
    work_id: Any,
    title: Any = "",
    kind: Any = "feature",
    priority: Any = 5,
    depends_on: Iterable[Any] | None = None,
    lease_files: Iterable[Any] | None = None,
    worktree: Any = "",
    now: Any = None,
) -> dict[str, Any]:
    """Create one durable item; replay of an identical item is a no-op.

    A different item at the same ``work_id`` is refused: the queue is
    create-only and never silently replaces intent.
    """

    wid = _safe_id(work_id, "work_id")
    kind_text = str(kind or "")
    if kind_text not in KINDS:
        raise SchedulerError(
            "kind_invalid", f"kind {kind_text!r} is not one of {KINDS}"
        )
    deps = _ids(depends_on, "depends_on")
    if wid in deps:
        raise SchedulerError(
            "dependency_invalid",
            f"item {wid!r} may not depend on itself",
            "Name a different upstream item.",
        )
    stamp = _now_iso(now)
    item = {
        "work_id": wid,
        "title": str(title or ""),
        "kind": kind_text,
        "priority": _priority(priority),
        "status": "pending",
        "depends_on": deps,
        "owner": "",
        "lease_files": sorted(_lease_set(lease_files)),
        "worktree": str(worktree or ""),
        "checkpoint": None,
        "created_at": stamp,
        "updated_at": stamp,
    }
    with _locked(state):
        items = _read_queue(state)
        for existing in items:
            if existing["work_id"] == wid:
                if _same_intent(existing, item):
                    return _copy(existing)
                raise SchedulerError(
                    "work_exists",
                    f"item {wid!r} already exists with different content",
                    "Choose a new work_id; scheduler items are create-only.",
                )
        items.append(item)
        _write_queue(state, items)
    return _copy(item)


def _same_intent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = ("work_id", "title", "kind", "priority", "depends_on", "lease_files", "worktree")
    return all(left.get(key) == right.get(key) for key in keys)


def list_items(state: Any) -> list[dict[str, Any]]:
    """All items in deterministic ``created_at``/``work_id`` order."""

    items = _read_queue(state)
    items.sort(key=lambda item: (str(item["created_at"]), str(item["work_id"])))
    return [_copy(item) for item in items]


def _done_ids(items: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(item["work_id"]) for item in items if item["status"] == "done"}


def _active_leases(
    items: Sequence[Mapping[str, Any]], extra: Iterable[str]
) -> dict[str, set[str]]:
    active: dict[str, set[str]] = {}
    for item in items:
        if item["status"] != "claimed":
            continue
        owner = str(item.get("owner") or item["work_id"])
        active.setdefault(owner, set()).update(item.get("lease_files") or ())
    if extra:
        active.setdefault("", set()).update(extra)
    return active


def _flatten_leases(active_bee_leases: Any) -> set[str]:
    if active_bee_leases is None:
        return set()
    if isinstance(active_bee_leases, Mapping):
        files: set[str] = set()
        for value in active_bee_leases.values():
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                files.add(_lease_key(value))
            else:
                files.update(_lease_key(path) for path in value)
        return files
    values = list(active_bee_leases)
    if all(isinstance(value, (str, bytes)) for value in values):
        return _lease_set(values)
    files = set()
    for value in values:
        if isinstance(value, (str, bytes)):
            files.add(_lease_key(value))
        else:
            files.update(_lease_key(path) for path in value)
    return files


def _conflicts(leased: Iterable[str], active: Iterable[str]) -> bool:
    return bool(set(leased) & set(active))


def _ready(
    items: Sequence[Mapping[str, Any]], active_leases: set[str]
) -> list[dict[str, Any]]:
    done = _done_ids(items)
    ready: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "pending":
            continue
        deps = list(item.get("depends_on") or ())
        if any(dep not in done for dep in deps):
            continue
        if _conflicts(item.get("lease_files") or (), active_leases):
            continue
        ready.append(dict(item))
    ready.sort(key=lambda item: (int(item["priority"]), str(item["created_at"]), str(item["work_id"])))
    return [_copy(item) for item in ready]


def ready_items(state: Any, active_bee_leases: Any = None) -> list[dict[str, Any]]:
    """Pending items with satisfied dependencies and no lease conflict.

    ``active_bee_leases`` is an owner-to-files mapping (or a flat iterable of
    files) for bees already running outside the queue. Claims recorded *inside*
    the queue always count as active leases too.

    Order is deterministic: priority (0 first), then ``created_at``, then
    ``work_id``.
    """

    items = _read_queue(state)
    active = _flatten_leases(active_bee_leases)
    for files in _active_leases(items, ()).values():
        active.update(files)
    return _ready(items, active)


def _active_count(items: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in items if item["status"] == "claimed")


def _owner_active(items: Sequence[Mapping[str, Any]], owner: str) -> dict[str, Any] | None:
    for item in items:
        if item["status"] == "claimed" and item.get("owner") == owner:
            return dict(item)
    return None


def claim(
    state: Any,
    work_id: Any,
    owner: Any,
    worktree: Any = "",
    *,
    now: Any = None,
    max_active: int | None = None,
) -> dict[str, Any]:
    """Atomically give one pending item to one owner.

    Refused when dependencies are unmet, the item or one of its leased files is
    already active, the owner already holds an item, or the bounded (or
    planning-reserved) capacity is exhausted. Re-claiming an item by its
    current owner is idempotent and returns the stored record.
    """

    wid = _safe_id(work_id, "work_id")
    owner_id = str(owner or "").strip()
    if not owner_id:
        raise SchedulerError(
            "owner_invalid", "claim needs a non-empty owner", "Name the bee that will do the work."
        )
    limit = MAX_ACTIVE if max_active is None else int(max_active)
    if limit < 1:
        raise SchedulerError("capacity_invalid", "max_active must be at least 1")
    stamp = _now_iso(now)
    with _locked(state):
        items = _read_queue(state)
        index, item = _find(items, wid)
        if item["status"] == "claimed" and item.get("owner") == owner_id:
            return _copy(item)
        if item["status"] == "done":
            raise SchedulerError("work_done", f"item {wid!r} is already done")
        if item["status"] != "pending":
            raise SchedulerError(
                "work_not_pending",
                f"item {wid!r} is {item['status']!r}, not pending",
                "Release or resume the item before claiming it.",
            )
        done = _done_ids(items)
        unmet = [dep for dep in item.get("depends_on") or () if dep not in done]
        if unmet:
            raise SchedulerError(
                "dependency_blocked",
                f"item {wid!r} waits on unfinished {unmet}",
                "Complete the upstream items first.",
            )
        active = _active_count(items)
        limit_for_kind = limit
        if item["kind"] in PLANNING_KINDS:
            limit_for_kind = max(1, limit - RESERVED_FOR_LANDING)
        if active >= limit_for_kind:
            raise SchedulerError(
                "capacity_exhausted",
                f"{active} active item(s) already use the {limit_for_kind} slot(s) "
                f"available to {item['kind']!r} work",
                "Wait for a claim to finish; the last slot is reserved for landing events.",
            )
        busy = _owner_active(items, owner_id)
        if busy is not None:
            raise SchedulerError(
                "owner_busy",
                f"owner {owner_id!r} already holds {busy['work_id']!r}",
                "One owner may hold at most one active item.",
            )
        active_leases = set()
        for other in items:
            if other["status"] == "claimed":
                active_leases.update(other.get("lease_files") or ())
        clash = set(item.get("lease_files") or ()) & active_leases
        if clash:
            raise SchedulerError(
                "lease_conflict",
                f"item {wid!r} leases files already active: {sorted(clash)}",
                "Wait for the conflicting claim to release its lease.",
            )
        item["status"] = "claimed"
        item["owner"] = owner_id
        item["worktree"] = str(worktree or "")
        item["updated_at"] = stamp
        items[index] = item
        _write_queue(state, items)
        return _copy(item)


def checkpoint(
    state: Any,
    work_id: Any,
    note: Any,
    evidence: Iterable[Any] | None = None,
    *,
    now: Any = None,
) -> dict[str, Any]:
    """Record one durable, fsynced checkpoint on a claimed item."""

    wid = _safe_id(work_id, "work_id")
    text = str(note or "").strip()
    if not text:
        raise SchedulerError(
            "checkpoint_invalid", "a checkpoint needs a non-empty note", "Say what is safe to resume from."
        )
    proof = _evidence(evidence)
    stamp = _now_iso(now)
    with _locked(state):
        items = _read_queue(state)
        index, item = _find(items, wid)
        if item["status"] != "claimed":
            raise SchedulerError(
                "work_not_claimed",
                f"item {wid!r} is {item['status']!r}; only a claimed item can checkpoint",
                "Claim the item before checkpointing it.",
            )
        item["checkpoint"] = {"note": text, "evidence": proof, "at": stamp}
        item["updated_at"] = stamp
        items[index] = item
        _write_queue(state, items)
        return _copy(item)


def complete(
    state: Any,
    work_id: Any,
    *,
    owner: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Mark a claimed item done and free its owner. Idempotent when already done."""

    wid = _safe_id(work_id, "work_id")
    stamp = _now_iso(now)
    with _locked(state):
        items = _read_queue(state)
        index, item = _find(items, wid)
        if item["status"] == "done":
            return _copy(item)
        if item["status"] != "claimed":
            raise SchedulerError(
                "work_not_claimed", f"item {wid!r} is {item['status']!r}, not claimed"
            )
        if owner is not None and str(owner) != item.get("owner"):
            raise SchedulerError(
                "not_owner",
                f"item {wid!r} is held by {item.get('owner')!r}, not {owner!r}",
                "Only the current owner may complete the item.",
            )
        item["status"] = "done"
        item["owner"] = ""
        item["worktree"] = ""
        item["updated_at"] = stamp
        items[index] = item
        _write_queue(state, items)
        return _copy(item)


def release(
    state: Any,
    work_id: Any,
    *,
    owner: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Return a claimed item to pending, keeping its durable checkpoint."""

    wid = _safe_id(work_id, "work_id")
    stamp = _now_iso(now)
    with _locked(state):
        items = _read_queue(state)
        index, item = _find(items, wid)
        if item["status"] == "pending":
            return _copy(item)
        if item["status"] != "claimed":
            raise SchedulerError(
                "work_not_claimed", f"item {wid!r} is {item['status']!r}, not claimed"
            )
        if owner is not None and str(owner) != item.get("owner"):
            raise SchedulerError(
                "not_owner",
                f"item {wid!r} is held by {item.get('owner')!r}, not {owner!r}",
                "Only the current owner may release the item.",
            )
        item["status"] = "pending"
        item["owner"] = ""
        item["worktree"] = ""
        item["updated_at"] = stamp
        items[index] = item
        _write_queue(state, items)
        return _copy(item)


def resume(state: Any, work_id: Any, *, now: Any = None) -> dict[str, Any]:
    """Report the checkpoint to resume from after a landing event or restart.

    A ``blocked`` item whose dependencies are now done becomes ``pending``
    again; every other state is returned unchanged. The queue is read from disk
    on each call, so a resume survives a process restart by construction.
    """

    wid = _safe_id(work_id, "work_id")
    stamp = _now_iso(now)
    with _locked(state):
        items = _read_queue(state)
        index, item = _find(items, wid)
        if item.get("checkpoint") is None:
            raise SchedulerError(
                "no_checkpoint",
                f"item {wid!r} has no checkpoint to resume from",
                "Checkpoint the item before interrupting it.",
            )
        resumed = False
        reason = "status_unchanged"
        if item["status"] == "blocked":
            done = _done_ids(items)
            unmet = [dep for dep in item.get("depends_on") or () if dep not in done]
            if unmet:
                reason = "dependency_blocked"
            else:
                item["status"] = "pending"
                item["owner"] = ""
                item["worktree"] = ""
                item["updated_at"] = stamp
                items[index] = item
                _write_queue(state, items)
                resumed = True
                reason = "unblocked"
        elif item["status"] == "pending":
            reason = "ready_to_claim"
        elif item["status"] == "claimed":
            reason = "owner_still_holds"
        elif item["status"] == "done":
            reason = "done"
        return {
            "work_id": wid,
            "status": item["status"],
            "owner": item.get("owner") or "",
            "worktree": item.get("worktree") or "",
            "checkpoint": dict(item["checkpoint"]) if item.get("checkpoint") else None,
            "resumed": resumed,
            "reason": reason,
        }


def _checkpoint_safe(item: Mapping[str, Any]) -> bool:
    checkpoint = item.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return False
    return bool(str(checkpoint.get("at") or "").strip()) and bool(
        str(checkpoint.get("note") or "").strip()
    )


def preempt_for_event(state: Any, event: Any, *, now: Any = None) -> dict[str, Any]:
    """Advise a safe checkpoint action for a landing event; never mutate.

    The returned action is a *request*: the coordinator tells the owner to stop
    at its durable checkpoint and release. An item that has never checkpointed
    is never proposed, so a non-idempotent operation is never interrupted. A
    non-landing event never preempts anything.
    """

    stamp = _now_iso(now)
    payload = dict(event) if isinstance(event, Mapping) else {"kind": str(event or "")}
    kind = str(payload.get("kind") or payload.get("event") or "").strip().lower()
    if kind not in _LANDING_EVENTS:
        return {
            "action": "none",
            "reason": "event_not_landing",
            "event": {"kind": kind},
            "at": stamp,
        }
    items = _read_queue(state)
    active = [item for item in items if item["status"] == "claimed"]
    max_active = int(payload.get("max_active", MAX_ACTIVE))
    if _active_count(items) < max_active:
        return {
            "action": "none",
            "reason": "capacity_available",
            "event": {"kind": kind},
            "active": len(active),
            "capacity": max_active,
            "at": stamp,
        }
    event_leases = _lease_set(payload.get("lease_files") or ())
    candidates = [
        item
        for item in active
        if item["kind"] in PLANNING_KINDS and _checkpoint_safe(item)
    ]
    if not candidates:
        return {
            "action": "none",
            "reason": "no_safe_checkpoint",
            "event": {"kind": kind},
            "active": len(active),
            "capacity": max_active,
            "at": stamp,
        }

    def order(item: Mapping[str, Any]) -> tuple[int, int, str, str]:
        overlap = 1 if event_leases & _lease_set(item.get("lease_files") or ()) else 0
        return (-int(item["priority"]), -overlap, str(item["created_at"]), str(item["work_id"]))

    victim = sorted(candidates, key=order)[0]
    return {
        "action": "checkpoint",
        "event": {"kind": kind},
        "work_id": victim["work_id"],
        "owner": victim.get("owner") or "",
        "worktree": victim.get("worktree") or "",
        "checkpoint": dict(victim["checkpoint"]),
        "then": "release",
        "safe": True,
        "interrupts_non_idempotent": False,
        "at": stamp,
    }


def sleep_when_idle(ready: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    """The idle signal: never sleeps, never dispatches, only reports.

    When ``ready`` is empty the coordinator may wait for an event; when it is
    not, the caller must schedule instead. Calling this is always a no-op.
    """

    items = [dict(item) for item in (ready or ()) if isinstance(item, Mapping)]
    return {
        "sleep": not items,
        "count": len(items),
        "work_ids": [str(item.get("work_id") or "") for item in items],
        "reason": "no_eligible_work" if not items else "work_available",
    }


__all__ = [
    "SCHEMA",
    "KINDS",
    "STATUSES",
    "PLANNING_KINDS",
    "DELIVERY_KINDS",
    "MAX_ACTIVE",
    "RESERVED_FOR_LANDING",
    "SchedulerError",
    "queue_path",
    "add_item",
    "list_items",
    "ready_items",
    "claim",
    "checkpoint",
    "complete",
    "release",
    "resume",
    "preempt_for_event",
    "sleep_when_idle",
]
