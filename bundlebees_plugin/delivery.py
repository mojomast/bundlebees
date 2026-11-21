"""Durable delivery intents and an idempotent PR reconciler (rizzle.delivery/1).

Why this exists (2026-11-17): three verified branches were pushed and then sat on
origin with no pull request because the step between "verify pushed the branch"
and "a PR exists" was a human act. Finished work whose state lives only in a
coordinator's head is a delivery gap, and the fix is the same one distributed
systems use everywhere:

* **Transactional outbox** (microservices.io/patterns/data/transactional-outbox):
  record the *intent* in durable storage as part of the same operation that
  changes state, then let a separate relay finish the external side effect.
* **Idempotent side effects** (temporal.io: retries are safe when the effect is
  keyed): PR creation is keyed by (repo, branch); an existing PR for the head
  branch is the success condition, not an error.
* **Level-triggered reconciliation** (Kubernetes controllers): the sweep compares
  desired state (a pushed delivery must have a PR) with actual state (query
  GitHub) and converges, instead of relying on having observed an event.

Lifecycle of one delivery record (the same steps the sweep walks):

    intent  --(verify pushes)-->  pushed  --(sweep)-->  pr_open

`intent` alone means the push never happened: the sweep must NOT open a PR for
it, because the branch may not exist or may not contain the verified payload.
`pushed` records the exact commit verify pushed, so the PR always points at
evidence the coordinator actually tested.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable

SCHEMA = "rizzstack.delivery/1"

STATE_INTENT = "intent"
STATE_PUSHED = "pushed"
STATE_PR_OPEN = "pr_open"

#: `gh` already-exists messages, across gh versions.
_ALREADY_EXISTS = re.compile(r"already exists|A pull request already exists|HTTP 422", re.I)


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def _deliveries_dir(state_dir: str | os.PathLike) -> Path:
    directory = Path(state_dir) / "deliveries"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _record_path(state_dir: str | os.PathLike, attempt_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", attempt_id)
    return _deliveries_dir(state_dir) / f"{safe}.json"


def _write(record: dict) -> None:
    path = _record_path(record["state_dir"], record["attempt_id"])
    record = {k: v for k, v in record.items() if k != "state_dir"}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def record_intent(state_dir: str | os.PathLike, *, attempt_id: str, repo: str, branch: str,
                  base_sha: str, title: str, body: str = "") -> dict:
    """Write the durable intent BEFORE the push (the outbox row).

    A crash between here and the push leaves an ``intent`` record: the sweep
    reports it and refuses to open a PR, because there is no evidence the
    verified payload ever reached the remote.
    """
    record = {
        "schema": SCHEMA,
        "attempt_id": attempt_id,
        "repo": repo,
        "branch": branch,
        "base_sha": base_sha,
        "title": title or f"verify: {branch}",
        "body": body,
        "state": STATE_INTENT,
        "pushed_sha": None,
        "pr_url": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "state_dir": str(state_dir),
    }
    _write(record)
    return record


def record_pushed(state_dir: str | os.PathLike, attempt_id: str, pushed_sha: str) -> dict:
    """Record the exact commit the push landed (idempotent: re-running overwrites with the same sha)."""
    path = _record_path(state_dir, attempt_id)
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if record is None:
        raise DeliveryError("no_intent", f"no delivery intent recorded for {attempt_id}",
                            "Call record_intent before pushing.")
    record["pushed_sha"] = pushed_sha
    record["state"] = STATE_PUSHED
    record["updated_at"] = time.time()
    record["state_dir"] = str(state_dir)
    _write(record)
    return record


def record_pr(state_dir: str | os.PathLike, attempt_id: str, pr_url: str) -> dict:
    path = _record_path(state_dir, attempt_id)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["pr_url"] = pr_url
    record["state"] = STATE_PR_OPEN
    record["updated_at"] = time.time()
    record["state_dir"] = str(state_dir)
    _write(record)
    return record


_PENDING_STATES = (STATE_INTENT, STATE_PUSHED)
_KNOWN_STATES = (STATE_INTENT, STATE_PUSHED, STATE_PR_OPEN)


def _damaged(path: Path, reason: str) -> dict:
    return {"attempt_id": path.stem, "file": path.name, "reason": reason}


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _created_key(record: dict) -> float:
    """Total ordering key: any non-real ``created_at`` is unknown, so oldest (0.0)."""
    value = record.get("created_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def scan(state_dir: str | os.PathLike) -> dict:
    """Classify every ``deliveries/*.json`` as pending, terminal, or damaged.

    Each file is read exactly once. ``pending`` keeps the historical contract:
    records in ``intent`` or ``pushed`` usable by :func:`next_step`, oldest first.
    ``damaged`` entries carry the file stem, basename, and a stable reason code;
    they are never passed to the relay or to any path builder.
    """
    pending_rows: list[dict] = []
    damaged_rows: list[dict] = []
    for path in sorted(_deliveries_dir(state_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            damaged_rows.append(_damaged(path, "unreadable_record"))
            continue
        if not isinstance(record, dict):
            damaged_rows.append(_damaged(path, "not_an_object"))
            continue
        attempt_id = record.get("attempt_id")
        if not isinstance(attempt_id, str):
            damaged_rows.append(_damaged(path, "missing_attempt_id"))
            continue
        if record.get("schema") != SCHEMA:
            damaged_rows.append(_damaged(path, "schema_mismatch"))
            continue
        if _record_path(state_dir, attempt_id).name != path.name:
            damaged_rows.append(_damaged(path, "identity_mismatch"))
            continue
        state = record.get("state")
        if state not in _KNOWN_STATES:
            damaged_rows.append(_damaged(path, "unknown_state"))
            continue
        if state == STATE_PR_OPEN:
            complete = _nonempty_str(record.get("pr_url"))
        elif state == STATE_PUSHED:
            complete = (_nonempty_str(record.get("repo"))
                        and _nonempty_str(record.get("branch"))
                        and _nonempty_str(record.get("pushed_sha")))
        else:
            complete = _nonempty_str(record.get("repo")) and _nonempty_str(record.get("branch"))
        if not complete:
            damaged_rows.append(_damaged(path, "incomplete_record"))
            continue
        if state in _PENDING_STATES:
            record["state_dir"] = str(state_dir)
            pending_rows.append(record)
    return {
        "pending": sorted(pending_rows, key=_created_key),
        "damaged": damaged_rows,
    }


def pending(state_dir: str | os.PathLike) -> list[dict]:
    """Every valid record that is not yet ``pr_open``, oldest first."""
    return scan(state_dir)["pending"]


def next_step(record: dict) -> str:
    """The single next reconcilable action for a record (level-triggered decision)."""
    state = record.get("state")
    if state == STATE_PUSHED:
        return "record_pr" if _nonempty_str(record.get("pr_url")) else "open_pr"
    if state == STATE_INTENT:
        return "blocked_no_push"
    return "done"


def _run_gh(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def find_existing_pr(repo: str, branch: str, runner: Callable[[list[str]], tuple[int, str]] = _run_gh) -> str | None:
    """Return the PR URL whose head is ``branch``, or None. The idempotency key."""
    code, out = runner(["pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                        "--json", "url", "-q", ".[0].url"])
    if code != 0:
        raise DeliveryError("gh_list_failed", f"gh pr list failed for {repo} {branch}: {out.strip()[:200]}",
                            "Check gh auth and repo access.")
    url = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return url or None


def open_pr_idempotent(repo: str, branch: str, title: str, body: str = "",
                       runner: Callable[[list[str]], tuple[int, str]] = _run_gh) -> tuple[str, bool]:
    """Open the PR for ``branch`` or return the existing one. (url, created)."""
    existing = find_existing_pr(repo, branch, runner)
    if existing:
        return existing, False
    code, out = runner(["pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                        "--title", title, "--body", body or title])
    if code == 0:
        url = out.strip().splitlines()[-1].strip()
        if url.startswith("http"):
            return url, True
        # gh printed a URL we could not parse; fall through to the authoritative list call
    elif not _ALREADY_EXISTS.search(out):
        raise DeliveryError("gh_create_failed", f"gh pr create failed for {repo} {branch}: {out.strip()[:200]}",
                            "Resolve the gh error, then rerun the sweep; it is idempotent.")
    existing = find_existing_pr(repo, branch, runner)
    if existing:
        return existing, code == 0  # created if our create actually succeeded
    raise DeliveryError("pr_missing", f"no PR found for {repo} {branch} after create attempt",
                        "Inspect gh output; the sweep can be rerun safely.")


def sweep(state_dir: str | os.PathLike, runner: Callable[[list[str]], tuple[int, str]] = _run_gh,
          dry_run: bool = False) -> dict:
    """Reconcile every pending delivery: open missing PRs, record URLs, flag blocked intents.

    Idempotent and safe to run on a timer: an existing PR is recognized before any
    create call, so repeated sweeps never duplicate work.
    """
    scanned = scan(state_dir)
    result = {"opened": [], "existing": [], "blocked": [], "errors": [],
              "damaged": list(scanned["damaged"])}
    for record in scanned["pending"]:
        step = next_step(record)
        if step == "blocked_no_push":
            result["blocked"].append({"attempt_id": record.get("attempt_id"), "branch": record.get("branch"),
                                      "reason": "intent recorded but no pushed_sha: the push never landed"})
            continue
        if step == "record_pr":
            pr_url = record["pr_url"]
            row = {"attempt_id": record.get("attempt_id"), "branch": record.get("branch"),
                   "pr_url": pr_url}
            if dry_run:
                row["dry_run"] = True
            else:
                record_pr(state_dir, record["attempt_id"], pr_url)
            result["existing"].append(row)
            continue
        if step != "open_pr":
            continue
        try:
            if dry_run:
                existing = find_existing_pr(record["repo"], record["branch"], runner)
                key = "existing" if existing else "opened"
                result[key].append({"attempt_id": record.get("attempt_id"), "branch": record.get("branch"),
                                    "pr_url": existing, "dry_run": True})
                continue
            url, created = open_pr_idempotent(record["repo"], record["branch"], record.get("title") or
                                              f"verify: {record['branch']}", record.get("body") or "", runner)
            record_pr(state_dir, record["attempt_id"], url)
            result["opened" if created else "existing"].append(
                {"attempt_id": record.get("attempt_id"), "branch": record.get("branch"), "pr_url": url})
        except DeliveryError as exc:
            result["errors"].append({"attempt_id": record.get("attempt_id"), "branch": record.get("branch"),
                                     "error": exc.to_dict()})
    return result
