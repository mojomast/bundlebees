"""One crash-safe reconciler tick for durable jobs.

The reconciler advances a job by exactly one tick at a time. It never treats the
launcher journal as a truth claim, and it never resolves an uncertain dispatch by
dispatching again:

* a request that provably never left the process (``DispatchNotSent``) is retried
  on the next tick;
* a request whose outcome is unknown (``DispatchUncertain``) is reconciled
  against the journal — one *strict* match adopts, two escalate, none waits out a
  grace period measured in **elapsed seconds** and then escalates;
* an intent left on disk by a crashed tick is never overwritten: it is treated as
  uncertainty until the journal proves otherwise;
* ``exactly_once`` is claimed only when the launcher itself enforces idempotency.

State changes go through :mod:`bundlebees_plugin.job`, so every move is signed,
persisted and attributable. The launcher is an injected adapter with exactly the
four members named in :class:`Launcher`; anything else fails closed rather than
being guessed at.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback keeps O_EXCL
    fcntl = None  # type: ignore[assignment]
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import job as jobs
from .brief import BriefError, validate_brief

DEFAULT_GRACE_S = 60.0
DEFAULT_MAX_UNCERTAIN_TICKS = 5
DEFAULT_STALE_LOCK_S = 300.0

INTENT_DISPATCHING = "dispatching"
INTENT_DISPATCHED = "dispatched"
INTENT_NOT_SENT = "not_sent"
INTENT_UNCERTAIN = "uncertain"
INTENT_ESCALATED = "escalated"

EVIDENCE_PROVEN_PRESENT = "proven_present"
EVIDENCE_ADOPTED = "adopted"

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX_LAUNCHER = (
    "Inject a launcher exposing list_attempts(), get_attempt(id), post_attempt(brief) "
    "and a bool idempotent property."
)


class DispatchNotSent(Exception):
    """The request provably never left this process."""


class DispatchUncertain(Exception):
    """The request may or may not have reached the launcher."""


class DispatchRejected(Exception):
    """The launcher refused the brief."""


class ReconcileError(Exception):
    """The tick could not proceed; nothing was dispatched."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


class Launcher(Protocol):
    """Exactly the contract's launcher surface — no aliases, no guessing."""

    idempotent: bool

    def list_attempts(self) -> Sequence[Mapping[str, Any]]: ...

    def get_attempt(self, attempt_id: str) -> Mapping[str, Any] | None: ...

    def post_attempt(self, brief: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class TickResult:
    status: str
    transitions: tuple[str, ...] = ()
    action: str | None = None
    detail: str = ""
    exactly_once: bool = False
    evidence: str = ""


# ---------------------------------------------------------------------------
# Launcher adapter — fail closed on anything unexpected
# ---------------------------------------------------------------------------


class _Adapter:
    __slots__ = ("_launcher", "idempotent")

    def __init__(self, launcher: Any) -> None:
        if launcher is None:
            raise ReconcileError("launcher_incompatible", "no launcher was supplied", _FIX_LAUNCHER)
        missing = [
            name
            for name in ("list_attempts", "get_attempt", "post_attempt")
            if not callable(getattr(launcher, name, None))
        ]
        if missing:
            raise ReconcileError(
                "launcher_incompatible",
                "the launcher is missing " + ", ".join(missing),
                _FIX_LAUNCHER,
            )
        flag = getattr(launcher, "idempotent", None)
        if not isinstance(flag, bool):
            raise ReconcileError(
                "launcher_incompatible",
                "launcher.idempotent must be a bool, not " + type(flag).__name__,
                _FIX_LAUNCHER,
            )
        self._launcher = launcher
        self.idempotent = flag

    def list_attempts(self) -> list[Mapping[str, Any]]:
        entries = self._launcher.list_attempts()
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            raise ReconcileError(
                "launcher_incompatible",
                "list_attempts() must return a sequence of attempts",
                _FIX_LAUNCHER,
            )
        return [entry for entry in entries if isinstance(entry, Mapping)]

    def get_attempt(self, attempt_id: str) -> Mapping[str, Any] | None:
        entry = self._launcher.get_attempt(attempt_id)
        if entry is not None and not isinstance(entry, Mapping):
            raise ReconcileError(
                "launcher_incompatible",
                "get_attempt() must return an attempt or None",
                _FIX_LAUNCHER,
            )
        return entry

    def post_attempt(self, brief: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._launcher.post_attempt(brief)
        if not isinstance(result, Mapping):
            raise ReconcileError(
                "launcher_incompatible",
                "post_attempt() must return an attempt mapping",
                _FIX_LAUNCHER,
            )
        return result


# ---------------------------------------------------------------------------
# Identifiers and paths
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str) -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise ReconcileError(
            "identifier_invalid",
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 96 characters.",
        )
    return text


def intent_path(state_dir: Path, job_id: str, task_id: str, attempt_no: int) -> Path:
    return (
        Path(state_dir)
        / "jobs"
        / _safe_id(job_id, "job_id")
        / "intents"
        / f"{_safe_id(task_id, 'task_id')}-{int(attempt_no)}.json"
    )


def lock_path(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / "jobs" / f"{_safe_id(job_id, 'job_id')}.lock"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_intent(path: Path) -> dict[str, Any] | None:
    """Read an intent, refusing to guess. A corrupt intent is not permission."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconcileError(
            "intent_unreadable",
            f"intent {path.name} cannot be read",
            "Inspect it by hand; do not dispatch until it is understood.",
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReconcileError(
            "intent_corrupt",
            f"intent {path.name} is not valid JSON",
            "Inspect it by hand; a corrupt intent is never permission to POST again.",
        ) from exc
    if not isinstance(data, dict):
        raise ReconcileError(
            "intent_corrupt",
            f"intent {path.name} is not an object",
            "Inspect it by hand; a corrupt intent is never permission to POST again.",
        )
    return data


# ---------------------------------------------------------------------------
# Single-writer lock, with safe recovery from a killed holder
# ---------------------------------------------------------------------------


class _Lock:
    """A job lock held by the kernel, not by file existence.

    ``flock`` is released automatically when the process dies, so a crashed tick
    cannot wedge the job and no heuristic steal is needed. The file itself is
    never unlinked: deleting it would let a later process create a *new* inode
    while another still holds the old one, which is how two writers appear.
    """

    def __init__(self, fd: int, path: Path, note: str | None = None) -> None:
        self.fd = fd
        self.path = path
        self.held = True
        self.stolen = note

    def release(self) -> None:
        if not self.held:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.held = False


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


def _acquire_lock(
    state_dir: Path,
    job_id: str,
    clock: float,
    stale_after_s: float = DEFAULT_STALE_LOCK_S,
) -> _Lock | None:
    """Take the job lock, or return None when another live process owns it.

    Ownership is kernel-held: a lock is unavailable exactly while some process
    holds it, regardless of how old the file looks. ``stale_after_s`` is kept for
    reporting only — age is never grounds to take a lock from a live holder.
    """

    path = lock_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    if fcntl is None:  # pragma: no cover - non-POSIX fallback
        return None if path.exists() else _exclusive_lock(path, clock)

    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None

    previous = ""
    try:
        previous = os.pread(fd, 4096, 0).decode("utf-8", "replace").strip()
    except OSError:
        previous = ""

    payload = json.dumps({"pid": os.getpid(), "at": _iso(clock)}).encode("utf-8")
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, payload, 0)
        os.fsync(fd)
    except OSError:
        pass

    note = None
    if previous:
        try:
            old = json.loads(previous)
        except ValueError:
            old = {}
        if old and old.get("pid") != os.getpid():
            note = f"recovered lock last held by pid {old.get('pid')}"
    return _Lock(fd, path, note)


def _exclusive_lock(path: Path, clock: float) -> _Lock | None:  # pragma: no cover
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    with os.fdopen(fd, "wb") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "at": _iso(clock)}).encode())
    fd2 = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    return _Lock(fd2, path)


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------


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
        raise ReconcileError(
            "clock_invalid",
            "now must be epoch seconds, an ISO timestamp or a callable",
            "Pass a clock the reconciler can read.",
        )
    return epoch


# ---------------------------------------------------------------------------
# Strict attempt identity
# ---------------------------------------------------------------------------


def _brief_of(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    brief = entry.get("brief")
    return brief if isinstance(brief, Mapping) else {}


def _fingerprint(job: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "objective": str(task.get("objective") or ""),
        "source_rev": str((job.get("goal") or {}).get("base_rev") or ""),
        "assigned_files": sorted(str(path) for path in (task.get("lease") or [])),
    }


def _matches(
    entry: Mapping[str, Any],
    fingerprint: Mapping[str, Any],
    written_at: float,
    *,
    job_id: str,
    task_id: str,
) -> bool:
    """Strict identity: an incomplete or foreign journal entry is never proof."""

    if not _attempt_id(entry):
        return False
    brief = _brief_of(entry)
    if not brief:
        return False

    if str(brief.get("job_id") or "") != job_id:
        return False
    if str(brief.get("task_id") or "") != task_id:
        return False

    objective = brief.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        return False
    if objective != fingerprint["objective"]:
        return False

    source_rev = brief.get("source_rev")
    if not isinstance(source_rev, str) or source_rev != fingerprint["source_rev"]:
        return False

    files = brief.get("assigned_files")
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return False
    if sorted(str(item) for item in files) != fingerprint["assigned_files"]:
        return False

    created = _epoch(entry.get("created_at"))
    if created is None:
        return False  # no trustworthy timestamp, no adoption
    if created + 1.0 < written_at:
        return False
    return True


def _attempt_id(entry: Mapping[str, Any]) -> str | None:
    for key in ("attempt_id", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _state_of(entry: Mapping[str, Any]) -> str:
    return str(entry.get("state") or entry.get("status") or "").strip().lower()


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


def tick(
    state_dir: Path,
    job_id: str,
    *,
    launcher: Any,
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    integrator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    now: Any = None,
    grace_s: float = DEFAULT_GRACE_S,
    max_uncertain_ticks: int = DEFAULT_MAX_UNCERTAIN_TICKS,
    stale_lock_s: float = DEFAULT_STALE_LOCK_S,
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TickResult:
    """Run one reconciler tick. See the module docstring for the rules."""

    if grace_s < 0:
        raise ReconcileError("grace_invalid", "grace_s must not be negative", "Pass a non-negative grace period.")
    job_id = _safe_id(job_id, "job_id")
    adapter = _Adapter(launcher)
    clock = _clock(now)

    lock = _acquire_lock(Path(state_dir), job_id, clock, stale_lock_s)
    if lock is None:
        return TickResult(status="locked", detail="another live tick holds the job lock")
    try:
        job = jobs.load_job(Path(state_dir), job_id)
        result = _run(
            Path(state_dir), job, adapter, clock, grace_s, max_uncertain_ticks, verifier, integrator, binder
        )
        if lock.stolen:
            result = TickResult(
                status=result.status,
                transitions=result.transitions,
                action=result.action,
                detail=(result.detail + f" [{lock.stolen}]").strip(),
                exactly_once=result.exactly_once,
                evidence=result.evidence,
            )
        return result
    finally:
        lock.release()


def _run(
    state_dir: Path,
    job: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    grace_s: float,
    max_uncertain_ticks: int,
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    integrator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TickResult:
    attempts = adapter.list_attempts()
    transitions: list[str] = []

    # 1. Uncertainty — including an intent a crashed tick left behind — is
    #    resolved before anything else can dispatch.
    for task in job["tasks"]:
        intent = _intent_for(state_dir, job, task)
        if intent is None:
            continue
        status = intent.get("status")
        # A confirmed intent whose job state was never saved (crash between the
        # confirmation write and the job save) is adoptable, not pending work.
        adoptable = status == INTENT_DISPATCHED and task.get("state") in {"pending", "dispatching"}
        if status not in {INTENT_UNCERTAIN, INTENT_DISPATCHING} and not adoptable:
            continue
        outcome = _reconcile_uncertain(
            state_dir, job, task, intent, attempts, adapter, clock, grace_s, max_uncertain_ticks, transitions, binder
        )
        if outcome is not None:
            return outcome

    # 2. Reap terminal attempts.
    changed = _reap(job, attempts, transitions)

    # 3. One heavy operation at most: integrate, else verify.
    integration = job.setdefault("integration", {"wave": 1, "state": "pending"})
    in_flight = {"dispatching", "dispatched", "running", "verifying"}
    wave_ready = (
        any(t.get("state") == "verified" for t in job["tasks"])
        and not any(t.get("state") in in_flight for t in job["tasks"])
    )
    if wave_ready and integrator is not None and not integration.get("terminal"):
        stalled = [t["task_id"] for t in job["tasks"] if t.get("state") in {"failed", "rejected"}]
        if stalled and not job.get("allow_partial_wave"):
            result = _stop_wave(
                state_dir,
                job,
                clock,
                "wave_incomplete",
                f"tasks {', '.join(stalled)} did not complete; refusing to integrate a partial wave",
            )
            jobs.save_job(state_dir, job)
            return result
        result = _integrate(state_dir, job, integrator, clock, transitions)
        jobs.save_job(state_dir, job)
        return result

    verifying = next((t for t in job["tasks"] if t.get("state") == "verifying"), None)
    if verifying is not None and verifier is not None:
        result = _verify(state_dir, job, verifying, verifier, transitions)
        jobs.save_job(state_dir, job)
        return result

    # 4. Dispatch ready tasks (a batch; the heavy-operation rule is about verify).
    if verifying is None:
        dispatched_before = len(transitions)
        outcome = _dispatch_ready(state_dir, job, adapter, clock, transitions, binder)
        if outcome is not None:
            jobs.save_job(state_dir, job)
            return outcome
        if len(transitions) > dispatched_before:
            jobs.save_job(state_dir, job)
            return TickResult(status="ok", transitions=tuple(transitions), action="dispatch")

    if changed or transitions:
        jobs.save_job(state_dir, job)
        return TickResult(status="ok", transitions=tuple(transitions), action="reap")
    return TickResult(status="idle", detail="nothing to do")


# --- uncertainty -----------------------------------------------------------


def _reconcile_uncertain(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    adapter: _Adapter,
    clock: float,
    grace_s: float,
    max_uncertain_ticks: int,
    transitions: list[str],
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TickResult | None:
    job_id = str(job["job_id"])
    task_id = str(task["task_id"])
    attempt_no = int(intent.get("attempt_no", 1))
    path = intent_path(state_dir, job_id, task_id, attempt_no)

    if intent.get("status") == INTENT_ESCALATED:
        return None

    if intent.get("status") == INTENT_DISPATCHED and intent.get("attempt_id"):
        entry = adapter.get_attempt(str(intent["attempt_id"]))
        if (
            entry is not None
            and _attempt_id(entry) == str(intent["attempt_id"])
            and _matches(
                entry,
                intent.get("fingerprint") or _fingerprint(job, task),
                float(intent.get("written_at") or 0.0),
                job_id=job_id,
                task_id=task_id,
            )
        ):
            attempt_id = str(intent["attempt_id"])
            task.setdefault("attempts", [])
            if attempt_id not in task["attempts"]:
                task["attempts"].append(attempt_id)
            task["intent"] = dict(intent)
            _bind(binder, attempt_id, _brief_for(job, task))
            _to_dispatched(job, task, "adopted confirmed intent after a crash")
            transitions.append(f"{task_id}->dispatched")
            jobs.save_job(state_dir, job)
            return TickResult(
                status="ok",
                transitions=tuple(transitions),
                action="adopt",
                detail=f"adopted {attempt_id} for {task_id} from the confirmed intent",
                exactly_once=adapter.idempotent,
                evidence=EVIDENCE_PROVEN_PRESENT if adapter.idempotent else EVIDENCE_ADOPTED,
            )
        intent["status"] = INTENT_UNCERTAIN
        intent.setdefault("first_uncertain_at", _iso(float(intent.get("written_at") or clock)))

    # An intent still marked "dispatching" is a crash window, not a retry
    # licence: the POST may have landed. Treat it as uncertain.
    if intent.get("status") == INTENT_DISPATCHING:
        intent["status"] = INTENT_UNCERTAIN
        intent.setdefault("first_uncertain_at", _iso(float(intent.get("written_at") or clock)))

    first = _epoch(intent.get("first_uncertain_at")) or clock
    elapsed = max(0.0, clock - first)
    fingerprint = intent.get("fingerprint") or _fingerprint(job, task)
    written_at = float(intent.get("written_at") or 0.0)
    matches = [
        entry
        for entry in attempts
        if _matches(entry, fingerprint, written_at, job_id=job_id, task_id=task_id)
    ]

    if len(matches) > 1:
        return _escalate(state_dir, job, task, intent, clock, "dispatch_ambiguous", f"{len(matches)} journal entries match task {task_id}; refusing to choose")

    if len(matches) == 1:
        attempt_id = _attempt_id(matches[0]) or ""
        task.setdefault("attempts", [])
        if attempt_id and attempt_id not in task["attempts"]:
            task["attempts"].append(attempt_id)
        intent.update(status=INTENT_DISPATCHED, attempt_id=attempt_id)
        task["intent"] = dict(intent)
        _bind(binder, attempt_id, _brief_for(job, task))
        _to_dispatched(job, task, "adopted after uncertain dispatch")
        transitions.append(f"{task_id}->dispatched")
        _write_json(path, intent)
        jobs.save_job(state_dir, job)
        return TickResult(
            status="ok",
            transitions=tuple(transitions),
            action="adopt",
            detail=f"adopted {attempt_id} for {task_id}",
            exactly_once=adapter.idempotent,
            evidence=EVIDENCE_PROVEN_PRESENT if adapter.idempotent else EVIDENCE_ADOPTED,
        )

    ticks = int(intent.get("uncertain_ticks") or 0) + 1
    intent["uncertain_ticks"] = ticks
    intent["first_uncertain_at"] = _iso(first)
    intent["last_checked_at"] = _iso(clock)

    if elapsed >= float(grace_s) or ticks > int(max_uncertain_ticks):
        reason = "grace elapsed" if elapsed >= float(grace_s) else "tick cap reached"
        detail = f"dispatch_uncertain_exhausted for {task_id} ({reason}, {elapsed:.0f}s of {grace_s:.0f}s, {ticks} ticks)"
        return _escalate(state_dir, job, task, intent, clock, "dispatch_uncertain_exhausted", detail)

    _write_json(path, intent)
    task["intent"] = dict(intent)
    jobs.save_job(state_dir, job)
    return TickResult(
        status="reconcile_wait",
        detail=f"{task_id}: dispatch uncertain, {elapsed:.0f}s of {grace_s:.0f}s grace elapsed, no journal match yet",
    )


def _escalate(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    intent: dict[str, Any],
    clock: float,
    code: str,
    detail: str,
) -> TickResult:
    """Persist the escalation once, and put the task beyond further dispatch."""

    job_id = str(job["job_id"])
    task_id = str(task["task_id"])
    path = intent_path(state_dir, job_id, task_id, int(intent.get("attempt_no", 1)))
    intent.update(status=INTENT_ESCALATED, escalated=code, escalated_at=_iso(clock))
    _write_json(path, intent)
    task["intent"] = dict(intent)

    if task.get("state") in {"pending", "dispatching", "dispatched", "running", "verifying"}:
        jobs.transition(job, task_id, "failed", actor="reconciler", reason=f"{code}: {detail}")
        transitions = [f"{task_id}->failed"]
    else:
        transitions = []

    job.setdefault("escalations", []).append(
        {"at": _iso(clock), "task_id": task_id, "code": code, "detail": detail}
    )
    jobs.save_job(state_dir, job)
    return TickResult(status="escalated", transitions=tuple(transitions), detail=detail, evidence=code)



def _bind(
    binder: Callable[[str, Mapping[str, Any]], None] | None,
    attempt_id: str,
    brief: Mapping[str, Any],
) -> None:
    """Persist the coordinator-signed lease. Failure aborts the tick."""

    if binder is None or not attempt_id:
        return
    try:
        binder(attempt_id, brief)
    except Exception as exc:  # noqa: BLE001 - any binding failure must stop the tick
        raise ReconcileError(
            "lease_binding_failed",
            f"the lease for {attempt_id} could not be recorded: {exc}",
            "Fix the lease state; an attempt without a lease must never be verified.",
        ) from exc


def _to_dispatched(job: dict[str, Any], task: dict[str, Any], reason: str) -> None:
    """Walk the legal transitions to ``dispatched`` from wherever the task is."""

    if task.get("state") == "pending":
        jobs.transition(job, str(task["task_id"]), "dispatching", actor="reconciler", reason=reason)
    if task.get("state") == "dispatching":
        jobs.transition(job, str(task["task_id"]), "dispatched", actor="reconciler", reason=reason)


# --- reap / verify / integrate --------------------------------------------


def _reap(job: dict[str, Any], attempts: Sequence[Mapping[str, Any]], transitions: list[str]) -> bool:
    by_id: dict[str, Mapping[str, Any]] = {}
    for entry in attempts:
        attempt_id = _attempt_id(entry)
        if attempt_id:
            by_id[attempt_id] = entry
    changed = False
    for task in job["tasks"]:
        attempt_id = (task.get("attempts") or [None])[-1]
        if not attempt_id or attempt_id not in by_id:
            continue
        state = _state_of(by_id[attempt_id])
        current = task.get("state")
        if state in {"succeeded", "completed", "done"} and current in {"dispatched", "running"}:
            if current == "dispatched":
                jobs.transition(job, str(task["task_id"]), "running", actor="reconciler", reason="worker started")
                transitions.append(f"{task['task_id']}->running")
            jobs.transition(job, str(task["task_id"]), "verifying", actor="reconciler", reason="worker finished")
            transitions.append(f"{task['task_id']}->verifying")
            changed = True
        elif state in {"failed", "cancelled", "canceled", "error"} and current in {"dispatched", "running"}:
            jobs.transition(job, str(task["task_id"]), "rejected", actor="reconciler", reason=f"worker ended {state}")
            transitions.append(f"{task['task_id']}->rejected")
            changed = True
    return changed


def _verdict(verdict: Any, what: str) -> tuple[bool, str]:
    """A passing verdict must say so literally, and must carry evidence."""

    if not isinstance(verdict, Mapping):
        raise ReconcileError(f"{what}_invalid", f"the {what} must return a mapping", f"Fix the {what}; a missing result is not success.")
    if "ok" not in verdict:
        raise ReconcileError(f"{what}_invalid", f"the {what} returned no 'ok' field", f"Fix the {what}; a missing verdict is not success.")
    ok = verdict.get("ok")
    if not isinstance(ok, bool):
        raise ReconcileError(
            f"{what}_invalid",
            f"the {what} returned a non-boolean ok ({type(ok).__name__})",
            "Return True or False, not a truthy value.",
        )
    evidence = verdict.get("evidence")
    if ok and (not isinstance(evidence, str) or not evidence.strip()):
        raise ReconcileError(
            f"{what}_invalid",
            f"the {what} passed without evidence",
            "A passing verdict must name what was checked.",
        )
    return ok, str(evidence or "").strip()


def _verify(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    transitions: list[str],
) -> TickResult:
    task_id = str(task["task_id"])
    attempt_id = (task.get("attempts") or [None])[-1]
    verdict = verifier({"attempt_id": attempt_id, "task": dict(task), "job_id": job["job_id"]})
    ok, evidence = _verdict(verdict, "verifier")
    if ok:
        jobs.transition(job, task_id, "verified", actor="reconciler", reason=evidence, evidence=evidence)
        transitions.append(f"{task_id}->verified")
        return TickResult(status="ok", transitions=tuple(transitions), action="verify", detail=f"{task_id} verified")
    jobs.transition(job, task_id, "rejected", actor="reconciler", reason=evidence or "verifier rejected", evidence=evidence)
    transitions.append(f"{task_id}->rejected")
    return TickResult(status="ok", transitions=tuple(transitions), action="verify", detail=f"{task_id} rejected")


def _stop_wave(state_dir: Path, job: dict[str, Any], clock: float, code: str, detail: str) -> TickResult:
    """End the wave once: terminal, recorded once, never re-claimed."""

    integration = job.setdefault("integration", {"wave": 1})
    if integration.get("terminal"):
        return TickResult(status="idle", detail="wave already stopped")
    integration.update(terminal=True, state="stopped", stopped=code, evidence=detail)
    job.setdefault("escalations", []).append(
        {"at": _iso(clock), "task_id": "integration", "code": code, "detail": detail}
    )
    return TickResult(status="escalated", detail=detail, evidence=code)


def _wave_number(integration: Mapping[str, Any]) -> int:
    """Accept the job schema's "w1" as well as a plain integer."""

    value = integration.get("wave", 1)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, value)
    text = str(value or "").strip().lower().lstrip("w")
    try:
        return max(1, int(text))
    except ValueError:
        return 1


def _integrate(
    state_dir: Path,
    job: dict[str, Any],
    integrator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    clock: float,
    transitions: list[str],
) -> TickResult:
    integration = job.setdefault("integration", {"wave": 1})
    if integration.get("terminal"):
        return TickResult(status="idle", detail="integration budget exhausted")

    verdict = integrator(dict(job))
    ok, evidence = _verdict(verdict, "integrator")
    integration["attempts"] = int(integration.get("attempts") or 0) + 1
    integration["evidence"] = evidence
    integration["state"] = "accepted" if ok else "failed"

    if ok:
        # Accept the wave, then open the next one so dependants can complete.
        for task in job["tasks"]:
            if task.get("state") == "verified":
                task_id = str(task["task_id"])
                jobs.transition(job, task_id, "integrating", actor="reconciler", reason="wave accepted")
                jobs.transition(job, task_id, "accepted", actor="reconciler", reason="wave accepted", evidence=evidence)
                transitions.append(f"{task_id}->accepted")
        wave = _wave_number(integration)
        accepted = list(integration.get("accepted_waves") or [])
        accepted.append({"wave": wave, "at": _iso(clock), "evidence": evidence})
        integration.update(
            accepted_waves=accepted,
            wave=wave + 1,
            attempts=0,
            state="pending",
        )
        transitions.append("integration->accepted")
        return TickResult(status="ok", transitions=tuple(transitions), action="integrate", detail=f"wave {wave} accepted")

    transitions.append("integration->failed")
    if int(integration["attempts"]) >= 2:
        integration["terminal"] = True
        job.setdefault("escalations", []).append(
            {
                "at": _iso(clock),
                "task_id": "integration",
                "code": "integration_failed",
                "detail": f"two integration attempts failed; last: {evidence}",
            }
        )
        return TickResult(
            status="escalated",
            transitions=tuple(transitions),
            action="integrate",
            detail="integration failed twice; budget exhausted",
            evidence="integration_failed",
        )
    return TickResult(
        status="ok",
        transitions=tuple(transitions),
        action="integrate",
        detail="integration failed; one retry remains",
        evidence="integration_failed",
    )


# --- dispatch --------------------------------------------------------------


def _dispatch_ready(
    state_dir: Path,
    job: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    transitions: list[str],
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TickResult | None:
    states = {t.get("task_id"): t.get("state") for t in job["tasks"]}
    for task in job["tasks"]:
        state = task.get("state")
        if state in {"pending", "dispatching"}:
            intent = _intent_for(state_dir, job, task)
            if intent is not None and intent.get("status") == INTENT_ESCALATED:
                continue
            if state == "dispatching" and (intent is None or intent.get("status") != INTENT_NOT_SENT):
                continue
        else:
            continue
        if any(states.get(dep) != "accepted" for dep in (task.get("depends_on") or [])):
            continue
        outcome = _dispatch_one(state_dir, job, task, adapter, clock, transitions, binder)
        if outcome is not None:
            return outcome
    return None


def _brief_for(job: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    """Build the production brief shape and validate it with the real schema.

    The signed lease is written by the dispatch path in ``tools.py`` once an
    attempt id exists; that binding is the M3 adapter's job, not this module's.
    """

    fingerprint = _fingerprint(job, task)
    contract = task.get("contract")
    if task.get("contract_sha256") and not isinstance(contract, Mapping):
        raise ReconcileError(
            "contract_missing",
            f"task {task['task_id']} declares a frozen contract that is not loaded",
            "Hydrate the contract from its path and verify its sha256 before dispatching.",
        )
    brief: dict[str, Any] = {
        "objective": fingerprint["objective"],
        "source_rev": fingerprint["source_rev"],
        "assigned_files": list(fingerprint["assigned_files"]),
        "acceptance": task.get("acceptance"),
        "job_id": str(job["job_id"]),
        "task_id": str(task["task_id"]),
    }
    if isinstance(contract, Mapping):
        brief["contract"] = dict(contract)
    binding = task.get("binding")
    if isinstance(binding, Mapping) and binding.get("project"):
        # The launcher dispatches into this project; without it the worker falls
        # back to a default, which is exactly the failure this binding prevents.
        brief["project"] = str(binding["project"])
    try:
        validate_brief(brief)
    except BriefError as exc:
        raise ReconcileError(
            "brief_invalid",
            f"task {task['task_id']}: {exc}",
            "Give the task an acceptance block (criteria/commands/tests); the launcher requires one.",
        ) from exc
    return brief


def _dispatch_one(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    transitions: list[str],
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> TickResult | None:
    job_id = str(job["job_id"])
    task_id = str(task["task_id"])
    attempt_no = len(task.get("attempts") or []) + 1
    path = intent_path(state_dir, job_id, task_id, attempt_no)
    existing = read_intent(path)

    if existing:
        status = existing.get("status")
        if status == INTENT_NOT_SENT:
            attempt_no = int(existing.get("attempt_no", attempt_no)) + 1
            path = intent_path(state_dir, job_id, task_id, attempt_no)
        else:
            # A persisted intent is never overwritten: the previous POST may
            # have landed. Reconcile it; do not issue a second one.
            return TickResult(
                status="reconcile_wait",
                detail=f"{task_id}: intent {status!r} already on disk; reconciling, not redispatching",
            )

    fingerprint = _fingerprint(job, task)
    intent = {
        "task_id": task_id,
        "attempt_no": attempt_no,
        "idempotency_key": hashlib.sha256(f"{job_id}:{task_id}:{attempt_no}".encode()).hexdigest()[:32],
        "status": INTENT_DISPATCHING,
        "written_at": clock,
        "first_uncertain_at": None,
        "uncertain_ticks": 0,
        "fingerprint": fingerprint,
    }
    brief = _brief_for(job, task)
    _write_json(path, intent)  # fsynced before the call

    if task.get("state") == "pending":
        jobs.transition(job, task_id, "dispatching", actor="reconciler", reason="dependencies accepted")
    try:
        created = adapter.post_attempt(brief)
    except DispatchNotSent as exc:
        intent.update(status=INTENT_NOT_SENT, detail=str(exc))
        _write_json(path, intent)
        task["intent"] = dict(intent)
        return TickResult(
            status="ok",
            transitions=tuple(transitions),
            action="dispatch",
            detail=f"{task_id}: not sent ({exc}); retry next tick",
        )
    except DispatchUncertain as exc:
        intent.update(status=INTENT_UNCERTAIN, first_uncertain_at=_iso(clock), uncertain_ticks=1, detail=str(exc))
        _write_json(path, intent)
        task["intent"] = dict(intent)
        return TickResult(
            status="reconcile_wait",
            detail=f"{task_id}: dispatch uncertain ({exc}); reconciling, not retrying",
        )
    except DispatchRejected as exc:
        intent.update(status="rejected", detail=str(exc))
        _write_json(path, intent)
        jobs.transition(job, task_id, "rejected", actor="reconciler", reason=f"launcher rejected the brief: {exc}")
        transitions.append(f"{task_id}->rejected")
        return TickResult(status="ok", transitions=tuple(transitions), action="dispatch", detail=f"{task_id} rejected by the launcher")

    attempt_id = _attempt_id(created) or ""
    _bind(binder, attempt_id, brief)
    intent.update(status=INTENT_DISPATCHED, attempt_id=attempt_id)
    _write_json(path, intent)
    task.setdefault("attempts", []).append(attempt_id)
    task["intent"] = dict(intent)
    jobs.transition(job, task_id, "dispatched", actor="reconciler", reason=f"attempt {attempt_id}")
    transitions.append(f"{task_id}->dispatched")
    return None


# --- intents ---------------------------------------------------------------


def _intent_for(state_dir: Path, job: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any] | None:
    recorded = task.get("intent")
    if isinstance(recorded, Mapping) and recorded.get("status"):
        return dict(recorded)
    task_id = str(task.get("task_id") or "")
    base = Path(state_dir) / "jobs" / str(job.get("job_id")) / "intents"
    if not task_id or not base.is_dir():
        return None
    for path in sorted(base.glob(f"{task_id}-*.json"), reverse=True):
        intent = read_intent(path)
        if intent is not None:
            return intent
    return None
