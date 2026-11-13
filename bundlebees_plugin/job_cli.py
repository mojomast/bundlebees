"""`bundlebees job ...` — the coordinator's mechanical loop and agent handoff.

The split that matters: `job tick` is deterministic and safe to run from cron with
no model in the loop. When a transition needs judgement it writes a durable
handoff (`needs-agent.json`) and returns. `job agent-pending` prints that prompt
for an agent turn; claiming it does **not** delete it, because a decision that is
not durably acknowledged must be retried, not lost. `job note` records the
decision and marks the handoff acknowledged.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import integrate_job, job as jobs, reconcile, verify_local
from .launcher_adapter import LauncherAdapter, LeaseBinder


class JobCliError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


# ---------------------------------------------------------------------------
# Handoff
# ---------------------------------------------------------------------------


def handoff_path(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / "jobs" / job_id / "needs-agent.json"


def _write_handoff(state_dir: Path, job_id: str, payload: Mapping[str, Any]) -> Path:
    path = handoff_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_handoff(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(handoff_path(state_dir, job_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def record_handoff(state_dir: Path, job_id: str, result: reconcile.TickResult) -> dict[str, Any] | None:
    """Create a durable handoff for a tick that needs reasoning."""

    if result.status != "escalated":
        return None
    existing = read_handoff(state_dir, job_id)
    if existing and existing.get("status") in {"pending", "claimed"}:
        return existing

    code = result.evidence or "escalated"
    prompt = (
        f"Job {job_id} stopped at {code}. Detail: {result.detail}\n"
        f"Inspect the recorded evidence under jobs/{job_id}/, decide whether to re-dispatch, "
        f"supersede the contract, or abandon the job, then record the decision with:\n"
        f"  bundlebees job note --job-id {job_id} --note '<decision and why>'"
    )
    payload_obj = {
        "job_id": job_id,
        "kind": code,
        "status": "pending",
        "prompt": prompt,
        "detail": result.detail,
        "created_at": _now(),
        "claimed_at": None,
        "claimed_by": None,
        "acknowledged_at": None,
    }
    _write_handoff(state_dir, job_id, payload_obj)
    return payload_obj


def pending_prompt(state_dir: Path, job_id: str) -> str | None:
    """Claim the pending prompt without deleting it."""

    payload = read_handoff(state_dir, job_id)
    if not payload or payload.get("status") != "pending":
        return None
    payload.update(status="claimed", claimed_at=_now(), claimed_by=f"pid:{os.getpid()}")
    _write_handoff(state_dir, job_id, payload)
    return str(payload.get("prompt") or "")


def acknowledge(state_dir: Path, job_id: str, note: str) -> dict[str, Any]:
    payload = read_handoff(state_dir, job_id)
    if not payload:
        raise JobCliError("no_handoff", "this job has no pending handoff", "Nothing to acknowledge.")
    if not str(note or "").strip():
        raise JobCliError("empty_note", "a decision needs a note", "Say what you decided and why.")
    payload.update(status="done", acknowledged_at=_now(), decision=str(note).strip())
    _write_handoff(state_dir, job_id, payload)
    job = jobs.load_job(state_dir, job_id)
    job.setdefault("events", []).append(
        {"at": _now(), "task_id": "job", "transition": "agent_ack", "actor": "agent", "reason": str(note).strip()}
    )
    jobs.save_job(state_dir, job)
    return payload


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def tick(
    state_dir: Path,
    job_id: str,
    *,
    client: Any,
    repo: Path,
    tests_for_task=None,
    gitleaks: str | None = None,
    suite_command: Sequence[str] = ("python", "-m", "pytest", "-q"),
    now: Any = None,
) -> dict[str, Any]:
    """One mechanical tick, wired to the production adapter, verifier and integrator."""

    adapter = LauncherAdapter(client)
    binder = LeaseBinder(state_dir)
    job = jobs.load_job(state_dir, job_id)

    def verifier(ctx: Mapping[str, Any]) -> Mapping[str, Any]:
        task = ctx.get("task") or {}
        attempt_id = str(ctx.get("attempt_id") or "")
        envelope = client.result_envelope(attempt_id)
        patch = _patch_text(envelope)
        if not patch:
            return {"ok": False, "evidence": "no patch in the worker result"}
        files = [str(item.get("path")) for item in ((envelope.get("patch") or {}).get("files") or []) if item.get("path")]
        source = str(((envelope.get("source") or {}).get("repo_rev")) or "")
        acceptance = task.get("acceptance") or {}
        tests = list(acceptance.get("tests") or []) if isinstance(acceptance, Mapping) else []
        try:
            record = verify_local.verify_and_commit(
                state_dir,
                repo,
                attempt_id,
                patch=patch,
                declared_files=files,
                base_rev=source,
                tests=tests,
                suite_command=list(suite_command),
                gitleaks=gitleaks,
            )
        except verify_local.VerifyError as exc:
            return {"ok": False, "evidence": f"{exc.code}: {exc.message}"}
        return {"ok": True, "evidence": f"commit {record['sha'][:12]} verified locally"}

    def integrator(job_doc: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            result = integrate_job.integrate(
                state_dir,
                job_doc,
                repo,
                suite_command=suite_command,
            )
        except (integrate_job.IntegrateError, Exception) as exc:  # noqa: BLE001 - reported as a failed wave
            return {"ok": False, "evidence": f"integration error: {exc}"}
        return {"ok": bool(result.get("ok")), "evidence": str(result.get("evidence") or "")}

    result = reconcile.tick(
        state_dir,
        job_id,
        launcher=adapter,
        verifier=verifier,
        integrator=integrator,
        binder=binder,
        now=now,
    )
    handoff = record_handoff(state_dir, job_id, result)
    payload = {
        "status": result.status,
        "action": result.action,
        "transitions": list(result.transitions),
        "detail": result.detail,
        "evidence": result.evidence,
        "exactly_once": result.exactly_once,
        "handoff": handoff,
    }
    return payload


def _patch_text(envelope: Mapping[str, Any]) -> str | None:
    patch = envelope.get("patch")
    if isinstance(patch, str):
        return patch if patch.strip() else None
    if isinstance(patch, Mapping):
        diff = patch.get("diff")
        if isinstance(diff, str) and diff.strip():
            return diff
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Status / report
# ---------------------------------------------------------------------------


def status(state_dir: Path, job_id: str) -> dict[str, Any]:
    job = jobs.load_job(state_dir, job_id)
    handoff = read_handoff(state_dir, job_id)
    return {
        "job_id": job["job_id"],
        "goal": job.get("goal", {}),
        "integration": job.get("integration", {}),
        "escalations": job.get("escalations", []),
        "tasks": [
            {
                "task_id": task.get("task_id"),
                "state": task.get("state"),
                "attempts": task.get("attempts", []),
                "depends_on": task.get("depends_on", []),
            }
            for task in job.get("tasks", [])
        ],
        "handoff": None if handoff is None else {"status": handoff.get("status"), "kind": handoff.get("kind")},
    }


def report(state_dir: Path, job_id: str) -> dict[str, Any]:
    job = jobs.load_job(state_dir, job_id)
    rows = []
    for task in job.get("tasks", []):
        attempt_id = (task.get("attempts") or [None])[-1]
        record = verify_local.read_record(state_dir, attempt_id) if attempt_id else None
        rows.append(
            {
                "task_id": task.get("task_id"),
                "state": task.get("state"),
                "attempt_id": attempt_id,
                "commit": (record or {}).get("sha"),
                "verified": bool((record or {}).get("ok")),
            }
        )
    payload = {
        "job_id": job_id,
        "goal": job.get("goal", {}),
        "tasks": rows,
        "integration": job.get("integration", {}),
        "escalations": job.get("escalations", []),
        "measurements": {"tokens": "UNKNOWN", "cost": "UNKNOWN"},
    }
    target = Path(state_dir) / "jobs" / f"{job_id}.report.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# argparse + dispatch
# ---------------------------------------------------------------------------


def add_parsers(subparsers: Any) -> None:
    job = subparsers.add_parser(
        "job",
        help="Durable job commands: authorize, create, status, tick, note, agent-pending, report.",
    )
    sub = job.add_subparsers(dest="job_command", required=True)

    auth = sub.add_parser("authorize", help="Record a human authorization (interactive).")
    auth.add_argument("--job-id", required=True)
    auth.add_argument("--repo", required=True)
    auth.add_argument("--base-rev", required=True)
    auth.add_argument("--scope", action="append", required=True)
    auth.add_argument("--bees", type=int, default=3)
    auth.add_argument("--wall-clock", type=int, default=3600)
    auth.add_argument("--statement", required=True)
    auth.add_argument("--state-dir", default=None)
    auth.set_defaults(command="job", job_handler="authorize")

    create = sub.add_parser("create", help="Create a job from a JSON task list.")
    create.add_argument("--job-id", required=True)
    create.add_argument("--tasks", required=True, help="Path to a JSON list of tasks.")
    create.add_argument("--state-dir", default=None)
    create.set_defaults(command="job", job_handler="create")

    stat = sub.add_parser("status", help="Print job state and the pending handoff.")
    stat.add_argument("--job-id", required=True)
    stat.add_argument("--state-dir", default=None)
    stat.set_defaults(command="job", job_handler="status")

    tick = sub.add_parser("tick", help="Run one mechanical tick (safe from cron).")
    tick.add_argument("--job-id", required=True)
    tick.add_argument("--repo", required=True, help="Repository worktree the verification runs against.")
    tick.add_argument("--gitleaks", default=None)
    tick.add_argument("--state-dir", default=None)
    tick.set_defaults(command="job", job_handler="tick")

    note = sub.add_parser("note", help="Record a decision for the pending handoff.")
    note.add_argument("--job-id", required=True)
    note.add_argument("--note", required=True)
    note.add_argument("--state-dir", default=None)
    note.set_defaults(command="job", job_handler="note")

    pending = sub.add_parser("agent-pending", help="Print the pending agent prompt, if any.")
    pending.add_argument("--job-id", required=True)
    pending.add_argument("--state-dir", default=None)
    pending.set_defaults(command="job", job_handler="agent-pending")

    rep = sub.add_parser("report", help="Write and print the job acceptance report.")
    rep.add_argument("--job-id", required=True)
    rep.add_argument("--state-dir", default=None)
    rep.set_defaults(command="job", job_handler="report")


def cmd_job(ctx: Any, args: Any, *, state_dir: Path, client_factory) -> int:
    handler = getattr(args, "job_handler", None)
    job_id = getattr(args, "job_id", None)
    try:
        if handler == "authorize":
            record = jobs.record_authorization(
                state_dir,
                job_id=job_id,
                statement=args.statement,
                repo=args.repo,
                base_rev=args.base_rev,
                scope=args.scope,
                bees=args.bees,
                wall_clock_s=args.wall_clock,
            )
            print(json.dumps({"authorized": record["job_id"], "auth_id": record["auth_id"]}, indent=2))
            return 0

        if handler == "create":
            tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
            job = jobs.create_job(state_dir, job_id=job_id, tasks=tasks)
            print(json.dumps({"job_id": job["job_id"], "tasks": len(job["tasks"])}, indent=2))
            return 0

        if handler == "status":
            print(json.dumps(status(state_dir, job_id), indent=2, sort_keys=True))
            return 0

        if handler == "tick":
            client = client_factory()
            result = tick(state_dir, job_id, client=client, repo=Path(args.repo), gitleaks=args.gitleaks)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        if handler == "note":
            payload = acknowledge(state_dir, job_id, args.note)
            print(json.dumps({"acknowledged": payload["acknowledged_at"]}, indent=2))
            return 0

        if handler == "agent-pending":
            prompt = pending_prompt(state_dir, job_id)
            if prompt:
                print(prompt)
            return 0

        if handler == "report":
            print(json.dumps(report(state_dir, job_id), indent=2, sort_keys=True))
            return 0

        raise JobCliError("invalid_argument", "unknown job command", "Use authorize, create, status, tick, note, agent-pending or report.")
    except (JobCliError, jobs.JobError, reconcile.ReconcileError, verify_local.VerifyError) as exc:
        print(json.dumps({"error": getattr(exc, "code", "error"), "message": str(exc)}, indent=2))
        return 1
