"""Local-only verification: verify a worker patch and commit it locally.

`verify --commit-local` never pushes and never touches the caller's working tree.
It builds an isolated worktree at the pinned base, refuses anything the lease does
not authorise, proves the patch is RED before and GREEN after, runs the regression
suite and gitleaks, and only then creates a local commit on ``bb-verify/<attempt>``.

The resulting commit is what integration consumes: its parent is the pinned base
and its file set is exactly the patch's, so `integrate_wave`'s ancestry checks are
satisfied without rewriting history.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import leases

RECORD_DIR = "verify-results"


class VerifyError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def _run(args: Sequence[str], cwd: Path, *, timeout: float = 600.0, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def record_path(state_dir: Path, attempt_id: str) -> Path:
    return Path(state_dir) / RECORD_DIR / f"{attempt_id}.json"


def read_record(state_dir: Path, attempt_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(record_path(state_dir, attempt_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(state_dir: Path, attempt_id: str, record: Mapping[str, Any]) -> Path:
    path = record_path(state_dir, attempt_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(dict(record), sort_keys=True, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _branch(attempt_id: str) -> str:
    return f"bb-verify/{attempt_id}"


def _files_in_patch(patch: str) -> list[str]:
    found = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                found.add(parts[3][2:] if parts[3].startswith("b/") else parts[3])
    return sorted(found)


def verify_and_commit(
    state_dir: Path,
    repo: Path,
    attempt_id: str,
    *,
    patch: str,
    declared_files: Sequence[str],
    base_rev: str,
    tests: Sequence[str] = (),
    suite_command: Sequence[str] | None = None,
    gitleaks: str | None = None,
    require_gitleaks: bool = True,
    tests_required: bool = True,
    expected_patch_sha256: str | None = None,
    acdd_preflight: Any = None,
    worktree_root: Path | None = None,
    identity: Sequence[str] = ("-c", "user.name=bundlebees", "-c", "user.email=bees@localhost"),
) -> dict[str, Any]:
    """Verify one patch and commit it locally. Returns the verify record."""

    state_dir = Path(state_dir)
    repo = Path(repo)

    # Fail closed on missing evidence rather than recording a hollow pass.
    if tests_required and not list(tests):
        raise VerifyError(
            "tests_required",
            "this contract needs named tests and none were given",
            "Pass the contract's test targets; an empty list is not a pass.",
        )
    if require_gitleaks and not gitleaks:
        raise VerifyError(
            "security_tool_missing",
            "gitleaks is required for verification but is not available",
            "Install gitleaks, or record an explicit exemption in the contract.",
        )
    if expected_patch_sha256:
        actual = hashlib.sha256(patch.encode()).hexdigest()
        if actual != expected_patch_sha256:
            raise VerifyError(
                "artifact_mismatch",
                f"patch sha256 {actual[:12]} does not match the declared {expected_patch_sha256[:12]}",
                "Reject this artifact; its bytes changed after dispatch.",
            )
    if acdd_preflight is not None:
        try:
            acdd_preflight(list(declared_files))
        except Exception as exc:  # noqa: BLE001 - any contract refusal is fatal
            raise VerifyError(
                "contract_violation",
                f"the frozen contract refused this patch: {exc}",
                "Do not verify a patch that changes its own acceptance criteria.",
            ) from exc

    # 1. The lease is the authority; worker claims never widen it.
    try:
        lease = leases.load_lease(state_dir, attempt_id)
    except leases.LeaseError as exc:
        raise VerifyError(exc.code, str(exc), exc.fix) from exc
    lease_files = sorted(leases.lease_files(lease))
    declared = sorted(str(path) for path in declared_files)
    if not declared:
        declared = _files_in_patch(patch)
    if declared != lease_files:
        raise VerifyError(
            "lease_scope_mismatch",
            f"patch files {declared} do not match the lease {lease_files}",
            "Do not verify a patch that touches files outside its lease.",
        )

    # 2. Source revision binding.
    if str(lease.get("source_rev") or "") != str(base_rev):
        raise VerifyError(
            "source_rev_mismatch",
            "the lease and the result disagree about the source revision",
            "Re-dispatch against the pinned revision; do not verify this artifact.",
        )

    # 3. Idempotent restart: an accepted record with a real commit is reused.
    existing = read_record(state_dir, attempt_id)
    if existing and existing.get("ok") is True:
        sha = str(existing.get("sha") or "")
        if not sha or not _commit_exists(repo, sha):
            raise VerifyError(
                "verify_record_stale",
                "a verify record exists but its commit does not",
                "Re-run verification; the record cannot be trusted without its commit.",
            )
        if not _commit_matches(repo, sha, base_rev, declared):
            raise VerifyError(
                "verify_record_stale",
                "the recorded commit no longer matches the pinned base or patch file set",
                "Re-run verification; the record is not evidence for this artifact.",
            )
        if _evidence_complete(existing, require_gitleaks=require_gitleaks, tests_required=tests_required):
            return existing
        # Incomplete evidence: fall through and re-run the checks on this commit.
        existing = None

    branch = _branch(attempt_id)
    root = Path(worktree_root) if worktree_root else repo.parent / f".bb-verify-{attempt_id}"
    worktree = root / "wt"
    if worktree.exists():
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)

    # A commit left by a crashed run is not a verified result: it is rechecked
    # from scratch below, and replaced if it does not survive the checks.
    if _branch_exists(repo, branch):
        _run(["git", "branch", "-D", branch], repo)

    _run(["git", "worktree", "add", "--detach", str(worktree), base_rev], repo)
    try:
        # 4. RED before the patch, when tests are named.
        red: dict[str, Any] | None = None
        if tests:
            red = _run_tests(worktree, tests, suite_command)
            if red["exit_code"] == 0:
                raise VerifyError(
                    "red_missing",
                    "the named tests pass before the patch, so there is no RED evidence",
                    "Fix the contract; a change that changes nothing cannot be accepted.",
                )

        # 5. Apply the patch.
        check = _run(["git", "apply", "--check", "--whitespace=nowarn", "-"], worktree, input_text=patch)
        if check.returncode != 0:
            raise VerifyError("patch_conflict", f"the patch does not apply: {check.stderr.strip()[:300]}", "Re-dispatch against the pinned base.")
        applied = _run(["git", "apply", "--whitespace=nowarn", "-"], worktree, input_text=patch)
        if applied.returncode != 0:
            raise VerifyError("patch_conflict", f"the patch failed to apply: {applied.stderr.strip()[:300]}", "Re-dispatch against the pinned base.")

        touched = [line[3:] for line in _run(["git", "status", "--porcelain"], worktree).stdout.splitlines()]
        touched = sorted({path.split(" -> ")[-1] for path in touched if path})
        if touched != declared:
            raise VerifyError(
                "patch_scope_mismatch",
                f"the applied patch touched {touched}, expected {declared}",
                "Reject this artifact; it changed more than its lease allows.",
            )

        # 6. GREEN after the patch.
        green = _run_tests(worktree, tests, suite_command)
        if green["exit_code"] != 0:
            raise VerifyError("tests_failed", f"named tests failed after the patch: {green.get('tail', '')}", "Reject and re-dispatch.")

        suite = None
        if suite_command:
            suite = _run(list(suite_command), worktree)
            if suite.returncode != 0:
                raise VerifyError("suite_failed", f"regression suite failed: {suite.stdout.strip()[-300:]}", "Reject and re-dispatch.")

        scan = None
        if gitleaks:
            scan = _run([gitleaks, "stdin", "--no-banner", "--redact"], worktree, input_text=patch)
            if scan.returncode != 0:
                raise VerifyError("secrets_found", "gitleaks reported a finding in the patch", "Reject this artifact.")

        # 7. Commit locally, on a branch, never pushed.
        _run(["git"] + list(identity) + ["checkout", "-b", branch], worktree)
        _run(["git", "add", "--", *declared], worktree)
        commit = _run(["git"] + list(identity) + ["commit", "-q", "-m", f"verify: {attempt_id}"], worktree)
        if commit.returncode != 0:
            raise VerifyError("commit_failed", f"the verification commit failed: {commit.stderr.strip()[:200]}", "Inspect the worktree.")
        sha = _rev(worktree, "HEAD")
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)

    # 8. Prove the commit is what we say it is.
    parent = _rev(repo, f"{sha}^")
    if parent != base_rev:
        raise VerifyError("commit_parent_mismatch", f"{sha} is not a child of {base_rev}", "Reject this verification.")
    diff_files = _diff_files(repo, base_rev, sha)
    if diff_files != declared:
        raise VerifyError("commit_scope_mismatch", f"commit touches {diff_files}, expected {declared}", "Reject this verification.")

    record = _record(attempt_id, sha, base_rev, declared, tests, red=red, suite=suite, gitleaks=scan, patch_sha256=hashlib.sha256(patch.encode()).hexdigest())
    _write_record(state_dir, attempt_id, record)
    return record


def _record(attempt_id, sha, base_rev, files, tests, *, red, suite, gitleaks, patch_sha256=None, resumed=False) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "ok": True,
        "sha": sha,
        "branch": _branch(attempt_id),
        "base_rev": base_rev,
        "files": list(files),
        "tests": list(tests),
        "red": None if red is None else {"exit_code": red["exit_code"]},
        "suite": None if suite is None else {"exit_code": suite.returncode},
        "gitleaks": None if gitleaks is None else {"exit_code": gitleaks.returncode},
        "mode": "commit-local",
        "patch_sha256": patch_sha256,
        "resumed": resumed,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _evidence_complete(record: Mapping[str, Any], *, require_gitleaks: bool, tests_required: bool) -> bool:
    """A reused record must carry the evidence this configuration requires."""

    if tests_required and not record.get("tests"):
        return False
    if record.get("red") is None:
        return False
    if record.get("suite") is None:
        return False
    if require_gitleaks and record.get("gitleaks") is None:
        return False
    if not record.get("patch_sha256"):
        return False
    return True


def _run_tests(worktree: Path, tests: Sequence[str], suite_command: Sequence[str] | None) -> dict[str, Any]:
    if suite_command:
        base = list(suite_command)
    else:
        base = ["python", "-m", "pytest", "-q"]
    cmd = base + list(tests)
    proc = _run(cmd, worktree)
    return {"exit_code": proc.returncode, "tail": (proc.stdout or proc.stderr).strip()[-300:]}


def _rev(cwd: Path, ref: str) -> str:
    proc = _run(["git", "rev-parse", ref], cwd)
    if proc.returncode != 0:
        raise VerifyError("git_error", f"cannot resolve {ref}: {proc.stderr.strip()[:200]}", "Inspect the repository.")
    return proc.stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    return _run(["git", "rev-parse", "--verify", "--quiet", branch], repo).returncode == 0


def _commit_exists(repo: Path, sha: str) -> bool:
    return _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo).returncode == 0


def _commit_matches(repo: Path, sha: str, base_rev: str, files: Sequence[str]) -> bool:
    try:
        return _rev(repo, f"{sha}^") == base_rev and _diff_files(repo, base_rev, sha) == sorted(files)
    except VerifyError:
        return False


def _diff_files(repo: Path, base_rev: str, sha: str) -> list[str]:
    proc = _run(["git", "diff", "--name-only", base_rev, sha], repo)
    if proc.returncode != 0:
        raise VerifyError("git_error", f"cannot diff {base_rev}..{sha}", "Inspect the repository.")
    return sorted(line for line in proc.stdout.splitlines() if line.strip())
