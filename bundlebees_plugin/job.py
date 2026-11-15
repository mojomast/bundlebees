"""Durable job documents and human authorizations.

Chapter 22 of the method: an HMAC proves integrity, not consent, so authority
lives in a separate document written through an interactive path, and a job may
only narrow it. Everything here is plain JSON on disk, signed with the same key
the lease records use, written atomically and readable after a crash.

Recovery permission is not implicit either. An authorization may carry a sealed
``recovery`` block — attempts, corrections, the exact replacement selectors, the
file and test scope a correction may touch, and the human gates that stay in
force — and that block is part of the signed body, digested, and pinned into the
job document when the job is admitted. A job or task ``recovery_policy`` may
only restate a permission to narrow it (``min`` for attempts and corrections,
intersection for selectors and scopes, union for gates). An authorization with
no block grants no retry and no correction at all.

The seal is an HMAC over canonical JSON with a key file in the state directory.
It is tamper *evidence* for a reader of that directory, not OS isolation: any
principal that can read the key can seal a new authorization. The separate
interactive path exists so a coordinator cannot mint authority for itself, not
to defend against a principal that already owns the state directory.

An authorization can also be recorded from a one-time owner approval receipt
(:mod:`bundlebees_plugin.approval`). That path does not weaken the terminal
rule: the receipt must be verified, unexpired, unused, and its binding must
equal the authorization fields exactly. It authorizes the trial run only; it
grants no merge, release, deploy or acceptance.

Refusal codes are stable strings, because a coordinator that cannot tell "the
file was edited" from "the scope was widened" cannot respond correctly.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets
import sys

from . import packets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .leases import LeaseError, _canonical, _ensure_key, _load_key, _sign, _utc_now

JOB_SCHEMA_VERSION = 1

AUTH_DIR = "authorizations"
JOBS_DIR = "jobs"
APPROVALS_DIR = "approvals"

#: Task states, in the order a healthy task walks them.
TASK_STATES: tuple[str, ...] = (
    "pending",
    "dispatching",
    "dispatched",
    "running",
    "verifying",
    "verified",
    "integrating",
    "accepted",
)

#: Sideways and terminal states, each reachable only from the states listed.
TASK_SIDE_STATES: dict[str, tuple[str, ...]] = {
    "rejected": ("dispatching", "dispatched", "running", "verifying", "integrating"),
    "superseded": ("pending", "dispatched", "rejected"),
    "failed": tuple(TASK_STATES) + ("rejected",),
}

_LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("dispatching", "failed"),
    "dispatching": ("dispatched", "rejected", "failed"),
    "dispatched": ("running", "rejected", "superseded", "failed"),
    "running": ("verifying", "rejected", "failed"),
    "verifying": ("verified", "rejected", "failed"),
    "verified": ("integrating", "failed"),
    "integrating": ("accepted", "rejected", "failed"),
    "accepted": (),
    "rejected": ("dispatching", "superseded", "failed"),
    "superseded": (),
    "failed": (),
}

_FIX_TAMPERED = (
    "The file was edited outside the coordinator, or the key changed. Restore it "
    "from the recorded copy; do not hand-edit job or authorization documents."
)
_FIX_TRANSITION = "Only move a task along the documented state machine."
_FIX_SCOPE = "Narrow the lease to a path the authorization names."
_FIX_AUTH = "Ask the approver for an authorization that covers this work."


class JobError(Exception):
    """A job document, authorization or transition was refused."""

    def __init__(self, code: str, message: str, fix: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _subdir(state_dir: Path, name: str) -> Path:
    path = state_dir / name
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def authorization_path(state_dir: Path, job_id: str) -> Path:
    return _subdir(state_dir, AUTH_DIR) / f"{_job_id(job_id)}.json"


def job_path(state_dir: Path, job_id: str) -> Path:
    return _subdir(state_dir, JOBS_DIR) / f"{_job_id(job_id)}.json"


def approval_path(state_dir: Path, job_id: str, gate: str) -> Path:
    return _subdir(state_dir, APPROVALS_DIR) / f"{_job_id(job_id)}.{_slug(gate)}.json"


def _job_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 96:
        raise JobError("job_invalid", "job_id must be 1-96 characters", _FIX_AUTH)
    if not all(ch.isalnum() or ch in "-_." for ch in text):
        raise JobError(
            "job_invalid",
            "job_id may contain only letters, digits, '-', '_' and '.'",
            _FIX_AUTH,
        )
    return text


def _slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        raise JobError("job_invalid", "gate name must not be empty", _FIX_AUTH)
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in text)


# ---------------------------------------------------------------------------
# Signed, atomically replaced documents
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _without_seal(body: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k != "seal"}


def _create_only(path: Path, payload: bytes, mode: int = 0o400) -> bool:
    """Publish a document only if it does not exist yet.

    The bytes are written in full to a private temporary file and then linked
    into place, which fails atomically when the target exists. Two concurrent
    writers therefore cannot both believe they created the document, and a
    crash mid-write can never leave a half-written document where a reader
    would take it for authority.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except OSError:
            # Filesystems without hard links: exclusive create is still atomic.
            try:
                direct = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            except FileExistsError:
                return False
            with os.fdopen(direct, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        return True
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _seal(body: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    document = dict(body)
    return {"body": document, "mac": _sign(document, key)}


def verify_seal(body: Mapping[str, Any], key: bytes, what: str) -> dict[str, Any]:
    """Verify a sealed object already in memory (used by sealed policy files)."""
    provided = body.get("seal")
    expected = hmac.new(key, _canonical(_without_seal(body)), hashlib.sha256).hexdigest()
    if not isinstance(provided, str) or not hmac.compare_digest(provided, expected):
        raise JobError("seal_invalid", f"the {what} seal does not verify", "Refuse it; re-enroll from a trusted copy.")
    return _without_seal(body)


def open_sealed(envelope: Mapping[str, Any], key: bytes, what: str) -> dict[str, Any]:
    """Open the on-disk ``{"body", "mac"}`` envelope and verify it."""
    body, mac = envelope.get("body"), envelope.get("mac")
    if not isinstance(body, Mapping) or not isinstance(mac, str):
        raise JobError("seal_invalid", f"the {what} envelope is malformed", "Refuse it.")
    expected = hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        raise JobError("seal_invalid", f"the {what} seal does not verify", "Refuse it; re-enroll from a trusted copy.")
    return dict(body)


def _unseal(path: Path, key: bytes, what: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise JobError(f"{what}_missing", f"{what} document does not exist: {path}", _FIX_AUTH) from exc
    except OSError as exc:
        raise JobError(f"{what}_invalid", f"{what} document could not be read", _FIX_TAMPERED) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise JobError(f"{what}_invalid", f"{what} document is not valid JSON", _FIX_TAMPERED) from exc
    if not isinstance(document, dict):
        raise JobError(f"{what}_invalid", f"{what} document must be an object", _FIX_TAMPERED)
    body = document.get("body")
    mac = document.get("mac")
    if not isinstance(body, dict) or not isinstance(mac, str):
        raise JobError(f"{what}_invalid", f"{what} document is not sealed", _FIX_TAMPERED)
    expected = _sign(body, key)
    if not hmac.compare_digest(mac, expected):
        raise JobError(f"{what}_tampered", f"{what} document failed its MAC check", _FIX_TAMPERED)
    return body


# ---------------------------------------------------------------------------
# Authorizations — written only through an interactive path
# ---------------------------------------------------------------------------

#: The exact fields a sealed authorization's ``recovery`` block may carry.
#: Every one of them is a permission: a job or task ``recovery_policy`` may
#: restate a subset to *narrow* the ceiling, and can never add a field or a
#: value the approver did not name.
RECOVERY_FIELDS: tuple[str, ...] = (
    "max_attempts_per_task",
    "contract_corrections_allowed",
    "corrected_tests_allowed",
    "file_scope",
    "test_scope",
    "gates",
)

#: v1 caps the attempt ceiling at five total attempts and one correction.
RECOVERY_MAX_ATTEMPTS = 5

#: A selector is a test path with an optional ``::node`` suffix: the path part
#: carries no whitespace, and the node part carries no newline.
_SELECTOR_RE = re.compile(r"^[^:\s]+(::[^\n]+)?$")


def recovery_digest(recovery: Mapping[str, Any] | None) -> str:
    """SHA-256 over one canonical recovery block.

    The digest is stored *inside* the sealed authorization body and pinned in
    the job's authorization reference, so a recovery block that is rewritten
    after the approval — even by re-sealing the authorization with the state
    key — no longer matches what the job was admitted under.

    This is tamper *evidence*, not isolation: the state key is a file readable
    by the OS principal that runs the coordinator, so a principal with that
    access can mint a whole new authorization. Nothing here claims OS-level
    separation between the approver and the coordinator.
    """

    return hashlib.sha256(_canonical(dict(recovery or {}))).hexdigest()


def _selector(value: Any, where: str) -> str:
    text = str(value or "").strip()
    if not text or not _SELECTOR_RE.fullmatch(text) or text.startswith("/") or ".." in Path(text).parts:
        raise JobError(
            "authorization_incomplete",
            f"{where} must be one test selector (path or path::node), not {value!r}",
            _FIX_AUTH,
        )
    return text


def _recovery_envelope(
    value: Mapping[str, Any] | None, *, gates: Sequence[str], scope: Sequence[str]
) -> dict[str, Any]:
    """Validate and normalise the recovery ceiling an approver types in.

    Nothing is inferred. Every permission an envelope can carry — attempts,
    corrections, the exact replacement selectors, the file and test scope a
    correction may touch, and the human gates that stay in force — must be
    named by the approver. A block that budgets a correction without naming its
    selectors, its file scope and its test scope is refused rather than filled
    in from the job or the contract: those documents are the thing this ceiling
    exists to bound.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise JobError("authorization_incomplete", "recovery must be a mapping", _FIX_AUTH)
    unknown = sorted(set(value) - set(RECOVERY_FIELDS))
    if unknown:
        raise JobError(
            "authorization_incomplete",
            "unknown recovery field(s): " + ", ".join(unknown),
            "Enroll only the documented recovery ceiling fields; an unknown field is never a permission.",
        )
    out: dict[str, Any] = {}
    attempts = value.get("max_attempts_per_task")
    if attempts is not None:
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= RECOVERY_MAX_ATTEMPTS
        ):
            raise JobError(
                "authorization_incomplete",
                f"recovery.max_attempts_per_task must be an integer from 1 to {RECOVERY_MAX_ATTEMPTS}",
                _FIX_AUTH,
            )
        out["max_attempts_per_task"] = int(attempts)
    corrections = value.get("contract_corrections_allowed")
    if corrections is not None:
        if isinstance(corrections, bool):
            corrections = int(corrections)
        if not isinstance(corrections, int) or corrections not in (0, 1):
            raise JobError(
                "authorization_incomplete",
                "recovery.contract_corrections_allowed must be the integer 0 or 1",
                _FIX_AUTH,
            )
        out["contract_corrections_allowed"] = int(corrections)
    allowed = value.get("corrected_tests_allowed")
    if allowed is not None:
        if not isinstance(allowed, (list, tuple)):
            raise JobError(
                "authorization_incomplete",
                "recovery.corrected_tests_allowed must be a list of test selections",
                _FIX_AUTH,
            )
        out["corrected_tests_allowed"] = sorted(
            {_selector(item, "recovery.corrected_tests_allowed[]") for item in allowed}
        )
    for name in ("file_scope", "test_scope"):
        entries = value.get(name)
        if entries is None:
            continue
        if not isinstance(entries, (list, tuple)):
            raise JobError("authorization_incomplete", f"recovery.{name} must be a list of paths", _FIX_AUTH)
        out[name] = _normalize_scope(entries)
        if not out[name]:
            raise JobError(
                "authorization_incomplete",
                f"recovery.{name} names no path, so it permits nothing",
                _FIX_AUTH,
            )
    named_gates = value.get("gates")
    if named_gates is not None:
        if not isinstance(named_gates, (list, tuple)) or not all(
            isinstance(gate, str) and gate.strip() for gate in named_gates
        ):
            raise JobError("authorization_incomplete", "recovery.gates must be a list of gate names", _FIX_AUTH)
        normalized = sorted({_slug(gate) for gate in named_gates})
        missing = sorted(set(gates) - set(normalized))
        if missing:
            raise JobError(
                "authorization_incomplete",
                "recovery.gates removes the authorization gate(s): " + ", ".join(missing),
                "A recovery block may add a human gate, never remove one.",
            )
        out["gates"] = normalized

    budget = int(out.get("contract_corrections_allowed") or 0)
    outside = [path for path in out.get("file_scope") or [] if not _covered(path, scope)]
    if outside:
        raise JobError(
            "authorization_incomplete",
            "recovery.file_scope names path(s) outside the authorized scope: " + ", ".join(outside),
            "A recovery ceiling may narrow the authorization, never reach past it.",
        )
    outside = [path for path in out.get("test_scope") or [] if not _covered(path, out.get("file_scope") or [])]
    if outside:
        raise JobError(
            "authorization_incomplete",
            "recovery.test_scope names path(s) outside recovery.file_scope: " + ", ".join(outside),
            "A corrected test must live in a file the recovery may already touch.",
        )
    if budget == 1:
        for name in ("corrected_tests_allowed", "file_scope", "test_scope"):
            if not out.get(name):
                raise JobError(
                    "authorization_incomplete",
                    f"a contract correction needs an explicit recovery.{name}",
                    "Name the replacement selectors and the files they may touch; nothing is inferred.",
                )
        outside = [
            selector
            for selector in out["corrected_tests_allowed"]
            if not _covered(selector.split("::", 1)[0], out["test_scope"])
        ]
        if outside:
            raise JobError(
                "authorization_incomplete",
                "recovery.corrected_tests_allowed names test(s) outside recovery.test_scope: "
                + ", ".join(sorted(outside)),
                _FIX_AUTH,
            )
    elif out.get("corrected_tests_allowed"):
        raise JobError(
            "authorization_incomplete",
            "recovery.corrected_tests_allowed names replacements but no correction is budgeted",
            "Grant both or neither; a test list with no budget authorises nothing.",
        )
    return out


def _grant_job_inputs(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract the one repository, revision, scope and budget a repo grant covers."""
    repos: set[str] = set()
    revisions: set[str] = set()
    profiles: set[str] = set()
    hosts: set[str] = set()
    files: set[str] = set()
    contracts: set[str] = set()
    wall_clock = 0
    for task in tasks:
        raw_provision = task.get("provision")
        provision: Mapping[str, Any] = dict(raw_provision) if isinstance(raw_provision, Mapping) else {}
        repo = str(provision.get("repo") or task.get("repo") or "").strip()
        if repo:
            repos.add(repo)
        revision = str(provision.get("source_rev") or task.get("base_rev") or "").strip()
        if revision:
            revisions.add(revision)
        profile = str(provision.get("profile") or task.get("profile") or "").strip()
        if profile:
            profiles.add(profile)
        host = str(provision.get("host") or task.get("host") or "").strip()
        if host:
            hosts.add(host)
        contracts.add(str(provision.get("contract_sha256") or task.get("contract_sha256") or ""))
        for path in task.get("lease") or []:
            text = str(path or "").strip()
            if text:
                files.add(text)
        try:
            wall_clock = max(wall_clock, int(task.get("wall_clock_s") or 0))
        except (TypeError, ValueError):
            raise JobError("grant_job_invalid", "task wall_clock_s must be an integer", "Use a bounded integer wall clock.")
    if len(repos) != 1:
        raise JobError("grant_repo_ambiguous", "a repo grant can only auto-authorize one repository per job", "Split the job or use per-job authorization.")
    if len(revisions) != 1 or not _is_sha(next(iter(revisions))):
        raise JobError("grant_revision_missing", "a repo grant requires one full 40-hex base revision on the job", "Pin the exact base revision.")
    return {
        "repo": next(iter(repos)),
        "source_rev": next(iter(revisions)),
        "profile": next(iter(profiles), ""),
        "host": next(iter(hosts), ""),
        "assigned_files": sorted(files) or ["**"],
        "contract_sha256": next((item for item in contracts if item), ""),
        "bees": max(1, len(tasks)),
        "wall_clock_s": max(60, wall_clock or 3600),
    }


def authorize_from_repo_grant(state_dir: Path, *, job_id: str, tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive and seal a job authorization from a human-enrolled repo grant.

    The grant is the authority. This function only narrows it to the job's
    repository, pinned revision, file leases and budget; the per-job TTY path is
    deliberately not used here, because the human's one-time grant is the
    approval. It refuses rather than falling back when no usable grant exists.
    """
    from . import grants as grant_mod
    from .leases import LeaseError, _load_key

    shape = _grant_job_inputs(tasks)
    try:
        key = _load_key(Path(state_dir))
    except LeaseError as exc:
        raise JobError("grant_missing", "no standing grant key exists for this state directory", "Enroll a repo grant in a real terminal.") from exc
    store = grant_mod.GrantStore(Path(state_dir), key=key)
    try:
        grant = store.find_active_for_repo(shape["repo"])
    except grant_mod.GrantError as exc:
        raise JobError(exc.code, exc.message, exc.fix) from exc
    try:
        derived = store.derive(
            grant.grant_id,
            job_id=job_id,
            repo=shape["repo"],
            host=shape["host"] or "*",
            profile=shape["profile"] or "*",
            source_rev=shape["source_rev"],
            assigned_files=shape["assigned_files"],
            bees=shape["bees"],
            wall_clock_s=shape["wall_clock_s"],
            contract_sha256=shape["contract_sha256"],
        )
    except grant_mod.GrantError as exc:
        raise JobError(exc.code, exc.message, exc.fix) from exc

    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "authorization",
        "job_id": _job_id(job_id),
        "auth_id": secrets.token_hex(8),
        "statement": grant.statement,
        "repo": shape["repo"],
        "base_rev": shape["source_rev"],
        "scope": list(derived["assigned_files"]),
        "budget": {"bees": int(shape["bees"]), "wall_clock_s": int(shape["wall_clock_s"])},
        "gates": sorted({_slug(gate) for gate in grant_mod.EXCLUDED_ACTIONS}),
        "by": f"repo-grant:{grant.grant_id}",
        "at": _utc_now(),
        "authority": "repo-grant",
        "grant": {
            "grant_id": grant.grant_id,
            "statement": grant.statement,
            "approved_by": grant.approved_by,
            "approved_at": grant.approved_at,
            "expires_at": grant.expires_at,
            "digest": grant.digest,
        },
        "grant_id": grant.grant_id,
        "grant_statement": grant.statement,
        "grant_approved_at": grant.approved_at,
        "recovery_digest": recovery_digest({}),
        "derivation_digest": derived.get("digest", ""),
    }
    key = _ensure_key(Path(state_dir))
    path = authorization_path(Path(state_dir), job_id)
    if not _create_only(path, _canonical(_seal(body, key)) + b"\n", mode=0o400):
        existing = load_authorization(Path(state_dir), job_id)
        if str(existing.get("authority") or "") == "repo-grant" and str(existing.get("job_id")) == str(job_id):
            return existing
        raise JobError("authorization_exists", "an authorization already exists for this job id", "Use a new job id.")
    return body


def release_repo_grant_for_job(state_dir: Path, job_id: str) -> None:
    """Release a repo-grant reservation once every task in the job is accepted."""
    try:
        job = load_job(Path(state_dir), job_id, recover=False)
    except JobError:
        return
    raw_authorization = job.get("authorization")
    authorization: Mapping[str, Any] = raw_authorization if isinstance(raw_authorization, Mapping) else {}
    if str(authorization.get("authority") or "") != "repo-grant":
        return
    grant_id = str(authorization.get("grant_id") or "")
    tasks = job.get("tasks") or []
    if not grant_id or not tasks or any(str(task.get("state")) != "accepted" for task in tasks):
        return
    try:
        from . import grants as grant_mod
        from .leases import _load_key

        store = grant_mod.GrantStore(Path(state_dir), key=_load_key(Path(state_dir)))
        store.complete(grant_id, job_id)
    except Exception:  # noqa: BLE001 - reservation cleanup must not invalidate an accepted job
        return


def record_authorization(

    state_dir: Path,
    *,
    job_id: str,
    statement: str,
    repo: str,
    base_rev: str,
    scope: Sequence[str],
    bees: int,
    wall_clock_s: int,
    gates: Sequence[str] = ("merge",),
    require_tty: bool = True,
    interactive: bool | None = None,
    by: str | None = None,
    recovery: Mapping[str, Any] | None = None,
    approval_receipt: Mapping[str, Any] | None = None,
    approval_signing_key: bytes | str | None = None,
    tasks: Sequence[Mapping[str, Any]] | None = None,
    code_rev: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Record one human authorization. Immutable once written.

    ``interactive`` defaults to whether stdin is a TTY. A coordinator running
    from cron has no TTY and therefore cannot mint authority for itself.

    ``approval_receipt`` is the additive, non-TTY path: a receipt written by
    the owner through the trusted application (:mod:`bundlebees_plugin.approval`).
    It is accepted only with ``require_tty=False``, only after its signature,
    expiry, one-time consumption and exact binding are verified, and only when
    ``tasks`` (when supplied) and the running Bundlebees revision match what the
    owner approved. The sealed authorization then names ``owner-approval`` and
    keeps the receipt identity and digest for audit. The receipt never grants
    acceptance, integration, merge, release or deployment.
    """

    if approval_receipt is not None and require_tty:
        raise JobError(
            "authorization_receipt_requires_nontty",
            "an owner approval receipt is only accepted on the non-interactive authorization path",
            "Call the receipt path with require_tty=False; a receipt never substitutes for a typed authorization.",
        )

    is_tty = sys.stdin.isatty() if interactive is None else bool(interactive)
    if require_tty and not is_tty:
        raise JobError(
            "authorization_not_interactive",
            "an authorization must be typed at a terminal",
            "Have the approver run `bundlebees job authorize` themselves.",
        )

    text = str(statement or "").strip()
    if len(text) < 8:
        raise JobError(
            "authorization_incomplete",
            "the statement of intent must be a real sentence, not a placeholder",
            "Record what the approver actually asked for.",
        )
    globs = _normalize_scope(scope)
    if not globs:
        raise JobError("authorization_incomplete", "an authorization needs at least one scope glob", _FIX_AUTH)
    if not str(repo or "").strip() or "/" not in str(repo):
        raise JobError("authorization_incomplete", "repo must be 'owner/name'", _FIX_AUTH)
    if not _is_sha(base_rev):
        raise JobError("authorization_incomplete", "base_rev must be a full 40-hex commit", _FIX_AUTH)
    if int(bees) < 1:
        raise JobError("authorization_incomplete", "the bee budget must be at least 1", _FIX_AUTH)
    if int(wall_clock_s) < 60:
        raise JobError("authorization_incomplete", "the wall-clock budget must be at least 60 s", _FIX_AUTH)

    path = authorization_path(state_dir, job_id)
    key = _ensure_key(state_dir)
    authorized_gates = sorted({_slug(gate) for gate in gates}) or ["merge"]
    envelope = _recovery_envelope(recovery, gates=authorized_gates, scope=globs)
    approval_block: dict[str, Any] = {}
    if approval_receipt is not None:
        approval_block = _verify_owner_approval(
            state_dir,
            receipt=approval_receipt,
            signing_key=approval_signing_key,
            job_id=job_id,
            statement=text,
            repo=str(repo).strip(),
            base_rev=str(base_rev).strip().lower(),
            scope=globs,
            bees=int(bees),
            wall_clock_s=int(wall_clock_s),
            gates=authorized_gates,
            recovery=envelope,
            tasks=tasks,
            code_rev=code_rev,
            now=now,
        )
    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "authorization",
        "job_id": _job_id(job_id),
        "auth_id": secrets.token_hex(8),
        "statement": text,
        "repo": str(repo).strip(),
        "base_rev": str(base_rev).strip().lower(),
        "scope": globs,
        "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
        "gates": authorized_gates,
        "by": by or _current_user(),
        "at": _utc_now(),
        # Always sealed, even when the block is empty: the digest is the pin a
        # job is admitted under, and an empty ceiling is a fact worth proving.
        "recovery_digest": recovery_digest(envelope),
    }
    if envelope:
        # The approver's recovery ceiling is part of the sealed authorization.
        # A job or task policy may restate it to narrow it, never to widen it,
        # and no job document is consulted for a permission.
        body["recovery"] = envelope
    if approval_block:
        # The owner's decision is the authority, and the receipt identity plus
        # digest stay in the sealed body so the record can be audited without
        # trusting an agent-supplied receipt later.
        body["by"] = f"owner:{approval_block['owner']}"
        body["authority"] = "owner-approval"
        body["approval"] = approval_block
    if not _create_only(path, _canonical(_seal(body, key)) + b"\n", mode=0o400):
        if approval_block:
            existing = load_authorization(state_dir, job_id)
            if all(existing.get(name) == body.get(name) for name in _APPROVAL_IDENTITY):
                # Replaying the same receipt against the same authorization is
                # safe and must not turn a successful owner click into an error.
                return existing
        raise JobError(
            "authorization_exists",
            "an authorization already exists for this job id",
            "Authorize a new job id; recorded authorizations are immutable.",
        )
    return body


#: The authorization fields an owner receipt is replayed against. ``auth_id``
#: and ``at`` are random/time values, so they are deliberately excluded.
_APPROVAL_IDENTITY = (
    "job_id", "statement", "repo", "base_rev", "scope", "budget", "gates",
    "recovery_digest", "authority", "approval",
)


def _verify_owner_approval(
    state_dir: Path,
    *,
    receipt: Mapping[str, Any],
    signing_key: bytes | str | None,
    job_id: str,
    statement: str,
    repo: str,
    base_rev: str,
    scope: Sequence[str],
    bees: int,
    wall_clock_s: int,
    gates: Sequence[str],
    recovery: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]] | None,
    code_rev: str | None,
    now: Any,
) -> dict[str, Any]:
    """Verify one owner receipt against the exact authorization being written.

    The expected binding is rebuilt with the same normaliser the requester
    used, taking only the non-authorization facts (tasks, worker
    project/profile, code revision, coordinator plan, extra budgets) from the
    receipt itself. A changed statement, scope, budget, gate or recovery
    ceiling therefore changes the digest and is refused. The receipt is then
    consumed exactly once, before the authorization is created.
    """

    from . import approval as approval_mod  # noqa: PLC0415 - approval imports this module lazily

    binding = receipt.get("binding") if isinstance(receipt, Mapping) else None
    if not isinstance(binding, Mapping):
        raise JobError(
            "approval_receipt_invalid", "the approval receipt has no binding object", _FIX_AUTH
        )
    raw_worker = binding.get("worker")
    worker: Mapping[str, Any] = dict(raw_worker) if isinstance(raw_worker, Mapping) else {}
    raw_coordinator = binding.get("coordinator")
    coordinator: Mapping[str, Any] = dict(raw_coordinator) if isinstance(raw_coordinator, Mapping) else {}
    raw_budgets = binding.get("budgets")
    budgets: Mapping[str, Any] = dict(raw_budgets) if isinstance(raw_budgets, Mapping) else {}
    extra = {name: value for name, value in budgets.items() if name not in {"bees", "wall_clock_s"}}
    try:
        expected = approval_mod.binding_for(
            job_id=job_id,
            statement=statement,
            repo=repo,
            base_rev=base_rev,
            scope=scope,
            tasks=binding.get("tasks") or [],
            bees=int(bees),
            wall_clock_s=int(wall_clock_s),
            worker_project=str(worker.get("project") or ""),
            worker_profile=str(worker.get("profile") or ""),
            code_rev=str(binding.get("code_rev") or ""),
            coordinator_stop=coordinator.get("stop"),
            restart=coordinator.get("restart"),
            budgets=extra,
            gates=gates,
            recovery=recovery,
            expires_at=str(binding.get("expires_at") or "").strip() or None,
        )
        verified = approval_mod.verify_receipt(
            receipt, signing_key=signing_key, now=now, expected_binding=expected
        )
        if str(verified.get("decision")) != "approved":
            raise approval_mod.ApprovalError(
                "approval_declined",
                f"the owner declined request {verified.get('request_id')}",
                "Nothing is authorized. Ask for a new request if the work changes.",
            )
        if tasks is not None:
            approved = str(verified["binding"].get("tasks_digest") or "")
            if approval_mod.tasks_digest(tasks) != approved:
                raise approval_mod.ApprovalError(
                    "approval_tasks_mismatch",
                    "the task list to authorize is not the task list the owner approved",
                    "Re-request approval for the exact task set; do not substitute tasks.",
                )
        local_rev = approval_mod.code_revision()
        if not local_rev:
            raise approval_mod.ApprovalError(
                "approval_code_rev_unverifiable",
                "the Bundlebees checkout cannot name its own revision",
                "Run the authorization from the approved checkout.",
            )
        if code_rev is not None and str(code_rev).strip().lower() != local_rev:
            raise approval_mod.ApprovalError(
                "approval_code_rev_mismatch",
                "the caller-supplied code revision is not this checkout's HEAD",
                "Use the checkout that will run; do not substitute a revision string.",
            )
        if local_rev != str(verified["binding"].get("code_rev") or ""):
            raise approval_mod.ApprovalError(
                "approval_code_rev_mismatch",
                "the running Bundlebees revision is not the revision the owner approved",
                "Re-request approval from the revision that will run.",
            )
        consumer = "job-authorization:" + hashlib.sha256(
            _canonical(
                {
                    "job_id": _job_id(job_id),
                    "statement": statement,
                    "repo": repo,
                    "base_rev": base_rev,
                    "scope": list(scope),
                    "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
                    "gates": list(gates),
                    "recovery_digest": recovery_digest(recovery),
                }
            )
        ).hexdigest()
        approval_mod.consume_receipt(
            state_dir,
            verified,
            signing_key=signing_key,
            now=now,
            expected_binding=expected,
            consumer=consumer,
        )
    except approval_mod.ApprovalError as exc:
        raise JobError(exc.code, exc.message, exc.fix) from exc
    return {
        "schema": approval_mod.RECEIPT_SCHEMA,
        "request_id": str(verified["request_id"]),
        "receipt_id": str(verified["receipt_id"]),
        "receipt_digest": approval_mod.receipt_digest(verified),
        "request_digest": str(verified["request_digest"]),
        "decision": str(verified["decision"]),
        "owner": str(verified["owner"]),
        "auth_method": str(verified["auth_method"]),
        "issued_at": str(verified["issued_at"]),
        "expires_at": str(verified["expires_at"]),
        "code_rev": str(verified["binding"].get("code_rev") or ""),
        "tasks_digest": str(verified["binding"].get("tasks_digest") or ""),
        "worker": dict(verified["binding"].get("worker") or {}),
        "coordinator": dict(verified["binding"].get("coordinator") or {}),
        "budgets": dict(verified["binding"].get("budgets") or {}),
    }


def load_authorization(state_dir: Path, job_id: str) -> dict[str, Any]:
    try:
        key = _load_key(state_dir)
    except LeaseError as exc:
        # No key means nothing was ever authorized here; that is a missing
        # authorization, not a tampered one.
        raise JobError(
            "authorization_missing",
            f"no authorization exists for job {job_id!r}",
            _FIX_AUTH,
        ) from exc
    body = _unseal(authorization_path(state_dir, job_id), key, "authorization")
    block = body.get("recovery")
    block = dict(block) if isinstance(block, Mapping) else {}
    if "recovery" in body or "recovery_digest" in body:
        stored = str(body.get("recovery_digest") or "")
        if stored != recovery_digest(block):
            # A block that was rewritten after the seal — with or without the
            # state key — is not the ceiling the approver wrote. A recovery
            # block without a digest is refused too: nothing can prove it.
            raise JobError(
                "authorization_recovery_altered",
                f"the recovery block in authorization {body.get('auth_id')!r} does not match its sealed digest",
                "Re-authorize the job interactively; a recovery ceiling is never repaired in place.",
            )
    # An authorization sealed before the digest existed carries neither key and
    # therefore no recovery permission at all: it reads as the empty ceiling.
    return body


def _load_enrolled_policy(state_dir: Path, policy_id: str) -> tuple[dict[str, Any], str]:
    """Load and verify the human-enrolled policy that is the root of this authority.

    Imported lazily because the handoff module depends on this one at import time.
    The policy is verified with the enrollment (approval) key, never with the
    coordinator's job key, so a job-sealing key cannot mint authority here.
    """

    from . import handoff  # noqa: PLC0415 - breaks the import cycle by design

    policy = handoff.load_policy(Path(state_dir), str(policy_id))
    return policy, handoff.policy_digest(policy)


_DERIVED_IDENTITY = (
    "kind", "job_id", "statement", "repo", "base_rev", "scope", "budget", "gates",
    "policy", "derived_from", "dependency_evidence", "descendant", "recovery_digest",
)


def _derived_identity(body: Mapping[str, Any]) -> dict[str, Any]:
    return {key: body.get(key) for key in _DERIVED_IDENTITY}


def record_derived_authorization(
    state_dir: Path,
    *,
    job_id: str,
    parent_job_id: str,
    statement: str,
    repo: str,
    candidate_rev: str,
    scope: Sequence[str],
    bees: int,
    wall_clock_s: int,
    policy_id: str,
    dependency_feature: str = "",
    dependent_feature: str = "",
    dependency_evidence: Mapping[str, Any] | None = None,
    gates: Sequence[str] = ("merge",),
    by: str = "coordinator(derived)",
) -> dict[str, Any]:
    """Seal a narrow, derived authority for one dependent job.

    This does not create authority: it records that an already-approved descendant
    policy was resolved to a concrete commit by validated evidence. The ceiling is
    the *sealed, enrolled* policy read back from disk — never a caller-supplied
    scope — and every dimension (repository, dependency, scope, budget, gates,
    dependency evidence, resolved revision) is compared against it.
    """

    policy, policy_digest = _load_enrolled_policy(state_dir, policy_id)
    parent = load_authorization(state_dir, parent_job_id)

    dependency_jobs = [str(item) for item in policy.get("dependencies") or []]
    dependency_features = [str(item) for item in policy.get("dependency_features") or []]
    if len(dependency_jobs) != 1 or len(dependency_features) != 1:
        raise JobError("policy_ambiguous", f"policy {policy.get('policy_id')} does not name exactly one dependency",
                       "Enroll one bounded policy per dependent job.")
    if str(parent_job_id) != dependency_jobs[0]:
        raise JobError("derived_dependency_mismatch",
                       f"policy {policy.get('policy_id')} is not bound to dependency job {parent_job_id!r}",
                       "Enroll the policy against this dependency.")
    feature = str(dependency_feature or dependency_features[0])
    if feature != dependency_features[0]:
        raise JobError("derived_dependency_mismatch", f"{feature!r} is not the policy's dependency feature",
                       "Name the feature the policy was enrolled for.")
    if str(repo) != str(policy.get("repo") or ""):
        raise JobError("derived_repo_mismatch", f"{repo!r} is not the policy's repository",
                       "A derived authority may not change repository.")
    dependent = str(dependent_feature or policy.get("dependent_feature") or "")
    if dependent and dependent != str(policy.get("dependent_feature") or ""):
        raise JobError("derived_feature_mismatch", f"{dependent!r} is not the policy's dependent feature",
                       "Enroll a policy bound to this feature.")
    if str(policy.get("dependent_job") or "") != _job_id(job_id):
        raise JobError("derived_job_mismatch", f"policy is bound to dependent job {policy.get('dependent_job')!r}",
                       "Enroll a policy bound to this job id.")

    revision = str(candidate_rev or "").strip().lower()
    if not _is_sha(revision):
        raise JobError("derived_revision_invalid", "the derived source revision must be a full 40-hex commit",
                       "Resolve the dependency's accepted commit first.")
    from . import descendants  # noqa: PLC0415 - descendants imports this module at import time

    try:
        accepted = descendants.accepted_commit(Path(state_dir), str(parent_job_id))
    except descendants.DescendantError as exc:
        raise JobError(exc.code, exc.message, exc.fix) from exc
    if accepted != revision:
        raise JobError("derived_revision_mismatch",
                       f"{revision} is not dependency {parent_job_id}'s accepted commit {accepted}",
                       "Use the exact accepted integration commit, or ask the human for a new policy.")

    scope_globs = _normalize_scope(scope)
    if not scope_globs:
        raise JobError("derived_scope_missing", "a derived authorization needs a file scope", _FIX_SCOPE)
    ceiling = _normalize_scope(policy.get("scope") or [])
    outside = [p for p in scope_globs if not _covered(p, ceiling)]
    if outside:
        raise JobError("derived_scope_widened", f"outside the enrolled policy scope: {', '.join(outside)}",
                       "A derived authority may only narrow the enrolled policy.")

    budget = policy.get("budget") or {}
    if int(bees) < 1 or int(bees) > int(budget.get("bees") or 0):
        raise JobError("derived_budget_exceeded", f"bees={bees} exceeds the enrolled policy budget",
                       "Stay inside the enrolled envelope.")
    if int(wall_clock_s) < 1 or int(wall_clock_s) > int(budget.get("wall_clock_s") or 0):
        raise JobError("derived_budget_exceeded", f"wall_clock_s={wall_clock_s} exceeds the enrolled policy budget",
                       "Stay inside the enrolled envelope.")

    policy_gates = sorted({_slug(gate) for gate in policy.get("gates") or []}) or ["merge"]
    wanted_gates = sorted({_slug(gate) for gate in gates}) or ["merge"]
    if not set(wanted_gates).issubset(policy_gates):
        raise JobError("derived_gate_widened", f"gates {wanted_gates} are not all enrolled in the policy {policy_gates}",
                       "A derived authority may not add a gate.")

    evidence = dict(dependency_evidence or {})
    if not evidence:
        evidence = {"job_id": str(parent_job_id), "accepted_commit": revision, "attempts": []}
    if str(evidence.get("job_id")) != str(parent_job_id) or str(evidence.get("accepted_commit")) != revision:
        raise JobError("derived_evidence_mismatch",
                       "the dependency evidence does not name this dependency and its accepted commit",
                       "Re-read the dependency's accepted wave.")

    body = {
        "kind": "authorization",
        "job_id": _job_id(job_id),
        "auth_id": secrets.token_hex(8),
        "statement": str(statement),
        "repo": str(repo),
        "base_rev": revision,
        "scope": scope_globs,
        "budget": {"bees": int(bees), "wall_clock_s": int(wall_clock_s)},
        "gates": wanted_gates,
        "by": by,
        "at": _utc_now(),
        "authority": "derived",
        "policy": {"policy_id": str(policy.get("policy_id")), "policy_digest": policy_digest},
        "derived_from": {"job_id": str(parent_job_id), "auth_id": parent.get("auth_id"),
                         "base_rev": parent.get("base_rev")},
        "dependency_evidence": evidence,
        "descendant": {"policy": "rizzstack.descendant-authorization/1",
                       "root_rev": str(policy.get("root_rev")),
                       "dependencies": dependency_jobs,
                       "dependency_features": dependency_features,
                       "dependent_feature": str(policy.get("dependent_feature") or ""),
                       "dependent_job": str(policy.get("dependent_job") or ""),
                       "scope": ceiling},
        # A derived authorization is not a human authorization: it cannot mint a
        # recovery ceiling. Descendant jobs run without retry or correction and
        # escalate instead, which is the fail-closed reading of "recovery
        # permission originates in the sealed human authorization".
        "recovery_digest": recovery_digest({}),
    }
    key = _ensure_key(state_dir)
    path = authorization_path(state_dir, job_id)
    if _create_only(path, _canonical(_seal(body, key)) + b"\n", mode=0o400):
        return body
    existing = _unseal(path, key, "authorization")
    if _derived_identity(existing) == _derived_identity(body):
        return existing
    raise JobError("derived_authorization_exists", f"a different authorization already exists for {job_id}",
                   "Refuse: derived authority is create-only.")


def record_approval(
    state_dir: Path,
    *,
    job_id: str,
    gate: str,
    decision: str,
    statement: str,
    require_tty: bool = True,
    interactive: bool | None = None,
    by: str | None = None,
) -> dict[str, Any]:
    """Record a gate decision. Only a TTY can write one."""

    is_tty = sys.stdin.isatty() if interactive is None else bool(interactive)
    if require_tty and not is_tty:
        raise JobError(
            "approval_not_interactive",
            "a gate decision must be typed at a terminal",
            "Have the approver run `bundlebees job approve` themselves.",
        )
    verdict = str(decision or "").strip().lower()
    if verdict not in {"granted", "refused"}:
        raise JobError("approval_invalid", "decision must be 'granted' or 'refused'", _FIX_AUTH)

    path = approval_path(state_dir, job_id, gate)
    key = _ensure_key(state_dir)
    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "approval",
        "job_id": _job_id(job_id),
        "gate": _slug(gate),
        "decision": verdict,
        "statement": str(statement or "").strip(),
        "by": by or _current_user(),
        "at": _utc_now(),
    }
    if not _create_only(path, _canonical(_seal(body, key)) + b"\n", mode=0o400):
        raise JobError(
            "approval_exists",
            "this gate already has a recorded decision",
            "A gate is decided once; start a new job for new work.",
        )
    return body


def load_approval(state_dir: Path, job_id: str, gate: str) -> dict[str, Any]:
    """Load and verify one recorded gate decision.

    A file at ``approvals/<job>.<gate>.json`` is *not* a decision until its
    sealed envelope verifies, its job id and gate match the request exactly,
    and its verdict is one of ``granted`` or ``refused``. A hand-written
    ``{}``, a copied file, or a flipped verdict raises a named refusal instead
    of reading as consent.
    """

    identity = _job_id(job_id)
    name = _slug(gate)
    try:
        key = _load_key(state_dir)
    except LeaseError as exc:
        raise JobError(
            "approval_missing",
            f"no approval decision exists for job {identity!r} gate {name!r}",
            _FIX_AUTH,
        ) from exc
    body = _unseal(approval_path(state_dir, identity, name), key, "approval")
    if str(body.get("kind")) != "approval":
        raise JobError(
            "approval_invalid", "the approval document is not an approval record", _FIX_TAMPERED
        )
    if str(body.get("job_id")) != identity:
        raise JobError(
            "approval_mismatch",
            f"the approval document names job {body.get('job_id')!r}, not {identity!r}",
            "Refuse it; a gate decision never carries to another job.",
        )
    if str(body.get("gate")) != name:
        raise JobError(
            "approval_mismatch",
            f"the approval document decides gate {body.get('gate')!r}, not {name!r}",
            "Refuse it; a gate decision never carries to another gate.",
        )
    if str(body.get("decision")) not in {"granted", "refused"}:
        raise JobError(
            "approval_invalid", "the approval document has no valid decision", _FIX_TAMPERED
        )
    return body


def gate_decision(state_dir: Path, job_id: str, gate: str) -> str | None:
    """Return ``granted``/``refused`` for a verified decision, else ``None``.

    A missing file is ``None``. A file that exists but does not verify raises
    with its own code: a damaged or forged record is never read as "no
    decision", because that would let it disappear from a report silently.
    """

    path = Path(state_dir) / APPROVALS_DIR / f"{_job_id(job_id)}.{_slug(gate)}.json"
    if not path.exists():
        return None
    return str(load_approval(state_dir, job_id, gate)["decision"])


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def create_job(
    state_dir: Path,
    *,
    job_id: str,
    tasks: Sequence[Mapping[str, Any]],
    summary: str = "",
    workflow_ref: str | None = None,
    packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a job that narrows an existing authorization.

    A workflow-native job carries its validated intent packet: the packet's
    ``job_id`` and repository must be this job's, its ``workflow_ref`` must equal
    the argument exactly (an omitted argument inherits the packet's pin), and the
    packet bytes are persisted once at ``packets/<job_id>.json`` with their digest
    sealed into the job. Legacy packet-free jobs keep working unchanged.
    """

    authorization = load_authorization(state_dir, job_id)
    normalized_tasks = [_normalize_task(task, for_create=True) for task in tasks]
    approval = authorization.get("approval")
    approval = dict(approval) if isinstance(approval, Mapping) else {}
    approved_tasks_digest = str(approval.get("tasks_digest") or "")
    if approved_tasks_digest:
        from . import approval as approval_mod  # noqa: PLC0415 - approval imports this module lazily

        try:
            digest = approval_mod.tasks_digest(normalized_tasks)
        except approval_mod.ApprovalError as exc:
            raise JobError(exc.code, exc.message, exc.fix) from exc
        if digest != approved_tasks_digest:
            # The owner approved an exact task set. A job that substitutes,
            # adds or rewrites tasks is a different trial run, even when every
            # lease still fits the scope and the bee budget.
            raise JobError(
                "approval_scope_changed",
                "the job's tasks are not the task set the owner approved",
                "Re-request approval for the exact task list; do not substitute tasks.",
            )
    packet_digest = ""
    if packet is not None:
        packet_digest = packets.validate(packet)
        if str(packet.get("job_id")) != str(job_id):
            raise JobError("packet_job_mismatch", "the packet names another job", "Refuse: packet and job identity must match.")
        if str((packet.get("repository") or {}).get("identity")) != str(authorization.get("repo")):
            raise JobError("packet_repo_mismatch", "the packet names another repository", "Refuse.")
        pinned = str(packet.get("workflow_ref") or "")
        if workflow_ref is None:
            workflow_ref = pinned
        elif str(workflow_ref) != pinned:
            raise JobError("workflow_ref_mismatch",
                           f"workflow_ref {workflow_ref!r} is not the packet's pinned {pinned!r}",
                           "Refuse: a job runs under exactly the protocol it was admitted with.")

    path = job_path(state_dir, job_id)
    if path.exists():
        raise JobError(
            "job_exists",
            "a job already exists for this id",
            "Pick a new job id; the recorded job is the record.",
        )

    body = {
        "schema_version": JOB_SCHEMA_VERSION,
        "kind": "job",
        "job_id": _job_id(job_id),
        "authorization": {
            "ref": f"{AUTH_DIR}/{_job_id(job_id)}.json",
            "auth_id": authorization["auth_id"],
            # The recovery ceiling this job was admitted under, pinned by the
            # digest sealed inside the authorization. A job is a mutable
            # document; this pin is what makes a recovery block rewritten after
            # admission visible instead of authoritative.
            "recovery_digest": str(authorization.get("recovery_digest") or ""),
            "authority": str(authorization.get("authority") or "human"),
            "grant": dict(authorization.get("grant") or {}),
            "grant_id": str(authorization.get("grant_id") or ""),
            "grant_statement": str(authorization.get("grant_statement") or ""),
            "grant_approved_at": str(authorization.get("grant_approved_at") or ""),
            "approval": approval,
        },
        "goal": {
            "summary": str(summary or authorization["statement"]),
            "repo": authorization["repo"],
            "base_rev": authorization["base_rev"],
            # The protocol commit the feature is executed under. Pinned at creation;
            # a resumed job never reinterprets itself under a different workflow.
            "workflow_ref": str(workflow_ref or ""),
            "packet_digest": packet_digest,
        },
        "tasks": normalized_tasks,
        "integration": {
            "wave": "w1",
            "state": "pending",
            "evidence": None,
            # The durable name of the wave record this job is working on. It is
            # derived from the job id alone, so a crashed tick can find the
            # record it wrote without trusting an in-memory copy.
            "wave_id": _wave_id(_job_id(job_id), "w1"),
        },
        "approval_gate": {
            "required": True,
            "gates": list(authorization["gates"]),
            "state": "waiting",
        },
        "injected_failures": [],
        "events": [],
    }
    _enforce_narrowing(body, authorization)

    if packet is not None:
        packets.store_packet(state_dir, job_id, packet)
    save_job(state_dir, body)
    return body


def _wave_index(value: Any) -> int:
    """The raw integer a schema wave value names (``"w2"`` -> 2); 0 if unusable."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value or "").strip().lower().lstrip("w")
    try:
        return int(text)
    except ValueError:
        return 0


def _wave_number(value: Any) -> int:
    """Accept the schema's ``"w1"`` as well as a plain integer wave number."""
    return max(1, _wave_index(value))


def _wave_id(job_id: Any, wave: Any) -> str:
    from . import revision  # noqa: PLC0415 - wave names are revision's business

    return revision.wave_id_for(str(job_id or ""), wave)


def _wave_record(state_dir: Path, wave_id: str) -> dict[str, Any] | None:
    """Read one wave record: absent is ``None``, damaged is a named refusal."""
    from . import revision  # noqa: PLC0415

    try:
        return revision.read_wave(Path(state_dir), wave_id)
    except revision.RevisionError as exc:
        if exc.code == revision.WAVE_MISSING:
            return None
        raise JobError(exc.code, exc.error, exc.fix) from exc


def recover_integration(state_dir: Path, job: dict[str, Any]) -> bool:
    """Adopt an accepted wave that a crash left out of this job document.

    A wave record is published before the job is saved, so a crash between the
    two leaves durable proof that the wave was accepted next to a job that does
    not say so. Re-running integration from that state would commit the same
    work twice, so the accepted record is adopted instead: the tasks it
    integrated move to ``accepted``, the wave counter advances, and the record's
    head commit becomes ``last_commit``. Evidence that exists but cannot be read
    raises with its own code; it is never read as "no accepted wave".
    """
    job_id = str(job.get("job_id") or "").strip()
    integration = job.get("integration")
    if not job_id or not isinstance(integration, dict):
        return False
    wave = _wave_number(integration.get("wave", 1))
    settled = [entry for entry in (integration.get("accepted_waves") or []) if isinstance(entry, Mapping)]
    if any(_wave_index(entry.get("wave", 0)) >= wave for entry in settled):
        return False
    wave_id = _wave_id(job_id, wave)
    record = _wave_record(state_dir, wave_id)
    if record is None or record.get("accepted") is not True:
        return False

    named = str(record.get("wave_id") or wave_id)
    if named != wave_id:
        raise JobError(
            "wave_evidence_mismatch",
            f"wave record {wave_id} names itself {named!r}",
            "Inspect the waves directory by hand; do not integrate again.",
        )
    head = str(record.get("head_sha") or "")
    if not _is_sha(head):
        raise JobError(
            "wave_evidence_invalid",
            f"accepted wave {wave_id} does not name a full commit",
            "Inspect the wave record; an incomplete acceptance is not evidence.",
        )
    listed = record.get("attempts")
    if not isinstance(listed, (list, tuple)) or not listed:
        raise JobError(
            "wave_evidence_invalid",
            f"accepted wave {wave_id} lists no attempts",
            "Inspect the wave record; an acceptance with no attempts is not evidence.",
        )
    pending = [task for task in job.get("tasks") or [] if task.get("state") in {"verified", "integrating"}]
    integrated = sorted(str((task.get("attempts") or [None])[-1]) for task in pending)
    if sorted(str(item) for item in listed) != integrated:
        raise JobError(
            "wave_evidence_mismatch",
            f"accepted wave {wave_id} integrates {sorted(str(item) for item in listed)}, "
            f"which is not this job's pending verification {integrated}",
            "Inspect the wave record and the job; do not integrate again.",
        )

    for task in pending:
        task_id = str(task.get("task_id") or "")
        if task.get("state") == "verified":
            transition(job, task_id, "integrating", actor="recovery",
                       reason=f"wave {wave_id} was already accepted")
        transition(job, task_id, "accepted", actor="recovery",
                   reason=f"adopted accepted wave {wave_id} after a crash",
                   evidence=f"recovered wave {wave_id} at {head[:12]}")

    integration.update(
        accepted_waves=[
            *settled,
            {
                "wave": wave,
                "at": str(record.get("at") or ""),
                "evidence": f"recovered accepted wave {wave_id} at {head}",
                "commit": head,
                "recovered": True,
            },
        ],
        wave=wave + 1,
        wave_id=_wave_id(job_id, wave + 1),
        attempts=0,
        state="pending",
        last_commit=head,
        recovered={"wave": wave, "wave_id": wave_id, "head_sha": head,
                   "at": str(record.get("at") or "")},
    )
    return True


def load_job(state_dir: Path, job_id: str, *, recover: bool = True) -> dict[str, Any]:
    """Load a sealed job. Absent, unreadable and unsealed are different answers.

    With ``recover`` (the default) an accepted wave record that the crash window
    left out of the document is adopted in memory, so every reader — the queue's
    completion check included — sees the evidence that exists on disk. Nothing is
    written here; ``save_job`` persists the adoption, and a caller that wants to
    be explicit about it passes ``recover=False`` and calls
    :func:`recover_integration` itself.
    """

    path = job_path(state_dir, job_id)
    if not path.exists():
        raise JobError(
            "job_missing",
            f"no job exists for id {job_id!r}",
            _FIX_AUTH,
        )
    try:
        key = _load_key(state_dir)
    except LeaseError as exc:
        raise JobError(
            "job_unsealed",
            f"job {job_id!r} exists but its signing key is missing; it is not absent",
            _FIX_TAMPERED,
        ) from exc
    body = _unseal(path, key, "job")
    if recover:
        recover_integration(Path(state_dir), body)
    return body


def _refuse_lost_evidence(previous: Mapping[str, Any], job: Mapping[str, Any]) -> None:
    """A save may add accepted-wave evidence; it may never drop or rewind it."""
    before = previous.get("integration")
    after = job.get("integration")
    before = before if isinstance(before, Mapping) else {}
    after = after if isinstance(after, Mapping) else {}
    prior = list(before.get("accepted_waves") or [])
    current = list(after.get("accepted_waves") or [])
    if len(current) < len(prior) or any(left != right for left, right in zip(prior, current)):
        raise JobError(
            "accepted_wave_dropped",
            "this save would drop or rewrite an accepted wave record",
            "Reload the job; accepted integration evidence is append-only.",
        )
    if _wave_number(before.get("wave", 1)) > _wave_number(after.get("wave", 1)):
        raise JobError(
            "integration_wave_regressed",
            "this save would move the job back to an earlier wave",
            "Reload the job; the wave counter only moves forward.",
        )


def save_job(state_dir: Path, job: Mapping[str, Any]) -> None:
    """Persist a job, refusing to overwrite an unreadable one or lose evidence."""

    job_id = _job_id(str(job.get("job_id") or ""))
    path = job_path(state_dir, job_id)
    goal = job.get("goal") or {}
    digest = str(goal.get("packet_digest") or "")
    previous = None
    if path.exists():
        try:
            key = _load_key(state_dir)
        except LeaseError as exc:
            raise JobError(
                "job_unsealed",
                f"job {job_id!r} exists but its signing key is missing; refusing to overwrite it",
                _FIX_TAMPERED,
            ) from exc
        # An unreadable, unsealed or tampered document raises here: it is never
        # treated as "no previous job" and never replaced.
        previous = _unseal(path, key, "job")
    if previous is not None:
        before = str((previous.get("goal") or {}).get("packet_digest") or "")
        if before and not digest:
            raise JobError("packet_digest_missing",
                           "the sealed job carries a packet digest and this save would drop it",
                           "Refuse: a job keeps the packet it was admitted under.")
        if before != digest:
            raise JobError("packet_identity_changed", "the job would resume under a different packet identity",
                           "Refuse: a sealed job keeps the protocol and packet it started with.")
        _refuse_lost_evidence(previous, job)
    if digest:
        try:
            packets.verify_stored(state_dir, job_id, digest)
        except packets.PacketError as exc:
            raise JobError(exc.code, exc.message, exc.fix) from exc
    document = dict(job)
    integration = document.get("integration")
    if isinstance(integration, Mapping):
        # Keep the durable wave name truthful: it is derived from the job id and
        # the wave counter, and a stale pin is worse than no pin at all.
        refreshed = dict(integration)
        refreshed["wave_id"] = _wave_id(job_id, refreshed.get("wave", 1))
        document["integration"] = refreshed
    key = _ensure_key(state_dir)
    _atomic_write(path, _canonical(_seal(document, key)) + b"\n")


def transition(
    job: dict[str, Any],
    task_id: str,
    to_state: str,
    *,
    actor: str,
    reason: str = "",
    evidence: str | None = None,
) -> dict[str, Any]:
    """Move one task forward, enforcing the state machine and dependencies."""

    task = _task(job, task_id)
    current = task.get("state", "pending")
    if to_state not in _LEGAL_TRANSITIONS.get(current, ()):
        raise JobError(
            "job_bad_transition",
            f"task {task_id} cannot move from {current!r} to {to_state!r}",
            _FIX_TRANSITION,
        )
    if to_state == "dispatching":
        _require_dependencies_accepted(job, task)

    task["state"] = to_state
    if evidence:
        task["evidence"] = evidence
    job.setdefault("events", []).append(
        {
            "at": _utc_now(),
            "task_id": task_id,
            "transition": f"{current}->{to_state}",
            "actor": str(actor or "reconciler"),
            "reason": str(reason or ""),
        }
    )
    return job


def _task(job: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    for task in job.get("tasks", []):
        if task.get("task_id") == task_id:
            return task  # type: ignore[return-value]
    raise JobError("job_invalid", f"job has no task {task_id!r}", _FIX_TRANSITION)


def _require_dependencies_accepted(job: Mapping[str, Any], task: Mapping[str, Any]) -> None:
    states = {t.get("task_id"): t.get("state") for t in job.get("tasks", [])}
    unmet = [dep for dep in task.get("depends_on") or [] if states.get(dep) != "accepted"]
    if unmet:
        raise JobError(
            "task_dependency_unmet",
            f"task {task.get('task_id')!r} depends on unfinished task(s): {', '.join(unmet)}",
            "Wait for the dependency to be accepted, or remove it from the plan.",
        )


# ---------------------------------------------------------------------------
# Narrowing
# ---------------------------------------------------------------------------


def _normalize_task(task: Mapping[str, Any], *, for_create: bool = False) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        raise JobError("job_invalid", "every task needs a task_id", _FIX_TRANSITION)
    lease = _normalize_scope(task.get("lease") or [])
    if not lease:
        raise JobError("job_invalid", f"task {task_id} needs at least one leased path", _FIX_SCOPE)
    depends_on = task.get("depends_on") or []
    if not isinstance(depends_on, (list, tuple)):
        raise JobError("job_invalid", f"task {task_id} depends_on must be a list", _FIX_TRANSITION)
    provision = task.get("provision")
    body = {
        "task_id": task_id,
        "objective": str(task.get("objective") or ""),
        "lease": lease,
        "depends_on": [str(dep) for dep in depends_on],
        "contract_path": task.get("contract_path"),
        "contract_sha256": task.get("contract_sha256"),
        "acceptance": task.get("acceptance"),
        "contract": task.get("contract"),
        "provision": dict(provision) if isinstance(provision, Mapping) else None,
    }
    if for_create:
        # A new job always starts at the beginning: an author-supplied state,
        # attempt list, binding or verification is never trusted as history.
        body.update(binding=None, provisioning=None, state="pending", attempts=[], verify=None, intent=None)
    else:
        # Restoring a persisted document: these fields are written by the
        # coordinator, not by the caller of create_job().
        body.update(
            binding=task.get("binding"), provisioning=task.get("provisioning"),
            state=str(task.get("state") or "pending"), attempts=list(task.get("attempts") or []),
            verify=task.get("verify"), intent=task.get("intent"),
        )
    return body


def _normalize_scope(scope: Iterable[Any]) -> list[str]:
    globs: list[str] = []
    for entry in scope or []:
        text = str(entry or "").strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/") or ".." in Path(text).parts:
            raise JobError(
                "lease_out_of_scope",
                f"scope entry {text!r} is absolute or escapes the repository",
                _FIX_SCOPE,
            )
        while text.startswith("./"):
            text = text[2:]
        globs.append(text)
    return sorted(set(globs))


def _enforce_narrowing(job: Mapping[str, Any], authorization: Mapping[str, Any]) -> None:
    if job["goal"]["repo"] != authorization["repo"]:
        raise JobError(
            "authorization_widened",
            f"job repo {job['goal']['repo']!r} differs from the authorized repo {authorization['repo']!r}",
            _FIX_AUTH,
        )
    if job["goal"]["base_rev"] != authorization["base_rev"]:
        raise JobError(
            "authorization_widened",
            "job base_rev differs from the authorized base_rev",
            _FIX_AUTH,
        )
    budget = authorization["budget"]
    if len(job["tasks"]) > int(budget["bees"]):
        raise JobError(
            "authorization_widened",
            f"job has {len(job['tasks'])} tasks but the authorization allows {budget['bees']} bees",
            _FIX_AUTH,
        )
    for task in job["tasks"]:
        outside = [path for path in task["lease"] if not _covered(path, authorization["scope"])]
        if outside:
            raise JobError(
                "lease_out_of_scope",
                f"task {task['task_id']} leases path(s) outside the authorized scope: {', '.join(outside)}",
                _FIX_SCOPE,
            )


def _covered(path: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in globs)


def _is_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text.lower())


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def job_digest(job: Mapping[str, Any]) -> str:
    """A stable digest other documents can reference."""

    return hashlib.sha256(_canonical(dict(job))).hexdigest()
