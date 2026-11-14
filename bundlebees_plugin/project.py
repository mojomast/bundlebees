"""Project-level planning and completion for the rizzstack factory.

A project is an intake document plus the durable job that executes its feature
tasks. The planner is deliberately pure: it validates the intake, turns it into
the task shape consumed by :mod:`bundlebees_plugin.job`, and keeps the project
record separate from the job. The job remains the authority for execution
state; the project record never claims work is done because a task was merely
dispatched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "rizzstack.project-plan/1"
PROJECT_DIR = "projects"

_FEATURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_ACCEPTED = "accepted"
_BAD_TASK_STATES = {"failed", "rejected", "superseded"}


class ProjectError(Exception):
    """A stable refusal from project intake or completion."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectError("project_invalid", f"{field} must be a non-empty string")
    return value.strip()


def _path(value: Any, field: str) -> str:
    text = _text(value, field).replace("\\", "/")
    if text.startswith("/") or text.startswith("../") or "/../" in text or text == "..":
        raise ProjectError("project_lease_invalid", f"{field} must stay inside the repository")
    if any(part in ("", ".", "..") for part in text.split("/")):
        raise ProjectError("project_lease_invalid", f"{field} is not a clean relative path")
    return text


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _validate_acceptance(entries: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(entries, (list, tuple)) or not entries:
        raise ProjectError("project_acceptance_missing", f"{field} must be a non-empty list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ProjectError("project_acceptance_invalid", f"{field}[{index}] must be an object")
        criterion_id = _text(entry.get("id"), f"{field}[{index}].id")
        if criterion_id in seen:
            raise ProjectError("project_acceptance_duplicate", f"duplicate acceptance id {criterion_id!r}")
        seen.add(criterion_id)
        statement = _text(entry.get("statement"), f"{field}[{index}].statement")
        test = _text(entry.get("test"), f"{field}[{index}].test")
        result.append({"id": criterion_id, "statement": statement, "test": test})
    return result


def _feature_acceptance(entry: Mapping[str, Any], index: int) -> dict[str, Any]:
    raw = entry.get("acceptance")
    if raw is None:
        raise ProjectError("project_acceptance_missing", f"features[{index}] needs acceptance tests")
    if isinstance(raw, (list, tuple)):
        tests = [_text(item, f"features[{index}].acceptance") for item in raw]
        if not tests:
            raise ProjectError("project_acceptance_missing", f"features[{index}] has no acceptance tests")
        return {"tests": tests}
    if not isinstance(raw, Mapping):
        raise ProjectError("project_acceptance_invalid", f"features[{index}].acceptance must be a list or object")
    tests = raw.get("tests")
    if not isinstance(tests, (list, tuple)) or not tests:
        raise ProjectError("project_acceptance_missing", f"features[{index}].acceptance.tests is empty")
    criteria = raw.get("criteria")
    if criteria is not None and not isinstance(criteria, (list, tuple)):
        raise ProjectError("project_acceptance_invalid", f"features[{index}].acceptance.criteria must be a list")
    return {
        "tests": [_text(item, f"features[{index}].acceptance.tests") for item in tests],
        "criteria": list(criteria or []),
    }


def plan(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an intake document and return its deterministic task plan."""

    if not isinstance(spec, Mapping):
        raise ProjectError("project_invalid", "project spec must be an object")
    if spec.get("schema") != SCHEMA:
        raise ProjectError("project_invalid", f"project schema must be {SCHEMA}")
    project_id = _text(spec.get("project_id"), "project_id")
    if not _FEATURE_ID.match(project_id):
        raise ProjectError("project_invalid", "project_id must be a safe single path component")
    repo = _text(spec.get("repo"), "repo")
    if not _REPO.match(repo):
        raise ProjectError("project_invalid", "repo must be owner/name")
    base_rev = _text(spec.get("base_rev"), "base_rev").lower()
    if not _SHA.match(base_rev):
        raise ProjectError("project_invalid", "base_rev must be a full 40-hex commit")
    acceptance = _validate_acceptance(spec.get("acceptance"), "acceptance")
    features = spec.get("features")
    if not isinstance(features, (list, tuple)) or not features:
        raise ProjectError("project_invalid", "features must be a non-empty list")

    tasks: list[dict[str, Any]] = []
    ids: set[str] = set()
    leases: list[tuple[str, str]] = []
    dependencies: dict[str, list[str]] = {}
    for index, raw in enumerate(features):
        if not isinstance(raw, Mapping):
            raise ProjectError("project_invalid", f"features[{index}] must be an object")
        feature_id = _text(raw.get("feature_id"), f"features[{index}].feature_id")
        if not _FEATURE_ID.match(feature_id):
            raise ProjectError("project_invalid", f"feature id {feature_id!r} is not a safe path component")
        if feature_id in ids:
            raise ProjectError("project_duplicate_feature", f"duplicate feature id {feature_id!r}")
        ids.add(feature_id)
        objective = _text(raw.get("objective"), f"features[{index}].objective")
        raw_lease = raw.get("lease")
        if not isinstance(raw_lease, (list, tuple)) or not raw_lease:
            raise ProjectError("project_lease_invalid", f"features[{index}].lease must be non-empty")
        lease = [_path(item, f"features[{index}].lease") for item in raw_lease]
        for path in lease:
            for other_id, other_path in leases:
                if _overlap(path, other_path):
                    raise ProjectError(
                        "project_lease_overlap",
                        f"features {other_id!r} and {feature_id!r} overlap at {path!r}",
                        "Give each feature a disjoint lease before dispatch.",
                    )
            leases.append((feature_id, path))
        deps = raw.get("depends_on") or []
        if not isinstance(deps, (list, tuple)):
            raise ProjectError("project_invalid", f"features[{index}].depends_on must be a list")
        depends_on = [_text(dep, f"features[{index}].depends_on") for dep in deps]
        if len(set(depends_on)) != len(depends_on):
            raise ProjectError("project_duplicate_dependency", f"features[{index}] repeats a dependency")
        for dep in depends_on:
            if dep == feature_id:
                raise ProjectError("project_dependency_cycle", f"feature {feature_id!r} depends on itself")
        dependencies[feature_id] = depends_on
        tasks.append({
            "task_id": feature_id,
            "objective": objective,
            "lease": lease,
            "depends_on": depends_on,
            "acceptance": _feature_acceptance(raw, index),
        })

    for feature_id, deps in dependencies.items():
        for dep in deps:
            if dep not in ids:
                raise ProjectError("project_dependency_unknown", f"feature {feature_id!r} depends on unknown {dep!r}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ProjectError("project_dependency_cycle", f"dependency cycle includes {node!r}")
        if node in visited:
            return
        visiting.add(node)
        for dep in dependencies.get(node, []):
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for feature_id in dependencies:
        visit(feature_id)

    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "repo": repo,
        "base_rev": base_rev,
        "summary": str(spec.get("summary") or "").strip(),
        "acceptance": acceptance,
        "tasks": tasks,
    }


def load_plan(path: Path | str) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        spec = json.loads(raw)
    except OSError as exc:
        raise ProjectError("project_unreadable", f"cannot read project spec: {exc}") from exc
    except ValueError as exc:
        raise ProjectError("project_invalid", f"project spec is not valid JSON: {exc}") from exc
    return plan(spec)


def project_path(state_dir: Path | str, project_id: str) -> Path:
    if not _FEATURE_ID.match(str(project_id or "")):
        raise ProjectError("project_invalid", "project_id must be a safe single path component")
    return Path(state_dir) / PROJECT_DIR / f"{project_id}.json"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def create_project(state_dir: Path | str, spec: Mapping[str, Any] | Path | str, *, job_id: str, tasks: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Persist a validated project record after its job has been created.

    The caller owns job creation, because authorization is a separate human
    decision. This function refuses to overwrite an existing record; the job
    document remains the execution authority.
    """

    planned = plan(spec) if isinstance(spec, Mapping) else load_plan(spec)
    if tasks is None:
        task_list = planned["tasks"]
    else:
        task_list = [dict(item) for item in tasks]
    if [item.get("task_id") for item in task_list] != [item["task_id"] for item in planned["tasks"]]:
        raise ProjectError("project_plan_mismatch", "task list does not match the validated project plan")
    path = project_path(state_dir, planned["project_id"])
    if path.exists():
        raise ProjectError("project_exists", f"project {planned['project_id']!r} already exists")
    record = {
        "schema": SCHEMA,
        "project_id": planned["project_id"],
        "job_id": str(job_id),
        "repo": planned["repo"],
        "base_rev": planned["base_rev"],
        "summary": planned["summary"],
        "acceptance": planned["acceptance"],
        "tasks": task_list,
        "created_at": _now(),
    }
    _write_json(path, record)
    return record


def load_project(state_dir: Path | str, project_id: str) -> dict[str, Any] | None:
    path = project_path(state_dir, project_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectError("project_corrupt", f"project record {path.name} is unreadable: {exc}") from exc


def project_completion(job: Mapping[str, Any]) -> dict[str, Any]:
    """Derive project status from task states, never from a worker claim."""

    tasks = job.get("tasks")
    if not isinstance(tasks, (list, tuple)):
        raise ProjectError("project_job_invalid", "job has no task list")
    states = {str(task.get("task_id")): str(task.get("state") or "pending") for task in tasks if isinstance(task, Mapping)}
    accepted = [task_id for task_id, state in states.items() if state == _ACCEPTED]
    failed = [task_id for task_id, state in states.items() if state in _BAD_TASK_STATES]
    blocked: list[str] = []
    ready: list[str] = []
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        task_id = str(task.get("task_id"))
        if task_id in accepted or task_id in failed:
            continue
        deps = [str(dep) for dep in task.get("depends_on") or []]
        unmet = [dep for dep in deps if states.get(dep) != _ACCEPTED]
        if unmet:
            if any(states.get(dep) in _BAD_TASK_STATES or dep not in states for dep in unmet):
                blocked.append(task_id)
        else:
            ready.append(task_id)
    if len(accepted) == len(states) and states:
        status = "complete"
    elif failed:
        status = "failed"
    elif ready:
        status = "active"
    else:
        status = "blocked"
    return {
        "complete": status == "complete",
        "status": status,
        "next": ready,
        "blocked": blocked,
        "failed": failed,
        "accepted": accepted,
        "states": states,
    }


def project_status(state_dir: Path | str, project_id: str, job: Mapping[str, Any] | None = None) -> dict[str, Any]:
    record = load_project(state_dir, project_id)
    if record is None:
        raise ProjectError("project_missing", f"no project exists for {project_id!r}")
    if job is None:
        from . import job as jobs

        job = jobs.load_job(Path(state_dir), str(record["job_id"]))
    completion = project_completion(job)
    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "job_id": record["job_id"],
        "repo": record["repo"],
        "base_rev": record["base_rev"],
        "acceptance": record["acceptance"],
        "completion": completion,
    }
