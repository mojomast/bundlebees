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
    lock = oracle.from_git(repo, sha)
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
