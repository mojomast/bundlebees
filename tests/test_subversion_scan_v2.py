"""RED corpus for WA-A1A-V2: the tree-based test-subversion check.

A green run is necessary, not sufficient. Code under test can rewrite the
evidence files, force the exit code, or load a plugin, so verify refuses a
patch that reaches into the test runner itself.

V1 reconstructed the changed paths and the added lines from the diff text.
V2 changes the input, not the promise: ``scan_change(patch, repo_root,
source_rev)`` builds the post-patch tree from a temp index, derives changed
paths with ``git diff --name-status -M``, classifies those paths from
``bundlebees_plugin/policy/risk-floor.json``, parses pytest configuration with
``tomllib``/``configparser`` and AST-checks the head version of every changed
``.py`` file.

Corpus provenance
-----------------
Every node id below is one of the 98 ids collected from the A1a RED branches
(``red/wave-a-a1a-subversion-scan@46e03db``, ``-fup1-cli-subversion@bf8df74``,
``-fup2@93b10a1``, ``-fup3-r2@ce1fcbb``, ``-fup4@3ae0d6f``, ``-fup5@9b22a1e``,
``-fup6@7f0623c``, ``-fup7@ae94361``) plus the file-level guard. 53 cases are
kept with their assertions intact; only the harness is adapted to the tree API.

45 cases are obsolete by design: they pin patch-text regex edges (quoted or
escaped TOML keys, INI comments, comment-token spoofing, CRLF headers, GIT
binary differ blocks, multiline addopts) that cannot exist once the check reads
the applied tree and the parsed configuration. They stay in the corpus, marked
``pytest.mark.skip`` with an ``obsolete-by-design:`` reason, so the count is
never quietly reduced.

Harness adaptations (the assertions are unchanged)
--------------------------------------------------
* each case builds a throwaway git repository holding the pre-image the patch
  expects, so ``git apply`` and ``name-status -M`` see a real tree;
* ``_edit`` adds the section header a parsed config needs (``[pytest]``,
  ``[tool.pytest.ini_options]``, ...) as a pre-existing context line, and makes
  an added ``.py`` snippet parse (a body for a bare ``def``, a function for an
  indented statement) because V2 parses the head file with ``ast``;
* the two rename cases take their diff from a real ``git mv`` so similarity
  pairing is exercised, and the verify/CLI cases pass ``repo_root`` and
  ``source_rev`` instead of relying on a patch-only shim.
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from bundlebees_plugin import cli, subversion_scan, verify_local
from tests.test_cli import (
    ATTEMPT_ID,
    FakeClient,
    _ctx,
    _git as _cli_git,
    _invoke,
    _lease,
    _repo as _cli_repo,
    _result,
    _verify_argv,
)

#: Node ids that must pass under the tree-based check.
KEPT_CASES = 53
#: Node ids that pin a patch-text edge and are skipped, never deleted.
OBSOLETE_CASES = 45


# --- the V2 entry point ---------------------------------------------------
#
# Resolved lazily on purpose. At the RED commit the module exists and
# ``scan_change`` does not, so every kept case fails *inside* the test with an
# AttributeError instead of hiding the corpus behind a collection error.


def _scan_change():
    try:
        return subversion_scan.scan_change
    except AttributeError:
        raise AttributeError(
            "bundlebees_plugin.subversion_scan.scan_change is missing: the V2 "
            "tree-based check is not implemented yet (WA-A1A-V2 RED)"
        ) from None


# --- git harness ----------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=bundlebees",
            "-c",
            "user.email=bees@localhost",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _init_repo(root: Path, files: dict[str, str]) -> str:
    """Create ``root`` as a git repo whose HEAD holds ``files``; return the sha."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base", "--allow-empty")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _make_repo(files: dict[str, str]) -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp(prefix="a1a-v2-base-"))
    return root, _init_repo(root, files)


def _section_path(raw: str) -> str:
    old, _, _new = raw[len("diff --git ") :].partition(" b/")
    return old[2:] if old.startswith("a/") else old


def _preimage(patch: str) -> dict[str, str]:
    """Rebuild the files the patch expects to exist, from its pre-image lines."""
    bodies: dict[str, list[str]] = {}
    absent: set[str] = set()
    path = ""
    in_hunk = False
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = _section_path(raw), False
            bodies.setdefault(path, [])
            continue
        if not path:
            continue
        if raw.startswith("new file mode"):
            absent.add(path)
        elif raw[:4] in {"--- ", "+++ "}:
            in_hunk = False
        elif raw.startswith("@@"):
            in_hunk = True
        elif in_hunk and raw[:1] in {" ", "-"}:
            bodies[path].append(raw[1:])
    return {
        path: "".join(f"{text}\n" for text in body)
        for path, body in bodies.items()
        if path not in absent
    }


@contextlib.contextmanager
def _case(
    patch: str, base: dict[str, str] | None = None
) -> Iterator[tuple[Path, str]]:
    root, sha = _make_repo({**_preimage(patch), **(base or {})})
    try:
        yield root, sha
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _scan(patch: str, base: dict[str, str] | None = None):
    """Run the tree-based check against a real base repository for this case."""
    scan_change = _scan_change()
    with _case(patch, base) as (repo, sha):
        return scan_change(patch, repo, sha)


def _kinds(patch: str) -> set[str]:
    return {finding["kind"] for finding in _scan(patch)}


def _check(patch: str, base: dict[str, str] | None = None):
    """Drive the wired verify gate with the tree inputs (V2 signature)."""
    _scan_change()
    with _case(patch, base) as (repo, sha):
        return verify_local.check_patch_subversion(
            patch, repo_root=repo, source_rev=sha
        )


# --- patch builders -------------------------------------------------------


def _new_file(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _new_empty_file(path: str) -> str:
    """What git emits for an empty new file: no ---/+++ header and no hunk."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
    )


def _config_context(path: str, added: list[str]) -> list[str]:
    """Pre-existing lines that make the post-patch document a valid config."""
    first = added[0].lstrip() if added else ""
    if path == "pyproject.toml":
        if first.startswith("["):
            return ["[project]", 'name = "pkg"']
        return ["[tool.pytest.ini_options]"]
    if path == "setup.cfg":
        if any(line.lstrip().startswith("pytest11") for line in added):
            return ["[options.entry_points]"]
        return ["[tool:pytest]"]
    if path in {"pytest.ini", "tox.ini"}:
        return ["[pytest]"]
    return []


def _python_context(added: list[str]) -> list[str]:
    """Pre-existing lines that make the added snippet a parseable module."""
    try:
        ast.parse("\n".join(added))
        return []
    except SyntaxError:
        pass
    if added and added[-1].rstrip().endswith(":"):
        added.append("    pass")
        try:
            ast.parse("\n".join(added))
            return []
        except SyntaxError:
            added.pop()
    if added and added[0][:1].isspace():
        context = ["def _case_body():"]
        try:
            ast.parse("\n".join(context + added))
            return context
        except SyntaxError:
            pass
    raise AssertionError(f"harness cannot make this snippet parse: {added!r}")


def _edit(path: str, added: list[str], removed: list[str] | None = None) -> str:
    """A one-hunk edit whose pre-image the tree harness can rebuild."""
    removed = list(removed or [])
    added = list(added)
    if path.endswith(".py"):
        context = _python_context(added)
    else:
        context = _config_context(path, added)
    body = (
        "".join(f" {line}\n" for line in context)
        + "".join(f"-{line}\n" for line in removed)
        + "".join(f"+{line}\n" for line in added)
    )
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(context) + len(removed)} +1,{len(context) + len(added)} @@\n{body}"
    )


def _delete(path: str, *lines: str) -> str:
    """What git emits for deleting a non-empty file."""
    body = "".join(f"-{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        f"@@ -1,{len(lines)} +0,0 @@\n{body}"
    )


def _empty_delete(path: str) -> str:
    """What git emits for deleting an empty file: no ---/+++ header and no hunk."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index e69de29..0000000\n"
    )


def _mode_change(path: str) -> str:
    """What git emits for a mode-only change: no ---/+++ header and no hunk."""
    return f"diff --git a/{path} b/{path}\nold mode 100644\nnew mode 100755\n"


def _binary_modification(path: str, payload: str = "Scc@XpScc@XpScc@Xp") -> str:
    """What git emits when a path is marked binary: no header pair, no hunk."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 587be6b..b2e9a4e 100644\n"
        "GIT binary patch\n"
        f"literal {len(payload)}\n"
        f"{payload}\n"
        "\n"
    )


def _rename(old: str, new: str) -> tuple[str, dict[str, str]]:
    """A real pure-rename diff plus the base tree it was taken from."""
    content = "keep\n"
    root, _sha = _make_repo({old: content})
    try:
        _git(root, "mv", old, new)
        patch = _git(root, "diff", "--cached", "-M", "HEAD").stdout
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return patch, {old: content}


# --- path classes ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["tests/conftest.py", "conftest.py", "src/pkg/conftest.py"],
    ids=["tests/conftest.py", "conftest.py", "src/pkg/conftest.py"],
)
def test_added_conftest_is_a_finding(path):
    assert "conftest" in _kinds(_new_file(path, "import pytest"))


@pytest.mark.parametrize(
    "path",
    ["sitecustomize.py", "src/usercustomize.py"],
    ids=["sitecustomize.py", "src/usercustomize.py"],
)
def test_interpreter_startup_hooks_are_findings(path):
    assert "startup_hook" in _kinds(_new_file(path, "x = 1"))


def test_pth_file_is_a_finding():
    assert "pth_file" in _kinds(_new_file("src/evil.pth", "import os"))


# --- parsed pytest configuration -----------------------------------------


@pytest.mark.parametrize(
    "path,line",
    [
        ("pyproject.toml", 'addopts = "-p myplugin"'),
        ("pytest.ini", "addopts = -p no:cacheprovider -p myplugin"),
        ("setup.cfg", "addopts = -p myplugin"),
        ("tox.ini", "addopts = -p myplugin"),
        ("src/pkg/__init__.py", 'pytest_plugins = ["pkg.hooks"]'),
    ],
    ids=[
        'pyproject.toml-addopts = "-p myplugin"',
        "pytest.ini-addopts = -p no:cacheprovider -p myplugin",
        "setup.cfg-addopts = -p myplugin",
        "tox.ini-addopts = -p myplugin",
        'src/pkg/__init__.py-pytest_plugins = ["pkg.hooks"]',
    ],
)
def test_pytest_plugin_loading_is_a_finding(path, line):
    assert "pytest_plugin" in _kinds(_edit(path, [line]))


# --- AST checks on the head version of changed .py files ------------------


@pytest.mark.parametrize(
    "line,kind",
    [
        ("    os._exit(0)", "forced_exit"),
        ("atexit.register(_rewrite)", "atexit"),
        ("def pytest_runtest_makereport(item, call):", "pytest_hook"),
        ("def pytest_collection_modifyitems(items):", "pytest_hook"),
        ("builtins.AssertionError = type('A', (Exception,), {})", "patched_assertion"),
        ("unittest.TestCase.assertEqual = lambda *a, **k: None", "patched_assertion"),
    ],
    ids=[
        "    os._exit(0)-forced_exit",
        "atexit.register(_rewrite)-atexit",
        "def pytest_runtest_makereport(item, call):-pytest_hook",
        "def pytest_collection_modifyitems(items):-pytest_hook",
        "builtins.AssertionError = type('A', (Exception,), {})-patched_assertion",
        "unittest.TestCase.assertEqual = lambda *a, **k: None-patched_assertion",
    ],
)
def test_runner_subversion_in_added_lines_is_a_finding(line, kind):
    assert kind in _kinds(_edit("src/pkg/core.py", [line]))


def test_benign_patch_has_no_findings():
    patch = _edit("src/pkg/core.py", ["def value():", "    return 2"], ["def value():", "    return 1"])
    assert _scan(patch) == []


def test_removed_lines_are_not_findings():
    patch = _edit("src/pkg/core.py", ["pass"], ["    os._exit(0)", "atexit.register(_rewrite)"])
    assert _scan(patch) == []


def test_findings_name_the_path_and_line_without_echoing_secrets():
    finding = _scan(_edit("src/pkg/core.py", ["os._exit(0)  # token=" + "abc" + "def123"]))[0]
    assert finding["path"] == "src/pkg/core.py"
    assert isinstance(finding["line"], int) and finding["line"] >= 1
    assert "abcdef123" not in repr(finding)


# --- pyproject scope: pytest/coverage only, build tables are clean -------
#
# A pyproject.toml edit is not test-runner subversion by itself.  A build or
# packaging table and ordinary [project] metadata must stay clean; a pytest or
# coverage table, a pytest11 entry point and a pytest plugin dependency must
# not.  Every line is attributed to the real TOML table that contains it, so a
# header-looking line in a multiline string cannot move a build line into a
# pytest table.


_PYPROJECT_BASE = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pkg"
version = "0.1.0"
description = "A package"
dependencies = [
  "requests>=2",
]

[tool.hatch.build.targets.sdist]
include = [
  "src/**",
  "README.md",
]

[tool.hatch.build.targets.wheel]
packages = ["pkg"]
"""


def _text_patch(path: str, before: str, after: str) -> str:
    """A git-shaped unified diff between two whole-file texts."""
    lines = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return f"diff --git a/{path} b/{path}\n" + "\n".join(lines) + "\n"


def _pyproject_patch(before: str, after: str) -> list[subversion_scan.Finding]:
    assert before != after
    return _scan(
        _text_patch("pyproject.toml", before, after),
        base={"pyproject.toml": before},
    )


_C2_PKG_PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "hyvv"
version = "0.1.18"

[tool.hatch.build.targets.sdist]
include = [
  "result.schema.json",
  "config.example.json",
  "contract/**",
  "scripts/**",
  "tests/**",
  "README.md",
]

[tool.hatch.build.targets.wheel]
packages = ["hyvv"]

[tool.hatch.build.targets.wheel.force-include]
"rails_log_source.py" = "rails_log_source.py"
"rails_export.py" = "rails_export.py"
"contrib/rizzstack_rails" = "contrib/rizzstack_rails"
"agy_cua_mcp.py" = "agy_cua_mcp.py"
"brief.schema.json" = "brief.schema.json"
"result.schema.json" = "result.schema.json"
"""


def _c2_pkg_patch() -> str:
    after = _C2_PKG_PYPROJECT.replace(
        '  "contract/**",\n',
        '  "contract/**",\n  "schemas/**",\n',
    ).replace(
        '"contrib/rizzstack_rails" = "contrib/rizzstack_rails"\n',
        '"contrib/rizzstack_rails" = "contrib/rizzstack_rails"\n'
        '"schemas/rizzstack-ticket.schema.json" = "schemas/rizzstack-ticket.schema.json"\n',
    )
    return _text_patch("pyproject.toml", _C2_PKG_PYPROJECT, after)


def test_c2_pkg_actual_packaging_lines_are_not_findings():
    patch = _c2_pkg_patch()
    assert _scan(patch, base={"pyproject.toml": _C2_PKG_PYPROJECT}) == []
    assert subversion_scan.scan_patch(patch) == []


def test_build_system_and_hatch_build_lines_are_not_findings():
    before = _PYPROJECT_BASE
    after = before.replace(
        'requires = ["hatchling"]', 'requires = ["hatchling", "wheel"]'
    ).replace('  "README.md",\n', '  "README.md",\n  "schemas/**",\n')
    assert _pyproject_patch(before, after) == []


def test_project_metadata_lines_are_not_findings():
    before = _PYPROJECT_BASE
    after = before.replace('version = "0.1.0"', 'version = "0.1.1"').replace(
        'description = "A package"', 'description = "A renamed package"'
    )
    assert _pyproject_patch(before, after) == []


def test_build_line_is_clean_when_pytest_config_already_exists():
    before = (
        _PYPROJECT_BASE
        + '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-p preexisting"\n'
    )
    after = before.replace('  "README.md",\n', '  "README.md",\n  "schemas/**",\n')
    assert _pyproject_patch(before, after) == []


def test_build_line_is_clean_when_a_pytest11_entry_point_already_exists():
    before = (
        _PYPROJECT_BASE
        + '\n[project.entry-points.pytest11]\npreexisting = "preexisting.plugin"\n'
    )
    after = before.replace('  "README.md",\n', '  "README.md",\n  "schemas/**",\n')
    assert _pyproject_patch(before, after) == []


@pytest.mark.parametrize(
    "option,value",
    [
        ("addopts", '"-q"'),
        ("testpaths", '["tests"]'),
        ("markers", '["slow: slow tests"]'),
        ("filterwarnings", '["ignore::DeprecationWarning"]'),
        ("minversion", '"7.0"'),
        ("python_files", '"test_*.py"'),
    ],
    ids=["addopts", "testpaths", "markers", "filterwarnings", "minversion", "python_files"],
)
def test_tool_pytest_option_is_a_finding(option, value):
    before = _PYPROJECT_BASE + "\n[tool.pytest.ini_options]\n"
    kinds = {finding["kind"] for finding in _pyproject_patch(before, before + f"{option} = {value}\n")}
    assert "pytest_config" in kinds


def test_tool_pytest_addopts_plugin_flag_is_a_finding():
    before = _PYPROJECT_BASE + "\n[tool.pytest.ini_options]\n"
    kinds = {
        finding["kind"]
        for finding in _pyproject_patch(before, before + 'addopts = "-q -p evil"\n')
    }
    assert {"pytest_config", "pytest_plugin"} <= kinds


def test_tool_coverage_table_is_a_finding():
    before = _PYPROJECT_BASE + "\n[tool.coverage.run]\n"
    findings = _pyproject_patch(before, before + 'omit = ["pkg/*"]\n')
    assert "coverage_config" in {finding["kind"] for finding in findings}


def test_pytest11_entry_point_table_is_a_finding():
    before = _PYPROJECT_BASE
    after = before + '\n[project.entry-points.pytest11]\nevil = "evil.plugin"\n'
    findings = _pyproject_patch(before, after)
    assert {finding["kind"] for finding in findings} >= {"pytest_plugin"}


def test_quoted_pytest11_entry_point_table_is_a_finding():
    before = _PYPROJECT_BASE
    after = before + '\n[project.entry-points."pytest11"]\nevil = "evil.plugin"\n'
    findings = _pyproject_patch(before, after)
    assert "pytest_plugin" in {finding["kind"] for finding in findings}


def test_core_pytest_plugin_dependency_is_a_finding():
    before = _PYPROJECT_BASE
    after = before.replace(
        '  "requests>=2",\n', '  "requests>=2",\n  "pytest-evil>=1",\n'
    )
    findings = _pyproject_patch(before, after)
    assert "pytest_plugin" in {finding["kind"] for finding in findings}


def test_optional_pytest_plugin_dependency_is_a_finding():
    before = _PYPROJECT_BASE + '\n[project.optional-dependencies]\ntest = [\n  "pytest>=8",\n]\n'
    after = before.replace('  "pytest>=8",\n', '  "pytest>=8",\n  "pytest-evil>=1",\n')
    findings = _pyproject_patch(before, after)
    assert "pytest_plugin" in {finding["kind"] for finding in findings}


def test_dependency_named_by_a_pytest11_entry_point_is_a_finding():
    before = _PYPROJECT_BASE + '\n[project.entry-points.pytest11]\nacme = "acme_hooks"\n'
    after = before.replace(
        '  "requests>=2",\n', '  "requests>=2",\n  "acme-hooks>=1",\n'
    )
    findings = _pyproject_patch(before, after)
    assert "pytest_plugin" in {finding["kind"] for finding in findings}


def test_ordinary_dependency_is_not_a_finding():
    before = _PYPROJECT_BASE
    after = before.replace('  "requests>=2",\n', '  "requests>=2",\n  "httpx>=0.27",\n')
    assert _pyproject_patch(before, after) == []


def test_pytest_looking_key_in_a_build_table_is_not_a_finding():
    before = _PYPROJECT_BASE + "\n[tool.hatch.build.targets.sdist.extra]\n"
    assert _pyproject_patch(before, before + 'addopts = "-p evil"\n') == []


def test_header_shaped_line_inside_a_multiline_string_is_not_a_finding():
    before = (
        _PYPROJECT_BASE
        + "\n[tool.hatch.build.targets.sdist.metadata]\nnote = \"\"\"\n"
    )
    after = before + '[tool.pytest.ini_options]\n"""\n'
    assert _pyproject_patch(before, after) == []


def test_pytest_header_after_a_build_table_is_a_finding():
    before = _PYPROJECT_BASE
    after = before + "\n[tool.pytest.ini_options]\n"
    findings = _pyproject_patch(before, after)
    assert [(f["kind"], f["path"]) for f in findings if f["kind"] == "pytest_config"] == [
        ("pytest_config", "pyproject.toml")
    ]


# --- the wired verify gate ------------------------------------------------


def test_verify_refuses_a_patch_with_findings():
    patch = _new_file("tests/conftest.py", "import pytest")
    with pytest.raises(verify_local.VerifyError) as caught:
        _check(patch)
    assert caught.value.code == "test_subversion"
    assert "tests/conftest.py" in caught.value.message


def test_verify_accepts_a_clean_patch():
    patch = _edit("src/pkg/core.py", ["    return 2"], ["    return 1"])
    assert _check(patch) == []


# --- file-level shapes need no added line ---------------------------------


@pytest.mark.parametrize(
    "path,kind",
    [
        ("tests/conftest.py", "conftest"),
        ("sitecustomize.py", "startup_hook"),
        ("src/evil.pth", "pth_file"),
    ],
    ids=[
        "tests/conftest.py-conftest",
        "sitecustomize.py-startup_hook",
        "src/evil.pth-pth_file",
    ],
)
def test_empty_added_file_is_a_finding(path, kind):
    findings = _scan(_new_empty_file(path))
    assert [(f["path"], f["line"]) for f in findings if f["kind"] == kind] == [(path, 1)]


def test_pure_rename_to_conftest_is_a_finding():
    patch, base = _rename("tests/old_helpers.py", "tests/conftest.py")
    findings = _scan(patch, base)
    assert [(f["path"], f["line"]) for f in findings if f["kind"] == "conftest"] == [
        ("tests/conftest.py", 1)
    ]


def test_rename_away_from_conftest_is_not_a_finding():
    patch, base = _rename("tests/conftest.py", "tests/helpers.py")
    assert _scan(patch, base) == []


# --- cheap evasion hardening ---------------------------------------------


@pytest.mark.parametrize(
    "line,kind",
    [
        ("from os import _exit", "forced_exit"),
        ("from os import _exit; _exit(0)", "forced_exit"),
        ("import os as o; o._exit(0)", "forced_exit"),
        ("import atexit as a; a.register(_rewrite)", "atexit"),
    ],
    ids=[
        "from os import _exit-forced_exit",
        "from os import _exit; _exit(0)-forced_exit",
        "import os as o; o._exit(0)-forced_exit",
        "import atexit as a; a.register(_rewrite)-atexit",
    ],
)
def test_evasive_runner_subversion_is_a_finding(line, kind):
    assert kind in _kinds(_edit("src/pkg/core.py", [line]))


# --- plugin loads outside addopts/pytest_plugins -------------------------


@pytest.mark.parametrize(
    "path,line",
    [
        ("pyproject.toml", "[project.entry-points.pytest11]"),
        ("setup.cfg", "pytest11 = mypkg.hooks"),
    ],
    ids=["pyproject.toml-[project.entry-points.pytest11]", "setup.cfg-pytest11 = mypkg.hooks"],
)
def test_pytest11_entry_point_is_a_finding(path, line):
    assert "pytest_plugin" in _kinds(_edit(path, [line]))


# --- no false positives on prose and comparisons -------------------------


def test_doc_prose_about_runner_hooks_is_not_a_finding():
    patch = _edit("docs/runner-notes.md", ["Never call os._exit(0).", "Prefer atexit.register(_close)."])
    assert _scan(patch) == []


def test_legitimate_addopts_plugin_option_is_still_a_finding():
    assert "pytest_plugin" in _kinds(_edit("pyproject.toml", ['addopts = "-q -p no:cacheprovider"']))


def test_assertion_comparison_is_not_a_patched_assertion():
    assert "patched_assertion" not in _kinds(_edit("tests/test_x.py", ["assert AssertionError == type(e)"]))


def test_pytest_plugins_in_docs_is_not_a_finding():
    assert _scan(_edit("docs/plugins.md", ['pytest_plugins = ["pkg.hooks"]'])) == []


# --- the locked verify path is wired -------------------------------------


def _exploding_runner(*args, **kwargs):
    raise AssertionError("the test runner must not be invoked")


def test_locked_verify_refuses_subversion_before_runner_or_worktree(tmp_path):
    _scan_change()
    base_sha = _init_repo(tmp_path / "repo", {})
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local._verify_and_commit_locked(
            tmp_path / "state",
            tmp_path / "repo",
            "att-0000000000000000",
            patch=_new_file("tests/conftest.py", "import pytest"),
            declared_files=["tests/conftest.py"],
            base_rev=base_sha,
            tests=["tests/test_subversion_scan_v2.py"],
            runner=_exploding_runner,
            worktree_root=tmp_path,
        )
    assert caught.value.code == "test_subversion"
    assert not (tmp_path / "wt").exists()


# --- deletions and mode-only changes are not findings --------------------


def test_deleting_an_empty_conftest_is_not_a_finding():
    assert _scan(_empty_delete("tests/conftest.py")) == []


@pytest.mark.parametrize(
    "path", ["sitecustomize.py", "src/evil.pth"], ids=["sitecustomize.py", "src/evil.pth"]
)
def test_deleting_an_empty_risky_file_is_not_a_finding(path):
    assert _scan(_empty_delete(path)) == []


def test_deleting_a_non_empty_conftest_is_not_a_finding():
    assert _scan(_delete("tests/conftest.py", "import os", "os._exit(0)")) == []


def test_verify_accepts_deleting_a_conftest():
    assert _check(_empty_delete("tests/conftest.py")) == []
    deleted = _delete("tests/conftest.py", "import os", "os._exit(0)")
    assert _check(deleted) == []


def test_deletion_only_edit_of_conftest_is_not_a_finding():
    patch = (
        "diff --git a/tests/conftest.py b/tests/conftest.py\n"
        "index c01d8a7..21b405d 100644\n"
        "--- a/tests/conftest.py\n"
        "+++ b/tests/conftest.py\n"
        "@@ -1,2 +1 @@\n"
        " import os\n"
        "-os._exit(0)\n"
    )
    assert _scan(patch) == []


def test_mode_only_change_of_conftest_is_not_a_finding():
    assert _scan(_mode_change("tests/conftest.py")) == []


def test_edit_that_adds_a_line_to_conftest_is_still_a_finding():
    patch = _edit("tests/conftest.py", ["import pytest"], ["import os"])
    assert [(f["kind"], f["line"]) for f in _scan(patch)] == [("conftest", 1)]


# --- a user-defined _exit helper is not a forced exit --------------------


def test_user_defined_exit_helper_is_not_a_forced_exit():
    patch = _edit("src/pkg/core.py", ["def _exit(code):", "    raise SystemExit(code)"])
    assert "forced_exit" not in _kinds(patch)


def test_one_line_exit_helper_still_reports_forced_exit():
    patch = _edit(
        "src/pkg/core.py",
        ["def _exit(code): os._exit(code)", "ex = _exit", "ex(0)"],
    )
    assert ("forced_exit", 1) in {(f["kind"], f["line"]) for f in _scan(patch)}


# --- the refusal message lists findings and counts them ------------------


def test_refusal_message_lists_findings_and_counts_them():
    patch = _new_file(
        "tests/conftest.py",
        "import os",
        "os._exit(0)",
        "atexit.register(_rewrite)",
        "os._exit(1)",
    )
    with pytest.raises(verify_local.VerifyError) as caught:
        _check(patch)
    message = caught.value.message
    assert "7 finding" in message
    assert "conftest@tests/conftest.py:1" in message
    assert message.count("@") == 5
    assert "+2 more" in message


# --- obsolete-by-design: patch-text edges V2 cannot have -----------------
#
# These node ids are kept, skipped and labelled. They pin behaviour of a
# scanner that reads diff text and regex-parses configuration. V2 reads the
# applied tree and the parsed configuration, so each case is replaced by a
# semantic equivalent (for example, quoted TOML keys are the same table to
# tomllib). Never delete them silently and never let them run against V2.


@pytest.mark.skip(
    reason="obsolete-by-design: V2 parses the head TOML document with tomllib, so a quoted or escaped key spelling cannot change which table is read"
)
@pytest.mark.parametrize(
    "line",
    [
        '[project."entry-points".pytest11]',
        '["project"."entry-points"."pytest11"]',
        "[project.'entry-points'.'pytest11']",
        "[project.\"entry-points\".'pytest11']",
        '[ project . "entry-points" . pytest11 ]',
        '[project.entry-points."pytest\\u0031\\u0031"]',
        '[project.entry-points."pytest\\U00000031\\U00000031"]',
        '["pro\\u006Aect".entry-points.pytest11]',
        '["project"."entry-points"."pytest11"]  # entry point group',
    ],
    ids=[
        "quoted-entry-points",
        "all-quoted",
        "single-quoted",
        "mixed-quotes",
        "spaced-and-quoted",
        "escaped-u",
        "escaped-U",
        "escaped-project",
        "quoted-with-comment",
    ],
)
def test_quoted_or_escaped_toml_key_segments_are_a_finding(line):
    assert "pytest_plugin" in _kinds(_edit("pyproject.toml", [line]))


@pytest.mark.skip(
    reason="obsolete-by-design: V2 parses the head TOML document with tomllib, so a quoted or escaped key spelling cannot turn a different table into pytest11"
)
@pytest.mark.parametrize(
    "line",
    [
        "[project.entry-points.pytest11x]",
        '[project."entry-points"."pytest11x"]',
        "[project.entry-points.pytest11.foo]",
        '["project.entry-points".pytest11]',
        '[project."entry-points.pytest11"]',
        '[project.entry-points."pytest1\\u0031x"]',
        '["project"."entry-points"."pytest12"]',
    ],
    ids=[
        "suffix-x",
        "quoted-suffix-x",
        "child-table",
        "dot-inside-first-key",
        "dot-inside-second-key",
        "escape-then-x",
        "quoted-other-group",
    ],
)
def test_quoted_or_escaped_key_segments_do_not_over_match(line):
    assert _scan(_edit("pyproject.toml", [line])) == []


@pytest.mark.skip(
    reason="obsolete-by-design: tomllib resolves the pytest11 table whatever the quoting of its key segments"
)
@pytest.mark.parametrize(
    "line",
    [
        '[project.entry-points."pytest11"]',
        "[project . entry-points . pytest11]",
        "[ project . entry-points . 'pytest11' ]",
    ],
    ids=["double-quoted", "dotted-spaces", "single-quoted-spaces"],
)
def test_quoted_pytest11_table_is_a_finding(line):
    assert "pytest_plugin" in _kinds(_edit("pyproject.toml", [line]))


@pytest.mark.skip(
    reason="obsolete-by-design: addopts is read from the parsed TOML value, so a '#' inside the string is data and not a comment"
)
def test_toml_hash_in_quoted_addopts_is_still_a_plugin_finding():
    assert "pytest_plugin" in _kinds(
        _edit("pyproject.toml", ['addopts = """', "-q", "# prefer -p", '"""'])
    )


@pytest.mark.skip(
    reason="obsolete-by-design: configparser strips INI comments before the addopts value is read"
)
@pytest.mark.parametrize(
    "path,lines",
    [
        ("setup.cfg", ["addopts =", "    -q", "    # prefer -p", "    -x"]),
        ("pytest.ini", ["addopts =", "    ; prefer -p", "    -q"]),
        ("tox.ini", ["addopts =", "    -q", "    # prefer -p"]),
    ],
    ids=["setup.cfg-hash", "pytest.ini-semicolon", "tox.ini-hash"],
)
def test_ini_comment_in_addopts_is_not_a_plugin_finding(path, lines):
    assert "pytest_plugin" not in _kinds(_edit(path, lines))


@pytest.mark.skip(
    reason="obsolete-by-design: configparser strips INI comments before the addopts value is read"
)
def test_ini_comment_mentioning_the_long_option_is_not_a_finding():
    assert "pytest_plugin" not in _kinds(
        _edit("tox.ini", ["addopts =", "    # prefer --plugin", "    -q"])
    )


@pytest.mark.skip(
    reason="obsolete-by-design: V2 takes changed paths from git name-status, never from a quoted +++ header, so a decoded separator cannot reach the refusal message"
)
def test_quoted_path_control_characters_cannot_spoof_the_refusal_message():
    body = "+os._exit(0)\n"
    patch = (
        "diff --git a/src/evil.py b/src/evil.py\n"
        "--- /dev/null\n"
        '+++ "b/src/x\\nspoof.py"\n'
        "@@ -0,0 +1,1 @@\n" + body
    )
    findings = _scan(patch)
    assert findings
    assert all("\n" not in f["path"] and "\r" not in f["path"] for f in findings)
    with pytest.raises(verify_local.VerifyError) as caught:
        _check(patch)
    assert "\n" not in caught.value.message and "\r" not in caught.value.message


@pytest.mark.skip(
    reason="obsolete-by-design: V2 takes changed paths from git name-status, never from a quoted +++ header, so a decoded separator cannot reach the refusal message"
)
@pytest.mark.parametrize("escape", ["\\u2028", "\\u2029"], ids=["u2028", "u2029"])
def test_quoted_path_unicode_separators_cannot_spoof_the_refusal_message(escape):
    patch = (
        "diff --git a/src/evil.py b/src/evil.py\n"
        "--- /dev/null\n"
        f'+++ "b/src/x{escape}spoof.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+os._exit(0)\n"
    )
    findings = _scan(patch)
    assert findings
    for finding in findings:
        assert "\u2028" not in finding["path"] and "\u2029" not in finding["path"]
    with pytest.raises(verify_local.VerifyError) as caught:
        _check(patch)
    message = caught.value.message
    assert "\u2028" not in message and "\u2029" not in message
    assert "\n" not in message and "\r" not in message


@pytest.mark.skip(
    reason="obsolete-by-design: V2 takes changed paths from git name-status, never from a quoted +++ header, so a raw separator in patch text cannot split a header"
)
@pytest.mark.parametrize("separator", ["\u2028", "\u2029"], ids=["u2028", "u2029"])
def test_raw_unicode_separator_in_quoted_path_keeps_the_finding(separator):
    patch = (
        "diff --git a/src/evil.py b/src/evil.py\n"
        "--- /dev/null\n"
        f'+++ "b/tests/conftest{separator}.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+os._exit(0)\n"
    )
    findings = _scan(patch)
    assert {finding["kind"] for finding in findings} == {"conftest", "forced_exit"}
    assert all(finding["path"] == "tests/conftest.py" for finding in findings)
    with pytest.raises(verify_local.VerifyError) as caught:
        _check(patch)
    message = caught.value.message
    assert "\u2028" not in message and "\u2029" not in message
    assert "\n" not in message and "\r" not in message


@pytest.mark.skip(
    reason="obsolete-by-design: V2 takes changed paths from git name-status, never from a quoted +++ header, so a raw separator in patch text cannot split a header"
)
@pytest.mark.parametrize(
    "separator",
    ["\u000b", "\u000c", "\u001c", "\u001d", "\u001e", "\u0085"],
    ids=["vt", "ff", "fs", "gs", "rs", "nel"],
)
def test_raw_control_separator_in_quoted_path_keeps_the_finding(separator):
    patch = (
        "diff --git a/src/evil.py b/src/evil.py\n"
        "--- /dev/null\n"
        f'+++ "b/tests/conftest{separator}.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+os._exit(0)\n"
    )
    findings = _scan(patch)
    assert {finding["kind"] for finding in findings} == {"conftest", "forced_exit"}
    assert all(finding["path"] == "tests/conftest.py" for finding in findings)


@pytest.mark.skip(
    reason="obsolete-by-design: the patch is applied by git, not split line by line, so a CRLF header cannot truncate a path"
)
def test_crlf_terminated_patch_headers_still_parse():
    patch = _new_file("tests/conftest.py", "import pytest").replace("\n", "\r\n")
    assert "conftest" in _kinds(patch)


@pytest.mark.skip(
    reason="obsolete-by-design: V2 reads the applied tree; a binary-marked modification is a name-status M with a changed blob, not a GIT binary patch to parse"
)
def test_binary_marked_modification_of_conftest_is_still_a_finding():
    patch = _binary_modification("tests/conftest.py")
    assert [(f["kind"], f["line"]) for f in _scan(patch)] == [("conftest", 1)]


@pytest.mark.skip(
    reason="obsolete-by-design: V2 reads the applied tree; a binary-marked modification is a name-status M with a changed blob, not a GIT binary patch to parse"
)
def test_binary_marked_modification_of_a_risky_pth_is_still_a_finding():
    assert "pth_file" in _kinds(_binary_modification("src/evil.pth"))


@pytest.mark.skip(
    reason="obsolete-by-design: addopts is a parsed value, not a stream of hunk lines, so multiline continuation state is gone"
)
@pytest.mark.parametrize(
    "path,lines",
    [
        ("pyproject.toml", ['addopts = """', "-q", "-p myplugin", '"""']),
        ("setup.cfg", ["addopts =", "    -q", "    -p myplugin"]),
    ],
    ids=["pyproject.toml-lines0", "setup.cfg-lines1"],
)
def test_multiline_addopts_plugin_is_a_finding(path, lines):
    assert "pytest_plugin" in _kinds(_edit(path, lines))


@pytest.mark.skip(
    reason="obsolete-by-design: addopts is a parsed value, not a stream of hunk lines, so a -p at the end of a continuation line is just another token"
)
@pytest.mark.parametrize(
    "path,lines",
    [
        ("pyproject.toml", ['addopts = """', "-q", "-p", "myplugin", '"""']),
        ("setup.cfg", ["addopts =", "    -q", "    -p", "    myplugin"]),
        ("pytest.ini", ["addopts = -q -p", "    myplugin"]),
    ],
    ids=["pyproject.toml-lines0", "setup.cfg-lines1", "pytest.ini-lines2"],
)
def test_addopts_plugin_option_at_end_of_line_is_a_finding(path, lines):
    assert "pytest_plugin" in _kinds(_edit(path, lines))


@pytest.mark.skip(
    reason="obsolete-by-design: addopts is a parsed value, not a stream of hunk lines, so --plugin at the end of a continuation line is just another token"
)
@pytest.mark.parametrize(
    "path,lines",
    [
        ("pyproject.toml", ['addopts = """', "-q", "--plugin", "myplugin", '"""']),
        ("setup.cfg", ["addopts =", "    -q", "    --plugin", "    myplugin"]),
    ],
    ids=["pyproject.toml-triple-quoted", "setup.cfg-indented"],
)
def test_plugin_long_option_at_end_of_line_is_a_finding(path, lines):
    assert "pytest_plugin" in _kinds(_edit(path, lines))


# --- the main ``verify`` command must refuse before any work --------------
#
# WA-A1A wired the scan into ``verify_local.verify_and_commit`` only;
# ``cli._cmd_verify`` (the ``verify`` command a coordinator reaches) applied
# the patch and ran the sandbox without scanning it. These approved cases drive
# the real ``_cmd_verify`` with a fake client and a real temporary
# lease/worktree (no network, no launcher, no remote push) and pin the refusal
# to the scan:
#
# * a subverting patch is refused with code ``test_subversion``, naming the
#   finding, before any worktree, sandbox, commit or push work;
# * the same patch clears every other gate (the negative case), so only the
#   scan can be responsible for the refusal;
# * a clean patch still verifies.

SUBVERSION_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,3 @@
 value = 1
+import os
+os._exit(0)
"""

CLEAN_PATCH = """diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1 +1,2 @@
 value = 1
+value = 2
"""


def _record(state: Path) -> dict:
    return json.loads(
        (state / "verify-results" / "dry-run" / f"{ATTEMPT_ID}.json").read_text()
    )


class _Doubles:
    """Hermetic stand-ins that record every step the refusal must prevent."""

    def __init__(self) -> None:
        self.sandbox: list[list[str]] = []
        self.commits: list[str] = []
        self.pushes: list[str] = []

    def run_in_sandbox(self, _workdir: Path, argv: list[str], **_kwargs) -> dict:
        self.sandbox.append(list(argv))
        return {"backend": "fake", "exit_code": 0, "timed_out": False, "output_tail": ""}

    def commit_changes(self, workdir: Path, attempt_id: str, files: list[str]) -> str:
        self.commits.append(attempt_id)
        raise AssertionError("verify must refuse before committing")

    def push_branch(self, workdir: Path, branch: str) -> None:
        self.pushes.append(branch)
        raise AssertionError("verify must refuse before pushing")


def _drive(repo: Path, state: Path, patch: str, base_sha: str, monkeypatch: pytest.MonkeyPatch):
    """Run ``verify --dry-run``; return ``(rc, propagated error, steps that ran)``."""
    _scan_change()
    doubles = _Doubles()
    monkeypatch.setattr(cli.sandbox, "run_in_sandbox", doubles.run_in_sandbox)
    monkeypatch.setattr(cli, "_commit_changes", doubles.commit_changes)
    monkeypatch.setattr(cli, "_push_branch", doubles.push_branch)
    client = FakeClient(_result(patch, base_rev=base_sha))
    try:
        rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    except cli.CliError as exc:  # some entry points propagate instead of printing
        return 1, exc, doubles
    return rc, None, doubles


def _code(error, rc: int, state: Path) -> str:
    if error is not None:
        return str(error.code)
    return str(_record(state).get("code") or "")


def _setup(tmp_path: Path) -> tuple[Path, Path, str]:
    repo, _remote, base_sha = _cli_repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    return repo, state, base_sha


def test_cli_verify_refuses_a_subverting_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc != 0, "the verify command accepted a subverting patch"
    assert _code(error, rc, state) == "test_subversion"
    record = _record(state)
    assert record["ok"] is False
    assert "forced_exit" in record["error"]
    assert "src/a.py" in record["error"]
    assert doubles.sandbox == []


def test_cli_verify_refusal_precedes_worktree_sandbox_commit_and_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc != 0, "the verify command accepted a subverting patch"
    assert _code(error, rc, state) == "test_subversion"
    assert _cli_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert doubles.sandbox == []
    assert doubles.commits == []
    assert doubles.pushes == []


def test_cli_verify_accepts_a_clean_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, state, base_sha = _setup(tmp_path)

    rc, error, doubles = _drive(repo, state, CLEAN_PATCH, base_sha, monkeypatch)

    assert rc == 0, f"a clean patch was refused: {_code(error, rc, state)}"
    assert len(doubles.sandbox) == 1
    record = _record(state)
    assert record["ok"] is True
    assert "code" not in record


def test_the_scan_alone_refuses_the_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Negative case: without the scan the old path accepts this patch.

    Every other gate (lease scope, revision checks, branch, sandbox) passes for
    ``SUBVERSION_PATCH``; that is what made the CLI gap exploitable, and this
    case fails if a future change starts refusing unrelated patches.
    """
    repo, state, base_sha = _setup(tmp_path)
    monkeypatch.setattr(verify_local, "check_patch_subversion", lambda *a, **k: [])
    monkeypatch.setattr(cli, "check_patch_subversion", lambda *a, **k: [], raising=False)

    rc, error, _doubles = _drive(repo, state, SUBVERSION_PATCH, base_sha, monkeypatch)

    assert rc == 0, f"something other than the scan refused: {_code(error, rc, state)}"
    record = _record(state)
    assert record["ok"] is True
    assert "code" not in record
