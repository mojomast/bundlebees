"""Revision provenance checks and full-suite wave integration.

Threat model
------------
A worker patch, its declared base revision, and the JSON verify results are
untrusted third-party data. A patch is only accepted when it is proven to have
been built against the brief's exact ``source_rev``: the declared base must
match that full commit id, the commit must exist, and a temporary index built
from ``source_rev`` must accept the patch. The temporary index lives in a
``TemporaryDirectory`` and is passed to git as ``GIT_INDEX_FILE``, so neither
the caller's real index nor its worktree is touched.

Git is invoked with an argument list (never a shell) and a minimal exact-name
environment from :func:`bundlebees_plugin.security.child_env`; nothing is
inherited from the ambient process environment. No credentials are read,
stored, or printed, and every error text is passed through
:func:`bundlebees_plugin.security.scrub`.

A wave is accepted only when every listed attempt has an ``ok`` verify result
whose 40-hex sha descends from ``HEAD`` *and* a caller-supplied full test suite
returns a non-timeout exit code of zero. An accepted wave record is final:
later attempts to overwrite it raise ``RevisionError("wave_already_accepted")``.
The full-suite evidence comes from a disposable sandbox; its ``output_tail`` is
scrubbed before it is stored.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .security import child_env, scrub

BASE_KEYS = ("base_rev", "source_rev", "base_sha", "base")

_WAVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FULL_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_SHA40_RE = re.compile(r"[0-9a-fA-F]{40}")
_MAX_DETAIL = 800

RunGit = Callable[..., subprocess.CompletedProcess]
SuiteRunner = Callable[[Path], dict]


class RevisionError(Exception):
    """A revision-provenance failure with a stable code and remediation."""

    def __init__(self, code: str, error: str, fix: str) -> None:
        self.code = str(code)
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        super().__init__(self.error)


# ---------------------------------------------------------------------------
# Git runner
# ---------------------------------------------------------------------------


def _tail(text: Any, limit: int = _MAX_DETAIL) -> str:
    value = str(text or "").replace("\x00", "").strip()
    if len(value) > limit:
        value = value[-limit:]
    return str(scrub(value))


def _git_failure(args: Any, proc: subprocess.CompletedProcess) -> RevisionError:
    verb = next(iter(list(args)), "command")
    detail = _tail(proc.stderr or proc.stdout)
    message = f"git {verb} failed"
    if detail:
        message = f"{message}: {detail}"
    return RevisionError(
        "git_failed",
        message,
        "Check that git is installed and the worktree is a readable repository.",
    )


def default_run_git(
    args: list[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    check: bool = True,
    env_extra: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run git with a minimal child environment and no shell.

    The base environment names ``GIT_TERMINAL_PROMPT=0`` so git cannot prompt.
    Callers may pass only exact-name allowlisted extras (for example the
    temporary ``GIT_INDEX_FILE`` used by :func:`check_applies_at`).
    """
    extra: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        extra.update({str(name): str(value) for name, value in env_extra.items()})
    command = ["git", *(str(item) for item in args)]
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=child_env(extra),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RevisionError(
            "git_failed",
            f"could not run git: {exc}",
            "Check that git is installed and the worktree is readable.",
        ) from exc
    if check and proc.returncode != 0:
        raise _git_failure(args, proc)
    return proc


# ---------------------------------------------------------------------------
# Declared base
# ---------------------------------------------------------------------------


def _full_hex(value: Any) -> str | None:
    if isinstance(value, str) and _FULL_HEX_RE.fullmatch(value):
        return value
    return None


def _declared_in(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in BASE_KEYS:
        found = _full_hex(value.get(key))
        if found is not None:
            return found
    return None


def declared_base(result: Mapping) -> str | None:
    """Find the worker's declared base revision, if any.

    Looks for the first full 40/64-character hex value under ``BASE_KEYS`` at
    the top level, then inside ``result["observed"]``, then inside
    ``result["result"]`` (including one nested observed/result level).
    Callers normally pass the adapter-normalized result
    (``adapters.normalize_result``), so runtime-specific shapes never reach
    this module.
    """
    found = _declared_in(result)
    if found is not None:
        return found
    if not isinstance(result, Mapping):
        return None
    for container_key in ("observed", "result"):
        container = result.get(container_key)
        found = _full_hex(container)
        if found is not None:
            return found
        found = _declared_in(container)
        if found is not None:
            return found
        if isinstance(container, Mapping):
            for inner_key in ("observed", "result"):
                found = _declared_in(container.get(inner_key))
                if found is not None:
                    return found
    return None


def check_declared_base(result: Mapping, source_rev: str) -> str:
    """Require the result's declared base to match ``source_rev`` exactly."""
    declared = declared_base(result)
    if declared is None:
        raise RevisionError(
            "base_undeclared",
            "the worker result does not declare a full-hex base revision",
            "Rebuild the worker patch against the brief's source_rev and retry.",
        )
    if declared.casefold() != str(source_rev or "").strip().casefold():
        raise RevisionError(
            "base_mismatch",
            "the worker result was built against a different base revision",
            "Rebuild the worker patch against the brief's exact source_rev.",
        )
    return declared


# ---------------------------------------------------------------------------
# Git provenance checks
# ---------------------------------------------------------------------------


def check_source_rev(
    workdir: Path,
    source_rev: str,
    run_git: RunGit = default_run_git,
) -> None:
    """Require ``source_rev`` to be a full commit id present in the repository."""
    if not isinstance(source_rev, str) or _FULL_HEX_RE.fullmatch(source_rev) is None:
        raise RevisionError(
            "source_rev_missing",
            "source_rev is not a full 40- or 64-character commit id",
            "Pass the brief's exact source_rev commit id.",
        )
    proc = run_git(
        ["cat-file", "-e", f"{source_rev}^{{commit}}"],
        Path(workdir),
        check=False,
    )
    if proc.returncode != 0:
        raise RevisionError(
            "source_rev_missing",
            "source_rev is not a commit in this repository",
            "Fetch the brief's source_rev or dispatch against a commit this worktree has.",
        )


def check_applies_at(
    workdir: Path,
    source_rev: str,
    patch: str,
    run_git: RunGit = default_run_git,
) -> None:
    """Prove ``patch`` applies at ``source_rev`` without touching the real index."""
    if not isinstance(source_rev, str) or _FULL_HEX_RE.fullmatch(source_rev) is None:
        raise RevisionError(
            "source_rev_missing",
            "source_rev is not a full 40- or 64-character commit id",
            "Pass the brief's exact source_rev commit id.",
        )
    with tempfile.TemporaryDirectory(prefix="bundlebees-index-") as temp_dir:
        index_path = Path(temp_dir) / "index"
        env_extra = {"GIT_INDEX_FILE": str(index_path)}
        read = run_git(
            ["read-tree", source_rev],
            Path(workdir),
            check=False,
            env_extra=env_extra,
        )
        if read.returncode != 0:
            raise RevisionError(
                "patch_not_at_source_rev",
                "the source revision could not be read into a temporary index",
                "Verify source_rev and rebuild the patch against it.",
            )
        applied = run_git(
            ["apply", "--cached", "--check", "-"],
            Path(workdir),
            input_text=patch,
            check=False,
            env_extra=env_extra,
        )
        if applied.returncode != 0:
            detail = _tail(applied.stderr)
            message = "the patch does not apply at the declared source_rev"
            if detail:
                message = f"{message}: {detail}"
            raise RevisionError(
                "patch_not_at_source_rev",
                message,
                "Rebuild the worker patch against the brief's exact source_rev.",
            )


def check_branch_descends(
    workdir: Path,
    source_rev: str,
    run_git: RunGit = default_run_git,
) -> None:
    """Require ``HEAD`` to descend from ``source_rev``."""
    if not isinstance(source_rev, str) or _FULL_HEX_RE.fullmatch(source_rev) is None:
        raise RevisionError(
            "branch_diverged",
            "source_rev is not a full commit id",
            "Pass the brief's exact source_rev commit id.",
        )
    proc = run_git(
        ["merge-base", "--is-ancestor", source_rev, "HEAD"],
        Path(workdir),
        check=False,
    )
    if proc.returncode != 0:
        raise RevisionError(
            "branch_diverged",
            "the current branch does not descend from source_rev",
            "Rebase the feature branch onto the brief's source_rev and retry.",
        )


def head_sha(workdir: Path, run_git: RunGit = default_run_git) -> str:
    """Return the full HEAD commit id."""
    proc = run_git(["rev-parse", "HEAD"], Path(workdir))
    sha = str(proc.stdout or "").strip()
    if _FULL_HEX_RE.fullmatch(sha) is None:
        raise RevisionError(
            "git_failed",
            "git did not report a full HEAD commit id",
            "Check the worktree and retry.",
        )
    return sha


# ---------------------------------------------------------------------------
# Wave records
# ---------------------------------------------------------------------------


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_wave_id(wave_id: Any) -> bool:
    return isinstance(wave_id, str) and _WAVE_ID_RE.fullmatch(wave_id) is not None


def _safe_attempt_id(attempt_id: Any) -> bool:
    if not isinstance(attempt_id, str):
        return False
    if ".." in attempt_id:
        return False
    return _ATTEMPT_ID_RE.fullmatch(attempt_id) is not None


def load_wave(state_dir: Path, wave_id: str) -> dict | None:
    """Load a wave record, or return ``None`` when it is absent or unreadable."""
    if not _safe_wave_id(wave_id):
        return None
    target = Path(state_dir) / "waves" / f"{wave_id}.json"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return dict(data) if isinstance(data, Mapping) else None


def _wave_is_accepted(state_dir: Path, wave_id: str) -> bool:
    record = load_wave(state_dir, wave_id)
    return bool(record is not None and record.get("accepted") is True)


def attempt_accepted(state_dir: Path, attempt_id: str) -> bool:
    """True only when an accepted wave record lists this attempt."""
    if not _safe_attempt_id(attempt_id):
        return False
    state = Path(state_dir)
    try:
        paths = sorted((state / "waves").glob("*.json"))
    except OSError:
        return False
    for path in paths:
        record = load_wave(state, path.stem)
        if record is None or record.get("accepted") is not True:
            continue
        attempts = record.get("attempts")
        if not isinstance(attempts, (list, tuple)):
            continue
        for item in attempts:
            if item == attempt_id:
                return True
            if isinstance(item, Mapping) and item.get("attempt_id") == attempt_id:
                return True
    return False


def _attempt_problem(
    state_dir: Path,
    attempt_id: str,
    workdir: Path,
    run_git: RunGit,
) -> str | None:
    target = state_dir / "verify-results" / f"{attempt_id}.json"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "verify result is missing"
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "verify result is not valid JSON"
    if not isinstance(data, Mapping):
        return "verify result is not an object"
    if data.get("ok") is not True:
        return "verify result is not ok"
    sha = data.get("sha")
    if not isinstance(sha, str) or _SHA40_RE.fullmatch(sha) is None:
        return "verify result has no full 40-hex sha"
    proc = run_git(
        ["merge-base", "--is-ancestor", sha, "HEAD"],
        workdir,
        check=False,
    )
    if proc.returncode != 0:
        return "verified sha is not an ancestor of HEAD"
    return None


def _junit_regressions(junit: Any) -> int:
    if not isinstance(junit, Mapping):
        return 0
    total = 0
    for key in ("failures", "errors"):
        try:
            total += max(0, int(junit.get(key) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _clean_record(record: Mapping[str, Any], head: str) -> dict:
    problems = record.get("problems") or {}
    clean_problems = {
        str(attempt_id): str(scrub(reason))
        for attempt_id, reason in dict(problems).items()
    }
    suite = scrub(record.get("suite"))
    raw_suites = (record.get("suite") or {}).get("suites") if isinstance(record.get("suite"), Mapping) else None
    if isinstance(suite, dict) and isinstance(raw_suites, Mapping):
        # Category names are fixed labels ("security" would match scrub()'s "uri" key rule).
        suite["suites"] = {
            name: scrub(dict(raw_suites[name]))
            for name in ("regression", "contract", "security")
            if isinstance(raw_suites.get(name), Mapping)
        }
    raw_failures = (record.get("suite") or {}).get("failures") if isinstance(record.get("suite"), Mapping) else None
    if isinstance(suite, dict) and isinstance(raw_failures, Mapping):
        suite["failures"] = {k: (list(v) if isinstance(v, list) else bool(v)) for k, v in raw_failures.items()}
    return {
        "wave_id": record["wave_id"],
        "accepted": bool(record["accepted"]),
        "head_sha": head,
        "attempts": list(record["attempts"]),
        "problems": clean_problems,
        "suite": suite,
        "regressions": int(record["regressions"]),
        "at": record["at"],
    }


def _write_wave(state_dir: Path, wave_id: str, record: dict) -> None:
    if _wave_is_accepted(state_dir, wave_id):
        raise RevisionError(
            "wave_already_accepted",
            "this wave record is already accepted and final",
            "Use a new wave id; accepted waves cannot be overwritten.",
        )
    waves_dir = state_dir / "waves"
    waves_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(waves_dir, 0o700)
    except OSError:
        pass
    target = waves_dir / f"{wave_id}.json"
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    target.write_text(text, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass


def integrate_wave(
    state_dir: Path,
    wave_id: str,
    workdir: Path,
    attempt_ids: list[str],
    run_suite: SuiteRunner,
    run_git: RunGit = default_run_git,
    now: str | None = None,
) -> dict:
    """Integrate verified attempt slices into one wave record.

    Every attempt must have an ``ok`` verify result with a 40-hex sha that is
    an ancestor of ``HEAD``. Only when all slices pass is ``run_suite(workdir)``
    called; the wave is accepted only if that full suite exits zero and does
    not time out. Problems are collected per attempt instead of stopping at the
    first one.
    """
    if not _safe_wave_id(wave_id):
        raise RevisionError(
            "invalid_wave_id",
            "wave id is not a safe record name",
            "Use 1-64 characters from A-Z, a-z, 0-9, dot, underscore, or hyphen.",
        )
    if not isinstance(attempt_ids, (list, tuple)) or not attempt_ids:
        raise RevisionError(
            "invalid_attempt_id",
            "at least one attempt id is required",
            "Pass the attempt ids that the wave integrates.",
        )
    ids: list[str] = []
    for attempt_id in attempt_ids:
        if not _safe_attempt_id(attempt_id):
            raise RevisionError(
                "invalid_attempt_id",
                "an attempt id is not a safe record name",
                "Pass attempt ids exactly as returned by bundlebees_dispatch.",
            )
        if attempt_id not in ids:
            ids.append(attempt_id)

    state = Path(state_dir)
    repo = Path(workdir)
    if _wave_is_accepted(state, wave_id):
        raise RevisionError(
            "wave_already_accepted",
            "this wave record is already accepted and final",
            "Use a new wave id; accepted waves cannot be overwritten.",
        )

    status = run_git(["status", "--porcelain"], repo)
    if str(status.stdout or "").strip():
        raise RevisionError(
            "worktree_dirty",
            "the integration worktree has uncommitted changes",
            "Commit or stash local changes, then retry integration.",
        )

    head = head_sha(repo, run_git)

    problems: dict[str, str] = {}
    for attempt_id in ids:
        reason = _attempt_problem(state, attempt_id, repo, run_git)
        if reason:
            problems[attempt_id] = reason

    suite: dict | None = None
    accepted = False
    if not problems:
        evidence = run_suite(repo)
        suite = dict(evidence) if isinstance(evidence, Mapping) else {}
        # A runner may name test categories that failed or did not run (fail closed).
        accepted = (
            suite.get("exit_code") == 0
            and not bool(suite.get("timed_out"))
            and not suite.get("blocking")
        )

    regressions = 0
    if not accepted and suite is not None:
        regressions = _junit_regressions(suite.get("junit"))

    record = _clean_record(
        {
            "wave_id": wave_id,
            "accepted": accepted,
            "head_sha": head,
            "attempts": ids,
            "problems": problems,
            "suite": suite,
            "regressions": regressions,
            "at": now or _now(),
        },
        head,
    )
    _write_wave(state, wave_id, record)
    return record
