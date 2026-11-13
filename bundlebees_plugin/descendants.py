"""Bounded descendant-revision authorization.

M1 authorizes an exact baseline. A dependent feature's baseline is not known until
its dependency is integrated, so a human may authorize a *rule* once:

    same repository, a descendant of the authorized root, equal to an accepted
    integration commit of named dependencies, same scope, same budget.

The coordinator may resolve that rule to a concrete SHA from verified evidence and
never widen it. Anything else stops at needs_approval.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import job as jobs

POLICY = "rizzstack.descendant-authorization/1"


class DescendantError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def _safe_sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise DescendantError("revision_invalid", f"{value!r} is not a full 40-hex commit", "Pin an exact revision.")
    return text


@dataclass(frozen=True)
class DescendantPolicy:
    root_rev: str
    repo: str
    scopes: tuple[str, ...]
    max_bees: int
    max_wall_clock_s: int
    dependencies: tuple[str, ...]

    @classmethod
    def parse(cls, authorization: Mapping[str, Any]) -> "DescendantPolicy | None":
        block = authorization.get("descendant")
        if not isinstance(block, Mapping):
            return None
        if block.get("policy") != POLICY:
            raise DescendantError("policy_unsupported", f"unknown descendant policy {block.get('policy')!r}", "Enroll the supported policy.")
        dependencies = tuple(str(item) for item in block.get("dependencies") or ())
        if not dependencies:
            raise DescendantError("policy_incomplete", "a descendant policy must name its dependencies", "Name the jobs whose accepted integration may become the baseline.")
        return cls(
            root_rev=_safe_sha(authorization.get("base_rev")),
            repo=str(authorization.get("repo") or ""),
            scopes=tuple(str(item) for item in authorization.get("scope") or ()),
            max_bees=int((authorization.get("budget") or {}).get("bees") or 0),
            max_wall_clock_s=int((authorization.get("budget") or {}).get("wall_clock_s") or 0),
            dependencies=dependencies,
        )


def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_TERMINAL_PROMPT": "0", "HOME": str(Path.home())})


def accepted_commit(state_dir: Path, dependency_job: str) -> str:
    """The exact accepted integration commit of a dependency, from its job record."""
    try:
        job = jobs.load_job(Path(state_dir), dependency_job)
    except Exception as exc:  # noqa: BLE001
        raise DescendantError("dependency_unknown", f"no job {dependency_job}: {exc}", "Integrate the dependency first.") from exc
    integration = job.get("integration") or {}
    waves = [w for w in integration.get("accepted_waves") or [] if isinstance(w, Mapping)]
    commit = integration.get("last_commit") or (waves[-1].get("commit") if waves else "")
    if not commit:
        raise DescendantError("dependency_not_accepted", f"{dependency_job} has no accepted integration commit",
                              "Wait for its integration; a card status is not evidence.")
    return _safe_sha(commit)


def derive(policy: DescendantPolicy, *, candidate_rev: str, repo: str, worktree: Path,
           assigned_files: Iterable[str], state_dir: Path, bees: int, wall_clock_s: int) -> dict[str, Any]:
    """Validate the rule against reality and return the derived authorization."""

    candidate = _safe_sha(candidate_rev)
    if repo != policy.repo:
        raise DescendantError("repo_mismatch", f"{repo} is not the authorized repository {policy.repo}", "A descendant may not change repository.")
    if bees > policy.max_bees or bees < 1:
        raise DescendantError("budget_exceeded", f"bees={bees} exceeds the authorized {policy.max_bees}", "Stay inside the envelope.")
    if wall_clock_s > policy.max_wall_clock_s or wall_clock_s < 1:
        raise DescendantError("budget_exceeded", f"wall_clock_s={wall_clock_s} exceeds {policy.max_wall_clock_s}", "Stay inside the envelope.")
    files = sorted(str(item) for item in assigned_files)
    if not files:
        raise DescendantError("scope_empty", "a task needs at least one leased file", "Declare the scope.")
    outside = [path for path in files if not any(path == s or path.startswith(s.rstrip("*").rstrip("/") + "/") or (s.endswith("*") and path.startswith(s[:-1])) for s in policy.scopes)]
    if policy.scopes and outside:
        raise DescendantError("scope_widened", f"files outside the authorized scope: {', '.join(outside)}", "Do not widen scope in a descendant job.")

    ancestor = _git(["merge-base", "--is-ancestor", policy.root_rev, candidate], Path(worktree))
    if ancestor.returncode != 0:
        raise DescendantError("not_a_descendant", f"{candidate} is not a descendant of {policy.root_rev}", "Refuse: the derived baseline must descend from the authorized root.")

    proven: dict[str, str] = {}
    for dependency in policy.dependencies:
        accepted = accepted_commit(Path(state_dir), dependency)
        if accepted != candidate:
            raise DescendantError("lineage_mismatch", f"{candidate} is not {dependency}'s accepted commit {accepted}",
                                  "Use the exact accepted integration commit, or ask the human for a new authorization.")
        proven[dependency] = accepted

    return {
        "base_rev": candidate,
        "repo": repo,
        "scope": files,
        "budget": {"bees": bees, "wall_clock_s": wall_clock_s},
        "descendant": {"policy": POLICY, "root_rev": policy.root_rev, "dependencies": list(policy.dependencies)},
        "lineage": proven,
    }
