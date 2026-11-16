"""A read-only Git/PR observer for campaign reconciliation (workflow-F3).

Campaign reconcile compares its checkpoint against durable job state; this
module is the missing external half: it compares the checkpoint's *recorded
revisions* against the world as it actually is — real git refs in a worktree
and open PR heads through the ``gh`` CLI — and reports the difference.

The observer is strictly report-only:

* it runs only allowlisted read-only git commands (``rev-parse``,
  ``merge-base``, ``rev-list``, ``show-ref``, ``cat-file``,
  ``symbolic-ref``) and read-only ``gh pr view``/``gh pr list`` queries;
* it never resolves drift: nothing is reset, pushed, merged, closed or
  rewritten, and no checkpoint is modified (drift is returned, never applied);
* subprocesses run under :func:`bundlebees_plugin.security.child_env` — a
  fresh minimal environment; nothing ambient (and no credential variable) is
  copied or forwarded, and ``gh`` is never handed a token on argv;
* the report never carries a URL and redacts free-text fields through
  :func:`bundlebees_plugin.security.scrub`; a commit digest is validated
  against its exact shape and printed verbatim, exactly like the coordinator
  prints digests.

Drift kinds, each carrying the exact expected-vs-actual values:

* ``branch_moved`` — the recorded branch no longer descends from the recorded
  base revision (rewritten or force-moved away from the authorized base);
* ``branch_missing`` — the recorded branch resolves to no head at all;
* ``pr_merged`` / ``pr_closed`` — the recorded PR is no longer open;
* ``pr_missing`` — the recorded PR cannot be resolved at all;
* ``pr_head_branch_changed`` — the recorded PR's head branch differs from the
  recorded branch;
* ``unexpected_pr_head`` — an open PR head for the recorded branch that the
  checkpoint does not record (an extra head, or any head when none is
  recorded).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import campaign as campaign_state
from .security import child_env, scrub

SCHEMA = "rizzstack.git-observer-report/1"

#: The complete set of git subcommands the observer may run. Anything else is
#: refused before dispatch, so "read-only" is structural, not conventional.
_READ_ONLY_GIT = frozenset(
    {"rev-parse", "show-ref", "merge-base", "rev-list", "cat-file", "symbolic-ref"}
)
#: The exact ``gh`` shapes the observer may run.
_GH_FIRST = frozenset({"pr"})
_GH_PR_VIEW_FIELDS = "number,state,headRefName,headRefOid"
_GH_PR_LIST_FIELDS = "number,headRefOid,headRefName"

_GIT_TIMEOUT_S = 30
_GH_TIMEOUT_S = 60
_MAX_ERROR_TEXT = 300

_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

_FIX_READONLY = (
    "The observer is read-only by construction; mutation belongs to an explicit, "
    "authorized path outside this module."
)
_FIX_WORLD = (
    "Check that the worktree and the gh CLI are reachable and authenticated for "
    "the recorded repository; an unobservable world is never reported as drift."
)
_FIX_CHECKPOINT = (
    "Re-create or repair the campaign checkpoint; the observer refuses to guess "
    "what a malformed record meant."
)


class GitObserverError(Exception):
    """The world could not be observed, or the checkpoint is unusable.

    ``code`` is a stable refusal name. An observation failure is *not* drift:
    the caller must treat it as "cannot observe", never as "world moved".
    """

    def __init__(self, code: str, message: str, fix: str = _FIX_WORLD) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


GitRunner = Callable[[Sequence[str], str | None], tuple[int, str, str]]
GhRunner = Callable[[Sequence[str]], tuple[int, str, str]]
PullRequestSource = Callable[[str, str, int | None], Mapping[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha(value: Any) -> str | None:
    """Return the value if it is exactly a commit-digest shape, else ``None``.

    Digests are validated, then kept (trimmed and lower-cased, git's own
    spelling) because they are the exact actual values a drift report exists to
    carry; every other free text is scrubbed instead.
    """

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if _SHA_RE.fullmatch(text) else None


def _text(value: Any) -> str:
    return scrub(value) if isinstance(value, str) else ""


def _pr_state(view: Any) -> str:
    """The PR state, normalized to the checkpoint's lowercase spelling.

    gh emits the GraphQL enum (OPEN / MERGED / CLOSED); the campaign records
    lowercase states, so the comparison normalizes once, at the source, and
    every report field carries the same normalized value.
    Scrubbing runs before case normalization: lowering first would defeat
    case-sensitive secret patterns (the AKIA access-key id class).
    """

    state = view.get("state") if isinstance(view, Mapping) else None
    return _text(state.strip()).lower() if isinstance(state, str) else ""


def _fact_value(value: Any) -> Any:
    """One drift fact: digests byte-exact, every other string scrubbed."""

    if isinstance(value, str):
        return value if _SHA_RE.fullmatch(value.strip().lower()) else _text(value)
    if isinstance(value, Mapping):
        return {key: _fact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_fact_value(item) for item in value]
    return value


def _clip(text: str) -> str:
    return text[:_MAX_ERROR_TEXT]


def _checkpoint_facts(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise GitObserverError("checkpoint_invalid", "the campaign checkpoint is not an object", _FIX_CHECKPOINT)
    cid = str(record.get("campaign_id") or "")
    jid = str(record.get("job_id") or "")
    if not _ID_RE.fullmatch(cid) or not _ID_RE.fullmatch(jid):
        raise GitObserverError("checkpoint_invalid", "the campaign checkpoint has no usable campaign/job id", _FIX_CHECKPOINT)
    auth = record.get("authorization")
    auth = auth if isinstance(auth, Mapping) else {}
    slug = str(auth.get("repo") or "").strip()
    if not slug or "/" not in slug:
        raise GitObserverError(
            "checkpoint_invalid",
            "the campaign checkpoint's authorization has no repository slug",
            _FIX_CHECKPOINT,
        )
    base = _sha(auth.get("base_rev"))
    if base is None:
        raise GitObserverError(
            "checkpoint_invalid",
            "the campaign checkpoint's authorization has no usable base revision",
            _FIX_CHECKPOINT,
        )
    branch = record.get("branch")
    branch = branch.strip() if isinstance(branch, str) else ""
    number = record.get("pull_request")
    if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 1):
        raise GitObserverError(
            "checkpoint_invalid",
            "the campaign checkpoint's recorded pull request is not a positive integer",
            _FIX_CHECKPOINT,
        )
    return {"campaign_id": cid, "job_id": jid, "repo": slug, "base_rev": base, "branch": branch, "pull_request": number}


# ---------------------------------------------------------------------------
# Default adapters: minimal environment, read-only argv, scrubbed failures
# ---------------------------------------------------------------------------


def _default_git(argv: Sequence[str], cwd: str | None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            env=child_env(),
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitObserverError("observer_timeout", "git did not finish in time", _FIX_WORLD) from exc
    except OSError as exc:
        raise GitObserverError("git_unavailable", f"git could not be started: {type(exc).__name__}", _FIX_WORLD) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _default_gh(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            env=child_env(),
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitObserverError("observer_timeout", "gh did not finish in time", _FIX_WORLD) from exc
    except OSError as exc:
        raise GitObserverError("gh_unavailable", f"gh could not be started: {type(exc).__name__}", _FIX_WORLD) from exc
    return proc.returncode, proc.stdout, proc.stderr


def _gh_json(argv: Sequence[str], gh: GhRunner, what: str) -> Any:
    if not argv or argv[0] not in _GH_FIRST:
        raise GitObserverError("gh_refused", f"refusing to run non-read-only gh command: {argv[0]!r}", _FIX_READONLY)
    code, out, err = gh(list(argv))
    if code != 0:
        text = _clip(scrub(err.strip()))
        lowered = err.lower()
        if "not found" in lowered or "could not resolve to a pullrequest" in lowered:
            return None
        raise GitObserverError("gh_failed", f"gh {what} failed: {text}", _FIX_WORLD)
    try:
        return json.loads(out)
    except ValueError as exc:
        raise GitObserverError("gh_failed", f"gh {what} returned unusable output: {exc}", _FIX_WORLD) from exc


def default_pull_requests(slug: str, branch: str, number: int | None, *, gh: GhRunner | None = None) -> dict[str, Any]:
    """Read the recorded PR and every open PR head for the branch via ``gh``.

    Only ``pr view``/``pr list`` JSON queries are issued; no URL is requested,
    so no URL can reach the report. Credentials stay with the gh CLI's own
    configuration under the minimal child environment.
    """

    gh = gh or _default_gh
    view: Mapping[str, Any] | None = None
    if number is not None:
        loaded = _gh_json(
            ["pr", "view", str(int(number)), "--repo", slug, "--json", _GH_PR_VIEW_FIELDS],
            gh,
            f"pr view {int(number)}",
        )
        if isinstance(loaded, Mapping):
            view = loaded
    listing: list[Any] = []
    if branch:
        loaded = _gh_json(
            ["pr", "list", "--repo", slug, "--head", branch, "--state", "open", "--json", _GH_PR_LIST_FIELDS],
            gh,
            "pr list",
        )
        if isinstance(loaded, list):
            listing = loaded
    return {"view": view, "open": listing}


# ---------------------------------------------------------------------------
# Git observations
# ---------------------------------------------------------------------------


def _guarded_git(runner: GitRunner, repo: Path, argv: Sequence[str]) -> tuple[int, str, str]:
    """Run one git command through the read-only allowlist, or refuse."""

    if not argv or argv[0] not in _READ_ONLY_GIT:
        raise GitObserverError("git_refused", f"refusing to run non-read-only git command: {argv[0]!r}", _FIX_READONLY)
    return runner(list(argv), str(repo))


def _head_of(runner: GitRunner, repo: Path, ref: str) -> str | None:
    """The commit a ref points at, or ``None`` when the ref does not exist.

    ``rev-parse --verify --quiet`` exits 1 exactly when the ref is missing; any
    other failure is an unobservable world, not a missing ref.
    """

    code, out, err = _guarded_git(runner, repo, ["rev-parse", "--verify", "--quiet", f"refs/{ref}"])
    if code == 0:
        found = _sha(out)
        if found is None:
            raise GitObserverError("git_failed", "git rev-parse returned an unusable revision", _FIX_WORLD)
        return found
    if code == 1:
        return None
    raise GitObserverError("git_failed", f"git rev-parse failed: {_clip(scrub(err.strip()))}", _FIX_WORLD)


def _resolves(runner: GitRunner, repo: Path, rev: str) -> bool:
    code, out, err = _guarded_git(runner, repo, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
    if code == 0:
        return True
    if code == 1:
        return False
    raise GitObserverError("git_failed", f"git rev-parse failed: {_clip(scrub(err.strip()))}", _FIX_WORLD)


def _descends(runner: GitRunner, repo: Path, base: str, head: str) -> bool:
    """``merge-base --is-ancestor``: 0 descends, 1 does not, else unobservable."""

    code, out, err = _guarded_git(runner, repo, ["merge-base", "--is-ancestor", base, head])
    if code == 0:
        return True
    if code == 1:
        return False
    raise GitObserverError("git_failed", f"git merge-base failed: {_clip(scrub(err.strip()))}", _FIX_WORLD)


def _ahead_count(runner: GitRunner, repo: Path, base: str, head: str) -> int | None:
    code, out, err = _guarded_git(runner, repo, ["rev-list", "--count", f"{base}..{head}"])
    if code != 0:
        raise GitObserverError("git_failed", f"git rev-list failed: {_clip(scrub(err.strip()))}", _FIX_WORLD)
    try:
        return max(0, int(out.strip() or 0))
    except ValueError as exc:
        raise GitObserverError("git_failed", "git rev-list returned an unusable count", _FIX_WORLD) from exc


def _observe_git(
    runner: GitRunner | None,
    repo: Path | None,
    base: str,
    branch: str,
) -> dict[str, Any]:
    if runner is None or repo is None or not branch:
        return {
            "observed": False,
            "local_head": None,
            "remote_head": None,
            "base_rev_resolves": None,
            "descends_from_base": None,
            "ahead_of_base": None,
        }
    work = Path(repo)
    local = _head_of(runner, work, f"heads/{branch}")
    remote = _head_of(runner, work, f"remotes/origin/{branch}")
    resolves = _resolves(runner, work, base) if local or remote else None
    head = local or remote
    descends: bool | None = None
    ahead: int | None = None
    if head and resolves:
        descends = _descends(runner, work, base, head)
        ahead = _ahead_count(runner, work, base, head)
    return {
        "observed": True,
        "local_head": local,
        "remote_head": remote,
        "base_rev_resolves": resolves,
        "descends_from_base": descends,
        "ahead_of_base": ahead,
    }


# ---------------------------------------------------------------------------
# PR observations and drift
# ---------------------------------------------------------------------------


def _observe_pull_requests(
    source: PullRequestSource | None,
    slug: str,
    branch: str,
    number: int | None,
) -> dict[str, Any]:
    if source is None:
        return {"observed": False, "view": None, "open": []}
    loaded = source(slug, branch, number)
    if not isinstance(loaded, Mapping):
        raise GitObserverError("gh_failed", "the PR source did not return an object", _FIX_WORLD)
    view = loaded.get("view")
    listing = loaded.get("open")
    if view is not None and not isinstance(view, Mapping):
        raise GitObserverError("gh_failed", "the recorded PR observation is not an object", _FIX_WORLD)
    if not isinstance(listing, list):
        raise GitObserverError("gh_failed", "the open PR observation is not a list", _FIX_WORLD)
    heads: list[dict[str, Any]] = []
    for item in listing:
        if not isinstance(item, Mapping):
            continue
        num = item.get("number")
        if isinstance(num, bool) or not isinstance(num, int) or num < 1:
            continue
        heads.append({"number": num, "head_oid": _sha(item.get("headRefOid")), "head_branch": _text(item.get("headRefName"))})
    heads.sort(key=lambda item: item["number"])
    return {"observed": True, "view": view, "open": heads}


def _drift(
    kind: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    detail: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "expected": _fact_value(dict(expected)),
        "actual": _fact_value(dict(actual)),
        "detail": _clip(scrub(detail)),
    }


def _branch_drift(base: str, branch: str, git_seen: dict[str, Any]) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    if not git_seen["observed"]:
        return drift
    head = git_seen["local_head"] or git_seen["remote_head"]
    if head is None:
        drift.append(
            _drift(
                "branch_missing",
                {"branch": _text(branch), "head": "present"},
                {"branch": _text(branch), "local_head": None, "remote_head": None},
                f"the recorded branch {branch} resolves to no local or remote head",
            )
        )
        return drift
    if git_seen["base_rev_resolves"] and git_seen["descends_from_base"] is False:
        drift.append(
            _drift(
                "branch_moved",
                {"base_rev": base, "head_descends_from_base": True},
                {
                    "head": head,
                    "head_descends_from_base": False,
                    "ahead_of_base": git_seen["ahead_of_base"],
                },
                f"branch {branch} no longer descends from the recorded base revision",
            )
        )
    return drift


def _pr_drift(
    slug: str,
    branch: str,
    number: int | None,
    pr_seen: dict[str, Any],
) -> list[dict[str, Any]]:
    drift: list[dict[str, Any]] = []
    if not pr_seen["observed"]:
        return drift
    view = pr_seen["view"]
    heads = pr_seen["open"]
    if number is not None:
        if view is None:
            drift.append(
                _drift(
                    "pr_missing",
                    {"pull_request": number, "state": "open"},
                    {"pull_request": number, "state": "missing"},
                    f"the recorded PR {number} cannot be resolved in {slug}",
                )
            )
        else:
            state = _pr_state(view)
            if state and state != "open":
                kind = "pr_merged" if state == "merged" else "pr_closed"
                drift.append(
                    _drift(
                        kind,
                        {"pull_request": number, "state": "open"},
                        {"pull_request": number, "state": state},
                        f"the recorded PR {number} is {state}, not open",
                    )
                )
            head_branch = view.get("headRefName")
            head_branch = head_branch.strip() if isinstance(head_branch, str) else ""
            if branch and head_branch and head_branch != branch:
                drift.append(
                    _drift(
                        "pr_head_branch_changed",
                        {"pull_request": number, "head_branch": _text(branch)},
                        {"pull_request": number, "head_branch": _text(head_branch)},
                        f"the recorded PR {number} heads from a branch other than the recorded one",
                    )
                )
    known = number if number is not None else 0
    extra = [item for item in heads if item["number"] != known]
    if branch and extra:
        drift.append(
            _drift(
                "unexpected_pr_head",
                {"head_branch": _text(branch), "open_heads": [] if number is None else [number]},
                {"head_branch": _text(branch), "open_heads": [item["number"] for item in extra]},
                (
                    "the recorded branch carries open PR heads the checkpoint does not record"
                    if number is not None
                    else "the recorded branch carries open PR heads although the checkpoint records none"
                ),
            )
        )
    return drift


# ---------------------------------------------------------------------------
# The observation
# ---------------------------------------------------------------------------


def observe(
    record: Mapping[str, Any],
    *,
    repo: Path | str | None = None,
    git: GitRunner | None = None,
    pull_requests: PullRequestSource | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Compare a campaign checkpoint's recorded revisions with the real world.

    ``record`` is the campaign checkpoint. ``repo`` is the local worktree the
    git refs are read from (``None`` observes the PR side only). ``git`` and
    ``pull_requests`` inject the world for tests; the defaults are the read-only
    git subprocess and the gh CLI. The returned report is a plain dict; nothing
    is written anywhere, and drift is never resolved here.
    """

    facts = _checkpoint_facts(record)
    slug, branch, number = facts["repo"], facts["branch"], facts["pull_request"]
    observed_at = _text(now) if now else _now()
    if not observed_at:
        observed_at = _now()
    git_runner: GitRunner | None = git if git is not None else _default_git
    worktree: Path | None = Path(repo) if repo is not None else None
    git_seen = _observe_git(git_runner, worktree, facts["base_rev"], branch)
    if pull_requests is None:
        source: PullRequestSource | None = None
        if worktree is not None or number is not None or branch:
            source = default_pull_requests
    else:
        source = pull_requests
    pr_seen = _observe_pull_requests(source, slug, branch, number) if source is not None else {
        "observed": False,
        "view": None,
        "open": [],
    }

    scope: list[str] = []
    if git_seen["observed"]:
        scope.append("git")
    if pr_seen["observed"]:
        scope.append("pr")

    drift = _branch_drift(facts["base_rev"], branch, git_seen) + _pr_drift(slug, branch, number, pr_seen)
    view = pr_seen["view"]
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "campaign_id": facts["campaign_id"],
        "job_id": facts["job_id"],
        "repo": _text(slug),
        "observed_at": observed_at,
        "scope": scope,
        "expected": {
            "base_rev": facts["base_rev"],
            "branch": _text(branch),
            "pull_request": number,
        },
        "actual": {
            "local_head": git_seen["local_head"],
            "remote_head": git_seen["remote_head"],
            "base_rev_resolves": git_seen["base_rev_resolves"],
            "descends_from_base": git_seen["descends_from_base"],
            "ahead_of_base": git_seen["ahead_of_base"],
            "pull_request": (
                {
                    "number": int(number),
                    "state": _pr_state(view),
                    "head_branch": _text(view.get("headRefName")),
                    "head_oid": _sha(view.get("headRefOid")),
                }
                if isinstance(view, Mapping)
                else None
            ),
            "open_heads": pr_seen["open"],
        },
        "drift": drift,
        "clean": not drift,
    }
    return report


def observe_campaign(
    state_dir: Path | str,
    campaign_id: str,
    *,
    repo: Path | str | None = None,
    git: GitRunner | None = None,
    pull_requests: PullRequestSource | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Load one campaign checkpoint and observe its world. Report-only."""

    record = campaign_state.load_campaign(state_dir, campaign_id)
    return observe(record, repo=repo, git=git, pull_requests=pull_requests, now=now)
