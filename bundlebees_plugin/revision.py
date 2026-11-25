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
whose 40-hex sha descends from ``HEAD`` — or, when a squash or replay landing
put the change on ``HEAD`` as a different commit, every path the verified
change touches resolves to exactly the same object at ``HEAD`` (per-file blob
equality, so a landing that also carries other files still proves the files it
shares) — or the change is exactly patch-identical (``git patch-id --stable``)
to a commit ``HEAD`` carries — or, when a landing also changed the content, a
landed-verify record proves the named tests ran green on that exact landing
commit's tree and the commit's message names this attempt — *and* a
caller-supplied full test suite returns a non-timeout exit code of zero. An
accepted wave record is final: later attempts to overwrite it raise
``RevisionError("wave_already_accepted")``.
Records are published through a fsynced temporary file and a create-only link,
so an accepted record is written once and never replaced, and a record that
exists but cannot be read raises ``wave_unreadable``/``wave_malformed`` — it is
never treated as absent permission to integrate again.
The full-suite evidence comes from a disposable sandbox; its ``output_tail`` is
scrubbed before it is stored.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .security import child_env, scrub

BASE_KEYS = ("base_rev", "source_rev", "base_sha", "base")

#: Stable refusal codes for wave evidence. They are deliberately distinct:
#: "there is no record" (normal before acceptance) must never be confused with
#: "there is a record I cannot read" (a refusal to integrate again).
WAVE_MISSING = "wave_missing"
WAVE_UNREADABLE = "wave_unreadable"
WAVE_MALFORMED = "wave_malformed"

#: Distinct problem codes for the refusals that are not about missing
#: evidence. ``verified_change_not_landed`` says the verified commit exists in
#: this checkout but HEAD does not carry its exact content, naming the first
#: path that differs or is missing; ``attempt_repo_foreign`` says the attempt
#: belongs to another repository, so an ancestry message about this worktree
#: would be misleading.
PROBLEM_CHANGE_NOT_LANDED = "verified_change_not_landed"
PROBLEM_FOREIGN_REPO = "attempt_repo_foreign"

#: The landed-verification evidence class. ``landed_verify_not_in_head`` says a
#: record for this attempt's landing commit exists but that commit is not in
#: HEAD's history; ``landed_verify_failed`` says the landed tree's own test run
#: did not pass, which is stronger and more honest than "not landed".
PROBLEM_LANDED_NOT_IN_HEAD = "landed_verify_not_in_head"
PROBLEM_LANDED_FAILED = "landed_verify_failed"

#: Schema written by ``verify-landed`` (mirrors
#: ``verify_local.LANDED_RECORD_SCHEMA``; a test pins the two together).
LANDED_VERIFY_SCHEMA = "rizzstack.verify-landed/1"
LANDED_VERIFY_PREFIX = "landed-"
LANDED_VERIFY_METHOD = "landed-tests"

#: Upper bound on landed records read for one integrate call. Records are
#: operator-produced, one per landed commit; the bound keeps a refusal cheap.
LANDED_VERIFY_RECORD_LIMIT = 200

#: How a non-ancestor landing was proven. ``blob-equality`` is the general
#: rule: every path the verified change touches has the same object at HEAD.
#: ``patch-id`` is only an extra acceptance path for a landing that is exactly
#: the verified change and nothing else.
REPLAY_METHOD_BLOB_EQUALITY = "blob-equality"
REPLAY_METHOD_PATCH_ID = "patch-id"

#: How far back HEAD's own history is scanned for a patch-identical landing.
#: Ancestry is still the fast path; this bound keeps a refusal cheap.
LANDED_EVIDENCE_SCAN_LIMIT = 200

#: Upper bound on the paths a single blob-equality proof will record. A bee
#: slice is file-leased and small; a change above this is refused rather than
#: silently truncated, because the record must name every proven file.
REPLAY_EVIDENCE_PATH_LIMIT = 512


_WAVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FULL_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
_SHA40_RE = re.compile(r"[0-9a-fA-F]{40}")
_PATCH_ID_RE = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?")
_ZERO_OID_RE = re.compile(r"0{40}(?:0{24})?")
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


def _wave_number(value: Any) -> int:
    """Accept the job schema's ``"w1"`` as well as a plain integer."""
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, value)
    text = str(value or "").strip().lower().lstrip("w")
    try:
        return max(1, int(text))
    except ValueError:
        return 1


def wave_id_for(job_id: str, wave: Any = 1) -> str:
    """The deterministic, job-scoped record id for one job's wave.

    Identical to ``integrate_job.job_wave_id`` (a test pins the agreement): a
    wave record must be derivable from the job document alone, because after a
    crash the record is the only proof that the wave was accepted.
    """
    text = str(job_id or "").strip()
    if not text:
        raise RevisionError("job_invalid", "a wave record needs a job id", "Pass the job's own id.")
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text)
    suffix = f".w{_wave_number(wave)}"
    if len(safe) + len(suffix) > 64:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        safe = f"{safe[:44]}.{digest}"
    return f"{safe}{suffix}"


def waves_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "waves"


def wave_path(state_dir: Path, wave_id: str) -> Path:
    if not _safe_wave_id(wave_id):
        raise RevisionError(
            "invalid_wave_id",
            "wave id is not a safe record name",
            "Use 1-64 characters from A-Z, a-z, 0-9, dot, underscore, or hyphen.",
        )
    return waves_dir(state_dir) / f"{wave_id}.json"


def read_wave(state_dir: Path, wave_id: str) -> dict:
    """Read a wave record, keeping "absent" and "unreadable" distinct.

    ``wave_missing`` is the only normal answer: the wave has not been accepted
    yet. ``wave_unreadable`` and ``wave_malformed`` are refusals — a record that
    exists and cannot be read may be the accepted evidence that forbids a
    second integration, so it is never treated as absent.
    """
    target = wave_path(state_dir, wave_id)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RevisionError(
            WAVE_MISSING,
            f"no wave record exists for {wave_id}",
            "Absent evidence is legal only before the wave is accepted.",
        ) from exc
    except OSError as exc:
        raise RevisionError(
            WAVE_UNREADABLE,
            f"wave record {wave_id} cannot be read: {exc}",
            "Inspect the waves directory by hand; do not integrate again until it is understood.",
        ) from exc
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RevisionError(
            WAVE_MALFORMED,
            f"wave record {wave_id} is not valid JSON: {exc}",
            "Inspect the waves directory by hand; a corrupt record is never permission to integrate again.",
        ) from exc
    if not isinstance(data, Mapping):
        raise RevisionError(
            WAVE_MALFORMED,
            f"wave record {wave_id} is not a JSON object",
            "Inspect the waves directory by hand; a corrupt record is never permission to integrate again.",
        )
    return dict(data)


def read_wave_or_none(state_dir: Path, wave_id: str) -> dict | None:
    """``read_wave`` with the one normal case (absence) mapped to ``None``."""
    try:
        return read_wave(state_dir, wave_id)
    except RevisionError as exc:
        if exc.code == WAVE_MISSING:
            return None
        raise


def load_wave(state_dir: Path, wave_id: str) -> dict | None:
    """Load a wave record, or ``None`` when it is absent or unreadable.

    Kept for reporting callers. Any path that decides whether to integrate
    again must use :func:`read_wave`, which distinguishes absence from damage.
    """
    if not _safe_wave_id(wave_id):
        return None
    try:
        return read_wave(state_dir, wave_id)
    except RevisionError:
        return None


def _fsync_dir(path: Path) -> None:
    """Persist the directory entry created by a link or rename (best effort).

    Filesystems that cannot fsync a directory answer ``EINVAL``/``ENOTSUP``;
    that is not a reason to refuse the record, but the attempt is always made.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def attempt_accepted(state_dir: Path, attempt_id: str) -> bool:
    """True only when an accepted wave record lists this attempt.

    A wave file that cannot be read is an error, not "no accepted wave": it may
    be exactly the record that says this attempt was integrated.
    """
    if not _safe_attempt_id(attempt_id):
        return False
    state = Path(state_dir)
    try:
        paths = sorted((state / "waves").glob("*.json"))
    except OSError:
        return False
    for path in paths:
        if not _safe_wave_id(path.stem):
            continue
        record = read_wave(state, path.stem)
        if record.get("accepted") is not True:
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


def _strip_remote_origin(text: str) -> str:
    """Drop a scheme or an scp-like ``user@host:`` prefix from a remote."""
    if "://" in text:
        return text.split("://", 1)[1]
    if "@" in text and ":" in text and text.index("@") < text.index(":"):
        return text.split(":", 1)[1]
    return text


def _repo_identity(value: Any) -> tuple[str, str] | None:
    """Normalise a repository reference to ``(slug-or-name, final name)``.

    Accepts ``owner/name``, an https or scp-like remote URL, or a local path.
    ``None`` means "not a usable identity", and callers must not compare it: a
    folder name alone is never evidence about which repository a lease named.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or "\x00" in text or any(ch.isspace() for ch in text):
        return None
    text = _strip_remote_origin(text.rstrip("/"))
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    if not parts:
        return None
    name = parts[-1].lower()
    if len(parts) == 1:
        return name, name
    return f"{parts[-2].lower()}/{name}", name


def _same_repository(left: Any, right: Any) -> bool | None:
    """Tri-state repository comparison: True same, False foreign, None unknown.

    Final-name equality is treated as the same repository on purpose: a local
    clone path has no owner component, and a fork that shares the name must
    never be refused by a guess. Only two *different* normalised names are
    proof of a foreign repository.
    """
    first = _repo_identity(left)
    second = _repo_identity(right)
    if first is None or second is None:
        return None
    return first[0] == second[0] or first[1] == second[1]


def _workdir_repository(workdir: Path, run_git: RunGit) -> str | None:
    """The worktree's declared ``origin`` remote, never its folder name.

    A linked integration worktree shares the repository config, so it answers
    exactly like its main checkout. No origin means the repository cannot be
    compared, and the caller must not invent one from a path.
    """
    proc = run_git(["config", "--get", "remote.origin.url"], workdir, check=False)
    if proc.returncode != 0:
        return None
    text = str(proc.stdout or "").strip()
    return text or None


def _attempt_repository(state_dir: Path, attempt_id: str) -> str | None:
    """The repository an attempt was dispatched for, when a record names it.

    The lease's brief is the dispatch-time statement; the job document's
    ``goal.repo`` is the sealed authorization when the brief does not repeat
    it. Anything unreadable or absent is simply "not named" — this evidence
    only explains a refusal, it never authorizes one on its own.
    """
    try:
        from . import leases  # noqa: PLC0415 - keep revision's import surface small

        lease = leases.load_lease(state_dir, attempt_id)
    except Exception:  # noqa: BLE001 - a missing or damaged lease is not this gate's refusal
        return None
    brief = lease.get("brief")
    brief = brief if isinstance(brief, Mapping) else {}
    for candidate in (brief.get("repo"), lease.get("repo")):
        text = candidate.strip() if isinstance(candidate, str) else ""
        if text:
            return text
    job_id = brief.get("job_id") or lease.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    try:
        from . import job as jobs  # noqa: PLC0415 - job imports revision lazily

        job = jobs.load_job(state_dir, job_id.strip(), recover=False)
    except Exception:  # noqa: BLE001 - the job record is optional evidence here
        return None
    goal = job.get("goal") if isinstance(job, Mapping) else None
    repo = goal.get("repo") if isinstance(goal, Mapping) else None
    text = repo.strip() if isinstance(repo, str) else ""
    return text or None


def _patch_id_index(output: str) -> dict[str, str]:
    """Parse ``git patch-id`` lines into ``{patch_id: commit}`` (newest wins)."""
    index: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or not _PATCH_ID_RE.fullmatch(parts[0]):
            continue
        commit = parts[1].lower()
        if not _PATCH_ID_RE.fullmatch(commit) or _ZERO_OID_RE.fullmatch(commit):
            continue
        index.setdefault(parts[0].lower(), commit)
    return index


def _commit_patch_id(run_git: RunGit, workdir: Path, rev: str) -> str | None:
    """``git patch-id --stable`` of one commit's change over its first parent.

    ``git log -p`` is used rather than ``git diff`` so the verified commit and
    the commits HEAD carries are rendered through exactly the same code path.
    A root or merge commit has no single change of its own and yields ``None``.
    """
    rendered = run_git(
        [
            "log", "-p", "-n", "1", "--no-merges", "--no-color", "--no-ext-diff",
            "--no-show-signature", "--format=commit %H", rev,
        ],
        workdir,
        check=False,
    )
    if rendered.returncode != 0 or not str(rendered.stdout or "").strip():
        return None
    hashed = run_git(
        ["patch-id", "--stable"],
        workdir,
        input_text=str(rendered.stdout),
        check=False,
    )
    if hashed.returncode != 0:
        return None
    for line in str(hashed.stdout or "").splitlines():
        parts = line.split()
        if parts and _PATCH_ID_RE.fullmatch(parts[0]):
            return parts[0].lower()
    return None


def _changed_paths(run_git: RunGit, workdir: Path, sha: str) -> list[str] | None:
    """The paths one commit changes against its first parent, sorted.

    ``--no-renames`` keeps the set deterministic: a rename is a deleted path
    plus an added path, and the deleted side must be reported as missing at
    HEAD instead of being silently followed. A root commit has no parent, so
    its whole tree is its change. ``-z`` keeps unusual names intact.
    """
    parent = run_git(["rev-parse", "--verify", "--quiet", f"{sha}^"], workdir, check=False)
    if parent.returncode == 0:
        proc = run_git(
            ["diff", "--name-only", "--no-renames", "-z", f"{sha}^", sha],
            workdir,
            check=False,
        )
    else:
        proc = run_git(["ls-tree", "-r", "--name-only", "-z", sha], workdir, check=False)
    if proc.returncode != 0:
        return None
    return sorted({path for path in str(proc.stdout or "").split("\x00") if path})


def _blob_at(run_git: RunGit, workdir: Path, rev: str, path: str) -> str | None:
    """The object ``rev:path`` resolves to, or ``None`` when it is absent.

    ``rev-parse`` looks the name up literally, so a file whose name looks like
    a glob can never match some other path. A submodule entry resolves to its
    commit and is compared the same way as a blob: the pointer must match.
    """
    proc = run_git(
        ["rev-parse", "--verify", "--quiet", f"{rev}:{path}"],
        workdir,
        check=False,
    )
    if proc.returncode != 0:
        return None
    lines = [line for line in str(proc.stdout or "").splitlines() if line]
    if len(lines) != 1 or _SHA40_RE.fullmatch(lines[0]) is None:
        return None
    return lines[0].lower()


def _blob_equality_proof(
    run_git: RunGit,
    workdir: Path,
    sha: str,
    head: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Prove every path the verified change touches is byte-identical at HEAD.

    Exactness is the point: object ids are compared, so a landing that carries
    extra commits (a squash bundling the RED test commit, say) still passes
    while one byte of difference in a shared file fails. The first failing path
    in sorted order is named, and a path HEAD does not have is reported as
    missing instead of raising.
    """
    paths = _changed_paths(run_git, workdir, sha)
    if paths is None:
        return None, (
            f"{PROBLEM_CHANGE_NOT_LANDED}: the verified commit exists, but its change "
            "cannot be read from this object store"
        )
    if not paths:
        return None, (
            f"{PROBLEM_CHANGE_NOT_LANDED}: the verified commit changes no paths, so it "
            "proves nothing about HEAD"
        )
    if len(paths) > REPLAY_EVIDENCE_PATH_LIMIT:
        return None, (
            f"{PROBLEM_CHANGE_NOT_LANDED}: the verified commit touches {len(paths)} paths, "
            f"above the {REPLAY_EVIDENCE_PATH_LIMIT}-path evidence limit"
        )

    verified: dict[str, str] = {}
    for path in paths:
        blob = _blob_at(run_git, workdir, sha, path)
        if blob is None:
            return None, (
                f"{PROBLEM_CHANGE_NOT_LANDED}: path {path} has no object at the verified "
                "commit"
            )
        verified[path] = blob

    for path in paths:
        landed = _blob_at(run_git, workdir, head, path)
        if landed is None:
            return None, (
                f"{PROBLEM_CHANGE_NOT_LANDED}: path {path} is missing at HEAD, so the "
                "verified change is not landed intact"
            )
        if landed != verified[path]:
            return None, (
                f"{PROBLEM_CHANGE_NOT_LANDED}: path {path} differs at HEAD "
                f"(verified blob {verified[path]}, HEAD blob {landed})"
            )
    return verified, None


class _LandedChanges:
    """Bounded, lazy patch-id index of the changes HEAD already carries.

    The fast path is ancestry. This index is read only when a verified commit
    exists in the object store but is not an ancestor, because a merge queue
    may squash- or replay-land the identical change as a different commit.
    Only an exact ``git patch-id --stable`` match counts, so the proof is that
    the merged change *is* the verified change — never that it resembles it.
    """

    def __init__(
        self,
        run_git: RunGit,
        workdir: Path,
        head: str,
        limit: int = LANDED_EVIDENCE_SCAN_LIMIT,
    ) -> None:
        self._run_git = run_git
        self._workdir = Path(workdir)
        self._head = head
        self._limit = max(1, int(limit))
        self._changes: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._changes is not None:
            return self._changes
        self._changes = {}
        rendered = self._run_git(
            [
                "log", "-p", "--no-merges", "--no-color", "--no-ext-diff",
                "--no-show-signature", f"--max-count={self._limit}",
                "--format=commit %H", self._head,
            ],
            self._workdir,
            check=False,
        )
        if rendered.returncode != 0:
            return self._changes
        hashed = self._run_git(
            ["patch-id", "--stable"],
            self._workdir,
            input_text=str(rendered.stdout or ""),
            check=False,
        )
        if hashed.returncode == 0:
            self._changes = _patch_id_index(str(hashed.stdout or ""))
        return self._changes

    def find(self, patch_id: str | None) -> str | None:
        """The commit on HEAD whose change has exactly this patch identity."""
        if not patch_id:
            return None
        return self._load().get(patch_id)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Read one JSON object from disk; anything unreadable or non-object is None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return dict(data) if isinstance(data, Mapping) else None


def _message_names_attempt(message: str, attempt_id: str) -> bool:
    """Whether a commit message names this attempt id as a whole token.

    The landing convention (``bundlebees: apply verified attempt <id>``) is the
    only link a landed-verify record has to an attempt. A substring match would
    let ``att_ab`` claim ``att_abc``'s landing, so the id must stand alone.
    """
    if not message or not attempt_id:
        return False
    pattern = rf"(?<![A-Za-z0-9._-]){re.escape(attempt_id)}(?![A-Za-z0-9._-])"
    return re.search(pattern, message) is not None


class _LandedVerifications:
    """Landed-commit verify records attributed to one attempt, lazily.

    ``verify-landed`` runs the named tests on the exact tree of a landed commit
    and writes ``verify-results/landed-<sha>.json``. The record names no
    attempt, so attribution uses the landing convention this repository already
    writes: the landing commit's message carries the attempt id. A record is
    consumable only when its filename and body name the same full sha, the
    schema is exact, the run passed, and that sha is in HEAD's history. A
    record for a commit outside HEAD is reported as its own problem; a failed
    run at an in-HEAD commit is reported as a failed landed verification.
    """

    def __init__(
        self,
        state_dir: Path,
        run_git: RunGit,
        workdir: Path,
        limit: int = LANDED_VERIFY_RECORD_LIMIT,
    ) -> None:
        self._state = Path(state_dir)
        self._run_git = run_git
        self._workdir = Path(workdir)
        self._limit = max(1, int(limit))
        self._records: dict[str, dict[str, Any]] | None = None
        self._messages: dict[str, str | None] = {}
        self._ancestors: dict[str, bool] = {}

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._records is not None:
            return self._records
        self._records = {}
        directory = self._state / "verify-results"
        try:
            paths = sorted(directory.glob(f"{LANDED_VERIFY_PREFIX}*.json"))[: self._limit]
        except OSError:
            return self._records
        for path in paths:
            sha = path.stem[len(LANDED_VERIFY_PREFIX):].lower()
            if _SHA40_RE.fullmatch(sha) is None:
                continue
            data = _read_json_object(path)
            if data is None or data.get("schema") != LANDED_VERIFY_SCHEMA:
                continue
            body_sha = data.get("sha")
            if not isinstance(body_sha, str) or body_sha.lower() != sha:
                continue
            tests = data.get("tests")
            if not isinstance(tests, list) or not tests:
                continue
            named = [
                str(item).strip()
                for item in tests
                if isinstance(item, str) and item.strip()
            ]
            if len(named) != len(tests):
                continue
            self._records[sha] = {
                "sha": sha,
                "ok": data.get("ok") is True,
                "tests": named,
                "record": f"verify-results/{LANDED_VERIFY_PREFIX}{sha}.json",
            }
        return self._records

    def _message(self, sha: str) -> str | None:
        if sha in self._messages:
            return self._messages[sha]
        proc = self._run_git(
            ["log", "-1", "--no-color", "--no-show-signature", "--format=%B", sha],
            self._workdir,
            check=False,
        )
        self._messages[sha] = str(proc.stdout or "")[:20_000] if proc.returncode == 0 else None
        return self._messages[sha]

    def _in_head(self, sha: str) -> bool:
        if sha in self._ancestors:
            return self._ancestors[sha]
        proc = self._run_git(
            ["merge-base", "--is-ancestor", sha, "HEAD"], self._workdir, check=False
        )
        self._ancestors[sha] = proc.returncode == 0
        return self._ancestors[sha]

    def for_attempt(self, attempt_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """The landed-tests proof for this attempt, or the most precise refusal."""
        proof: dict[str, Any] | None = None
        failed: str | None = None
        elsewhere: list[str] = []
        for sha, record in sorted(self._load().items()):
            message = self._message(sha)
            if message is None or not _message_names_attempt(message, attempt_id):
                continue
            if not self._in_head(sha):
                elsewhere.append(sha)
                continue
            if record["ok"]:
                if proof is None:
                    proof = {
                        "landed_sha": sha,
                        "tests": list(record["tests"]),
                        "record": record["record"],
                        "method": LANDED_VERIFY_METHOD,
                    }
            elif failed is None:
                failed = (
                    f"{PROBLEM_LANDED_FAILED}: the landed verify record for {sha} ran the "
                    "named tests at that commit and did not pass"
                )
        if proof is not None:
            return proof, None
        if failed is not None:
            return None, failed
        if elsewhere:
            return None, (
                f"{PROBLEM_LANDED_NOT_IN_HEAD}: a landed verify record exists for "
                f"{sorted(elsewhere)[0]}, but that commit is not in HEAD's history"
            )
        return None, None


def _verified_sha(state_dir: Path, attempt_id: str) -> tuple[str | None, str | None]:
    """The verified 40-hex sha for one attempt, or the record's refusal reason."""
    target = state_dir / "verify-results" / f"{attempt_id}.json"
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None, "verify result is missing"
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None, "verify result is not valid JSON"
    if not isinstance(data, Mapping):
        return None, "verify result is not an object"
    if data.get("ok") is not True:
        return None, "verify result is not ok"
    sha = data.get("sha")
    if not isinstance(sha, str) or _SHA40_RE.fullmatch(sha) is None:
        return None, "verify result has no full 40-hex sha"
    return sha, None


def _foreign_repository_problem(
    state_dir: Path,
    attempt_id: str,
    workdir_repository: str | None,
) -> str | None:
    """The refusal when the attempt names a repository that is not this one.

    Reported identities are normalised, never the raw remote: an origin URL can
    carry credentials and a brief is untrusted input. Anything the records do
    not name, or cannot be compared, is not a refusal.
    """
    declared = _attempt_repository(state_dir, attempt_id)
    declared_identity = _repo_identity(declared)
    workdir_identity = _repo_identity(workdir_repository)
    if declared_identity is None or workdir_identity is None:
        return None
    if _same_repository(declared, workdir_repository) is not False:
        return None
    return (
        f"{PROBLEM_FOREIGN_REPO}: the attempt was dispatched for {declared_identity[0]}, "
        f"but --workdir is {workdir_identity[0]}"
    )


def _attempt_evidence(
    state_dir: Path,
    attempt_id: str,
    workdir: Path,
    run_git: RunGit,
    *,
    workdir_repository: str | None = None,
    landed: _LandedChanges | None = None,
    landed_verify: _LandedVerifications | None = None,
    head: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """One attempt's refusal, plus the replay proof when equivalence passed.

    Order matters. A repository check refuses a cross-repo attempt by name
    before any ancestry message can mislead. Ancestry is then the fast path. A
    commit that is absent from this object store keeps the historical refusal
    unless a landed-verify record covers this attempt's landing commit. Only
    then is a landing proven: exact patch identity is an extra acceptance path
    for a landing that is the verified change and nothing else, and per-file
    blob equality is the general rule a squash landing bundling other commits
    still satisfies. A landing whose content drifted (so no equality proof can
    pass) may still be accepted by that commit's own landed test run, which is
    a different evidence class, never an equivalence claim. ``None`` proof
    means the attempt passed on ancestry.
    """
    sha, reason = _verified_sha(state_dir, attempt_id)
    if reason is not None or sha is None:
        return reason, None

    foreign = _foreign_repository_problem(state_dir, attempt_id, workdir_repository)
    if foreign is not None:
        return foreign, None

    ancestor = run_git(["merge-base", "--is-ancestor", sha, "HEAD"], workdir, check=False)
    if ancestor.returncode == 0:
        return None, None

    # The landed evidence class: the landing commit's exact tree was tested.
    landed_proof: dict[str, Any] | None = None
    landed_problem: str | None = None
    if landed_verify is not None:
        landed_proof, landed_problem = landed_verify.for_attempt(attempt_id)

    present = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], workdir, check=False)
    if present.returncode != 0:
        if landed_proof is not None:
            return None, landed_proof
        return landed_problem or "verified sha is not an ancestor of HEAD", None

    target = head.strip() if isinstance(head, str) else ""
    if _SHA40_RE.fullmatch(target) is None:
        target = "HEAD"

    patch_id = _commit_patch_id(run_git, workdir, sha)
    landed_sha = landed.find(patch_id) if landed is not None else None
    if landed_sha is not None:
        return None, {
            "verified_sha": sha,
            "landed_sha": landed_sha,
            "patch_id": str(patch_id),
            "method": REPLAY_METHOD_PATCH_ID,
        }

    blobs, problem = _blob_equality_proof(run_git, workdir, sha, target)
    if problem is None:
        return None, {
            "verified_sha": sha,
            "verified_blobs": dict(blobs or {}),
            "method": REPLAY_METHOD_BLOB_EQUALITY,
        }
    if landed_proof is not None:
        return None, landed_proof
    return landed_problem or problem, None


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


def suite_passed(suite: Any) -> bool:
    """Writing and consuming an accepted wave require the same positive evidence."""
    return (isinstance(suite, Mapping) and type(suite.get("exit_code")) is int
            and suite["exit_code"] == 0 and not suite.get("timed_out") and not suite.get("blocking"))


def _safe_replay_path(path: str) -> bool:
    """Whether a diff path is storable audit text: relative, printable, short."""
    if not path or len(path) > 400 or path.startswith("/"):
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    return not any(part in ("", ".", "..") for part in path.split("/"))


def _clean_blob_entry(path: Any, blob: Any) -> tuple[str, str] | None:
    """One stored proof row, or ``None`` when either side is not safe text."""
    if not isinstance(path, str) or not _safe_replay_path(path):
        return None
    if not isinstance(blob, str) or _SHA40_RE.fullmatch(blob) is None:
        return None
    return str(scrub(path)), blob.lower()


def _clean_blob_map(value: Any) -> dict[str, str] | None:
    """The verified blob ids, keyed by path; ``None`` drops the whole proof."""
    if not isinstance(value, Mapping) or not value:
        return None
    if len(value) > REPLAY_EVIDENCE_PATH_LIMIT:
        return None
    clean: dict[str, str] = {}
    for path, blob in value.items():
        entry = _clean_blob_entry(path, blob)
        if entry is None:
            return None
        clean[entry[0]] = entry[1]
    return clean


def _clean_patch_id_fields(proof: Mapping[str, Any]) -> dict[str, str] | None:
    """The landed commit and patch hash of a patch-id proof, or ``None``."""
    landed_sha = proof.get("landed_sha")
    patch_id = proof.get("patch_id")
    if not isinstance(landed_sha, str) or _SHA40_RE.fullmatch(landed_sha) is None:
        return None
    if not isinstance(patch_id, str) or _PATCH_ID_RE.fullmatch(patch_id) is None:
        return None
    return {"landed_sha": landed_sha.lower(), "patch_id": patch_id.lower()}


def _clean_replay_proof(proof: Any) -> dict[str, Any] | None:
    """One attempt's proof, restricted to fixed, hash-shaped fields.

    Proof is audit evidence, never a substitute for the suite: it names exactly
    which verified change a landing was proven equal to. A blob-equality proof
    carries the verified blob id for every path it proved; a patch-id proof
    carries the landed commit and the patch hash. Anything else is dropped, so
    a malformed proof can never make an accepted record look stronger than the
    gate that produced it.
    """
    if not isinstance(proof, Mapping):
        return None
    verified = proof.get("verified_sha")
    method = proof.get("method")
    if not isinstance(verified, str) or _SHA40_RE.fullmatch(verified) is None:
        return None
    entry: dict[str, Any] = {"verified_sha": verified.lower()}
    if method == REPLAY_METHOD_BLOB_EQUALITY:
        blobs = _clean_blob_map(proof.get("verified_blobs"))
        if blobs is None:
            return None
        entry["verified_blobs"] = blobs
    elif method == REPLAY_METHOD_PATCH_ID:
        fields = _clean_patch_id_fields(proof)
        if fields is None:
            return None
        entry.update(fields)
    else:
        return None
    entry["method"] = str(method)
    return entry


def _clean_replay(
    replay: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Every attempt's cleaned replay proof, keyed by attempt id."""
    clean: dict[str, dict[str, Any]] = {}
    for attempt_id, proof in dict(replay or {}).items():
        entry = _clean_replay_proof(proof)
        if entry is not None:
            clean[str(attempt_id)] = entry
    return clean


def _clean_landed_tests(value: Any) -> list[str] | None:
    """The landed record's test targets as storable audit text, or ``None``."""
    if not isinstance(value, list) or not value or len(value) > 50:
        return None
    clean: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if not text or len(text) > 300:
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            return None
        clean.append(str(scrub(text)))
    return clean


def _clean_landed(
    landed: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Every attempt's landed-tests proof, restricted to fixed safe fields.

    The record name is reconstructed from the validated sha rather than copied
    from the record, so a stored path can never be attacker-chosen.
    """
    clean: dict[str, dict[str, Any]] = {}
    for attempt_id, proof in dict(landed or {}).items():
        if not isinstance(proof, Mapping):
            continue
        sha = proof.get("landed_sha")
        if not isinstance(sha, str) or _SHA40_RE.fullmatch(sha) is None:
            continue
        tests = _clean_landed_tests(proof.get("tests"))
        if tests is None:
            continue
        clean[str(attempt_id)] = {
            "landed_sha": sha.lower(),
            "record": f"verify-results/{LANDED_VERIFY_PREFIX}{sha.lower()}.json",
            "tests": tests,
            "method": LANDED_VERIFY_METHOD,
        }
    return clean


def _clean_record(
    record: Mapping[str, Any],
    head: str,
    replay: Mapping[str, Mapping[str, Any]] | None = None,
    landed: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict:
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
    clean = {
        "wave_id": record["wave_id"],
        "accepted": bool(record["accepted"]),
        "head_sha": head,
        "attempts": list(record["attempts"]),
        "problems": clean_problems,
        "suite": suite,
        "regressions": int(record["regressions"]),
        "at": record["at"],
    }
    # Replay proof is audit evidence, never a substitute for the suite: it
    # records exactly which verified change a landed commit was proven equal
    # to. Only the three fixed, hash-shaped fields are stored.
    clean_replay = _clean_replay(replay)
    if clean_replay:
        clean["replay_evidence"] = clean_replay
    # Landed-tests proof is a separate evidence class: the landing commit's own
    # tree was tested and passed, which says nothing about content equality.
    clean_landed = _clean_landed(landed)
    if clean_landed:
        clean["landed_evidence"] = clean_landed
    return clean


def _lost_race(state_dir: Path, wave_id: str) -> RevisionError:
    """Another writer published this wave id first; read what it published."""
    record = read_wave_or_none(state_dir, wave_id)
    if record is not None and record.get("accepted") is True:
        return RevisionError(
            "wave_already_accepted",
            "this wave record is already accepted and final",
            "Use a new wave id; accepted waves cannot be overwritten.",
        )
    return RevisionError(
        "wave_exists",
        f"wave record {wave_id} was published by another writer",
        "Use a new wave id; a wave record is written once.",
    )


def _write_wave(state_dir: Path, wave_id: str, record: dict) -> None:
    """Publish a wave record atomically, durably, and create-only when accepted.

    The bytes go to a private temporary file that is fsynced, then linked into
    place (create-only) or renamed over a *non-accepted* record, and finally the
    directory is fsynced. Two concurrent writers therefore cannot both publish
    an accepted record, and one cannot replace another's accepted record with a
    partially written file. A record that exists but cannot be read is refused:
    it may be the accepted evidence this write would destroy.
    """
    state = Path(state_dir)
    directory = waves_dir(state)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = directory / f"{wave_id}.json"
    existing = read_wave_or_none(state, wave_id)
    if existing is not None and existing.get("accepted") is True:
        raise RevisionError(
            "wave_already_accepted",
            "this wave record is already accepted and final",
            "Use a new wave id; accepted waves cannot be overwritten.",
        )

    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = directory / f".{wave_id}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        if existing is None:
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise _lost_race(state, wave_id) from None
            except OSError:
                # Filesystems without hard links: exclusive create is still atomic.
                try:
                    direct = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise _lost_race(state, wave_id) from None
                with os.fdopen(direct, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(target, 0o600)
        else:
            # A non-accepted record is an attempt log, not authority: it may be
            # replaced, but only after it has been read and understood.
            os.replace(tmp, target)
        _fsync_dir(directory)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass

    if record.get("accepted") is True:
        written = read_wave_or_none(state, wave_id)
        if written is None or written.get("accepted") is not True:
            raise RevisionError(
                "wave_write_failed",
                f"wave record {wave_id} did not survive its own write",
                "Inspect the state directory: the accepted record is not durable.",
            )


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
    either an ancestor of ``HEAD`` (the fast path) or, when the checkout does
    not carry the verified commit itself, landed with every path the verified
    change touches byte-identical at ``HEAD`` (a squash or replay landing,
    proven per file so a landing that also carries other files still passes),
    or exactly patch-identical to a commit ``HEAD`` carries, or covered by a
    landed-verify record for the landing commit that names this attempt (a
    landing whose content drifted but whose own tree passed the named tests).
    A foreign-repository attempt is refused by name rather than with an
    ancestry message. Only when every slice passes is ``run_suite(workdir)``
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
    # Existing evidence is read strictly, before any git or suite work. An
    # accepted record is final; a record that exists and cannot be read is a
    # refusal, never permission to integrate a second time; a readable,
    # non-accepted record is only an attempt log, so a retry may replace it.
    existing = read_wave_or_none(state, wave_id)
    if existing is not None and existing.get("accepted") is True:
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
    workdir_repository = _workdir_repository(repo, run_git)
    landed = _LandedChanges(run_git, repo, head)
    landed_verify = _LandedVerifications(state, run_git, repo)

    problems: dict[str, str] = {}
    replay: dict[str, dict[str, Any]] = {}
    landed_evidence: dict[str, dict[str, Any]] = {}
    for attempt_id in ids:
        reason, proof = _attempt_evidence(
            state,
            attempt_id,
            repo,
            run_git,
            workdir_repository=workdir_repository,
            landed=landed,
            landed_verify=landed_verify,
            head=head,
        )
        if reason:
            problems[attempt_id] = reason
        elif proof:
            if proof.get("method") == LANDED_VERIFY_METHOD:
                landed_evidence[attempt_id] = proof
            else:
                replay[attempt_id] = proof

    suite: dict | None = None
    accepted = False
    if not problems:
        evidence = run_suite(repo)
        suite = dict(evidence) if isinstance(evidence, Mapping) else {}
        # A runner may name test categories that failed or did not run (fail closed).
        accepted = suite_passed(suite)

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
        replay,
        landed_evidence,
    )
    _write_wave(state, wave_id, record)
    return record
