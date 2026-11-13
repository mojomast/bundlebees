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

    outcomes: list[dict[str, Any]] = []
    changed = False

    for task in job.get("tasks") or []:
        spec = _spec(task, job)
        if spec is None:
            continue
        if isinstance(task.get("binding"), Mapping) and task["binding"].get("receipt_digest"):
            continue

        key = stable_key(str(job["job_id"]), str(task["task_id"]))
        intent = task.get("provisioning")
        if not isinstance(intent, Mapping):
            intent = {"state": "requested", "key": key, "request": request_for(job, task, spec, key), "attempts": 0}
            task["provisioning"] = intent
            changed = True
            jobs.save_job(state_dir, job)

        request = intent.get("request") or request_for(job, task, spec, key)
        intent["state"] = "preparing"
        intent["attempts"] = int(intent.get("attempts") or 0) + 1
        jobs.save_job(state_dir, job)

        receipt = None
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

    def __init__(self, inner: Any, bindings: Mapping[str, str]) -> None:
        self._inner = inner
        self._bindings = dict(bindings)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def post_attempt(self, brief: Mapping[str, Any]) -> Mapping[str, Any]:
        task_id = str(brief.get("task_id") or "")
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
