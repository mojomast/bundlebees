"""The gate watcher: the mechanical planned-to-queued transition.

Today's lesson from the completed-attempt reconciliation sweep is that any
campaign transition that depends on coordinator attention is the failure
class: the sweep exists because finished attempts waited for a human to look
at them. The planned backlog had the same shape — a planned slice moved to
queued only when the coordinator noticed that its gate had cleared. This
module makes that transition mechanical.

A machine-readable planned-work backlog lives at ``<root>/plans/backlog.json``
(``bundlebees.gate-backlog/1``). Each slice carries a pre-written dispatch
brief and declares its gates; a *beat* — a first-class coordinator duty, run
on the same cadence as the reconciliation tick — evaluates every planned
slice's gates against live state and moves the slice through

    planned -> queued -> dispatched

without coordinator attention. Gate types:

* ``pr_merged``         — a named pull request is merged (a merged PR implies
                          its dependency merges).
* ``attempt_succeeded`` — a named attempt, a named backlog slice's recorded
                          attempt, or a named campaign-record landing fact.
* ``date``              — an ISO date has arrived.
* ``owner_decision``    — the owner has approved; never auto-dispatched, routed
                          to the decision queue instead.
* ``none``              — no gate; eligible as soon as budget and leases allow.

Guarantees, in the same spirit as the rest of the plugin:

* **Idempotent.** A dispatched slice is marked with its attempt id and is
  never re-dispatched. Re-running a beat against unchanged state writes
  nothing (the campaign-reconcile rule).
* **Fail closed on the world.** The world is an injected adapter with exactly
  the five probe members named in :class:`World`; an unprovable gate state is
  ``waiting``, never ``cleared``. Unknown never implies success.
* **Stale gates block, never kill.** A PR closed unmerged or a failed attempt
  marks the slice with a durable ``blocked`` record and a reason; if the world
  changes back (a PR reopens, a retry attempt is recorded) the block clears on
  a later beat and normal evaluation resumes. Re-pointing a gate is the
  coordinator's decision, never the watcher's.
* **Owner gates never dispatch.** An owner-gated slice routes to the decision
  queue (status ``blocked-owner``, transition recorded) and dispatches only
  after a recorded approval; a recorded decline is terminal for that slice.
* **Budgeted.** Dispatch respects the owner envelope — at most 5-7
  implementation lanes in flight and at most 10 bees — and the lease rules: a
  slice whose brief leases a file an active bee already holds is not
  dispatched.
* **Every transition recorded.** Each move is written into the backlog record
  with the gate evidence that justified it, per slice and in a top-level log.

The clock is injectable (``now``) in every call, so ordering and timestamps
are deterministic under test.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot prove one transition
    fcntl = None  # type: ignore[assignment]

from .brief import BriefError, _normalise_path, validate_brief
from .security import scrub

SCHEMA = "bundlebees.gate-backlog/1"
BACKLOG_DIR = "plans"
BACKLOG_FILE = "backlog.json"

#: The slice statuses. ``blocked-owner`` is the decision-queue parking state;
#: there is no separate ``blocked`` status — a stale gate marks a *planned*
#: slice with a durable ``blocked`` record and the watcher re-evaluates it
#: every beat, so a changed world un-blocks without a coordinator.
STATUSES: tuple[str, ...] = ("planned", "queued", "dispatched", "blocked-owner")

GATE_TYPES: tuple[str, ...] = (
    "pr_merged",
    "attempt_succeeded",
    "date",
    "owner_decision",
    "none",
)

#: The owner envelope: 5-7 implementation lanes in flight, 10 bees at most.
MIN_LANES = 5
MAX_LANES = 7
MAX_BEES = 10

#: Launcher attempt states, mirroring the plugin's terminal-state vocabulary.
SUCCESS_ATTEMPT_STATES = frozenset(
    {
        "succeeded",
        "success",
        "completed",
        "complete",
        "done",
        "landed",
        "accepted",
        "merged",
        "verified",
    }
)
FAILED_ATTEMPT_STATES = frozenset(
    {
        "failed",
        "error",
        "cancelled",
        "canceled",
        "timed_out",
        "timeout",
        "lost",
        "killed",
        "rejected",
    }
)

DECISIONS: tuple[str, ...] = ("approve", "decline")

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX = (
    "Inspect the durable backlog at plans/backlog.json; a slice is a record of "
    "planned intent and is never overwritten to hide a second decision."
)
_FIX_WORLD = (
    "Inject a world exposing pr_state(), attempt_state(), fact_state(), "
    "active_bee_leases() and active_bee_count(); an unobservable world never "
    "clears a gate."
)
_FIX_DISPATCHER = (
    "Inject a dispatcher exposing dispatch(payload) -> {attempt_id, ...}; the "
    "watcher marks a slice dispatched only from the dispatcher's own confirmation."
)


class GateWatcherError(Exception):
    """A backlog request was refused: missing, corrupt, invalid or not dispatchable."""

    def __init__(self, code: str, message: str, fix: str = _FIX) -> None:
        super().__init__(str(scrub(message)))
        self.code = str(code)
        self.message = str(scrub(message))
        self.fix = str(scrub(fix))

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def iso(now: Any = None) -> str:
    """An ISO-8601 UTC timestamp from an epoch, a callable clock or an ISO string."""

    if now is None:
        epoch: Any = time.time()
    elif callable(now):
        epoch = now()
    elif isinstance(now, str):
        return _normalise_iso(now)
    else:
        epoch = now
    try:
        value = float(epoch)
    except (TypeError, ValueError) as exc:
        raise GateWatcherError(
            "clock_invalid", f"now {now!r} is not epoch seconds or an ISO timestamp"
        ) from exc
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch(now: Any = None) -> float:
    if now is None:
        return time.time()
    if callable(now):
        return float(now())
    if isinstance(now, str):
        parsed = datetime.fromisoformat(_normalise_iso(now).replace("Z", "+00:00"))
        return parsed.timestamp()
    try:
        return float(now)
    except (TypeError, ValueError) as exc:
        raise GateWatcherError(
            "clock_invalid", f"now {now!r} is not epoch seconds or an ISO timestamp"
        ) from exc


def _normalise_iso(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise GateWatcherError("clock_invalid", "an ISO timestamp may not be empty")
    if len(text) == 10 and text.count("-") == 2:
        text += "T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateWatcherError("clock_invalid", f"{text!r} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Paths, locking and durable JSON
# ---------------------------------------------------------------------------


def default_root() -> Path:
    """The backlog root: the pinned workflow checkout when configured, else cwd."""

    value = os.environ.get("WORKFLOW_SRC", "")
    if value.strip():
        return Path(value.strip())
    return Path.cwd()


def backlog_path(root: Any) -> Path:
    return Path(root) / BACKLOG_DIR / BACKLOG_FILE


def _lock_path(root: Any) -> Path:
    return Path(root) / BACKLOG_DIR / "backlog.lock"


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
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_payload(value))
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


@contextmanager
def _locked(root: Any) -> Iterator[None]:
    """Serialise read-modify-write of the whole backlog with a kernel-held lock."""

    if fcntl is None:  # pragma: no cover - no portable fallback for a proof of one transition
        raise GateWatcherError(
            "locking_unavailable",
            "this platform has no flock, so one mechanical transition cannot be proven",
            "Run the coordinator on POSIX (Linux/macOS).",
        )
    lock = _lock_path(root)
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
# Validation
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise GateWatcherError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe backlog identifier",
            "Use letters, digits, '-' and '_' only, at most 96 characters.",
        )
    return text


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GateWatcherError("slice_invalid", f"{field} must be a non-empty string")
    return value.strip()


def _priority(value: Any) -> int:
    if isinstance(value, bool):
        raise GateWatcherError("slice_invalid", "priority must be an integer 0..9")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise GateWatcherError(
            "slice_invalid", f"priority {value!r} must be an integer 0..9"
        ) from exc
    if number < 0 or number > 9:
        raise GateWatcherError(
            "slice_invalid", f"priority {value!r} must be an integer 0..9"
        )
    return number


def _lease_key(value: Any, code: str = "slice_invalid") -> str:
    try:
        return _normalise_path(value, 0)
    except BriefError as exc:
        raise GateWatcherError(code, str(exc)) from exc


def _normalise_gate(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, Mapping):
        raise GateWatcherError("gate_invalid", "a gate must be an object")
    kind = str(gate.get("type") or "")
    if kind not in GATE_TYPES:
        raise GateWatcherError(
            "gate_invalid",
            f"gate type {kind!r} is not one of {GATE_TYPES}",
            "Declare gates with the five supported types only.",
        )
    normalised: dict[str, Any] = {"type": kind}
    if kind == "pr_merged":
        pr = gate.get("pr")
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
            raise GateWatcherError(
                "gate_invalid", "a pr_merged gate needs a positive integer 'pr'"
            )
        normalised["pr"] = pr
    elif kind == "attempt_succeeded":
        refs = [
            name
            for name in ("attempt", "slice", "fact")
            if str(gate.get(name) or "").strip()
        ]
        if len(refs) != 1:
            raise GateWatcherError(
                "gate_invalid",
                "an attempt_succeeded gate names exactly one of 'attempt', 'slice' or 'fact'",
            )
        name = refs[0]
        value = str(gate.get(name)).strip()
        if name in ("slice", "fact"):
            value = _safe_id(value, f"gate.{name}")
        normalised[name] = value
    elif kind == "date":
        try:
            normalised["on"] = _normalise_iso(gate.get("on"))
        except GateWatcherError as exc:
            raise GateWatcherError(
                "gate_invalid",
                f"a date gate needs an ISO 'on' date: {exc.message}",
            ) from exc
    elif kind == "owner_decision":
        question = str(gate.get("question") or "").strip()
        if question:
            normalised["question"] = question
    return normalised


def _normalise_gates(gates: Any) -> list[dict[str, Any]]:
    if gates is None:
        return [{"type": "none"}]
    if not isinstance(gates, list):
        raise GateWatcherError("gate_invalid", "gates must be a list of gate objects")
    if not gates:
        return [{"type": "none"}]
    return [_normalise_gate(gate) for gate in gates]


def _normalise_depends_on(value: Any, slice_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise GateWatcherError("slice_invalid", "depends_on must be a list of slice ids")
    deps: list[str] = []
    for item in value:
        dep = _safe_id(item, "depends_on")
        if dep == slice_id:
            raise GateWatcherError(
                "slice_invalid", f"slice {slice_id!r} may not depend on itself"
            )
        if dep not in deps:
            deps.append(dep)
    return deps


def _normalise_blocked(value: Any, slice_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GateWatcherError(
            "slice_invalid", f"slice {slice_id!r} has a non-object blocked record"
        )
    return {
        "reason": str(value.get("reason") or "gate_stale"),
        "gate": str(value.get("gate") or ""),
        "evidence": dict(value.get("evidence") or {}),
        "at": str(value.get("at") or ""),
    }


def _normalise_owner_request(value: Any, slice_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GateWatcherError(
            "slice_invalid", f"slice {slice_id!r} has a non-object owner_request"
        )
    return {
        "question": str(value.get("question") or ""),
        "requested_at": str(value.get("requested_at") or ""),
    }


def _normalise_last_error(value: Any, slice_id: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise GateWatcherError(
            "slice_invalid", f"slice {slice_id!r} has a non-object last_dispatch_error"
        )
    return {
        "code": str(value.get("code") or "dispatch_failed"),
        "message": str(value.get("message") or ""),
        "at": str(value.get("at") or ""),
    }


def _normalise_slice(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise GateWatcherError("slice_invalid", "a slice must be an object")
    slice_id = _safe_id(record.get("id"), "slice id")
    status = str(record.get("status") or "planned")
    if status not in STATUSES:
        raise GateWatcherError(
            "slice_invalid",
            f"slice {slice_id!r} has status {status!r}, not one of {STATUSES}",
        )
    try:
        brief = validate_brief(record.get("brief"))
    except BriefError as exc:
        raise GateWatcherError(
            "brief_invalid",
            f"slice {slice_id!r} has an invalid brief: {exc.error}",
            "Briefs are pre-written dispatch payloads; fix the named field.",
        ) from exc
    attempt_id = record.get("attempt_id")
    transitions = [
        dict(item) for item in (record.get("transitions") or []) if isinstance(item, Mapping)
    ]
    return {
        "id": slice_id,
        "title": _text(record.get("title"), f"slice {slice_id!r} title"),
        "project": _text(record.get("project"), f"slice {slice_id!r} project"),
        "lane": _safe_id(record.get("lane"), f"slice {slice_id!r} lane"),
        "priority": _priority(record.get("priority", 5)),
        "status": status,
        "brief": brief,
        "gates": _normalise_gates(record.get("gates")),
        "depends_on": _normalise_depends_on(record.get("depends_on"), slice_id),
        "notes": str(record.get("notes") or ""),
        "attempt_id": str(attempt_id) if attempt_id else None,
        "queued_at": str(record.get("queued_at") or "") or None,
        "dispatched_at": str(record.get("dispatched_at") or "") or None,
        "blocked": _normalise_blocked(record.get("blocked"), slice_id),
        "owner_request": _normalise_owner_request(record.get("owner_request"), slice_id),
        "last_dispatch_error": _normalise_last_error(
            record.get("last_dispatch_error"), slice_id
        ),
        "transitions": transitions,
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or record.get("created_at") or ""),
    }


def _same_intent(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = ("id", "title", "project", "lane", "priority", "gates", "depends_on", "brief")
    return all(left.get(key) == right.get(key) for key in keys)


def _budget(value: Any) -> dict[str, int]:
    mapping = dict(value) if isinstance(value, Mapping) else {}
    lanes = int(mapping.get("lanes", MAX_LANES))
    bees = int(mapping.get("bees", MAX_BEES))
    if not MIN_LANES <= lanes <= MAX_LANES:
        raise GateWatcherError(
            "budget_invalid",
            f"the lane budget {lanes} is outside the owner envelope of "
            f"{MIN_LANES}-{MAX_LANES} implementation lanes",
        )
    if not 1 <= bees <= MAX_BEES:
        raise GateWatcherError(
            "budget_invalid", f"the bee cap {bees} is above the owner cap of {MAX_BEES}"
        )
    return {"lanes": lanes, "bees": bees}


# ---------------------------------------------------------------------------
# Backlog document
# ---------------------------------------------------------------------------


def _read_document(root: Any) -> dict[str, Any]:
    path = backlog_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GateWatcherError(
            "backlog_missing",
            f"no gate backlog at {BACKLOG_DIR}/{BACKLOG_FILE}",
            "Seed the backlog first (seed_backlog); the watcher never invents planned work.",
        ) from exc
    except OSError as exc:
        raise GateWatcherError(
            "backlog_unreadable", f"gate backlog cannot be read: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise GateWatcherError(
            "backlog_corrupt",
            f"gate backlog is not valid JSON: {exc}",
            "Inspect it by hand; a corrupt backlog is not permission to decide.",
        ) from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise GateWatcherError(
            "backlog_corrupt",
            f"gate backlog is not schema {SCHEMA!r}",
            "Restore the backlog from its last durable copy.",
        )
    slices = data.get("slices")
    if not isinstance(slices, list):
        raise GateWatcherError("backlog_corrupt", "gate backlog has no slices list")
    normalised = [_normalise_slice(item) for item in slices]
    seen: set[str] = set()
    for item in normalised:
        if item["id"] in seen:
            raise GateWatcherError(
                "backlog_corrupt", f"gate backlog has a duplicate slice {item['id']!r}"
            )
        seen.add(item["id"])
    return {
        "schema": SCHEMA,
        "budget": _budget(data.get("budget")),
        "slices": sorted(normalised, key=lambda item: item["id"]),
        "transitions": [
            dict(item) for item in (data.get("transitions") or []) if isinstance(item, Mapping)
        ],
        "decisions": [
            dict(item) for item in (data.get("decisions") or []) if isinstance(item, Mapping)
        ],
        "revision": int(data.get("revision") or 1),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _persist(root: Any, document: dict[str, Any], *, stamp: str) -> None:
    """Write the live document; the lock holder owns every record in it.

    The slice records are the beat's own objects — persisting must never
    detach them into copies, or the next mutation would land outside the
    durable document.
    """

    document["slices"] = sorted(document["slices"], key=lambda item: item["id"])
    document["updated_at"] = stamp
    document["revision"] = int(document.get("revision") or 1) + 1
    _write_json(backlog_path(root), document)


def _snapshot(document: Mapping[str, Any]) -> str:
    return _payload(
        {
            "slices": document["slices"],
            "transitions": document["transitions"],
            "decisions": document["decisions"],
        }
    ).decode("utf-8")


def _decision_map(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in document.get("decisions") or ():
        slice_id = str(item.get("slice_id") or "")
        if slice_id:
            latest[slice_id] = dict(item)
    return latest


# ---------------------------------------------------------------------------
# The seeded backlog: the known planned slices with their real gates
# ---------------------------------------------------------------------------


def _seed_rev(slice_id: str) -> str:
    """The campaign record's pinned base revision placeholder for a seeded slice.

    The digest is deterministic so the seeded document is reproducible; the
    coordinator re-pins each brief's ``source_rev`` from the campaign record
    before the first beat that can dispatch real work.
    """

    return hashlib.sha256(f"bundlebees-seed:{slice_id}".encode("utf-8")).hexdigest()[:40]


def _seed_brief(
    slice_id: str,
    objective: str,
    files: Sequence[str],
    *,
    branch: str | None = None,
) -> dict[str, Any]:
    brief: dict[str, Any] = {
        "objective": objective,
        "source_rev": _seed_rev(slice_id),
        "assigned_files": list(files),
        "acceptance": {
            "criteria": [f"{slice_id} ships with its gate evidence recorded"],
            "tests": [f"tests/test_slice_{slice_id.replace('-', '_')}.py"],
        },
        "deliverables": [f"{slice_id} merged to the campaign integration branch"],
    }
    if branch:
        brief["branch"] = branch
    return brief


#: The current known planned slices, seeded with the gates the campaign record
#: records. ``ov0-spike`` carries no gate: it is the spike everything in the
#: orbit lane waits on. ``F18``/``F19`` wait on the dependency cluster through
#: ``F16`` — a conservative superset of the campaign record's "after deps".
#: ``F5``/``M2`` wait on the recorded ``F2`` landing fact and ``F14`` on
#: ``M1``. The go-switch is a pure date gate. Wave-6 closeout is the one
#: owner-gated slice: it routes to the decision queue and never dispatches on
#: its own. If a seeded slice's work was already committed outside the backlog
#: (an already-dispatched spike, for example), re-seed it with its recorded
#: ``status``/``attempt_id`` so the watcher adopts rather than repeats it.
SEED_SLICES: tuple[dict[str, Any], ...] = (
    {
        "id": "ov0-spike",
        "title": "OV-0 spike: prove the blocking-wait op end to end",
        "project": "orbitdesktop",
        "lane": "orbit",
        "priority": 1,
        "notes": "The spike the orbit lane gates on; no external gate of its own.",
        "brief": _seed_brief(
            "ov0-spike",
            "Spike the launcher blocking-wait operation end to end and record the "
            "boundary conditions (restart mid-wait, multi-bee concurrency, TTL).",
            ["pc-launcher/await_op.py", "docs/LOCAL_WORKERS.md"],
            branch="feat/ov0-await-spike",
        ),
        "gates": [{"type": "none"}],
    },
    {
        "id": "go-switch",
        "title": "Nov 29 go-switch",
        "project": "orbitdesktop",
        "lane": "release",
        "priority": 2,
        "notes": "Pure date gate: the switch activates on 2026-11-29.",
        "brief": _seed_brief(
            "go-switch",
            "Execute the Nov 29 go-switch: flip the planned switch and record the "
            "activation evidence in the campaign record.",
            ["pc-launcher/switch_config.py"],
            branch="feat/go-switch",
        ),
        "gates": [{"type": "date", "on": "2026-11-29T00:00:00Z"}],
    },
    {
        "id": "F16",
        "title": "F16: wave-6 feature slice (after #44 and its deps)",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 3,
        "notes": (
            "Campaign record: F16 after #44+deps. A merged #44 implies its "
            "dependency merges, so the mechanical gate is the #44 merge itself."
        ),
        "brief": _seed_brief(
            "F16",
            "Implement feature slice F16 on the post-#44 base.",
            ["src/features/f16.py"],
            branch="feat/f16",
        ),
        "gates": [{"type": "pr_merged", "pr": 44}],
    },
    {
        "id": "F18",
        "title": "F18: wave-6 feature slice (after deps)",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 5,
        "notes": (
            "Campaign record: F18 after deps. Seeded conservatively as depends_on "
            "F16, the dependency cluster's lead slice; re-seed with the exact "
            "cluster edges from the campaign record."
        ),
        "brief": _seed_brief(
            "F18",
            "Implement feature slice F18 once its dependency cluster has landed.",
            ["src/features/f18.py"],
            branch="feat/f18",
        ),
        "gates": [{"type": "none"}],
        "depends_on": ["F16"],
    },
    {
        "id": "F19",
        "title": "F19: wave-6 feature slice (after deps)",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 5,
        "notes": (
            "Campaign record: F19 after deps. Seeded conservatively as depends_on "
            "F16; re-seed with the exact cluster edges from the campaign record."
        ),
        "brief": _seed_brief(
            "F19",
            "Implement feature slice F19 once its dependency cluster has landed.",
            ["src/features/f19.py"],
            branch="feat/f19",
        ),
        "gates": [{"type": "none"}],
        "depends_on": ["F16"],
    },
    {
        "id": "OV-A",
        "title": "OV-A: op=await on the proven spike",
        "project": "orbitdesktop",
        "lane": "orbit",
        "priority": 4,
        "notes": "Campaign record: OV-A after the OV-0 spike lands.",
        "brief": _seed_brief(
            "OV-A",
            "Build bundlebees_attempt op=await on the proven OV-0 spike: the "
            "bounded internal loop, the waiter record and the immediate return "
            "on a terminal attempt.",
            ["bundlebees_plugin/tools.py", "docs/w6-native-completion-design.md"],
            branch="feat/ov-a-await",
        ),
        "gates": [{"type": "attempt_succeeded", "slice": "ov0-spike"}],
        "depends_on": ["ov0-spike"],
    },
    {
        "id": "R02",
        "title": "R02: rails slice (after the #40 merge)",
        "project": "bundlebees",
        "lane": "rails",
        "priority": 4,
        "notes": "Campaign record: rails R02+ after the #40 merge.",
        "brief": _seed_brief(
            "R02",
            "Implement rails slice R02 on the post-#40 base.",
            ["rails/r02.py"],
            branch="feat/r02",
        ),
        "gates": [{"type": "pr_merged", "pr": 40}],
    },
    {
        "id": "R03",
        "title": "R03: rails slice (after the #40 merge)",
        "project": "bundlebees",
        "lane": "rails",
        "priority": 5,
        "notes": "Stands in the R02+ rails series; the same #40 gate applies per rail.",
        "brief": _seed_brief(
            "R03",
            "Implement rails slice R03 on the post-#40 base.",
            ["rails/r03.py"],
            branch="feat/r03",
        ),
        "gates": [{"type": "pr_merged", "pr": 40}],
    },
    {
        "id": "F5",
        "title": "F5: feature slice (after F2)",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 4,
        "notes": "Campaign record: F5 after F2. The gate reads the recorded F2 landing fact.",
        "brief": _seed_brief(
            "F5",
            "Implement feature slice F5 once F2 has landed.",
            ["src/features/f5.py"],
            branch="feat/f5",
        ),
        "gates": [{"type": "attempt_succeeded", "fact": "F2"}],
    },
    {
        "id": "M2",
        "title": "M2 canary (after F2)",
        "project": "bundlebees",
        "lane": "canary",
        "priority": 6,
        "notes": "Campaign record: M2 canary after F2. The canary runs on the landed F2 base.",
        "brief": _seed_brief(
            "M2",
            "Run the M2 canary on the landed F2 base and record its evidence.",
            ["canary/m2.py"],
            branch="feat/m2-canary",
        ),
        "gates": [{"type": "attempt_succeeded", "fact": "F2"}],
    },
    {
        "id": "F14",
        "title": "F14: feature slice (after M1)",
        "project": "orbitdesktop",
        "lane": "features",
        "priority": 5,
        "notes": "Campaign record: F14 after M1. The gate reads the recorded M1 landing fact.",
        "brief": _seed_brief(
            "F14",
            "Implement feature slice F14 once M1 has landed.",
            ["src/features/f14.py"],
            branch="feat/f14",
        ),
        "gates": [{"type": "attempt_succeeded", "fact": "M1"}],
    },
    {
        "id": "wave6-closeout",
        "title": "Wave-6 closeout",
        "project": "bundlebees",
        "lane": "closeout",
        "priority": 8,
        "notes": "Owner-gated: routes to the decision queue and never auto-dispatches.",
        "brief": _seed_brief(
            "wave6-closeout",
            "Prepare the Wave-6 closeout: product evidence, audit triage and the "
            "closeout report, for the owner's decision.",
            ["docs/closeout.md"],
            branch="feat/wave6-closeout",
        ),
        "gates": [
            {
                "type": "owner_decision",
                "question": "Close out Wave 6 and prepare the campaign closeout report?",
            }
        ],
    },
)


# ---------------------------------------------------------------------------
# The world and the dispatcher: the exact injected seams
# ---------------------------------------------------------------------------


class World:
    """The structural contract for the live-state adapter (an injected seam).

    ``pr_state(pr)`` answers ``"merged"``/``"open"``/``"closed"``/None for one
    pull request. ``attempt_state(attempt_id)`` answers the launcher's attempt
    state or None. ``fact_state(fact)`` answers the campaign record's landing
    fact for a named slice or dependency (``"landed"``/``"failed"``/other/None).
    ``active_bee_leases()`` answers the owner-to-assigned-files mapping for
    bees in flight, and ``active_bee_count()`` the number of live bees. A
    probe that cannot observe answers None: an unprovable gate state is
    ``waiting``, never ``cleared``.
    """

    __slots__ = ("_world",)

    _MEMBERS = (
        "pr_state",
        "attempt_state",
        "fact_state",
        "active_bee_leases",
        "active_bee_count",
    )

    def __init__(self, world: Any) -> None:
        if world is None:
            raise GateWatcherError("world_incompatible", "no world was supplied", _FIX_WORLD)
        missing = [name for name in self._MEMBERS if not callable(getattr(world, name, None))]
        if missing:
            raise GateWatcherError(
                "world_incompatible",
                "the world is missing " + ", ".join(missing),
                _FIX_WORLD,
            )
        self._world = world

    def pr_state(self, pr: int) -> str | None:
        state = self._world.pr_state(int(pr))
        if state is not None and not isinstance(state, str):
            raise GateWatcherError(
                "world_incompatible",
                "pr_state() must return a state string or None",
                _FIX_WORLD,
            )
        return state

    def attempt_state(self, attempt_id: str) -> str | None:
        state = self._world.attempt_state(str(attempt_id))
        if state is not None and not isinstance(state, str):
            raise GateWatcherError(
                "world_incompatible",
                "attempt_state() must return a state string or None",
                _FIX_WORLD,
            )
        return state

    def fact_state(self, fact: str) -> str | None:
        state = self._world.fact_state(str(fact))
        if state is not None and not isinstance(state, str):
            raise GateWatcherError(
                "world_incompatible",
                "fact_state() must return a state string or None",
                _FIX_WORLD,
            )
        return state

    def active_bee_leases(self) -> dict[str, list[str]]:
        leases = self._world.active_bee_leases()
        if leases is None:
            return {}
        if isinstance(leases, (str, bytes)) or not isinstance(leases, Mapping):
            raise GateWatcherError(
                "world_incompatible",
                "active_bee_leases() must return an owner-to-files mapping",
                _FIX_WORLD,
            )
        result: dict[str, list[str]] = {}
        for owner, files in leases.items():
            if isinstance(files, (str, bytes)) or not isinstance(files, Iterable):
                raise GateWatcherError(
                    "world_incompatible",
                    "active_bee_leases() must map an owner to a file list",
                    _FIX_WORLD,
                )
            try:
                result[str(owner)] = [_lease_key(path, "world_incompatible") for path in files]
            except GateWatcherError as exc:
                raise GateWatcherError(
                    "world_incompatible",
                    f"active_bee_leases() returned an unusable lease path: {exc.message}",
                    _FIX_WORLD,
                ) from exc
        return result

    def active_bee_count(self) -> int:
        count = self._world.active_bee_count()
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise GateWatcherError(
                "world_incompatible",
                "active_bee_count() must return a non-negative integer",
                _FIX_WORLD,
            )
        return count


class _Dispatcher:
    """The structural contract for the dispatch adapter (an injected seam)."""

    __slots__ = ("_dispatcher",)

    def __init__(self, dispatcher: Any) -> None:
        if dispatcher is None or not callable(getattr(dispatcher, "dispatch", None)):
            raise GateWatcherError(
                "dispatcher_incompatible",
                "the dispatcher is missing dispatch(payload)",
                _FIX_DISPATCHER,
            )
        self._dispatcher = dispatcher

    def dispatch(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            result = self._dispatcher.dispatch(dict(payload))
        except GateWatcherError:
            raise
        except Exception as exc:
            raise GateWatcherError(
                "dispatch_failed", f"the dispatcher refused the dispatch: {exc}"
            ) from exc
        if not isinstance(result, Mapping) or not str(result.get("attempt_id") or "").strip():
            raise GateWatcherError(
                "dispatch_result_invalid",
                "dispatch() returned no attempt_id; reconcile against the launcher "
                "journal before the next beat",
                _FIX_DISPATCHER,
            )
        return result


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def evaluate_gate(
    gate: Mapping[str, Any],
    *,
    world: Any,
    now: Any = None,
    slices: Mapping[str, Mapping[str, Any]] | None = None,
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
    slice_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """One gate against live state: ``cleared``, ``waiting``, ``stale`` or
    ``owner_required``, plus the evidence for the transition record.

    The world is wrapped on each call, so a raw adapter can be passed. An
    unprovable state is ``waiting``: unknown never implies success.
    """

    observed = world if isinstance(world, World) else World(world)
    spec = _normalise_gate(gate)
    kind = spec["type"]
    if kind == "none":
        return "cleared", {}
    if kind == "pr_merged":
        state = observed.pr_state(spec["pr"])
        if state == "merged":
            return "cleared", {"pr": spec["pr"], "state": "merged"}
        if state == "closed":
            return (
                "stale",
                {"pr": spec["pr"], "state": "closed", "reason": "pr_closed_unmerged"},
            )
        return "waiting", {"pr": spec["pr"], "state": state}
    if kind == "attempt_succeeded":
        if "attempt" in spec:
            ref = spec["attempt"]
            probe = observed.attempt_state(ref)
            evidence: dict[str, Any] = {"attempt": ref}
        elif "slice" in spec:
            record = (slices or {}).get(spec["slice"])
            attempt_id = str((record or {}).get("attempt_id") or "")
            if not attempt_id:
                return "waiting", {"slice": spec["slice"], "state": "not_dispatched"}
            ref = attempt_id
            probe = observed.attempt_state(ref)
            evidence = {"slice": spec["slice"], "attempt": ref}
        else:
            ref = spec["fact"]
            probe = observed.fact_state(ref)
            evidence = {"fact": ref}
        if probe in SUCCESS_ATTEMPT_STATES:
            return "cleared", {**evidence, "state": probe}
        if probe in FAILED_ATTEMPT_STATES:
            return (
                "stale",
                {
                    **evidence,
                    "state": probe,
                    "reason": "fact_failed" if "fact" in evidence else "attempt_failed",
                },
            )
        return "waiting", {**evidence, "state": probe}
    if kind == "date":
        moment = _epoch(now)
        on = spec["on"]
        if moment >= _epoch(on):
            return "cleared", {"on": on, "now": iso(now)}
        return "waiting", {"on": on, "now": iso(now)}
    if kind == "owner_decision":
        decision = (decisions or {}).get(str(slice_id or ""))
        if decision and str(decision.get("decision") or "") == "approve":
            return (
                "cleared",
                {
                    "type": kind,
                    "decision": "approve",
                    "by": str(decision.get("by") or ""),
                    "at": str(decision.get("at") or ""),
                },
            )
        if decision and str(decision.get("decision") or "") == "decline":
            return (
                "stale",
                {"type": kind, "decision": "decline", "reason": "owner_declined"},
            )
        evidence = {"type": kind}
        if spec.get("question"):
            evidence["question"] = spec["question"]
        return "owner_required", evidence
    raise GateWatcherError("gate_invalid", f"gate type {kind!r} is not one of {GATE_TYPES}")


def _gate_evidence(gate: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": str(gate.get("type") or ""), **dict(evidence)}


def _dependency_state(
    dep_id: str,
    slices_by_id: Mapping[str, Mapping[str, Any]],
    observed: World,
) -> tuple[str, dict[str, Any]]:
    """One ``depends_on`` edge: cleared when the dependency's attempt landed.

    A dependency that has never been dispatched, or whose attempt is still in
    flight, keeps the dependent waiting; a failed dependency attempt is stale
    (the dependent can never proceed on that edge until it is re-pointed).
    """

    record = slices_by_id.get(dep_id)
    if record is None:
        return "waiting", {"dep": dep_id, "state": "unknown_slice"}
    if record.get("status") != "dispatched":
        return "waiting", {"dep": dep_id, "state": str(record.get("status") or "planned")}
    attempt_id = str(record.get("attempt_id") or "")
    if not attempt_id:
        return "waiting", {"dep": dep_id, "state": "no_attempt"}
    probe = observed.attempt_state(attempt_id)
    evidence: dict[str, Any] = {"dep": dep_id, "attempt": attempt_id, "state": probe}
    if probe in SUCCESS_ATTEMPT_STATES:
        return "cleared", evidence
    if probe in FAILED_ATTEMPT_STATES:
        return "stale", {**evidence, "reason": "dependency_failed"}
    return "waiting", evidence


def _evaluate_slice(
    record: Mapping[str, Any],
    slices_by_id: Mapping[str, Mapping[str, Any]],
    observed: World,
    *,
    now: Any,
    decisions: Mapping[str, Mapping[str, Any]],
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[tuple[Mapping[str, Any], str, dict[str, Any]]]]:
    """Every declared condition for one slice, in declaration order.

    Returns the combined ``(source, state, evidence)`` list — dependencies
    first, then gates — and the gate-only view used for transition evidence.
    """

    states: list[tuple[str, str, dict[str, Any]]] = []
    for dep_id in record["depends_on"]:
        state, evidence = _dependency_state(dep_id, slices_by_id, observed)
        states.append(("depends_on", state, evidence))
    gates: list[tuple[Mapping[str, Any], str, dict[str, Any]]] = []
    for gate in record["gates"]:
        state, evidence = evaluate_gate(
            gate,
            world=observed,
            now=now,
            slices=slices_by_id,
            decisions=decisions,
            slice_id=str(record["id"]),
        )
        states.append((str(gate.get("type") or ""), state, evidence))
        gates.append((gate, state, evidence))
    return states, gates


def _combine(
    states: Sequence[tuple[str, str, dict[str, Any]]],
) -> tuple[str, dict[str, Any], str]:
    """Deps-then-gates combination: any stale blocks, any owner requirement routes."""

    for source, state, evidence in states:
        if state == "stale":
            return "stale", dict(evidence), source
    for source, state, evidence in states:
        if state == "owner_required":
            return "owner_required", dict(evidence), source
    if states and all(state == "cleared" for _, state, _ in states):
        return "cleared", {}, ""
    return "waiting", {}, ""


_STALE_REASONS: dict[str, str] = {
    "pr_merged": "pr_closed_unmerged",
    "attempt_succeeded": "attempt_failed",
    "owner_decision": "owner_declined",
    "depends_on": "dependency_failed",
}


def _record_transition(
    record: dict[str, Any],
    document: dict[str, Any],
    *,
    to: str,
    reason: str,
    evidence: Mapping[str, Any],
    stamp: str,
) -> None:
    entry = {
        "from": str(record["status"]),
        "to": str(to),
        "at": stamp,
        "reason": reason,
        "evidence": dict(evidence),
    }
    record["transitions"].append(entry)
    record["updated_at"] = stamp
    document["transitions"].append({"slice": record["id"], **entry})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def seed_backlog(
    root: Any,
    *,
    slices: Iterable[Mapping[str, Any]] | None = None,
    budget: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Create the backlog once with the known planned slices; never overwrite.

    ``slices`` defaults to :data:`SEED_SLICES` (the campaign record's current
    planned slices with their real gates). An existing backlog is refused:
    seeding is create-only, exactly like scheduler intake.
    """

    validated = _budget(budget)
    stamp = iso(now)
    normalised = [_normalise_slice(record) for record in (slices if slices is not None else SEED_SLICES)]
    ids = [item["id"] for item in normalised]
    if len(ids) != len(set(ids)):
        raise GateWatcherError("slice_invalid", "the backlog may not seed a duplicate slice id")
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "budget": validated,
        "slices": sorted(normalised, key=lambda item: item["id"]),
        "transitions": [],
        "decisions": [],
        "revision": 1,
        "created_at": stamp,
        "updated_at": stamp,
    }
    with _locked(root):
        if backlog_path(root).exists():
            raise GateWatcherError(
                "backlog_exists",
                f"gate backlog {BACKLOG_DIR}/{BACKLOG_FILE} already exists",
                "Adopt the existing backlog; add slices with add_slice instead.",
            )
        _write_json(backlog_path(root), document)
    return document


def load_backlog(root: Any) -> dict[str, Any]:
    """The durable backlog document, validated and normalised."""

    return _read_document(root)


def add_slice(root: Any, record: Mapping[str, Any], *, now: Any = None) -> dict[str, Any]:
    """Create one planned slice; a replay of identical intent is a no-op.

    A different slice at the same id is refused: the backlog is create-only
    and never silently replaces planned intent.
    """

    incoming = _normalise_slice({**record, "status": "planned"})
    stamp = iso(now)
    incoming["created_at"] = stamp
    incoming["updated_at"] = stamp
    with _locked(root):
        document = _read_document(root)
        for existing in document["slices"]:
            if existing["id"] == incoming["id"]:
                if _same_intent(existing, incoming):
                    return dict(existing)
                raise GateWatcherError(
                    "slice_exists",
                    f"slice {incoming['id']!r} already exists with different content",
                    "Choose a new slice id; backlog slices are create-only.",
                )
        document["slices"].append(incoming)
        _persist(root, document, stamp=stamp)
    return dict(incoming)


def record_owner_decision(
    root: Any,
    slice_id: Any,
    *,
    decision: Any,
    by: Any,
    note: str = "",
    now: Any = None,
) -> dict[str, Any]:
    """Record one owner decision in the decision queue (append-only).

    ``approve`` lets the next beat clear the slice's owner gate; ``decline``
    is terminal for that slice id. The queue is the backlog's own
    ``decisions`` log: one durable place the owner writes and the watcher
    reads, so a decision is never a chat message.
    """

    value = str(decision or "").strip().lower()
    if value not in DECISIONS:
        raise GateWatcherError(
            "decision_invalid",
            f"decision {decision!r} is not one of {DECISIONS}",
            "Record 'approve' or 'decline'.",
        )
    entry = {
        "slice_id": _safe_id(slice_id, "slice_id"),
        "decision": value,
        "by": _text(by, "decision by"),
        "note": str(note or ""),
        "at": iso(now),
    }
    with _locked(root):
        document = _read_document(root)
        if not any(item["id"] == entry["slice_id"] for item in document["slices"]):
            raise GateWatcherError(
                "slice_not_found",
                f"no backlog slice {entry['slice_id']!r}",
                "List the backlog and use an exact slice id.",
            )
        document["decisions"].append(entry)
        _persist(root, document, stamp=entry["at"])
    return dict(entry)


def _dispatch_order(record: Mapping[str, Any]) -> tuple[int, str, str]:
    return (
        int(record.get("priority") or 5),
        str(record.get("created_at") or ""),
        str(record.get("id") or ""),
    )


def _lane_of(record: Mapping[str, Any]) -> str:
    return str(record.get("lane") or "")


def _assigned_files(record: Mapping[str, Any]) -> set[str]:
    return {
        _lease_key(path)
        for path in (record.get("brief") or {}).get("assigned_files") or ()
    }


def run_beat(
    root: Any,
    *,
    world: Any,
    dispatcher: Any,
    now: Any = None,
    max_lanes: int | None = None,
    max_bees: int | None = None,
) -> dict[str, Any]:
    """One gate-watcher beat: evaluate every planned slice, then dispatch.

    The beat is the first-class duty on the coordinator's cadence (the same
    tick that drives the completed-attempt sweep). It never waits for
    coordinator attention: gates that clear move their slices; owner gates
    route to the decision queue; stale gates block with a reason; the lane
    budget, the bee cap and the lease rules bound every dispatch. A dispatched
    slice is marked and never re-dispatched; a beat against unchanged state
    writes nothing.
    """

    budget = _budget(
        {
            "lanes": MAX_LANES if max_lanes is None else int(max_lanes),
            "bees": MAX_BEES if max_bees is None else int(max_bees),
        }
    )
    observed = World(world)
    sender = _Dispatcher(dispatcher)
    stamp = iso(now)
    with _locked(root):
        document = _read_document(root)
        before = _snapshot(document)
        slices_by_id = {item["id"]: item for item in document["slices"]}
        decisions = _decision_map(document)
        report: dict[str, Any] = {
            "schema": SCHEMA,
            "at": stamp,
            "evaluated": 0,
            "queued": [],
            "dispatched": [],
            "routed_owner": [],
            "owner_pending": [],
            "declined": [],
            "stale": [],
            "waiting": [],
            "skipped_budget": [],
            "lease_skipped": [],
            "dispatch_errors": [],
            "failed_attempts": [],
            "budget": {"lanes": budget["lanes"], "bees": budget["bees"]},
        }

        # Phase 0: decision-queue consumption. An approval moves a blocked-owner
        # slice back into the mechanical flow; a decline stays terminal.
        for record in document["slices"]:
            if record["status"] != "blocked-owner":
                continue
            decision = decisions.get(record["id"])
            if not decision:
                report["owner_pending"].append(record["id"])
                continue
            if decision.get("decision") == "decline":
                report["declined"].append(record["id"])
                continue
            states, gates = _evaluate_slice(
                record, slices_by_id, observed, now=now, decisions=decisions
            )
            combined, _, _ = _combine(states)
            decision_evidence = {
                "decision": "approve",
                "by": str(decision.get("by") or ""),
                "at": str(decision.get("at") or ""),
            }
            if combined == "cleared":
                _record_transition(
                    record,
                    document,
                    to="queued",
                    reason="owner_approved",
                    evidence={
                        **decision_evidence,
                        "gates": [_gate_evidence(gate, item) for gate, _, item in gates],
                    },
                    stamp=stamp,
                )
                record["status"] = "queued"
                record["queued_at"] = stamp
                report["queued"].append(record["id"])
            else:
                _record_transition(
                    record,
                    document,
                    to="planned",
                    reason="owner_approved_gates_pending",
                    evidence={**decision_evidence},
                    stamp=stamp,
                )
                record["status"] = "planned"
                record["owner_request"] = None

        # Phase 1: evaluate every planned slice's declared conditions.
        eligible: list[tuple[dict[str, Any], list[tuple[Mapping[str, Any], str, dict[str, Any]]]]] = []
        for record in document["slices"]:
            if record["status"] != "planned":
                continue
            report["evaluated"] += 1
            states, gates = _evaluate_slice(
                record, slices_by_id, observed, now=now, decisions=decisions
            )
            combined, evidence, source = _combine(states)
            if combined == "stale":
                blocked = {
                    "reason": str(evidence.get("reason") or _STALE_REASONS.get(source, "gate_stale")),
                    "gate": source,
                    "evidence": dict(evidence),
                    "at": stamp,
                }
                if record.get("blocked") != blocked:
                    record["blocked"] = blocked
                    record["updated_at"] = stamp
                report["stale"].append(
                    {"id": record["id"], "reason": blocked["reason"], "gate": source}
                )
                continue
            if record.get("blocked") is not None:
                record["blocked"] = None
                record["updated_at"] = stamp
            if combined == "owner_required":
                request = {
                    "question": str(
                        next(
                            (
                                gate.get("question") or ""
                                for gate in record["gates"]
                                if gate.get("type") == "owner_decision"
                            ),
                            "",
                        )
                    ),
                    "requested_at": stamp,
                }
                if record.get("owner_request") != request or record["status"] != "blocked-owner":
                    record["owner_request"] = request
                    _record_transition(
                        record,
                        document,
                        to="blocked-owner",
                        reason="owner_gate",
                        evidence={
                            "gates": [_gate_evidence(gate, item) for gate, _, item in gates]
                        },
                        stamp=stamp,
                    )
                    record["status"] = "blocked-owner"
                    report["routed_owner"].append(record["id"])
                continue
            if combined == "cleared":
                eligible.append((record, gates))
            else:
                report["waiting"].append(record["id"])

        # Phase 2a: queue every eligible slice whose lease is free. A slice's
        # own lease never conflicts with itself, but two eligible slices may
        # not queue onto the same files: the first to queue holds them.
        world_files: set[str] = set()
        for files in observed.active_bee_leases().values():
            world_files.update(files)
        committed: dict[str, set[str]] = {
            record["id"]: _assigned_files(record)
            for record in document["slices"]
            if record["status"] in ("queued", "dispatched")
        }

        def _lease_free(record: Mapping[str, Any]) -> bool:
            wanted = _assigned_files(record)
            if wanted & world_files:
                return False
            for owner, files in committed.items():
                if owner != record["id"] and wanted & files:
                    return False
            return True

        for record, gates in sorted(eligible, key=lambda pair: _dispatch_order(pair[0])):
            if not _lease_free(record):
                report["lease_skipped"].append({"id": record["id"], "reason": "lease_conflict"})
                continue
            evidence: dict[str, Any] = {
                "gates": [_gate_evidence(gate, item) for gate, _, item in gates]
            }
            states, _ = _evaluate_slice(
                record, slices_by_id, observed, now=now, decisions=decisions
            )
            dep_evidence = [
                dict(item) for source, _, item in states if source == "depends_on"
            ]
            if dep_evidence:
                evidence["deps"] = dep_evidence
            _record_transition(
                record,
                document,
                to="queued",
                reason="gates_cleared",
                evidence=evidence,
                stamp=stamp,
            )
            record["status"] = "queued"
            record["queued_at"] = stamp
            report["queued"].append(record["id"])
            committed[str(record["id"])] = _assigned_files(record)

        # Phase 2b: dispatch queued slices within the lane budget, the bee cap
        # and the lease rules. A lane is in flight once a slice on it is
        # dispatched; several queued slices may share one lane's slot, bounded
        # by the bee cap. Each dispatch is persisted before the next one.
        in_flight = observed.active_bee_count()
        active_lanes = {
            _lane_of(record)
            for record in document["slices"]
            if record["status"] == "dispatched"
        }
        for record in sorted(
            [item for item in document["slices"] if item["status"] == "queued"],
            key=_dispatch_order,
        ):
            lane = _lane_of(record)
            if lane not in active_lanes and len(active_lanes) >= budget["lanes"]:
                report["skipped_budget"].append({"id": record["id"], "reason": "lane_budget"})
                continue
            if in_flight >= budget["bees"]:
                report["skipped_budget"].append({"id": record["id"], "reason": "bee_cap"})
                continue
            if not _lease_free(record):
                report["lease_skipped"].append({"id": record["id"], "reason": "lease_conflict"})
                continue
            try:
                result = sender.dispatch(
                    {
                        "brief": dict(record["brief"]),
                        "project": record["project"],
                        "kanban_task_id": record["id"],
                    }
                )
            except GateWatcherError as exc:
                if exc.code == "dispatch_result_invalid":
                    raise
                record["last_dispatch_error"] = {
                    "code": exc.code,
                    "message": exc.message,
                    "at": stamp,
                }
                record["updated_at"] = stamp
                report["dispatch_errors"].append({"id": record["id"], "code": exc.code})
                continue
            attempt_id = str(result.get("attempt_id") or "")
            _record_transition(
                record,
                document,
                to="dispatched",
                reason="dispatched",
                evidence={"attempt_id": attempt_id, "kanban_task_id": record["id"]},
                stamp=stamp,
            )
            record["status"] = "dispatched"
            record["attempt_id"] = attempt_id
            record["dispatched_at"] = stamp
            record["last_dispatch_error"] = None
            active_lanes.add(lane)
            in_flight += 1
            report["dispatched"].append({"id": record["id"], "attempt_id": attempt_id})
            _persist(root, document, stamp=stamp)

        for record in document["slices"]:
            if record["status"] == "dispatched" and record.get("attempt_id"):
                probe = observed.attempt_state(str(record["attempt_id"]))
                if probe in FAILED_ATTEMPT_STATES:
                    report["failed_attempts"].append(
                        {
                            "id": record["id"],
                            "attempt_id": record["attempt_id"],
                            "state": probe,
                        }
                    )

        changed = _snapshot(document) != before
        report["changed"] = changed
        report["budget"]["active_lanes"] = len(active_lanes)
        report["budget"]["active_bees"] = in_flight
        if changed:
            _persist(root, document, stamp=stamp)
        return report


__all__ = [
    "SCHEMA",
    "BACKLOG_DIR",
    "BACKLOG_FILE",
    "STATUSES",
    "GATE_TYPES",
    "MIN_LANES",
    "MAX_LANES",
    "MAX_BEES",
    "SUCCESS_ATTEMPT_STATES",
    "FAILED_ATTEMPT_STATES",
    "DECISIONS",
    "SEED_SLICES",
    "GateWatcherError",
    "World",
    "default_root",
    "backlog_path",
    "iso",
    "evaluate_gate",
    "seed_backlog",
    "load_backlog",
    "add_slice",
    "record_owner_decision",
    "run_beat",
]
