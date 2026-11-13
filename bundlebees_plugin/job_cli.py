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

from . import integrate_job, job as jobs, leases, reconcile, verify_local
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


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def pending_prompt(
    state_dir: Path,
    job_id: str,
    *,
    now: Any = None,
    claim_timeout_s: float = 300.0,
) -> str | None:
    """Claim the pending prompt without deleting it.

    A claim is a lease, not a tombstone: if the agent turn that claimed it dies,
    the prompt becomes available again once ``claim_timeout_s`` has elapsed.
    """

    payload = read_handoff(state_dir, job_id)
    if not payload:
        return None
    clock = _epoch(now) if now is not None else time.time()
    status = payload.get("status")
    if status == "pending":
        pass
    elif status == "claimed":
        claimed = _epoch(payload.get("claimed_at"))
        if claimed is not None and clock - claimed <= float(claim_timeout_s):
            return None
        payload["reclaimed"] = int(payload.get("reclaimed") or 0) + 1
    else:
        return None
    payload.update(status="claimed", claimed_at=_now_at(clock), claimed_by=f"pid:{os.getpid()}")
    _write_handoff(state_dir, job_id, payload)
    return str(payload.get("prompt") or "")


def acknowledge(state_dir: Path, job_id: str, note: str, *, now: Any = None) -> dict[str, Any]:
    """Record the decision. Idempotent: a crash mid-write replays cleanly."""

    payload = read_handoff(state_dir, job_id)
    if not payload:
        raise JobCliError("no_handoff", "this job has no pending handoff", "Nothing to acknowledge.")
    text = str(note or "").strip()
    if not text:
        raise JobCliError("empty_note", "a decision needs a note", "Say what you decided and why.")

    clock = _epoch(now) if now is not None else time.time()
    already = payload.get("status") == "done"
    if not already:
        payload.update(status="done", acknowledged_at=_now_at(clock), decision=text)
        _write_handoff(state_dir, job_id, payload)

    # The event is written after the handoff, and only once, so a crash between
    # the two is repaired by replaying this call rather than duplicating it.
    job = jobs.load_job(state_dir, job_id)
    events = job.setdefault("events", [])
    marker = payload.get("acknowledged_at")
    if not any(e.get("transition") == "agent_ack" and e.get("at") == marker for e in events):
        events.append(
            {
                "at": marker or _now_at(clock),
                "task_id": "job",
                "transition": "agent_ack",
                "actor": "agent",
                "reason": str(payload.get("decision") or text),
            }
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
    require_gitleaks: bool = True,
    suite_command: Sequence[str] = ("python", "-m", "pytest", "-q"),
    now: Any = None,
    provisioner: Any = None,
    expect_host: str | None = None,
) -> dict[str, Any]:
    """One mechanical tick: provisioning phase, then ordinary reconcile."""

    binder = LeaseBinder(state_dir)
    job = jobs.load_job(state_dir, job_id)
    job = _hydrate_contracts(state_dir, job, repo)

    if provisioner is not None:
        from . import provision_bind

        outcome = provision_bind.advance(state_dir, job, provisioner=provisioner, expect_host=expect_host)
        job = jobs.load_job(state_dir, job_id)
        if outcome.get("blocked"):
            return {"status": "uncertain", "action": "provision", "transitions": [],
                    "detail": "provisioning outcome is uncertain; reconciled by receipt lookup, not re-POSTed",
                    "evidence": outcome, "exactly_once": False, "handoff": None}

    adapter = LauncherAdapter(client)
    from . import provision_bind

    bindings = provision_bind.bindings_for(job)
    if bindings:
        adapter = provision_bind.BoundLauncher(adapter, bindings)

    def verifier(ctx: Mapping[str, Any]) -> Mapping[str, Any]:
        from . import acdd_cli

        task = ctx.get("task") or {}
        attempt_id = str(ctx.get("attempt_id") or "")
        acceptance = task.get("acceptance") or {}
        tests = list(acceptance.get("tests") or []) if isinstance(acceptance, Mapping) else []

        # Preferred path: the capability-bound artifact API, which verifies the
        # patch sha256 and the result sha256 client-side.
        patch: str | None = None
        files: list[str] = []
        source = ""
        expected_sha: str | None = None

        artifact = getattr(client, "artifact", None)
        if callable(artifact):
            try:
                art = artifact(attempt_id)
            except Exception as exc:  # noqa: BLE001 - capability or integrity refusal
                return {"ok": False, "evidence": f"artifact refused: {exc}"}
            patch = art.get("patch") if isinstance(art, Mapping) else None
            source = str((art or {}).get("source_rev") or "")
            expected_sha = str((art or {}).get("patch_sha256") or "") or None
            files = _files_from_patch(patch or "")
        else:
            # Documented trust boundary: the launcher result is read with the
            # coordinator token, then every check below re-proves the artifact
            # (lease, source rev, sha256 when declared, files, tests, gitleaks).
            envelope = client.result_envelope(attempt_id)
            patch = _patch_text(envelope)
            source = str(((envelope.get("source") or {}).get("repo_rev")) or "")
            expected_sha = str(envelope.get("patch_sha256") or "") or None
            files = [
                str(item.get("path"))
                for item in ((envelope.get("patch") or {}).get("files") or [])
                if item.get("path")
            ]

        if not patch:
            return {"ok": False, "evidence": "no patch in the worker result"}

        lease_record = leases.load_lease(state_dir, attempt_id)
        declared = bool((lease_record.get("brief") or {}).get("contract"))

        def preflight(patch_files: list[str]) -> object:
            return acdd_cli.verify_preflight(lease_record, state_dir, patch_files)

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
                require_gitleaks=require_gitleaks,
                expected_patch_sha256=expected_sha,
                acdd_preflight=preflight,
                contract_declared=declared,
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


def _now_at(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


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


def _files_from_patch(patch: str) -> list[str]:
    found: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                if path not in found:
                    found.append(path)
    return found


def _hydrate_contracts(state_dir: Path, job: dict[str, Any], repo: Path) -> dict[str, Any]:
    """Load each task's frozen contract from its path and verify its sha256.

    A task that declares a contract must never reach dispatch without it: that
    would silently drop the contract checks and fall back to legacy mode.
    """

    changed = False
    for task in job.get("tasks", []):
        path = task.get("contract_path")
        if not path:
            continue
        source = Path(repo) / str(path)
        try:
            raw_bytes = source.read_bytes()
        except OSError as exc:
            raise JobCliError(
                "contract_unavailable",
                f"task {task.get('task_id')} contract {path} cannot be read: {exc}",
                "Provide the frozen contract at the recorded path before dispatching.",
            ) from exc
        digest = __import__("hashlib").sha256(raw_bytes).hexdigest()
        expected = task.get("contract_sha256")
        if expected and str(expected) != digest:
            raise JobCliError(
                "contract_mismatch",
                f"task {task.get('task_id')} contract sha256 {digest[:12]} != declared {str(expected)[:12]}",
                "Restore the frozen contract; do not dispatch against a changed one.",
            )
        try:
            task["contract"] = json.loads(raw_bytes)
        except ValueError as exc:
            raise JobCliError(
                "contract_invalid",
                f"task {task.get('task_id')} contract is not valid JSON: {exc}",
                "Fix the frozen contract file.",
            ) from exc
        changed = True
    if changed:
        jobs.save_job(state_dir, job)
    return job
