"""No-weakening gate for the two B4 scanner fixes.

Both fixes only *remove* false refusals: the patch-only scan stopped calling a
hunk fragment a ``parse_error``, and the repo-backed scan stopped re-reporting
rule shapes the base revision already carried. Neither may turn off a rule, so
this file pins the subversion shapes the owner named — a new module-level
``sys.modules`` write, a patched ``AssertionError`` and an ``os._exit`` — in
*both* scanner paths: the patch-only reconstruction (``scan_patch`` with no
repository, which is what ``check_patch_subversion`` falls back to) and the
repo-backed post-image scan with base subtraction.

The repo-backed half overlaps ``tests/test_subversion_base_subtraction.py`` on
purpose: that file owns the subtraction edges, this one owns the cross-path
matrix the gate was asked to prove.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bundlebees_plugin import subversion_scan, verify_local

#: No pre-existing ``sys.modules`` write and no exit call: every shape below is
#: introduced by the patch, so nothing can legitimately be subtracted.
CLEAN = '''"""Clean module (gate fixture)."""


def value():
    return 1
'''

#: A function whose body opens a bracketed expression: any later hunk is an
#: unrepairable fragment, so the patch-only scan must fall back to its
#: per-added-line rules (the ``forced_exit`` rule has to still fire there).
BRACKETED = '''"""Bracketed helper (gate fixture)."""


def collect(values):
    parts = [
        value.strip()
        for value in values
    ]
    return parts
'''


def _git(repo: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
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
    target = repo / path
    target.write_text(content, encoding="utf-8")
    try:
        return _git(repo, "diff", "--no-ext-diff", "--", path).stdout
    finally:
        _git(repo, "checkout", "--", path)


def _kinds(findings) -> set[str]:
    return {finding["kind"] for finding in findings}


def _line_of(text: str, needle: str) -> int:
    return text.splitlines().index(needle) + 1


def _assert_applies(repo: Path, patch: str) -> None:
    applied = _git(repo, "apply", "--check", "--whitespace=nowarn", "-", input_text=patch)
    assert applied.returncode == 0, applied.stderr


def test_a_new_module_level_sys_modules_write_is_refused_in_both_paths(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + '\n\nsys.modules["helpers"] = sys.modules[__name__]\n'
    patch = _edit_patch(repo, "pkg/clean.py", post)
    _assert_applies(repo, patch)
    line = _line_of(post, 'sys.modules["helpers"] = sys.modules[__name__]')

    patch_only = subversion_scan.scan_patch(patch)
    assert _kinds(patch_only) == {"patched_sys_modules"}, patch_only
    assert [finding["line"] for finding in patch_only] == [line], patch_only

    with_repo = subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base)
    assert [finding["kind"] for finding in with_repo] == ["patched_sys_modules"], with_repo
    assert [finding["line"] for finding in with_repo] == [line], with_repo

    # The wired gate refuses through both of its call sites, not just the scan.
    for kwargs in ({}, {"repo_root": repo, "source_rev": base}):
        try:
            verify_local.check_patch_subversion(patch, **kwargs)
        except verify_local.VerifyError as exc:
            assert exc.code == "test_subversion", exc
            assert "patched_sys_modules" in str(exc), exc
        else:  # pragma: no cover - a clean return is the regression
            raise AssertionError(f"the gate accepted a new sys.modules write ({kwargs})")


def test_a_patched_assertion_error_is_refused_in_both_paths(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + "\n\nAssertionError = RuntimeError\n"
    patch = _edit_patch(repo, "pkg/clean.py", post)
    _assert_applies(repo, patch)

    patch_only = subversion_scan.scan_patch(patch)
    assert _kinds(patch_only) == {"patched_assertion"}, patch_only

    with_repo = subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base)
    assert _kinds(with_repo) == {"patched_assertion"}, with_repo


def test_an_os_exit_call_is_refused_in_both_paths(tmp_path: Path):
    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN.replace("    return 1\n", "    os._exit(1)\n    return 1\n")
    patch = _edit_patch(repo, "pkg/clean.py", post)
    _assert_applies(repo, patch)

    patch_only = subversion_scan.scan_patch(patch)
    assert _kinds(patch_only) == {"forced_exit"}, patch_only

    with_repo = subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base)
    assert _kinds(with_repo) == {"forced_exit"}, with_repo


def test_an_os_exit_in_an_unrepairable_fragment_is_refused_patch_only(tmp_path: Path):
    """The fragment fallback is not a no-op: the added line is still checked."""

    repo, _base = _repo(tmp_path, {"pkg/bracketed.py": BRACKETED})
    post = BRACKETED.replace("    return parts\n", "    return parts\n    os._exit(7)\n", 1)
    patch = _edit_patch(repo, "pkg/bracketed.py", post)
    _assert_applies(repo, patch)

    findings = subversion_scan.scan_patch(patch)
    assert _kinds(findings) == {"forced_exit"}, findings
    assert [finding["line"] for finding in findings] == [_line_of(post, "    os._exit(7)")], findings


def test_an_os_exit_assignment_is_disclosed_as_a_pre_existing_rule_gap(tmp_path: Path):
    """``os._exit = fn`` is not a finding — before or after these fixes.

    The scanner's ``forced_exit`` rules match a *call* (``os._exit(...)``,
    ``sys.exit(...)``); rebinding the attribute is a different shape the rules
    never covered. This case is pinned so the gap is disclosed rather than
    mistaken for a weakening, and so a future widening notices it.
    """

    repo, base = _repo(tmp_path, {"pkg/clean.py": CLEAN})
    post = CLEAN + "\n\nos._exit = _never\n"
    patch = _edit_patch(repo, "pkg/clean.py", post)
    _assert_applies(repo, patch)

    assert subversion_scan.scan_patch(patch) == []
    assert subversion_scan.scan_patch(patch, repo_root=repo, source_rev=base) == []
