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
``--execute`` with an alias already stored in plugin config. ``attempts`` and
``status`` are read-only JSON views over the local attempt store: they never
touch the network and never need a configured credential.

All child processes receive ``security.child_env`` rather than the ambient
environment. Nothing here reads credentials from the environment. The
``doctor`` command asks Hermes's ``agent.secret_scope.get_secret`` whether a
token is configured and never prints its value. ``status`` performs exactly
one secret-store read: the launcher-token presence probe --
``client.token_configured()`` calls ``get_secret(BUNDLEBEES_LAUNCHER_TOKEN)``
to fill ``health.credential_configured``. The probe reads presence only,
never a value, never emits one, and fails closed when the secret scope is
unavailable. Settings come from ``ctx.get_config``; state lives in
``plugin_data_dir("bundlebees")``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import brief as _brief_mod
from . import coverage as _coverage
from . import delivery as _delivery
from . import campaign_cli
from . import job_cli
from . import acdd_cli, adapters, dispatch, leases, metrics, revision, sandbox
from .security import child_env, config_flag, contain_path, first_secret_rule, looks_like_secret, scrub

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


def _literal_branch(value: Any) -> str | None:
    """Return *value* only when it is a fully validated branch name.

    The branch must match the same grammar ``_safe_branch`` enforces, and a
    branch that is itself secret-shaped is never echoed literally.
    """
    if not isinstance(value, str):
        return None
    if not _BRANCH_RE.fullmatch(value):
        return None
    if ".." in value or value.endswith("/") or value.endswith(".") or "//" in value:
        return None
    if looks_like_secret(value):
        return None
    return value


def _literal_state_path(value: Any, state_dir: Path) -> str | None:
    """Return *value* only when it resolves strictly inside *state_dir*."""
    if not isinstance(value, (str, Path)):
        return None
    text = str(value)
    if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    try:
        root = Path(state_dir).expanduser().resolve()
        resolved = Path(text).expanduser().resolve()
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative.parts:
        return None
    return str(resolved)


def _print_branch(value: Any, suffix: str = "") -> None:
    """Print a validated branch literally; anything else keeps full scrubbing."""
    branch = _literal_branch(value)
    if branch is not None:
        print(f"branch: {branch}{suffix}")
    else:
        _print(f"branch: {value}{suffix}")


def _print_state_path(label: str, value: Any, state_dir: Path) -> None:
    """Print a state-directory path literally; outside paths stay redacted."""
    literal = _literal_state_path(value, state_dir)
    if literal is not None:
        print(f"{label}: {literal}")
    else:
        _print(f"{label}: {value}")


def _print_sha(sha: str | None, label: str = "sha") -> None:
    """Print a validated commit digest without passing it through scrub()."""
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
        print(f"{label}: {sha}")
    else:
        print(f"{label}: unavailable")


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
        action="extend",
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

    coverage_cmd = subparsers.add_parser("coverage", help="Classify queued backlog tasks against open/merged PRs (read-only).")
    coverage_cmd.add_argument("--backlog", required=True)
    coverage_cmd.add_argument("--candidates", required=True)
    coverage_cmd.add_argument("--json", action="store_true")
    coverage_cmd.set_defaults(command="coverage")
    reviewer = subparsers.add_parser("reviewer", help="Enrolled reviewer identities for signed REVIEW evidence.")
    reviewer_sub = reviewer.add_subparsers(dest="reviewer_command", required=True)
    rv_enroll = reviewer_sub.add_parser("enroll", help="Create (or adopt) a reviewer key and record the identity.")
    rv_enroll.add_argument("--name", required=True, help="Stable reviewer name (also the record slug).")
    rv_enroll.add_argument("--key-file", default=None, help="Key path (default: <state>/reviewers/<slug>.key, 0600).")
    rv_enroll.add_argument("--state-dir", default=None)
    rv_enroll.set_defaults(command="reviewer")
    rv_list = reviewer_sub.add_parser("list", help="List enrolled reviewer identities (read-only).")
    rv_list.add_argument("--state-dir", default=None)
    rv_list.set_defaults(command="reviewer")

    owner = subparsers.add_parser("owner", help="Enrolled owner authority for board/feedback writes (E2/E5).")
    owner_sub = owner.add_subparsers(dest="owner_command", required=True)
    ow_enroll = owner_sub.add_parser("enroll", help="Create (or adopt) an owner key and record its authority.")
    ow_enroll.add_argument("--name", required=True)
    ow_enroll.add_argument("--scope", default="board,feedback", help="Comma-separated scopes (board,feedback,rail_clear,approve).")
    ow_enroll.add_argument("--expires", default="never", help="ISO-8601 date or never.")
    ow_enroll.add_argument("--key-file", default=None)
    ow_enroll.add_argument("--state-dir", default=None)
    ow_enroll.set_defaults(command="owner")
    ow_list = owner_sub.add_parser("list", help="List enrolled owner authorities (read-only).")
    ow_list.add_argument("--state-dir", default=None)
    ow_list.set_defaults(command="owner")

    deliver = subparsers.add_parser(
        "deliver",
        help="Reconcile pending deliveries: open missing PRs for verified branches (idempotent).",
    )
    deliver.add_argument("--state-dir", default=None, help="Plugin data directory (defaults to Hermes plugin data).")
    deliver.add_argument("--dry-run", action="store_true", help="Report what would happen; touch nothing.")
    deliver.set_defaults(command="deliver")

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
        help="Report stack parity and health; never prints a credential value.",
    )
    doctor.add_argument("--json", action="store_true", help="Print the machine-readable doctor report.")
    doctor.add_argument(
        "--check-parity",
        action="store_true",
        help="Exit nonzero when installed, loaded or deployed revisions diverge from reviewed main.",
    )
    doctor.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    doctor.set_defaults(command="doctor")

    attempts_cmd = subparsers.add_parser(
        "attempts",
        help="List recorded attempts from the local store as JSON (read-only).",
    )
    attempts_cmd.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    attempts_cmd.set_defaults(command="attempts")

    status_cmd = subparsers.add_parser(
        "status",
        help="Summarize in-flight work and the bee cap as JSON (read-only).",
    )
    status_cmd.add_argument("--state-dir", default=None, help="Plugin data directory override.")
    status_cmd.set_defaults(command="status")

    from . import rails_cli
    rails_cli.add_parsers(subparsers)
    ingest = subparsers.add_parser('rails-import', help='Import one bounded page from a configured Rails source.')
    ingest.add_argument('--source', choices=('bundlebees','outbox','queue','hermes','launcher'), required=True)
    ingest.add_argument('--limit', type=int, default=100)
    acdd_cli.add_parsers(subparsers)
    job_cli.add_parsers(subparsers)
    campaign_cli.add_parsers(subparsers)
    return group

def _cmd_deliver(ctx: Any, args: Any) -> int:
    """Reconcile pending deliveries (open missing PRs). Idempotent; safe on a timer."""
    state_dir = Path(getattr(args, "state_dir", None) or settings.state_dir(ctx))
    rows = _delivery.pending(state_dir)
    if not rows:
        _print("deliver: nothing pending (every pushed delivery already has a PR)")
        return 0
    result = _delivery.sweep(state_dir, dry_run=getattr(args, "dry_run", False))
    for row in result["opened"]:
        _print(f"opened: {row['branch']} -> {row['pr_url']}")
    for row in result["existing"]:
        _print(f"existing: {row['branch']} -> {row['pr_url']}")
    for row in result["blocked"]:
        _print(f"blocked: {row['branch']} - {row['reason']} (re-run verify)")
    for row in result["errors"]:
        _print(f"error: {row['branch']} - {row['error']['message']}")
    _print(f"deliver: opened={len(result['opened'])} existing={len(result['existing'])} "
           f"blocked={len(result['blocked'])} errors={len(result['errors'])}")
    return 0 if not result["errors"] else 2


def _cmd_owner(ctx: Any, args: Any) -> int:
    """Enrolled owner authority (rizzstack.owner-authority/1)."""
    from . import owners as _owners
    state_dir = _state_dir(ctx, args)
    sub = getattr(args, "owner_command", None)
    if sub == "enroll":
        try:
            record = _owners.enroll(state_dir, args.name,
                                    scope=[s for s in str(args.scope).split(",") if s.strip()],
                                    expires=getattr(args, "expires", "never"),
                                    key_path=getattr(args, "key_file", None))
        except _owners.OwnerAuthorityError as exc:
            raise _as_cli_error(exc)
        _print("bundlebees owner enroll")
        _print(f"owner: {record['name']}")
        _print(f"key_id: {record['key_id']}")
        _print(f"scope: {', '.join(record['scope'])}")
        _print(f"expires_at: {record['expires_at']}")
        _print(f"key_file: {record['key_path']} (0600; the key never leaves this file)")
        return 0
    if sub == "list":
        directory = state_dir / "owners"
        rows = sorted(directory.glob("*.json")) if directory.exists() else []
        if not rows:
            _print("owner: none enrolled")
            return 0
        for path in rows:
            record = json.loads(path.read_text(encoding="utf-8"))
            _print(f"{record.get('name')}  key_id={record.get('key_id')}  scope={','.join(record.get('scope') or [])}")
        return 0
    _print("owner: unknown subcommand")
    return 2


def _cmd_reviewer(ctx: Any, args: Any) -> int:
    """Enrolled reviewer identities (rizzstack.reviewer/1)."""
    from . import reviewers as _reviewers
    state_dir = Path(getattr(args, "state_dir", None) or _state_dir(ctx, args))
    sub = getattr(args, "reviewer_command", None)
    if sub == "enroll":
        try:
            record = _reviewers.enroll(state_dir, args.name, key_path=getattr(args, "key_file", None))
        except _reviewers.ReviewerError as exc:
            raise _as_cli_error(exc)
        _print("bundlebees reviewer enroll")
        _print(f"reviewer: {record['name']}")
        _print(f"key_id: {record['key_id']}")
        _print(f"key_file: {record['key_path']} (0600; the key never leaves this file)")
        _print("use: hermes bundlebees tdd review <contract> --reviewer <name> --reviewer-key <key_file> --approve --note ...")
        return 0
    if sub == "list":
        directory = state_dir / "reviewers"
        rows = sorted(directory.glob("*.json")) if directory.exists() else []
        if not rows:
            _print("reviewer: none enrolled")
            return 0
        for path in rows:
            record = json.loads(path.read_text(encoding="utf-8"))
            _print(f"{record.get('name')}  key_id={record.get('key_id')}  fingerprint={record.get('fingerprint')}")
        return 0
    _print("reviewer: unknown subcommand")
    return 2


def _cmd_coverage(ctx: Any, args: Any) -> int:
    import json as _json
    backlog = _json.loads(Path(args.backlog).read_text(encoding="utf-8"))
    raw = _json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    tasks = []
    for row in backlog.get("slices") or backlog.get("tasks") or []:
        brief = row.get("brief") or {}
        tasks.append(_coverage.Task.build(
            task_id=str(row.get("id") or row.get("task_id")), repo=str(row.get("project") or row.get("repo") or ""),
            assigned_files=brief.get("assigned_files") or row.get("assigned_files") or [],
            tests=brief.get("tests") or row.get("tests") or [], contract=str(brief.get("source_rev") or ""),
            status=str(row.get("status") or ""), attempt_ids=row.get("attempt_ids") or [],
            attempt_states=row.get("attempt_states") or {},
            blocked_by=[d for d in (row.get("depends_on") or []) if d],
            superseded_by=str(row.get("superseded_by") or "")))
    candidates = [_coverage.Candidate.build(repo=str(c["repo"]), number=int(c["number"]), head_sha=str(c.get("head_sha") or ""),
                  merged=bool(c.get("merged")), files=c.get("files") or [], evidence=str(c.get("evidence") or ""),
                  supersedes=c.get("supersedes") or ()) for c in raw]
    report = _coverage.reconcile(tasks, candidates)
    _print(_json.dumps(report, indent=1, sort_keys=True) if getattr(args, "json", False) else _coverage.format_table(report))
    return 0


def _dispatch(ctx: Any, args: Any) -> int:
    command = getattr(args, "command", None)
    try:
        if command == 'rails':
            from . import rails_cli
            return rails_cli.cmd_rails(ctx, args)
        if command == 'rails-import':
            return _rails_import(ctx, args)
        if command == "owner":
            return _cmd_owner(ctx, args)
        if command == "reviewer":
            return _cmd_reviewer(ctx, args)
        if command == "coverage":
            return _cmd_coverage(ctx, args)
        if command == "verify":
            if getattr(args, "commit_local", False):
                return _cmd_verify_commit_local(ctx, args)
            return _cmd_verify(ctx, args)
        if command == "deliver":
            return _cmd_deliver(ctx, args)
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
        if command == "attempts":
            return _cmd_attempts(ctx, args)
        if command == "status":
            return _cmd_status(ctx, args)
        if command == "contract":
            return acdd_cli.cmd_contract(ctx, args)
        if command == "tdd":
            return acdd_cli.cmd_tdd(ctx, args)
        if command == "job":
            if getattr(args, "command", "") == "queue":
                return job_cli.cmd_queue(ctx, args, state_dir=state_dir, client_factory=lambda: _client_for(ctx),
                                         provisioner_factory=lambda: __import__("bundlebees_plugin.provisioning", fromlist=["from_client"]).from_client(_config(ctx), _client_for(ctx)))
            return job_cli.cmd_job(
                ctx,
                args,
                state_dir=_state_dir(ctx, args),
                client_factory=lambda: _client_for(ctx),
                provisioner_factory=lambda: __import__("bundlebees_plugin.provisioning", fromlist=["from_client"]).from_client(
                    _config(ctx), _client_for(ctx)
                ),
            )
        if command == "repo":
            return job_cli.cmd_repo(ctx, args, state_dir=_state_dir(ctx, args))
        if command == "campaign":
            return campaign_cli.cmd_campaign(ctx, args, state_dir=_state_dir(ctx, args))
        raise CliError(
            "invalid_argument",
            "unknown bundlebees command",
            "Use one of: verify, integrate, lease, metrics, deploy, doctor, "
            "contract, tdd, job, repo, campaign, attempts, status.",
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
# Read-only attempt-store views (no network, no credentials)
# ---------------------------------------------------------------------------

_ATTEMPT_DISPATCHED = "dispatched"
_ATTEMPT_VERIFIED = "verified"
_ATTEMPT_FAILED = "failed"
_ATTEMPT_ACCEPTED = "accepted"
#: Dispatched, no outcome ever recorded, and past the window in which an
#: outcome could plausibly still arrive. This is not a success/failure claim:
#: it says the store has no verified outcome for the attempt, so counting it
#: as in-flight would report work that is not happening.
_ATTEMPT_EXPIRED = "expired"

#: How long a dispatched attempt with no recorded outcome still counts as
#: in-flight. A lease is written at dispatch and is immutable; nothing closes
#: it if the bee dies with its workspace, so without a window every abandoned
#: dispatch would stay "in flight" forever and the fleet view would report
#: hundreds of bees that do not exist.
ATTEMPT_TTL_S = 12 * 60 * 60

#: Work that is still in a bee's hands. ``verified`` is deliberately NOT here:
#: a verify result means the bee already finished and the result is waiting for
#: acceptance, so counting it inflates the fleet view with work that is over.
#: (104 such rows sat in this store, all of them reported as active bees.)
_IN_FLIGHT_STATES = frozenset({_ATTEMPT_DISPATCHED})


def _age_s(stamp: Any, now: _dt.datetime) -> float | None:
    """Seconds between an ISO-8601 Zulu stamp and ``now``; None if unparseable."""
    text = str(stamp or "").strip()
    if len(text) < 20:
        return None
    try:
        parsed = _dt.datetime.strptime(text[:20], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return (now - parsed.replace(tzinfo=_dt.timezone.utc)).total_seconds()


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _latest_stamp(*values: Any) -> str:
    """The newest well-formed ISO-8601 Zulu stamp; equal formats sort lexically."""
    best = ""
    for value in values:
        text = str(value or "").strip()
        if len(text) >= 20 and text > best:
            best = text
    return best


def _accepted_waves(state_dir: Path) -> dict[str, str]:
    """Attempt id -> the accepted wave's stamp, from the durable wave records.

    A wave file that cannot be read is skipped here: this is a reporting view,
    while the paths that decide whether to integrate again use ``read_wave``,
    which treats damage as a refusal rather than as absence.
    """
    accepted: dict[str, str] = {}
    try:
        paths = sorted((Path(state_dir) / "waves").glob("*.json"))
    except OSError:
        return accepted
    for path in paths:
        record = revision.load_wave(state_dir, path.stem)
        if not record or record.get("accepted") is not True:
            continue
        stamp = str(record.get("at") or "")
        for item in record.get("attempts") or []:
            if isinstance(item, str) and item:
                name = item
            elif isinstance(item, Mapping) and isinstance(item.get("attempt_id"), str):
                name = str(item["attempt_id"])
            else:
                continue
            accepted[name] = _latest_stamp(accepted.get(name), stamp)
    return accepted


def _lease_records(state_dir: Path) -> list[dict[str, Any]]:
    """Verified lease records from the local attempt store, oldest first.

    A record that fails its MAC check is skipped, never listed, and never an
    error: this view is read-only, and verify keeps refusing such attempts.
    """
    records: list[dict[str, Any]] = []
    try:
        paths = sorted((Path(state_dir) / "leases").glob("*.json"))
    except OSError:
        return records
    for path in paths:
        try:
            records.append(leases.load_lease(state_dir, path.stem))
        except Exception:  # noqa: BLE001 - an unreadable lease is skipped, not fatal
            continue
    records.sort(
        key=lambda record: (
            str(record.get("created_at") or ""),
            str(record.get("attempt_id") or ""),
        )
    )
    return records


def _attribution_route(state_dir: Path, attempt_id: str) -> tuple[str | None, str | None]:
    """Route and model from one verified attribution intent, else ``(None, None)``.

    The view has no live launcher facts, so the audit re-checks the recorded
    intent itself: its signature, its unique binding to this attempt and the
    tuple it recorded. A missing attribution store, an unreadable or tampered
    intent, an adopted attempt (no bound intent) and two bound intents all
    yield null rather than a guess. The route is never inferred from a model.
    """
    from . import attribution

    root = Path(state_dir) / "attribution"
    if not root.is_dir():
        return None, None
    try:
        intents = attribution.load_intents(state_dir)
    except Exception:  # noqa: BLE001 - an unreadable store cannot attribute anything
        return None, None
    bound = [
        intent for intent in intents
        if str(intent.get("attempt_id") or "") == attempt_id
    ]
    if len(bound) != 1:
        return None, None
    intent = bound[0]
    facts = {field: intent.get(field) for field in attribution.FIELDS}
    try:
        verdict = attribution.verify_attribution(
            state_dir, attempt_id=attempt_id, facts=facts
        )
    except Exception:  # noqa: BLE001 - any refusal leaves the row unattributed
        return None, None
    if not isinstance(verdict, Mapping) or verdict.get("ok") is not True:
        return None, None
    route = _text_or_none(intent.get("route"))
    model = _text_or_none(intent.get("model")) or _text_or_none(intent.get("attributed_model"))
    return route, model


def _attempt_rows(state_dir: Path) -> list[dict[str, Any]]:
    """One row per recorded attempt, derived from local evidence only.

    The state comes from the coordinator's own records -- an accepted wave
    record first, then the verify result -- so the view never asks the launcher
    and never widens what the lease recorded. Every value passes through
    ``scrub`` before it can reach the output. A validated attempt id survives
    the scrub as evidence, not a credential -- unless it is itself
    secret-shaped: then the scrubbed form always wins and the raw id is never
    re-inserted into the row.
    """
    from . import verify_local

    accepted = _accepted_waves(state_dir)
    rows: list[dict[str, Any]] = []
    for record in _lease_records(state_dir):
        attempt_id = str(record.get("attempt_id") or "")
        if not attempt_id:
            continue
        lease: Mapping[str, Any] = record if isinstance(record, Mapping) else {}
        brief = lease.get("brief") if isinstance(lease.get("brief"), Mapping) else {}
        result = verify_local.read_record(state_dir, attempt_id) or {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        updated_at = _latest_stamp(
            record.get("created_at"), result.get("at"), accepted.get(attempt_id)
        )
        if attempt_id in accepted:
            state = _ATTEMPT_ACCEPTED
        elif result.get("ok") is True:
            state = _ATTEMPT_VERIFIED
        elif result.get("ok") is False:
            state = _ATTEMPT_FAILED
        elif (age := _age_s(updated_at, _dt.datetime.now(_dt.timezone.utc))) is not None and (
            age > ATTEMPT_TTL_S
        ):
            # No outcome in the store and the dispatch is old: the workspace is
            # long gone, so this is not in-flight work.
            state = _ATTEMPT_EXPIRED
        else:
            state = _ATTEMPT_DISPATCHED
        route, attributed_model = _attribution_route(state_dir, attempt_id)
        row = {
            "attempt_id": attempt_id,
            "state": state,
            "branch": _text_or_none(record.get("branch")),
            "model": _text_or_none(brief.get("model")) or _text_or_none(evidence.get("model")),
            "route": route,
            "attributed_model": attributed_model,
            "project": _text_or_none(brief.get("project")),
            # Dispatch identity: the brief pins the job the work was dispatched
            # for, and the fleet view cannot link an attempt to anything without
            # it. ``repo`` is only what the brief or lease actually names - the
            # project label is not a repo slug, so it is never substituted.
            "job_id": _text_or_none(brief.get("job_id")) or _text_or_none(lease.get("job_id")),
            "repo": _text_or_none(brief.get("repo")) or _text_or_none(lease.get("repo")),
            "created_at": str(record.get("created_at") or ""),
            "updated_at": updated_at,
        }
        clean = scrub(row)
        if (
            _ATTEMPT_ID_RE.fullmatch(attempt_id)
            and ".." not in attempt_id
            and not looks_like_secret(attempt_id)
        ):
            clean["attempt_id"] = attempt_id
        validated_branch = _literal_branch(row.get("branch"))
        if validated_branch is not None:
            clean["branch"] = validated_branch
        rows.append(clean)
    return rows


def _cmd_attempts(ctx: Any, args: Any) -> int:
    """``attempts``: the local attempt store as JSON; strictly read-only."""
    state_dir = _state_dir(ctx, args)
    print(json.dumps({"attempts": _attempt_rows(state_dir)}, indent=2, sort_keys=True))
    return 0


def _cmd_status(ctx: Any, args: Any) -> int:
    """``status``: in-flight work, the bee cap, and credential-free launcher health."""
    from . import grants as grants_mod

    state_dir = _state_dir(ctx, args)
    config = _config(ctx)
    rows = _attempt_rows(state_dir)
    in_flight = [row["attempt_id"] for row in rows if row["state"] in _IN_FLIGHT_STATES]
    payload = {
        "in_flight": len(in_flight),
        "in_flight_attempts": in_flight,
        "bee_cap": int(grants_mod.GLOBAL_MAX_BEES),
        "health": {
            "adapter": str(config.get("adapter") or "orbit-loopback"),
            "launcher_url_configured": bool(str(config.get("launcher_url") or "").strip()),
            "credential_configured": bool(_token_configured(ctx)),
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


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
    return {path: text for path, (text, _) in _added_lines_detail(patch).items()}


def _added_lines_detail(patch: str) -> dict[str, tuple[str, list[int]]]:
    """Lines each file gains in a unified diff, keyed by its new path, paired
    with each line's 1-based line number in the new file (from ``@@`` hunk
    headers; a diff without them falls back to numbering within the added
    lines). The text is exactly what :func:`_added_lines` returns.
    """
    lines_by_path: dict[str, list[str]] = {}
    numbers_by_path: dict[str, list[int]] = {}
    current: str | None = None
    new_line = 1
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            target = line[4:].strip().split("\t", 1)[0]
            current = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
            if current is not None:
                lines_by_path.setdefault(current, [])
                numbers_by_path.setdefault(current, [])
                new_line = 1
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1))
        elif current is not None and line.startswith("+"):
            lines_by_path[current].append(line[1:])
            numbers_by_path[current].append(new_line)
            new_line += 1
        elif current is not None and not line.startswith("-") and not line.startswith("\\"):
            # Context (or hunk padding): only the new-file numbering moves.
            new_line += 1
    return {
        path: ("\n".join(lines_by_path[path]), numbers_by_path[path])
        for path in lines_by_path
    }


def _mask_excerpt(line: str | None, limit: int = 240) -> str | None:
    """Mask every secret-shaped span in *line* with the fixed ``[redacted]``
    placeholder (the scrubber's own predicate does the masking, so a span that
    trips the scan can never survive into the refusal evidence)."""
    if line is None:
        return None
    return scrub(line)[:limit]


_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz")
_GZIP_MAGIC = b"\x1f\x8b"
#: Total uncompressed bytes one archive may yield during the scan. A worker
#: archive is untrusted input; without a bound a small gzip can exhaust the
#: coordinator's memory. Over the bound the scan fails closed with no evidence.
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class _ArchiveParseError(Exception):
    pass


class _UnsafeArchiveMember(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _looks_like_archive(rel: str, data: bytes) -> bool:
    """True for a tar path suffix or gzip magic (a gzip blob is treated as a
    possible tar.gz; if it does not parse the scan fails closed)."""
    return rel.lower().endswith(_TAR_SUFFIXES) or data[:2] == _GZIP_MAGIC


def _unsafe_archive_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", name):
        return True
    return ".." in re.split(r"[\\/]+", name)


def _read_tar_members(data: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            total = 0
            for member in archive.getmembers():
                if _unsafe_archive_member(member.name):
                    raise _UnsafeArchiveMember(member.name)
                if not member.isfile():
                    continue
                total += member.size
                if total > _MAX_ARCHIVE_BYTES:
                    raise _ArchiveParseError("archive expands beyond the scan bound")
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                members[member.name] = handle.read()
    except _UnsafeArchiveMember:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise _ArchiveParseError(str(exc)) from exc
    return members


def _added_member_lines(base_text: str, new_text: str) -> list[tuple[int, str]]:
    """Lines added to one archive member, with their 1-based line numbers in the
    new member (the same added-line principle as a unified diff)."""
    base_lines = base_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(None, base_lines, new_lines, autojunk=False)
    added: list[tuple[int, str]] = []
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend((index + 1, new_lines[index]) for index in range(j1, j2))
    return added


def _archive_secret_trips(rel: str, base_bytes: bytes, new_bytes: bytes) -> list[dict[str, Any]]:
    """Pure archive comparison: scan only member lines the new archive adds
    relative to the base blob. An unparseable archive, an unsafe member path, or
    a differing member larger than the scan cap fails closed with no evidence.
    """
    base_members: dict[str, bytes] = {}
    if base_bytes:
        try:
            base_members = _read_tar_members(base_bytes)
        except _UnsafeArchiveMember as exc:
            return [{"file": rel, "member": exc.name, "line_no": None, "rule": None, "excerpt": None}]
        except _ArchiveParseError:
            return [{"file": rel, "line_no": None, "rule": None, "excerpt": None}]
    try:
        new_members = _read_tar_members(new_bytes)
    except _UnsafeArchiveMember as exc:
        return [{"file": rel, "member": exc.name, "line_no": None, "rule": None, "excerpt": None}]
    except _ArchiveParseError:
        return [{"file": rel, "line_no": None, "rule": None, "excerpt": None}]

    trips: list[dict[str, Any]] = []
    for name, content in new_members.items():
        base_content = base_members.get(name)
        if base_content == content:
            continue
        if len(content) > _MAX_SCAN_BYTES:
            trips.append({"file": rel, "member": name, "line_no": None, "rule": None, "excerpt": None})
            continue
        base_text = base_content.decode("utf-8", errors="replace") if base_content is not None else ""
        added = _added_member_lines(base_text, content.decode("utf-8", errors="replace"))
        private = next(((no, line) for no, line in added if _PRIVATE_KEY_RE.search(line)), None)
        if private is not None:
            trips.append(
                {
                    "file": rel,
                    "member": name,
                    "line_no": private[0],
                    "rule": "private_key",
                    "excerpt": _mask_excerpt(private[1]),
                }
            )
            continue
        for line_no, line in added:
            rule = first_secret_rule(line)
            if rule:
                trips.append(
                    {
                        "file": rel,
                        "member": name,
                        "line_no": line_no,
                        "rule": rule,
                        "excerpt": _mask_excerpt(line),
                    }
                )
                break
    return trips


def _git_blob_bytes(workdir: Path, source_rev: str, rel: str) -> bytes | None:
    """Read one base-revision blob as raw bytes for archive comparison. Returns
    ``None`` when the revision cannot be resolved; an absent path is an empty
    base (every member line is added). Used only by the real verify path."""
    env = child_env({"GIT_TERMINAL_PROMPT": "0"})
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{source_rev}^{{commit}}"],
            cwd=str(workdir),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if rev.returncode != 0:
            return None
        proc = subprocess.run(
            ["git", "show", f"{source_rev}:{rel}"],
            cwd=str(workdir),
            env=dict(env),
            capture_output=True,
            text=False,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return b""
    return proc.stdout


def _secret_trips(
    workdir: Path,
    files: Iterable[str],
    patch: str | None = None,
    *,
    source_rev: str | None = None,
    base_blobs: Mapping[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    """Structured secret-scan trips: ``file``, 1-based ``line_no``, ``rule``
    id, masked ``excerpt``. Flags exactly the files :func:`_secret_scan` flags;
    evidence is enrichment on top of the same decision, never a separate one.

    An oversize tar archive is compared member by member against the base blob
    (``source_rev`` on the real verify path, or an explicit ``base_blobs`` in
    tests) and only added member lines are scanned. Every other oversize file
    still fails closed.
    """
    trips: list[dict[str, Any]] = []
    added = _added_lines_detail(patch) if patch else {}
    for rel in files:
        if rel in added:
            text, line_numbers = added[rel]
            if len(text.encode("utf-8", errors="replace")) > _MAX_SCAN_BYTES:
                trips.append({"file": rel, "line_no": None, "rule": None, "excerpt": None})
                continue
        else:
            path = Path(contain_path(rel, str(workdir)))
            try:
                if path.stat().st_size > _MAX_SCAN_BYTES:
                    try:
                        with path.open("rb") as handle:
                            head = handle.read(2)
                    except OSError:
                        trips.append({"file": rel, "line_no": None, "rule": None, "excerpt": None})
                        continue
                    if _looks_like_archive(rel, head):
                        try:
                            new_bytes = path.read_bytes()
                        except OSError:
                            trips.append({"file": rel, "line_no": None, "rule": None, "excerpt": None})
                            continue
                        if base_blobs is not None:
                            base_bytes: bytes | None = base_blobs.get(rel, b"")
                        elif source_rev:
                            base_bytes = _git_blob_bytes(workdir, source_rev, rel)
                        else:
                            base_bytes = b""
                        if base_bytes is None:
                            trips.append({"file": rel, "line_no": None, "rule": None, "excerpt": None})
                        else:
                            trips.extend(_archive_secret_trips(rel, base_bytes, new_bytes))
                    else:
                        trips.append({"file": rel, "line_no": None, "rule": None, "excerpt": None})
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            line_numbers = None
        if _PRIVATE_KEY_RE.search(text):
            numbered = (
                list(zip(line_numbers, text.splitlines()))
                if line_numbers is not None
                else list(enumerate(text.splitlines(), start=1))
            )
            match = next(((no, line) for no, line in numbered if _PRIVATE_KEY_RE.search(line)), None)
            trips.append(
                {
                    "file": rel,
                    "line_no": match[0] if match else None,
                    "rule": "private_key",
                    "excerpt": _mask_excerpt(match[1]) if match else None,
                }
            )
            continue
        for index, line in enumerate(text.splitlines()):
            rule = first_secret_rule(line)
            if rule:
                line_no = line_numbers[index] if line_numbers else index + 1
                trips.append({"file": rel, "line_no": line_no, "rule": rule, "excerpt": _mask_excerpt(line)})
                break
    return trips


def _secret_refusal(flagged: list[str], trips: list[dict[str, Any]]) -> str:
    """Refusal text: the original file list plus one diagnosable entry per trip."""
    parts: list[str] = []
    for trip in trips:
        detail = trip["file"]
        if trip.get("member"):
            detail += f" member={trip['member']}"
        if trip["line_no"] is not None:
            detail += f" line={trip['line_no']}"
        if trip["rule"]:
            detail += f" rule={trip['rule']}"
        if trip["excerpt"] is not None:
            detail += f' excerpt="{trip["excerpt"]}"'
        if (
            trip["line_no"] is None
            and not trip["rule"]
            and trip["excerpt"] is None
            and not trip.get("member")
        ):
            detail += " (no line evidence: file exceeds the scan size limit)"
        parts.append(detail)
    return "secret scan found secret-like content in: " + ", ".join(flagged) + "; trips: " + "; ".join(parts)


def _secret_scan(workdir: Path, files: Iterable[str], patch: str | None = None) -> list[str]:
    """Flag files whose patch-added lines look secret-like.

    With a patch, only the lines it adds are scanned: content already on source_rev is
    not the worker's output (and bundlebees' own sources mention tokens by design). A
    file the diff does not describe line by line is scanned whole (fail closed).
    """
    return sorted({trip["file"] for trip in _secret_trips(workdir, files, patch)})


def _sandbox_options(config: Mapping[str, Any]) -> dict[str, Any]:
    from .execution import sandbox_options

    try:
        return sandbox_options(config)
    except dispatch.PayloadError as exc:
        raise _as_cli_error(exc) from exc


_NODE_TARGET_SUFFIXES = (".mjs", ".cjs", ".js")
_NODE_MISSING_MARKERS = (
    "enoent",
    "no such file or directory",
    "not found",
    "command not found",
)
_FIX_TEST_TARGETS = (
    "Pass either Python test files/node ids or Node .mjs/.cjs/.js test files, not both."
)
_FIX_NODE_MISSING = "Install Node in the sandbox or pass Python test targets."


def _pytest_targets(targets: list[str], workdir: Path) -> list[str]:
    """Keep only the approved targets pytest can collect; drop non-Python fixtures.

    This is the legacy ACDD path: a contract's ``scope.test_files`` may name
    non-Python server fixtures alongside its Python tests. Verify itself uses
    the strict selector below, where dropping a target is a refusal.
    """
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


def _select_test_runner(targets: list[str], workdir: Path) -> tuple[str, list[str]]:
    """Choose exactly one runner for the approved targets; never drop one.

    Python targets keep pytest's existing selection rules. Node targets must be
    existing ``.mjs``/``.cjs``/``.js`` files. A mixed or unrecognised set is a
    refusal, because silently dropping a target would report a false pass.
    """
    python_targets: list[str] = []
    node_targets: list[str] = []
    offenders: list[str] = []
    for target in targets:
        if not isinstance(target, str):
            offenders.append(repr(target))
            continue
        text = target.strip()
        if not text:
            continue
        if "::" in text:
            python_targets.append(text)
            continue
        try:
            candidate = Path(contain_path(text.split("::", 1)[0] or ".", str(workdir)))
        except ValueError:
            offenders.append(text)
            continue
        if text == "." or text.endswith(".py"):
            python_targets.append(text)
            continue
        if candidate.is_dir():
            python_targets.append(text)
            continue
        if text.lower().endswith(_NODE_TARGET_SUFFIXES) and candidate.is_file():
            node_targets.append(text)
            continue
        if text.lower().endswith(_NODE_TARGET_SUFFIXES):
            offenders.append(f"{text} (file does not exist)")
        else:
            offenders.append(f"{text} (unknown extension)")
    if offenders or (python_targets and node_targets):
        named = offenders or [*python_targets, *node_targets]
        raise CliError(
            "mixed_test_targets",
            "test targets are mixed or unrecognised: " + ", ".join(named),
            _FIX_TEST_TARGETS,
        )
    if node_targets:
        return "node-test", node_targets
    if python_targets:
        return "pytest", python_targets
    raise CliError(
        "no_test_targets",
        "none of the approved test files can be collected by pytest",
        "Reference at least one Python test file in scope.test_files.",
    )


def _node_runner_missing(evidence: Mapping[str, Any]) -> bool:
    if evidence.get("exit_code") != 127:
        return False
    tail = str(evidence.get("output_tail") or "").lower()
    return any(marker in tail for marker in _NODE_MISSING_MARKERS)


def _test_argv_text(argv: list[str]) -> str:
    return " ".join(argv)[:2000]


def _run_sandboxed(
    workdir: Path,
    targets: list[str],
    config: Mapping[str, Any],
    *,
    strict_targets: bool = False,
) -> dict[str, Any]:
    """Run the selected test runner on a disposable copy of *workdir*; evidence only.

    ``strict_targets`` is the verify contract: every named target must be a
    Python or Node test, and a mixed/unrecognised set is refused. ACDD passes
    contract ``scope.test_files``, which may include non-test fixtures, so its
    callers keep the legacy pytest-only selection.
    """
    if strict_targets:
        runner, selected = _select_test_runner(list(targets), Path(workdir))
    else:
        pytest_targets = _pytest_targets(list(targets), Path(workdir))
        if pytest_targets:
            runner, selected = "pytest", pytest_targets
        else:
            runner, selected = _select_test_runner(list(targets), Path(workdir))
    if runner == "node-test":
        argv = ["node", "--test", *selected]
    else:
        argv = sandbox.pytest_argv(selected)
    try:
        result = sandbox.run_in_sandbox(workdir, argv, **_sandbox_options(config))
    except sandbox.SandboxUnavailable as exc:
        raise CliError(exc.code, exc.error, exc.fix) from exc
    except OSError as exc:
        if runner == "node-test" and (
            isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == 2
        ):
            raise CliError(
                "node_runner_unavailable",
                "node is not resolvable inside the sandbox",
                _FIX_NODE_MISSING,
            ) from exc
        raise
    except ValueError as exc:
        raise CliError(
            "invalid_argument",
            f"unknown verify_isolation backend: {exc}",
            "Set verify_isolation to auto, bwrap or firejail (unshare needs the weak override).",
        ) from exc
    if runner == "node-test" and _node_runner_missing(result):
        raise CliError(
            "node_runner_unavailable",
            "node is not resolvable inside the sandbox: " + _tail(result.get("output_tail")),
            _FIX_NODE_MISSING,
        )
    evidence = dict(result)
    evidence["runner"] = runner
    evidence["test_argv"] = _test_argv_text(argv)
    return evidence


def _run_tests(workdir: Path, tests: list[str], config: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _run_sandboxed(workdir, tests, config, strict_targets=True)
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


def _origin_repo(workdir: Path) -> str:
    """owner/repo from the worktree's origin URL (the delivery record's PR target)."""
    out = _run_git(["remote", "get-url", "origin"], workdir, check=False).stdout.strip()
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", out)
    if not m:
        raise _delivery.DeliveryError("origin_unparsable", f"cannot derive owner/repo from origin URL: {out[:120]}",
                            "Add an origin remote and rerun verify.")
    return f"{m.group(1)}/{m.group(2)}"


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


_VERIFY_REASON_LIMIT = 2000
_VERIFY_REASON_CLIP = "...[clipped]"


def _clip_verify_reason(text: str) -> str:
    if len(text) <= _VERIFY_REASON_LIMIT:
        return text
    return text[: _VERIFY_REASON_LIMIT - len(_VERIFY_REASON_CLIP)] + _VERIFY_REASON_CLIP


def _verify_refusal_reason(
    exc: BaseException,
    *,
    state_dir: Path,
    workdir: Path | None = None,
    repo: Path | None = None,
) -> str:
    """Refusal text with operator path prefixes replaced and output bounded."""
    text = str(getattr(exc, "error", exc) or "")
    prefixes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, value in (("<state_dir>", state_dir), ("<workdir>", workdir), ("<repo>", repo)):
        if value is None:
            continue
        candidates = {str(value)}
        try:
            candidates.add(str(Path(str(value)).expanduser().resolve()))
        except OSError:
            pass
        for candidate in sorted(candidates, key=len, reverse=True):
            candidate = candidate.rstrip("/\\")
            if candidate and candidate not in seen:
                seen.add(candidate)
                prefixes.append((candidate, label))
    for prefix, label in sorted(prefixes, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(prefix, label)
    return _clip_verify_reason(text)


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
    code: str | None = None,
    error: str | None = None,
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
    if code is not None:
        payload["code"] = str(code)
    if error is not None:
        payload["error"] = _clip_verify_reason(str(error))
    source_rev = (evidence or {}).get("source_rev")
    clean = scrub(payload)
    validated_branch = _literal_branch(branch)
    if validated_branch is not None:
        clean["branch"] = validated_branch
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
    from .execution import SandboxRunner, scanner

    config = _config(ctx)
    gitleaks = scanner(config)

    try:
        # --commit-local serves the payload from the environment without
        # binding a lease manifest (no lease exists on this path); the
        # disclosure lives in docs/pinned-sources.md.
        runner = SandboxRunner(config)
    except dispatch.PayloadError as exc:
        raise _as_cli_error(exc) from exc

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
            runner=runner,
        )
    except verify_local.VerifyError as exc:
        raise CliError(exc.code, exc.message, exc.fix) from exc

    _print("bundlebees verify --commit-local")
    _print(f"attempt: {attempt_id}")
    _print_sha(record["sha"], label="commit")
    _print_branch(record["branch"], " (local only, not pushed)")
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
    workdir: Path | None = None
    repo: Path | None = None
    run_id = "default"
    evidence: dict[str, Any] = {}
    repo_value = config.get("repo_path")
    if repo_value:
        try:
            repo = Path(str(repo_value)).expanduser().resolve()
        except (OSError, RuntimeError):
            repo = Path(str(repo_value)).expanduser()

    try:
        # 1. Lease authority: only the coordinator record written at dispatch.
        try:
            record = leases.load_lease(state_dir, attempt_id)
        except leases.LeaseError as exc:
            raise _as_cli_error(exc) from exc
        run_id = str(record.get("run_id") or "default")
        source_rev = str(record.get("source_rev") or "")
        evidence["source_rev"] = source_rev

        # Lease-payload binding, before any test runs: the manifest recorded at
        # dispatch must match what this coordinator serves now, byte for byte.
        # A stale pin is a refusal, never a softened fallback; a lease without
        # a recorded manifest binds nothing.
        try:
            dispatch.lease_payload(record)
        except dispatch.PayloadError as exc:
            raise _as_cli_error(exc) from exc

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

        trips = _secret_trips(workdir, files, patch, source_rev=source_rev)
        flagged = sorted({trip["file"] for trip in trips})
        if flagged:
            _reset_patch(workdir)
            raise CliError(
                "secret_detected",
                _secret_refusal(flagged, trips),
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
            runner=test_evidence.get("runner"),
            test_argv=test_evidence.get("test_argv"),
        )

        if dry_run:
            target = _write_verify_result(
                state_dir, attempt_id, ok=True, sha=None, branch=branch,
                files=files, tests=tests, evidence=evidence,
            )
            _record_verify(state_dir, run_id, attempt_id, record, worker, ok=True, code="dry_run")
            _print("bundlebees verify")
            _print(f"attempt: {attempt_id}")
            _print_branch(branch)
            _print(f"files: {', '.join(files)}")
            _print(f"tests: {', '.join(tests)}")
            _print(f"sandbox: {evidence.get('backend')}")
            _print_digest("patch_sha256", evidence.get("patch_sha256"))
            acdd_cli.print_mode(acdd_state, green)
            _print("result: dry-run passed; nothing was committed or pushed")
            _print_state_path("verify result", target, state_dir)
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
        # Durable delivery intent BEFORE the push (transactional-outbox style): a crash here
        # leaves an `intent` record the `deliver` sweep reports instead of silently losing the
        # obligation to open a PR.
        delivery_record = None
        try:
            delivery_record = _delivery.record_intent(
                state_dir, attempt_id=attempt_id, repo=_origin_repo(workdir), branch=branch,
                base_sha=source_rev, title=f"verify: {branch} (attempt {attempt_id})",
                body=(f"Verified worker payload.\n\n- attempt: `{attempt_id}`\n- branch: `{branch}`\n"
                      f"- tests: {', '.join(tests)}\n- files: {len(files)}\n"),
            )
        except Exception as exc:  # noqa: BLE001 - delivery bookkeeping must never block the push
            _print(f"delivery: intent not recorded: {exc}")
        _push_branch(workdir, branch)
        if delivery_record is not None:
            try:
                _delivery.record_pushed(state_dir, attempt_id, sha)
            except Exception as exc:  # noqa: BLE001
                _print(f"delivery: pushed-sha not recorded: {exc}")

        target = _write_verify_result(
            state_dir, attempt_id, ok=True, sha=sha, branch=branch,
            files=files, tests=tests, evidence=evidence,
        )
        _record_verify(state_dir, run_id, attempt_id, record, worker, ok=True, code="verified", sha=sha)

        _print("bundlebees verify")
        _print(f"attempt: {attempt_id}")
        _print_branch(branch)
        _print_sha(sha)
        _print(f"files: {', '.join(files)}")
        _print(f"tests: {', '.join(tests)}")
        _print(f"sandbox: {evidence.get('backend')}")
        _print_digest("patch_sha256", evidence.get("patch_sha256"))
        acdd_cli.print_mode(acdd_state, green)
        _print(f"pushed: feature branch only ({BRANCH_BUDGET_NOTE})")
        if delivery_record is not None:
            # Eager relay: the PR is opened as part of the same verify run, so "verified" is
            # never a resting state. Failure is non-fatal - the record stays pending for the
            # `deliver` sweep (the level-triggered backstop).
            try:
                url, created = _delivery.open_pr_idempotent(
                    delivery_record["repo"], branch, delivery_record["title"], delivery_record["body"])
                _delivery.record_pr(state_dir, attempt_id, url)
                _print(f"pr: {url}" + ("" if created else " (existing)"))
            except Exception as exc:  # noqa: BLE001
                _print(f"pr: pending - the platform refused or lacked access ({exc}); "
                       "run `hermes bundlebees deliver` to converge")
        _print_state_path("verify result", target, state_dir)
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
        reason = _verify_refusal_reason(exc, state_dir=state_dir, workdir=workdir, repo=repo)
        try:
            _write_verify_result(
                state_dir, attempt_id, ok=False, sha=sha, branch=branch,
                files=files, tests=tests, evidence=evidence,
                code=str(exc.code), error=reason,
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
    except (_brief_mod.BriefError, leases.LeaseError, dispatch.PayloadError) as exc:
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
    _print_branch(branch)
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
    """The rizzstack doctor: read-only stack parity and health diagnostic."""
    from . import doctor

    path = _rails_setting(ctx, 'rails_db')
    if not path:
        return doctor.run(ctx, args)
    checkers = getattr(ctx, 'bundlebees_doctor_checkers', None)
    report = doctor.collect(ctx, args, checkers if isinstance(checkers, doctor.Checkers) else None)
    row = _rails_doctor(path)
    report['checks'].append(row)
    if row['fix']:
        report['remediation'].append('rails: ' + row['fix'])
    if args.json:
        print(json.dumps(doctor._public_report(report), indent=2, sort_keys=True))
    else:
        print('\n'.join(doctor.render(report)))
    return int(bool(args.check_parity and not report['parity_ok']))


def _rails_setting(ctx, name, default=''):
    return getattr(ctx, 'get_config', lambda k, d: d)(name, default)


def _rails_doctor(path):
    from contextlib import closing
    from rizzstack_rails import RailsLedger
    from rizzstack_rails.query import make_request, query
    row = dict(check='rails', state='unavailable', detail='Configured ledger is unavailable.',
               fix='Check the local ledger and its profile read permission.', rev=None)
    try:
        with closing(RailsLedger(path, read_only=True)) as reader:
            head = reader.head()
            if head['seq']:
                result = query(make_request('event', f"{head['chain_id']}:{head['seq']}", format='json'), reader)
                if not result.get('ok') or not result['items']:
                    return row
            origins = reader.query("select count(*) as total, coalesce(sum(state='halted'),0) as halted from import_cursors")[0]
            pages = 0
            if reader.query("select 1 from sqlite_master where type='table' and name='source_pages'"):
                pages = reader.query('select count(distinct source_key) as n from source_pages')[0]['n']
            row.update(state='stale' if origins['halted'] else 'healthy',
                       detail=f"Read-only ledger; local head {head['seq']}, checkpointed sources {pages}, "
                       f"imported origins {origins['total']}, halted origins {origins['halted']}. "
                       'Full integrity and freshness unchecked.',
                       fix='Reconcile the halted origin against its pinned head.' if origins['halted'] else '')
    except Exception:
        pass  # Never expose a configured path or exception text in doctor.
    return row


def _rails_import(ctx, args):
    """Explicit CLI importer, no network/filesystem parameters on the model tool."""
    from contextlib import closing
    from rizzstack_rails import RailsLedger
    from rizzstack_rails.importer import Importer
    from rizzstack_rails.source import SourceError, token
    try:
        path = _rails_setting(ctx, 'rails_db')
        name = args.source
        configured = _rails_setting(ctx, 'rails_import_sources')
        if not path or not Path(path).is_absolute() or name not in {n.strip() for n in configured.split(',')}:
            raise SourceError('not_configured')
        if type(args.limit) is not int or not 1 <= args.limit <= 500:
            raise SourceError('invalid_limit')
        state = getattr(ctx, 'bundlebees_state_dir', None) or plugin_state_dir()
        if not state or not Path(state).is_dir():
            raise SourceError('not_configured')
        authorities = {'imported_observation'}  # local page checkpoints
        if name == 'bundlebees':
            from .rails_bundlebees_source import BundlebeesSource
            source = BundlebeesSource.from_existing_state(state)
            authorities.update(('worker_report','coordinator_record','verifier_record','reviewer_record'))
        elif name == 'outbox':
            from .rails_outbox_source import RailsOutboxSource
            source = RailsOutboxSource(state)
            authorities.add('worker_report')
        elif name == 'queue':
            from .rails_queue_source import QueueSource
            source = QueueSource(state)
            authorities.add('coordinator_record')
        elif name == 'hermes':
            from .rails_hermes_source import HermesSource
            aliases = json.loads(_rails_setting(ctx, 'rails_session_aliases', '{}'))
            db = _rails_setting(ctx, 'rails_hermes_db')
            if not aliases or not isinstance(aliases, dict) or not db or not Path(db).is_absolute():
                raise SourceError('not_configured')
            source = HermesSource(db, aliases)
        elif name == 'launcher':
            from .rails_remote_source import RemoteRailSource
            from .client import get_launcher_token
            base = _rails_setting(ctx, 'launcher_url')
            if not base or _rails_setting(ctx, 'adapter') != 'launcher':
                raise SourceError('not_configured')
            chain = token(_rails_setting(ctx, 'rails_launcher_chain'))
            authorities.add('worker_report')
            source = RemoteRailSource(base_url=base, peer_id='worker-1', env={},
                                      config={'launcher_url':base, 'allow_private': _rails_setting(ctx, 'allow_private', False) is True},
                                      token_provider=lambda: get_launcher_token('BUNDLEBEES_RAILS_READ_TOKEN'),
                                      max_response_bytes=1 << 20)
        else:
            raise SourceError('invalid_source')
        with closing(RailsLedger(path, allowed_authorities=authorities)) as ledger:
            importer = Importer(ledger, authorities=authorities)
            if name == 'launcher':
                pos = ledger.import_cursor(chain)
                if pos['state'] != 'active':
                    raise SourceError('source_fork')
                # R09 compares the peer head to the durable receipt, even
                # on an empty page. R04 validates original bytes and authority.
                report = source.import_into(importer, chain, cursor=pos['next_seq']-1, limit=args.limit)
                result = dict(imported=report['ledger'].get('imported',0), has_more=report['has_more'])
            else:
                result = importer.import_once(source, limit=args.limit)
        print(json.dumps(dict(ok=True,source=name,imported=result['imported'],has_more=result['has_more'])))
        return 0
    except Exception as exc:
        code = getattr(exc, 'code', 'source_unavailable')
        if not isinstance(code,str) or not re.fullmatch(r'[a-z_]{1,48}', code):
            code = 'source_unavailable'
        print(json.dumps(dict(ok=False, error={'code':code})))
        return 2 if code.startswith('invalid_') else 3
