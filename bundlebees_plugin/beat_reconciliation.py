"""The launcher-side completed-attempt reconciliation sweep.

The gap this module closes: a completed bee whose branch was never pushed is
invisible to origin-delta detection, so it can sit unprocessed indefinitely
while every beat turn is busy with the merge wave. The scout diffs the launcher
attempt list against its own snapshot and *reports*; it structurally cannot
verify, push or open a PR. This module is the launcher-side duty it cannot do:

every sweep diffs the launcher's attempt list against origin and the open PR
heads, and for every attempt that is **succeeded + branch not on origin + no
open PR** it runs the processor (verify, push, open PR, record). A branch
already on origin, an attempt with an open PR, or an attempt already recorded
is skipped, and the skip reason is counted durably.

Idempotence and serialization:

* the sweep holds a durable claim file (:func:`claim_key` flock) for its whole
  run, so two concurrent sweeps serialize and the loser reports ``busy``;
* each attempt is processed under its own durable claim file
  (:func:`attempt_claim_key`), so a second owner can never double-push or
  double-open a PR for it;
* the ledger under ``beat/attempts-reconciliation/ledger`` records each
  outcome once; a processed attempt with a PR is never processed again, and a
  push whose PR step failed is resumed PR-only — the branch is never pushed
  twice;
* a processing failure records a bounded, doubling backoff (capped at 30
  minutes) so the attempt stays visible and is retried, never dropped.

The residual crash window — a push that landed before the ledger write —
leaves the branch on origin with no ledger and no PR; per the skip rule it is
reported as ``branch_on_origin`` rather than guessed at, and the sweep report
surfaces it every beat until a human or the owner resolves it.

Nothing here touches the scout's snapshot state: the classification is
read-only and shared with :func:`bundlebees_plugin.beat.scout_tick`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import leases
from . import test_receipts as receipts
from .security import child_env, scrub

SCHEMA = "rizzstack.attempt-reconciliation/1"
RECORD_SCHEMA = "rizzstack.attempt-reconciliation-record/1"
SKIPS_SCHEMA = "rizzstack.attempt-reconciliation-skips/1"

BEAT_DIR = "beat"
SWEEP_DIR = "attempts-reconciliation"
LEDGER_DIRNAME = "ledger"
SKIPS_FILENAME = "skips.json"
LAST_SWEEP_FILENAME = "last-sweep.json"

#: Worker states that mean the bee's work is finished successfully. This is the
#: union of the reconciler's reap set and the landing/consumer terminal sets.
SUCCESS_STATES = frozenset(
    {"succeeded", "success", "completed", "complete", "done", "landed"}
)

#: Processing stages. ``full`` is verify, push, open PR, record; ``pr`` resumes
#: an attempt whose branch is already pushed but whose PR is still missing.
STAGE_FULL = "full"
STAGE_PR = "pr"

#: Skip reasons. Every skip is reported and (for sweep runs) counted durably.
NOT_SUCCEEDED = "not_succeeded"
BRANCH_UNKNOWN = "branch_unknown"
BRANCH_INVALID = "branch_invalid"
BRANCH_ON_ORIGIN = "branch_on_origin"
PR_OPEN = "pr_open"
ALREADY_RECORDED = "already_recorded"
BACKOFF = "backoff"
CLAIM_BUSY = "claim_busy"
MALFORMED_ATTEMPT = "malformed_attempt"
LEDGER_CORRUPT = "ledger_corrupt"
#: Skip reasons for work that already landed under another name: a merged PR or
#: commit on main names the attempt id (coordinator applies, squash bodies), a
#: merged PR came from the attempt's own branch (squash or merge commit, branch
#: since deleted), or from its retry branch (``<branch>-2``...).
LANDED_ATTEMPT_ID = "landed_attempt_id"
LANDED_MERGED_BRANCH = "landed_merged_branch"
LANDED_RETRY_BRANCH = "landed_retry_branch"
LANDED_REASONS = frozenset({LANDED_ATTEMPT_ID, LANDED_MERGED_BRANCH, LANDED_RETRY_BRANCH})

#: Detection reasons for attempts the sweep will process.
REASON_UNPUSHED = "succeeded_unpushed_no_pr"
REASON_RESUME_PR = "pushed_without_pr"

#: The durable claim-file keys. The sweep claims the sweep key for its whole
#: run and one key per attempt it processes.
CLAIM_SCOPE = "beat-attempt-reconciliation"
SWEEP_CLAIM_KEY = f"{CLAIM_SCOPE}:sweep"

_MAIN_BRANCHES = frozenset({"main", "master"})
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_ATTEMPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_PR_URL_RE = re.compile(r"/pull/([0-9]+)\s*$")
#: An attempt id named in text. Short forms (``att_55bbab04``) are common in
#: PR bodies; at least 8 hex digits are required to count as a reference.
_ATT_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])att_([0-9a-f]{8,32})(?![0-9a-f])")
_MERGE_FROM_RE = re.compile(r"^Merge pull request #([0-9]+) from [A-Za-z0-9._-]+/(\S+)", re.M)
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_RETRY_SUFFIX_RE = re.compile(r"^-([2-9]|[1-9][0-9])$")

_GIT_TIMEOUT_S = 120
_GH_TIMEOUT_S = 120
_BACKOFF_BASE_S = 60.0
_BACKOFF_CAP_S = 1800.0
_MAX_TEXT = 300

_FIX_WORLD = (
    "Check that the launcher, the origin remote and the gh CLI are reachable; "
    "an unobservable world is never processed."
)


class SweepError(Exception):
    """The sweep could not observe the world, or an argument is unusable.

    ``code`` is a stable refusal name; an observation failure is never
    reported as drift or as processed work.
    """

    def __init__(self, code: str, message: str, fix: str = _FIX_WORLD) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Clock helpers (same contract as reconcile.py)
# ---------------------------------------------------------------------------


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    epoch = _epoch(value)
    if epoch is None:
        raise SweepError("clock_invalid", "now must be epoch seconds, an ISO timestamp or a callable")
    return epoch


def stamp(now: Any = None) -> str:
    """The sweep's UTC timestamp for a clock value (or now)."""

    return _iso(_clock(now))


def _clip(text: str) -> str:
    return str(text)[:_MAX_TEXT]


# ---------------------------------------------------------------------------
# Identifiers and branches
# ---------------------------------------------------------------------------


def _safe_attempt_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _ATTEMPT_RE.fullmatch(text) else ""


def _clean_branch(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if text.startswith("refs/heads/"):
        text = text[len("refs/heads/"):]
    if not text or ".." in text or not _BRANCH_RE.fullmatch(text):
        return ""
    return text


def _is_protected(branch: str) -> bool:
    return branch.strip().lower() in _MAIN_BRANCHES


def _attempt_id_of(entry: Mapping[str, Any]) -> str:
    for key in ("attempt_id", "id"):
        value = entry.get(key)
        if isinstance(value, str) and _safe_attempt_id(value):
            return value.strip()
    return ""


def _state_of(entry: Mapping[str, Any]) -> str:
    value = entry.get("state") or entry.get("status")
    return value.strip().lower() if isinstance(value, str) else ""


def _pr_numbers(value: Any) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int) and value > 0:
        return [int(value)]
    if isinstance(value, (list, tuple)):
        return [
            int(item)
            for item in value
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        ]
    return []


def _pr_of(record: Mapping[str, Any] | None) -> int | None:
    numbers = _pr_numbers((record or {}).get("pull_request"))
    return numbers[0] if numbers else None


# ---------------------------------------------------------------------------
# Durable state: ledger, skips, last sweep report
# ---------------------------------------------------------------------------


def sweep_dir(state_dir: Path) -> Path:
    return Path(state_dir) / BEAT_DIR / SWEEP_DIR


def ledger_dir(state_dir: Path) -> Path:
    return sweep_dir(state_dir) / LEDGER_DIRNAME


def ledger_path(state_dir: Path, attempt_id: str) -> Path:
    attempt = _safe_attempt_id(attempt_id)
    if not attempt:
        raise SweepError("attempt_invalid", f"attempt id {attempt_id!r} is not a safe identifier")
    return ledger_dir(state_dir) / f"{attempt}.json"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(dict(value), sort_keys=True, indent=2).encode("utf-8") + b"\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def ledger_record(state_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    """The durable reconciliation record for one attempt, if any."""

    return _read_json(ledger_path(state_dir, attempt_id))


def ledger_records(state_dir: Path) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """All ledger records by attempt id, plus the ids whose records are corrupt.

    A corrupt record is never treated as evidence of processed work; the
    attempt it names is skipped for inspection instead of being reprocessed
    under a guess.
    """

    records: dict[str, dict[str, Any]] = {}
    corrupt: set[str] = set()
    root = ledger_dir(state_dir)
    if not root.is_dir():
        return records, corrupt
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("."):
            continue
        raw = None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            corrupt.add(path.stem)
            continue
        if isinstance(value, dict) and _safe_attempt_id(value.get("attempt_id") or path.stem):
            records[str(value.get("attempt_id") or path.stem)] = value
        else:
            corrupt.add(path.stem)
    return records, corrupt


def _skips_path(state_dir: Path) -> Path:
    return sweep_dir(state_dir) / SKIPS_FILENAME


def skipped_reasons(state_dir: Path) -> dict[str, int]:
    """The durable skip-reason counters of past sweeps."""

    record = _read_json(_skips_path(state_dir))
    counts = record.get("counts") if isinstance(record, Mapping) else None
    return {str(key): int(value) for key, value in (counts or {}).items()}


def _bump_skips(state_dir: Path, reasons: Sequence[str], stamp: str) -> None:
    if not reasons:
        return
    path = _skips_path(state_dir)
    record = _read_json(path) or {"schema": SKIPS_SCHEMA, "counts": {}}
    counts = record.get("counts") if isinstance(record.get("counts"), dict) else {}
    for reason in reasons:
        counts[str(reason)] = int(counts.get(str(reason)) or 0) + 1
    record.update(counts=counts, updated_at=stamp)
    _write_json(path, record)


# ---------------------------------------------------------------------------
# The world: launcher attempts, origin branches, open PR heads
# ---------------------------------------------------------------------------


def _read_attempts(launcher: Any) -> list[Any]:
    if isinstance(launcher, (list, tuple)):
        entries = launcher
    else:
        method = getattr(launcher, "list_attempts", None)
        if not callable(method):
            raise SweepError(
                "launcher_incompatible",
                "the launcher exposes neither a list_attempts() nor a sequence of attempts",
                "Pass the reconcile contract's launcher adapter or an attempt list.",
            )
        try:
            entries = method()
        except Exception as exc:  # noqa: BLE001 - any launcher failure is unavailability
            raise SweepError("launcher_unavailable", _clip(scrub(f"{type(exc).__name__}: {exc}"))) from exc
    if not isinstance(entries, (list, tuple)):
        raise SweepError(
            "launcher_incompatible", "the launcher's attempt list is not a sequence"
        )
    return list(entries)


def _origin_map(value: Any) -> dict[str, str]:
    if callable(value):
        try:
            value = value()
        except Exception as exc:  # noqa: BLE001 - an unobservable origin is an error
            raise SweepError("origin_unavailable", _clip(scrub(f"{type(exc).__name__}: {exc}"))) from exc
    if isinstance(value, Mapping):
        out: dict[str, str] = {}
        for name, head in value.items():
            branch = _clean_branch(name)
            if branch:
                out[branch] = head if isinstance(head, str) else ""
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for name in value:
            branch = _clean_branch(name)
            if branch:
                out[branch] = ""
        return out
    raise SweepError("origin_unavailable", "the origin branch view is not a mapping or a list")


def _pr_map(value: Any) -> dict[str, list[int]]:
    if callable(value):
        try:
            value = value()
        except Exception as exc:  # noqa: BLE001 - an unobservable PR view is an error
            raise SweepError("pr_view_unavailable", _clip(scrub(f"{type(exc).__name__}: {exc}"))) from exc
    if isinstance(value, Mapping):
        out: dict[str, list[int]] = {}
        for name, numbers in value.items():
            branch = _clean_branch(name)
            heads = _pr_numbers(numbers)
            if branch and heads:
                out[branch] = heads
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for item in value:
            if not isinstance(item, Mapping):
                continue
            branch = _clean_branch(item.get("headRefName") or item.get("head_branch") or item.get("branch"))
            numbers = _pr_numbers(item.get("number"))
            if branch and numbers:
                out.setdefault(branch, [])
                for number in numbers:
                    if number not in out[branch]:
                        out[branch].append(number)
        return out
    raise SweepError("pr_view_unavailable", "the open-PR view is not a mapping or a list")


def _git_runner(repo: Path) -> Callable[[Sequence[str]], tuple[int, str, str]]:
    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *argv],
                cwd=str(repo),
                capture_output=True,
                text=True,
                env=child_env(),
                timeout=_GIT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise SweepError("git_timeout", "git did not finish in time") from exc
        except OSError as exc:
            raise SweepError("git_unavailable", f"git could not be started: {type(exc).__name__}") from exc
        return proc.returncode, proc.stdout, proc.stderr

    return run


def _gh_runner(repo: Path) -> Callable[[Sequence[str]], tuple[int, str, str]]:
    def run(argv: Sequence[str]) -> tuple[int, str, str]:
        if shutil.which("gh") is None:
            raise SweepError("pr_tool_missing", "the gh CLI is not installed", "Install gh and authenticate it for the repository.")
        try:
            proc = subprocess.run(
                ["gh", *argv],
                cwd=str(repo),
                capture_output=True,
                text=True,
                env=child_env(),
                timeout=_GH_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise SweepError("gh_timeout", "gh did not finish in time") from exc
        except OSError as exc:
            raise SweepError("gh_unavailable", f"gh could not be started: {type(exc).__name__}") from exc
        return proc.returncode, proc.stdout, proc.stderr

    return run


def default_origin_branches(repo: Path, *, git: Any = None) -> dict[str, str]:
    """The branch heads origin carries: ``git ls-remote --heads origin``.

    Read-only; the result is the origin view the sweep diffs the attempt list
    against. A failing read raises :class:`SweepError` — it is never reported
    as "not on origin".
    """

    run = git if git is not None else _git_runner(Path(repo))
    code, out, err = run(["ls-remote", "--heads", "origin"])
    if code != 0:
        raise SweepError("origin_unavailable", _clip(scrub(err.strip() or "git ls-remote failed")))
    heads: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        branch = _clean_branch(parts[1])
        if branch:
            heads[branch] = parts[0]
    return heads


def default_open_prs(repo: Path, *, gh: Any = None) -> dict[str, list[int]]:
    """The open PR heads: ``gh pr list --state open`` reduced to branch numbers.

    Read-only; a failing read raises :class:`SweepError` — an unobservable PR
    view fails the sweep closed rather than risking a second PR.
    """

    run = gh if gh is not None else _gh_runner(Path(repo))
    code, out, err = run(["pr", "list", "--state", "open", "--json", "number,headRefName"])
    if code != 0:
        raise SweepError("pr_view_unavailable", _clip(scrub(err.strip() or "gh pr list failed")))
    try:
        listing = json.loads(out)
    except ValueError as exc:
        raise SweepError("pr_view_unavailable", "gh pr list returned unusable output") from exc
    return _pr_map(listing if isinstance(listing, list) else [])


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _resolve_branch(
    entry: Mapping[str, Any],
    attempt: str,
    *,
    branch_of: Callable[[Mapping[str, Any]], Any] | None,
    state_dir: Path,
) -> tuple[str, str]:
    """``(clean branch, problem)`` — the branch name is never guessed.

    Resolution order: the attempt record's ``branch``, then the coordinator
    lease written at dispatch. A present-but-unusable name is ``branch_invalid``
    (never pushed); an absent one is ``branch_unknown``.
    """

    raw = ""
    if branch_of is not None:
        try:
            raw = branch_of(entry)
        except Exception:  # noqa: BLE001 - a broken resolver names no branch
            raw = ""
        raw = raw if isinstance(raw, str) else ""
    else:
        value = entry.get("branch")
        raw = value if isinstance(value, str) else ""
    if raw.strip():
        branch = _clean_branch(raw)
        return (branch, "" if branch else BRANCH_INVALID)
    if attempt and branch_of is None:
        try:
            lease_branch = str(leases.load_lease(state_dir, attempt).get("branch") or "")
        except leases.LeaseError:
            lease_branch = ""
        if lease_branch.strip():
            branch = _clean_branch(lease_branch)
            return (branch, "" if branch else BRANCH_INVALID)
    return ("", BRANCH_UNKNOWN)


def _retry_pending(record: Mapping[str, Any], clock: float) -> bool:
    next_retry = _epoch(record.get("next_retry_at"))
    return next_retry is not None and clock < next_retry


def classify_attempt(
    entry: Mapping[str, Any],
    *,
    branch: str,
    origin_branches: Mapping[str, Any],
    open_prs: Mapping[str, Any],
    ledger_record: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Classify one attempt against origin, the open PRs and the ledger.

    Returns ``{"attempt_id", "branch", "decision": "process"|"skip",
    "reason", "stage"}``. The decision table is the contract:

    * not succeeded → skip ``not_succeeded``;
    * a recorded PR, or an open PR on the branch → skip (``already_recorded`` /
      ``pr_open``);
    * a recorded push without a PR → resume PR-only (never re-pushed);
    * the branch already on origin with no ledger → skip ``branch_on_origin``;
    * a failed processing attempt inside its bounded backoff → skip ``backoff``;
    * otherwise → process: succeeded + unpushed + no PR, the gap class.
    """

    attempt = _attempt_id_of(entry)
    if not attempt:
        return {"attempt_id": "", "branch": "", "decision": "skip", "reason": MALFORMED_ATTEMPT, "stage": ""}
    if _state_of(entry) not in SUCCESS_STATES:
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": NOT_SUCCEEDED, "stage": ""}
    if not branch:
        return {"attempt_id": attempt, "branch": "", "decision": "skip", "reason": BRANCH_UNKNOWN, "stage": ""}
    if _is_protected(branch) or _clean_branch(branch) != branch:
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": BRANCH_INVALID, "stage": ""}

    clock = _clock(now)
    origin = _origin_map(origin_branches)
    prs = _pr_map(open_prs)
    open_on_branch = prs.get(branch) or []
    record = ledger_record if isinstance(ledger_record, Mapping) else None
    recorded_pr = _pr_of(record)
    recorded_push = bool(record and record.get("pushed") is True)
    recorded_status = str((record or {}).get("status") or "")

    if recorded_pr:
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": ALREADY_RECORDED, "stage": ""}
    if open_on_branch:
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": PR_OPEN, "stage": ""}
    if recorded_push:
        return {
            "attempt_id": attempt,
            "branch": branch,
            "decision": "process",
            "reason": REASON_RESUME_PR,
            "stage": STAGE_PR,
        }
    if recorded_status == "processed":
        return {"attempt_id": attempt, "branch": branch, "decision": "process", "reason": REASON_UNPUSHED, "stage": STAGE_FULL}
    if branch in origin:
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": BRANCH_ON_ORIGIN, "stage": ""}
    if record is not None and recorded_status == "failed" and _retry_pending(record, clock):
        return {"attempt_id": attempt, "branch": branch, "decision": "skip", "reason": BACKOFF, "stage": ""}
    return {"attempt_id": attempt, "branch": branch, "decision": "process", "reason": REASON_UNPUSHED, "stage": STAGE_FULL}


def _texts_tokens(text: str) -> set[str]:
    return {"att_" + m.group(1) for m in _ATT_TOKEN_RE.finditer(text or "")}


def landed_index(prs: Any = (), commits: Any = ()) -> dict[str, Any]:
    """Index landed work for one repository: attempt-id references and merged branches.

    ``prs`` are **merged** pull requests as mappings (``number``,
    ``headRefName``, ``title``, ``body``); ``commits`` are commits on main as
    mappings (``sha``, ``message``) or message strings. Only references that
    are actually present are indexed; nothing is inferred from timing or files.
    """

    attempts: dict[str, str] = {}
    branches: dict[str, str] = {}
    for pr in prs if isinstance(prs, (list, tuple)) else []:
        if not isinstance(pr, Mapping):
            continue
        numbers = _pr_numbers(pr.get("number"))
        if not numbers:
            continue
        where = f"PR #{numbers[0]}"
        for token in _texts_tokens(f"{pr.get('title') or ''}\n{pr.get('body') or ''}"):
            attempts.setdefault(token, where)
        head = _clean_branch(pr.get("headRefName") or pr.get("head_branch") or "")
        if head:
            branches.setdefault(head, where)
    for commit in commits if isinstance(commits, (list, tuple)) else []:
        if isinstance(commit, Mapping):
            message = commit.get("message")
            sha = str(commit.get("sha") or "")
        else:
            message, sha = commit, ""
        if not isinstance(message, str):
            continue
        where = f"commit {sha[:12]}" if _SHA_RE.match(sha or "") else "commit on main"
        for token in _texts_tokens(message):
            attempts.setdefault(token, where)
        for match in _MERGE_FROM_RE.finditer(message):
            head = _clean_branch(match.group(2))
            if head:
                branches.setdefault(head, f"PR #{match.group(1)}")
    return {"attempts": attempts, "branches": branches}


def landed_match(attempt_id: str, branch: str, index: Mapping[str, Any] | None,
                 *, branch_rules: bool = True) -> dict[str, str] | None:
    """``{"reason", "evidence"}`` when ``index`` shows this attempt's work landed.

    An attempt-id reference matches the full id or an 8+ hex digit prefix of
    it. Branch rules need the attempt's own repository: a merged PR whose head
    is the attempt's branch, or exactly ``<branch>-N`` (a retry branch).
    """

    if not isinstance(index, Mapping):
        return None
    attempts = index.get("attempts") if isinstance(index.get("attempts"), Mapping) else {}
    for token, where in attempts.items():
        if isinstance(token, str) and len(token) >= 12 and attempt_id.startswith(token):
            return {"reason": LANDED_ATTEMPT_ID, "evidence": f"{where} names {token}"}
    if not branch_rules or not branch:
        return None
    branches = index.get("branches") if isinstance(index.get("branches"), Mapping) else {}
    if branch in branches:
        return {"reason": LANDED_MERGED_BRANCH, "evidence": f"{branches[branch]} merged from {branch}"}
    for head, where in branches.items():
        if isinstance(head, str) and head.startswith(branch) and _RETRY_SUFFIX_RE.match(head[len(branch):]):
            return {"reason": LANDED_RETRY_BRANCH, "evidence": f"{where} merged from retry branch {head}"}
    return None


def _project_of(entry: Mapping[str, Any], attempt: str, state_dir: Path) -> str:
    brief = entry.get("brief")
    project = brief.get("project") if isinstance(brief, Mapping) else None
    if not isinstance(project, str) or not project.strip():
        try:
            lease_brief = leases.load_lease(state_dir, attempt).get("brief")
        except leases.LeaseError:
            lease_brief = None
        project = lease_brief.get("project") if isinstance(lease_brief, Mapping) else None
    project = project.strip() if isinstance(project, str) else ""
    return project if _PROJECT_RE.match(project) else ""


class _LandedView:
    """Lazy, per-run, per-repository view of landed work. Never raises.

    ``source`` is a mapping ``project -> index`` or a callable ``project ->
    index``. A repository whose view cannot be read is reported and its
    attempts are classified exactly as before (still flagged): a missing view
    never hides a gap.
    """

    def __init__(self, source: Any) -> None:
        self.source = source
        self.cache: dict[str, dict[str, Any] | None] = {}
        self.errors: dict[str, str] = {}

    def get(self, project: str) -> dict[str, Any] | None:
        if project in self.cache:
            return self.cache[project]
        value: Any = None
        try:
            value = self.source.get(project) if isinstance(self.source, Mapping) else self.source(project)
        except Exception as exc:  # noqa: BLE001 - an unreadable repo is reported, not guessed
            self.errors[project] = _clip(scrub(f"{type(exc).__name__}: {exc}"))
            value = None
        if value is not None and not isinstance(value, Mapping):
            self.errors[project] = "the landed view is not a mapping"
            value = None
        self.cache[project] = dict(value) if isinstance(value, Mapping) else None
        return self.cache[project]

    def match(self, attempt: str, branch: str, project: str) -> dict[str, str] | None:
        if project:
            found = landed_match(attempt, branch, self.get(project))
            if found:
                found = dict(found, project=project)
            return found
        # No project: attempt ids are globally unique, branch names are not.
        for name, index in list(self.cache.items()):
            found = landed_match(attempt, branch, index, branch_rules=False)
            if found:
                return dict(found, project=name)
        return None

    def status(self) -> str:
        if not self.cache:
            return "ok"
        if self.errors and len(self.errors) == len(self.cache):
            return "unavailable"
        return "partial" if self.errors else "ok"


def attempt_claim_key(attempt_id: str) -> str:
    """The durable claim-file key for one attempt's processing."""

    attempt = _safe_attempt_id(attempt_id)
    if not attempt:
        raise SweepError("attempt_invalid", f"attempt id {attempt_id!r} is not a safe identifier")
    return f"{CLAIM_SCOPE}:{attempt}"


def _backoff_s(attempts: int) -> float:
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** max(0, attempts - 1)))


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _empty_report(stamp: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "at": stamp,
        "status": "ok",
        "busy": False,
        "attempts_seen": 0,
        "detected": [],
        "processed": [],
        "failed": [],
        "skipped": [],
        "counts": {"detected": 0, "processed": 0, "failed": 0, "skipped": 0, "by_reason": {}},
    }


def _error_report(report: dict[str, Any], code: str, exc: Exception) -> dict[str, Any]:
    message = getattr(exc, "message", None) or str(exc)
    report["status"] = "error"
    report["error"] = {"code": code, "message": _clip(scrub(str(message)))}
    return report


def _skip(report: dict[str, Any], attempt: str, branch: str, reason: str) -> None:
    report["skipped"].append({"attempt_id": attempt, "branch": branch, "reason": reason})
    report["counts"]["skipped"] += 1
    by_reason = report["counts"]["by_reason"]
    by_reason[reason] = int(by_reason.get(reason) or 0) + 1


def _apply_outcome(
    state_dir: Path,
    attempt: str,
    branch: str,
    *,
    stage: str,
    outcome: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    clock: float,
) -> dict[str, Any]:
    """Merge one processor outcome into the ledger under the attempt claim."""

    stamp = _iso(clock)
    prior = dict(previous) if isinstance(previous, Mapping) else {}
    attempts = int(prior.get("attempts") or 0) + 1
    pushed = bool(prior.get("pushed")) or outcome.get("pushed") is True
    sha = str(outcome.get("sha") or prior.get("sha") or "")
    ok = outcome.get("ok") is True
    if ok:
        number = _pr_of(outcome) or _pr_of(prior)
        record = {
            "schema": RECORD_SCHEMA,
            "attempt_id": attempt,
            "branch": branch,
            "status": "processed",
            "pushed": pushed,
            "sha": sha if _SHA_RE.fullmatch(sha) else "",
            "pull_request": number,
            "stage": str(outcome.get("stage") or stage),
            "attempts": attempts,
            "at": stamp,
        }
    else:
        code = str(outcome.get("code") or "processor_error")
        error = _clip(scrub(str(outcome.get("error") or "the processor reported no detail")))
        record = {
            "schema": RECORD_SCHEMA,
            "attempt_id": attempt,
            "branch": branch,
            "status": "failed",
            "pushed": pushed,
            "sha": sha if _SHA_RE.fullmatch(sha) else "",
            "pull_request": _pr_of(prior),
            "code": code,
            "error": error,
            "attempts": attempts,
            "next_retry_at": _iso(clock + _backoff_s(attempts)),
            "at": stamp,
        }
    _write_json(ledger_path(state_dir, attempt), record)
    return record


def sweep(
    state_dir: Path,
    *,
    launcher: Any,
    origin_branches: Any,
    open_prs: Any,
    processor: Any = None,
    branch_of: Callable[[Mapping[str, Any]], Any] | None = None,
    now: Any = None,
    wait_s: float = 0.0,
    record: bool = True,
    claim: bool = True,
    landed: Any = None,
) -> dict[str, Any]:
    """Run one completed-attempt reconciliation sweep and return its report.

    ``launcher`` is the reconcile contract's launcher adapter (or a plain
    attempt list); ``origin_branches`` and ``open_prs`` are the origin view and
    the open-PR view (mappings, lists or callables). ``processor`` is the
    mutating duty — verify, push, open PR, record — called as
    ``processor(attempt_id, entry=..., branch=..., stage=...)``; ``None``
    classifies only (the scout's read-only mode). ``record=False`` and
    ``claim=False`` keep a run read-only end to end.

    ``landed`` (optional) is the landed-work view (see :class:`_LandedView`):
    a succeeded, never-recorded attempt whose work a merged PR or a commit on
    main already carries is skipped with a ``landed_*`` reason and listed in
    ``report["landed"]`` with its evidence, instead of being detected.

    The report never raises for an unobservable world: it reports
    ``status: "error"`` with a stable code, so a beat cannot die on the sweep.
    """

    state = Path(state_dir)
    landed_view = _LandedView(landed) if landed is not None else None
    clock = _clock(now)
    report = _empty_report(_iso(clock))

    try:
        entries = _read_attempts(launcher)
    except SweepError as exc:
        return _error_report(report, exc.code, exc)
    try:
        origin = _origin_map(origin_branches)
        prs = _pr_map(open_prs)
    except SweepError as exc:
        return _error_report(report, exc.code, exc)

    records, corrupt = ledger_records(state)
    claim_handle = None
    if claim:
        try:
            claim_handle = receipts.claim_key(state, SWEEP_CLAIM_KEY, wait_s=wait_s)
        except receipts.ReceiptError as exc:
            report["status"] = "busy"
            report["busy"] = True
            report["error"] = {"code": exc.code, "message": _clip(scrub(str(exc.error)))}
            return report

    try:
        for entry in entries:
            if not isinstance(entry, Mapping):
                _skip(report, "", "", MALFORMED_ATTEMPT)
                continue
            report["attempts_seen"] += 1
            attempt = _attempt_id_of(entry)
            if not attempt:
                _skip(report, "", "", MALFORMED_ATTEMPT)
                continue
            branch, problem = _resolve_branch(entry, attempt, branch_of=branch_of, state_dir=state)
            decision = classify_attempt(
                entry,
                branch=branch,
                origin_branches=origin,
                open_prs=prs,
                ledger_record=records.get(attempt),
                now=clock,
            )
            if problem:
                decision = dict(decision)
                decision["decision"] = "skip"
                decision["reason"] = problem
                decision["stage"] = ""
            if decision["decision"] != "process":
                _skip(report, decision["attempt_id"], decision["branch"], decision["reason"])
                continue
            if (
                landed_view is not None
                and decision["reason"] == REASON_UNPUSHED
                and records.get(attempt) is None
            ):
                found = landed_view.match(attempt, branch, _project_of(entry, attempt, state))
                if found:
                    _skip(report, attempt, branch, found["reason"])
                    report.setdefault("landed", []).append({
                        "attempt_id": attempt,
                        "branch": branch,
                        "project": found.get("project", ""),
                        "reason": found["reason"],
                        "evidence": found["evidence"],
                    })
                    continue
            detected = {
                "attempt_id": attempt,
                "branch": branch,
                "reason": str(decision["reason"]),
                "stage": str(decision["stage"]),
                "state": _state_of(entry),
                "finished_at": str(entry.get("finished_at") or entry.get("created_at") or ""),
            }
            finished = _epoch(detected["finished_at"])
            detected["age_s"] = max(0.0, clock - finished) if finished is not None else None
            report["detected"].append(detected)
            report["counts"]["detected"] += 1

            if processor is None:
                continue

            try:
                handle = receipts.claim_key(state, attempt_claim_key(attempt), wait_s=wait_s)
            except receipts.ReceiptError:
                _skip(report, attempt, branch, CLAIM_BUSY)
                report["counts"]["detected"] -= 1
                report["detected"].pop()
                continue
            try:
                fresh, fresh_corrupt = ledger_records(state)
                if attempt in fresh_corrupt:
                    _skip(report, attempt, branch, LEDGER_CORRUPT)
                    report["counts"]["detected"] -= 1
                    report["detected"].pop()
                    continue
                fresh_decision = classify_attempt(
                    entry,
                    branch=branch,
                    origin_branches=origin,
                    open_prs=prs,
                    ledger_record=fresh.get(attempt),
                    now=clock,
                )
                if fresh_decision["decision"] != "process":
                    _skip(report, attempt, branch, fresh_decision["reason"])
                    report["counts"]["detected"] -= 1
                    report["detected"].pop()
                    continue
                if fresh_decision["stage"] == STAGE_PR and fresh_decision["reason"] == REASON_RESUME_PR:
                    observed = prs.get(branch) or []
                    if observed:
                        # An open PR appeared for our pushed branch: record it
                        # and skip, closing the ledger's PR gap without opening
                        # a second one.
                        record = _apply_outcome(
                            state, attempt, branch,
                            stage=STAGE_PR,
                            outcome={"ok": True, "pushed": True, "sha": "", "pull_request": observed[0], "stage": STAGE_PR},
                            previous=fresh.get(attempt),
                            clock=clock,
                        )
                        report["processed"].append({
                            "attempt_id": attempt,
                            "branch": branch,
                            "sha": str(record.get("sha") or ""),
                            "pushed": bool(record.get("pushed")),
                            "pull_request": record.get("pull_request"),
                            "stage": STAGE_PR,
                        })
                        report["counts"]["processed"] += 1
                        report["counts"]["detected"] -= 1
                        report["detected"].pop()
                        continue
                try:
                    outcome = processor(attempt, entry=dict(entry), branch=branch, stage=str(decision["stage"]))
                except Exception as exc:  # noqa: BLE001 - a processor crash is a recorded failure
                    outcome = {
                        "ok": False,
                        "code": "processor_error",
                        "error": _clip(scrub(f"{type(exc).__name__}: {exc}")),
                        "pushed": bool((fresh.get(attempt) or {}).get("pushed")),
                    }
                if not isinstance(outcome, Mapping):
                    outcome = {"ok": False, "code": "processor_error", "error": "the processor returned no mapping"}
                saved = _apply_outcome(
                    state, attempt, branch,
                    stage=str(decision["stage"]),
                    outcome=outcome,
                    previous=fresh.get(attempt),
                    clock=clock,
                )
                records[attempt] = saved
                if saved.get("status") == "processed":
                    report["processed"].append({
                        "attempt_id": attempt,
                        "branch": branch,
                        "sha": str(saved.get("sha") or ""),
                        "pushed": bool(saved.get("pushed")),
                        "pull_request": saved.get("pull_request"),
                        "stage": str(saved.get("stage") or ""),
                    })
                    report["counts"]["processed"] += 1
                else:
                    report["failed"].append({
                        "attempt_id": attempt,
                        "branch": branch,
                        "code": str(saved.get("code") or "processor_error"),
                        "error": str(saved.get("error") or ""),
                    })
                    report["counts"]["failed"] += 1
            finally:
                handle.release()
    finally:
        if claim_handle is not None:
            claim_handle.release()

    if landed_view is not None:
        report["landed_view"] = landed_view.status()
        report.setdefault("landed", [])
        report["counts"]["landed"] = len(report["landed"])
        if landed_view.errors:
            report["landed_errors"] = dict(sorted(landed_view.errors.items()))
    if record:
        _bump_skips(state, [item["reason"] for item in report["skipped"]], report["at"])
        _write_json(sweep_dir(state) / LAST_SWEEP_FILENAME, report)
    return report


# ---------------------------------------------------------------------------
# The default processor: verify, push, open PR
# ---------------------------------------------------------------------------


class ProcessorError(Exception):
    """One mutating step of the default processor refused or failed."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(scrub(str(message)))
        self.fix = str(scrub(str(fix)))


def _files_in_patch(patch: str) -> list[str]:
    found: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                found.add(parts[3][2:] if parts[3].startswith("b/") else parts[3])
    return sorted(found)


def _default_verify(
    state_dir: Path,
    repo: Path,
    attempt_id: str,
    *,
    patch: str,
    declared_files: Sequence[str],
    base_rev: str,
    tests: Sequence[str],
    suite_command: Sequence[str] | None,
    gitleaks: str | None,
    require_gitleaks: bool,
    runner: Any,
    expected_patch_sha256: str | None,
    acdd_preflight: Any,
    contract_declared: bool,
) -> Mapping[str, Any]:
    from . import verify_local

    return verify_local.verify_and_commit(
        Path(state_dir),
        Path(repo),
        attempt_id,
        patch=patch,
        declared_files=declared_files,
        base_rev=base_rev,
        tests=list(tests),
        suite_command=list(suite_command) if suite_command else None,
        gitleaks=gitleaks,
        require_gitleaks=require_gitleaks,
        runner=runner,
        expected_patch_sha256=expected_patch_sha256,
        acdd_preflight=acdd_preflight,
        contract_declared=contract_declared,
    )


def _default_push(repo: Path, sha: str, branch: str) -> bool:
    if not _SHA_RE.fullmatch(sha):
        raise ProcessorError("push_ref_invalid", "the verified commit is not a full revision", "Re-run verification.")
    if _is_protected(branch) or not _clean_branch(branch):
        raise ProcessorError("branch_invalid", f"refusing to push {branch!r}", "Only a safe feature branch is ever pushed.")
    code, _out, err = _git_runner(Path(repo))(["push", "origin", f"{sha}:refs/heads/{branch}"])
    if code != 0:
        raise ProcessorError("push_failed", _clip(err.strip() or "git push failed"), "Check the remote and the branch state; nothing was forced.")
    return True


def _default_open_pr(repo: Path, branch: str, title: str, body: str, *, base: str = "main") -> int:
    run = _gh_runner(Path(repo))
    code, out, err = run(
        ["pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body]
    )
    if code == 0:
        for line in reversed(out.strip().splitlines()):
            match = _PR_URL_RE.search(line)
            if match:
                return int(match.group(1))
        listed = _open_pr_numbers(run, branch)
        if listed:
            return listed[0]
        raise ProcessorError("pr_number_unknown", "the PR was created but its number could not be read")
    listed = _open_pr_numbers(run, branch)
    if listed:
        # The create may have landed before its failure was reported; an open
        # PR for the branch is adopted instead of opening a second one.
        return listed[0]
    raise ProcessorError("pr_open_failed", _clip(err.strip() or "gh pr create failed"))


def _open_pr_numbers(run: Callable[[Sequence[str]], tuple[int, str, str]], branch: str) -> list[int]:
    try:
        code, out, _err = run(["pr", "list", "--head", branch, "--state", "open", "--json", "number"])
    except SweepError:
        return []
    if code != 0:
        return []
    try:
        listing = json.loads(out)
    except ValueError:
        return []
    return _pr_map(listing if isinstance(listing, list) else []).get(branch, [])


def build_processor(
    client: Any,
    repo: Path,
    state_dir: Path,
    *,
    suite_command: Sequence[str] | None = None,
    gitleaks: str | None = None,
    require_gitleaks: bool | None = None,
    runner: Any = None,
    main_ref: str = "main",
    verify: Any = None,
    push: Any = None,
    open_pr: Any = None,
) -> Callable[..., dict[str, Any]]:
    """Build the production processor: verify, push, open PR, record.

    ``client`` is the launcher client (artifact API preferred, result envelope
    as the documented fallback). The lease written at dispatch is the only
    scope authority; a missing lease is a refusal, never a legacy verification.
    A lease brief that declares a frozen contract is verified through the
    contract path (``acdd_preflight`` + ``contract_declared``), never legacy
    mode — the F2 require-contract rule.

    ``verify``/``push``/``open_pr`` are injectable for tests; the defaults are
    :func:`verify_local.verify_and_commit` (commit-local, never pushed by the
    verifier), an explicit-refspec ``git push`` of the verified commit to the
    feature branch (no force, never main), and ``gh pr create`` into
    ``main_ref``.
    """

    state = Path(state_dir)
    repo_path = Path(repo)
    verify_fn = verify if verify is not None else _default_verify
    push_fn = push if push is not None else _default_push
    base = str(main_ref or "main")
    needs_gitleaks = (gitleaks is not None) if require_gitleaks is None else bool(require_gitleaks)

    def open_pr_for(branch: str, title: str, body: str) -> int:
        try:
            if open_pr is not None:
                return int(open_pr(repo_path, branch, title, body))
            return _default_open_pr(repo_path, branch, title, body, base=base)
        except ProcessorError:
            raise
        except Exception as exc:  # noqa: BLE001 - any PR refusal is a recorded failure
            code = str(getattr(exc, "code", "") or "pr_open_failed")
            message = str(getattr(exc, "message", None) or getattr(exc, "error", None) or exc)
            raise ProcessorError(code, message) from exc

    def push_verified(sha: str, branch: str) -> None:
        try:
            if push is not None:
                if push(repo_path, sha, branch) is False:
                    raise ProcessorError("push_failed", "the injected push reported failure")
                return
            _default_push(repo_path, sha, branch)
        except ProcessorError:
            raise
        except Exception as exc:  # noqa: BLE001 - any push refusal is a recorded failure
            code = str(getattr(exc, "code", "") or "push_failed")
            message = str(getattr(exc, "message", None) or getattr(exc, "error", None) or exc)
            raise ProcessorError(code, message) from exc

    def processor(attempt_id: str, *, entry: Mapping[str, Any], branch: str, stage: str) -> dict[str, Any]:
        attempt = _safe_attempt_id(attempt_id)
        if not attempt:
            return {"ok": False, "code": "attempt_invalid", "error": f"attempt id {attempt_id!r} is not usable", "pushed": False}
        branch = _clean_branch(branch)
        if not branch or _is_protected(branch):
            return {"ok": False, "code": "branch_invalid", "error": f"refusing to process branch {branch!r}", "pushed": False}
        try:
            lease = leases.load_lease(state, attempt)
        except leases.LeaseError as exc:
            return {"ok": False, "code": exc.code, "error": _clip(str(exc.error)), "pushed": False}

        title = _clip(scrub(f"bundlebees: verified attempt {attempt}"))

        if stage == STAGE_PR:
            try:
                number = open_pr_for(branch, title, _clip(scrub(f"Reconciliation sweep: open the PR for pushed branch {branch} (attempt {attempt}).")))
            except ProcessorError as exc:
                return {"ok": False, "code": exc.code, "error": exc.message, "pushed": True}
            return {"ok": True, "pushed": False, "sha": "", "pull_request": number, "stage": stage}

        declared = bool(isinstance(lease.get("brief"), Mapping) and lease["brief"].get("contract"))
        acceptance = lease.get("acceptance") if isinstance(lease.get("acceptance"), Mapping) else {}
        tests = [
            str(item)
            for item in (lease.get("tests") or acceptance.get("tests") or [])
            if isinstance(item, str) and item.strip()
        ]

        try:
            patch, files, source, expected = _fetch_result(client, attempt)
        except ProcessorError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "pushed": False}
        if not patch:
            return {"ok": False, "code": "no_result_yet", "error": "the worker result has no patch yet", "pushed": False}

        preflight = None
        if declared:
            from . import acdd_cli

            def preflight(patch_files: list[str]) -> object:
                return acdd_cli.verify_preflight(lease, state, patch_files)

        try:
            record = verify_fn(
                state,
                repo_path,
                attempt,
                patch=patch,
                declared_files=list(files),
                base_rev=source,
                tests=tests,
                suite_command=suite_command,
                gitleaks=gitleaks,
                require_gitleaks=needs_gitleaks,
                runner=runner,
                expected_patch_sha256=expected,
                acdd_preflight=preflight,
                contract_declared=declared,
            )
        except Exception as exc:  # noqa: BLE001 - any verification refusal is a recorded failure
            code = str(getattr(exc, "code", "") or "verify_error")
            message = str(getattr(exc, "message", None) or exc)
            return {"ok": False, "code": code, "error": _clip(message), "pushed": False}
        sha = str((record or {}).get("sha") or "")
        if not _SHA_RE.fullmatch(sha):
            return {"ok": False, "code": "verify_no_commit", "error": "the verifier returned no usable commit", "pushed": False}

        try:
            push_verified(sha, branch)
        except ProcessorError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "pushed": False, "sha": sha}

        body = _clip(scrub(
            f"Completed attempt {attempt} verified and pushed by the bundlebees "
            f"reconciliation sweep.\n\nattempt: {attempt}\nbranch: {branch}\n"
        )) + (f"\nsha: {sha}\n" if _SHA_RE.fullmatch(sha) else "")
        try:
            number = open_pr_for(branch, title, body)
        except ProcessorError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "pushed": True, "sha": sha}
        return {"ok": True, "pushed": True, "sha": sha, "pull_request": number, "stage": stage}

    return processor


def _fetch_result(client: Any, attempt: str) -> tuple[str | None, list[str], str, str | None]:
    """Fetch the worker artifact through the capability-bound API, else the envelope."""

    artifact = getattr(client, "artifact", None)
    if callable(artifact):
        try:
            art = artifact(attempt)
        except Exception as exc:  # noqa: BLE001 - a capability/integrity refusal is precise
            code = str(getattr(exc, "code", "") or "artifact_refused")
            raise ProcessorError(code, str(getattr(exc, "message", None) or getattr(exc, "error", None) or exc)) from exc
        if not isinstance(art, Mapping):
            raise ProcessorError("artifact_invalid", "the artifact API returned no object")
        patch = art.get("patch")
        patch = patch if isinstance(patch, str) and patch.strip() else None
        source = str(art.get("source_rev") or "")
        expected = art.get("patch_sha256") or None
        return patch, _files_in_patch(patch or ""), source, (expected if isinstance(expected, str) else None)

    try:
        envelope = client.result_envelope(attempt)
    except Exception as exc:  # noqa: BLE001 - the launcher's typed refusal is the code
        code = str(getattr(exc, "code", "") or "result_unavailable")
        raise ProcessorError(code, str(getattr(exc, "message", None) or getattr(exc, "error", None) or exc)) from exc
    if not isinstance(envelope, Mapping):
        raise ProcessorError("result_unavailable", "the launcher result envelope is not an object")
    block = envelope.get("patch") if isinstance(envelope.get("patch"), Mapping) else {}
    patch = block.get("diff") if isinstance(block.get("diff"), str) and block.get("diff").strip() else None
    if patch is None:
        raw = envelope.get("patch")
        patch = raw if isinstance(raw, str) and raw.strip() else None
    files = [
        str(item.get("path"))
        for item in (block.get("files") or [])
        if isinstance(item, Mapping) and item.get("path")
    ]
    source_block = envelope.get("source") if isinstance(envelope.get("source"), Mapping) else {}
    source = str(source_block.get("repo_rev") or "")
    expected = envelope.get("patch_sha256") or None
    return patch, files or _files_in_patch(patch or ""), source, (expected if isinstance(expected, str) else None)


__all__ = [
    "ALREADY_RECORDED",
    "BACKOFF",
    "BEAT_DIR",
    "BRANCH_INVALID",
    "BRANCH_ON_ORIGIN",
    "BRANCH_UNKNOWN",
    "CLAIM_BUSY",
    "CLAIM_SCOPE",
    "LEDGER_CORRUPT",
    "LEDGER_DIRNAME",
    "MALFORMED_ATTEMPT",
    "NOT_SUCCEEDED",
    "PR_OPEN",
    "REASON_RESUME_PR",
    "REASON_UNPUSHED",
    "RECORD_SCHEMA",
    "SCHEMA",
    "SKIPS_SCHEMA",
    "STAGE_FULL",
    "STAGE_PR",
    "SUCCESS_STATES",
    "SWEEP_CLAIM_KEY",
    "SweepError",
    "attempt_claim_key",
    "build_processor",
    "classify_attempt",
    "default_open_prs",
    "default_origin_branches",
    "ledger_dir",
    "ledger_path",
    "ledger_record",
    "ledger_records",
    "skipped_reasons",
    "stamp",
    "sweep",
    "sweep_dir",
]
