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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import agent_work as work_queue
from . import campaign as campaign_state
from . import git_observer
from . import job as jobs
from . import liveness
from . import queue as queue_mod
from . import recovery as recoveries
from . import work_scheduler
from .brief import BriefError, validate_brief
from .security import scrub

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
    #: The durable agent-work state for this job's outstanding request, if any.
    work_state: str = ""
    work_id: str = ""


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


_holder_alive = liveness.pid_alive


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


def reconcile_tool_attempts(state_dir: Path, backend: Any) -> dict[str, Any]:
    """Reconcile launcher-terminal attempts that have local coordinator leases."""

    from . import agent_consumer

    state_path = Path(state_dir)
    attempts_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in backend.attempts_raw():
        if not isinstance(entry, Mapping):
            continue
        attempt_id = _attempt_id(entry)
        if attempt_id:
            attempts_by_id[attempt_id] = entry

    reconciled: list[str] = []
    unchanged: list[str] = []
    lease_paths = sorted((state_path / "leases").glob("*.json"))
    for lease_path in lease_paths:
        attempt_id = lease_path.stem
        entry = attempts_by_id.get(attempt_id)
        launcher_state = _state_of(entry) if entry is not None else ""
        if launcher_state not in {"succeeded", "failed", "cancelled"}:
            unchanged.append(attempt_id)
            continue
        outcome = agent_consumer.record_tool_completion(
            state_path,
            attempt_id,
            state=launcher_state,
        )
        if outcome.get("emitted") is True:
            reconciled.append(attempt_id)
        else:
            unchanged.append(attempt_id)

    return {
        "checked": len(lease_paths),
        "reconciled": reconciled,
        "unchanged": unchanged,
    }


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
    accept_integration: Callable[[dict, Mapping[str, Any]], None] | None = None,
    now: Any = None,
    grace_s: float = DEFAULT_GRACE_S,
    max_uncertain_ticks: int = DEFAULT_MAX_UNCERTAIN_TICKS,
    stale_lock_s: float = DEFAULT_STALE_LOCK_S,
    binder: Callable[[str, Mapping[str, Any]], None] | None = None,
    git_evidence: Mapping[str, Any] | None = None,
) -> TickResult:
    """Run one reconciler tick. See the module docstring for the rules.

    ``git_evidence`` is an optional git-observer report (workflow-F3). When it
    proves the recorded PR's work is already integrated (``integrated_not_pr_merged``
    or ``pr_merged``) or otherwise disagrees with the recorded world, the tick
    never calls the integrator: a duplicate integration attempt is exactly the
    failure the evidence exists to prevent. Instead it returns a
    ``record_disposition`` action when the wait for the PR can end, or blocks
    for a human when the disagreement needs one.
    """

    if grace_s < 0:
        raise ReconcileError("grace_invalid", "grace_s must not be negative", "Pass a non-negative grace period.")
    job_id = _safe_id(job_id, "job_id")
    adapter = _Adapter(launcher)
    clock = _clock(now)
    decision = git_evidence_decision(git_evidence) if git_evidence is not None else None

    lock = _acquire_lock(Path(state_dir), job_id, clock, stale_lock_s)
    if lock is None:
        return TickResult(status="locked", detail="another live tick holds the job lock")
    try:
        # The native driver has already checked recovery with today's settings.
        # Do not re-adopt a different handoff here via a passive load.
        job = jobs.load_job(Path(state_dir), job_id, recover=accept_integration is None)
        # Durable work publication is an observer of the mechanical tick, never a
        # gate on it: a broken queue must not stop reconciliation from running.
        work_item: dict[str, Any] | None = None
        work_error = ""
        try:
            published = work_queue.publish_outstanding(Path(state_dir), job, now=clock)
            work_item = _work_item_for(Path(state_dir), job, published)
        except Exception as exc:  # noqa: BLE001 - publication must not block the tick
            work_error = str(getattr(exc, "code", None) or type(exc).__name__)
        result = _run(
            Path(state_dir), job, adapter, clock, grace_s, max_uncertain_ticks, verifier, integrator, binder, decision,
            accept_integration,
        )
        try:
            published = work_queue.publish_outstanding(Path(state_dir), job, now=clock)
            work_item = _work_item_for(Path(state_dir), job, published) or work_item
        except Exception as exc:  # noqa: BLE001 - a request created this tick may be unpublishable
            work_error = work_error or str(getattr(exc, "code", None) or type(exc).__name__)
        work_state = str((work_item or {}).get("state") or "")
        if not work_state and work_error:
            work_state = "publish_failed"
        if work_state or work_item:
            result = replace(
                result,
                work_state=work_state,
                work_id=str((work_item or {}).get("work_id") or ""),
            )
        if lock.stolen:
            result = TickResult(
                status=result.status,
                transitions=result.transitions,
                action=result.action,
                detail=(result.detail + f" [{lock.stolen}]").strip(),
                exactly_once=result.exactly_once,
                evidence=result.evidence,
                work_state=result.work_state,
                work_id=result.work_id,
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
    evidence_decision: Mapping[str, Any] | None = None,
    accept_integration: Callable[[dict, Mapping[str, Any]], None] | None = None,
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

    # 2. Recovery: a rejected task advances only under a validated decision.
    outcome, applied = _apply_recovery(state_dir, job, clock, transitions)
    if outcome is not None:
        jobs.save_job(state_dir, job)
        return outcome

    # 3. Reap terminal attempts.
    changed = _reap(state_dir, job, attempts, transitions, clock)

    # 4. One heavy operation at most: integrate, else verify.
    integration = job.setdefault("integration", {"wave": 1, "state": "pending"})
    in_flight = {"dispatching", "dispatched", "running", "verifying"}
    wave_ready = (
        any(t.get("state") == "verified" for t in job["tasks"])
        and not any(t.get("state") in in_flight for t in job["tasks"])
    )
    if wave_ready and integrator is not None and not integration.get("terminal"):
        if evidence_decision is not None and not evidence_decision.get("integration_permitted", True):
            # Git evidence says the world already disagrees with the checkpoint.
            # Integrating again would duplicate work that is provably done (or
            # build on a moved world), so the integrator is never called here.
            if changed or transitions:
                jobs.save_job(state_dir, job)
            evidence = str(evidence_decision.get("evidence") or "")
            reason = str(evidence_decision.get("reason") or "")
            if evidence_decision.get("stop_waiting"):
                return TickResult(
                    status="ok",
                    transitions=tuple(transitions),
                    action="record_disposition",
                    detail=f"git evidence {evidence}: {reason} — record the disposition and stop waiting",
                    evidence=evidence,
                )
            return TickResult(
                status="blocked",
                transitions=tuple(transitions),
                action="needs_human",
                detail=f"git evidence {evidence}: {reason}",
                evidence=evidence,
            )
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
        verdict = integrator(dict(job))
        ok, evidence = _verdict(verdict, "integrator")
        if ok and accept_integration is not None:
            # Native acceptance persists the proved result under its wave and ref
            # locks. Never follow it with a second, unlocked job save.
            wave = _wave_number(integration)
            pending = [t["task_id"] for t in job["tasks"] if t.get("state") in {"verified", "integrating"}]
            accept_integration(job, verdict)
            transitions.extend(f"{t}->accepted" for t in pending)
            transitions.append("integration->accepted")
            return TickResult(status="ok", transitions=tuple(transitions), action="integrate", detail=f"wave {wave} accepted")
        result = _integrate(job, verdict, ok, evidence, clock, transitions)
        jobs.save_job(state_dir, job)
        return result

    verifying = next((t for t in job["tasks"] if t.get("state") == "verifying"), None)
    if verifying is not None and verifier is not None:
        result = _verify(state_dir, job, verifying, verifier, transitions, clock)
        jobs.save_job(state_dir, job)
        return result

    # 5. Dispatch ready tasks (a batch; the heavy-operation rule is about verify).
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
        action = "recovery" if applied and not changed else "reap"
        return TickResult(status="ok", transitions=tuple(transitions), action=action)
    # Nothing else to do: say plainly that a rejection is waiting on a decision
    # rather than reporting a bare idle. No corrective attempt is dispatched.
    pending = _pending_recovery(state_dir, job)
    if pending is not None:
        failure = pending.get("failure") if isinstance(pending.get("failure"), Mapping) else {}
        return TickResult(
            status="awaiting_decision",
            action="recovery",
            detail=(
                f"{pending.get('task_id')}: {failure.get('class')}/{failure.get('code')} rejection is waiting "
                f"for a validated decision ({pending.get('request_id')}); no corrective attempt is dispatched"
            ),
            evidence=str(pending.get("request_id") or ""),
        )
    return TickResult(status="idle", detail="nothing to do")


def reconcile_tool_attempts(state_dir: Path, backend: Any) -> dict[str, Any]:
    """Sweep terminal tool-path launcher attempts into the local attempt store.

    The launcher is read once; matched rows whose state is ``succeeded``,
    ``failed`` or ``cancelled`` are written through the same
    :func:`agent_consumer.record_tool_completion` the polling path uses, so
    ``status`` stops reporting finished work as in flight. Queued, running and
    absent rows are left alone, and this never recreates an attempt.
    """

    from . import agent_consumer, leases

    state = Path(state_dir)
    try:
        paths = sorted((state / "leases").glob("*.json"))
    except OSError:
        paths = []
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            records.append(leases.load_lease(state, path.stem))
        except Exception:  # noqa: BLE001 - an unreadable lease is not a launcher row
            continue

    attempts = backend.attempts_raw()
    states: dict[str, str] = {}
    if isinstance(attempts, list):
        for row in attempts:
            if not isinstance(row, Mapping):
                continue
            attempt_id = str(row.get("attempt_id") or "").strip()
            if not attempt_id:
                continue
            states[attempt_id] = str(
                row.get("state") or row.get("status") or ""
            ).strip().lower()

    checked = 0
    reconciled: list[str] = []
    unchanged: list[str] = []
    for record in records:
        attempt_id = str(record.get("attempt_id") or "")
        if not attempt_id:
            continue
        checked += 1
        state_name = states.get(attempt_id, "")
        if state_name not in {"succeeded", "failed", "cancelled"}:
            unchanged.append(attempt_id)
            continue
        outcome = agent_consumer.record_tool_completion(
            state, attempt_id, state=state_name
        )
        if isinstance(outcome, Mapping) and outcome.get("emitted"):
            reconciled.append(attempt_id)
        else:
            unchanged.append(attempt_id)
    return {"checked": checked, "reconciled": reconciled, "unchanged": unchanged}


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


# --- recovery --------------------------------------------------------------


def _work_item_for(
    state_dir: Path,
    job: Mapping[str, Any],
    published: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """The item whose state this tick reports: active first, else the newest.

    A decision recorded outside the queue does not erase its work item; the
    consumer still has to close it. Reading the durable items keeps the tick
    honest about that instead of reporting "no work" once a decision exists.
    """

    if published:
        return dict(published[0])
    try:
        items = work_queue.list(Path(state_dir), job_id=str(job.get("job_id") or ""))
    except work_queue.WorkError:
        return None
    if not items:
        return None
    active = [item for item in items if str(item.get("state") or "") in {"pending", "claimed"}]
    return active[0] if active else items[-1]


def _authorization_for(state_dir: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The sealed authorization that bounds recovery, or ``None`` if absent.

    A missing authorization is not permission: the envelope then falls back to
    its documented default rather than to anything the task asked for.
    """

    try:
        return jobs.load_authorization(Path(state_dir), str(job.get("job_id") or ""))
    except jobs.JobError:
        return None


def _record_failure(
    state_dir: Path,
    job: Mapping[str, Any],
    task: Mapping[str, Any],
    record: Mapping[str, Any],
    clock: float,
) -> dict[str, Any] | None:
    """Persist the durable recovery request for a rejection, exactly once.

    The request is written *before* the rejection is committed, so a task can
    never be left rejected with no recorded path forward. Calling this again for
    the same attempt and evidence code returns the request already on disk.
    """

    try:
        return recoveries.build_request(
            Path(state_dir),
            job,
            task,
            failure=dict(record),
            authorization=_authorization_for(state_dir, job),
            now=clock,
        )
    except recoveries.RecoveryError as exc:
        raise ReconcileError(exc.code, exc.message, exc.fix) from exc


def _apply_recovery(
    state_dir: Path,
    job: dict[str, Any],
    clock: float,
    transitions: list[str],
) -> tuple[TickResult | None, list[dict[str, Any]]]:
    """Apply every validated decision; escalate what cannot dispatch.

    A retry or supersede appends the task's return to ``dispatching`` so the
    dispatch step below can issue exactly one corrective attempt in this tick.
    """

    try:
        applied = recoveries.apply_ready_decisions(state_dir, job, now=clock)
    except recoveries.RecoveryError as exc:
        raise ReconcileError(exc.code, exc.message, exc.fix) from exc
    except jobs.JobError as exc:
        raise ReconcileError(exc.code, exc.message, exc.fix) from exc

    escalations = [item for item in applied if item.get("decision") == "escalate"]
    if escalations:
        item = escalations[0]
        return (
            TickResult(
                status="escalated",
                transitions=tuple(transitions),
                action="needs_approval",
                detail=f"{item['task_id']}: {item.get('detail') or 'recovery escalated'}",
                evidence="recovery_escalated",
            ),
            applied,
        )
    for item in applied:
        if item.get("transition"):
            transitions.append(str(item["transition"]))
    return None, applied


def _pending_recovery(state_dir: Path, job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The newest request still waiting for a decision, if any."""

    try:
        return recoveries.outstanding(state_dir, job)
    except recoveries.RecoveryError as exc:
        raise ReconcileError(exc.code, exc.message, exc.fix) from exc


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


def _reap(
    state_dir: Path,
    job: dict[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    transitions: list[str],
    clock: float,
) -> bool:
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
            # A dead worker is recorded as a rejection too: without a durable
            # request the task would sit rejected with nothing able to move it.
            _record_failure(
                state_dir,
                job,
                task,
                {"code": "worker_failed", "evidence": f"worker ended {state}"},
                clock,
            )
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
    clock: float,
) -> TickResult:
    task_id = str(task["task_id"])
    attempt_id = (task.get("attempts") or [None])[-1]
    verdict = verifier({"attempt_id": attempt_id, "task": dict(task), "job_id": job["job_id"]})
    ok, evidence = _verdict(verdict, "verifier")
    if ok:
        jobs.transition(job, task_id, "verified", actor="reconciler", reason=evidence, evidence=evidence)
        transitions.append(f"{task_id}->verified")
        return TickResult(status="ok", transitions=tuple(transitions), action="verify", detail=f"{task_id} verified")
    # The rejection is only durable once its reason is: the recovery request is
    # written first, and a rejection with no recorded path forward never happens.
    # A legacy job that never froze a contract cannot carry a canonical request;
    # it rejects exactly as it did before M4 and never dispatches a correction.
    try:
        _record_failure(state_dir, job, task, {"ok": False, "evidence": evidence, "attempt_id": attempt_id}, clock)
    except ReconcileError as exc:
        if exc.code != "recovery_not_applicable":
            raise
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
    job: dict[str, Any],
    verdict: Mapping[str, Any],
    ok: bool,
    evidence: str,
    clock: float,
    transitions: list[str],
) -> TickResult:
    integration = job.setdefault("integration", {"wave": 1})
    if integration.get("terminal"):
        return TickResult(status="idle", detail="integration budget exhausted")

    integration["attempts"] = int(integration.get("attempts") or 0) + 1
    integration["evidence"] = evidence
    integration["state"] = "accepted" if ok else "failed"
    if ok and isinstance(verdict.get("commit"), str) and verdict["commit"]:
        integration["last_commit"] = verdict["commit"]

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
        accepted.append({"wave": wave, "at": _iso(clock), "evidence": evidence,
                         "commit": integration.get("last_commit") or ""})
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
        if isinstance(task.get("provision"), Mapping) and not str(
            (task.get("binding") or {}).get("receipt_digest") or ""
        ):
            # Provisioning runs before reconcile. A task whose binding was dropped
            # by a contract supersession must be re-provisioned under the corrected
            # contract before it may dispatch again.
            continue
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
    project = ""
    if isinstance(binding, Mapping):
        project = str(binding.get("project") or "").strip()
    project = project or str(task.get("project") or "").strip()
    if project:
        # The launcher dispatches into this project; without it scoped code work fails with
        # workspace_invalid (a registered local-path project has no provisioning binding).
        brief["project"] = project
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
    # go-max lane routing: a no-op unless the owner turned go-max on (docs/contracts/go-max.md).
    from . import go_max as _go_max

    brief = _go_max.route_brief(state_dir, brief, task)
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


def campaign_git_drift(
    state_dir: Path | str,
    campaign_id: str,
    *,
    repo: Path | str | None = None,
    git: git_observer.GitRunner | None = None,
    pull_requests: git_observer.PullRequestSource | None = None,
    now: str | None = None,
    main_ref: str | None = None,
) -> dict[str, Any]:
    """Observe a campaign checkpoint against real git refs and open PR heads.

    Read-only and report-only (workflow-F3): the checkpoint's recorded base
    revision, branch and pull request are compared with the actual refs in
    ``repo`` and the open PR heads ``gh`` reports, and the difference is
    returned as drift with the exact expected-vs-actual values. ``main_ref``
    names the authoritative main branch; when given, the recorded PR's exact
    head is also classified against it (``integrated_not_pr_merged`` when the
    head is already an ancestor of main while GitHub still reports the PR
    unmerged — the orbitdesktop #34 class). Nothing is resolved, reset, pushed,
    merged or rewritten here, and no checkpoint is modified; a caller decides
    what, if anything, to do about drift. An unobservable world raises
    :class:`git_observer.GitObserverError` — an observation failure is never
    reported as drift.
    """

    return git_observer.observe_campaign(
        state_dir,
        campaign_id,
        repo=repo,
        git=git,
        pull_requests=pull_requests,
        now=now,
        main_ref=main_ref,
    )


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


# ---------------------------------------------------------------------------
# Git evidence: the scheduler's consumer of the read-only observer report
# ---------------------------------------------------------------------------


#: The evidence code for the orbitdesktop #34 class: the PR's exact head is
#: already an ancestor of the authoritative main branch while GitHub still
#: reports the PR unmerged. The work is integrated; the pull request was not.
GIT_EVIDENCE_INTEGRATED = "integrated_not_pr_merged"
GIT_EVIDENCE_MERGED = "pr_merged"
GIT_EVIDENCE_EQUIVALENT = "equivalent_content_unverified"


def git_evidence_decision(
    report: Mapping[str, Any],
    proposed: str | None = None,
) -> dict[str, Any]:
    """Turn one git-observer report into the scheduler's next action.

    The observer is read-only; this function is the decision half that consumes
    its evidence. Rules, in priority order:

    * ``integrated_not_pr_merged`` — the work is provably on main while GitHub
      still reports the PR unmerged. Integration is **not permitted** (a second
      integration attempt would duplicate work that is already integrated), the
      wait for the PR merge ends, and the next action is to record the
      disposition. A proposed ``integrate…`` next action is suppressed and
      named in ``suppressed_next_action``.
    * ``pr_merged`` — GitHub reports the merge. The same disposition rule
      applies: adopt the external merge, never integrate again.
    * ``equivalent_content_unverified`` — main carries a patch-equivalent of
      the PR's commits, but equivalence is not proof of a merge. Integration is
      not permitted and the wait does **not** end: a verifier must confirm the
      content before any disposition is recorded.
    * any other drift — the world disagrees with the checkpoint; integration is
      not permitted and the disagreement is a human decision.
    * no drift and no anomaly — integration proceeds exactly as before.
    """

    if not isinstance(report, Mapping) or not isinstance(report.get("drift"), list):
        raise ReconcileError(
            "git_evidence_invalid",
            "the git observation report is not a valid observer report",
            "Pass the mapping git_observer.observe() returned; nothing is dispatched from a guess.",
        )
    drift_kinds = sorted(
        {
            str(item.get("kind") or "")
            for item in report["drift"]
            if isinstance(item, Mapping) and str(item.get("kind") or "")
        }
    )
    actual = report.get("actual")
    actual = actual if isinstance(actual, Mapping) else {}
    integration = actual.get("integration")
    integration = integration if isinstance(integration, Mapping) else {}
    verdict = str(integration.get("verdict") or "")

    def decision(
        evidence: str,
        *,
        permitted: bool,
        stop_waiting: bool,
        next_action: str | None,
        reason: str,
    ) -> dict[str, Any]:
        suppressed = ""
        if (
            proposed
            and isinstance(proposed, str)
            and not permitted
            and (proposed == "integrate" or proposed.startswith("integrate:"))
        ):
            suppressed = proposed
        return {
            "evidence": evidence,
            "disposition": evidence,
            "integration_permitted": permitted,
            "stop_waiting": stop_waiting,
            "next_action": next_action,
            "suppressed_next_action": suppressed,
            "reason": reason,
        }

    if verdict == GIT_EVIDENCE_INTEGRATED or GIT_EVIDENCE_INTEGRATED in drift_kinds:
        return decision(
            GIT_EVIDENCE_INTEGRATED,
            permitted=False,
            stop_waiting=True,
            next_action="record_disposition:integrated_not_pr_merged",
            reason=(
                "the PR's exact head is already an ancestor of the authoritative main branch while GitHub "
                "still reports the PR unmerged; the work is integrated, so record the disposition and stop "
                "waiting instead of integrating again"
            ),
        )
    if verdict == GIT_EVIDENCE_MERGED or GIT_EVIDENCE_MERGED in drift_kinds:
        return decision(
            GIT_EVIDENCE_MERGED,
            permitted=False,
            stop_waiting=True,
            next_action="record_disposition:pr_merged",
            reason="GitHub reports the PR merged; adopt the external merge and stop waiting instead of integrating again",
        )
    if verdict == GIT_EVIDENCE_EQUIVALENT or GIT_EVIDENCE_EQUIVALENT in drift_kinds:
        return decision(
            GIT_EVIDENCE_EQUIVALENT,
            permitted=False,
            stop_waiting=False,
            next_action="verify_integrated_content",
            reason=(
                "main carries patch-equivalent content but the PR's exact head is not an ancestor of main; "
                "equivalent content is not proof of a merge, so a verifier must confirm it first"
            ),
        )
    if drift_kinds:
        return decision(
            ",".join(drift_kinds),
            permitted=False,
            stop_waiting=False,
            next_action="needs_human:" + ",".join(drift_kinds),
            reason="the world disagrees with the recorded checkpoint; the disagreement needs a human decision",
        )
    return decision(
        "clean",
        permitted=True,
        stop_waiting=False,
        next_action=None,
        reason="the observed world matches the recorded checkpoint",
    )


# ---------------------------------------------------------------------------
# The reconciliation report: making the owner invariant measurable
# ---------------------------------------------------------------------------


RECONCILIATION_SCHEMA = "rizzstack.reconciliation-report/1"

#: Task states that close a unit of work. Superseded rows are terminal by
#: replacement — they are blocked work, not outstanding work.
_TERMINAL_TASK_STATES = frozenset({"accepted"})
_SUPERSEDED_TASK_STATE = "superseded"
#: Tasks a worker holds: claimed (sent, not yet running) through verifying.
_WORKER_IN_FLIGHT = frozenset({"dispatching", "dispatched", "running", "verifying"})
#: Tasks whose verified patch awaits an integration wave.
_INTEGRATION_WAITING = frozenset({"verified", "integrating"})
#: Task transitions that are automatic progression when a coordinator performs
#: them; a rejection or supersession is a stop, not a progression.
_AUTOMATIC_PROGRESS_STATES = frozenset(
    {"dispatching", "dispatched", "running", "verifying", "verified", "integrating", "accepted"}
)
#: The worker-completion marker: the durable event the reconciler writes when a
#: worker's result is reaped.
_WORKER_COMPLETED_SUFFIX = "->verifying"

_DEPLOYMENT_GATE = "deploy"


def reconciliation_report(
    state_dir: Path | str,
    *,
    capacity: int | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Measure whether authorized+eligible work plus capacity advances alone.

    Read-only: every source is opened for reading (sealed job documents,
    approval records, campaign checkpoints, the scheduler queue and feature
    claims) and nothing is written, unlocked, adopted or resolved. The metrics
    are the owner's §7 set:

    * ``outstanding`` — total/ready/claimed/active-workers task counts;
    * ``ready_waiting_despite_capacity`` — ready work sitting while slots are
      free and nothing genuinely requires the owner (the invariant's violation
      counter; zero when a human approval or a blocker legitimately gates it);
    * ``awaiting`` — verification / review / integration / deployment waits;
    * ``blocked`` — every blocked unit with its exact reason;
    * ``human_approvals_required`` — gates with no recorded decision (a gate
      decision is a create-only TTY record, so only a human can produce one);
    * ``last_successful_automatic_progression`` — the latest automatic forward
      transition any coordinator recorded;
    * ``worker_completion_to_next_action`` — the latency from a worker's
      completion being reaped to the next coordinator action on the same job.

    ``capacity`` is the scheduler's slot budget; it defaults to
    :data:`bundlebees_plugin.work_scheduler.MAX_ACTIVE`.
    """

    state = Path(state_dir)
    stamp = _iso(_clock(now))
    try:
        max_active = work_scheduler.MAX_ACTIVE if capacity is None else int(capacity)
    except (TypeError, ValueError) as exc:
        raise ReconcileError(
            "capacity_invalid",
            f"capacity {capacity!r} is not an integer slot budget",
            "Pass the scheduler's active-slot budget as an integer.",
        ) from exc
    if max_active < 0:
        raise ReconcileError("capacity_invalid", "capacity must not be negative", "Pass a non-negative slot budget.")

    blocked: list[dict[str, str]] = []
    approvals: list[dict[str, str]] = []
    awaiting = {"verification": 0, "review": 0, "integration": 0, "deployment": 0}
    outstanding = {"total": 0, "ready": 0, "claimed": 0, "active_workers": 0}
    progression: dict[str, Any] | None = None
    progression_at: float | None = None
    latencies: list[dict[str, Any]] = []

    jobs_root = state / jobs.JOBS_DIR
    if jobs_root.is_dir():
        for path in sorted(jobs_root.glob("*.json")):
            if path.name.startswith(".") or not path.is_file():
                continue
            job_id = path.stem
            try:
                job = jobs.load_job(state, job_id)
            except jobs.JobError as exc:
                blocked.append({"id": f"job:{job_id}", "reason": _reason(f"job unreadable: {exc.code}")})
                continue
            progression_new, latency_rows = _absorb_job(
                state, job, blocked=blocked, approvals=approvals, awaiting=awaiting, outstanding=outstanding
            )
            if progression_new is not None and (progression_at is None or progression_new[0] >= progression_at):
                progression_at, progression = progression_new
            latencies.extend(latency_rows)

    _absorb_campaigns(state, blocked=blocked, awaiting=awaiting)
    scheduler = _scheduler_block(state, blocked=blocked)
    features = _feature_block(state, blocked=blocked)

    in_use = outstanding["claimed"] + outstanding["active_workers"] + awaiting["verification"]
    available = max(0, max_active - in_use)
    owner_messages = len(approvals) + len(blocked)
    advancement_expected = bool(outstanding["ready"]) and available > 0 and owner_messages == 0

    latency_block: dict[str, Any] | None = None
    if latencies:
        ordered = sorted(
            latencies,
            key=lambda item: (_epoch(item["completed_at"]) or 0.0, str(item["completed_at"])),
        )
        latest = ordered[-1]
        latency_block = {
            "samples": len(ordered),
            "max_s": max(float(item["latency_s"]) for item in ordered),
            "latest_s": float(latest["latency_s"]),
            "latest_next_action": str(latest["next_action"]),
        }

    return {
        "schema": RECONCILIATION_SCHEMA,
        "generated_at": stamp,
        "capacity": {"max_active": max_active, "in_use": in_use, "available": available},
        "outstanding": outstanding,
        "ready_waiting_despite_capacity": outstanding["ready"] if advancement_expected else 0,
        "awaiting": awaiting,
        "blocked": blocked,
        "human_approvals_required": approvals,
        "last_successful_automatic_progression": progression,
        "worker_completion_to_next_action": latency_block,
        "scheduler": scheduler,
        "features": features,
        "invariant": {
            "advancement_expected_without_owner_messages": advancement_expected,
            "owner_messages_required": owner_messages,
        },
    }


def _reason(text: str) -> str:
    return _clip_reason(scrub(text))


def _clip_reason(text: str) -> str:
    return str(text)[:300]


def _absorb_job(
    state: Path,
    job: Mapping[str, Any],
    *,
    blocked: list[dict[str, str]],
    approvals: list[dict[str, str]],
    awaiting: dict[str, int],
    outstanding: dict[str, int],
) -> tuple[tuple[float, dict[str, Any]] | None, list[dict[str, Any]]]:
    """Fold one job's durable state into the report accumulators. Read-only."""

    job_id = str(job.get("job_id") or "")
    tasks = [task for task in (job.get("tasks") or []) if isinstance(task, Mapping)]
    states = {str(task.get("task_id") or ""): str(task.get("state") or "pending") for task in tasks}
    outstanding_recovery: Mapping[str, Any] | None = None
    try:
        outstanding_recovery = recoveries.outstanding(state, job)
    except recoveries.RecoveryError as exc:
        blocked.append({"id": f"job:{job_id}", "reason": _reason(f"recovery records are not canonical: {exc.code}")})
    except jobs.JobError as exc:
        blocked.append({"id": f"job:{job_id}", "reason": _reason(f"recovery state unreadable: {exc.code}")})

    ready_ids: list[str] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        state_name = states.get(task_id, "pending")
        if state_name == "accepted":
            continue
        if state_name == _SUPERSEDED_TASK_STATE:
            blocked.append(
                {
                    "id": f"{job_id}:{task_id}",
                    "reason": _reason(f"task {task_id} superseded; re-plan or a new authorization is required"),
                }
            )
            continue
        outstanding["total"] += 1
        if state_name in {"rejected", "failed"}:
            reason = f"task {task_id} {state_name}; a recovery or human decision is required"
            if (
                isinstance(outstanding_recovery, Mapping)
                and str(outstanding_recovery.get("task_id") or "") == task_id
            ):
                reason = (
                    f"task {task_id} {state_name}; recovery request "
                    f"{outstanding_recovery.get('request_id')} awaits a validated decision"
                )
            blocked.append({"id": f"{job_id}:{task_id}", "reason": _reason(reason)})
            continue
        if state_name == "pending":
            dependencies = [str(dep) for dep in (task.get("depends_on") or [])]
            if all(states.get(dep) == "accepted" for dep in dependencies):
                attempts = task.get("attempts") or []
                intent = task.get("intent")
                intent_status = str(intent.get("status") or "") if isinstance(intent, Mapping) else ""
                if not attempts and intent_status in {"", "not_sent"}:
                    ready_ids.append(task_id)
            continue
        if state_name in {"dispatching", "dispatched"}:
            outstanding["claimed"] += 1
            continue
        if state_name == "running":
            outstanding["active_workers"] += 1
            continue
        if state_name == "verifying":
            awaiting["verification"] += 1
            continue
        if state_name in _INTEGRATION_WAITING:
            awaiting["integration"] += 1
            continue
        blocked.append({"id": f"{job_id}:{task_id}", "reason": _reason(f"task {task_id} has unknown state {state_name!r}")})
    outstanding["ready"] += len(ready_ids)

    integration = job.get("integration")
    integration = integration if isinstance(integration, Mapping) else {}
    if integration.get("terminal"):
        evidence = str(integration.get("evidence") or integration.get("stopped") or "integration stopped")
        blocked.append({"id": f"job:{job_id}", "reason": _reason(f"integration_terminal: {evidence}")})
    elif tasks and all(states.get(str(task.get("task_id") or "")) == "accepted" for task in tasks):
        try:
            reasons = queue_mod.job_accepted_reasons(state, job_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable evidence check is a blocker, not a crash
            reasons = [f"job acceptance evidence could not be checked: {type(exc).__name__}"]
        if reasons:
            blocked.append({"id": f"job:{job_id}", "reason": _reason("job_evidence_incomplete: " + "; ".join(reasons[:4]))})

    gate_value = job.get("approval_gate")
    gate = gate_value if isinstance(gate_value, Mapping) else {}
    for name in [str(item) for item in (gate.get("gates") or [])]:
        path = state / jobs.APPROVALS_DIR / f"{job_id}.{jobs._slug(name)}.json"
        if not path.exists():
            approvals.append({"job_id": job_id, "gate": name})
            if jobs._slug(name) == _DEPLOYMENT_GATE:
                awaiting["deployment"] += 1

    events = [event for event in (job.get("events") or []) if isinstance(event, Mapping)]
    progression: tuple[float, dict[str, Any]] | None = None
    latencies: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        transition = str(event.get("transition") or "")
        at = _epoch(event.get("at"))
        if at is not None and transition.rsplit("->", 1)[-1] in _AUTOMATIC_PROGRESS_STATES:
            candidate = (at, {
                "at": str(event.get("at") or ""),
                "job_id": job_id,
                "task_id": str(event.get("task_id") or ""),
                "transition": transition,
            })
            if progression is None or candidate[0] >= progression[0]:
                progression = candidate
        if transition.endswith(_WORKER_COMPLETED_SUFFIX) and index + 1 < len(events):
            following = events[index + 1]
            completed_at = _epoch(event.get("at"))
            next_at = _epoch(following.get("at"))
            if completed_at is not None and next_at is not None:
                latencies.append(
                    {
                        "job_id": job_id,
                        "task_id": str(event.get("task_id") or ""),
                        "completed_at": str(event.get("at") or ""),
                        "next_action": str(following.get("transition") or ""),
                        "latency_s": next_at - completed_at,
                    }
                )
    return progression, latencies


def _absorb_campaigns(
    state: Path,
    *,
    blocked: list[dict[str, str]],
    awaiting: dict[str, int],
) -> None:
    root = state / campaign_state.CAMPAIGN_DIR
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        if path.name.startswith(".") or not path.is_file():
            continue
        try:
            record = campaign_state.load_campaign(state, path.stem)
        except campaign_state.CampaignError as exc:
            blocked.append({"id": f"campaign:{path.stem}", "reason": _reason(f"campaign checkpoint unreadable: {exc.code}")})
            continue
        phase = str(record.get("phase") or "")
        if phase == "ready_for_review":
            awaiting["review"] += 1
        blocker = record.get("blocker")
        if phase == "blocked" and isinstance(blocker, Mapping):
            code = str(blocker.get("code") or "blocked")
            detail = str(blocker.get("detail") or "")
            blocked.append({"id": f"campaign:{record.get('campaign_id') or path.stem}", "reason": _reason(f"{code}: {detail}")})


def _scheduler_block(state: Path, *, blocked: list[dict[str, str]]) -> dict[str, int]:
    try:
        items = work_scheduler.list_items(state)
    except work_scheduler.SchedulerError as exc:
        blocked.append({"id": "scheduler:queue", "reason": _reason(f"scheduler queue unreadable: {exc.code}")})
        return {"items": 0, "pending": 0, "claimed": 0, "blocked": 0, "done": 0}
    counts = {"items": len(items), "pending": 0, "claimed": 0, "blocked": 0, "done": 0}
    for item in items:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _feature_block(state: Path, *, blocked: list[dict[str, str]]) -> dict[str, int]:
    if not (state / "queue").is_dir():
        return {"active_claims": 0}
    try:
        claims = queue_mod.QueueStore(state).active()
    except queue_mod.QueueError as exc:
        blocked.append({"id": "queue:claims", "reason": _reason(f"feature queue state unreadable: {exc.code}")})
        return {"active_claims": 0}
    return {"active_claims": len(claims)}
