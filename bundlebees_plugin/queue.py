"""Feature queue: intake, durable claim, and one-pass consumption.

The M1 job store stays authoritative for execution. A queue item is a pointer to
an authorized job plus durable claim/link state; a Kanban status is never treated
as evidence that work completed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from . import job as jobs


class QueueError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


STATES = ("queued", "claimed", "running", "ready_for_review", "blocked", "failed", "needs_approval")


@dataclass(frozen=True)
class Item:
    feature_id: str
    repo: str
    job_id: str
    title: str = ""
    depends_on: tuple[str, ...] = ()
    status: str = "queued"


class QueueAdapter(Protocol):
    def list_items(self) -> list[Item]: ...
    def get(self, feature_id: str) -> Item | None: ...
    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None: ...


class JsonQueue:
    """File-backed queue for tests and for hosts without a Kanban bridge."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise QueueError("queue_invalid", f"{self.path} is not valid JSON: {exc}", "Repair the queue file.") from exc

    def _save(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def list_items(self) -> list[Item]:
        items = []
        for feature_id, body in sorted(self._load().items()):
            items.append(Item(feature_id=feature_id, repo=str(body.get("repo") or ""),
                              job_id=str(body.get("job_id") or ""), title=str(body.get("title") or ""),
                              depends_on=tuple(body.get("depends_on") or ()),
                              status=str(body.get("status") or "queued")))
        return items

    def get(self, feature_id: str) -> Item | None:
        return next((item for item in self.list_items() if item.feature_id == feature_id), None)

    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None:
        data = self._load()
        if feature_id not in data:
            raise QueueError("queue_unknown", f"no queue item {feature_id}", "Add it first.")
        data[feature_id]["status"] = status
        if evidence is not None:
            data[feature_id]["evidence"] = dict(evidence)
        data[feature_id]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save(data)


class KanbanQueue:
    """Adapter over a Kanban CLI exposing JSON (``hermes kanban ... --json``).

    The command is injected so the adapter is not tied to undocumented internals;
    a failure to produce valid JSON is an error, not an empty queue.
    """

    def __init__(self, run: Any) -> None:
        self._run = run

    def _json(self, args: list[str]) -> Any:
        out = self._run(["kanban", *args, "--json"])
        if getattr(out, "returncode", 1) != 0:
            raise QueueError("kanban_failed", f"kanban {' '.join(args)} failed: {getattr(out, 'stderr', '')[:200]}", "Check the Kanban CLI.")
        try:
            return json.loads(out.stdout or "[]")
        except ValueError as exc:
            raise QueueError("kanban_invalid", "the Kanban CLI did not return JSON", "Use a supported CLI version.") from exc

    def list_items(self) -> list[Item]:
        items = []
        for card in self._json(["list"]):
            if not isinstance(card, Mapping):
                continue
            body = card.get("metadata") or card.get("fields") or {}
            if not isinstance(body, Mapping) or not body.get("job_id"):
                continue
            items.append(Item(feature_id=str(card.get("id")), repo=str(body.get("repo") or ""),
                              job_id=str(body["job_id"]), title=str(card.get("title") or ""),
                              depends_on=tuple(body.get("depends_on") or ()),
                              status=str(card.get("status") or "queued")))
        return items

    def get(self, feature_id: str) -> Item | None:
        return next((item for item in self.list_items() if item.feature_id == feature_id), None)

    def set_status(self, feature_id: str, status: str, evidence: Mapping[str, Any] | None = None) -> None:
        args = ["move", feature_id, status]
        if evidence is not None:
            args += ["--note", json.dumps(dict(evidence), sort_keys=True)[:500]]
        self._json(args)


class QueueStore:
    """Durable claim + link records, one file per feature."""

    def __init__(self, state_dir: Path) -> None:
        self.root = Path(state_dir) / "queue"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, feature_id: str) -> Path:
        safe = "".join(ch for ch in feature_id if ch.isalnum() or ch in "-_.")
        if safe != feature_id or not safe:
            raise QueueError("feature_id_invalid", "feature ids are [A-Za-z0-9._-]+", "Use the queue's id.")
        return self.root / f"{safe}.json"

    def read(self, feature_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self.path(feature_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise QueueError("queue_state_corrupt", f"{feature_id} state is unreadable: {exc}", "Inspect it by hand.") from exc

    def write(self, feature_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(record, feature_id=feature_id, updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        path = self.path(feature_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return body

    def claim(self, feature_id: str, *, job_id: str) -> dict[str, Any]:
        """O_EXCL claim: two consumers cannot both take the same feature."""
        path = self.path(feature_id)
        record = {"state": "claimed", "job_id": job_id, "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "pid": os.getpid()}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = self.read(feature_id) or {}
            raise QueueError("already_claimed", f"{feature_id} is already {existing.get('state')}", "Inspect or release it; do not run twice.") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        return record


def eligible(items: list[Item], done: set[str]) -> Item | None:
    for item in sorted(items, key=lambda i: i.feature_id):
        if item.status not in ("queued",) or item.feature_id in done:
            continue
        if all(dep in done for dep in item.depends_on):
            return item
    return None


def run_once(state_dir: Path, queue: QueueAdapter, *, tick: Any, on_status: Any = None) -> dict[str, Any]:
    """Claim one eligible feature and advance it through the existing tick.

    ``tick(job_id)`` is the production job tick (already wired to provisioning,
    launcher, verifier and integrator). This function never interprets a queue
    status as completion: the job document is the evidence.
    """

    store = QueueStore(state_dir)
    items = queue.list_items()
    done = {item.feature_id for item in items if item.status == "ready_for_review"}

    item = eligible(items, done)
    if item is None:
        return {"action": "idle", "feature": None}

    record = store.read(item.feature_id)
    if record is None:
        record = store.claim(item.feature_id, job_id=item.job_id)
        queue.set_status(item.feature_id, "claimed", {"job_id": item.job_id})

    # The crash window: claimed, but the job may not exist yet.
    job = _load_job_or_none(state_dir, item.job_id)
    if job is None:
        return {"action": "blocked", "feature": item.feature_id,
                "detail": f"claimed, but job {item.job_id} does not exist", "state": "needs_approval"}

    result = tick(item.job_id)

    state = str(getattr(result, "get", lambda *_: None)("status") or "")
    transitions = list(result.get("transitions") or []) if hasattr(result, "get") else []
    accepted = _job_accepted(state_dir, item.job_id)

    if accepted:
        store.write(item.feature_id, dict(record, state="ready_for_review", evidence=result))
        queue.set_status(item.feature_id, "ready_for_review", {"job_id": item.job_id, "transitions": transitions})
        outcome = "ready_for_review"
    elif state in ("blocked", "failed", "error"):
        store.write(item.feature_id, dict(record, state=state, evidence=result))
        queue.set_status(item.feature_id, "blocked" if state == "blocked" else "failed", {"detail": str(result.get("detail") or "")[:400]})
        outcome = state
    else:
        store.write(item.feature_id, dict(record, state="running", evidence=result))
        queue.set_status(item.feature_id, "running", {"job_id": item.job_id})
        outcome = "running"

    if on_status is not None:
        on_status(item.feature_id, outcome)
    return {"action": outcome, "feature": item.feature_id, "job": item.job_id,
            "transitions": transitions, "status": state}


def _load_job_or_none(state_dir: Path, job_id: str) -> dict[str, Any] | None:
    try:
        return jobs.load_job(state_dir, job_id)
    except Exception:  # noqa: BLE001 - missing job is a normal crash-recovery state
        return None


def _job_accepted(state_dir: Path, job_id: str) -> bool:
    """Evidence of acceptance, from the job document (never from the card)."""
    try:
        job = jobs.load_job(state_dir, job_id)
    except Exception:  # noqa: BLE001
        return False
    tasks = job.get("tasks") or []
    if not tasks or any(str(task.get("state")) not in ("accepted", "verified", "succeeded") for task in tasks):
        return False
    integration = job.get("integration") or {}
    return str(integration.get("state") or "") in ("accepted", "integrated", "done")
