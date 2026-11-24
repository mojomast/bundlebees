"""RED for wave A slice A1a: a static test-subversion scan of each patch.

A green run is necessary, not sufficient. Code under test can rewrite the
evidence files or force the exit code, so verify refuses a patch whose
added lines or added files reach into the test runner itself. Only added
lines count; removing such code is never a finding.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import verify_local


def _scan():
    from bundlebees_plugin import subversion_scan

    return subversion_scan.scan_patch


def _new_file(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}"
    )


def _edit(path: str, added: list[str], removed: list[str] | None = None) -> str:
    removed = removed or []
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"
    )


def _kinds(patch: str) -> set[str]:
    return {finding["kind"] for finding in _scan()(patch)}


@pytest.mark.parametrize(
    "path",
    ["tests/conftest.py", "conftest.py", "src/pkg/conftest.py"],
)
def test_added_conftest_is_a_finding(path):
    assert "conftest" in _kinds(_new_file(path, "import pytest"))


@pytest.mark.parametrize("path", ["sitecustomize.py", "src/usercustomize.py"])
def test_interpreter_startup_hooks_are_findings(path):
    assert "startup_hook" in _kinds(_new_file(path, "x = 1"))


def test_pth_file_is_a_finding():
    assert "pth_file" in _kinds(_new_file("src/evil.pth", "import os"))


@pytest.mark.parametrize(
    "path,line",
    [
        ("pyproject.toml", 'addopts = "-p myplugin"'),
        ("pytest.ini", "addopts = -p no:cacheprovider -p myplugin"),
        ("setup.cfg", "addopts = -p myplugin"),
        ("tox.ini", "addopts = -p myplugin"),
        ("src/pkg/__init__.py", 'pytest_plugins = ["pkg.hooks"]'),
    ],
)
def test_pytest_plugin_loading_is_a_finding(path, line):
    assert "pytest_plugin" in _kinds(_edit(path, [line]))


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
)
def test_runner_subversion_in_added_lines_is_a_finding(line, kind):
    assert kind in _kinds(_edit("src/pkg/core.py", [line]))


def test_benign_patch_has_no_findings():
    patch = _edit("src/pkg/core.py", ["def value():", "    return 2"], ["def value():", "    return 1"])
    assert _scan()(patch) == []


def test_removed_lines_are_not_findings():
    patch = _edit("src/pkg/core.py", ["pass"], ["    os._exit(0)", "atexit.register(_rewrite)"])
    assert _scan()(patch) == []


def test_findings_name_the_path_and_line_without_echoing_secrets():
    finding = _scan()(_edit("src/pkg/core.py", ["os._exit(0)  # token=" + "abc" + "def123"]))[0]
    assert finding["path"] == "src/pkg/core.py"
    assert isinstance(finding["line"], int) and finding["line"] >= 1
    assert "abcdef123" not in repr(finding)


def test_verify_refuses_a_patch_with_findings():
    patch = _new_file("tests/conftest.py", "import pytest")
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local.check_patch_subversion(patch)
    assert caught.value.code == "test_subversion"
    assert "tests/conftest.py" in caught.value.message


def test_verify_accepts_a_clean_patch():
    patch = _edit("src/pkg/core.py", ["    return 2"], ["    return 1"])
    assert verify_local.check_patch_subversion(patch) == []
