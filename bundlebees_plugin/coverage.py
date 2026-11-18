"""Pre-dispatch coverage reconciliation (rizzstack.coverage/1).

The problem this solves (2026-11-17): a durable queue can hold work that some other
development effort has already implemented in a draft PR. Re-dispatching it wastes
workers and produces conflicting branches; silently dropping it lies about the
record. The queue must converge on reality without pretending a draft is a landing.

The distinction that matters:

* ``LANDED`` — implemented AND merged into the authoritative branch. Evidence kept,
  never redispatched.
* ``COVERED_BY_DRAFT`` — implemented and identifiable in an OPEN/DRAFT pull request,
  **not merged**. We record the PR, the exact commit and whatever evidence the draft
  carries, so the work is not repeated — but this is NOT acceptance: the draft still
  needs validation, review and a merge. Never reported as done.
* ``PARTIAL`` — the draft covers some of the task's leased files; only the remainder
  is dispatchable, with an explicit dependency on the existing implementation.
* ``IN_FLIGHT`` — a live attempt owns the lease; preserved, never duplicated.
* ``MISSING`` — nothing equivalent exists; eligible after the usual gates.
* ``SUPERSEDED`` — replaced by a newer accepted contract/implementation.
* ``BLOCKED`` — a real dependency prevents work; carries the blocker and next action.

Classification is contract-level, not title-level: coverage is decided by the task's
leased file set and its declared tests against the candidate's changed files, and a
candidate's ``merged`` flag decides LANDED vs COVERED_BY_DRAFT. Pure function, no
I/O: the CLI feeds it, tests drive it, and running it twice on the same inputs
produces byte-identical output (idempotent reconciliation).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "rizzstack.coverage/1"

LANDED = "LANDED"
COVERED_BY_DRAFT = "COVERED_BY_DRAFT"
PARTIAL = "PARTIAL"
IN_FLIGHT = "IN_FLIGHT"
MISSING = "MISSING"
SUPERSEDED = "SUPERSEDED"
BLOCKED = "BLOCKED"

DISPOSITIONS = (LANDED, COVERED_BY_DRAFT, PARTIAL, IN_FLIGHT, MISSING, SUPERSEDED, BLOCKED)

#: Only these may be dispatched (after the usual gates).
DISPATCHABLE = (MISSING, PARTIAL)

_TERMINAL_BEE = {"succeeded", "failed", "cancelled", "error"}


def _norm(path: str) -> str:
    """Normalize a repo-relative path for set comparison (POSIX form, no leading ./)."""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p) if p else ""


@dataclass(frozen=True)
class Candidate:
    """One implementation candidate: a draft PR, an open PR, or a merged landing."""

    repo: str
    number: int
    head_sha: str
    merged: bool
    files: frozenset[str]
    evidence: str = ""          # where the draft's proof lives (CI run, qualification record)
    supersedes: tuple[str, ...] = ()  # task ids this candidate declares it replaces

    @classmethod
    def build(cls, *, repo: str, number: int, head_sha: str, merged: bool,
              files: Iterable[str], evidence: str = "", supersedes: Iterable[str] = ()) -> "Candidate":
        return cls(repo=repo, number=int(number), head_sha=str(head_sha), merged=bool(merged),
                   files=frozenset(_norm(f) for f in files if _norm(f)),
                   evidence=evidence or "", supersedes=tuple(supersedes))


@dataclass(frozen=True)
class Task:
    """One queue item under reconciliation (backlog slice / package)."""

    task_id: str
    repo: str
    assigned_files: tuple[str, ...]
    tests: tuple[str, ...] = ()
    contract: str = ""
    status: str = ""
    attempt_ids: tuple[str, ...] = ()
    attempt_states: Mapping[str, str] = field(default_factory=dict)
    blocked_by: tuple[str, ...] = ()
    superseded_by: str = ""

    @classmethod
    def build(cls, *, task_id: str, repo: str, assigned_files: Iterable[str], tests: Iterable[str] = (),
              contract: str = "", status: str = "", attempt_ids: Iterable[str] = (),
              attempt_states: Mapping[str, str] | None = None, blocked_by: Iterable[str] = (),
              superseded_by: str = "") -> "Task":
        return cls(task_id=str(task_id), repo=str(repo),
                   assigned_files=tuple(_norm(f) for f in assigned_files if _norm(f)),
                   tests=tuple(_norm(t) for t in tests if _norm(t)), contract=contract or "",
                   status=str(status or ""), attempt_ids=tuple(attempt_ids or ()),
                   attempt_states=dict(attempt_states or {}), blocked_by=tuple(blocked_by or ()),
                   superseded_by=superseded_by or "")


def _repo_of(task_repo: str, candidate_repo: str) -> bool:
    """Repo match tolerant of 'mojomast/x', 'x', or a project slug."""
    a = str(task_repo or "").lower().rsplit("/", 1)[-1]
    b = str(candidate_repo or "").lower().rsplit("/", 1)[-1]
    return bool(a) and a == b


def _in_flight(task: Task) -> bool:
    return any(str(task.attempt_states.get(aid, "")).lower() not in _TERMINAL_BEE
               for aid in task.attempt_ids)


def _coverage(task: Task, candidate: Candidate) -> tuple[int, list[str]]:
    """(covered_count, missing_files) for one candidate against the task's lease."""
    if not task.assigned_files:
        return 0, []
    missing = [f for f in task.assigned_files if f not in candidate.files]
    return len(task.assigned_files) - len(missing), missing


def _best_candidate(task: Task, candidates: Sequence[Candidate]) -> tuple[Candidate | None, int, list[str]]:
    best = None
    best_covered = 0
    best_missing: list[str] = list(task.assigned_files)
    for cand in candidates:
        if not _repo_of(task.repo, cand.repo):
            continue
        covered, missing = _coverage(task, cand)
        # Prefer the most complete coverage; ties prefer merged over draft, then the newer PR.
        if covered > best_covered or (
            covered == best_covered and best is not None and covered > 0
            and ((cand.merged and not best.merged) or (cand.merged == best.merged and cand.number > best.number))
        ):
            best, best_covered, best_missing = cand, covered, missing
    return best, best_covered, best_missing


def classify(task: Task, candidates: Sequence[Candidate]) -> dict[str, Any]:
    """Classify one task against the known candidates. Pure; deterministic."""
    record: dict[str, Any] = {
        "task_id": task.task_id,
        "repo": task.repo,
        "contract": task.contract,
        "assigned_files": list(task.assigned_files),
        "tests": list(task.tests),
        "queue_status": task.status,
        "attempts": list(task.attempt_ids),
    }

    if task.superseded_by:
        record.update(disposition=SUPERSEDED, superseded_by=task.superseded_by,
                      reason=f"superseded by {task.superseded_by}")
        return record

    if _in_flight(task):
        live = [aid for aid in task.attempt_ids
                if str(task.attempt_states.get(aid, "")).lower() not in _TERMINAL_BEE]
        record.update(disposition=IN_FLIGHT, in_flight_attempts=live,
                      reason="a live attempt owns the lease; preserve ownership and reconcile its result")
        return record

    for cand in candidates:
        if task.task_id in cand.supersedes:
            record.update(disposition=SUPERSEDED, superseded_by=f"{cand.repo}#{cand.number}",
                          reason=f"{cand.repo}#{cand.number} declares it replaces this task")
            return record

    best, covered, missing = _best_candidate(task, candidates)
    if best is not None and covered == len(task.assigned_files) and task.assigned_files:
        evidence = best.evidence or "draft carries no recorded evidence; validate before use"
        if best.merged:
            record.update(disposition=LANDED, landed_by=f"{best.repo}#{best.number}",
                          commit=best.head_sha, evidence=evidence,
                          reason="implemented and merged into the authoritative branch")
        else:
            record.update(disposition=COVERED_BY_DRAFT, covered_by=f"{best.repo}#{best.number}",
                          draft_commit=best.head_sha, evidence=evidence,
                          requires_validation=True,
                          reason=("implemented in an unmerged draft; NOT accepted - link and validate, "
                                  "do not redispatch, do not report as done"))
        return record

    if best is not None and covered > 0:
        record.update(disposition=PARTIAL, covered_by=f"{best.repo}#{best.number}",
                      draft_commit=best.head_sha, covered_files=covered,
                      missing_files=missing,
                      reason="draft covers part of the lease; dispatch only the missing files with a "
                             "dependency on the existing implementation")
        return record

    if task.blocked_by:
        record.update(disposition=BLOCKED, blocked_by=list(task.blocked_by),
                      reason="a real dependency prevents work; next action is listed in blocked_by")
        return record

    record.update(disposition=MISSING, reason="no equivalent implementation found; eligible after the usual gates")
    return record


def reconcile(tasks: Iterable[Task], candidates: Iterable[Candidate]) -> dict[str, Any]:
    """Full report: per-task dispositions plus counts. Deterministic and idempotent."""
    cands = sorted(list(candidates), key=lambda c: (c.repo, c.number))
    records = [classify(t, cands) for t in sorted(tasks, key=lambda t: t.task_id)]
    counts = {name: 0 for name in DISPOSITIONS}
    for rec in records:
        counts[rec["disposition"]] = counts.get(rec["disposition"], 0) + 1
    dispatchable = [r["task_id"] for r in records if r["disposition"] in DISPATCHABLE]
    duplicates_prevented = [r["task_id"] for r in records
                            if r["disposition"] in (LANDED, COVERED_BY_DRAFT, IN_FLIGHT, SUPERSEDED)]
    return {
        "schema": SCHEMA,
        "counts": counts,
        "total": len(records),
        "dispatchable": dispatchable,
        "duplicate_dispatches_prevented": len(duplicates_prevented),
        "records": records,
    }


def format_table(report: Mapping[str, Any]) -> str:
    """Human-readable reconciliation table (stable order, no clock)."""
    lines = [f"{'task':14} {'disposition':18} {'covered_by':16} {'evidence'}"]
    for rec in report.get("records", []):
        lines.append(
            f"{rec['task_id']:14} {rec['disposition']:18} "
            f"{str(rec.get('covered_by') or rec.get('landed_by') or '-'):16} "
            f"{str(rec.get('evidence') or rec.get('reason') or '')[:70]}"
        )
    counts = report.get("counts", {})
    lines.append("")
    lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    lines.append(f"dispatchable: {len(report.get('dispatchable', []))}  "
                 f"duplicate_dispatches_prevented: {report.get('duplicate_dispatches_prevented', 0)}")
    return "\n".join(lines)
