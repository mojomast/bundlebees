"""Sealed descendant policies and the production A→B handoff.

A human enrolls one bounded policy before the run: repository, root revision,
dependency jobs, permitted scope, budget, gates and an approved contract template.
The coordinator may verify and spend that policy, and may never create or widen it.

The handoff is production code: the queue discovers the dependent feature, reads
the sealed policy, validates the dependency's accepted evidence, publishes the
accepted commit, derives the exact revision, records a create-only derived
authorization and creates the dependent job.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import contract as contract_mod
from . import descendants, job as jobs, transport


class HandoffError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


POLICY_KIND = "descendant-policy"


def policy_path(state_dir: Path, policy_id: str) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in policy_id)
    return Path(state_dir) / "policies" / f"{safe}.json"


def enroll_policy(state_dir: Path, *, policy_id: str, statement: str, repo: str, root_rev: str,
                  dependencies: Sequence[str], scope: Sequence[str], bees: int, wall_clock_s: int,
                  contract_template: Mapping[str, Any], gates: Sequence[str] = ("merge",),
                  host: str = "", profile: str = "", project: str = "", workflow_ref: str = "") -> Path:
    """Human enrollment. Create-only: an existing policy id is never replaced."""
    if not str(statement).strip():
        raise HandoffError("statement_required", "enrolling a policy needs the human's own statement")
    key = jobs._ensure_key(Path(state_dir))
    body = {
        "kind": POLICY_KIND, "policy_id": policy_id, "statement": statement, "repo": repo,
        "root_rev": root_rev, "dependencies": [str(item) for item in dependencies],
        "scope": [str(item) for item in scope],
        "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
        "contract_template": copy.deepcopy(dict(contract_template)),
        "gates": sorted({str(g) for g in gates}) or ["merge"],
        "host": host, "profile": profile, "project": project, "workflow_ref": workflow_ref,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = policy_path(state_dir, policy_id)
    if path.exists():
        raise HandoffError("policy_exists", f"policy {policy_id} is already enrolled", "Revoke it deliberately; do not overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(jobs._seal(body, key), indent=2, sort_keys=True).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def load_policy(state_dir: Path, policy_id: str) -> dict[str, Any]:
    key = jobs._load_key(Path(state_dir))
    path = policy_path(state_dir, policy_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HandoffError("policy_missing", f"no enrolled policy {policy_id}", "Enroll it before unattended execution.") from exc
    except (OSError, ValueError) as exc:
        raise HandoffError("policy_unreadable", f"policy {policy_id}: {exc}", "Refuse to proceed.") from exc
    body = jobs.open_sealed(raw, key, "descendant policy")
    if body.get("kind") != POLICY_KIND:
        raise HandoffError("policy_invalid", f"{policy_id} is not a descendant policy", "Fix the enrollment.")
    return body


def find_policy_for_dependency(state_dir: Path, dependency: str) -> dict[str, Any] | None:
    folder = Path(state_dir) / "policies"
    if not folder.exists():
        return None
    for path in sorted(folder.glob("*.json")):
        try:
            body = load_policy(state_dir, path.stem)
        except HandoffError:
            continue
        if dependency in body.get("dependencies") or []:
            return body
    return None


def materialize(state_dir: Path, *, feature_id: str, job_id: str, policy: Mapping[str, Any],
                worktree: Path, mirror: Path, repo_identity: str) -> dict[str, Any]:
    """Create the dependent job from a sealed policy. Create-only and idempotent."""

    existing = None
    try:
        existing = jobs.load_job(Path(state_dir), job_id)
    except Exception:  # noqa: BLE001 - absent job is the normal case
        existing = None
    if existing is not None:
        return {"job_id": job_id, "created": False}

    repo = str(policy.get("repo") or "")
    if repo_identity and repo != repo_identity:
        raise HandoffError("policy_repo_mismatch", f"policy is for {repo}, not {repo_identity}", "Refuse.")
    dependency = str((policy.get("dependencies") or [""])[0])

    accepted, _attempts = transport.accepted_commit(Path(state_dir), jobs.load_job(Path(state_dir), dependency))
    receipt = transport.publish(Path(state_dir), dependency, repo=Path(worktree), mirror=Path(mirror),
                                repo_identity=repo, root_rev=str(policy.get("root_rev") or ""))
    if receipt["source_sha"] != accepted:
        raise HandoffError("publication_mismatch", "the published commit is not the accepted commit", "Refuse.")

    rule = descendants.DescendantPolicy.parse({
        "repo": repo, "base_rev": policy["root_rev"], "scope": policy["scope"],
        "budget": policy["budget"],
        "descendant": {"policy": descendants.POLICY, "dependencies": policy["dependencies"]},
    })
    assert rule is not None
    derived = descendants.derive(rule, candidate_rev=accepted, repo=repo, worktree=Path(worktree),
                                 assigned_files=policy["scope"], state_dir=Path(state_dir),
                                 bees=int(policy["budget"]["bees"]),
                                 wall_clock_s=int(policy["budget"]["wall_clock_s"]))

    body = copy.deepcopy(dict(policy["contract_template"]))
    body["source_rev"] = derived["base_rev"]
    frozen = contract_mod.freeze_contract(body, approved_by=f"descendant-policy:{policy['policy_id']}",
                                          approved_at=str(policy.get("at")))
    digest = contract_mod.check_frozen(frozen)

    task_id = f"T-{feature_id}"
    jobs.record_derived_authorization(
        Path(state_dir), job_id=job_id, parent_job_id=dependency,
        statement=f"{feature_id} under policy {policy['policy_id']}", repo=repo,
        candidate_rev=derived["base_rev"], scope=policy["scope"],
        bees=int(policy["budget"]["bees"]), wall_clock_s=int(policy["budget"]["wall_clock_s"]),
        dependencies=policy["dependencies"], root_rev=policy["root_rev"],
        policy_scope=policy["scope"], gates=policy.get("gates") or ("merge",),
    )
    jobs.create_job(Path(state_dir), job_id=job_id, summary=str(policy.get("statement") or feature_id),
                    workflow_ref=str(policy.get("workflow_ref") or ""), tasks=[{
                        "task_id": task_id,
                        "objective": str(frozen.get("title") or feature_id),
                        "lease": list(policy["scope"]),
                        "acceptance": {"criteria": [c.get("statement") for c in frozen.get("acceptance_criteria") or []],
                                       "tests": [t for c in frozen.get("acceptance_criteria") or [] for t in c.get("tests") or []],
                                       "commands": ["python -m pytest -q"]},
                        "contract": frozen, "contract_sha256": digest,
                        "provision": {"repo": repo, "source_rev": derived["base_rev"],
                                      "host": str(policy.get("host") or ""), "profile": str(policy.get("profile") or ""),
                                      "project": str(policy.get("project") or f"smoke-{feature_id}"),
                                      "contract_sha256": digest},
                    }])
    return {"job_id": job_id, "created": True, "source_rev": derived["base_rev"],
            "mirror_ref": receipt["mirror_ref"], "policy_id": policy["policy_id"],
            "contract_sha256": digest}
