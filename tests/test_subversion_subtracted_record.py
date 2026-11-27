"""The repo-backed scan records the pre-existing findings it subtracts.

``test_subversion_base_subtraction.py`` owns the behaviour (a base shape is not
refused); this file pins the EVIDENCE half: the verify path must show which
findings were removed because the base revision already carried them. Without
that record a subtraction change can silently widen while every refusal stays
green, and the reviewer cannot tell "nothing found" from "everything
subtracted".

Fixtures are real git repositories: no network, launcher, sandbox or worker
code is involved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bundlebees_plugin import cli, subversion_scan, verify_local

LEGACY = '''"""Legacy service module (fixture)."""

import sys

sys.modules.setdefault("landing_export", sys.modules[__name__])


def run(argv):
    return len(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
'''

TOUCHED = LEGACY.replace(
    '"""Legacy service module (fixture)."""',
    '"""Legacy service module (fixture, documented)."""',
)

ROGUE = TOUCHED + '''

import os


def rogue():
    os._exit(0)
'''


def _git(
    repo: Path, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        input=input_text,
        check=True,
    )


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _edit_patch(repo: Path, path: str, content: str) -> str:
    """Real ``git diff`` for rewriting a tracked file; the tree stays at base."""

    target = repo / path
    target.write_text(content, encoding="utf-8")
    try:
        return _git(repo, "diff", "--no-ext-diff", "--", path).stdout
    finally:
        _git(repo, "checkout", "--", path)


def test_the_scan_records_the_base_findings_it_subtracted(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    patch = _edit_patch(repo, "pkg/legacy.py", TOUCHED)

    scan = subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base)

    assert list(scan) == [], "a documented pre-existing shape is not a new finding"
    assert sorted(finding["kind"] for finding in scan.subtracted) == [
        "forced_exit",
        "patched_sys_modules",
    ]
    assert {finding["path"] for finding in scan.subtracted} == {"pkg/legacy.py"}


def test_a_refusal_carries_both_the_findings_and_the_subtracted_ones(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    patch = _edit_patch(repo, "pkg/legacy.py", ROGUE)

    try:
        verify_local.check_patch_subversion(patch, repo_root=repo, source_rev=base)
    except verify_local.VerifyError as exc:
        assert exc.code == "test_subversion"
        assert [finding["kind"] for finding in exc.findings] == ["forced_exit"]
        assert sorted(finding["kind"] for finding in exc.subtracted) == [
            "forced_exit",
            "patched_sys_modules",
        ]
    else:  # pragma: no cover - the added exit must refuse
        raise AssertionError("a new os._exit(0) must still be refused")


def test_the_cli_evidence_records_the_scan_without_overclaiming(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    clean = subversion_scan.scan_patch(
        _edit_patch(repo, "pkg/legacy.py", TOUCHED), repo_root=repo, source_rev=base
    )
    refused = subversion_scan.scan_patch(
        _edit_patch(repo, "pkg/legacy.py", ROGUE), repo_root=repo, source_rev=base
    )

    clean_evidence = cli._subversion_evidence(clean)
    assert clean_evidence["scanned"] is True
    assert clean_evidence["findings"] == []
    assert [row["kind"] for row in clean_evidence["subtracted"]] == [
        "patched_sys_modules",
        "forced_exit",
    ]
    assert all(row["path"] == "pkg/legacy.py" and row["line"] > 0 for row in clean_evidence["subtracted"])
    evidence = cli._subversion_evidence(refused)
    assert [finding["kind"] for finding in evidence["findings"]] == ["forced_exit"]
    assert len(evidence["subtracted"]) == 2
