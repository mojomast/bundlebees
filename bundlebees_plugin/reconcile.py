"""One crash-safe reconciler tick for durable jobs.

The reconciler advances a job by exactly one tick at a time. It never treats the
launcher journal as a truth claim, and it never resolves an uncertain dispatch by
dispatching again:

* a request that provably never left the process (``DispatchNotSent``) is retried
  on the next tick;
* a request whose outcome is unknown (``DispatchUncertain``) is reconciled
  against the journal — one match adopts, two escalate, none waits out a grace
  period measured in **elapsed seconds** and then escalates;
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import job as jobs

DEFAULT_GRACE_S = 60.0
DEFAULT_MAX_UNCERTAIN_TICKS = 5

INTENT_DISPATCHING = "dispatching"
INTENT_DISPATCHED = "dispatched"
INTENT_NOT_SENT = "not_sent"
INTENT_UNCERTAIN = "uncertain"

EVIDENCE_PROVEN_PRESENT = "proven_present"
EVIDENCE_ADOPTED = "adopted"

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
# Filesystem helpers
# ---------------------------------------------------------------------------


def intent_path(state_dir: Path, job_id: str, task_id: str, attempt_no: int) -> Path:
    return Path(state_dir) / "jobs" / job_id / "intents" / f"{task_id}-{attempt_no}.json"


def lock_path(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / "jobs" / f"{job_id}.lock"


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def __enter__(self) -> "_Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        try:
            fd = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            return self
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "at": _iso(time.time())}))
        self.held = True
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


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
# Matching
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


def _matches(entry: Mapping[str, Any], fingerprint: Mapping[str, Any], written_at: float) -> bool:
    brief = _brief_of(entry)
    if brief:
        if str(brief.get("objective") or "") != fingerprint["objective"]:
            return False
        if str(brief.get("source_rev") or "") != fingerprint["source_rev"]:
            return False
        files = brief.get("assigned_files")
        if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
            if sorted(str(item) for item in files) != fingerprint["assigned_files"]:
                return False
    created = _epoch(entry.get("created_at"))
    if created is not None and created + 1.0 < written_at:
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
) -> TickResult:
    """Run one reconciler tick. See the module docstring for the rules."""

    if grace_s < 0:
        raise ReconcileError("grace_invalid", "grace_s must not be negative", "Pass a non-negative grace period.")
    adapter = _Adapter(launcher)
    clock = _clock(now)

    lock = _Lock(lock_path(Path(state_dir), job_id))
    with lock:
        if not lock.held:
            return TickResult(status="locked", detail="another tick holds the job lock")
        job = jobs.load_job(Path(state_dir), job_id)
        return _run(Path(state_dir), job, adapter, clock, grace_s, max_uncertain_ticks, verifier, integrator)


def _run(
    state_dir: Path,
    job: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    grace_s: float,
    max_uncertain_ticks: int,
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    integrator: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> TickResult:
    attempts = adapter.list_attempts()
    transitions: list[str] = []

    # 1. Uncertainty is resolved before anything else happens.
    for task in job["tasks"]:
        intent = _intent_for(state_dir, job, task)
        if intent is None or intent.get("status") != INTENT_UNCERTAIN:
            continue
        outcome = _reconcile_uncertain(
            state_dir, job, task, intent, attempts, adapter, clock, grace_s, max_uncertain_ticks, transitions
        )
        if outcome is not None:
            return outcome

    # 2. Reap terminal attempts.
    changed = _reap(job, attempts, transitions)

    # 3. One heavy operation at most: integrate, else verify.
    all_verified = bool(job["tasks"]) and all(t.get("state") == "verified" for t in job["tasks"])
    if all_verified and integrator is not None and (job.get("integration") or {}).get("state") != "accepted":
        result = _integrate(job, integrator, transitions)
        jobs.save_job(state_dir, job)
        return result

    verifying = next((t for t in job["tasks"] if t.get("state") == "verifying"), None)
    if verifying is not None and verifier is not None:
        result = _verify(job, verifying, verifier, transitions)
        jobs.save_job(state_dir, job)
        return result

    # 4. Dispatch ready tasks (a batch; the heavy-operation rule is about verify).
    if verifying is None:
        dispatched_before = len(transitions)
        outcome = _dispatch_ready(state_dir, job, adapter, clock, transitions)
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
) -> TickResult | None:
    job_id = str(job["job_id"])
    task_id = str(task["task_id"])
    attempt_no = int(intent.get("attempt_no", 1))
    path = intent_path(state_dir, job_id, task_id, attempt_no)
    first = _epoch(intent.get("first_uncertain_at")) or clock
    elapsed = max(0.0, clock - first)
    fingerprint = intent.get("fingerprint") or _fingerprint(job, task)
    written_at = float(intent.get("written_at") or 0.0)
    matches = [entry for entry in attempts if _matches(entry, fingerprint, written_at)]

    if len(matches) > 1:
        intent["escalated"] = "dispatch_ambiguous"
        _write_json(path, intent)
        return TickResult(
            status="escalated",
            detail=f"dispatch_ambiguous: {len(matches)} journal entries match task {task_id}; refusing to choose",
            evidence="dispatch_ambiguous",
        )

    if len(matches) == 1:
        attempt_id = _attempt_id(matches[0]) or str(intent.get("attempt_id") or "")
        task.setdefault("attempts", [])
        if attempt_id and attempt_id not in task["attempts"]:
            task["attempts"].append(attempt_id)
        intent.update(status=INTENT_DISPATCHED, attempt_id=attempt_id)
        task["intent"] = dict(intent)
        jobs.transition(job, task_id, "dispatched", actor="reconciler", reason="adopted after uncertain dispatch")
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
        intent["escalated"] = "dispatch_uncertain_exhausted"
        _write_json(path, intent)
        job.setdefault("events", []).append(
            {
                "at": _iso(clock),
                "task_id": task_id,
                "transition": "dispatch_uncertain_exhausted",
                "actor": "reconciler",
                "reason": reason,
            }
        )
        jobs.save_job(state_dir, job)
        return TickResult(
            status="escalated",
            detail=(
                f"dispatch_uncertain_exhausted for {task_id} "
                f"({reason}, {elapsed:.0f}s of {grace_s:.0f}s, {ticks} ticks)"
            ),
            evidence="dispatch_uncertain_exhausted",
        )

    _write_json(path, intent)
    task["intent"] = dict(intent)
    jobs.save_job(state_dir, job)
    return TickResult(
        status="reconcile_wait",
        detail=f"{task_id}: dispatch uncertain, {elapsed:.0f}s of {grace_s:.0f}s grace elapsed, no journal match yet",
    )


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
            jobs.transition(job, str(task["task_id"]), "running", actor="reconciler", reason="worker started")
            jobs.transition(job, str(task["task_id"]), "verifying", actor="reconciler", reason="worker finished")
            transitions.extend([f"{task['task_id']}->running", f"{task['task_id']}->verifying"])
            changed = True
        elif state in {"failed", "cancelled", "canceled", "error"} and current in {"dispatched", "running"}:
            jobs.transition(job, str(task["task_id"]), "rejected", actor="reconciler", reason=f"worker ended {state}")
            transitions.append(f"{task['task_id']}->rejected")
            changed = True
    return changed


def _verify(
    job: dict[str, Any],
    task: dict[str, Any],
    verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    transitions: list[str],
) -> TickResult:
    task_id = str(task["task_id"])
    attempt_id = (task.get("attempts") or [None])[-1]
    verdict = verifier({"attempt_id": attempt_id, "task": dict(task), "job_id": job["job_id"]})
    if not isinstance(verdict, Mapping) or "ok" not in verdict:
        raise ReconcileError(
            "verifier_invalid",
            "the verifier must return a mapping with 'ok'",
            "Fix the verifier; do not treat a missing verdict as success.",
        )
    evidence = str(verdict.get("evidence") or "")
    if verdict.get("ok"):
        jobs.transition(job, task_id, "verified", actor="reconciler", reason=evidence or "verifier green", evidence=evidence)
        transitions.append(f"{task_id}->verified")
        return TickResult(status="ok", transitions=tuple(transitions), action="verify", detail=f"{task_id} verified")
    jobs.transition(job, task_id, "rejected", actor="reconciler", reason=evidence or "verifier rejected", evidence=evidence)
    transitions.append(f"{task_id}->rejected")
    return TickResult(status="ok", transitions=tuple(transitions), action="verify", detail=f"{task_id} rejected")


def _integrate(
    job: dict[str, Any],
    integrator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    transitions: list[str],
) -> TickResult:
    verdict = integrator(dict(job))
    if not isinstance(verdict, Mapping) or "ok" not in verdict:
        raise ReconcileError(
            "integrator_invalid",
            "the integrator must return a mapping with 'ok'",
            "Fix the integrator; do not treat a missing verdict as success.",
        )
    integration = job.setdefault("integration", {"wave": "w1"})
    integration["state"] = "accepted" if verdict.get("ok") else "failed"
    integration["evidence"] = str(verdict.get("evidence") or "")
    transitions.append(f"integration->{integration['state']}")
    return TickResult(status="ok", transitions=tuple(transitions), action="integrate", detail=f"integration {integration['state']}")


# --- dispatch --------------------------------------------------------------


def _dispatch_ready(
    state_dir: Path,
    job: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    transitions: list[str],
) -> TickResult | None:
    states = {t.get("task_id"): t.get("state") for t in job["tasks"]}
    for task in job["tasks"]:
        state = task.get("state")
        # A task stuck in "dispatching" is only retryable when its intent proves
        # the request never left the process; anything else is uncertainty and is
        # handled before this point.
        if state == "dispatching":
            intent = _intent_for(state_dir, job, task)
            if intent is None or intent.get("status") != INTENT_NOT_SENT:
                continue
        elif state != "pending":
            continue
        if any(states.get(dep) != "accepted" for dep in (task.get("depends_on") or [])):
            continue
        outcome = _dispatch_one(state_dir, job, task, adapter, clock, transitions)
        if outcome is not None:
            return outcome
    return None


def _dispatch_one(
    state_dir: Path,
    job: dict[str, Any],
    task: dict[str, Any],
    adapter: _Adapter,
    clock: float,
    transitions: list[str],
) -> TickResult | None:
    job_id = str(job["job_id"])
    task_id = str(task["task_id"])
    attempt_no = len(task.get("attempts") or []) + 1
    path = intent_path(state_dir, job_id, task_id, attempt_no)
    existing = read_intent(path)
    if existing and existing.get("status") == INTENT_UNCERTAIN:
        return TickResult(status="reconcile_wait", detail=f"{task_id}: intent still uncertain")

    # Re-entering from "dispatching" after a not-sent intent: use a fresh
    # attempt number so the retry has its own intent record.
    if task.get("state") == "dispatching" and existing and existing.get("status") == INTENT_NOT_SENT:
        attempt_no = int(existing.get("attempt_no", 1)) + 1
        path = intent_path(state_dir, job_id, task_id, attempt_no)

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
    _write_json(path, intent)  # fsynced before the call

    brief = {
        "objective": fingerprint["objective"],
        "source_rev": fingerprint["source_rev"],
        "assigned_files": fingerprint["assigned_files"],
        "job_id": job_id,
        "task_id": task_id,
    }
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
