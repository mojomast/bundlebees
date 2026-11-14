"""Deterministic worker for the M4 dogfood acceptance test.

The worker is *scripted*, not scripted-to-pass: it produces one genuine patch per
project, computed as a real ``git diff`` against the pinned base. The controlled
failure lives in T2's first frozen contract (it names a test id that does not
exist), so the worker's honest patch cannot make that selection pass. The same
patch is produced again for the corrective attempt; only the contract changes.

Nothing here asserts and nothing here writes into the fixture repository: the
patches are built in a throwaway clone.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SOURCE_REV_MODULE = '''"""Refuse an acdd/1 contract whose ``source_rev`` is not a full 40-hex sha.

A contract pins the commit its evidence must descend from. If that pin is not a
commit id, every downstream evidence record is unverifiable, so the linter must
report it before anything else is checked. Malformed input stays silent.
"""

from __future__ import annotations

from typing import Any, Mapping

Problem = dict[str, str]

SHA_LENGTH = 40
HEX_DIGITS = frozenset("0123456789abcdef")


def _problem(code: str, where: str, message: str) -> Problem:
    return {"code": code, "where": where, "message": message}


def _where(contract: Mapping[str, Any]) -> str:
    contract_id = contract.get("id")
    if isinstance(contract_id, str) and contract_id.strip():
        return contract_id
    return "source_rev"


def _is_full_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA_LENGTH
        and all(character in HEX_DIGITS for character in value.lower())
    )


def check_source_rev(contract: Mapping[str, Any]) -> list[Problem]:
    """Return one problem for a ``source_rev`` that is not a full 40-hex sha."""
    if not isinstance(contract, Mapping):
        return []
    source_rev = contract.get("source_rev")
    if _is_full_sha(source_rev):
        return []
    shown = "missing" if source_rev is None else repr(source_rev)[:60]
    return [
        _problem(
            "contract_source_rev_malformed",
            _where(contract),
            f"source_rev {shown} is not a full 40-hex commit; pin the revision the evidence descends from",
        )
    ]
'''

REACHABILITY_MODULE = '''"""Refuse an acdd/1 contract whose ``source_rev`` is unreachable from head.

Reachability is a real git question: a commit object can exist in the repository
and still be on no branch anyone can reproduce. With no repository supplied the
check does not guess -- it reports ``checked: False`` with the reason, so an
"unchecked" contract is never mistaken for a checked one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

Problem = dict[str, str]

SHA_LENGTH = 40
HEX_DIGITS = frozenset("0123456789abcdef")


def _problem(code: str, where: str, message: str) -> Problem:
    return {"code": code, "where": where, "message": message}


def _where(contract: Mapping[str, Any]) -> str:
    contract_id = contract.get("id")
    if isinstance(contract_id, str) and contract_id.strip():
        return contract_id
    return "source_rev"


def _is_full_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA_LENGTH
        and all(character in HEX_DIGITS for character in value.lower())
    )


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0"},
        )
    except OSError as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or done.stderr).strip()


def _result(checked: bool, reason: str, problems: list[Problem]) -> dict[str, Any]:
    return {"checked": checked, "reason": reason, "problems": problems}


def check_reachability(contract: Mapping[str, Any], *, repo: Path | str | None,
                       head: str = "HEAD") -> dict[str, Any]:
    """Report whether ``contract.source_rev`` is reachable from ``head`` in *repo*."""
    if not isinstance(contract, Mapping):
        return _result(False, "the contract is not a mapping; reachability was not checked", [])
    if repo is None:
        return _result(False, "no repository supplied; source_rev reachability was not checked", [])

    source_rev = contract.get("source_rev")
    if not _is_full_sha(source_rev):
        return _result(
            True,
            "source_rev is not a full 40-hex commit, so it cannot be reachable",
            [_problem("contract_source_rev_unreachable", _where(contract),
                      f"source_rev {source_rev!r} is not a full 40-hex commit and cannot be reached from {head}")],
        )

    sha = str(source_rev).lower()
    path = Path(repo)
    head_code, head_sha = _git(path, "rev-parse", "--verify", f"{head}^{{commit}}")
    if head_code != 0 or not head_sha:
        return _result(
            True,
            f"cannot resolve {head} in {path}",
            [_problem("contract_source_rev_unreachable", _where(contract),
                      f"source_rev {sha} cannot be checked against an unresolvable {head}")],
        )

    exists, _ = _git(path, "cat-file", "-e", f"{sha}^{{commit}}")
    if exists != 0:
        return _result(
            True,
            f"{sha} is not a commit in {path}",
            [_problem("contract_source_rev_unreachable", _where(contract),
                      f"source_rev {sha} is not a commit in the repository and is unreachable from {head_sha}")],
        )

    ancestor, _ = _git(path, "merge-base", "--is-ancestor", sha, head_sha)
    if ancestor != 0:
        return _result(
            True,
            f"{sha} is not an ancestor of {head_sha}",
            [_problem("contract_source_rev_unreachable", _where(contract),
                      f"source_rev {sha} is not reachable from {head} ({head_sha}); rebase onto the pinned branch")],
        )
    return _result(True, f"{sha} is reachable from {head}", [])
'''

PROJECT_FILES = {
    "acdd-source-rev": {"tools/acdd-lint/acdd_lint/checks_source_rev.py": SOURCE_REV_MODULE},
    "acdd-reachability": {"tools/acdd-lint/acdd_lint/checks_reachability.py": REACHABILITY_MODULE},
}


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "worker@example.invalid",
             "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "worker@example.invalid",
             **(env or {})},
    )
    if done.returncode != 0:
        raise AssertionError(f"git {args} in {cwd}: {done.stderr}")
    return done.stdout


def build_patch(repo: Path, base_rev: str, files: dict[str, str], scratch_root: Path) -> str:
    """A real ``git diff --cached`` for *files* applied to *base_rev*."""

    scratch = Path(tempfile.mkdtemp(prefix="m4-patch-", dir=str(scratch_root)))
    try:
        _git(scratch_root, "clone", "-q", str(repo), str(scratch))
        _git(scratch, "checkout", "-q", base_rev)
        for relpath, body in files.items():
            target = scratch / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        _git(scratch, "add", "--", *files)
        return _git(scratch, "diff", "--cached", "--binary")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def build_patches(repo: Path, base_rev: str, scratch_root: Path) -> dict[str, str]:
    """One genuine patch per project, all diffed against the same pinned base."""

    return {project: build_patch(repo, base_rev, files, scratch_root)
            for project, files in PROJECT_FILES.items()}


def patch_files(patch: str) -> list[str]:
    """The file list a unified diff touches, in first-seen order."""

    found: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                if path not in found:
                    found.append(path)
    return found


class DeterministicWorker:
    """The launcher-side worker: one attempt per POST, one real patch per project.

    The attempt id is deterministic and the artifact is recomputed from the
    patch bytes, so a retry of the same project serves the same genuine change.
    """

    def __init__(self, patches: dict[str, str]) -> None:
        self.patches = dict(patches)
        self.posts: list[dict] = []
        self.by_attempt: dict[str, tuple[str, str]] = {}
        self._counter = 0

    def create(self, brief):
        self._counter += 1
        attempt_id = f"att_m4_{self._counter}"
        record = dict(brief)
        self.posts.append(record)
        self.by_attempt[attempt_id] = (str(brief.get("project") or ""), str(brief.get("source_rev") or ""))
        return {"attempt_id": attempt_id, "state": "succeeded", "brief": record}

    def attempts_raw(self):
        return [
            {"attempt_id": attempt_id, "state": "succeeded",
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "brief": {"project": project, "source_rev": rev}}
            for attempt_id, (project, rev) in self.by_attempt.items()
        ]

    def get_raw(self, attempt_id):
        project, rev = self.by_attempt[attempt_id]
        return {"attempt_id": attempt_id, "state": "succeeded",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "brief": {"project": project, "source_rev": rev}}

    def artifact(self, attempt_id):
        project, rev = self.by_attempt[attempt_id]
        patch = self.patches[project]
        return {"patch": patch, "source_rev": rev,
                "patch_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest()}
