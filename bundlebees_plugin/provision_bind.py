"""Provisioning phase of the existing job tick (M3.5 vertical slice).

One job, one missing worker project, one automatic provisioning operation, then
the ordinary reconciler dispatches. No second scheduler and no second job store:
the intent and the binding live inside the job document that M1 already owns.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import job as jobs
from . import provisioning as P

import contextlib
import os
import time


class ProvisionBindError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def stable_key(job_id: str, task_id: str) -> str:
    """uuid5 over the immutable identity: the same job/task always reuses it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rizzstack-provision:{job_id}:{task_id}"))


def _spec(task: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any] | None:
    spec = task.get("provision")
    if not isinstance(spec, Mapping):
        return None
    return {
        "repo": str(spec.get("repo") or job.get("repo") or ""),
        "source_rev": str(spec.get("source_rev") or job.get("base_rev") or ""),
        "host": str(spec.get("host") or ""),
        "profile": str(spec.get("profile") or ""),
        "project": str(spec.get("project") or ""),
        "contract_sha256": str(spec.get("contract_sha256") or task.get("contract_sha256") or ""),
    }


def request_for(job: Mapping[str, Any], task: Mapping[str, Any], spec: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {
        "repo": spec["repo"],
        "source_rev": spec["source_rev"],
        "profile": spec["profile"],
        "project": spec["project"],
        "job_id": str(job["job_id"]),
        "idempotency_key": key,
    }


@contextlib.contextmanager
def _tick_lock(state_dir: Path, job_id: str):
    """One provisioning advance per job at a time; a held lock is not a crash."""
    directory = Path(state_dir) / "provisioning"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{job_id}.lock"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ProvisionBindError("tick_in_progress", f"another tick is provisioning {job_id}", "Let it finish; do not duplicate the request.")
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            path.unlink()


def _authorization_matches(state_dir: Path, job: Mapping[str, Any], spec: Mapping[str, Any]) -> None:
    """A provisioning spec may narrow M1 authority, never widen it."""
    authorization = jobs.load_authorization(state_dir, str(job["job_id"]))
    goal = job.get("goal") or {}
    allowed_repos = {str(authorization.get("repo") or ""), str(goal.get("repo") or "")} - {""}
    allowed_revs = {str(authorization.get("base_rev") or ""), str(goal.get("base_rev") or "")} - {""}
    if spec["repo"] not in allowed_repos:
        raise ProvisionBindError("provisioning_repo_not_authorized", f"{spec['repo']} is not the authorized repository", "Fix the job; the spec cannot widen authority.")
    if spec["source_rev"] not in allowed_revs:
        raise ProvisionBindError("provisioning_rev_not_authorized", f"{spec['source_rev']} is not the authorized revision", "Pin the authorized revision.")
    scope = {str(item) for item in authorization.get("scope") or []}
    files = {str(item) for item in task_files(job, spec)}
    if scope and not files <= scope:
        raise ProvisionBindError("provisioning_scope_not_authorized", f"leased files outside the authorization: {sorted(files - scope)}", "Narrow the task.")


def task_files(job: Mapping[str, Any], spec: Mapping[str, Any]) -> list[str]:
    files: list[str] = []
    for task in job.get("tasks") or []:
        if _spec(task, job) and _spec(task, job)["project"] == spec["project"]:
            files.extend(str(item) for item in task.get("lease") or [])
    return files


def advance(
    state_dir: Path,
    job: dict[str, Any],
    *,
    provisioner: Any,
    expect_host: str | None = None,
) -> dict[str, Any]:
    """Advance every task that needs provisioning. Returns a compact outcome.

    The intent is persisted before the POST. An uncertain POST is reconciled with
    the read-only receipt lookup; it is never repeated blindly.
    """

    with _tick_lock(state_dir, str(job["job_id"])):
        return _advance_locked(state_dir, job, provisioner=provisioner, expect_host=expect_host)


def _advance_locked(
    state_dir: Path,
    job: dict[str, Any],
    *,
    provisioner: Any,
    expect_host: str | None = None,
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    changed = False

    for task in job.get("tasks") or []:
        spec = _spec(task, job)
        if spec is None:
            continue
        if isinstance(task.get("binding"), Mapping) and task["binding"].get("receipt_digest"):
            continue

        _authorization_matches(state_dir, job, spec)
        key = stable_key(str(job["job_id"]), str(task["task_id"]))
        intent = task.get("provisioning")
        if not isinstance(intent, Mapping):
            intent = {"state": "requested", "key": key, "request": request_for(job, task, spec, key), "attempts": 0}
            task["provisioning"] = intent
            changed = True
            jobs.save_job(state_dir, job)

        request = intent.get("request") or request_for(job, task, spec, key)
        prior_state = str(intent.get("state") or "requested")
        receipt = None

        if prior_state in ("preparing", "uncertain", "ready"):
            # The POST may already have landed. Reconcile read-only; never re-POST.
            intent["state"] = "uncertain" if prior_state != "ready" else "ready"
            jobs.save_job(state_dir, job)
            try:
                receipt = provisioner.authoritative_receipt(
                    key,
                    expect_repo=spec["repo"], expect_rev=spec["source_rev"],
                    expect_profile=spec["profile"], expect_project=spec["project"],
                    expect_host=expect_host or spec["host"] or None,
                )
            except P.ProvisionError as lookup_exc:
                intent.update(state="uncertain", detail=f"reconciled without a POST: {lookup_exc}")
                jobs.save_job(state_dir, job)
                outcomes.append({"task_id": task["task_id"], "state": "uncertain", "detail": intent["detail"]})
                continue
        else:
            intent["state"] = "preparing"
            intent["attempts"] = int(intent.get("attempts") or 0) + 1
            jobs.save_job(state_dir, job)
            try:
                receipt = provisioner.provision(request)
                intent["state"] = "ready"
            except P.ProvisionUncertain as exc:
                intent.update(state="uncertain", detail=str(exc))
                jobs.save_job(state_dir, job)
                try:
                    receipt = provisioner.authoritative_receipt(
                        key,
                        expect_repo=spec["repo"], expect_rev=spec["source_rev"],
                        expect_profile=spec["profile"], expect_project=spec["project"],
                        expect_host=expect_host or spec["host"] or None,
                    )
                except P.ProvisionError as lookup_exc:
                    intent["detail"] = f"{exc}; lookup: {lookup_exc}"
                    jobs.save_job(state_dir, job)
                    outcomes.append({"task_id": task["task_id"], "state": "uncertain", "detail": intent["detail"]})
                    continue

        binding = P.Binding(
            repo=spec["repo"], source_rev=spec["source_rev"], host=spec["host"] or receipt.host,
            profile=spec["profile"], project=spec["project"], receipt_digest=receipt.digest,
            contract_sha256=spec["contract_sha256"], workspace=receipt.workspace,
        )
        try:
            binding.validate(receipt, contract_sha256=spec["contract_sha256"])
        except P.ProvisionError as exc:
            intent.update(state="denied", detail=f"{exc.code}: {exc.message}")
            jobs.save_job(state_dir, job)
            raise ProvisionBindError("binding_refused", f"{exc.code}: {exc.message}", "Do not dispatch; inspect the host.") from exc

        task["binding"] = binding.to_dict()
        task["provisioning"] = dict(intent, state="bound", receipt_digest=receipt.digest,
                                    project=receipt.project, workspace=receipt.workspace)
        changed = True
        jobs.save_job(state_dir, job)
        outcomes.append({"task_id": task["task_id"], "state": "bound", "project": receipt.project,
                         "receipt_digest": receipt.digest})

    return {"changed": changed, "outcomes": outcomes,
            "blocked": any(item["state"] == "uncertain" for item in outcomes)}


class BoundLauncher:
    """Pre-dispatch seam: every POST must carry the bound launcher project.

    A task that requires provisioning but has no binding, or a brief whose project
    differs from the bound identity, is refused before it reaches the launcher.
    """

    def __init__(self, inner: Any, bindings: Mapping[str, str],
                 validators: Mapping[str, Any] | None = None) -> None:
        self._inner = inner
        self._bindings = dict(bindings)
        self._validators = dict(validators or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def post_attempt(self, brief: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id = str(brief.get("task_id") or "")
        validator = self._validators.get(task_id)
        if validator is not None:
            validator()   # authoritative receipt revalidation, immediately pre-POST
        bound = self._bindings.get(task_id)
        project = str(brief.get("project") or "")
        if bound is None:
            raise P.ProvisionDenied("binding_missing", f"task {task_id} has no provisioned binding", "Provision before dispatch.")
        if project != bound:
            raise P.ProvisionDenied("binding_project", f"brief project {project!r} is not the bound {bound!r}", "Stop: dispatch would target another project.")
        if str(brief.get("source_rev") or "") != str(self._bindings.get(task_id + ":rev") or brief.get("source_rev")):
            raise P.ProvisionDenied("binding_revision", "brief revision changed", "Stop.")
        return self._inner.post_attempt(brief)


def bindings_for(job: Mapping[str, Any]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for task in job.get("tasks") or []:
        binding = task.get("binding")
        if isinstance(binding, Mapping) and binding.get("project"):
            bindings[str(task["task_id"])] = str(binding["project"])
            bindings[str(task["task_id"]) + ":rev"] = str(binding.get("source_rev") or "")
    return bindings
