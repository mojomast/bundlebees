"""Feature queue: intake, durable claim, and one-pass consumption.

The M1 job store stays authoritative for execution. A queue item is a pointer to
an authorized job plus durable claim/link state; a Kanban status is never treated
as evidence that work completed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import job as jobs
from . import provision_bind
from . import provisioning
from . import verify_local


class QueueError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


STATES = ("queued", "claimed", "running", "ready_for_review", "blocked", "failed", "needs_approval")

#: Tick statuses that hand the job back to a human: nothing advances by itself.
#: ``escalated`` is a terminal integration stop and ``awaiting_decision`` is a
#: rejection waiting on a validated recovery decision; neither is progress.
HALTED_TICK_STATUSES = frozenset(
    {"blocked", "failed", "error", "escalated", "awaiting_decision", "needs_approval"}
)
#: Tick statuses where another live process already owns the job lock.
DEFERRED_TICK_STATUSES = frozenset({"locked", "deferred"})
#: Tick statuses that are positive progress on their own.
PROGRESS_TICK_STATUSES = frozenset({"ok", "running", "reconcile_wait"})
#: Task states a worker can still land a result for.
WORKER_TASK_STATES = frozenset({"dispatching", "dispatched", "running", "verifying"})


def _checkpoint(name: str, **payload: Any) -> None:
    """Fire a fault-injection checkpoint, if the faults lane installed one.

    Imported lazily on purpose: ``bundlebees_plugin.faults`` is owned by another
    lane and may be absent, and its absence must never break this module's
    import. A hook that raises is *not* caught here — that raise is the
    simulated crash.
    """
    try:
        import importlib

        faults = importlib.import_module(f"{__package__}.faults")
    except ImportError:
        return
    fire = getattr(faults, "checkpoint", None)
    if callable(fire):
        fire(name, **payload)


def _sync_dir(path: Path) -> None:
    """Persist a directory entry created by a rename (best effort)."""
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



@dataclass(frozen=True)
class Item:
    feature_id: str
    repo: str
    job_id: str
    title: str = ""
    depends_on: tuple[str, ...] = ()
    status: str = "queued"


class QueueAdapter(Protocol):
    def list_items(self) -> list[Item]: ...
    def get(self, feature_id: str) -> Item | None: ...
    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None: ...


class JsonQueue:
    """File-backed queue for tests and for hosts without a Kanban bridge."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise QueueError("queue_invalid", f"{self.path} is not valid JSON: {exc}", "Repair the queue file.") from exc

    def _save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def list_items(self) -> list[Item]:
        items = []
        for feature_id, body in sorted(self._load().items()):
            items.append(Item(feature_id=feature_id, repo=str(body.get("repo") or ""),
                              job_id=str(body.get("job_id") or ""), title=str(body.get("title") or ""),
                              depends_on=tuple(body.get("depends_on") or ()),
                              status=str(body.get("status") or "queued")))
        return items

    def get(self, feature_id: str) -> Item | None:
        return next((item for item in self.list_items() if item.feature_id == feature_id), None)

    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None:
        data = self._load()
        if feature_id not in data:
            raise QueueError("queue_unknown", f"no queue item {feature_id}", "Add it first.")
        data[feature_id]["status"] = status
        if evidence is not None:
            data[feature_id]["evidence"] = dict(evidence)
        data[feature_id]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save(data)


class KanbanQueue:
    """Adapter over a Kanban CLI exposing JSON (``hermes kanban ... --json``).

    The command is injected so the adapter is not tied to undocumented internals;
    a failure to produce valid JSON is an error, not an empty queue.
    """

    def __init__(self, run: Any) -> None:
        self._run = run

    def _json(self, args: list[str]) -> Any:
        out = self._run(["kanban", *args, "--json"])
        if getattr(out, "returncode", 1) != 0:
            raise QueueError("kanban_failed", f"kanban {' '.join(args)} failed: {getattr(out, 'stderr', '')[:200]}", "Check the Kanban CLI.")
        try:
            return json.loads(out.stdout or "[]")
        except ValueError as exc:
            raise QueueError("kanban_invalid", "the Kanban CLI did not return JSON", "Use a supported CLI version.") from exc

    def list_items(self) -> list[Item]:
        items = []
        for card in self._json(["list"]):
            if not isinstance(card, Mapping):
                continue
            body = card.get("metadata") or card.get("fields") or {}
            if not isinstance(body, Mapping) or not body.get("job_id"):
                continue
            items.append(Item(feature_id=str(card.get("id")), repo=str(body.get("repo") or ""),
                              job_id=str(body["job_id"]), title=str(card.get("title") or ""),
                              depends_on=tuple(body.get("depends_on") or ()),
                              status=str(card.get("status") or "queued")))
        return items

    def get(self, feature_id: str) -> Item | None:
        return next((item for item in self.list_items() if item.feature_id == feature_id), None)

    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None:
        args = ["move", feature_id, status]
        if evidence is not None:
            args += ["--note", json.dumps(dict(evidence), sort_keys=True)[:500]]
        self._json(args)


class QueueStore:
    """Durable claim + link records, one file per feature."""

    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "queue"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, feature_id: str) -> Path:
        safe = "".join(ch for ch in feature_id if ch.isalnum() or ch in "-_.")
        if safe != feature_id or not safe:
            raise QueueError("feature_id_invalid", "feature ids are [A-Za-z0-9._-]+", "Use the queue's id.")
        return self.root / f"{safe}.json"

    def read(self, feature_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.path(feature_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise QueueError("queue_state_corrupt", f"{feature_id} state is unreadable: {exc}", "Inspect it by hand.") from exc

    def write(self, feature_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(record, feature_id=feature_id, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        path = self.path(feature_id)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
        return body

    def active(self) -> list[dict[str, Any]]:
        """Every claim that is still in flight, refusing records it cannot read.

        A record that cannot be read might be an active claim, so it is a named
        refusal rather than a silently dropped item: dropping it would let the
        scheduler forget work that is genuinely running, and let a second
        consumer claim the same feature. A record that is genuinely absent
        (never written, or removed between listing and reading) is normal.
        """
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue  # genuinely absent: nothing claimed it
            except OSError as exc:
                raise QueueError(
                    "queue_state_unreadable",
                    f"{path.stem} queue state cannot be read: {exc}",
                    "Inspect it by hand; an unreadable record may be an active claim.",
                ) from exc
            try:
                record = json.loads(text)
            except ValueError as exc:
                raise QueueError(
                    "queue_state_corrupt",
                    f"{path.stem} queue state is not valid JSON: {exc}",
                    "Inspect it by hand; a corrupt record may be an active claim.",
                ) from exc
            if not isinstance(record, dict):
                raise QueueError(
                    "queue_state_corrupt",
                    f"{path.stem} queue state is not a JSON object",
                    "Inspect it by hand; a corrupt record may be an active claim.",
                )
            record.setdefault("feature_id", path.stem)
            if record.get("state") in ("claimed", "running"):
                records.append(record)
        return records

    def claim(self, feature_id: str, *, job_id: str) -> dict[str, Any]:
        """O_EXCL claim: two consumers cannot both take the same feature."""
        path = self.path(feature_id)
        record = {"feature_id": feature_id, "state": "claimed", "job_id": job_id, "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "pid": os.getpid()}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self.read(feature_id) or {}
            raise QueueError("already_claimed", f"{feature_id} is already {existing.get('state')}", "Inspect or release it; do not run twice.") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_dir(path.parent)
        return record


def eligible(items: list[Item], done: set[str]) -> Item | None:
    for item in sorted(items, key=lambda i: i.feature_id):
        if item.status not in ("queued",) or item.feature_id in done:
            continue
        if all(dep in done for dep in item.depends_on):
            return item
    return None


def _handoff_dependents(state_dir: Path, items: Mapping[str, "Item"], done: set[str], context: Mapping[str, Any]) -> list[str]:
    """Materialize queued dependents from their intent packets. Production path.

    Selection is by the packet's policy_id; a dependent without a stored packet
    is left queued (no authority is inferred from a card or a dependency).
    """
    from . import handoff, packets

    created = []
    for item in items.values():
        if item.status != "queued" or not item.depends_on:
            continue
        if not all(dep in done for dep in item.depends_on):
            continue
        if _load_job_or_none(state_dir, item.job_id) is not None:
            continue
        raw = packets.read_packet(state_dir, item.job_id)
        if raw is None:
            continue
        try:
            packet = json.loads(raw)
        except ValueError:
            continue
        result = handoff.materialize(state_dir, feature_id=item.feature_id, job_id=item.job_id,
                                     packet=packet, worktree=Path(context["worktree"]),
                                     mirror=Path(context["mirror"]),
                                     repo_identity=str(context.get("repo_identity") or ""))
        if result.get("created"):
            created.append(item.feature_id)
    return created


def _job_has_worker(state_dir: Path, job_id: str) -> bool:
    """Whether the durable job still has a task a worker can land a result for.

    A tick can honestly report ``idle`` while a worker runs: the reconciler has
    nothing to do until the result lands. The job document, not the status
    string, says whether that wait is a worker wait.
    """

    try:
        job = jobs.load_job(state_dir, job_id)
    except Exception:  # noqa: BLE001 - an unreadable job is a named queue outcome below
        return False
    for task in job.get("tasks") or []:
        if isinstance(task, Mapping) and str(task.get("state") or "") in WORKER_TASK_STATES:
            return True
    return False


def _tick_status(result: Any) -> str:
    return str(result.get("status") or "") if hasattr(result, "get") else ""


def _settle(
    state_dir: Path,
    queue: QueueAdapter,
    store: "QueueStore",
    feature_id: str,
    job_id: str,
    record: Mapping[str, Any],
    result: Any,
) -> dict[str, Any]:
    """Turn one tick result into a durable card/record state.

    A tick status is never accepted as completion on its own: a halted tick
    blocks the card instead of leaving it at ``running`` forever, a known
    progress status can keep an active card ``running``, and a tick that did not
    run (another live process holds the job lock) rewrites nothing.
    """

    status = _tick_status(result)
    if _job_accepted(state_dir, job_id):
        # Acknowledge the card before relaxing the local record, so a crash in
        # between leaves a record that is still active and is re-acked on the
        # next pass, rather than a card stuck at "running" with nothing left
        # to drive it.
        _checkpoint("queue.before_ack", feature_id=feature_id, job_id=job_id,
                    state="ready_for_review")
        queue.set_status(feature_id, "ready_for_review", {"job_id": job_id})
        store.write(feature_id, dict(record, state="ready_for_review", evidence=result, pid=os.getpid()))
        return {"action": "ready_for_review", "status": status}
    if status in DEFERRED_TICK_STATUSES:
        return {
            "action": "deferred",
            "status": status,
            "detail": str(result.get("detail") or "another tick holds the job lock"),
        }
    if status in HALTED_TICK_STATUSES:
        settled_state = "failed" if status in ("failed", "error") else "blocked"
        detail = str(result.get("detail") or result.get("action") or status)
        # Acknowledge the card before relaxing the record. If the card write
        # fails, the claim stays active and the next pass retries; the reverse
        # order would leave a non-active record behind a card stuck at running.
        queue.set_status(feature_id, settled_state, {"detail": detail[:400]})
        store.write(feature_id, dict(record, state=settled_state, evidence=result, pid=os.getpid()))
        return {"action": settled_state, "status": status, "detail": detail}
    if status in PROGRESS_TICK_STATUSES or _job_has_worker(state_dir, job_id):
        store.write(feature_id, dict(record, state="running", evidence=result, pid=os.getpid()))
        queue.set_status(feature_id, "running", {"job_id": job_id})
        return {"action": "running", "status": status}

    detail = str(result.get("detail") or "") if hasattr(result, "get") else ""
    message = (
        f"the tick reported {status or 'no status'!r}, the job is not accepted and no task is "
        "in flight: nothing will advance it without a decision"
    )
    if detail:
        message += f" ({detail})"
    # Same card-first rule as the halted branch: never strand an active-looking
    # card behind a record that already says blocked.
    queue.set_status(feature_id, "blocked", {"detail": message[:400]})
    store.write(feature_id, dict(record, state="blocked", evidence=result, pid=os.getpid()))
    return {"action": "blocked", "status": status, "detail": message}


def _provision_refusal(exc: Exception) -> dict[str, str]:
    """One typed provisioning refusal: code, fix and a reportable detail line.

    Both refusal types carry ``code``/``message``/``fix``; this keeps the
    reportable shape in one place instead of branching per exception class.
    """
    code = str(getattr(exc, "code", "") or "provision_error")
    message = str(exc) or code
    fix = str(getattr(exc, "fix", "") or "")
    detail = f"{code}: {message}"
    if fix:
        detail = f"{detail}; fix: {fix}"
    refusal = {"error": code, "message": message, "detail": detail}
    if fix:
        refusal["fix"] = fix
    return refusal


def run_once(state_dir: Path, queue: QueueAdapter, *, tick: Any, on_status: Any = None,
             handoff_context: Mapping[str, Any] | None = None,
             admit: bool = True) -> dict[str, Any]:
    """One pass: reconcile active work first, then admit at most one new feature.

    A feature that is claimed or running is never dropped from scheduling because
    of its displayed status, and completion is derived from the job document.
    ``admit=False`` is the observation-only pass: a tick that cannot dispatch or
    verify must not claim a feature, because the claim would only mark a card
    running with nothing able to advance it.
    """

    store = QueueStore(state_dir)
    items = {item.feature_id: item for item in queue.list_items()}
    if not admit:
        queued = sorted(feature_id for feature_id, item in items.items() if item.status == "queued")
        active = sorted(str(record.get("feature_id")) for record in store.active())
        if not queued and not active:
            return {"action": "idle", "feature": None}
        return {
            "action": "observation_only",
            "feature": None,
            "queued": queued[:50],
            "active": active[:50],
            "detail": (
                "this pass has no dispatch-capable tick (no launcher/repo/suite); it cannot "
                "claim, dispatch, verify or integrate, so no card was moved"
            ),
        }

    done = {feature_id for feature_id, item in items.items() if _job_accepted(state_dir, item.job_id)}
    if handoff_context:
        created = _handoff_dependents(state_dir, items, done, handoff_context)
        if created:
            return {"action": "handoff", "feature": created[0], "created": created}

    # 1. Advance existing work before admitting anything new.
    for record in store.active():
        feature_id = str(record.get("feature_id"))
        item = items.get(feature_id)
        if item is None:
            continue
        claimed_job = str(record.get("job_id") or "")
        if claimed_job and claimed_job != item.job_id:
            # The claim and the card disagree about the job. Ticking either one
            # would attach a live claim to authority it was not taken for.
            detail = (
                f"the claim names job {claimed_job!r} but the card names "
                f"{item.job_id!r}; refusing to tick either"
            )
            queue.set_status(feature_id, "blocked", {"detail": detail[:400]})
            store.write(feature_id, dict(record, state="blocked", detail=detail, pid=os.getpid()))
            return {
                "action": "blocked",
                "feature": feature_id,
                "job": claimed_job,
                "detail": detail,
            }
        job = _load_job_or_none(state_dir, item.job_id)
        if job is None:
            queue.set_status(feature_id, "needs_approval", {"detail": "claimed but no job; authorization or packet must be re-registered"})
            store.write(feature_id, dict(record, state="needs_approval", detail="claimed job does not exist", pid=os.getpid()))
            return {"action": "blocked", "feature": feature_id, "job": item.job_id,
                    "detail": "a claim exists but its job does not; recreate the authorized job, then re-run"}
        try:
            result = tick(item.job_id)
        except (provisioning.ProvisionError, provision_bind.ProvisionBindError) as exc:
            # A typed provisioning refusal short-circuits this pass the way it
            # short-circuits the job CLI: report the refusal, block the card,
            # and admit nothing else. Card first, then the record, so a failed
            # card write leaves an active claim the next pass can retry.
            refusal = _provision_refusal(exc)
            queue.set_status(feature_id, "blocked", {"detail": refusal["detail"][:400]})
            store.write(feature_id, dict(record, state="blocked", evidence=refusal,
                                          detail=refusal["detail"], pid=os.getpid()))
            return {"action": "blocked", "feature": feature_id, "job": item.job_id,
                    "code": refusal["error"], "detail": refusal["detail"]}
        settled = _settle(state_dir, queue, store, feature_id, item.job_id, record, result)
        payload = {"action": settled["action"], "feature": feature_id, "job": item.job_id,
                   "status": settled.get("status", "")}
        if settled.get("detail"):
            payload["detail"] = settled["detail"]
        return payload

    # 2. Admit one eligible feature, judged only against evidence.
    item = eligible(list(items.values()), done)
    if item is None:
        return {"action": "idle", "feature": None}

    try:
        record = store.claim(item.feature_id, job_id=item.job_id)
    except QueueError as exc:
        if exc.code != "already_claimed":
            raise
        existing = store.read(item.feature_id) or {}
        existing_state = str(existing.get("state") or "") or "unreadable"
        if existing_state in ("claimed", "running"):
            return {"action": "deferred", "feature": item.feature_id, "job": item.job_id,
                    "detail": f"{item.feature_id} is already {existing_state} under another consumer"}
        detail = (
            f"the card is queued but its claim record is {existing_state!r}; inspect "
            f"queue/{item.feature_id}.json and release it before requeueing"
        )
        # Mark the card blocked so this refusal cannot starve every later
        # eligible card on each pass. Releasing the stale record stays human work.
        queue.set_status(item.feature_id, "blocked", {"detail": detail[:400]})
        return {
            "action": "blocked",
            "feature": item.feature_id,
            "job": item.job_id,
            "detail": detail,
        }
    queue.set_status(item.feature_id, "claimed", {"job_id": item.job_id})
    if _load_job_or_none(state_dir, item.job_id) is None:
        queue.set_status(item.feature_id, "needs_approval", {"detail": "no authorized job for this feature"})
        store.write(item.feature_id, dict(record, state="needs_approval", detail="job missing at claim time", pid=os.getpid()))
        return {"action": "blocked", "feature": item.feature_id, "job": item.job_id,
                "detail": "claimed, but the authorized job does not exist"}

    try:
        result = tick(item.job_id)
    except (provisioning.ProvisionError, provision_bind.ProvisionBindError) as exc:
        # Same rule as the active pass above: a typed provisioning refusal is
        # reported and the freshly claimed card is blocked, not left running.
        refusal = _provision_refusal(exc)
        queue.set_status(item.feature_id, "blocked", {"detail": refusal["detail"][:400]})
        store.write(item.feature_id, dict(record, state="blocked", evidence=refusal,
                                          detail=refusal["detail"], pid=os.getpid()))
        return {"action": "blocked", "feature": item.feature_id, "job": item.job_id,
                "code": refusal["error"], "detail": refusal["detail"]}
    settled = _settle(state_dir, queue, store, item.feature_id, item.job_id, record, result)
    action = settled["action"]

    if on_status is not None:
        on_status(item.feature_id, action)
    payload = {"action": action, "feature": item.feature_id, "job": item.job_id,
               "status": settled.get("status", "")}
    if settled.get("detail"):
        payload["detail"] = settled["detail"]
    return payload


def _job_accepted_reasons(state_dir: Path, job_id: str) -> list[str]:
    """Why a job is not reviewable yet (empty list means accepted)."""
    reasons: list[str] = []
    try:
        job = jobs.load_job(state_dir, job_id)
    except Exception as exc:  # noqa: BLE001
        return [f"job unreadable: {exc}"]
    tasks = job.get("tasks") or []
    if not tasks:
        return ["job has no tasks"]
    for task in tasks:
        task_id = task.get("task_id")
        if str(task.get("state")) != "accepted":
            reasons.append(f"{task_id}: state {task.get('state')}")
            continue
        attempts = task.get("attempts") or []
        if not attempts:
            reasons.append(f"{task_id}: no attempts")
            continue
        record = verify_local.read_record(Path(state_dir), str(attempts[-1]))
        if not isinstance(record, Mapping) or record.get("ok") is not True or not record.get("sha"):
            reasons.append(f"{task_id}: verification record missing or invalid")
            continue
        binding = task.get("binding") or {}
        if binding.get("source_rev") and str(record.get("base_rev")) != str(binding.get("source_rev")):
            reasons.append(f"{task_id}: verified base {record.get('base_rev')} != bound {binding.get('source_rev')}")
        if binding.get("contract_sha256") and str(task.get("contract_sha256") or "") != str(binding.get("contract_sha256")):
            reasons.append(f"{task_id}: contract hash mismatch")
        if not isinstance(record.get("gitleaks"), Mapping) or record["gitleaks"].get("exit_code") != 0:
            reasons.append(f"{task_id}: gitleaks did not pass")
        if not isinstance(record.get("suite"), Mapping) or record["suite"].get("exit_code") != 0:
            reasons.append(f"{task_id}: regression suite did not pass")
    integration = job.get("integration") or {}
    waves = [w for w in integration.get("accepted_waves") or [] if isinstance(w, Mapping)]
    if not waves:
        reasons.append("no accepted integration wave")
    if not str(integration.get("last_commit") or (waves[-1].get("commit") if waves else "") or ""):
        reasons.append("accepted wave names no integrated commit")
    return reasons


def _job_accepted(state_dir: Path, job_id: str) -> bool:
    """Completion requires real verification records, not status strings."""
    return not _job_accepted_reasons(state_dir, job_id)


def job_accepted(state_dir: Path | str, job_id: str) -> bool:
    """Public read-only job-completion check for campaign/continuity callers."""
    return _job_accepted(Path(state_dir), job_id)


def job_accepted_reasons(state_dir: Path | str, job_id: str) -> list[str]:
    """Public read-only list of missing job evidence; empty means accepted."""
    return _job_accepted_reasons(Path(state_dir), job_id)


def _load_job_or_none(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Existence, not acceptance. Let the repo-aware tick finish a pending handoff."""
    try:
        return jobs.load_job(state_dir, job_id, recover=False)
    except jobs.JobError as exc:
        if exc.code == "job_missing":
            return None
        raise
