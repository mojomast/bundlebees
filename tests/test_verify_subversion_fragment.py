"""Fragment safety of the patch-only subversion scan.

A patch-only scan has no repository: it rebuilds each hunk as a standalone
"file", so the post-image it parses is a fragment of the real file. Such a
fragment can open inside a bracketed expression, mid-block or mid-function, and
then it cannot parse on its own. That is not evidence of test subversion, so it
must not refuse the patch - but the rules that *can* be evaluated on the lines
the hunk carries must still fire.

Covered here:

* a comment-looking line added inside a multi-line module docstring, which
  splits the string in the fragment (the ``launcher.py:3`` refusal);
* a repairable mid-function fragment: the real rule on its added line is still
  found, at its own line number;
* a module-level fragment keeps module-level depth, so ``sys.exit`` there is
  still a ``forced_exit`` (the repair must not hide it behind a synthetic
  function);
* an unrepairable fragment still evaluates the per-added-line rules
  (``os._exit``) instead of becoming a no-op;
* a *created* file's post-image is complete, so a file that really does not
  parse is still refused.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from bundlebees_plugin import subversion_scan, verify_local

#: A module whose docstring is longer than a three-line diff context: a line
#: added at line 3 splits the string, so the hunk's fragment is unterminated.
RUNTIME = '''#!/usr/bin/env python3
"""Worker runtime (fixture).

Owns every call, holds the provider key and never returns it, and exposes a
narrow token-scoped HTTP API. This paragraph is deliberately longer than the
three lines of trailing context a diff carries.
"""

import sys


def run(argv):
    return len(argv)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
'''

#: A function whose body opens a bracketed expression before the hunk starts,
#: so the fragment of any later hunk carries an unmatched ``]``.
LEDGER = '''"""Ledger helpers (fixture)."""


def run(values):
    parts = [
        value.strip()
        for value in values
    ]
    return parts
'''

#: One function, no brackets: a hunk inside it is repairable with a synthetic
#: enclosing scope but not by a plain dedent (the fragment would carry a
#: module-level ``return``).
REPORT = '''"""Report helpers (fixture)."""

import sys


def compute(values, flag):
    total = 0
    for value in values:
        total += value
    if flag:
        guard(total)
        return total
    return total * 2
'''


def _git(
    repo: Path, *args: str, input_text: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        input=input_text,
        check=check,
    )


def _fixture_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _patch_after(repo: Path, path: str, content: str) -> str:
    """Return the real ``git diff`` for *content*, leaving the tree at base."""

    target = repo / path
    target.write_text(content, encoding="utf-8")
    try:
        return _git(repo, "diff", "--no-ext-diff", "--", path).stdout
    finally:
        _git(repo, "checkout", "--", path)


def _patch_new_file(repo: Path, path: str, content: str) -> str:
    """Return the real ``git diff`` for a created file, leaving it untracked."""

    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-N", "--", path)
    try:
        return _git(repo, "diff", "--no-ext-diff", "--", path).stdout
    finally:
        _git(repo, "rm", "-q", "--cached", "--", path)
        target.unlink()


def _kinds(findings) -> set[str]:
    return {finding["kind"] for finding in findings}


def _assert_applies(repo: Path, patch: str, post_image: str | None = None) -> None:
    applied = _git(repo, "apply", "--check", "--whitespace=nowarn", "-", input_text=patch, check=False)
    assert applied.returncode == 0, applied.stderr
    if post_image is not None:
        ast.parse(post_image)


def _line_of(text: str, needle: str) -> int:
    return text.splitlines().index(needle) + 1


def test_patch_only_scan_accepts_a_comment_inside_a_top_of_file_docstring(tmp_path: Path):
    """The ``launcher.py:3`` refusal: a fragment that splits a docstring."""

    repo, _base = _fixture_repo(tmp_path, {"pkg/runtime.py": RUNTIME})
    post_image = RUNTIME.replace(
        '"""Worker runtime (fixture).\n',
        '"""Worker runtime (fixture).\n# a line added at line 3\n',
        1,
    )
    patch = _patch_after(repo, "pkg/runtime.py", post_image)
    _assert_applies(repo, patch, post_image)
    hunks = [line for line in patch.splitlines() if line.startswith("@@")]
    assert hunks == ["@@ -1,5 +1,6 @@"], hunks

    # The fragment is the file's first lines plus the added one, and the
    # docstring it opens is not closed inside it.
    assert list(verify_local.check_patch_subversion(patch)) == []
    assert subversion_scan.scan_patch(patch) == []


def test_a_repairable_mid_function_fragment_still_finds_real_rules():
    """A dedent cannot parse this fragment; the scanner must still see it."""

    fragment = (
        "    total = 0\n"
        "    for value in values:\n"
        "        total += value\n"
        "    if flag:\n"
        '        sys.modules["probe"] = guard\n'
        "        return total\n"
        "    return total * 2\n"
    )
    # Premise: it is a fragment, not a file - a plain dedent leaves the
    # statements at module level, where ``return`` is a syntax error.
    try:
        ast.parse(fragment)
    except SyntaxError:
        pass
    else:  # pragma: no cover - the fixture must stay a fragment
        raise AssertionError("the fixture parses as a file; it is not a fragment")

    partial = subversion_scan._python_findings("pkg/report.py", fragment.encode(), recover=True)
    assert _kinds(partial) == {"patched_sys_modules"}, partial
    assert [finding["line"] for finding in partial] == [5], partial

    # Whole-file semantics keep the parse_error and still scan the fragment.
    whole = subversion_scan._python_findings("pkg/report.py", fragment.encode())
    assert _kinds(whole) == {"parse_error", "patched_sys_modules"}, whole


def test_patch_only_scan_finds_rules_in_a_mid_function_hunk(tmp_path: Path):
    """End to end: the rule fires, the fragment does not refuse the patch."""

    repo, _base = _fixture_repo(tmp_path, {"pkg/report.py": REPORT})
    addition = '        sys.modules["probe"] = guard\n'
    post_image = REPORT.replace("        guard(total)\n", "        guard(total)\n" + addition, 1)
    patch = _patch_after(repo, "pkg/report.py", post_image)
    _assert_applies(repo, patch, post_image)

    findings = subversion_scan.scan_patch(patch)
    assert _kinds(findings) == {"patched_sys_modules"}, findings
    assert [finding["line"] for finding in findings] == [
        _line_of(post_image, '        sys.modules["probe"] = guard')
    ], findings


def test_a_module_level_fragment_keeps_its_module_depth():
    """``sys.exit`` in a module-level hunk is still a ``forced_exit``."""

    fragment = "VALUE = 1\nsys.exit(1)\n"
    tree = subversion_scan._repair_python(fragment)
    assert tree is not None
    # Parsed as-is (no synthetic scope), so the statements keep their own
    # numbering and module-level depth.
    assert [node.lineno for node in tree.body] == [1, 2]

    findings = subversion_scan._python_findings("pkg/tool.py", fragment.encode(), recover=True)
    assert _kinds(findings) == {"forced_exit"}, findings
    assert [finding["line"] for finding in findings] == [2], findings


def test_an_unrepairable_fragment_still_evaluates_the_added_lines(tmp_path: Path):
    """A fragment no scope can repair must not become a no-op."""

    repo, _base = _fixture_repo(tmp_path, {"pkg/ledger.py": LEDGER})
    benign = LEDGER.replace("    return parts\n", "    return parts\n    # tidy\n", 1)
    benign_patch = _patch_after(repo, "pkg/ledger.py", benign)
    _assert_applies(repo, benign_patch, benign)
    assert subversion_scan.scan_patch(benign_patch) == []

    exiting = LEDGER.replace("    return parts\n", "    return parts\n    os._exit(1)\n", 1)
    exiting_patch = _patch_after(repo, "pkg/ledger.py", exiting)
    _assert_applies(repo, exiting_patch, exiting)

    findings = subversion_scan.scan_patch(exiting_patch)
    assert _kinds(findings) == {"forced_exit"}, findings
    assert [finding["line"] for finding in findings] == [_line_of(exiting, "    os._exit(1)")], findings


def test_a_created_file_that_does_not_parse_is_still_refused(tmp_path: Path):
    """A created file's post-image is complete: ``parse_error`` stays real."""

    repo, _base = _fixture_repo(tmp_path, {"pkg/report.py": REPORT})
    broken = '"""Broken fixture."""\n\n\ndef broken(:\n    pass\n'
    patch = _patch_new_file(repo, "pkg/broken.py", broken)
    _assert_applies(repo, patch)
    assert "new file mode" in patch

    kinds = _kinds(subversion_scan.scan_patch(patch))
    assert kinds == {"parse_error"}, kinds
