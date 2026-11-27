"""RED corpus for #324's oracle lock (tripwire + repo_root + lock).

A pull request owns its working tree, so it also owns ``pyproject.toml``,
``pytest.ini``, ``tox.ini`` and ``setup.cfg``.  Those files decide which
plugins pytest loads (``addopts = "-p evil"``, ``pytest_plugins``, a
``pytest11`` entry point) and what pytest collects.  A test job that reads the
PR's configuration is not an independent oracle.

The lock extracts *main*'s pytest configuration to an ini file and runs

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS= \
        pytest -c <main_config> --rootdir <repo> -p <each allowlisted plugin>

with the plugin allowlist read from main.  Every case below first proves the
PR's own configuration loads the evil plugin (the control run) and then proves
the locked run neither loads it nor fails the test.  The four spellings are the
ones an approving reviewer called out: an inline table, a dotted key, a
``[tool.pytest.ini_options]`` header and a ``project.entry-points.pytest11``
declaration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MAIN_PYPROJECT = """\
[project]
name = "oracle-demo"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-q"
"""

MAIN_TEST = "def test_ok():\n    assert True\n"
EVIL_PLUGIN = "raise RuntimeError('evil plugin loaded')\n"


def _oracle():
    """Import the module lazily so a missing lock fails these tests, not collection."""
    try:
        from bundlebees_plugin import oracle_lock
    except ImportError:
        raise AssertionError(
            "bundlebees_plugin.oracle_lock is missing: the verify/CI oracle lock "
            "is not implemented (ORACLE-LOCK RED)"
        ) from None
    return oracle_lock


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


def _main_repo(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q")
    payload = {"pyproject.toml": MAIN_PYPROJECT, "tests/test_ok.py": MAIN_TEST}
    payload.update(files or {})
    for rel, content in payload.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _pr_tree(
    repo: Path, tmp_path: Path, pyproject: str, extras: dict[str, str] | None = None
) -> Path:
    pr = tmp_path / "pr"
    shutil.copytree(repo, pr)
    (pr / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (pr / "evil_plugin.py").write_text(EVIL_PLUGIN, encoding="utf-8")
    for rel, content in (extras or {}).items():
        target = pr / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return pr


def _run(
    pr: Path,
    *,
    locked: bool,
    main_repo: Path,
    main_sha: str,
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    oracle = _oracle()
    # The shard that runs this suite may itself be under the lock, so strip the
    # lock variables from the control run: the control's whole point is that
    # the PR's configuration *would* load the plugin without the lock.
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in oracle.LOCK_ENV
    }
    env = oracle.locked_env(base) if locked else base
    if not locked:
        # Only the control run needs the PR tree importable for the evil plugin;
        # the locked run must not receive an import root from the judged tree.
        env["PYTHONPATH"] = str(pr)
    argv = [sys.executable, "-B", "-m", "pytest", "-q"]
    if locked:
        lock = oracle.from_git(main_repo, main_sha, dest=tmp_path / "main-pytest.ini")
        argv = oracle.locked_argv(lock, argv, rootdir=pr)
    return subprocess.run(
        argv, cwd=pr, env=env, capture_output=True, text=True, timeout=120, check=False
    )


def _assert_no_effect(tmp_path: Path, pyproject: str, extras: dict[str, str] | None = None):
    repo, sha = _main_repo(tmp_path)
    pr = _pr_tree(repo, tmp_path, pyproject, extras)
    control = _run(pr, locked=False, main_repo=repo, main_sha=sha, tmp_path=tmp_path)
    assert control.returncode != 0, "the PR configuration did not reach pytest"
    assert "evil plugin loaded" in (control.stdout + control.stderr)
    locked = _run(pr, locked=True, main_repo=repo, main_sha=sha, tmp_path=tmp_path)
    assert "evil plugin loaded" not in (locked.stdout + locked.stderr)
    assert locked.returncode == 0, (locked.stdout, locked.stderr)
    assert locked.stdout.strip().endswith("[100%]"), locked.stdout


# --- extraction: the lock reads main, never the PR -----------------------


def test_main_pyproject_pytest_options_are_extracted(tmp_path: Path):
    oracle = _oracle()
    repo, sha = _main_repo(tmp_path)
    text, plugins = oracle.main_config(repo, sha)
    assert "[pytest]" in text
    assert "testpaths" in text and "tests" in text
    assert 'addopts = -q' in text
    assert plugins == ()


def test_main_pytest_ini_section_and_plugin_allowlist_are_extracted(tmp_path: Path):
    oracle = _oracle()
    repo, sha = _main_repo(
        tmp_path,
        {
            "pytest.ini": (
                "[pytest]\n"
                "testpaths = tests\n"
                "addopts = -q -p allowed_plugin\n"
                "pytest_plugins = allowed_hooks\n"
                "markers =\n"
                "    slow: slow tests\n"
                "    net: network tests\n"
            )
        },
    )
    text, plugins = oracle.main_config(repo, sha)
    assert "addopts = -q" in text
    assert "markers = \n    slow: slow tests\n    net: network tests" in text
    assert "allowed_plugin" not in text  # re-added explicitly as -p
    assert "allowed_hooks" not in text  # re-added explicitly as -p
    assert plugins == ("allowed_plugin", "allowed_hooks")


def test_main_setup_cfg_section_is_extracted(tmp_path: Path):
    oracle = _oracle()
    repo, sha = _main_repo(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "oracle-demo"\nversion = "0.1.0"\n',
            "setup.cfg": "[tool:pytest]\naddopts = -p cfg_plugin\n",
        },
    )
    text, plugins = oracle.main_config(repo, sha)
    assert "cfg_plugin" not in text  # re-added explicitly as -p
    assert plugins == ("cfg_plugin",)


def test_the_locked_environment_disables_autoload_and_clears_addopts(tmp_path: Path):
    oracle = _oracle()
    env = oracle.locked_env({"PATH": "/bin", "PYTEST_ADDOPTS": "-p evil"})
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTEST_ADDOPTS"] == ""


# --- the four PR config spellings have no effect -------------------------


def test_pr_inline_table_plugin_load_has_no_effect(tmp_path: Path):
    pyproject = (
        'tool = { pytest = { ini_options = { addopts = "-p evil_plugin" } } }\n\n'
        '[project]\nname = "oracle-demo"\nversion = "0.1.0"\n'
    )
    _assert_no_effect(tmp_path, pyproject)


def test_pr_dotted_key_plugin_load_has_no_effect(tmp_path: Path):
    pyproject = (
        'tool.pytest.ini_options.addopts = "-p evil_plugin"\n\n'
        '[project]\nname = "oracle-demo"\nversion = "0.1.0"\n'
    )
    _assert_no_effect(tmp_path, pyproject)


def test_pr_pytest_header_plugin_load_has_no_effect(tmp_path: Path):
    pyproject = (
        '[project]\nname = "oracle-demo"\nversion = "0.1.0"\n\n'
        '[tool.pytest.ini_options]\naddopts = "-p evil_plugin"\n'
    )
    _assert_no_effect(tmp_path, pyproject)


def test_pr_pytest11_entry_point_autoload_has_no_effect(tmp_path: Path):
    pyproject = (
        '[project]\nname = "oracle-demo"\nversion = "0.1.0"\n\n'
        '[project.entry-points.pytest11]\nevil = "evil_plugin"\n'
    )
    dist_info = {
        "evil_plugin-0.1.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: evil-plugin\nVersion: 0.1.0\n"
        ),
        "evil_plugin-0.1.0.dist-info/entry_points.txt": "[pytest11]\nevil = evil_plugin\n",
    }
    _assert_no_effect(tmp_path, pyproject, dist_info)


# --- verify's own named-test command is locked ---------------------------


def test_verify_named_tests_run_with_the_main_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    oracle = _oracle()
    from bundlebees_plugin import verify_local

    repo, sha = _main_repo(tmp_path)
    lock = oracle.from_git(repo, sha, inside_name=oracle.CONFIG_NAME)
    seen: list[list[str]] = []

    def runner(workdir: Path, argv):
        seen.append(list(argv))
        return {"exit_code": 1, "timed_out": False, "output_tail": "red"}

    verify_local._run_tests(repo, ["tests/test_ok.py"], None, runner=runner, oracle=lock)

    assert len(seen) == 1
    argv = seen[0]
    assert argv[0] == sys.executable
    assert ["-c", lock.config_name] == argv[argv.index("-c") : argv.index("-c") + 2]
    assert argv[argv.index("--rootdir") + 1] == "."
    assert argv[-1] == "tests/test_ok.py"
    assert "-p" in argv  # at least the disable-cache provider argument
    assert not any("pyproject.toml" in argument for argument in argv)
    lock.cleanup()


# --- drill cases for the 782fd3b review -----------------------------------


def test_from_git_extracts_outside_the_worktree(tmp_path: Path):
    """The ini must not exist in the tree a patch is applied to."""
    oracle = _oracle()
    repo, sha = _main_repo(tmp_path)
    lock = oracle.from_git(repo, sha, inside_name=oracle.CONFIG_NAME)
    try:
        assert not (repo / oracle.CONFIG_NAME).exists()
        assert lock.config_path.parent != repo
        assert lock.config_name == oracle.CONFIG_NAME
        assert lock.config_path.read_text(encoding="utf-8") == oracle.main_config(repo, sha)[0]
    finally:
        lock.cleanup()
    assert not lock.config_path.exists()


def test_from_git_refuses_a_destination_inside_the_worktree(tmp_path: Path):
    oracle = _oracle()
    repo, sha = _main_repo(tmp_path)
    with pytest.raises(oracle.OracleLockError):
        oracle.from_git(repo, sha, dest=repo / oracle.CONFIG_NAME)


def test_patch_that_touches_the_oracle_config_name_is_refused(tmp_path: Path):
    oracle = _oracle()
    from bundlebees_plugin import verify_local

    assert verify_local._oracle_config_paths(
        [oracle.CONFIG_NAME, f"nested/{oracle.CONFIG_NAME}"], oracle.CONFIG_NAME
    ) == [oracle.CONFIG_NAME, f"nested/{oracle.CONFIG_NAME}"]
    assert verify_local._oracle_config_paths(["tests/test_ok.py"], oracle.CONFIG_NAME) == []


def test_top_level_import_shadows_are_refused():
    from bundlebees_plugin import verify_local

    found = verify_local._import_shadow_paths(
        ["pytest.py", "_pytest/__init__.py", "pluggy.py", "iniconfig.py",
         "packaging/__init__.py", "json.py", "pytest_asyncio.py",
         "tests/test_ok.py", "bundlebees_plugin/verify_local.py"],
        ("pytest_asyncio",),
    )
    assert found == [
        "_pytest/__init__.py",
        "iniconfig.py",
        "json.py",
        "packaging/__init__.py",
        "pluggy.py",
        "pytest.py",
        "pytest_asyncio.py",
    ]


def test_locked_argv_uses_the_safe_path_interpreter_flags(tmp_path: Path):
    oracle = _oracle()
    repo, sha = _main_repo(tmp_path)
    lock = oracle.from_git(repo, sha)
    try:
        argv = oracle.locked_argv(lock, [sys.executable, "-m", "pytest", "-q"], rootdir=repo)
        assert argv[:5] == [sys.executable, "-P", "-B", "-m", "pytest"]
        assert oracle.LOCK_ENV["PYTHONSAFEPATH"] == "1"
    finally:
        lock.cleanup()


def test_attached_short_config_and_override_options_are_stripped():
    oracle = _oracle()
    assert oracle._strip_config_overrides(
        ["-cEVIL.ini", "-oEVIL=1", "-pEVIL", "tests/test_ok.py"]
    ) == ["tests/test_ok.py"]


def test_a_shadowing_root_pytest_module_cannot_stand_in_for_pytest(tmp_path: Path):
    """A root pytest.py printing '1 passed' must not decide the run."""
    oracle = _oracle()
    repo, sha = _main_repo(tmp_path)
    pr = _pr_tree(
        repo,
        tmp_path,
        MAIN_PYPROJECT,
        {
            "pytest.py": "print('1 passed')\nraise SystemExit(0)\n",
            "tests/test_fail.py": "def test_fail():\n    assert False\n",
        },
    )
    lock = oracle.from_git(repo, sha)
    try:
        argv = oracle.locked_argv(
            lock, [sys.executable, "-B", "-m", "pytest", "-q"], rootdir=pr
        )
        env = oracle.locked_env()
        completed = subprocess.run(
            argv, cwd=pr, env=env, capture_output=True, text=True, timeout=120, check=False
        )
    finally:
        lock.cleanup()
    assert completed.returncode != 0, (completed.stdout, completed.stderr)
    assert "1 passed" not in completed.stdout


def test_ci_loads_the_oracle_lock_from_main_not_the_checkout():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "git show origin/main:bundlebees_plugin/oracle_lock.py" in workflow
    assert "bootstrap exception" not in workflow
    assert "cp bundlebees_plugin/oracle_lock.py" not in workflow
    assert "python -m bundlebees_plugin.oracle_lock" not in workflow
    assert "python -P" in workflow


def test_harness_owned_tool_dir_is_refused_by_name():
    from bundlebees_plugin import verify_local

    assert verify_local._harness_owned_paths([".bundlebees-tools/uv"]) == [
        ".bundlebees-tools/uv"
    ]
    assert verify_local._harness_owned_paths(["pkg/.bundlebees-tools/pwsh/x"]) == [
        "pkg/.bundlebees-tools/pwsh/x"
    ]
    assert verify_local._harness_owned_paths(["src/app.py", "tests/test_app.py"]) == []


def test_shard_tests_loads_the_main_lock_with_its_dataclasses():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "shard_tests_under_test", root / ".github" / "scripts" / "shard_tests.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock = module._load_main_oracle_lock("HEAD")
    assert lock.CONFIG_NAME == ".bundlebees-oracle-pytest.ini"


def test_test_evidence_carries_staged_tools():
    from bundlebees_plugin import verify_local

    tools = {"uv": "a" * 64, "pwsh": "b" * 64}
    out = verify_local._test_evidence({"exit_code": 0, "staged_tools": tools})
    assert out["staged_tools"] == tools


def test_shard_loader_fails_closed_when_main_has_no_lock():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "shard_tests_fail_closed", root / ".github" / "scripts" / "shard_tests.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        module._load_main_oracle_lock("refs/heads/no-such-ref")
    except SystemExit as exc:
        assert "refusing to use the checkout's copy" in str(exc)
    else:
        raise AssertionError("loader fell back to the checkout copy instead of failing closed")


def test_receipt_evidence_carries_staged_tools():
    from bundlebees_plugin import verify_local

    tools = {"uv": "a" * 64}
    out = verify_local._receipt_evidence(
        {"key": "k", "result": {"exit_code": 0, "timed_out": False, "tail": "", "staged_tools": tools}}
    )
    assert out["staged_tools"] == tools


def test_receipt_key_identity_hashes_the_pinned_tools(tmp_path, monkeypatch):
    import hashlib

    from bundlebees_plugin import sandbox, verify_local

    tool = tmp_path / "uv"
    tool.write_bytes(b"uv-bytes")
    tool.chmod(0o644)
    monkeypatch.setattr(sandbox, "PINNED_TOOLS", (("uv", str(tool)),))
    assert verify_local._pinned_tools_identity() == {
        "uv": hashlib.sha256(b"uv-bytes").hexdigest()
    }


def test_pinned_tool_identity_resolves_links_and_caches(tmp_path, monkeypatch):
    import hashlib

    from bundlebees_plugin import sandbox, verify_local

    target = tmp_path / "uv-real"
    target.write_bytes(b"uv-bytes")
    target.chmod(0o644)
    link = tmp_path / "uv"
    link.symlink_to(target)
    monkeypatch.setattr(sandbox, "PINNED_TOOLS", (("uv", str(link)),))
    verify_local._TOOL_IDENTITY_CACHE.clear()
    calls = {"n": 0}
    real = sandbox._staged_digest

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(sandbox, "_staged_digest", counting)
    first = verify_local._pinned_tools_identity()
    second = verify_local._pinned_tools_identity()

    assert first == second == {"uv": hashlib.sha256(b"uv-bytes").hexdigest()}
    assert calls["n"] == 1


def test_record_carries_staged_tools():
    from bundlebees_plugin import verify_local

    tools = {"uv": "a" * 64}
    record = verify_local._record(
        "att_x",
        "sha",
        "base",
        ["f"],
        ["t"],
        red={"exit_code": 1, "timed_out": False, "staged_tools": tools},
        suite={"exit_code": 0, "timed_out": False, "staged_tools": tools},
        gitleaks=None,
    )
    assert record["red"]["staged_tools"] == tools
    assert record["suite"]["staged_tools"] == tools


def test_verification_identity_includes_pinned_tools(tmp_path, monkeypatch):
    from bundlebees_plugin import verify_local

    (tmp_path / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    kwargs = dict(
        base_rev="0" * 40,
        source_tree_sha256="a" * 64,
        patch_sha256="b" * 64,
        tests=["test_x.py"],
        suite_command=["python", "-m", "pytest"],
        runner=None,
        backend="bwrap",
        allow_unsafe_local=False,
        allow_weak=False,
        timeout=60,
        require_gitleaks=False,
        gitleaks=None,
    )
    monkeypatch.setattr(verify_local, "_pinned_tools_identity", lambda: {"uv": "a" * 64})
    first = verify_local._verification_identity(tmp_path, **kwargs)
    monkeypatch.setattr(verify_local, "_pinned_tools_identity", lambda: {"uv": "b" * 64})
    second = verify_local._verification_identity(tmp_path, **kwargs)

    assert first["pinned_tools"] == {"uv": "a" * 64}
    assert first != second


def test_landed_evidence_and_record_carry_staged_tools(tmp_path):
    from bundlebees_plugin import verify_local

    tools = {"uv": "c" * 64}
    evidence = verify_local._landed_evidence(
        tmp_path,
        ["true"],
        runner_name="pytest",
        runner=lambda tree, argv: {
            "exit_code": 0,
            "junit": {"tests": 1},
            "staged_tools": tools,
        },
        backend="bwrap",
        allow_unsafe_local=False,
        allow_weak=False,
        timeout=5,
    )
    assert evidence["staged_tools"] == tools

    record = verify_local._landed_record(
        "sha",
        ["test_x.py"],
        evidence,
        runner=None,
        backend="bwrap",
        timeout=5,
        now="2026-11-27T00:00:00Z",
    )
    assert record["sandbox"]["staged_tools"] == tools


def test_pinned_identity_excludes_a_shared_writable_pin(tmp_path, monkeypatch):
    from bundlebees_plugin import sandbox, verify_local

    tool = tmp_path / "uv"
    tool.write_bytes(b"uv")
    tool.chmod(0o666)
    monkeypatch.setattr(sandbox, "PINNED_TOOLS", (("uv", str(tool)),))
    verify_local._TOOL_IDENTITY_CACHE.clear()

    assert verify_local._pinned_tools_identity() == {}
