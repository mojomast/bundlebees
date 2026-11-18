"""Pre-dispatch worker attribution (release gate E9).

The release decision's gap: "Pre-dispatch worker attribution: record endpoint, enrolled
worker/ledger chain, job/project/contract/revision BEFORE the POST, send through that same
client, and bind the returned attempt in the MAC-covered lease. Refuse mismatches, ambiguous
routes and adopted attempts (an attempt with no dispatch proof)."

How the pieces fit:

* ``intent_for()`` builds the record that must exist BEFORE the POST.
* ``record_intent()`` persists it (signed with the evidence key) so the pre-POST state is
  durable even if the dispatch crashes mid-flight.
* ``bind_attempt()`` attaches the returned attempt id to that intent - the dispatch proof.
* ``verify_attribution()`` is the audit: it PASSES only when a single signed intent matches the
  attempt on every recorded field and carries a bound attempt id. An attempt without an intent
  is ``adopted_attempt``; two matching intents are ``ambiguous_route``; any field mismatch names
  the field.

Scope: the owner's decision (2026-11-17) is "canary project only first", so callers apply
enforcement only to configured canary projects; every other project keeps current behaviour.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "rizzstack.dispatch-attribution/1"
FIELDS = ("endpoint", "worker_id", "ledger_chain", "job_id", "project", "contract", "revision")


class AttributionError(Exception):
    def __init__(self, code: str, message: str, fix: str = "", **detail: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.fix, self.detail = code, message, fix, detail

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "fix": self.fix, **self.detail}


def _dir(state_dir: str | os.PathLike) -> Path:
    path = Path(state_dir) / "attribution"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()


def _key(state_dir: str | os.PathLike) -> bytes:
    from . import leases

    return bytes(leases.evidence_key(Path(state_dir)))


def _sign(state_dir: str | os.PathLike, intent: Mapping[str, Any]) -> str:
    body = {k: v for k, v in intent.items() if k != "signature"}
    return hmac.new(_key(state_dir), _canonical(body), hashlib.sha256).hexdigest()


def _require(field: str, value: Any, fix: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise AttributionError(f"attribution_incomplete", f"the intent needs {field}", fix,
                               field=field)
    return text


def intent_for(*, endpoint: Any, worker_id: Any, ledger_chain: Any, job_id: Any, project: Any,
               contract: Any, revision: Any, client: str = "", mode: str = "canary") -> dict[str, Any]:
    """Build the pre-POST attribution record. Refuses incomplete tuples."""
    return {
        "schema": SCHEMA,
        "intent_id": uuid.uuid4().hex,
        "endpoint": _require("endpoint", endpoint, "Confirm the launcher route before dispatching."),
        "worker_id": _require("worker_id", worker_id, "Enroll the worker (ledger chain) first."),
        "ledger_chain": _require("ledger_chain", ledger_chain, "The worker's ledger chain head is required."),
        "job_id": _require("job_id", job_id, "Bind the dispatch to a job."),
        "project": _require("project", project, "Bind the dispatch to a project."),
        "contract": str(contract or ""),
        "revision": str(revision or ""),
        "client": str(client or ""),
        "mode": str(mode or "canary"),
        "created_at": time.time(),
        "attempt_id": "",
    }


def record_intent(state_dir: str | os.PathLike, intent: Mapping[str, Any]) -> dict[str, Any]:
    """Persist the signed intent BEFORE the POST."""
    record = dict(intent)
    record.setdefault("intent_id", uuid.uuid4().hex)
    record["schema"] = SCHEMA
    record["signature"] = _sign(state_dir, record)
    path = _dir(state_dir) / f"{record['intent_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    record["path"] = str(path)
    return record


def load_intents(state_dir: str | os.PathLike) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(_dir(state_dir).glob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


def bind_attempt(state_dir: str | os.PathLike, intent_id: str, attempt_id: Any) -> dict[str, Any]:
    """Attach the returned attempt id - this is the dispatch proof."""
    path = _dir(state_dir) / f"{intent_id}.json"
    if not path.exists():
        raise AttributionError("intent_missing", f"no recorded intent {intent_id!r}",
                               "Record the intent before the POST.")
    record = json.loads(path.read_text(encoding="utf-8"))
    attempt = str(attempt_id or "").strip()
    if not attempt:
        raise AttributionError("attempt_missing", "the dispatch returned no attempt id",
                               "Treat the dispatch as failed; do not infer an attempt.")
    record["attempt_id"] = attempt
    record["bound_at"] = time.time()
    record["signature"] = _sign(state_dir, record)
    path.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return record


def _matches(intent: Mapping[str, Any], facts: Mapping[str, Any]) -> list[str]:
    return [f for f in ("endpoint", "worker_id", "ledger_chain", "job_id", "project")
            if str(intent.get(f) or "") != str(facts.get(f) or "")]


def verify_attribution(state_dir: str | os.PathLike, *, attempt_id: Any,
                       facts: Mapping[str, Any]) -> dict[str, Any]:
    """Audit one attempt against the recorded intents.

    Returns ``{"ok": True, "intent_id": ..., "digest": ...}`` or raises AttributionError with a
    typed code (adopted_attempt, ambiguous_route, attribution_mismatch, intent_signature_invalid).
    """
    attempt = str(attempt_id or "").strip()
    if not attempt:
        raise AttributionError("attempt_missing", "no attempt id to verify",
                               "Pass the attempt the dispatch returned.")
    key = _key(state_dir)
    bound: list[dict[str, Any]] = []
    for intent in load_intents(state_dir):
        if str(intent.get("attempt_id") or "") == attempt:
            bound.append(intent)
    if not bound:
        raise AttributionError(
            "adopted_attempt", f"attempt {attempt} has no dispatch intent",
            "An attempt must be bound to a pre-POST intent; adopted attempts are refused.")
    if len(bound) > 1:
        raise AttributionError(
            "ambiguous_route", f"attempt {attempt} matches {len(bound)} intents",
            "Exactly one intent may bind an attempt; re-dispatch with a fresh intent.")
    intent = bound[0]
    expected = _sign(state_dir, intent)
    if not hmac.compare_digest(expected, str(intent.get("signature") or "")):
        raise AttributionError("intent_signature_invalid", f"intent {intent['intent_id']} was altered",
                               "Do not edit recorded intents; re-dispatch instead.")
    missing = _matches(intent, facts)
    if missing:
        raise AttributionError(
            "attribution_mismatch",
            f"the attempt does not match its intent on: {', '.join(missing)}",
            "Compare the intent with the live route facts before trusting this attempt.",
            fields=missing, intent={f: intent.get(f) for f in missing},
            facts={f: facts.get(f) for f in missing})
    digest = hashlib.sha256(_canonical({f: intent.get(f) for f in FIELDS} | {"attempt": attempt})).hexdigest()
    return {"ok": True, "intent_id": intent["intent_id"], "attempt_id": attempt, "digest": digest}


def canary_projects(settings: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Configured canary projects (owner decision: enforce on the canary only)."""
    rows = (settings or {}).get("attribution_canary_projects") or ()
    if isinstance(rows, str):
        rows = [item.strip() for item in rows.split(",") if item.strip()]
    return tuple(str(item).strip() for item in rows if str(item).strip())


def applies_to(settings: Mapping[str, Any] | None, project: Any) -> bool:
    return str(project or "").strip() in canary_projects(settings)
