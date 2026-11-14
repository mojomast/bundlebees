"""User-run ``hermes bundlebees`` CLI: verify worker results and deploy a host.

These commands are deliberately *not* model tools. ``verify`` fetches one
worker result, loads the immutable coordinator lease written at dispatch (the
only file-scope authority; worker metadata never widens it), proves the patch
was built against the brief's exact ``source_rev``, applies it to a clean
feature-branch worktree, runs a secret scan, runs the named tests in a
disposable sandbox (no network, no coordinator credentials, scrubbed env), and
on green commits and pushes the feature branch (never ``main``). ``integrate``
runs the full suite on the combined branch; only an accepted wave counts.
``lease`` records a lease for a brief dispatched outside the tool, and
``metrics`` summarizes the append-only outcome log. ``deploy`` prints the host
setup prompt by default and only reaches a host when the user passes
``--execute`` with an alias already stored in plugin config.

All child processes receive ``security.child_env`` rather than the ambient
environment. Nothing here reads credentials from the environment; the
``doctor`` command asks Hermes's ``agent.secret_scope.get_secret`` whether a
token is configured and never prints its value. Settings come from
``ctx.get_config``; state lives in ``plugin_data_dir("bundlebees")``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import brief as _brief_mod
from . import job_cli
from . import acdd_cli, adapters, leases, metrics, revision, sandbox
from .security import child_env, config_flag, contain_path, looks_like_secret, scrub

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
        "--commit-local",
        action="store_true",
        help="Verify in an isolated worktree and create a local commit; never push.",
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

    lease = subparsers.add_parser(
        "lease",
        help="Record the immutable coordinator lease for a brief dispatched outside the tool.",
    )
    lease.add_argument("attempt_id", help="Worker attempt id.")
    lease.add_argument("--brief", required=True, help="Path to the brief JSON that was dispatched.")
    lease.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    lease.set_defaults(command="lease")

    integrate = subparsers.add_parser(
        "integrate",
        help="Run the full suite on the combined branch; a wave is accepted only if it passes.",
    )
    integrate.add_argument("--wave", required=True, help="Wave id, e.g. w1.")
    integrate.add_argument("--attempts", nargs="+", required=True, help="Verified attempt ids in this wave.")
    integrate.add_argument("--workdir", default=None, help="Clean git worktree (defaults to the configured one).")
    integrate.add_argument("--branch", default=None, help="Combined feature branch. main/master are refused.")
    integrate.add_argument("--run", default=None, help="Metrics run id (defaults to the first lease's run_id).")
    integrate.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    integrate.set_defaults(command="integrate")

    metrics_cmd = subparsers.add_parser(
        "metrics",
        help="Summarize the append-only outcome log, or record a human intervention.",
    )
    metrics_cmd.add_argument("--run", default=None, help="Run id (default: every run).")
    metrics_cmd.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    metrics_cmd.add_argument("--note", default=None, help="Record one human intervention with this note.")
    metrics_cmd.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    metrics_cmd.set_defaults(command="metrics")

    doctor = subparsers.add_parser(
        "doctor",
        help="Report token configured/not, adapter reachability, and schema size.",
    )
    doctor.set_defaults(command="doctor")

    acdd_cli.add_parsers(subparsers)
    job_cli.add_parsers(subparsers)
    return group


def _dispatch(ctx: Any, args: Any) -> int:
    command = getattr(args, "command", None)
    try:
        if command == "verify":
            if getattr(args, "commit_local", False):
                return _cmd_verify_commit_local(ctx, args)
            return _cmd_verify(ctx, args)
        if command == "deploy":
            return _cmd_deploy(ctx, args)
        if command == "doctor":
            return _cmd_doctor(ctx, args)
        if command == "lease":
            return _cmd_lease(ctx, args)
        if command == "integrate":
            return _cmd_integrate(ctx, args)
        if command == "metrics":
            return _cmd_metrics(ctx, args)
        if command == "contract":
            return acdd_cli.cmd_contract(ctx, args)
        if command == "tdd":
            return acdd_cli.cmd_tdd(ctx, args)
        if command == "job":
            return job_cli.cmd_job(
                ctx,
                args,
                state_dir=_state_dir(ctx, args),
                client_factory=lambda: _client_for(ctx),
            )
        raise CliError(
            "invalid_argument",
            "unknown bundlebees command",
            "Use one of: verify, integrate, lease, metrics, deploy, doctor, contract, tdd, job.",
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
    """Settings via ``ctx.get_config(key, default)`` for each declared key."""
    from .settings import settings_from_ctx

    return settings_from_ctx(ctx)


def plugin_state_dir() -> Path | None:
    """``plugins.plugin_storage.plugin_data_dir("bundlebees")``, or None outside Hermes.

    That is ``<hermes home>/plugin-data/bundlebees`` for the active profile.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except Exception:
        return None
    try:
        return Path(plugin_data_dir("bundlebees")).resolve()
    except Exception:
        return None


def _state_dir_or_none(args: Any = None) -> Path | None:
    explicit = getattr(args, "state_dir", None) if args is not None else None
    if explicit:
        return Path(str(explicit)).expanduser().resolve()
    return plugin_state_dir()


def _state_dir(ctx: Any, args: Any) -> Path:
    found = _state_dir_or_none(args)
    if found is not None:
        return found
    raise CliError(
        "not_configured",
        "the Hermes plugin data directory is not available",
        "Run this command through `hermes bundlebees`, or pass --state-dir.",
    )


def _client_for(ctx: Any, args: Any = None) -> Any:
    injected = getattr(ctx, "bundlebees_client", None)
    if injected is not None:
        return injected
    try:
        return adapters.adapter_from_config(
            _config(ctx), state_dir=_state_dir_or_none(args)
        )
    except Exception as exc:  # noqa: BLE001 - map client configuration errors
        code = getattr(exc, "code", "not_configured")
        fix = getattr(
            exc,
            "fix",
            "Configure the launcher endpoint in bundlebees plugin settings and retry.",
        )
        raise CliError(str(code), str(exc), str(fix)) from exc


def _token_configured(ctx: Any) -> bool:
    """Whether Hermes has the launcher token. Never prints the value."""
    injected = getattr(ctx, "bundlebees_token_configured", None)
    if callable(injected):
        try:
            return bool(injected())
        except Exception:
            return False
    from .client import token_configured

    try:
        return token_configured()
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


def _patch_text(value: Any) -> str | None:
    """Patch text from a string, or from an object that carries one."""

    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, Mapping):
        for key in ("diff", "patch.diff", "unified_diff", "patch"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested
    return None


def _result_patch(result: Mapping[str, Any]) -> str | None:
    """The patch text from a result envelope, whatever shape it arrived in.

    The launcher returns ``patch`` as an object (``{diff, files}``); older
    envelopes return a bare string. Both are the same patch, so accept both —
    this is shape handling, not a bypass: the caller still checks the lease and
    the files the patch touches.
    """

    def find(value: Any, depth: int = 0) -> str | None:
        if depth > 3:
            return None
        if isinstance(value, Mapping):
            for key in ("patch", "unified_diff", "diff"):
                candidate = _patch_text(value.get(key))
                if candidate:
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
        envelope = getattr(client, "result_envelope", None)
        if callable(envelope):
            result = envelope(attempt_id)
        else:
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
    if _result_patch(data) is None:
        raise CliError(
            "no_result_yet",
            "the worker result has no patch yet",
            "Wait for the worker to finish and collect a result with a patch.",
        )
    return data


_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_HEX_REV_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_USAGE_KEYS = (
    "inputTokens", "outputTokens", "reasoningTokens", "cachedInputTokens", "totalTokens",
    "input_tokens", "output_tokens", "total_tokens", "tokens", "cost_usd", "costUsd",
)


def _artifact_usage(usage: Any) -> dict[str, Any]:
    """Launcher-reported usage, or an explicit ``unavailable`` marker (never a fake zero)."""
    if not isinstance(usage, Mapping):
        return {"source": "unavailable"}
    out: dict[str, Any] = {}
    for key in _USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    if not out:
        return {"source": "unavailable"}
    out["source"] = str(usage.get("source") or "launcher_reported")[:40]
    return out


def _print_digest(label: str, digest: Any) -> None:
    """Print a validated sha256 digest without passing it through scrub()."""
    if isinstance(digest, str) and _HEX64_RE.fullmatch(digest):
        print(f"{label}: {digest}")


def _fetch_artifact(
    client: Any, attempt_id: str, state_dir: Path, source_rev: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch the patch through Orbit's coordinator-only artifact API.

    Checks that the artifact was produced for the lease's ``source_rev`` and that
    its patch hash matches the one pinned on first read. Returns the result for
    ``adapters.normalize_result`` and the non-secret evidence fields. Never prints
    or stores the patch itself.
    """
    try:
        art = client.artifact(attempt_id)
    except Exception as exc:  # noqa: BLE001 - ClientError or fake error
        code = getattr(exc, "code", "launcher_error")
        fix = getattr(exc, "fix", "Check Orbit and retry when the worker finishes.")
        raise CliError(str(code), str(exc), str(fix)) from exc
    if not isinstance(art, Mapping):
        raise CliError("artifact_invalid", "Orbit did not return an artifact object", "Reject this worker result.")
    if art.get("source_rev") != source_rev:
        raise CliError(
            "stale_revision",
            "the artifact was produced for a different source_rev than the lease",
            "Reject this worker result and re-dispatch from the current source_rev; nothing was applied.",
        )
    digest = art.get("patch_sha256")
    pin_path = Path(contain_path(f"artifact-pins/{attempt_id}.json", str(state_dir)))
    if pin_path.exists():
        try:
            pinned = json.loads(pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pinned = {}
        if pinned.get("patch_sha256") != digest:
            raise CliError(
                "artifact_changed",
                "the worker patch differs from the one first retrieved for this attempt",
                "Reject this attempt; a retry must return the identical artifact.",
            )
    elif digest is not None:
        pin_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = pin_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "attempt_id": attempt_id, "patch_sha256": digest, "result_sha256": art.get("result_sha256"),
            "source_rev": source_rev, "first_read_at": _now(),
        }, sort_keys=True), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(pin_path)
    observed = art.get("observed") if isinstance(art.get("observed"), Mapping) else {}
    base = observed.get("local_base_rev")
    usage = _artifact_usage(art.get("usage"))
    tokens = usage.get("totalTokens", usage.get("total_tokens", usage.get("tokens")))
    result: dict[str, Any] = {
        "attempt_id": attempt_id,
        "state": art.get("state"),
        "patch": art.get("patch"),
        "usage": {"tokens": tokens if isinstance(tokens, int) else None, "cost_usd": usage.get("cost_usd")},
    }
    if isinstance(base, str) and _HEX_REV_RE.fullmatch(base):
        result["base_rev"] = base
    evidence: dict[str, Any] = {
        "artifact": art.get("artifact_version"),
        "patch_sha256": digest,
        "result_sha256": art.get("result_sha256"),
        "usage": usage,
        "model": art.get("model"),
        "launcher": art.get("launcher"),
        "observed": {k: observed.get(k) for k in ("tool_calls", "stop_reason", "status") if k in observed},
    }
    return result, evidence


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


def _added_lines(patch: str) -> dict[str, str]:
    """Lines each file gains in a unified diff, keyed by its new path."""
    added: dict[str, list[str]] = {}
    current: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip().split("\t", 1)[0]
            current = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            if current is not None:
                added.setdefault(current, [])
        elif line.startswith("diff --git "):
            current = None
        elif current is not None and line.startswith("+"):
            added[current].append(line[1:])
    return {path: "\n".join(lines) for path, lines in added.items()}


def _secret_scan(workdir: Path, files: Iterable[str], patch: str | None = None) -> list[str]:
    """Flag files whose patch-added lines look secret-like.

    With a patch, only the lines it adds are scanned: content already on source_rev is
    not the worker's output (and bundlebees' own sources mention tokens by design). A
    file the diff does not describe line by line is scanned whole (fail closed).
    """
    flagged: list[str] = []
    added = _added_lines(patch) if patch else {}
    for rel in files:
        if rel in added:
            text = added[rel]
            if len(text.encode("utf-8", errors="replace")) > _MAX_SCAN_BYTES:
                flagged.append(rel)
                continue
        else:
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


def _sandbox_options(config: Mapping[str, Any]) -> dict[str, Any]:
    backend = str(config.get("verify_isolation") or "auto").strip().lower()
    try:
        timeout = int(config.get("verify_timeout_s") or 900)
    except (TypeError, ValueError):
        timeout = 900
    tiktoken_cache = str(config.get("sandbox_tiktoken_cache") or "").strip() or None
    return {
        "backend": backend,
        "allow_unsafe_local": config_flag(config.get("verify_allow_unsafe_local"), False),
        "allow_weak": sandbox.weak_override_enabled(
            config_flag(config.get("verify_allow_weak_unshare"), False)
        ),
        "timeout": max(30, min(timeout, 7200)),
        "tiktoken_cache": tiktoken_cache,
    }


def _pytest_targets(targets: list[str], workdir: Path) -> list[str]:
    """Keep only the approved targets pytest can collect; drop non-Python fixtures."""
    kept: list[str] = []
    for target in targets:
        if not isinstance(target, str):
            continue
        text = target.strip()
        if not text:
            continue
        if "::" in text or text.endswith(".py") or text == ".":
            kept.append(text)
            continue
        try:
            candidate = Path(contain_path(text.split("::", 1)[0] or ".", str(workdir)))
        except ValueError:
            continue
        if candidate.is_dir():
            kept.append(text)
    return kept


def _run_sandboxed(workdir: Path, targets: list[str], config: Mapping[str, Any]) -> dict[str, Any]:
    """Run pytest on a disposable copy of *workdir*; return evidence only."""
    pytest_targets = _pytest_targets(list(targets), Path(workdir))
    if not pytest_targets:
        raise CliError(
            "no_test_targets",
            "none of the approved test files can be collected by pytest",
            "Reference at least one Python test file in scope.test_files.",
        )
    try:
        return sandbox.run_in_sandbox(workdir, sandbox.pytest_argv(pytest_targets), **_sandbox_options(config))
    except sandbox.SandboxUnavailable as exc:
        raise CliError(exc.code, exc.error, exc.fix) from exc
    except ValueError as exc:
        raise CliError(
            "invalid_argument",
            f"unknown verify_isolation backend: {exc}",
            "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).",
        ) from exc


def _run_tests(workdir: Path, tests: list[str], config: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _run_sandboxed(workdir, tests, config)
    if evidence.get("timed_out") or evidence.get("exit_code") != 0:
        detail = _tail(evidence.get("output_tail"))
        raise CliError(
            "tests_failed",
            f"sandboxed tests failed ({evidence.get('backend')}): {', '.join(tests)}\n{detail}",
            "Fix the failure in the worktree, then rerun verify; nothing was pushed.",
        )
    return evidence


def _diffstat(workdir: Path, patch: str) -> dict[str, int]:
    proc = _run_git(["apply", "--numstat", "-"], workdir, input_text=patch, check=False)
    added = deleted = files = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added += int(parts[0]) if parts[0].isdigit() else 0
        deleted += int(parts[1]) if parts[1].isdigit() else 0
    return {"files": files, "added": added, "deleted": deleted}


def _usage(worker: Mapping[str, Any]) -> dict[str, Any]:
    """Token/cost usage from the adapter-normalized result (untrusted, metrics only)."""
    usage = worker.get("usage") if isinstance(worker.get("usage"), Mapping) else {}
    out: dict[str, Any] = {}
    tokens = usage.get("tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        out["tokens"] = tokens
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        out["cost_usd"] = float(cost)
    return out


def _as_cli_error(exc: Exception) -> CliError:
    return CliError(
        str(getattr(exc, "code", "invalid_argument")),
        str(getattr(exc, "error", exc)),
        str(getattr(exc, "fix", "Check the inputs and retry.")),
    )


def _metric(state_dir: Path, run_id: str, event: str, **fields: Any) -> None:
    try:
        metrics.append_event(state_dir, run_id, event, **fields)
    except Exception:  # noqa: BLE001 - metrics never block verify or integrate
        pass


def _first_verify(state_dir: Path, run_id: str, attempt_id: str) -> bool:
    try:
        events = metrics.read_events(state_dir, run_id)
    except Exception:  # noqa: BLE001
        return False
    return not any(e.get("event") == "verify" and e.get("attempt_id") == attempt_id for e in events)


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
    evidence: Mapping[str, Any] | None = None,
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
        "evidence": dict(evidence or {}),
    }
    source_rev = (evidence or {}).get("source_rev")
    clean = scrub(payload)
    if isinstance(source_rev, str) and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", source_rev):
        clean["evidence"]["source_rev"] = source_rev
    for key in ("patch_sha256", "result_sha256"):
        digest = (evidence or {}).get(key)
        if isinstance(digest, str) and _HEX64_RE.fullmatch(digest):
            clean["evidence"][key] = digest
    usage = (evidence or {}).get("usage")
    if isinstance(usage, Mapping):
        # Validated numeric counts; scrub() would redact the *Tokens key names.
        clean["evidence"]["usage"] = _artifact_usage(usage)
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


def _cmd_verify_commit_local(ctx: Any, args: Any) -> int:
    """`verify --commit-local`: isolated worktree, local commit, no push."""

    from . import verify_local

    state_dir = _state_dir(ctx, args)
    workdir = Path(getattr(args, "workdir", None) or os.getcwd())
    client = _client_for(ctx)
    attempt_id = str(args.attempt_id)

    envelope = client.result_envelope(attempt_id) if hasattr(client, "result_envelope") else client.result(attempt_id)
    patch = _result_patch(envelope)
    if not patch:
        raise CliError("no_result_yet", "the worker result has no patch yet", "Wait for the worker to finish.")
    patch_block = envelope.get("patch") if isinstance(envelope.get("patch"), Mapping) else {}
    files = [str(item.get("path")) for item in (patch_block.get("files") or []) if item.get("path")]
    source = str(((envelope.get("source") or {}).get("repo_rev")) or "")
    tests = list(getattr(args, "tests", None) or [])
    gitleaks = shutil.which("gitleaks")

    try:
        record = verify_local.verify_and_commit(
            state_dir,
            workdir,
            attempt_id,
            patch=patch,
            declared_files=files,
            base_rev=source,
            tests=tests,
            gitleaks=gitleaks,
        )
    except verify_local.VerifyError as exc:
        raise CliError(exc.code, exc.message, exc.fix) from exc

    _print("bundlebees verify --commit-local")
    _print(f"attempt: {attempt_id}")
    _print(f"commit: {record['sha']}")
    _print(f"branch: {record['branch']} (local only, not pushed)")
    _print(f"files: {', '.join(record['files'])}")
    return 0


def _cmd_verify(ctx: Any, args: Any) -> int:
    attempt_id = _safe_attempt_id(getattr(args, "attempt_id", None))
    state_dir = _state_dir(ctx, args)
    config = _config(ctx)
    dry_run = bool(getattr(args, "dry_run", False))

    branch = ""
    files: list[str] = []
    tests: list[str] = []
    sha: str | None = None
    run_id = "default"
    evidence: dict[str, Any] = {}

    try:
        # 1. Lease authority: only the coordinator record written at dispatch.
        try:
            record = leases.load_lease(state_dir, attempt_id)
        except leases.LeaseError as exc:
            raise _as_cli_error(exc) from exc
        run_id = str(record.get("run_id") or "default")
        source_rev = str(record.get("source_rev") or "")
        evidence["source_rev"] = source_rev

        client = _client_for(ctx, args)
        if callable(getattr(client, "artifact", None)):
            # Supported path: Orbit's coordinator-only artifact API, never launcher files.
            result, artifact_evidence = _fetch_artifact(client, attempt_id, state_dir, source_rev)
            evidence.update(artifact_evidence)
        else:
            result = _fetch_result(client, attempt_id)
        warnings = leases.ignored_worker_claims(record, result)
        if warnings:
            evidence["ignored_worker_claims"] = warnings

        # Agent-agnostic boundary: every runtime's output is reduced to the
        # adapter result contract (patch, declared base, usage, worker claims).
        worker = adapters.normalize_result(result)
        patch = worker.get("patch")
        if not patch:
            raise CliError(
                "no_result_yet",
                "the worker result has no patch yet",
                "Wait for the worker to finish and collect a result with a patch.",
            )

        # ACDD: a contract in the lease makes approved tests read-only and
        # requires a valid RED before anything is applied. None = legacy.
        acdd_state = acdd_cli.verify_preflight(record, state_dir, _patch_files(patch))
        if acdd_state is not None:
            evidence["contract_id"] = acdd_state["contract"]["id"]

        # Branch comes from the user or the lease, never from worker metadata.
        branch = _safe_branch(
            getattr(args, "branch", None)
            or record.get("branch")
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
        files = _assigned_files(leases.lease_files(record), workdir)

        requested_tests = getattr(args, "tests", None)
        acceptance = record.get("acceptance") if isinstance(record.get("acceptance"), Mapping) else {}
        if requested_tests:
            tests = _test_targets(requested_tests, workdir)
        elif acdd_state is not None:
            tests = _test_targets(acdd_state["tests"], workdir)
        else:
            tests = _test_targets(record.get("tests"), workdir) or _test_targets(
                acceptance.get("tests"), workdir
            )
            if not tests:
                tests = _tests_from_commands(acceptance.get("commands"), workdir)
            if not tests:
                brief = record.get("brief") if isinstance(record.get("brief"), Mapping) else {}
                tests = _tests_from_commands(brief.get("commands"), workdir)
        if not tests:
            raise CliError(
                "no_tests",
                "no tests were named for this attempt",
                "Pass --tests or put test targets in the brief's acceptance.tests.",
            )

        kanban_id = (record.get("brief") or {}).get("kanban_task_id") if isinstance(record.get("brief"), Mapping) else None
        if getattr(args, "kanban_comment", False) and not kanban_id:
            raise CliError(
                "missing_kanban_task",
                "--kanban-comment requires a kanban_task_id on the attempt",
                "Dispatch with kanban_task_id or omit --kanban-comment.",
            )

        # 2. Revision guarantees: declared base, exact-commit apply, ancestry.
        try:
            revision.check_declared_base({"base_rev": worker.get("base_rev")}, source_rev)
            revision.check_source_rev(workdir, source_rev)
            revision.check_applies_at(workdir, source_rev, patch)
            revision.check_branch_descends(workdir, source_rev)
        except revision.RevisionError as exc:
            raise _as_cli_error(exc) from exc

        # 3. Scope + apply to the feature branch (git apply never runs code).
        evidence["diffstat"] = _diffstat(workdir, patch)
        changed = _apply_patch(workdir, patch, files)
        files = changed

        flagged = _secret_scan(workdir, files, patch)
        if flagged:
            _reset_patch(workdir)
            raise CliError(
                "secret_detected",
                "secret scan found secret-like content in: " + ", ".join(flagged),
                "Remove secrets from the worker output; nothing was committed or pushed.",
            )

        # 4. Worker-produced code runs only in the disposable sandbox.
        green: dict[str, Any] | None = None
        if acdd_state is not None:
            test_evidence = _run_sandboxed(workdir, tests, config)
            passed = not test_evidence.get("timed_out") and test_evidence.get("exit_code") == 0
            if not passed or dry_run:
                # Record against source_rev: the patch is applied but not committed.
                try:
                    green = acdd_cli.record_green(
                        state_dir, acdd_state, workdir, test_evidence, source_rev,
                        note="patch applied on source_rev, not committed",
                    )
                except CliError:
                    _reset_patch(workdir)
                    raise
        else:
            try:
                test_evidence = _run_tests(workdir, tests, config)
            except CliError:
                _reset_patch(workdir)
                raise
        evidence.update(
            backend=test_evidence.get("backend"),
            exit_code=test_evidence.get("exit_code"),
            junit=test_evidence.get("junit"),
            duration_s=test_evidence.get("duration_s"),
        )

        if dry_run:
            target = _write_verify_result(
                state_dir, attempt_id, ok=True, sha=None, branch=branch,
                files=files, tests=tests, evidence=evidence,
            )
            _record_verify(state_dir, run_id, attempt_id, record, worker, ok=True, code="dry_run")
            _print("bundlebees verify")
            _print(f"attempt: {attempt_id}")
            _print(f"branch: {branch}")
            _print(f"files: {', '.join(files)}")
            _print(f"tests: {', '.join(tests)}")
            _print(f"sandbox: {evidence.get('backend')}")
            _print_digest("patch_sha256", evidence.get("patch_sha256"))
            acdd_cli.print_mode(acdd_state, green)
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
        if acdd_state is not None:
            green = acdd_cli.record_green(state_dir, acdd_state, workdir, test_evidence, sha)
        _push_branch(workdir, branch)

        target = _write_verify_result(
            state_dir, attempt_id, ok=True, sha=sha, branch=branch,
            files=files, tests=tests, evidence=evidence,
        )
        _record_verify(state_dir, run_id, attempt_id, record, worker, ok=True, code="verified", sha=sha)

        _print("bundlebees verify")
        _print(f"attempt: {attempt_id}")
        _print(f"branch: {branch}")
        _print_sha(sha)
        _print(f"files: {', '.join(files)}")
        _print(f"tests: {', '.join(tests)}")
        _print(f"sandbox: {evidence.get('backend')}")
        _print_digest("patch_sha256", evidence.get("patch_sha256"))
        acdd_cli.print_mode(acdd_state, green)
        _print(f"pushed: feature branch only ({BRANCH_BUDGET_NOTE})")
        _print(f"verify result: {target}")
        _print("wave: not accepted until `hermes bundlebees integrate` passes the full suite")
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
                state_dir, attempt_id, ok=False, sha=sha, branch=branch,
                files=files, tests=tests, evidence=evidence,
            )
        except Exception:  # noqa: BLE001 - preserve the original CLI error
            pass
        if exc.code not in ("lease_missing", "lease_tampered", "lease_invalid"):
            _metric(state_dir, run_id, "verify", attempt_id=attempt_id, ok=False, code=exc.code)
        raise exc


def _record_verify(
    state_dir: Path,
    run_id: str,
    attempt_id: str,
    record: Mapping[str, Any],
    worker: Mapping[str, Any],
    *,
    ok: bool,
    code: str,
    sha: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "attempt_id": attempt_id,
        "ok": ok,
        "code": code,
        "first_pass": _first_verify(state_dir, run_id, attempt_id),
        **_usage(worker),
    }
    if sha:
        fields["sha"] = sha
    _metric(state_dir, run_id, "verify", **fields)
    retry_of = record.get("retry_of")
    if ok and isinstance(retry_of, str) and retry_of:
        _metric(state_dir, run_id, "recovery", attempt_id=attempt_id, retry_of=retry_of)


def _cmd_lease(ctx: Any, args: Any) -> int:
    attempt_id = _safe_attempt_id(getattr(args, "attempt_id", None))
    state_dir = _state_dir(ctx, args)
    path = Path(str(getattr(args, "brief", "") or "")).expanduser()
    try:
        if path.stat().st_size > 1_000_000:
            raise CliError("brief_invalid", "brief file is larger than 1 MB", "Pass the dispatched brief JSON.")
        data = json.loads(path.read_text(encoding="utf-8"))
    except CliError:
        raise
    except (OSError, ValueError) as exc:
        raise CliError("brief_invalid", "brief file could not be read as JSON", "Pass the dispatched brief JSON file.") from exc
    if not isinstance(data, Mapping):
        raise CliError("brief_invalid", "brief must be a JSON object", "Pass the dispatched brief JSON file.")
    try:
        brief = _brief_mod.validate_brief(data)
        record = leases.record_lease(state_dir, attempt_id, brief)
    except (_brief_mod.BriefError, leases.LeaseError) as exc:
        raise _as_cli_error(exc) from exc
    _metric(state_dir, str(record.get("run_id") or "default"), "dispatch", attempt_id=attempt_id)
    _print("bundlebees lease")
    _print(f"attempt: {attempt_id}")
    print(f"source_rev: {record['source_rev']}")
    _print(f"files: {', '.join(record['assigned_files'])}")
    _print("lease: recorded (read-only, HMAC-signed); verify trusts only this record")
    return 0


_CATEGORIES = ("regression", "contract", "security")


def _split_targets(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,]+", value) if item]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, str) and item.strip()]
    return []


def _repo_testpaths(workdir: Path) -> list[str]:
    """``testpaths`` from pytest.ini, pyproject.toml, tox.ini or setup.cfg (first found)."""
    import configparser

    for name, section in (("pytest.ini", "pytest"), ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        path = workdir / name
        if path.is_file():
            parser = configparser.ConfigParser()
            try:
                parser.read(path, encoding="utf-8")
            except configparser.Error:
                continue
            if parser.has_option(section, "testpaths"):
                return _split_targets(parser.get(section, "testpaths"))
    pyproject = workdir / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unreadable config: fall back to defaults
            data = {}
        paths = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options", {}).get("testpaths")
        if paths:
            return _split_targets(paths)
    return []


def _integration_plan(workdir: Path, config: Mapping[str, Any], items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Targets per category: configured regression suite, approved contract tests, security tests."""
    regression = _split_targets(config.get("integrate_suite")) or _repo_testpaths(workdir) or ["."]
    contract = sorted({str(t) for item in items for t in item.get("tests") or []})
    security = _split_targets(config.get("integrate_security_tests"))
    plan: dict[str, dict[str, Any]] = {}
    for name, targets, configured in (
        ("regression", regression, True),
        ("contract", contract, bool(items)),
        ("security", security, bool(security)),
    ):
        clean = _test_targets(targets, workdir) if targets else []
        missing = [t for t in clean if not (workdir / t.split("::", 1)[0]).exists()]
        plan[name] = {"targets": clean, "configured": configured, "missing": missing}
    return plan


def _case_in(case_id: str, target: str) -> bool:
    if target in (".", ""):
        return True
    path = case_id.split("::", 1)[0]
    if "::" in target:
        return case_id == target or case_id.startswith(target + "::") or case_id.startswith(target + "[")
    return path == target or path.startswith(target.rstrip("/") + "/")


def _categorize(plan: Mapping[str, Mapping[str, Any]], suite: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Per-category passed / failed / not_run from the suite's JUnit cases."""
    cases = [c for c in suite.get("cases") or [] if isinstance(c, Mapping) and isinstance(c.get("id"), str)]
    out: dict[str, dict[str, Any]] = {}
    blocking: list[str] = []
    for name in _CATEGORIES:
        entry = plan.get(name) or {}
        targets = list(entry.get("targets") or [])
        if not entry.get("configured"):
            reason = "no contract attempts" if name == "contract" else "not configured"
            out[name] = {"status": "not_run", "reason": reason, "targets": [], "tests": 0}
            continue
        mine = [c for c in cases if any(_case_in(c["id"], t) for t in targets)]
        failed = sorted({c["id"] for c in mine if c.get("outcome") in ("failed", "error")})
        info: dict[str, Any] = {"targets": targets, "tests": len(mine), "failed": failed[:50]}
        if entry.get("missing"):
            info.update(status="failed", reason="missing: " + ", ".join(entry["missing"]))
        elif suite.get("timed_out"):
            info.update(status="failed", reason="timed out")
        elif failed:
            info.update(status="failed", reason=f"{len(failed)} failing")
        elif not mine:
            info.update(status="failed", reason="no tests collected")
        else:
            info.update(status="passed")
        out[name] = info
        if info["status"] == "failed":
            blocking.append(name)
    return out, blocking


_ENVIRONMENTAL_RE = re.compile(
    r"(?i)(temporary failure in name resolution|name or service not known|nodename nor servname|"
    r"NameResolutionError|gaierror|could not be resolved|network is unreachable|"
    r"connection refused|ConnectionError|ConnectTimeout|no route to host|"
    r"plugin directory must be named)"
)


def _failed_ids(suite: Mapping[str, Any]) -> list[str]:
    return sorted({
        str(c["id"]) for c in suite.get("cases") or []
        if isinstance(c, Mapping) and isinstance(c.get("id"), str) and c.get("outcome") in ("failed", "error")
    })


def _is_environmental(case: Mapping[str, Any]) -> bool:
    text = f"{case.get('error_type') or ''} {case.get('message') or ''}"
    return bool(_ENVIRONMENTAL_RE.search(text))


def _classify_failures(
    suite: Mapping[str, Any], base_failed: set[str] | None
) -> dict[str, list[str]]:
    """Split failing test ids into regressions, environmental and pre-existing (also red on base)."""
    cases = {str(c["id"]): c for c in suite.get("cases") or [] if isinstance(c, Mapping) and isinstance(c.get("id"), str)}
    out: dict[str, list[str]] = {"failed_tests": [], "environmental_tests": [], "pre_existing_tests": []}
    for test_id in _failed_ids(suite):
        if _is_environmental(cases.get(test_id) or {}):
            out["environmental_tests"].append(test_id)
        elif base_failed is not None and test_id in base_failed:
            out["pre_existing_tests"].append(test_id)
        else:
            out["failed_tests"].append(test_id)
    for key in out:
        out[key] = out[key][:200]
    return out


def _base_failures(workdir: Path, base: str, targets: list[str], config: Mapping[str, Any]) -> set[str] | None:
    """Failing test ids when the same targets run on the base revision (None if it cannot run)."""
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="bundlebees-base-") as tmp:
            repo = acdd_cli._checkout(workdir, base, Path(tmp))
            present = [t for t in targets if (repo / t.split("::", 1)[0]).exists()]
            if not present:
                return set()
            return set(_failed_ids(_run_sandboxed(repo, present, config)))
    except Exception:  # noqa: BLE001 - no base comparison: every failure stays a regression
        return None


def _integration_base(workdir: Path, config: Mapping[str, Any], state_dir: Path, attempt_ids: list[str]) -> str | None:
    """The revision the combined branch is compared with for pre-existing failures.

    ``integrate_base`` (a ref) if set, else the remote default branch (origin/HEAD, origin/main,
    origin/master), else the first attempt's lease source_rev.
    """
    refs = [str(config.get("integrate_base") or "").strip()] if config.get("integrate_base") else []
    refs += ["origin/HEAD", "origin/main", "origin/master"]
    for ref in refs:
        if not ref or ref.startswith("-"):
            continue
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=workdir,
                              capture_output=True, text=True, env=child_env())
        sha = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):
            return sha
    for attempt_id in attempt_ids:
        try:
            rev = str(leases.load_lease(state_dir, attempt_id).get("source_rev") or "")
        except leases.LeaseError:
            continue
        if rev:
            return rev
    return None


def _cmd_integrate(ctx: Any, args: Any) -> int:
    state_dir = _state_dir(ctx, args)
    config = _config(ctx)
    attempt_ids = [_safe_attempt_id(a) for a in (getattr(args, "attempts", None) or [])]
    branch = _safe_branch(getattr(args, "branch", None) or config.get("branch"))
    workdir_value = getattr(args, "workdir", None) or config.get("worktree")
    workdir = _prepare_worktree(workdir_value, branch)
    run_id = getattr(args, "run", None)
    if not run_id:
        try:
            run_id = str(leases.load_lease(state_dir, attempt_ids[0]).get("run_id") or "default")
        except (leases.LeaseError, IndexError):
            run_id = "default"
    contract_items = acdd_cli.integrate_preflight(state_dir, attempt_ids)
    plan = _integration_plan(workdir, config, contract_items)
    run_targets: list[str] = []
    for name in _CATEGORIES:
        for target in plan[name]["targets"]:
            if target in plan[name]["missing"]:
                continue
            # pytest collects a file passed alongside a directory that holds it only once, and
            # not always both; pass each test exactly once and attribute cases by path afterwards.
            if any(_case_in(target, earlier) for earlier in run_targets if "::" not in earlier):
                continue
            run_targets = [t for t in run_targets if not _case_in(t, target)] + [target]

    base_rev = _integration_base(workdir, config, state_dir, attempt_ids)

    def run_suite(wd: Path) -> dict[str, Any]:
        evidence = dict(_run_sandboxed(wd, run_targets, config))
        evidence["targets"] = list(run_targets)
        evidence["suites"], evidence["blocking"] = _categorize(plan, evidence)
        failing = _failed_ids(evidence)
        # Only when something fails: re-run the same targets on the base to find pre-existing failures.
        base_failed = _base_failures(wd, base_rev, run_targets, config) if failing and base_rev else None
        evidence["failures"] = _classify_failures(evidence, base_failed)
        evidence["failures"]["base_compared"] = base_failed is not None
        return evidence

    try:
        record = revision.integrate_wave(
            state_dir,
            str(getattr(args, "wave", "")),
            workdir,
            attempt_ids,
            run_suite,
        )
    except revision.RevisionError as exc:
        raise _as_cli_error(exc) from exc
    accepted = bool(record.get("accepted"))
    suite = record.get("suite") if isinstance(record.get("suite"), Mapping) else {}
    failures = suite.get("failures") if isinstance(suite.get("failures"), Mapping) else None
    metric_fields: dict[str, Any] = {"regressions": int(record.get("regressions") or 0)}
    if failures is not None:
        metric_fields = {
            "regressions": len(failures.get("failed_tests") or []),
            "failed_tests": list(failures.get("failed_tests") or []),
            "environmental_tests": list(failures.get("environmental_tests") or []),
            "pre_existing_tests": list(failures.get("pre_existing_tests") or []),
        }
    elif record.get("problems") and not suite:
        metric_fields = {"regressions": 0, "failed_tests": [], "environmental_tests": [], "pre_existing_tests": []}
    _metric(
        state_dir, str(run_id), "integrate",
        wave_id=record.get("wave_id"), ok=accepted, attempts=list(record.get("attempts") or []),
        sha=record.get("head_sha"), **metric_fields,
    )
    integration = acdd_cli.record_integration(state_dir, contract_items, workdir, suite, record.get("head_sha"))
    _print("bundlebees integrate")
    _print(f"wave: {record.get('wave_id')}")
    _print(f"branch: {branch}")
    _print_sha(record.get("head_sha"))
    _print(f"attempts: {', '.join(record.get('attempts') or [])}")
    for attempt, problem in sorted((record.get("problems") or {}).items()):
        _print(f"problem: {attempt}: {problem}")
    if suite:
        _print(f"suite: exit {suite.get('exit_code')} via {suite.get('backend')} junit={json.dumps(suite.get('junit'))}")
    for name, info in (suite.get("suites") or {}).items():
        reason = f" ({info.get('reason')})" if info.get("reason") and info.get("status") != "passed" else ""
        _print(f"suite {name}: {info.get('status')}{reason}, {info.get('tests', 0)} tests")
    if failures is not None:
        _print(
            f"failures: regressions: {len(failures.get('failed_tests') or [])}, "
            f"environmental: {len(failures.get('environmental_tests') or [])}, "
            f"pre_existing: {len(failures.get('pre_existing_tests') or [])}"
            + ("" if failures.get("base_compared") or not _failed_ids(suite) else " (base not compared)")
        )
    for contract_id, status in integration:
        _print(f"contract: {contract_id}")
        _print(f"integration: {'not run' if status == 'unrun' else status}")
    _print(f"accepted: {'yes' if accepted else 'no'}")
    if not accepted and suite.get("output_tail"):
        _print(_tail(suite.get("output_tail"), 1500))
    return 0 if accepted else 1


def _cmd_metrics(ctx: Any, args: Any) -> int:
    state_dir = _state_dir(ctx, args)
    run = getattr(args, "run", None)
    note = getattr(args, "note", None)
    if note is not None:
        try:
            metrics.append_event(state_dir, run or "default", "intervention", note=str(note)[:500])
        except metrics.MetricsError as exc:
            raise _as_cli_error(exc) from exc
        _print("bundlebees metrics: human intervention recorded")
        return 0
    try:
        runs = [run] if run else metrics.list_runs(state_dir)
        summaries = [metrics.summarize(metrics.read_events(state_dir, r)) for r in runs]
    except metrics.MetricsError as exc:
        raise _as_cli_error(exc) from exc
    for summary, name in zip(summaries, runs):
        summary["run_id"] = summary.get("run_id") or name
    if getattr(args, "json", False):
        # Summaries hold only counts, rates and validated run ids; scrub() would
        # mask keys such as tokens_per_accepted.
        print(json.dumps(summaries, indent=2, sort_keys=True))
        return 0
    if not summaries:
        _print("bundlebees metrics: no runs recorded yet")
        return 0
    for summary in summaries:
        _print(metrics.format_summary(summary))
        _print("")
    return 0


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
    # Fixed text, never a value: printed directly so scrub() does not mask
    # the word after "token:" (0.1.0 printed "token: [redacted] configured").
    print(f"token: {'configured' if configured else 'not configured'}")

    reachable = False
    detail = "not checked"
    client = getattr(ctx, "bundlebees_client", None)
    if client is None:
        try:
            client = _client_for(ctx, args)
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

    options = _sandbox_options(_config(ctx))
    try:
        backend = sandbox.detect_backend(options["backend"], allow_weak=options["allow_weak"])
    except ValueError:
        backend = None
    if backend in sandbox.WEAK_BACKENDS:
        _print(f"sandbox: {backend} (WEAK override on: coordinator filesystem stays readable)")
    elif backend:
        _print(f"sandbox: {backend}")
    elif options["backend"] in sandbox.WEAK_BACKENDS:
        _print("sandbox: none (verify_isolation=unshare refused without the weak override)")
    elif options["allow_unsafe_local"]:
        _print("sandbox: none (verify_allow_unsafe_local=true: tests run UNISOLATED)")
    else:
        _print("sandbox: none (verify and integrate will refuse; install bubblewrap)")

    measured = _measure_schema_tokens()
    if measured is None:
        _print("schema_tokens: unavailable (install tiktoken to measure)")
    else:
        _print(f"schema_tokens: {measured} (budget 900 o200k)")
    return 0
