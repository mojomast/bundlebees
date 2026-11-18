"""Board -> dispatcher bridge: turn an approved ready card into dispatchable work.

The Kanban board and the bundlebees factory driver had no common surface: the
board held approved work in ``ready`` while the driver's queue and pre-approval
directory sat empty, so nothing was ever dispatched.  This module is that
surface, and it is deliberately the only writer of a board-derived queue
feature.

One stock pass:

1. reads the board's ``ready`` column through the public ``hermes kanban`` CLI
   (never the SQLite file, never a card body as authority);
2. loads the durable owner approval record for the card at
   ``<state>/factory/board-approvals/<task_id>.json`` -- the record carries the
   approved brief, the repository and the id of an authorized job.  A card
   without that record is never stocked: the bridge fails closed and the record
   is never inferred from a card, a title or a comment;
3. validates the brief with the strict brief validator (full ``source_rev``,
   repository-relative non-empty lease, acceptance) and requires the named job
   document to exist, because a feature whose job is missing can never dispatch
   (the queue stage would only mark it ``needs_approval``);
4. writes, idempotently:

   * one feature in ``queue.json`` -- the live docket envelope
     (``schema``/``generated_at``/``note``) is preserved and existing features
     are kept verbatim;
   * one ``factory/dispatch-approvals/<work_id>.json`` pre-approval record.

A replay with the same inputs changes nothing.  A different job, repository or
scope at the same identity is a conflict and is never overwritten; a human
decides.  Every refusal is reported per card, so one bad card never stops the
others.

Nothing here dispatches, claims, merges, restarts or approves.  Stocking is the
whole effect; the resident factory driver picks the feature up on its next pass
and owns the dispatch decision.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import board_stock
from . import brief as brief_mod
from . import factory_driver
from . import job as job_mod
from .security import child_env, scrub

SCHEMA = "bundlebees.board-dispatch/1"
APPROVAL_SCHEMA = "bundlebees.board-approval/1"
APPROVAL_DIRNAME = ("factory", "board-approvals")
WORK_ID_PREFIX = "bd_"
BOARD_ENV = "BUNDLEBEES_BOARD_DISPATCH_BOARD"

_KANBAN_TIMEOUT_S = 60
_MAX_CARDS = 10_000
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_FIX_APPROVAL = (
    "Write <state>/factory/board-approvals/<task_id>.json with schema "
    f"{APPROVAL_SCHEMA!r}, approved: true, approved_by, repo, job_id and the brief; "
    "the bridge never infers consent."
)


class BoardDispatchError(Exception):
    """A refusal that stops the pass (the board or the queue could not be read)."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def approval_path(state_dir: Any, task_id: Any) -> Path:
    """The owner approval record for one card (input, never written here)."""

    name = str(task_id if task_id is not None else "").strip()
    if not _SAFE_ID_RE.match(name):
        name = "invalid-" + re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64]
    return Path(state_dir).joinpath(*APPROVAL_DIRNAME) / f"{name}.json"


def work_id_for(task_id: Any) -> str:
    """The deterministic dispatch work id for one card."""

    text = str(task_id if task_id is not None else "").strip()
    if not _SAFE_ID_RE.match(text):
        raise BoardDispatchError(
            "task_id_invalid",
            f"card id {text!r} is not a safe identifier",
            "Fix the card id; the bridge never invents one.",
        )
    return f"{WORK_ID_PREFIX}{text}"


def _queue_path(state_dir: Any) -> Path:
    return Path(state_dir) / "queue.json"


def _job_path(state_dir: Any, job_id: str) -> Path:
    return Path(state_dir) / "jobs" / f"{job_id}.json"


# ---------------------------------------------------------------------------
# Board read (public CLI only)
# ---------------------------------------------------------------------------


def _default_runner(argv: list[str]) -> str:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_KANBAN_TIMEOUT_S,
        env=child_env(),
    )
    if completed.returncode != 0:
        raise OSError(
            f"kanban exited {completed.returncode}: {scrub(completed.stderr or '')}"
        )
    return completed.stdout


def ready_cards(
    *,
    board: str = "",
    runner: Callable[[list[str]], str] | None = None,
) -> list[dict[str, Any]]:
    """The cards the board reports in ``ready``, through the public CLI."""

    run = runner or _default_runner
    try:
        raw = run(board_stock.kanban_argv(board))
    except BoardDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001 - any unreadable board is one refusal
        raise BoardDispatchError(
            "board_unreadable",
            f"the kanban ready column could not be read: {type(exc).__name__}",
            "Check that the hermes CLI and the board exist; nothing is stocked "
            "from an unread board.",
        ) from exc
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BoardDispatchError(
            "board_unreadable",
            "the kanban ready payload is not JSON",
            "Run `hermes kanban list --status ready --json` by hand.",
        ) from exc
    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("tasks")
    if not isinstance(rows, list):
        raise BoardDispatchError(
            "board_unreadable",
            "the kanban ready payload is not a task list",
            "Run `hermes kanban list --status ready --json` by hand.",
        )
    cards = [dict(row) for row in rows[:_MAX_CARDS] if isinstance(row, Mapping)]
    return cards


# ---------------------------------------------------------------------------
# Approval record (input)
# ---------------------------------------------------------------------------


def _load_approval(
    state_dir: Any, task_id: str
) -> tuple[dict[str, Any] | None, str, str]:
    """Return ``(approval, reason, detail)``; reason is empty when approved."""

    path = approval_path(state_dir, task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "approval_absent", ""
    except OSError as exc:
        return None, "approval_unreadable", scrub(f"the record could not be read: {exc}")
    try:
        body = json.loads(raw)
    except ValueError:
        return None, "approval_unreadable", "the record is not JSON"
    if not isinstance(body, Mapping):
        return None, "approval_unreadable", "the record is not a JSON object"
    if str(body.get("schema") or "") != APPROVAL_SCHEMA:
        return None, "approval_unreadable", f"the record is not schema {APPROVAL_SCHEMA!r}"
    if str(body.get("task_id") or "") != task_id:
        return None, "approval_unreadable", "the record names another card"
    if body.get("approved") is not True:
        return None, "approval_invalid", "approved is not true"
    approved_by = body.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        return None, "approval_invalid", "approved_by is missing"
    if not isinstance(body.get("approved_at"), str) or not str(body.get("approved_at")).strip():
        return None, "approval_invalid", "approved_at is missing"
    repo = body.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo.strip()):
        return None, "approval_invalid", "repo is not 'owner/name'"
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not _SAFE_ID_RE.match(job_id.strip()):
        return None, "approval_invalid", "job_id is not a safe identifier"
    brief = body.get("brief")
    if not isinstance(brief, Mapping):
        return None, "approval_invalid", "brief is missing"
    return dict(body), "", ""


# ---------------------------------------------------------------------------
# Queue feature (output)
# ---------------------------------------------------------------------------


def _read_queue_document(path: Path) -> tuple[str, Any]:
    """Return ``("features", doc)`` or ``("map", doc)`` for the two live shapes."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "features", {"schema": "bundlebees.queue/1", "features": []}
    except OSError as exc:
        raise BoardDispatchError(
            "queue_unreadable", f"{path} could not be read: {exc}", "Repair the queue file."
        ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise BoardDispatchError(
            "queue_invalid", f"{path} is not valid JSON: {exc}", "Repair the queue file."
        ) from exc
    if not isinstance(document, dict):
        raise BoardDispatchError(
            "queue_invalid",
            "the queue file is neither a feature map nor a features document",
            "Repair the queue file: use a features list or a feature-id map.",
        )
    if "features" in document:
        if not isinstance(document.get("features"), list):
            raise BoardDispatchError(
                "queue_invalid",
                "the queue 'features' value is not a list",
                "Repair the queue file: features must be a JSON array.",
            )
        return "features", document
    return "map", document


def _feature_rows(shape: str, document: Any) -> list[dict[str, Any]]:
    if shape == "features":
        return [dict(row) for row in document.get("features") or [] if isinstance(row, Mapping)]
    return [
        dict(row, feature_id=str(feature_id))
        for feature_id, row in document.items()
        if isinstance(row, Mapping)
    ]


def _write_queue_document(path: Path, document: Any, *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _upsert_feature(
    path: Path,
    *,
    feature: Mapping[str, Any],
    dry_run: bool,
) -> tuple[str, str]:
    """Insert the feature, or return ``("unchanged"|"conflict", detail)``."""

    shape, document = _read_queue_document(path)
    rows = _feature_rows(shape, document)
    existing = next(
        (row for row in rows if str(row.get("feature_id") or "") == feature["feature_id"]),
        None,
    )
    if existing is not None:
        if str(existing.get("job_id") or "") != feature["job_id"]:
            return "conflict", "a feature with this id already names another job"
        if str(existing.get("repo") or "") != feature["repo"]:
            return "conflict", "a feature with this id already names another repository"
        return "unchanged", ""
    if shape == "features":
        document.setdefault("features", []).append(dict(feature))
        document["features"] = sorted(
            document["features"], key=lambda row: str(row.get("feature_id") or "")
        )
    else:
        document[feature["feature_id"]] = {
            key: value for key, value in feature.items() if key != "feature_id"
        }
    _write_queue_document(path, document, dry_run=dry_run)
    return "written", ""


# ---------------------------------------------------------------------------
# The stock pass
# ---------------------------------------------------------------------------


def stock(
    state_dir: Any,
    *,
    board: str = "",
    runner: Callable[[list[str]], str] | None = None,
    now: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stock every eligible ready card; return the per-card report."""

    cards = ready_cards(board=board, runner=runner)
    queue_file = _queue_path(state_dir)
    stocked: list[dict[str, Any]] = []
    would_stock: list[dict[str, Any]] = []
    unchanged: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    intended: list[str] = []

    def skip(card: Mapping[str, Any], task_id: str, reason: str, detail: str = "") -> None:
        skipped.append(
            {
                "task_id": task_id,
                "title": str(card.get("title") or ""),
                "reason": reason,
                "detail": scrub(detail)[:400],
            }
        )

    for card in cards:
        task_id = str(card.get("id") or "").strip()
        if not _SAFE_ID_RE.match(task_id):
            skip(card, task_id, "task_id_invalid", "the card id is not a safe identifier")
            continue
        claim = str(card.get("status") or "ready").strip()
        if claim and claim != "ready":
            skip(card, task_id, "not_ready", f"the card is {claim!r}")
            continue

        approval, reason, detail = _load_approval(state_dir, task_id)
        if approval is None:
            skip(card, task_id, reason, detail)
            continue
        brief = approval["brief"]
        try:
            brief_mod.validate_brief(dict(brief))
        except brief_mod.BriefError as exc:
            skip(card, task_id, "brief_invalid", f"{exc.error} ({exc.fix})")
            continue

        work_id = work_id_for(task_id)
        job_id = str(approval["job_id"]).strip()
        repo = str(approval["repo"]).strip()

        try:
            job_body = job_mod.load_job(Path(state_dir), job_id, recover=False)
        except job_mod.JobError as exc:
            reason = "job_absent" if exc.code == "job_missing" else "job_unreadable"
            detail = f"no job document for {job_id}" if reason == "job_absent" else exc.message
            skip(card, task_id, reason, detail)
            continue
        except (OSError, ValueError):
            skip(card, task_id, "job_unreadable", f"the job document for {job_id} is unreadable")
            continue
        if not isinstance(job_body, Mapping) or str(job_body.get("job_id") or "") != job_id:
            skip(card, task_id, "job_unreadable", f"the job document for {job_id} names another job")
            continue

        feature = {
            "feature_id": task_id,
            "repo": repo,
            "job_id": job_id,
            "title": str(card.get("title") or ""),
            "status": "queued",
        }

        # The queue feature first: a conflict here must stop this card before
        # the approval record is written, so the two never disagree.
        queue_after = queue_file
        try:
            action, conflict = _upsert_feature(queue_file, feature=feature, dry_run=dry_run)
        except BoardDispatchError:
            raise
        if action == "conflict":
            skip(card, task_id, "feature_conflict", conflict)
            continue

        approval_file = factory_driver.dispatch_approval_path(state_dir, work_id)
        existing = None
        if approval_file.exists():
            try:
                existing = factory_driver.read_dispatch_approval(state_dir, work_id)
            except factory_driver.FactoryError as exc:
                skip(card, task_id, "approval_conflict", exc.message)
                continue
            wanted_scope = sorted(str(item) for item in brief["assigned_files"])
            if (
                str(existing.get("job_id") or "") != job_id
                or sorted(str(item) for item in existing.get("scope") or []) != wanted_scope
            ):
                skip(card, task_id, "approval_conflict", "the pre-approval names another job or scope")
                continue
            action = "unchanged" if action == "unchanged" else "written"

        if action == "unchanged":
            unchanged.append({"task_id": task_id, "work_id": work_id, "job_id": job_id})
            continue

        if dry_run:
            intended.extend([str(queue_after), str(approval_file)])
            would_stock.append({"task_id": task_id, "work_id": work_id, "job_id": job_id})
            continue

        if existing is None:
            factory_driver.record_dispatch_approval(
                state_dir,
                work_id,
                job_id=job_id,
                scope=list(brief["assigned_files"]),
                approved=True,
                by=str(approval["approved_by"]).strip(),
                now=now,
            )
        stocked.append({"task_id": task_id, "work_id": work_id, "job_id": job_id})

    return {
        "schema": SCHEMA,
        "ok": True,
        "board": board,
        "checked": len(cards),
        "stocked": stocked,
        "would_stock": would_stock,
        "unchanged": unchanged,
        "skipped": skipped,
        "intended": intended,
        "dry_run": bool(dry_run),
        "at": _iso(now),
    }


def _iso(now: Any = None) -> str:
    if now is None:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(now, str) and now.strip():
        return now.strip()
    try:
        value = float(now)
    except (TypeError, ValueError):
        return str(now)
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def board_from_environment(environ: Mapping[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    return str(source.get(BOARD_ENV, "")).strip()
