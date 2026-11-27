"""Base-revision subtraction in the repo-backed subversion scan.

The repo-backed scan (``scan_patch(patch, repo_root, source_rev)``) must not
refuse a patch for a rule shape that the base revision already carries, and it
must keep refusing every shape the patch itself introduces. The positive
fixtures live in ``tests/test_verify_subversion_false_positives.py``; this file
owns the "still refused" corpus and the new-file / rename / delete edges, so a
future change to the subtraction cannot quietly turn a real rule off.

All fixtures are real git repositories: no network, launcher, sandbox or
worker code is involved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import subversion_scan, verify_local

#: Both shapes below are pre-existing and legitimate for this module: the
#: landing-export bootstrap registers itself in ``sys.modules`` at import time
#: and the CLI entry point exits with the run status.
LEGACY = '''"""Legacy service module (fixture)."""

import sys

sys.modules.setdefault("landing_export", sys.modules[__name__])


def run(argv):
    return len(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
'''

CLEAN = '''"""Clean module (fixture)."""


def value():
    return 1
'''

#: A pre-existing module-level shape whose payload spans several lines: the
#: finding names the first line, so a shape keyed on that line alone would
#: subtract a rewritten continuation.
MULTILINE = '''"""Multi-line legacy module (fixture)."""

import sys

sys.modules["probe"] = (
    object()
)


def run(argv):
    return len(argv)
'''

#: A pre-existing module-level exit behind a real guard. Flipping the guard to
#: ``if True:`` makes it run at import time while the exit line itself never
#: moves, so the guard has to be part of the finding's shape.
GUARDED = '''"""Guarded module (fixture)."""

import sys


def main():
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

#: A pre-existing exit that the patch only moves into another function. The
#: moved line is added by the diff and must never be subtracted.
MOVED_EXIT = '''"""Move fixture: a pre-existing exit inside main()."""

import os


def helper():
    return 0


def main():
    os._exit(0)
'''

#: A dormant module stub moved to import time by a patch that only moves it.
MOVED_STUB = '''"""Move fixture: a dormant module stub."""

import sys


def cli():
    sys.modules["probe"] = None
    return 0
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


def _staged_patch(repo: Path) -> str:
    """Real ``git diff --cached -M`` for a staged add/rename/delete."""
    try:
        return _git(repo, "diff", "--cached", "--no-ext-diff", "-M").stdout
    finally:
        _git(repo, "reset", "-q", "--hard", "HEAD")


def _findings(repo: Path, base: str, patch: str) -> list[subversion_scan.Finding]:
    return list(subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base))


def _kinds(repo: Path, base: str, patch: str) -> set[str]:
    return {finding["kind"] for finding in _findings(repo, base, patch)}


# --- the shapes that must still be refused --------------------------------


def test_a_new_module_level_sys_modules_write_is_refused(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + '\n\nsys.modules["helpers"] = sys.modules[__name__]\n'

    findings = _findings(repo, base, _edit_patch(repo, "pkg/clean.py", post))

    assert [finding["kind"] for finding in findings] == ["patched_sys_modules"]
    assert findings[0]["line"] == len(post.splitlines())


def test_patching_assertion_error_is_refused(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + "\n\nAssertionError = RuntimeError\n"

    assert "patched_assertion" in _kinds(repo, base, _edit_patch(repo, "pkg/clean.py", post))


def test_calling_os_exit_is_refused(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN.replace("    return 1\n", "    os._exit(1)\n    return 1\n")

    assert "forced_exit" in _kinds(repo, base, _edit_patch(repo, "pkg/clean.py", post))


def test_registering_atexit_is_refused(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + '\n\natexit.register(lambda: None)\n'

    assert "atexit" in _kinds(repo, base, _edit_patch(repo, "pkg/clean.py", post))


def test_a_second_copy_of_a_pre_existing_shape_is_refused(tmp_path: Path):
    """Counted subtraction: one base copy cannot pay for two post-image copies."""

    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    twin = 'sys.modules.setdefault("landing_export", sys.modules[__name__])\n'
    post = LEGACY.replace("def run(argv):\n", twin + "\n\ndef run(argv):\n")

    findings = _findings(repo, base, _edit_patch(repo, "pkg/legacy.py", post))

    assert [finding["kind"] for finding in findings] == ["patched_sys_modules"]


def test_rewriting_a_pre_existing_multiline_shape_is_refused(tmp_path: Path):
    """The shape is the whole statement, not just the line the finding names.

    A patch can leave a pre-existing ``sys.modules`` statement's first line
    untouched and rewrite its payload on a continuation line. Keyed on the
    first line alone that shape was subtracted and the repo-backed scan
    accepted a changed tampering payload (review-round-1 finding 2).
    """

    repo, base = _repo(tmp_path, {"pkg/legacy.py": MULTILINE})
    post = MULTILINE.replace("    object()\n", "    __import__('evil')\n", 1)

    findings = _findings(repo, base, _edit_patch(repo, "pkg/legacy.py", post))

    assert [finding["kind"] for finding in findings] == ["patched_sys_modules"]
    assert findings[0]["line"] == 5
    with pytest.raises(verify_local.VerifyError) as refused:
        verify_local.check_patch_subversion(
            _edit_patch(repo, "pkg/legacy.py", post), repo_root=repo, source_rev=base
        )
    assert refused.value.code == "test_subversion"

    # And the patch-only scan names the same rule: the change is real in both
    # paths, only the refusal mechanism differs.
    patch_only = subversion_scan.scan_patch(_edit_patch(repo, "pkg/legacy.py", post))
    assert {finding["kind"] for finding in patch_only} == {"patched_sys_modules"}


def test_a_comment_inside_a_pre_existing_multiline_shape_is_still_subtracted(tmp_path: Path):
    """Comment-only lines inside the statement are not part of its shape.

    The whole-segment identity must not turn a comment added inside a
    pre-existing multi-line statement into a refusal.
    """

    repo, base = _repo(tmp_path, {"pkg/legacy.py": MULTILINE})
    post = MULTILINE.replace("    object()\n", "    # note\n    object()\n", 1)

    assert _findings(repo, base, _edit_patch(repo, "pkg/legacy.py", post)) == []


def test_flipping_the_guard_around_a_pre_existing_exit_is_refused(tmp_path: Path):
    """The guard is part of the shape: ``if True:`` makes the exit run at import.

    The exit line itself never moves, so a statement-only shape subtracted it
    and the repo-backed scan accepted a patch that turns a guarded exit into an
    import-time exit (review round 2, guard flip).
    """

    repo, base = _repo(tmp_path, {"pkg/app.py": GUARDED})
    post = GUARDED.replace('if __name__ == "__main__":', "if True:", 1)

    findings = _findings(repo, base, _edit_patch(repo, "pkg/app.py", post))

    assert [finding["kind"] for finding in findings] == ["forced_exit"]
    assert findings[0]["line"] == 11
    with pytest.raises(verify_local.VerifyError) as refused:
        verify_local.check_patch_subversion(
            _edit_patch(repo, "pkg/app.py", post), repo_root=repo, source_rev=base
        )
    assert refused.value.code == "test_subversion"

    # The patch-only path already names the real rule; keep it pinned.
    patch_only = subversion_scan.scan_patch(_edit_patch(repo, "pkg/app.py", post))
    assert {finding["kind"] for finding in patch_only} == {"forced_exit"}


def test_moving_a_pre_existing_exit_into_a_helper_is_refused(tmp_path: Path):
    """A moved shape sits on an added line and is never subtracted."""

    repo, base = _repo(tmp_path, {"pkg/app.py": MOVED_EXIT})
    post = MOVED_EXIT.replace("def helper():\n    return 0\n",
                              "def helper():\n    os._exit(0)\n", 1)
    post = post.replace("def main():\n    os._exit(0)\n",
                        "def main():\n    return 0\n", 1)

    findings = _findings(repo, base, _edit_patch(repo, "pkg/app.py", post))

    assert [finding["kind"] for finding in findings] == ["forced_exit"]
    assert findings[0]["line"] == 7


def test_moving_a_pre_existing_module_stub_to_import_time_is_refused(tmp_path: Path):
    """Turning a dormant stub into an import-time one is the rule's purpose."""

    repo, base = _repo(tmp_path, {"pkg/app.py": MOVED_STUB})
    post = MOVED_STUB.replace(
        "def cli():\n    sys.modules[\"probe\"] = None\n    return 0\n",
        "def _boot():\n    sys.modules[\"probe\"] = None\n    return 0\n\n\n_boot()\n",
        1,
    )

    findings = _findings(repo, base, _edit_patch(repo, "pkg/app.py", post))

    assert [finding["kind"] for finding in findings] == ["patched_sys_modules"]
    assert findings[0]["line"] == 7


def test_dedenting_a_pre_existing_exit_out_of_its_guard_is_refused(tmp_path: Path):
    """A dedent is a code change, not a line shift; indentation is the shape."""

    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    post = LEGACY.replace(
        'if __name__ == "__main__":\n    sys.exit(run(sys.argv[1:]))\n',
        "sys.exit(run(sys.argv[1:]))\n",
    )

    assert "forced_exit" in _kinds(repo, base, _edit_patch(repo, "pkg/legacy.py", post))


def test_a_new_file_with_a_module_level_exit_is_refused(tmp_path: Path) -> None:
    """No pre-image, nothing to subtract: the whole file is new."""

    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    (repo / "pkg/added.py").write_text(
        'import sys\n\nif __name__ == "__main__":\n    sys.exit(2)\n', encoding="utf-8"
    )
    _git(repo, "add", "-N", "--", "pkg/added.py")
    patch = _git(repo, "diff", "--no-ext-diff", "--", "pkg/added.py").stdout

    assert "forced_exit" in _kinds(repo, base, patch)


# --- the edges the subtraction has to keep sane ---------------------------


def test_a_pure_line_shift_does_not_resurrect_base_findings(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    post = "# leading comment\n" * 5 + LEGACY + "\n# trailing comment\n"

    assert _findings(repo, base, _edit_patch(repo, "pkg/legacy.py", post)) == []


def test_a_pure_rename_keeps_base_findings_subtracted(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    _git(repo, "mv", "pkg/legacy.py", "pkg/legacy_moved.py")

    assert _findings(repo, base, _staged_patch(repo)) == []


def test_a_rename_with_a_new_shape_is_refused(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    _git(repo, "mv", "pkg/legacy.py", "pkg/legacy_moved.py")
    moved = repo / "pkg/legacy_moved.py"
    moved.write_text(LEGACY.replace("    return len(argv)\n", "    os._exit(3)\n"), encoding="utf-8")
    _git(repo, "add", "-A")

    assert "forced_exit" in _kinds(repo, base, _staged_patch(repo))


def test_deleting_a_file_with_base_shapes_is_not_a_finding(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    (repo / "pkg/legacy.py").unlink()
    patch = _git(repo, "diff", "--no-ext-diff", "--", "pkg/legacy.py").stdout
    _git(repo, "checkout", "--", "pkg/legacy.py")

    assert _findings(repo, base, patch) == []


def test_ini_config_findings_stay_fail_closed(tmp_path: Path) -> None:
    """Deliberate limit: an INI config rule reports the whole document at line 1.

    A line shape cannot tell a pre-existing ``addopts`` plugin option from a
    newly added one, so those findings are never subtracted. This case pins
    that the fix did not silently open that hole. ``pyproject.toml`` is not
    affected: its findings are already attributed to added lines.
    """

    document = "[pytest]\naddopts = -q -p no:cacheprovider\nmarkers =\n    slow: slow tests\n"
    repo, base = _repo(tmp_path, {"pytest.ini": document})
    post = document.replace("    slow: slow tests\n", "    slow: slow tests\n    smoke: smoke tests\n")

    assert "pytest_plugin" in _kinds(repo, base, _edit_patch(repo, "pytest.ini", post))


# --- the wired gate -------------------------------------------------------


def test_the_verify_gate_accepts_a_comment_over_base_shapes(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, {"pkg/legacy.py": LEGACY})
    post = LEGACY.replace("    return len(argv)\n", "    # note\n    return len(argv)\n")
    patch = _edit_patch(repo, "pkg/legacy.py", post)

    assert verify_local.check_patch_subversion(patch, repo_root=repo, source_rev=base) == []
