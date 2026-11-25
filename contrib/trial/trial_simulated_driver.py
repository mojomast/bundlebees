"""REHEARSAL ONLY: a simulated coordinator for the trial harness.

This module is **not** a coordinator and must never run against a real
coordinator state directory. It exists so the restart harness can exercise its
own process, deadline, downtime and evidence paths offline, with a child
process that is started, recorded and signalled exactly like the real driver.

Safety belt
-----------
It refuses to run unless ``<state>/trial-rehearsal.marker`` exists, which only
:func:`contrib.trial.trial_harness.build_rehearsal_state` writes. It never
contacts a launcher, never dispatches to a worker, never runs git and never
touches a repository. Its "verification" and "integration" are simulated by
writing the same durable record shapes the real modules write, so the harness's
readers are exercised; the report always carries ``rehearsal: true``.

Passes
------
``--pass 1``
    Simulate one dispatch (intent record, task state ``dispatched``, one attempt
    id), write ``driver-1.ready``, then stay alive until it is signalled. If the
    job already has an attempt it refuses: a rehearsal restart never dispatches
    twice.
``--pass 2``
    Adopt the existing attempt (refusing to dispatch again), require the
    simulated worker result written during the downtime, record verification and
    one accepted wave, reconcile the campaign, write ``driver-2.ready`` and exit
    0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bundlebees_plugin import campaign as campaign_mod
from bundlebees_plugin import job as jobs
from bundlebees_plugin import queue as queue_mod
from bundlebees_plugin import reconcile as reconcile_mod
from bundlebees_plugin import revision

MARKER = "trial-rehearsal.marker"
SHA = "b" * 40
DEFAULT_ATTEMPT = "att-rehearsal-1"


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _refuse(code: str, message: str) -> None:
    print(json.dumps({"status": "refused", "error": code, "message": message}), file=sys.stderr)
    raise SystemExit(3)


def _job(state: Path, job_id: str) -> dict[str, Any]:
    return jobs.load_job(state, job_id, recover=False)


def _attempts(job: dict[str, Any]) -> list[str]:
    return [str(item) for task in job.get("tasks") or [] for item in (task.get("attempts") or [])]


def _one_task(job: dict[str, Any]) -> dict[str, Any]:
    tasks = [task for task in job.get("tasks") or [] if isinstance(task, dict)]
    if len(tasks) != 1:
        _refuse("rehearsal_single_task_only", f"the rehearsal needs exactly one task, found {len(tasks)}")
    return tasks[0]


def pass_one(state: Path, job_id: str, trial_dir: Path, attempt_id: str) -> int:
    job = _job(state, job_id)
    existing = _attempts(job)
    if existing:
        _refuse("dispatch_refused_existing_attempt", f"job already records attempts {existing!r}")
    task = _one_task(job)
    task_id = str(task["task_id"])
    attempt_no = 1
    intent_path = reconcile_mod.intent_path(state, job_id, task_id, attempt_no)
    jobs.transition(job, task_id, "dispatching", actor="trial-simulated-driver", reason="rehearsal dispatch")
    jobs.transition(job, task_id, "dispatched", actor="trial-simulated-driver", reason="rehearsal dispatch")
    task["attempts"] = [attempt_id]
    jobs.save_job(state, job)
    _write_json(
        intent_path,
        {
            "schema": "bundlebees.dispatch-intent/1",
            "task_id": task_id,
            "attempt_no": attempt_no,
            "idempotency_key": "rehearsal-" + attempt_id,
            "status": reconcile_mod.INTENT_DISPATCHED,
            "attempt_id": attempt_id,
            "written_at": time.monotonic(),
            "fingerprint": {
                "job_id": job_id,
                "task_id": task_id,
                "source_rev": str((job.get("goal") or {}).get("base_rev") or ""),
                "rehearsal": True,
            },
        },
    )
    _write_json(
        trial_dir / "pass-1.json",
        {
            "pass": 1,
            "job_id": job_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "intent": str(intent_path),
            "state": "dispatched",
            "rehearsal": True,
        },
    )
    (trial_dir / "driver-1.ready").write_text("ready\n", encoding="utf-8")
    # Stay resident exactly like `factory_driver --interval`. A default SIGTERM
    # terminates this process; the harness records and confirms that exit.
    while True:
        time.sleep(0.02)


def pass_two(state: Path, job_id: str, campaign_id: str, trial_dir: Path, attempt_id: str) -> int:
    job = _job(state, job_id)
    attempts = _attempts(job)
    if attempts != [attempt_id]:
        _refuse(
            "dispatch_refused_existing_attempt",
            f"pass 2 must adopt exactly [{attempt_id!r}], found {attempts!r}; it will not dispatch",
        )
    worker_result = trial_dir / "worker-result.json"
    if not worker_result.exists():
        _refuse("worker_result_missing", f"no simulated worker result at {worker_result}")
    result = json.loads(worker_result.read_text(encoding="utf-8"))
    if str(result.get("attempt_id")) != attempt_id or str(result.get("state")) != "succeeded":
        _refuse("worker_result_unusable", f"the simulated worker result is not a success for {attempt_id}")
    task = _one_task(job)
    task_id = str(task["task_id"])
    if str(task.get("state")) != "dispatched":
        _refuse("unexpected_task_state", f"task {task_id} is {task.get('state')!r}, expected 'dispatched'")

    # Simulated worker outcome, recorded in the real verifier's record shape and
    # location. It is not a real verification; the report says so.
    verify_path = state / "verify-results" / f"{attempt_id}.json"
    _write_json(
        verify_path,
        {
            "ok": True,
            "sha": SHA,
            "base_rev": str((job.get("goal") or {}).get("base_rev") or ""),
            "gitleaks": {"exit_code": 0},
            "suite": {"exit_code": 0},
            "rehearsal": True,
            "note": "simulated verification for the local restart rehearsal; no git or suite ran",
        },
    )
    for step in ("running", "verifying", "verified"):
        jobs.transition(job, task_id, step, actor="trial-simulated-driver", reason="rehearsal verification")

    integration = job.setdefault("integration", {"wave": 1})
    wave_number = 1
    wave_id = revision.wave_id_for(job_id, wave_number)
    # The real create-only writer, so the record's own semantics (accepted,
    # never overwritten) are part of the rehearsal too.
    revision._write_wave(  # noqa: SLF001 - rehearsal uses the real durable writer
        state,
        wave_id,
        {
            "wave_id": wave_id,
            "accepted": True,
            "head_sha": SHA,
            "attempts": [attempt_id],
            "problems": {},
            "at": _iso(),
            "rehearsal": True,
            "note": "simulated integration for the local restart rehearsal; no git or suite ran",
        },
    )
    integration.update(
        attempts=1,
        evidence="rehearsal: simulated integration",
        state="pending",
        last_commit=SHA,
        accepted_waves=[{"wave": wave_number, "at": _iso(), "evidence": "rehearsal: simulated integration",
                         "commit": SHA}],
        wave=wave_number + 1,
    )
    jobs.transition(job, task_id, "integrating", actor="trial-simulated-driver", reason="rehearsal wave accepted")
    jobs.transition(job, task_id, "accepted", actor="trial-simulated-driver", reason="rehearsal wave accepted")
    jobs.save_job(state, job)

    accepted = queue_mod.job_accepted_reasons(state, job_id)
    campaign_record = None
    campaign_plan = None
    try:
        campaign_record = campaign_mod.reconcile(state, campaign_id)
        campaign_plan = campaign_mod.resume_plan(state, campaign_id)
    except campaign_mod.CampaignError as exc:
        _refuse("campaign_unreadable", f"campaign reconcile failed: {exc.code}: {exc}")
    if accepted or (campaign_plan or {}).get("phase") != "job_complete":
        _refuse(
            "rehearsal_did_not_complete",
            f"job acceptance reasons={accepted!r} campaign_phase={(campaign_plan or {}).get('phase')!r}",
        )
    _write_json(
        trial_dir / "pass-2.json",
        {
            "pass": 2,
            "job_id": job_id,
            "attempt_id": attempt_id,
            "adopted": True,
            "dispatched": False,
            "wave_id": wave_id,
            "campaign_phase": (campaign_record or {}).get("phase"),
            "job_accepted": True,
            "rehearsal": True,
        },
    )
    (trial_dir / "driver-2.ready").write_text("done\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bundlebees-trial-simulated-driver")
    parser.add_argument("--state", required=True)
    parser.add_argument("--trial-dir", required=True)
    parser.add_argument("--job", default="trial-job")
    parser.add_argument("--campaign", default="trial-campaign")
    parser.add_argument("--pass", dest="pass_number", type=int, choices=(1, 2), default=1)
    parser.add_argument("--attempt-id", default=DEFAULT_ATTEMPT)
    args = parser.parse_args(argv)

    state = Path(args.state)
    trial_dir = Path(args.trial_dir)
    if not (state / MARKER).is_file():
        _refuse("rehearsal_marker_missing", f"{state} is not a rehearsal state directory ({MARKER} absent)")
    if not trial_dir.is_dir():
        _refuse("trial_dir_missing", f"{trial_dir} does not exist")
    job_id = str(args.job)
    try:
        deadline = time.monotonic() + 120.0
        while not (state / jobs.JOBS_DIR / f"{job_id}.json").exists():
            if time.monotonic() > deadline:
                _refuse("job_missing", f"no rehearsal job at {state / jobs.JOBS_DIR / (job_id + '.json')}")
            time.sleep(0.01)
        if args.pass_number == 1:
            return pass_one(state, job_id, trial_dir, args.attempt_id)
        return pass_two(state, job_id, str(args.campaign), trial_dir, args.attempt_id)
    except jobs.JobError as exc:
        _refuse("job_refused", f"{exc.code}: {exc}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
