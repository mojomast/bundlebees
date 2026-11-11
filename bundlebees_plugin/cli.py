"""User-run ``hermes bundlebees`` CLI: verify worker results and deploy a host.

These commands are deliberately *not* model tools. ``verify`` fetches one
worker result, applies its patch to a clean feature-branch worktree, enforces
the attempt's file lease, runs only the named tests plus a secret scan, and on
green commits and pushes the feature branch (never ``main``). ``deploy`` prints
the host setup prompt by default and only reaches a host when the user passes
``--execute`` with an alias already stored in plugin config.

All child processes receive ``security.child_env`` rather than the ambient
environment. Nothing here reads credentials from the environment; the optional
``doctor`` command asks the client's secret-scope helper whether a token is
configured and never prints its value.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from .security import child_env, contain_path, looks_like_secret, scrub

TOKEN_SECRET_NAME = "BUNDLEBEES_LAUNCHER_TOKEN"
BRANCH_BUDGET_NOTE = "no merge, tag, force-push, or hook skip"

_MAIN_BRANCHES = {"main", "master"}
_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_MAX_SCAN_BYTES = 2_000_000
_MAX_TEST_OUTPUT = 4000

_DEPLOY_STEPS = (
    "1. Print or copy the host setup prompt; run it on the worker host.",
    "2. Configure the launcher token and provider key in the host secret store.",
    "3. Start the launcher from its documented path; never restart a busy launcher.",
    "4. Return a health summary that says configured/not configured, never values.",
    "5. Store a host alias in bundlebees plugin settings if you want an SSH deploy path.",
)

_SSH_SCRIPT = (
    "set -eu\n"
    "# bundlebees host deploy (user-run). Apply the host setup prompt on this host.\n"
    "echo 'bundlebees: host setup prompt is ready on the coordinator'\n"
)


class CliError(Exception):
    """A user-facing CLI failure with a stable code and remediation."""

    def __init__(self, code: str, error: str, fix: str) -> None:
        self.code = code
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix)))
        super().__init__(self.error)


def _now() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _print(message: str = "") -> None:
    print(scrub(message))


def _print_sha(sha: str | None) -> None:
    """Print a validated commit digest without passing it through scrub()."""
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
        print(f"sha: {sha}")
    else:
        print("sha: unavailable")


def _print_error(exc: CliError) -> None:
    print(f"bundlebees: error ({exc.code}): {scrub(exc.error)}")
    print(f"fix: {scrub(exc.fix)}")


def _tail(text: Any, limit: int = _MAX_TEST_OUTPUT) -> str:
    value = str(text or "")
    value = value.replace("\x00", "")
    if len(value) > limit:
        value = value[-limit:]
    return str(scrub(value)).strip()


# ---------------------------------------------------------------------------
# Registration and argparse
# ---------------------------------------------------------------------------


def register_cli(ctx: Any) -> None:
    """Register ``hermes bundlebees`` with a fake-ctx-compatible setup/handler."""

    def setup(parser: Any) -> None:
        _build_parser(parser)

    def handler(args: Any) -> int:
        return _dispatch(ctx, args)

    kwargs = {
        "name": "bundlebees",
        "help": "Verify a bundlebees worker result or deploy a worker host (user-run).",
        "setup_fn": setup,
        "handler_fn": handler,
        "description": (
            "Verify applies a worker patch to a clean feature branch, runs only the "
            "named tests plus a secret scan, then pushes that feature branch. Deploy "
            "prints host setup steps and only uses a stored SSH host alias with --execute."
        ),
    }
    try:
        ctx.register_cli_command(**kwargs)
    except TypeError:
        kwargs.pop("description", None)
        ctx.register_cli_command(**kwargs)


def _build_parser(parser: Any) -> Any:
    if hasattr(parser, "add_parser"):
        group = parser.add_parser(
            "bundlebees",
            help="Verify worker results or deploy a worker host (user-run).",
        )
    else:
        group = parser

    subparsers = group.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser(
        "verify",
        help="Apply a worker patch, test it, and push the feature branch.",
    )
    verify.add_argument("attempt_id", help="Worker attempt id returned by dispatch.")
    verify.add_argument(
        "--workdir",
        default=None,
        help="Clean git worktree to use. Defaults to the configured worktree.",
    )
    verify.add_argument(
        "--branch",
        default=None,
        help="Feature branch to check out and push. main/master are refused.",
    )
    verify.add_argument(
        "--tests",
        nargs="+",
        default=None,
        help="Test targets to run. Defaults to the brief's named tests.",
    )
    verify.add_argument(
        "--dry-run",
        action="store_true",
        help="Apply and test without committing or pushing.",
    )
    verify.add_argument(
        "--kanban-comment",
        action="store_true",
        help="Also post a comment through the public hermes kanban CLI.",
    )
    verify.add_argument(
        "--state-dir",
        default=None,
        help="Plugin data directory for verify-results (defaults to Hermes plugin data).",
    )
    verify.set_defaults(command="verify")

    deploy = subparsers.add_parser(
        "deploy",
        help="Print host setup steps; execute a stored host alias only with --execute.",
    )
    deploy.add_argument(
        "--execute",
        action="store_true",
        help="Use the stored host alias. No host or key may be passed on argv.",
    )
    deploy.set_defaults(command="deploy")

    doctor = subparsers.add_parser(
        "doctor",
        help="Report token configured/not, adapter reachability, and schema size.",
    )
    doctor.set_defaults(command="doctor")

    return group


def _dispatch(ctx: Any, args: Any) -> int:
    command = getattr(args, "command", None)
    try:
        if command == "verify":
            return _cmd_verify(ctx, args)
        if command == "deploy":
            return _cmd_deploy(ctx, args)
        if command == "doctor":
            return _cmd_doctor(ctx, args)
        raise CliError(
            "invalid_argument",
            "unknown bundlebees command",
            "Use one of: verify, deploy, doctor.",
        )
    except CliError as exc:
        _print_error(exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary must answer
        _print_error(
            CliError(
                "internal_error",
                str(exc) or "unexpected error",
                "Retry once; if it persists, check the worktree and Hermes logs.",
            )
        )
        return 1


# ---------------------------------------------------------------------------
# Config / data-dir helpers (never print values)
# ---------------------------------------------------------------------------


def _config(ctx: Any) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if callable(config):
        try:
            config = config()
        except Exception:
            config = None
    if isinstance(config, Mapping):
        return dict(config)
    getter = getattr(ctx, "get_config", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _state_dir(ctx: Any, args: Any) -> Path:
    explicit = getattr(args, "state_dir", None)
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    for name in ("plugin_data_dir", "get_plugin_data_dir"):
        getter = getattr(ctx, name, None)
        value: Any = None
        if callable(getter):
            try:
                value = getter("bundlebees")
            except TypeError:
                try:
                    value = getter()
                except Exception:
                    value = None
            except Exception:
                value = None
        elif getter:
            value = getter
        if value:
            return Path(str(value)).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "hermes" / "bundlebees").resolve()


def _client_for(ctx: Any) -> Any:
    injected = getattr(ctx, "bundlebees_client", None)
    if injected is not None:
        return injected
    try:
        from .client import client_from_agent
    except ImportError as exc:  # pragma: no cover - skeleton without client.py
        raise CliError(
            "not_configured",
            "the bundlebees launcher client is not available",
            "Install the full bundlebees plugin and configure the launcher endpoint.",
        ) from exc
    agent = getattr(ctx, "agent", None)
    try:
        return client_from_agent(agent, _config(ctx))
    except Exception as exc:  # noqa: BLE001 - map client configuration errors
        code = getattr(exc, "code", "not_configured")
        fix = getattr(
            exc,
            "fix",
            "Configure the launcher endpoint in bundlebees plugin settings and retry.",
        )
        raise CliError(str(code), str(exc), str(fix)) from exc


def _token_configured(ctx: Any) -> bool:
    try:
        from .client import token_from_agent
    except ImportError:
        agent = getattr(ctx, "agent", None)
        scope = getattr(agent, "secret_scope", None)
        getter = getattr(scope, "get_secret", None)
        if not callable(getter):
            return False
        try:
            return bool(getter(TOKEN_SECRET_NAME))
        except Exception:
            return False
    try:
        return bool(token_from_agent(getattr(ctx, "agent", None)))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Attempt address / lease / patch validation
# ---------------------------------------------------------------------------


def _safe_attempt_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _ATTEMPT_ID_RE.match(text) or ".." in text:
        raise CliError(
            "invalid_argument",
            "attempt id is not a safe file name",
            "Pass the attempt id exactly as returned by bundlebees_dispatch.",
        )
    return text


def _safe_branch(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("refs/heads/"):
        text = text[len("refs/heads/") :]
    if not text or not _BRANCH_RE.match(text):
        raise CliError(
            "invalid_argument",
            "branch name is missing or contains unsupported characters",
            "Pass a feature branch name such as feat/v0.1.0.",
        )
    if ".." in text or text.endswith("/") or text.endswith(".") or "//" in text:
        raise CliError(
            "invalid_argument",
            "branch name is not a valid git branch",
            "Pass a feature branch name such as feat/v0.1.0.",
        )
    if text.casefold() in _MAIN_BRANCHES:
        raise CliError(
            "main_not_allowed",
            f"refusing to use {text} as the verify push target",
            "Pass the attempt's feature branch; verify never pushes main or master.",
        )
    return text


def _rel_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or "\x00" in text:
        raise CliError(
            "invalid_lease",
            "assigned file path is empty",
            "Use relative repository paths in assigned_files.",
        )
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise CliError(
            "scope_violation",
            f"assigned file path must be relative: {text}",
            "Use repository-relative paths in the brief's assigned_files.",
        )
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise CliError(
            "scope_violation",
            f"assigned file path escapes the worktree: {text}",
            "Use repository-relative paths in the brief's assigned_files.",
        )
    if parts[0] == ".git":
        raise CliError(
            "scope_violation",
            "assigned files may not include .git",
            "Lease source files, not git metadata.",
        )
    return "/".join(parts)


def _assigned_files(values: Any, workdir: Path) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or not values:
        raise CliError(
            "invalid_lease",
            "the attempt has no assigned_files lease",
            "Code-work briefs must list the exact files the worker may change.",
        )
    files: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise CliError(
                "invalid_lease",
                "assigned_files entries must be strings",
                "Use repository-relative file paths.",
            )
        rel = _rel_path(item)
        try:
            contain_path(rel, str(workdir))
        except ValueError as exc:
            raise CliError(
                "scope_violation",
                f"assigned file escapes the worktree: {rel}",
                "Use repository-relative paths inside the configured worktree.",
            ) from exc
        if rel not in files:
            files.append(rel)
    return files


def _patch_files(patch: str) -> list[str]:
    found: list[str] = []
    for line in patch.splitlines():
        candidate: str | None = None
        if line.startswith("+++ "):
            candidate = line[4:]
        elif line.startswith("--- "):
            candidate = line[4:]
        elif line.startswith("rename to "):
            candidate = line[len("rename to ") :]
        elif line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+) b/(.+)$", line)
            if match:
                candidate = match.group(2)
        if not candidate:
            continue
        candidate = candidate.split("\t", 1)[0].strip()
        if candidate.startswith('"') and candidate.endswith('"'):
            candidate = candidate[1:-1]
        if candidate == "/dev/null":
            continue
        if candidate.startswith("a/") or candidate.startswith("b/"):
            candidate = candidate[2:]
        rel = _rel_path(candidate)
        if rel not in found:
            found.append(rel)
    return found


def _test_targets(value: Any, workdir: Path) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not value:
        return []
    targets: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        target = item.strip()
        if not target or "\x00" in target or "\n" in target:
            continue
        if target.startswith("-"):
            raise CliError(
                "invalid_argument",
                "test targets may not be command-line options",
                "Pass file or node-id test targets, not flags.",
            )
        path_part = target.split("::", 1)[0] or "."
        try:
            contain_path(path_part, str(workdir))
        except ValueError as exc:
            raise CliError(
                "scope_violation",
                f"test target escapes the worktree: {target}",
                "Use test paths inside the configured worktree.",
            ) from exc
        if target not in targets:
            targets.append(target)
    return targets


def _tests_from_commands(commands: Any, workdir: Path) -> list[str]:
    if not isinstance(commands, (list, tuple)):
        return []
    targets: list[str] = []
    for command in commands:
        if not isinstance(command, str) or "pytest" not in command:
            continue
        try:
            parts = shlex.split(command)
        except ValueError:
            continue
        start = None
        for index, part in enumerate(parts):
            if part == "pytest" or part.endswith("/pytest"):
                start = index + 1
                break
        if start is None:
            continue
        skip_next = False
        for part in parts[start:]:
            if skip_next:
                skip_next = False
                continue
            if part in ("-k", "-m", "-p", "-c", "--rootdir", "--ignore"):
                skip_next = True
                continue
            if part.startswith("-") or part in (".", "&&", ";", "|"):
                continue
            if part.endswith(".py") or "::" in part:
                try:
                    targets.extend(_test_targets([part], workdir))
                except CliError:
                    continue
    return targets


def _result_patch(result: Mapping[str, Any]) -> str | None:
    def find(value: Any, depth: int = 0) -> str | None:
        if depth > 3:
            return None
        if isinstance(value, Mapping):
            for key in ("patch", "unified_diff", "diff"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate
            for key in ("result", "data", "output", "observed"):
                nested = value.get(key)
                if isinstance(nested, Mapping):
                    found = find(nested, depth + 1)
                    if found:
                        return found
        return None

    return find(result)


def _fetch_result(client: Any, attempt_id: str) -> dict[str, Any]:
    try:
        result = client.result(attempt_id)
    except Exception as exc:  # noqa: BLE001 - ClientError or fake error
        code = getattr(exc, "code", "launcher_error")
        fix = getattr(exc, "fix", "Check the launcher and retry when the worker finishes.")
        raise CliError(str(code), str(exc), str(fix)) from exc
    if not isinstance(result, Mapping):
        raise CliError(
            "no_result_yet",
            "the launcher did not return a result object",
            "Wait for the worker to finish, then retry verify.",
        )
    data = dict(result)
    for key in ("result", "data"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            merged = dict(nested)
            for extra_key, extra_value in data.items():
                merged.setdefault(extra_key, extra_value)
            data = merged
            break
    if _result_patch(data):
        return data
    raw_getter = getattr(client, "get_raw", None)
    if callable(raw_getter):
        try:
            raw = raw_getter(attempt_id)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", "launcher_error")
            fix = getattr(exc, "fix", "Check the launcher and retry.")
            raise CliError(str(code), str(exc), str(fix)) from exc
        if isinstance(raw, Mapping):
            merged = dict(raw)
            patch = _result_patch(raw)
            if patch:
                merged["patch"] = patch
            for key in ("brief", "tests", "kanban_task_id", "branch", "assigned_files"):
                if key not in merged and key in data:
                    merged[key] = data[key]
            data = merged
    return data


def _brief_from(result: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("brief", "request"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = None
        if isinstance(value, Mapping):
            brief = value.get("brief") if key == "request" else value
            if isinstance(brief, str):
                try:
                    brief = json.loads(brief)
                except json.JSONDecodeError:
                    brief = None
            if isinstance(brief, Mapping):
                return dict(brief)
    return {}


# ---------------------------------------------------------------------------
# Git / process runners (minimal child environment)
# ---------------------------------------------------------------------------


def _run_subprocess(
    command: Iterable[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else child_env(),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_git(
    args: Iterable[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    check: bool = True,
    env_extra: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    command = ["git", *list(args)]
    try:
        extra = {"GIT_TERMINAL_PROMPT": "0"}
        if env_extra:
            extra.update(env_extra)
        proc = _run_subprocess(
            command,
            cwd=cwd,
            env=child_env(extra),
            input_text=input_text,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(
            "git_failed",
            f"could not run git: {exc}",
            "Confirm git is installed and the worktree is readable.",
        ) from exc
    if check and proc.returncode != 0:
        raise CliError(
            "git_failed",
            f"git {next(iter(args), '')} failed: {_tail(proc.stderr)}",
            "Check the worktree state and retry; verify never force-pushes or skips hooks.",
        )
    return proc


def _prepare_worktree(workdir_value: Any, branch: str) -> Path:
    if not workdir_value:
        raise CliError(
            "not_configured",
            "no verify worktree is configured",
            "Pass --workdir or set a worktree in bundlebees plugin settings.",
        )
    workdir = Path(str(workdir_value)).expanduser().resolve()
    if not workdir.is_dir():
        raise CliError(
            "invalid_argument",
            "verify worktree is not a directory",
            "Pass --workdir pointing at a clean git worktree.",
        )
    top = _run_git(["rev-parse", "--show-toplevel"], workdir, check=False)
    if top.returncode != 0:
        raise CliError(
            "invalid_argument",
            "verify worktree is not a git repository",
            "Pass --workdir pointing at a clean git worktree.",
        )
    try:
        if Path(top.stdout.strip()).resolve() != workdir:
            raise CliError(
                "invalid_argument",
                "verify worktree must be the repository root",
                "Pass --workdir pointing at the repository root.",
            )
    except OSError:
        raise CliError(
            "invalid_argument",
            "verify worktree path could not be resolved",
            "Pass --workdir pointing at the repository root.",
        ) from None

    status = _run_git(["status", "--porcelain"], workdir)
    if status.stdout.strip():
        raise CliError(
            "worktree_dirty",
            "verify worktree has uncommitted changes",
            "Commit or stash local changes, then retry with a clean worktree.",
        )

    exists = _run_git(
        ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        workdir,
        check=False,
    )
    if exists.returncode != 0:
        remote_exists = _run_git(
            ["rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}"],
            workdir,
            check=False,
        )
        if remote_exists.returncode != 0:
            raise CliError(
                "branch_not_found",
                f"feature branch {branch} does not exist locally or on origin",
                "Fetch the feature branch or pass --branch with an existing local branch.",
            )
        _run_git(
            ["checkout", "--quiet", "-b", branch, "--track", f"origin/{branch}"],
            workdir,
        )

    current = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], workdir).stdout.strip()
    if current != branch:
        _run_git(["checkout", "--quiet", branch], workdir)
        current = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], workdir).stdout.strip()
    if current != branch:
        raise CliError(
            "checkout_failed",
            "could not check out the feature branch",
            "Check the worktree state and retry.",
        )
    if current.casefold() in _MAIN_BRANCHES:
        raise CliError(
            "main_not_allowed",
            f"refusing to verify on {current}",
            "Check out the attempt's feature branch; verify never pushes main or master.",
        )
    status = _run_git(["status", "--porcelain"], workdir)
    if status.stdout.strip():
        raise CliError(
            "worktree_dirty",
            "feature branch worktree is not clean",
            "Commit or stash local changes, then retry.",
        )
    return workdir


def _changed_files(workdir: Path) -> list[str]:
    tracked = _run_git(["diff", "--name-only"], workdir).stdout.splitlines()
    other = _run_git(["ls-files", "--others", "--exclude-standard"], workdir).stdout.splitlines()
    files = []
    for item in [*tracked, *other]:
        text = item.strip()
        if text and text not in files:
            files.append(text)
    return sorted(files)


def _reset_patch(workdir: Path) -> None:
    # The worktree was clean before git apply, so only the patch's changes exist.
    _run_git(["reset", "--hard", "HEAD"], workdir, check=False)
    _run_git(["clean", "-fd"], workdir, check=False)


def _apply_patch(workdir: Path, patch: str, assigned: list[str]) -> list[str]:
    patch_files = _patch_files(patch)
    if not patch_files:
        raise CliError(
            "invalid_patch",
            "the worker result has no applicable patch",
            "Collect a result with a unified diff before running verify.",
        )
    outside = sorted(set(patch_files) - set(assigned))
    if outside:
        raise CliError(
            "scope_violation",
            "patch changes files outside assigned_files: " + ", ".join(outside),
            "Reject this worker result or amend the brief lease; nothing was applied.",
        )
    check = _run_git(
        ["apply", "--check", "-"], workdir, input_text=patch, check=False
    )
    if check.returncode != 0:
        raise CliError(
            "patch_failed",
            "the worker patch does not apply cleanly: " + _tail(check.stderr),
            "Collect a patch against the current feature branch and retry.",
        )
    applied = _run_git(["apply", "-"], workdir, input_text=patch, check=False)
    if applied.returncode != 0:
        _reset_patch(workdir)
        raise CliError(
            "patch_failed",
            "the worker patch could not be applied: " + _tail(applied.stderr),
            "Collect a patch against the current feature branch and retry.",
        )
    changed = _changed_files(workdir)
    if not changed:
        _reset_patch(workdir)
        raise CliError(
            "invalid_patch",
            "the worker patch produced no changes",
            "Collect a result with a non-empty diff before running verify.",
        )
    outside = sorted(set(changed) - set(assigned))
    if outside:
        _reset_patch(workdir)
        raise CliError(
            "scope_violation",
            "applied patch changed files outside assigned_files: " + ", ".join(outside),
            "Reject this worker result or amend the brief lease; the patch was reverted.",
        )
    return changed


# ---------------------------------------------------------------------------
# Secret scan and test runner
# ---------------------------------------------------------------------------


def _secret_scan(workdir: Path, files: Iterable[str]) -> list[str]:
    flagged: list[str] = []
    for rel in files:
        path = Path(contain_path(rel, str(workdir)))
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                flagged.append(rel)
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _PRIVATE_KEY_RE.search(text):
            flagged.append(rel)
            continue
        for line in text.splitlines():
            if looks_like_secret(line):
                flagged.append(rel)
                break
    return sorted(set(flagged))


def _run_tests(workdir: Path, tests: list[str]) -> None:
    for target in tests:
        try:
            proc = _run_subprocess(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "-q",
                    target,
                ],
                cwd=workdir,
                env=child_env(),
                timeout=900,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CliError(
                "tests_failed",
                f"test run failed for {target}: {exc}",
                "Fix the test environment or rerun verify; nothing was pushed.",
            ) from exc
        if proc.returncode != 0:
            detail = _tail(proc.stdout or proc.stderr)
            raise CliError(
                "tests_failed",
                f"test target failed: {target}\n{detail}",
                "Fix the failure in the worktree, then rerun verify; nothing was pushed.",
            )


def _push_branch(workdir: Path, branch: str) -> None:
    # Explicit feature-branch refspec; no force, no tags, no hook skip.
    _run_git(["push", "origin", f"HEAD:refs/heads/{branch}"], workdir)


def _commit_changes(workdir: Path, attempt_id: str, files: list[str]) -> str:
    _run_git(["add", "--", *files], workdir)
    message = f"bundlebees: apply verified attempt {attempt_id}"
    commit = _run_git(
        [
            "-c",
            "user.name=bundlebees",
            "-c",
            "user.email=bundlebees@localhost",
            "commit",
            "-m",
            message,
        ],
        workdir,
        check=False,
    )
    if commit.returncode != 0:
        raise CliError(
            "commit_failed",
            "could not commit the verified patch: " + _tail(commit.stderr),
            "Check repository hooks and worktree state; nothing was pushed.",
        )
    return _run_git(["rev-parse", "HEAD"], workdir).stdout.strip()


# ---------------------------------------------------------------------------
# Verify result data
# ---------------------------------------------------------------------------


def _write_verify_result(
    state_dir: Path,
    attempt_id: str,
    *,
    ok: bool,
    sha: str | None,
    branch: str,
    files: list[str],
    tests: list[str],
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path(contain_path(f"verify-results/{attempt_id}.json", str(state_dir)))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "attempt_id": attempt_id,
        "ok": bool(ok),
        "sha": sha,
        "branch": branch,
        "files": list(files),
        "tests": list(tests),
        "at": _now(),
    }
    clean = scrub(payload)
    # A git SHA is evidence, not a secret; scrub() cannot distinguish a 40-char
    # hex digest from a key-shaped string, so restore the validated digest.
    clean["sha"] = sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) else None
    text = json.dumps(clean, indent=2, sort_keys=True) + "\n"
    target.write_text(text, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return target


# ---------------------------------------------------------------------------
# Kanban / SSH helpers
# ---------------------------------------------------------------------------


def _run_kanban_comment(task_id: str, message: str) -> int:
    try:
        proc = _run_subprocess(
            ["hermes", "kanban", "comment", task_id, message],
            env=child_env(),
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return proc.returncode


def _run_ssh(host_alias: str, env: Mapping[str, str]) -> subprocess.CompletedProcess:
    return _run_subprocess(
        ["ssh", host_alias, "bash", "-s"],
        env=env,
        input_text=_SSH_SCRIPT,
        timeout=300,
    )


def _deploy_alias(config: Mapping[str, Any]) -> str | None:
    def safe(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or text.startswith("-") or any(ch in text for ch in "\x00\r\n\t "):
            return None
        return text

    for key in (
        "deploy_host",
        "deploy_host_alias",
        "deploy_alias",
        "ssh_host",
        "ssh_alias",
        "host_alias",
    ):
        alias = safe(config.get(key))
        if alias:
            return alias
    for nested_key in ("deploy", "ssh"):
        nested = config.get(nested_key)
        if isinstance(nested, Mapping):
            for key in ("host", "host_alias", "alias"):
                alias = safe(nested.get(key))
                if alias:
                    return alias
    return None


def _host_setup_prompt_text() -> str:
    root = Path(__file__).resolve()
    candidates = (
        root.parents[1] / "skills" / "bundlebees-operator" / "templates" / "host-setup-prompt.md",
        root.parent / "templates" / "host-setup-prompt.md",
    )
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        return str(scrub(text))
    return (
        "Host setup: configure the launcher token and provider key in the host "
        "secret store, start the launcher from its documented path, and report "
        "configured/not configured without printing values."
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_verify(ctx: Any, args: Any) -> int:
    attempt_id = _safe_attempt_id(getattr(args, "attempt_id", None))
    state_dir = _state_dir(ctx, args)
    config = _config(ctx)
    dry_run = bool(getattr(args, "dry_run", False))

    branch = ""
    files: list[str] = []
    tests: list[str] = []
    sha: str | None = None

    try:
        client = _client_for(ctx)
        result = _fetch_result(client, attempt_id)
        brief = _brief_from(result)

        patch = _result_patch(result)
        if not patch:
            raise CliError(
                "no_result_yet",
                "the worker result has no patch yet",
                "Wait for the worker to finish and collect a result with a patch.",
            )

        branch = _safe_branch(
            getattr(args, "branch", None)
            or result.get("branch")
            or brief.get("branch")
            or config.get("branch")
            or config.get("sync_branch")
        )

        workdir_value = (
            getattr(args, "workdir", None)
            or config.get("worktree")
            or config.get("worktree_path")
            or config.get("repo_path")
        )
        workdir = _prepare_worktree(workdir_value, branch)

        observed = result.get("observed")
        assigned_values = result.get("assigned_files") or brief.get("assigned_files")
        if not assigned_values and isinstance(observed, Mapping):
            assigned_values = observed.get("assigned_files")
        files = _assigned_files(assigned_values, workdir)

        requested_tests = getattr(args, "tests", None)
        if requested_tests:
            tests = _test_targets(requested_tests, workdir)
        else:
            observed_tests = observed.get("tests") if isinstance(observed, Mapping) else None
            for candidate in (result.get("tests"), brief.get("tests"), observed_tests):
                tests = _test_targets(candidate, workdir)
                if tests:
                    break
            if not tests:
                tests = _tests_from_commands(brief.get("commands"), workdir)
        if not tests:
            raise CliError(
                "no_tests",
                "no tests were named for this attempt",
                "Pass --tests with the brief's test targets; verify does not run full suites.",
            )

        kanban_id = result.get("kanban_task_id") or brief.get("kanban_task_id")
        if getattr(args, "kanban_comment", False) and not kanban_id:
            raise CliError(
                "missing_kanban_task",
                "--kanban-comment requires a kanban_task_id on the attempt",
                "Dispatch with kanban_task_id or omit --kanban-comment.",
            )

        changed = _apply_patch(workdir, patch, files)
        files = changed

        flagged = _secret_scan(workdir, files)
        if flagged:
            _reset_patch(workdir)
            raise CliError(
                "secret_detected",
                "secret scan found secret-like content in: " + ", ".join(flagged),
                "Remove secrets from the worker output; nothing was committed or pushed.",
            )

        _run_tests(workdir, tests)

        if dry_run:
            target = _write_verify_result(
                state_dir,
                attempt_id,
                ok=True,
                sha=None,
                branch=branch,
                files=files,
                tests=tests,
            )
            _print("bundlebees verify")
            _print(f"attempt: {attempt_id}")
            _print(f"branch: {branch}")
            _print(f"files: {', '.join(files)}")
            _print(f"tests: {', '.join(tests)}")
            _print("result: dry-run passed; nothing was committed or pushed")
            _print(f"verify result: {target}")
            if kanban_id:
                _print(
                    f"suggested: hermes kanban comment {kanban_id} "
                    f'\"verify passed for {attempt_id} (dry-run)\"'
                )
            else:
                _print(
                    'suggested: hermes kanban comment <task_id> '
                    f'\"bundlebees verify passed for {attempt_id} (dry-run)\"'
                )
            return 0

        sha = _commit_changes(workdir, attempt_id, files)
        _push_branch(workdir, branch)

        target = _write_verify_result(
            state_dir,
            attempt_id,
            ok=True,
            sha=sha,
            branch=branch,
            files=files,
            tests=tests,
        )

        _print("bundlebees verify")
        _print(f"attempt: {attempt_id}")
        _print(f"branch: {branch}")
        _print_sha(sha)
        _print(f"files: {', '.join(files)}")
        _print(f"tests: {', '.join(tests)}")
        _print(f"pushed: feature branch only ({BRANCH_BUDGET_NOTE})")
        _print(f"verify result: {target}")
        suggested = (
            f'hermes kanban comment {kanban_id} '
            f'"bundlebees verify passed for {attempt_id}"'
            if kanban_id
            else f'hermes kanban comment <task_id> "bundlebees verify passed for {attempt_id}"'
        )
        _print(f"suggested: {suggested}")
        if kanban_id:
            if getattr(args, "kanban_comment", False):
                rc = _run_kanban_comment(
                    str(kanban_id),
                    f"bundlebees verify passed for {attempt_id} at {sha}",
                )
                if rc != 0:
                    _print("warning: the kanban comment command returned non-zero; the board was not changed by verify itself")
        return 0
    except CliError as exc:
        try:
            _write_verify_result(
                state_dir,
                attempt_id,
                ok=False,
                sha=sha,
                branch=branch,
                files=files,
                tests=tests,
            )
        except Exception:  # noqa: BLE001 - preserve the original CLI error
            pass
        raise exc


def _cmd_deploy(ctx: Any, args: Any) -> int:
    config = _config(ctx)
    alias = _deploy_alias(config)
    execute = bool(getattr(args, "execute", False))

    _print("bundlebees deploy (user-run)")
    _print("steps:")
    for step in _DEPLOY_STEPS:
        _print(f"  {step}")
    _print(f"host alias: {'configured (value withheld)' if alias else 'not configured'}")
    _print("SSH is not attempted unless --execute is passed and a host alias is stored.")

    if not execute:
        _print("")
        _print("host setup prompt:")
        _print(_host_setup_prompt_text())
        _print("")
        _print("No SSH was attempted. Re-run with --execute after storing a host alias.")
        return 0

    if not alias:
        raise CliError(
            "not_configured",
            "no stored SSH host alias is configured",
            "Store a host alias in bundlebees plugin settings; hosts are never passed on argv.",
        )

    _print("Executing the stored SSH deploy path (host alias value withheld).")
    env = child_env()
    try:
        proc = _run_ssh(alias, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError(
            "ssh_failed",
            "the stored SSH host deploy path could not start",
            "Confirm SSH is available and the stored alias is correct, then retry.",
        ) from exc
    if proc.returncode != 0:
        # Do not echo ssh stderr: it can contain the stored host alias.
        raise CliError(
            "ssh_failed",
            "the stored SSH host deploy path returned non-zero",
            "Check the stored host alias and worker host, then retry.",
        )
    _print("SSH deploy path completed; apply the host setup prompt on the worker host.")
    return 0


def _measure_schema_tokens() -> int | None:
    try:
        from .tools import _SCHEMAS
    except Exception:
        return None
    try:
        body = json.dumps([schema for _, schema in _SCHEMAS], separators=(",", ":"))
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(body))
    except Exception:
        return None


def _cmd_doctor(ctx: Any, args: Any) -> int:
    _print("bundlebees doctor")
    configured = _token_configured(ctx)
    _print(f"token: {'configured' if configured else 'not configured'}")

    reachable = False
    detail = "not checked"
    client = getattr(ctx, "bundlebees_client", None)
    if client is None:
        try:
            client = _client_for(ctx)
        except CliError:
            client = None
    health = getattr(client, "health", None) if client is not None else None
    if callable(health):
        try:
            data = health()
        except Exception:
            detail = "unreachable"
        else:
            reachable = bool(data.get("ok", True)) if isinstance(data, Mapping) else True
            detail = "reachable" if reachable else "reported not ok"
    else:
        detail = "unavailable"
    _print(f"adapter: {detail}")

    measured = _measure_schema_tokens()
    if measured is None:
        _print("schema_tokens: unavailable (install tiktoken to measure)")
    else:
        _print(f"schema_tokens: {measured} (budget 900 o200k)")
    return 0
