"""Regression fixtures for two false refusals in the verify subversion scan.

Both fixtures are RED on the pre-fix scanner (bundlebees #304, ``6cfc272``);
each one targets exactly one cause and must go green only when that cause is
fixed.

Cause A -- the patch-only scan. ``verify_local.check_patch_subversion(patch)``
rebuilds every hunk as a standalone file and ``ast.parse``-s the fragment.
``_repair_python`` tries a dedent and a synthetic enclosing ``def``; a hunk
that starts mid-function and whose three-line context crosses into the next
top-level ``def`` defeats both, so the fragment of *valid* Python is reported
as ``parse_error`` at the hunk's first line and the patch is refused.

Cause B -- the repo-backed scan. ``verify_local.verify_and_commit`` scans the
whole post-image of every changed file and never subtracts findings that are
already present at the base revision. A repository file that legitimately
carries a module-level ``sys.modules`` registration and a ``sys.exit`` in
``__main__`` is therefore refused for those pre-existing shapes as soon as any
patch touches the file, even a one-line comment.

The fixtures are real git repositories; nothing runs worker code, needs the
network, the launcher, or a sandbox.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from bundlebees_plugin import leases, subversion_scan, verify_local

#: ``pkg/render.py`` at the base revision. The one blank line between ``wrap``
#: and ``render`` is deliberate: it pulls the next top-level ``def`` into the
#: trailing context of a hunk that ends ``wrap``, which is what makes the
#: shortened fragment unrepairable.
RENDER = '''"""Render helpers (fixture)."""

import textwrap


def wrap(values):
    parts = [
        value.strip()
        for value in values
    ]
    return "-".join(parts)


def render(name, values):
    body = wrap(values)
    label = textwrap.shorten(name, width=12)
    prefix = label.upper()
    suffix = body.lower()
    return f"{prefix}:{suffix}"
'''

#: ``pkg/legacy.py`` at the base revision. Both shapes are pre-existing and
#: legitimate for this module: the landing-export bootstrap registers itself in
#: ``sys.modules`` at import time, and the CLI entry point exits with the run
#: status. They are here to prove that patching this file is not a refusal.
LEGACY = '''"""Legacy service module (fixture).

The landing-export bootstrap registers the module in sys.modules at import
time; the CLI entry point exits with the run status.
"""

import sys

sys.modules.setdefault("landing_export", sys.modules[__name__])

DEFAULT_LIMIT = 3


def build(name, limit=DEFAULT_LIMIT):
    parts = [
        name,
        str(limit),
    ]
    return "-".join(parts)


def compute(values):
    total = 0
    for value in values:
        total += value
        total *= 2
    return total


def run(argv):
    if not argv:
        return 0
    return len(build(argv[0]))


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
'''

BASE_TEST = """from pkg.legacy import compute


def test_compute():
    assert compute([1, 2]) == 3
"""


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


def _hunk_headers(patch: str) -> list[str]:
    return [line for line in patch.splitlines() if line.startswith("@@")]


def _hunk_first_lines(patch: str) -> list[str]:
    """The first body line of every hunk (the line the scan blames)."""

    lines = patch.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("@@")]
    return [lines[index + 1] for index in starts]


def _assert_patch_is_legitimate(repo: Path, patch: str, post_image: str) -> None:
    """A refusal may only target a patch that applies and yields valid Python."""

    applied = _git(repo, "apply", "--check", "--whitespace=nowarn", "-", input_text=patch, check=False)
    assert applied.returncode == 0, applied.stderr
    ast.parse(post_image)


def test_patch_only_scan_accepts_a_multi_hunk_mid_function_patch(tmp_path: Path):
    """Cause A: two hunks that start mid-function must not report parse_error."""

    repo, base = _fixture_repo(tmp_path, {"pkg/render.py": RENDER})
    post_image = RENDER.replace(
        '    return "-".join(parts)\n', '    return "-".join(parts).strip()\n', 1
    ).replace(
        '    return f"{prefix}:{suffix}"\n', '    return f"{prefix}:{suffix}!"\n', 1
    )
    patch = _patch_after(repo, "pkg/render.py", post_image)
    _assert_patch_is_legitimate(repo, patch, post_image)

    # The fixture is what the case claims: several hunks, each opening on an
    # indented statement inside a function body.
    assert len(_hunk_headers(patch)) == 2, _hunk_headers(patch)
    opening = _hunk_first_lines(patch)
    assert all(line.startswith("     ") for line in opening), opening

    try:
        findings = list(verify_local.check_patch_subversion(patch))
    except verify_local.VerifyError as exc:
        raise AssertionError(
            "the patch-only scan refused a patch that applies cleanly and parses: "
            f"{exc.code}: {exc}"
        ) from exc
    assert findings == [], findings
    assert subversion_scan.scan_patch(patch) == [], "the patch-only scan reported findings"


def test_repo_backed_verify_accepts_a_one_line_comment_over_base_shapes(tmp_path: Path):
    """Cause B: pre-existing base shapes must not refuse a comment-only patch."""

    repo, base = _fixture_repo(
        tmp_path, {"pkg/legacy.py": LEGACY, "tests/test_base.py": BASE_TEST}
    )
    post_image = LEGACY.replace(
        "        total *= 2\n", "        total *= 2\n        # one-line comment change\n", 1
    )
    patch = _patch_after(repo, "pkg/legacy.py", post_image)
    _assert_patch_is_legitimate(repo, patch, post_image)
    assert len(_hunk_headers(patch)) == 1, _hunk_headers(patch)

    # The premise of the case: the base file itself carries both shapes, so a
    # whole-file scan that does not subtract base findings refuses any patch.
    base_kinds = {
        finding["kind"]
        for finding in subversion_scan._content_findings("pkg/legacy.py", LEGACY.encode())
    }
    assert base_kinds == {"patched_sys_modules", "forced_exit"}, base_kinds

    # Isolation: this patch carries nothing the patch-only scan can refuse, so
    # a refusal here can only come from the whole-file repo-backed scan.
    assert list(verify_local.check_patch_subversion(patch)) == []

    state = tmp_path / "state"
    leases.record_lease(
        state,
        "att-subversion-red",
        {
            "objective": "add one comment line over pre-existing base shapes",
            "source_rev": base,
            "assigned_files": ["pkg/legacy.py"],
            "acceptance": {
                "criteria": ["the comment change applies"],
                "tests": ["tests/test_base.py"],
            },
        },
    )

    def runner(cwd: Path, argv: Sequence[str]) -> Mapping[str, Any]:
        """Test-runner double: RED before the comment, GREEN after it.

        ``verify_and_commit`` requires the named tests to fail at the base
        revision and pass once the patch is applied. A comment-only change
        cannot do that for real, so this double keys on the patch's own marker
        line; the case under test is the scanner's refusal, not this gate.
        """

        del argv
        applied = "# one-line comment change" in (Path(cwd) / "pkg/legacy.py").read_text(
            encoding="utf-8"
        )
        return {"exit_code": 0 if applied else 1, "timed_out": False, "output_tail": ""}

    try:
        record = verify_local.verify_and_commit(
            state,
            repo,
            "att-subversion-red",
            patch=patch,
            declared_files=["pkg/legacy.py"],
            base_rev=base,
            tests=["tests/test_base.py"],
            suite_command=[sys.executable, "-m", "pytest", "-q", "tests/test_base.py"],
            require_gitleaks=False,
            runner=runner,
        )
    except verify_local.VerifyError as exc:
        raise AssertionError(
            "repo-backed verify refused a one-line comment patch for findings that "
            f"already exist at {base[:12]}: {exc.code}: {exc}"
        ) from exc
    assert record["ok"] is True, record
