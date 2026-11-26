"""The oracle lock: run pytest with main's configuration, never the PR's.

A pull request owns its working tree, so it owns ``pyproject.toml``,
``pytest.ini``, ``tox.ini`` and ``setup.cfg`` as well.  Those files decide
which plugins pytest loads (``addopts = "-p evil"``, ``pytest_plugins``, a
``pytest11`` entry point) and what pytest collects.  A test job that reads the
PR's configuration is therefore not an independent oracle: the change under
test can neuter the tests that judge it.

The lock reads the *main* revision's configuration instead.  It writes that
pytest section to a temporary ini file and runs pytest with

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTEST_ADDOPTS= \
        pytest -c <main_config> --rootdir <repo> -p <each allowlisted plugin>

The plugin allowlist (``addopts`` ``-p`` options plus ``pytest_plugins``) is
read from main too.  Because ``-c`` pins the configuration file, pytest never
looks at the PR's ``pyproject.toml``/``pytest.ini``/``setup.cfg``; because
plugin autoload is disabled, a PR's ``project.entry-points.pytest11`` cannot
register a plugin either.  An inline table, a dotted key and a table header
spelling of the same configuration all have no effect.

The scanner in :mod:`bundlebees_plugin.subversion_scan` remains in place as a
tripwire that names the subversion, but this module is the boundary: even a
configuration shape the scanner misses cannot change the oracle.
"""

from __future__ import annotations

import configparser
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

#: Written into the verified worktree and copied into the sandbox with it.
CONFIG_NAME = ".bundlebees-oracle-pytest.ini"

#: Exactly the two variables pytest reads before it reads any config file.
LOCK_ENV = {
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTEST_ADDOPTS": "",
}

_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_OPTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_PYTEST_NAMES = frozenset({"pytest", "pytest.exe"})
_OPTIONS_WITH_VALUE = frozenset(
    {"-c", "--config-file", "--rootdir", "-p", "--plugin", "-o", "--override-ini"}
)
_LONG_OVERRIDES = (
    "--config-file=",
    "--rootdir=",
    "--plugin=",
    "--override-ini=",
)


class OracleLockError(Exception):
    """The main configuration cannot be read; the lock must not fail open."""


@dataclass(frozen=True)
class OracleLock:
    """A main-config ini file plus the plugins main allows pytest to load."""

    config_name: str
    config_path: Path
    plugins: tuple[str, ...]
    main_ref: str

    def env(self) -> dict[str, str]:
        """The two pytest variables the lock pins, ready for a sandbox overlay."""
        return dict(LOCK_ENV)

    def lock_argv(self, argv: Sequence[str], *, rootdir: str | Path) -> list[str]:
        return locked_argv(self, argv, rootdir=rootdir)

    def cleanup(self) -> None:
        try:
            self.config_path.unlink()
        except OSError:
            pass


def _git_show(repo: Path, ref: str, name: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{name}"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _toml_options(text: str) -> Mapping[str, Any] | None:
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    options = document.get("tool", {}).get("pytest", {}).get("ini_options")
    return options if isinstance(options, Mapping) else None


def _ini_options(text: str, section: str) -> Mapping[str, str] | None:
    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section(section):
        return None
    return {name: parser.get(section, name, raw=True) for name in parser.options(section)}


def _main_options(repo: Path, ref: str) -> tuple[str | None, Mapping[str, Any]]:
    """Main's pytest options, in pytest's own config-file precedence order."""
    candidates = (
        ("pytest.ini", "pytest"),
        ("pyproject.toml", None),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    )
    for name, section in candidates:
        text = _git_show(repo, ref, name)
        if text is None:
            continue
        options = (
            _toml_options(text) if section is None else _ini_options(text, section)
        )
        if options is not None:
            return name, options
    return None, {}


def _as_items(value: Any) -> Iterable[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _addopts_tokens(value: Any) -> list[str]:
    tokens: list[str] = []
    for item in _as_items(value):
        try:
            tokens.extend(shlex.split(item, comments=False, posix=True))
        except ValueError:
            tokens.extend(item.split())
    return tokens


def _add_plugin_name(names: list[str], name: str) -> None:
    name = name.strip()
    if not name or name.startswith("no:") or not _PLUGIN_NAME_RE.match(name):
        return
    if name not in names:
        names.append(name)


def _option_plugin(tokens: Sequence[str], index: int) -> tuple[str | None, int]:
    """The plugin named by the token at *index*, and the index after it."""
    token = tokens[index]
    if token in {"-p", "--plugin"}:
        if index + 1 < len(tokens):
            return tokens[index + 1], index + 2
        return None, index + 1
    if token.startswith("--plugin="):
        return token[len("--plugin=") :], index + 1
    if token.startswith("-p") and not token.startswith("--") and len(token) > 2:
        return token[2:], index + 1
    return None, index + 1


def _plugin_allowlist(options: Mapping[str, Any]) -> tuple[str, ...]:
    """The ``-p`` args plus ``pytest_plugins`` *main* itself loads."""
    names: list[str] = []
    tokens = _addopts_tokens(options.get("addopts"))
    index = 0
    while index < len(tokens):
        candidate, index = _option_plugin(tokens, index)
        if candidate is not None:
            _add_plugin_name(names, candidate)
    for item in _as_items(options.get("pytest_plugins")):
        for part in re.split(r"[,\s]+", item):
            _add_plugin_name(names, part)
    return tuple(names)


def _ini_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return ""
        return "\n" + "\n".join(f"    {str(item)}" for item in value)
    return str(value).replace("\r\n", "\n")


def _strip_plugin_options(value: Any) -> list[str]:
    """Drop ``-p``/``--plugin`` tokens; the lock re-adds main's allowlist."""
    cleaned: list[str] = []
    for item in _as_items(value):
        tokens = _addopts_tokens(item)
        kept: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in {"-p", "--plugin"}:
                index += 2
                continue
            if token.startswith("--plugin=") or (
                token.startswith("-p") and not token.startswith("--") and len(token) > 2
            ):
                index += 1
                continue
            kept.append(token)
            index += 1
        cleaned.append(shlex.join(kept))
    return cleaned


def _ini_text(options: Mapping[str, Any]) -> str:
    lines = ["[pytest]"]
    for key, value in options.items():
        name = str(key)
        if not _OPTION_NAME_RE.match(name):
            continue
        if name == "pytest_plugins":
            # Main's plugin list is re-added explicitly as -p; an ini entry is
            # not a portable pytest option and would double-load the plugin.
            continue
        if name == "addopts":
            cleaned = _strip_plugin_options(value)
            value = " ".join(cleaned) if isinstance(value, str) else cleaned
        text = _ini_value(value)
        if isinstance(value, str) and "\n" in text:
            # Keep ini multiline values (markers, filterwarnings) as continuation
            # lines instead of joining them into one ambiguous value.
            text = "\n" + "\n".join(
                f"    {part}" for part in text.splitlines() if part.strip()
            )
        lines.append(f"{name} = {text}" if text else f"{name} =")
    return "\n".join(lines) + "\n"


def main_config(repo: str | Path, ref: str) -> tuple[str, tuple[str, ...]]:
    """Render main's pytest configuration and its plugin allowlist.

    Only the ini text and plugin names are returned; the PR tree is never
    consulted.  When main has no pytest section at all the result is an empty
    ``[pytest]`` file, which still prevents pytest from reading the PR's
    configuration.
    """
    repo = Path(repo)
    name, options = _main_options(repo, str(ref))
    return _ini_text(options), _plugin_allowlist(options)


def from_git(
    repo: str | Path, ref: str, *, dest: str | Path | None = None
) -> OracleLock:
    """Extract main *ref*'s config into *dest* and return the lock."""
    repo = Path(repo)
    try:
        text, plugins = main_config(repo, str(ref))
    except (OSError, ValueError, TypeError) as exc:
        raise OracleLockError(f"cannot read pytest configuration from {ref}: {exc}") from exc
    if dest is None:
        target = repo / CONFIG_NAME
    else:
        target = Path(dest)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise OracleLockError(f"cannot write the main pytest config: {exc}") from exc
    try:
        name = target.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError):
        name = str(target)
    return OracleLock(
        config_name=name,
        config_path=target,
        plugins=tuple(plugins),
        main_ref=str(ref),
    )


def locked_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A child environment that disables plugin autoload and PYTEST_ADDOPTS."""
    env = dict(os.environ if environ is None else environ)
    env.update(LOCK_ENV)
    return env


def _pytest_tail(tokens: Sequence[str]) -> tuple[list[str], list[str]] | None:
    """Split ``argv`` where pytest starts: ``(prefix, args after pytest)``."""
    for index, token in enumerate(tokens):
        if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] in _PYTEST_NAMES:
            return list(tokens[:index]), list(tokens[index + 2 :])
        if token in _PYTEST_NAMES or Path(token).name in _PYTEST_NAMES:
            return list(tokens[:index]), list(tokens[index + 1 :])
    return None


def _strip_config_overrides(tail: Sequence[str]) -> list[str]:
    """Drop options that would let the command override the locked config."""
    kept: list[str] = []
    index = 0
    while index < len(tail):
        token = tail[index]
        if token in _OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(token.startswith(prefix) for prefix in _LONG_OVERRIDES):
            index += 1
            continue
        if token.startswith("-p") and not token.startswith("--") and len(token) > 2:
            index += 1
            continue
        kept.append(token)
        index += 1
    return kept


def _interpreter_prefix(prefix: Sequence[str]) -> tuple[list[str], str]:
    """Keep wrappers (env/nice/timeout) but name a real interpreter.

    ``[python, -B, -m, pytest]`` puts ``-B`` between the interpreter and
    ``-m``; those interpreter flags are re-added by the locked argv, so they
    are dropped here instead of being mistaken for wrappers.
    """
    for index in range(len(prefix) - 1, -1, -1):
        name = Path(prefix[index]).name
        if "python" in name or name.startswith("pypy"):
            interpreter = str(prefix[index])
            if not os.path.isabs(interpreter):
                interpreter = sys.executable
            return list(prefix[:index]), interpreter
        if not prefix[index].startswith("-"):
            break
    return list(prefix), sys.executable


def locked_argv(
    lock: OracleLock, argv: Sequence[str], *, rootdir: str | Path = "."
) -> list[str]:
    """Rewrite a pytest invocation to use main's config and allowlist.

    A command that is not pytest is returned unchanged (verify also runs node
    suites and arbitrary contract commands).  ``--rootdir .`` is deliberate:
    the command is always executed with its cwd at the repository root (the
    sandbox's copy, or the CI checkout), and a stable relative rootdir keeps
    identical requests identical for the receipt ledger instead of differing
    by their per-attempt worktree path.
    """
    del rootdir  # the contract is "run with cwd at the repository root"
    tokens = [str(token) for token in argv]
    split = _pytest_tail(tokens)
    if split is None:
        return tokens
    prefix, tail = split
    wrappers, interpreter = _interpreter_prefix(prefix)
    return [
        *wrappers,
        interpreter,
        "-B",
        "-m",
        "pytest",
        "-c",
        lock.config_name,
        "--rootdir",
        ".",
        *[argument for plugin in lock.plugins for argument in ("-p", plugin)],
        "-p",
        "no:cacheprovider",
        *_strip_config_overrides(tail),
    ]


def run(
    lock: OracleLock,
    argv: Sequence[str],
    *,
    rootdir: str | Path,
    cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command under the lock without a shell."""
    command = locked_argv(lock, argv, rootdir=rootdir)
    return subprocess.run(
        command,
        cwd=str(cwd if cwd is not None else rootdir),
        env=locked_env(environ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _cli(argv: Sequence[str]) -> int:
    args = list(argv)
    main_ref = "origin/main"
    rootdir = Path.cwd()
    config_out: Path | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token in {"--main-ref", "--rootdir", "--config-out"}:
            if index + 1 >= len(args):
                raise SystemExit(f"{token} needs a value")
            value = args[index + 1]
            if token == "--main-ref":
                main_ref = value
            elif token == "--rootdir":
                rootdir = Path(value)
            else:
                config_out = Path(value)
            index += 2
            continue
        raise SystemExit(f"unknown argument: {token}")
    passthrough = args[index:]
    repo = Path.cwd().resolve()
    lock = from_git(repo, main_ref, dest=config_out)
    command = locked_argv(
        lock, [sys.executable, "-B", "-m", "pytest", *passthrough], rootdir=rootdir
    )
    os.chdir(Path(rootdir).resolve())
    os.execvpe(command[0], command, locked_env())


def main() -> int:
    """``python -m bundlebees_plugin.oracle_lock --main-ref REF -- pytest ...``."""
    _cli(sys.argv[1:])
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CI job
    raise SystemExit(main())
