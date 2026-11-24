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


# --- review finding 2: file-level shapes must not need an added line -------


def _new_empty_file(path: str) -> str:
    """What git emits for an empty new file: no ---/+++ header and no hunk."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..e69de29\n"
    )


def _rename(old: str, new: str) -> str:
    """A pure rename diff: no ---/+++ header and no hunk."""
    return (
        f"diff --git a/{old} b/{new}\n"
        "similarity index 100%\n"
        f"rename from {old}\n"
        f"rename to {new}\n"
    )


@pytest.mark.parametrize(
    "path,kind",
    [
        ("tests/conftest.py", "conftest"),
        ("sitecustomize.py", "startup_hook"),
        ("src/evil.pth", "pth_file"),
    ],
)
def test_empty_added_file_is_a_finding(path, kind):
    findings = _scan()(_new_empty_file(path))
    assert [(f["path"], f["line"]) for f in findings if f["kind"] == kind] == [(path, 1)]


def test_pure_rename_to_conftest_is_a_finding():
    findings = _scan()(_rename("tests/old_helpers.py", "tests/conftest.py"))
    assert [(f["path"], f["line"]) for f in findings if f["kind"] == "conftest"] == [
        ("tests/conftest.py", 1)
    ]


def test_rename_away_from_conftest_is_not_a_finding():
    assert _scan()(_rename("tests/conftest.py", "tests/helpers.py")) == []


# --- review finding 3: cheap evasion hardening --------------------------


@pytest.mark.parametrize(
    "line,kind",
    [
        ("from os import _exit", "forced_exit"),
        ("from os import _exit; _exit(0)", "forced_exit"),
        ("import os as o; o._exit(0)", "forced_exit"),
        ("import atexit as a; a.register(_rewrite)", "atexit"),
    ],
)
def test_evasive_runner_subversion_is_a_finding(line, kind):
    assert kind in _kinds(_edit("src/pkg/core.py", [line]))


# --- review finding 4: plugin loads outside addopts/pytest_plugins -------


@pytest.mark.parametrize(
    "path,line",
    [
        ("pyproject.toml", "[project.entry-points.pytest11]"),
        ("setup.cfg", "pytest11 = mypkg.hooks"),
    ],
)
def test_pytest11_entry_point_is_a_finding(path, line):
    assert "pytest_plugin" in _kinds(_edit(path, [line]))


@pytest.mark.parametrize(
    "path,lines",
    [
        ("pyproject.toml", ['addopts = """', "-q", "-p myplugin", '"""']),
        ("setup.cfg", ["addopts =", "    -q", "    -p myplugin"]),
    ],
)
def test_multiline_addopts_plugin_is_a_finding(path, lines):
    assert "pytest_plugin" in _kinds(_edit(path, lines))


# --- review finding 5: no false positives on prose and comparisons -------


def test_doc_prose_about_runner_hooks_is_not_a_finding():
    patch = _edit("docs/runner-notes.md", ["Never call os._exit(0).", "Prefer atexit.register(_close)."])
    assert _scan()(patch) == []


def test_legitimate_addopts_plugin_option_is_still_a_finding():
    assert "pytest_plugin" in _kinds(_edit("pyproject.toml", ['addopts = "-q -p no:cacheprovider"']))


def test_assertion_comparison_is_not_a_patched_assertion():
    assert "patched_assertion" not in _kinds(_edit("tests/test_x.py", ["assert AssertionError == type(e)"]))


def test_pytest_plugins_in_docs_is_not_a_finding():
    assert _scan()(_edit("docs/plugins.md", ['pytest_plugins = ["pkg.hooks"]'])) == []


# --- review finding 6: the locked verify path is wired -------------------


def _exploding_runner(*args, **kwargs):
    raise AssertionError("the test runner must not be invoked")


def test_locked_verify_refuses_subversion_before_runner_or_worktree(tmp_path):
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local._verify_and_commit_locked(
            tmp_path / "state",
            tmp_path / "repo",
            "att-0000000000000000",
            patch=_new_file("tests/conftest.py", "import pytest"),
            declared_files=["tests/conftest.py"],
            base_rev="0" * 40,
            tests=["tests/test_subversion_scan.py"],
            runner=_exploding_runner,
            worktree_root=tmp_path,
        )
    assert caught.value.code == "test_subversion"
    assert not (tmp_path / "wt").exists()


# --- review finding 7: quoted paths cannot spoof the refusal message -----


def test_quoted_path_control_characters_cannot_spoof_the_refusal_message():
    body = "+os._exit(0)\n"
    patch = (
        "diff --git a/src/evil.py b/src/evil.py\n"
        "--- /dev/null\n"
        '+++ "b/src/x\\nspoof.py"\n'
        "@@ -0,0 +1,1 @@\n" + body
    )
    findings = _scan()(patch)
    assert findings
    assert all("\n" not in f["path"] and "\r" not in f["path"] for f in findings)
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local.check_patch_subversion(patch)
    assert "\n" not in caught.value.message and "\r" not in caught.value.message


# --- review finding 8: report every finding up to a small limit ----------


def test_refusal_message_lists_findings_and_counts_them():
    patch = _new_file(
        "tests/conftest.py",
        "import os",
        "os._exit(0)",
        "atexit.register(_rewrite)",
        "os._exit(1)",
    )
    with pytest.raises(verify_local.VerifyError) as caught:
        verify_local.check_patch_subversion(patch)
    message = caught.value.message
    assert "7 finding" in message
    assert "conftest@tests/conftest.py:1" in message
    assert message.count("@") == 5
    assert "+2 more" in message
