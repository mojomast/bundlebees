"""Durable campaign checkpoints for the bundlebees factory.

A job is the execution authority; a campaign is the *reviewable objective* that
points at one authorized job. The record stores the objective, the sealed
authorization snapshot, the phase, active tasks, dependencies, the last completed
action, the next action and the current wait/blocker. It never grants authority:
the job's signed authorization remains the only source of permission.

Reconciliation is deliberately read-only with respect to the job tree. It reads
the durable job document and the queue's job-acceptance evidence, then writes a
new campaign checkpoint; dispatch, verification, integration and recovery stay
in ``job_cli.tick``/the queue.
Repeating a reconcile against unchanged job state does not advance the revision
or rewrite the file, so a restart can adopt the checkpoint without side effects.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import job as jobs
from . import queue as queue_mod

SCHEMA = "rizzstack.campaign-state/1"
CAMPAIGN_DIR = "campaigns"

ALLOWED_TASK_STATES = (
    "pending",
    "dispatching",
    "dispatched",
    "running",
    "verifying",
    "verified",
    "integrating",
    "accepted",
    "rejected",
    "superseded",
    "failed",
)
ACTIVE_TASK_STATES = frozenset(
    {"dispatching", "dispatched", "running", "verifying", "verified", "integrating"}
)
WORKER_TASK_STATES = frozenset({"dispatching", "dispatched", "running", "verifying"})
INTEGRATION_TASK_STATES = frozenset({"verified", "integrating"})
#: A rejected or failed row needs a recovery decision or a human; a superseded
#: row is terminal by replacement and can never be retried.
FAILED_TASK_STATES = frozenset({"rejected", "failed"})
SUPERSEDED_TASK_STATE = "superseded"
BAD_TASK_STATES = FAILED_TASK_STATES | {SUPERSEDED_TASK_STATE}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class CampaignError(Exception):
    """A campaign checkpoint could not be created, read or reconciled."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise CampaignError(
            "campaign_id_invalid",
            f"{field} must be 1-96 characters of letters, digits, '.', '_' or '-'",
            "Use a stable campaign id.",
        )
    return text


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignError("campaign_invalid", f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    return _text(value, field)


def _path(state_dir: Path | str, campaign_id: str) -> Path:
    return Path(state_dir) / CAMPAIGN_DIR / f"{_id(campaign_id, 'campaign_id')}.json"


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CampaignError("campaign_missing", f"no campaign checkpoint at {path.name}") from exc
    except OSError as exc:
        raise CampaignError("campaign_unreadable", f"campaign checkpoint cannot be read: {exc}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise CampaignError("campaign_corrupt", f"campaign checkpoint is not JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise CampaignError("campaign_corrupt", "campaign checkpoint has the wrong schema")
    return value


def _authorization(auth: Mapping[str, Any]) -> dict[str, Any]:
    budget_value = auth.get("budget")
    budget: Mapping[str, Any] = budget_value if isinstance(budget_value, Mapping) else {}
    return {
        "auth_id": str(auth.get("auth_id") or ""),
        "statement": str(auth.get("statement") or ""),
        "repo": str(auth.get("repo") or ""),
        "base_rev": str(auth.get("base_rev") or ""),
        "scope": list(auth.get("scope") or []),
        "budget": {
            "bees": int(budget.get("bees") or 0),
            "wall_clock_s": int(budget.get("wall_clock_s") or 0),
        },
        "gates": list(auth.get("gates") or []),
    }


def create_campaign(
    state_dir: Path | str,
    *,
    campaign_id: str,
    job_id: str,
    objective: str,
    branch: str = "",
    pull_request: int | None = None,
) -> dict[str, Any]:
    """Create a campaign checkpoint for an existing authorized job.

    The job must already exist and have a sealed authorization. This function
    records a snapshot for reporting and reconciliation; it does not create or
    widen authority.
    """

    cid = _id(campaign_id, "campaign_id")
    jid = _id(job_id, "job_id")
    path = _path(state_dir, cid)
    if path.exists():
        raise CampaignError("campaign_exists", f"campaign {cid!r} already exists")
    job = jobs.load_job(Path(state_dir), jid)
    auth = jobs.load_authorization(Path(state_dir), jid)
    snapshot = _authorization(auth)
    if not snapshot["repo"] or not snapshot["base_rev"]:
        raise CampaignError("campaign_job_mismatch", "the job authorization has no repository/base revision")
    if str((job.get("goal") or {}).get("repo")) != snapshot["repo"]:
        raise CampaignError("campaign_job_mismatch", "the job repository differs from its authorization")
    if str((job.get("goal") or {}).get("base_rev")) != snapshot["base_rev"]:
        raise CampaignError("campaign_job_mismatch", "the job base revision differs from its authorization")
    pr = None
    if pull_request is not None:
        if isinstance(pull_request, bool) or not isinstance(pull_request, int) or pull_request < 1:
            raise CampaignError("campaign_invalid", "pull_request must be a positive integer")
        pr = pull_request
    record = {
        "schema": SCHEMA,
        "campaign_id": cid,
        "job_id": jid,
        "objective": _text(objective, "objective"),
        "branch": _optional_text(branch, "branch"),
        "pull_request": pr,
        "authorization": snapshot,
        "limits": {
            "bees": snapshot["budget"]["bees"],
            "wall_clock_s": snapshot["budget"]["wall_clock_s"],
            "scope": list(snapshot["scope"]),
            "human_gates": list(snapshot["gates"]),
        },
        "phase": "authorized",
        "active_workers": [],
        "integration_queue": [],
        "dependencies": {},
        "last_completed_action": None,
        "next_action": "reconcile",
        "wait_condition": "not reconciled against the job yet",
        "blocker": None,
        "job_accepted": False,
        "job_acceptance_reasons": [],
        "task_counts": {
            "total": 0, "accepted": 0, "active": 0, "integrating": 0,
            "ready": 0, "failed": 0, "superseded": 0,
        },
        "revision": 1,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write(path, record)
    return record


def load_campaign(state_dir: Path | str, campaign_id: str) -> dict[str, Any]:
    return _read(_path(state_dir, _id(campaign_id, "campaign_id")))


def _derive(
    record: Mapping[str, Any],
    job: Mapping[str, Any],
    *,
    now: str,
    job_accepted: bool,
    job_acceptance_reasons: list[str],
) -> dict[str, Any]:
    auth_value = record.get("authorization")
    auth: Mapping[str, Any] = auth_value if isinstance(auth_value, Mapping) else {}
    goal_value = job.get("goal")
    goal: Mapping[str, Any] = goal_value if isinstance(goal_value, Mapping) else {}
    if str(goal.get("repo")) != str(auth.get("repo")):
        raise CampaignError(
            "campaign_job_mismatch",
            "the job repository no longer matches the campaign authorization snapshot",
        )
    if str(goal.get("base_rev")) != str(auth.get("base_rev")):
        raise CampaignError(
            "campaign_job_mismatch",
            "the job base revision no longer matches the campaign authorization snapshot",
        )
    tasks_value = job.get("tasks")
    tasks: list[Any] = tasks_value if isinstance(tasks_value, list) else []
    task_map: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        if isinstance(task, Mapping) and task.get("task_id"):
            task_map[str(task["task_id"])] = task
    states = {
        task_id: str(task.get("state") or "pending") for task_id, task in task_map.items()
    }
    dependencies = {
        task_id: [str(dep) for dep in task.get("depends_on") or []]
        for task_id, task in task_map.items()
    }
    accepted = sorted(task_id for task_id, state in states.items() if state == "accepted")
    failed = sorted(task_id for task_id, state in states.items() if state in FAILED_TASK_STATES)
    superseded = sorted(
        task_id for task_id, state in states.items() if state == SUPERSEDED_TASK_STATE
    )
    active = sorted(
        task_id for task_id, state in states.items() if state in ACTIVE_TASK_STATES
    )
    worker_active = sorted(
        task_id for task_id, state in states.items() if state in WORKER_TASK_STATES
    )
    integration_active = sorted(
        task_id for task_id, state in states.items() if state in INTEGRATION_TASK_STATES
    )
    ready: list[str] = []
    for task_id, task in task_map.items():
        if task_id in accepted or task_id in failed or task_id in active:
            continue
        if states.get(task_id) != "pending":
            continue
        if all(states.get(dep) == "accepted" for dep in dependencies.get(task_id, [])):
            ready.append(task_id)
    ready.sort()

    worker_records = []
    integration_records = []
    for task_id in active:
        task = task_map[task_id]
        attempts = [
            str(item) for item in task.get("attempts") or [] if item
        ] if isinstance(task.get("attempts"), list) else []
        bucket = worker_records if states[task_id] in WORKER_TASK_STATES else integration_records
        bucket.append({"task_id": task_id, "state": states[task_id], "attempts": attempts})

    events = job.get("events") if isinstance(job.get("events"), list) else []
    last_action = None
    if events:
        last = events[-1]
        if isinstance(last, Mapping):
            last_action = {
                "at": str(last.get("at") or ""),
                "task_id": str(last.get("task_id") or ""),
                "transition": str(last.get("transition") or ""),
                "reason": str(last.get("reason") or ""),
            }

    integration_value = job.get("integration")
    integration: Mapping[str, Any] = (
        integration_value if isinstance(integration_value, Mapping) else {}
    )
    if integration.get("terminal"):
        phase = "blocked"
        blocker = {
            "code": "integration_terminal",
            "detail": str(integration.get("evidence") or "integration stopped after its bounded attempts"),
        }
        next_action = "resolve_integration"
        wait_condition = "integration is terminal; a recovery or human decision is required"
    elif failed:
        phase = "blocked"
        blocker = {
            "code": "task_failed",
            "task_ids": failed,
            "detail": "a task is rejected or failed; recovery or a human decision is required",
        }
        next_action = "resolve:" + ",".join(failed)
        wait_condition = "failure requires recovery or an explicit decision"
    elif superseded:
        phase = "blocked"
        blocker = {
            "code": "task_superseded",
            "task_ids": superseded,
            "detail": (
                "a task row was superseded and cannot be retried; the job needs a re-plan "
                "or a new authorization"
            ),
        }
        next_action = "replan:" + ",".join(superseded)
        wait_condition = "a superseded task can never be accepted; re-plan before continuing"
    elif tasks and len(accepted) == len(states) and job_accepted:
        phase = "job_complete"
        blocker = None
        next_action = "prepare_campaign_closeout"
        wait_condition = (
            "job tasks are accepted; campaign-level product evidence, audit triage "
            "and the closeout report have not been verified"
        )
    elif tasks and len(accepted) == len(states):
        phase = "blocked"
        blocker = {
            "code": "job_evidence_incomplete",
            "detail": "; ".join(job_acceptance_reasons[:8]) or "job acceptance evidence is incomplete",
        }
        next_action = "repair_job_evidence"
        wait_condition = "task rows are accepted but the job is not reviewable"
    elif integration_active and not worker_active:
        phase = "running"
        blocker = None
        next_action = "integrate:" + integration_active[0]
        wait_condition = (
            "integration step pending for "
            + ", ".join(f"{task_id} ({states[task_id]})" for task_id in integration_active)
        )
    elif active:
        phase = "running"
        blocker = None
        next_action = "wait_for_worker"
        wait_condition = "worker/job progress for " + ", ".join(
            f"{item['task_id']} ({item['state']})" for item in worker_records
        )
        if integration_records:
            wait_condition += "; integration pending for " + ", ".join(
                f"{item['task_id']} ({item['state']})" for item in integration_records
            )
    elif ready:
        phase = "ready_to_advance"
        blocker = None
        next_action = "dispatch:" + ready[0]
        wait_condition = "ready task can be dispatched"
    elif tasks:
        phase = "blocked"
        blocker = {
            "code": "no_ready_task",
            "task_ids": sorted(task_id for task_id in states if task_id not in accepted),
            "detail": "no pending task has all dependencies accepted",
        }
        next_action = "inspect_dependencies"
        wait_condition = "dependency graph has no dispatchable task"
    else:
        phase = "blocked"
        blocker = {
            "code": "no_tasks",
            "task_ids": [],
            "detail": "the job has no tasks; a campaign has nothing to advance",
        }
        next_action = "add_tasks"
        wait_condition = "add tasks to the authorized job before continuing"

    return {
        "phase": phase,
        # Only a live worker belongs here; a verified row is integration work.
        "active_workers": worker_records,
        "integration_queue": integration_records,
        "dependencies": dependencies,
        "last_completed_action": last_action,
        "next_action": next_action,
        "wait_condition": wait_condition,
        "blocker": blocker,
        "job_accepted": bool(job_accepted),
        "job_acceptance_reasons": list(job_acceptance_reasons),
        "task_counts": {
            "total": len(states),
            "accepted": len(accepted),
            "active": len(active),
            "integrating": len(integration_active),
            "ready": len(ready),
            "failed": len(failed),
            "superseded": len(superseded),
        },
        "reconciled_at": now,
    }


def reconcile(
    state_dir: Path | str,
    campaign_id: str,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Compare the checkpoint with the durable job and persist one fresh view.

    The job document is authoritative for execution. This function never
    dispatches, verifies or integrates; it only records what the job already
    proves and what the coordinator should do next.
    """

    cid = _id(campaign_id, "campaign_id")
    path = _path(state_dir, cid)
    record = _read(path)
    job_id = str(record.get("job_id") or "")
    if not job_id:
        raise CampaignError("campaign_corrupt", "campaign checkpoint has no job_id")
    job = jobs.load_job(Path(state_dir), job_id)
    reasons = queue_mod.job_accepted_reasons(Path(state_dir), job_id)
    observed = _derive(
        record,
        job,
        now=now or _now(),
        job_accepted=not reasons,
        job_acceptance_reasons=reasons,
    )
    changed = any(record.get(key) != observed[key] for key in observed if key != "reconciled_at")
    if not changed:
        return record
    updated = dict(record)
    updated.update(observed)
    updated["revision"] = int(record.get("revision") or 0) + 1
    updated["updated_at"] = observed["reconciled_at"]
    _write(path, updated)
    return updated


def resume_plan(state_dir: Path | str, campaign_id: str, *, now: str | None = None) -> dict[str, Any]:
    """Return the current durable checkpoint plus the restart decision.

    ``should_continue`` is true only while there is actionable work or an
    in-flight worker. It is false at the human review gate and at a blocker;
    the caller must not turn those into an automatic retry.
    """

    record = reconcile(state_dir, campaign_id, now=now)
    phase = str(record.get("phase") or "")
    return {
        "campaign_id": record["campaign_id"],
        "job_id": record["job_id"],
        "phase": phase,
        "revision": record["revision"],
        "active_workers": list(record.get("active_workers") or []),
        "integration_queue": list(record.get("integration_queue") or []),
        "next_action": record.get("next_action"),
        "wait_condition": record.get("wait_condition"),
        "blocker": record.get("blocker"),
        "should_continue": phase in {"authorized", "ready_to_advance", "running"},
        "requires_human": phase in {"ready_for_review", "blocked"},
        "requires_campaign_closeout": phase == "job_complete",
    }
