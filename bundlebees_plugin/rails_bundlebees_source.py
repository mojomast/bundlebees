"""Read-only bundlebees source adapter for the rizzstack rails ledger.

The adapter reads only durable records bundlebees already wrote and turns
them into ``rizzstack.rails/1`` event drafts. It never writes to the state
directory, never consults the wall clock, and never adds a row that does not
correspond to a record.

Trust is decided from record provenance, not from the adapter's wishes:

* a committed verify record (``verify-results/<attempt>.json``) is a
  coordinator record and maps to ``verification.result``;
* a signed TDD evidence record produced by a verifier maps to
  ``verification.result``; a reviewer record maps to ``review.result``;
* an accepted or rejected wave record (``waves/<wave>.json``) maps to
  ``merge.result``;
* a worker-reported PASS, or a record whose signature failed, is an unsigned
  claim: it is emitted with verdict ``UNVERIFIED`` and authority
  ``worker_report`` so it can be audited but never becomes a PASS transition.

Replaying the same snapshot produces byte-identical drafts. Snapshot order is
normalized, digests are computed at runtime, and no lane/state/checkpoint row
is invented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "BundlebeesSource",
    "BundlebeesSourceAdapter",
    "SourceError",
    "append_events",
    "collect_events",
    "collect_snapshot",
    "events",
    "events_from_records",
    "events_from_snapshot",
    "snapshot",
]

try:
    from rizzstack_rails import contracts as rails_contracts
except ImportError:  # bundlebees keeps the runtime vendored under contrib/
    _CONTRIB = Path(__file__).resolve().parent.parent / "contrib"
    if str(_CONTRIB) not in sys.path:
        sys.path.insert(0, str(_CONTRIB))
    from rizzstack_rails import contracts as rails_contracts

SNAPSHOT_VERSION = "bundlebees-source/1"
ADAPTER_ID = "bundlebees"
DEFAULT_INSTANCE = "bundlebees"
DEFAULT_REDACTION = {"mode": "structured_safe", "path_detail": "omitted"}

_MAX_RECORD_BYTES = 4_000_000
_STAMP_SECONDS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z$")

_STATUS_VERDICT = {
    "passed": "PASS",
    "failed": "FAIL",
    "not_run": "NOT_RUN",
    "review_required": "UNVERIFIED",
}

_RECORD_LISTS = (
    ("verify", "verify"),
    ("verification", "verify"),
    ("verifications", "verify"),
    ("verify-results", "verify"),
    ("verify_results", "verify"),
    ("evidence", "verify"),
    ("review", "review"),
    ("reviews", "review"),
    ("review-results", "review"),
    ("review_results", "review"),
    ("merge", "merge"),
    ("merges", "merge"),
    ("merge-results", "merge"),
    ("merge_results", "merge"),
    ("waves", "merge"),
    ("claim", "claim"),
    ("claims", "claim"),
    ("worker_claims", "claim"),
    ("worker-claims", "claim"),
)

_KIND_ALIASES = {
    "verification": "verify",
    "verification.result": "verify",
    "verify.result": "verify",
    "review.result": "review",
    "merge.result": "merge",
    "worker_claim": "claim",
    "worker-report": "claim",
}


class SourceError(Exception):
    """A source record that cannot be read or mapped, with a stable code."""

    def __init__(self, code: str, error: str, fix: str = "") -> None:
        self.code = str(code)
        self.error = str(error)
        self.fix = str(fix)
        super().__init__(self.error)


# ---------------------------------------------------------------------------
# Runtime digests and small value helpers
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _digest(*parts: str) -> str:
    joined = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(
        b"bundlebees-rails-source:v1\x00" + joined.encode("utf-8")
    ).hexdigest()


def _record_digest(record: Mapping[str, Any]) -> str:
    return _digest("record", _canonical(record))


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _valid_digest(value: Any) -> str | None:
    if isinstance(value, str) and rails_contracts.HEX64_RE.match(value):
        return value
    return None


def _stamp(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if rails_contracts.STAMP_RE.match(text):
        return text
    if text.endswith("+00:00"):
        return _stamp(text[:-6] + "Z")
    match = _STAMP_SECONDS_RE.match(text)
    if match:
        return match.group(1) + ".000000Z"
    return None


def _duration(record: Mapping[str, Any]) -> int | None:
    value = record.get("duration_ms")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    for holder in (record, _mapping(record.get("environment"))):
        seconds = holder.get("duration_s")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
            return int(round(float(seconds) * 1000))
    return None


def _timing(record: Mapping[str, Any], occurred_at: str | None) -> str:
    declared = record.get("timing_quality")
    if declared in rails_contracts.TIMING_QUALITY:
        return str(declared)
    return "reported" if occurred_at else "unknown"


def _phase(record: Mapping[str, Any], default: str = "green") -> str:
    value = record.get("phase")
    if value in rails_contracts.PHASES:
        return str(value)
    return default


def _code_rev(record: Mapping[str, Any]) -> str:
    for key in ("sha", "revision", "head_sha", "code_rev", "base_rev"):
        value = _text(record.get(key))
        if value is not None:
            return value
    return _record_digest(record)


def _contract_digest(record: Mapping[str, Any]) -> str:
    declared = _valid_digest(record.get("contract_sha256"))
    if declared is not None:
        return declared
    return _record_digest(record)


def _instance_for(path: Path) -> str:
    return ADAPTER_ID + "-" + _digest("instance", str(path.resolve()))[:12]


# ---------------------------------------------------------------------------
# Snapshot collection (read-only)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise SourceError("source_invalid", "source record is not a regular file", path.name)
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise SourceError("source_invalid", "source record is larger than the limit", path.name)
        data = json.loads(path.read_text(encoding="utf-8"))
    except SourceError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceError(
            "source_invalid",
            "source record could not be read: %s" % (exc,),
            "Keep durable bundlebees records as readable JSON objects.",
        ) from exc
    if not isinstance(data, dict):
        raise SourceError("source_invalid", "source record is not a JSON object", path.name)
    return data


def _files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise SourceError("source_invalid", "source directory is unsafe", directory.name)
    return sorted(
        (path for path in directory.iterdir() if path.name.endswith(".json")),
        key=lambda path: path.name,
    )


def _contract_dirs(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise SourceError("source_invalid", "source directory is unsafe", directory.name)
    return sorted(
        (path for path in directory.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    )


def _as_record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise SourceError("snapshot_invalid", "snapshot record is not valid JSON") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise SourceError("snapshot_invalid", "snapshot record is not an object")


def _entry(kind: str, record_id: str, record: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    entry = {"type": kind, "record_id": record_id, "record": dict(record)}
    entry.update(extra)
    return entry


def _key_bytes(key: Any) -> bytes:
    return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def _verify_signature(record: Mapping[str, Any], key: Any) -> bool:
    signature = record.get("signature")
    if not isinstance(signature, str):
        return False
    unsigned = {
        name: value
        for name, value in record.items()
        if name != "signature" and not str(name).startswith("_")
    }
    expected = hmac.new(
        _key_bytes(key),
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        ),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def collect_snapshot(state_dir: str | os.PathLike[str], *, key: Any | None = None) -> dict[str, Any]:
    """Read durable records under *state_dir* into a deterministic snapshot."""

    root = Path(state_dir).expanduser()
    if not root.exists():
        return {"snapshot_version": SNAPSHOT_VERSION, "instance": _instance_for(root), "records": []}
    if root.is_symlink() or not root.is_dir():
        raise SourceError("source_invalid", "state directory is unsafe", str(root))
    entries: list[dict[str, Any]] = []

    for path in _files(root / "verify-results"):
        entries.append(_entry("verify", "verify-results/" + path.name, _read_json(path)))

    for contract_dir in _contract_dirs(root / "evidence"):
        for path in _files(contract_dir):
            record = _read_json(path)
            kind = "review" if record.get("phase") == "review" else "verify"
            extra: dict[str, Any] = {}
            if key is not None and isinstance(record.get("signature"), str):
                extra["signature_valid"] = _verify_signature(record, key)
            entries.append(
                _entry(
                    kind,
                    "evidence/%s/%s" % (contract_dir.name, path.name),
                    record,
                    **extra,
                )
            )

    for path in _files(root / "waves"):
        entries.append(_entry("merge", "waves/" + path.name, _read_json(path)))

    for path in _files(root / "worker-claims"):
        entries.append(_entry("claim", "worker-claims/" + path.name, _read_json(path)))

    entries.sort(key=lambda item: (item["type"], item["record_id"]))
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "instance": _instance_for(root),
        "records": entries,
    }


read_snapshot = collect_snapshot
snapshot_from_state = collect_snapshot


# ---------------------------------------------------------------------------
# Snapshot normalization
# ---------------------------------------------------------------------------


def _infer_type(record: Mapping[str, Any]) -> str:
    phase = record.get("phase")
    if phase == "review":
        return "review"
    if record.get("wave_id") or ("accepted" in record and "head_sha" in record):
        return "merge"
    if record.get("evidence_version") or phase in rails_contracts.PHASES:
        return "verify"
    if record.get("attempt_id") or ("ok" in record and "sha" in record):
        return "verify"
    if record.get("claim") is True or record.get("authority") == "worker_report":
        return "claim"
    return "verify"


def _infer_record_id(kind: str, record: Mapping[str, Any]) -> str:
    wave = _text(record.get("wave_id"))
    if kind == "merge" and wave is not None:
        return "waves/" + wave + ".json"
    attempt = _text(record.get("attempt_id"))
    if kind in ("verify", "claim") and attempt is not None:
        return "verify-results/" + attempt + ".json"
    contract = _text(record.get("contract_id"))
    phase = _text(record.get("phase"))
    if contract is not None and phase is not None:
        return "evidence/%s/%s.json" % (contract, phase)
    return "record:" + _record_digest(record)[:16]


def _normalize_entries(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = snapshot.get("records")
    if raw is None:
        raw = []
        for name, kind in _RECORD_LISTS:
            value = snapshot.get(name)
            if value is None:
                continue
            if not isinstance(value, (list, tuple)):
                raise SourceError(
                    "snapshot_invalid",
                    "snapshot.%s must be a list of records" % name,
                    "Pass records as a list under records or the named lists.",
                )
            for position, item in enumerate(value):
                record = _as_record(item)
                record_id = _infer_record_id(kind, record)
                raw.append({"type": kind, "record_id": record_id, "record": record})
    if not isinstance(raw, (list, tuple)):
        raise SourceError(
            "snapshot_invalid",
            "snapshot.records must be a list",
            "Pass records as a list of {type, record_id, record} objects.",
        )
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            item = _as_record(item)
        if not isinstance(item, Mapping):
            raise SourceError("snapshot_invalid", "snapshot entry is not an object")
        if "record" in item:
            record = _as_record(item.get("record"))
            kind = _KIND_ALIASES.get(str(item.get("type") or ""), str(item.get("type") or ""))
            record_id = _text(item.get("record_id"))
        else:
            record = _as_record(item)
            kind = ""
            record_id = None
        kind = _KIND_ALIASES.get(kind, kind)
        if kind not in ("verify", "review", "merge", "claim"):
            kind = _infer_type(record)
        if record_id is None:
            record_id = _infer_record_id(kind, record)
        entry = {
            "type": kind,
            "record_id": record_id,
            "record": record,
        }
        for flag in ("signature_valid",):
            if flag in item:
                entry[flag] = bool(item.get(flag))
        entries.append(entry)
    entries.sort(key=lambda item: (item["type"], item["record_id"]))
    return entries


def _snapshot_instance(snapshot: Mapping[str, Any]) -> str:
    value = _text(snapshot.get("instance"))
    if value is None:
        return DEFAULT_INSTANCE
    return value


# ---------------------------------------------------------------------------
# Shared mapping helpers
# ---------------------------------------------------------------------------


def _is_claim(entry: Mapping[str, Any]) -> bool:
    if entry.get("type") == "claim":
        return True
    if entry.get("signature_valid") is False:
        return True
    record = entry["record"]
    if record.get("authority") == "worker_report":
        return True
    if record.get("provenance") in ("worker", "worker_report"):
        return True
    if record.get("claim") is True or record.get("claimed") is True:
        return True
    if record.get("trusted") is False or record.get("verified") is False:
        return True
    if record.get("unsigned") is True:
        return True
    if record.get("signed") is False:
        return True
    if record.get("worker_pass") is True:
        return True
    role = _mapping(record.get("produced_by")).get("role")
    return role == "worker"


def _source(adapter: str, instance: str, record_id: str, revision: str) -> dict[str, str]:
    return {
        "adapter": adapter,
        "instance": instance,
        "record_id": record_id,
        "revision": revision,
    }


def _scope(
    record: Mapping[str, Any],
    *,
    task_id: str,
    code_rev: str,
    contract_sha256: str,
) -> dict[str, str]:
    scope: dict[str, str] = {}
    for name in (
        "repo_slug",
        "campaign_id",
        "job_id",
        "task_id",
        "attempt_id",
        "session_id",
        "action_id",
    ):
        value = _text(record.get(name))
        if value is not None:
            scope[name] = value
    if "task_id" not in scope:
        scope["task_id"] = task_id
    scope["contract_sha256"] = contract_sha256
    scope["code_rev"] = code_rev
    return scope


def _refs(record: Mapping[str, Any], record_id: str, code_rev: str) -> list[str]:
    refs = [record_id]
    for name in ("red", "suite", "gitleaks"):
        if record.get(name) is not None:
            refs.append(record_id + "#" + name)
    if record.get("result") is not None:
        refs.append(record_id + "#result")
    refs.append("revision:" + code_rev)
    seen: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.append(ref)
    return seen


def _ac_pairs(record: Mapping[str, Any], fallback: str) -> list[tuple[str, str | None]]:
    criteria = record.get("criteria")
    pairs: list[tuple[str, str | None]] = []
    if isinstance(criteria, Mapping):
        for ac_id in sorted(str(key) for key in criteria):
            status = criteria.get(ac_id)
            pairs.append((ac_id, str(status) if isinstance(status, str) else None))
    if pairs:
        return pairs
    ac_id = (
        _text(record.get("ac_id"))
        or _text(record.get("contract_id"))
        or _text(record.get("attempt_id"))
        or fallback
    )
    return [(ac_id, None)]


def _classify_verdict(record: Mapping[str, Any], phase: str) -> tuple[str, str | None]:
    classification = _text(record.get("classification"))
    if classification is not None:
        if classification == "passed":
            return "PASS", None
        if classification == "behavioral_failure":
            if phase == "red":
                return "EXPECTED_RED", None
            return "FAIL", "behavioral failure outside the red phase"
        if classification == "not_run":
            return "NOT_RUN", "the recorded run did not execute"
        return "FAIL", "classification is " + classification
    ok = record.get("ok")
    if ok is True:
        suite = _mapping(record.get("suite"))
        if suite.get("timed_out") is True:
            return "FAIL", "the suite timed out"
        exit_code = suite.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return "FAIL", "the suite exited non-zero"
        return "PASS", None
    if ok is False:
        return "FAIL", "the verify record is not ok"
    return "UNVERIFIED", "the record carries no verdict"


def _draft(
    *,
    kind: str,
    source: Mapping[str, str],
    scope: Mapping[str, str],
    payload: Mapping[str, Any],
    authority: str,
    record: Mapping[str, Any],
    occurred_at: str | None,
    duration_ms: int | None,
    discriminator: str,
) -> dict[str, Any]:
    draft = {
        "kind": kind,
        "source": dict(source),
        "scope": dict(scope),
        "payload": dict(payload),
        "authority": authority,
        "lane": None,
        "occurred_at": occurred_at,
        "duration_ms": duration_ms,
        "timing_quality": _timing(record, occurred_at),
        "redaction_policy": dict(DEFAULT_REDACTION),
        "dedupe_key": "bundlebees:"
        + kind
        + ":"
        + source["adapter"]
        + ":"
        + source["record_id"]
        + ":"
        + discriminator
        + ":"
        + _digest(kind, source["adapter"], source["record_id"], discriminator)[:24],
    }
    try:
        rails_contracts.validate_event(
            draft["kind"],
            draft["source"],
            draft["scope"],
            draft["payload"],
            draft["authority"],
            draft["lane"],
            draft["occurred_at"],
            draft["duration_ms"],
            draft["timing_quality"],
            draft["redaction_policy"],
        )
    except rails_contracts.RailsRefusal as exc:
        raise SourceError(
            "source_invalid",
            "record %s does not map to a valid rail event: %s"
            % (source["record_id"], exc),
            "Repair the durable record; the adapter never guesses missing fields.",
        ) from exc
    return draft


# ---------------------------------------------------------------------------
# Kind mappers
# ---------------------------------------------------------------------------


def _verification_drafts(entry: Mapping[str, Any], instance: str) -> list[dict[str, Any]]:
    record = entry["record"]
    record_id = str(entry["record_id"])
    claim = _is_claim(entry)
    phase = _phase(record)
    digest = _contract_digest(record)
    code_rev = _code_rev(record)
    occurred_at = _stamp(
        record.get("at") or record.get("occurred_at") or record.get("recorded_at")
    )
    duration = _duration(record)
    run_id = (
        _text(record.get("run_id"))
        or _text(record.get("attempt_id"))
        or _text(record.get("contract_id"))
        or record_id
    )
    base_verdict, base_reason = _classify_verdict(record, phase)
    claimed_verdict = base_verdict
    adapter = "bundlebees.claim" if claim else "bundlebees.verify"
    source = _source(adapter, instance, record_id, code_rev)
    scope = _scope(
        record,
        task_id=_text(record.get("attempt_id"))
        or _text(record.get("contract_id"))
        or run_id,
        code_rev=code_rev,
        contract_sha256=digest,
    )
    refs = _refs(record, record_id, code_rev)
    drafts: list[dict[str, Any]] = []
    for ac_id, status in _ac_pairs(record, run_id):
        if claim:
            verdict = "UNVERIFIED"
            reason = (
                _text(record.get("notes"))
                or "worker-reported result is a claim; no committed verifier record backs it"
            )
            authority = "worker_report"
        else:
            authority = "coordinator_record"
            if base_verdict == "PASS":
                verdict = _STATUS_VERDICT.get(status or "passed", "UNVERIFIED")
            elif base_verdict == "EXPECTED_RED":
                verdict = "EXPECTED_RED"
            else:
                verdict = base_verdict
            if verdict == "PASS":
                reason = None
            elif base_verdict not in ("PASS", "EXPECTED_RED") and base_reason:
                reason = base_reason
            elif status is not None and status != "passed":
                reason = "criterion is " + status
            else:
                reason = base_reason or "verification did not pass"
        payload: dict[str, Any] = {
            "ac_id": ac_id,
            "run_id": run_id,
            "contract_sha256": digest,
            "code_rev": code_rev,
            "phase": phase,
            "verdict": verdict,
            "evidence_refs": list(refs),
        }
        if verdict != "PASS":
            payload["reason"] = reason
        if verdict == "EXPECTED_RED":
            payload["frozen_test_intent"] = _frozen_intent(record, record_id)
        if claim:
            payload["claim"] = True
            payload["claimed_verdict"] = claimed_verdict
        drafts.append(
            _draft(
                kind="verification.result",
                source=source,
                scope=scope,
                payload=payload,
                authority=authority,
                record=record,
                occurred_at=occurred_at,
                duration_ms=duration,
                discriminator=ac_id,
            )
        )
    return drafts


def _frozen_intent(record: Mapping[str, Any], record_id: str) -> list[str]:
    fingerprints = record.get("test_fingerprints")
    if isinstance(fingerprints, Mapping) and fingerprints:
        return sorted(str(name) for name in fingerprints)
    tests = record.get("tests")
    if isinstance(tests, (list, tuple)) and tests:
        return [str(item) for item in tests]
    return [record_id]


def _review_drafts(entry: Mapping[str, Any], instance: str) -> list[dict[str, Any]]:
    record = entry["record"]
    record_id = str(entry["record_id"])
    claim = _is_claim(entry)
    attestation = _mapping(record.get("attestation"))
    produced_by = _mapping(record.get("produced_by"))
    reviewer = (
        _text(attestation.get("reviewer_label"))
        or _text(produced_by.get("name"))
        or "unidentified reviewer"
    )
    reviewed_head = _code_rev(record)
    digest = _contract_digest(record)
    occurred_at = _stamp(
        record.get("at") or record.get("occurred_at") or record.get("recorded_at")
    )
    author = (
        _text(record.get("author"))
        or _text(record.get("base_rev"))
        or _text(record.get("contract_id"))
        or "unknown author"
    )
    basis = (
        _text(record.get("notes"))
        or _text(attestation.get("kind"))
        or "coordinator_recorded_review"
    )
    independent = record.get("independent")
    if not isinstance(independent, bool):
        independent = False
    classification = _text(record.get("classification"))
    verdict = "PASS" if classification == "passed" else "FAIL"
    if claim:
        verdict = "UNVERIFIED"
        authority = "worker_report"
    elif attestation.get("kind") == "human_authorization":
        authority = "owner_record"
    else:
        authority = "reviewer_record"
    adapter = "bundlebees.claim" if claim else "bundlebees.review"
    source = _source(adapter, instance, record_id, reviewed_head)
    scope = _scope(
        record,
        task_id=_text(record.get("contract_id")) or reviewed_head,
        code_rev=reviewed_head,
        contract_sha256=digest,
    )
    payload: dict[str, Any] = {
        "review_id": record_id,
        "reviewer": reviewer,
        "author": author,
        "reviewed_head": reviewed_head,
        "verdict": verdict,
        "independent": independent,
        "basis": basis,
    }
    if verdict != "PASS":
        payload["reason"] = (
            _text(record.get("notes"))
            or ("worker-reported review is a claim" if claim else "the reviewer rejected the change")
        )
    if claim:
        payload["claim"] = True
    return [
        _draft(
            kind="review.result",
            source=source,
            scope=scope,
            payload=payload,
            authority=authority,
            record=record,
            occurred_at=occurred_at,
            duration_ms=_duration(record),
            discriminator="review",
        )
    ]


def _merge_drafts(entry: Mapping[str, Any], instance: str) -> list[dict[str, Any]]:
    record = entry["record"]
    record_id = str(entry["record_id"])
    wave_id = _text(record.get("wave_id")) or record_id
    head_sha = _code_rev(record)
    accepted = record.get("accepted") is True
    occurred_at = _stamp(
        record.get("at") or record.get("occurred_at") or record.get("recorded_at")
    )
    result = "merged" if accepted else "rejected"
    payload: dict[str, Any] = {
        "result": result,
        "wave_id": wave_id,
        "accepted": accepted,
        "head_sha": head_sha,
    }
    attempts = record.get("attempts")
    if isinstance(attempts, (list, tuple)):
        payload["attempts"] = [str(item) for item in attempts if isinstance(item, str)]
    problems = record.get("problems")
    if isinstance(problems, Mapping) and problems:
        payload["problems"] = {str(key): str(value) for key, value in sorted(problems.items())}
    if not accepted:
        payload["reason"] = "the wave was not accepted"
    source = _source("bundlebees.integrate", instance, record_id, head_sha)
    scope = _scope(record, task_id=wave_id, code_rev=head_sha, contract_sha256=_contract_digest(record))
    return [
        _draft(
            kind="merge.result",
            source=source,
            scope=scope,
            payload=payload,
            authority="coordinator_record",
            record=record,
            occurred_at=occurred_at,
            duration_ms=_duration(record),
            discriminator="merge",
        )
    ]


def _drafts_for(entry: Mapping[str, Any], instance: str) -> list[dict[str, Any]]:
    kind = entry.get("type")
    if kind == "merge":
        return _merge_drafts(entry, instance)
    if kind == "review":
        return _review_drafts(entry, instance)
    return _verification_drafts(entry, instance)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def events_from_snapshot(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map one snapshot to validated rail event drafts, deterministically."""

    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except ValueError as exc:
            raise SourceError("snapshot_invalid", "snapshot is not valid JSON") from exc
    if isinstance(snapshot, (list, tuple)):
        snapshot = {"records": list(snapshot)}
    if not isinstance(snapshot, Mapping):
        raise SourceError("snapshot_invalid", "snapshot must be an object")
    instance = _snapshot_instance(snapshot)
    drafts: list[dict[str, Any]] = []
    for entry in _normalize_entries(snapshot):
        drafts.extend(_drafts_for(entry, instance))
    drafts.sort(
        key=lambda draft: (
            draft["kind"],
            draft["source"]["adapter"],
            draft["source"]["record_id"],
            str(draft["payload"].get("ac_id") or ""),
            str(draft["payload"].get("verdict") or ""),
            draft["dedupe_key"],
        )
    )
    return drafts


events_for_snapshot = events_from_snapshot
snapshot_events = events_from_snapshot
snapshot_to_events = events_from_snapshot
build_events = events_from_snapshot
map_snapshot = events_from_snapshot
convert_snapshot = events_from_snapshot
to_events = events_from_snapshot
event_drafts = events_from_snapshot
drafts_for_snapshot = events_from_snapshot


def events_from_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Map a bare iterable of records to event drafts."""

    return events_from_snapshot({"records": list(records)})


def collect_events(
    state_dir: str | os.PathLike[str], *, key: Any | None = None
) -> list[dict[str, Any]]:
    """Read *state_dir* and return its event drafts. Read-only."""

    return events_from_snapshot(collect_snapshot(state_dir, key=key))


read_events = collect_events


def snapshot(state_dir: str | os.PathLike[str], *, key: Any | None = None) -> dict[str, Any]:
    """Alias for :func:`collect_snapshot`."""

    return collect_snapshot(state_dir, key=key)


def events(state_dir: str | os.PathLike[str], *, key: Any | None = None) -> list[dict[str, Any]]:
    """Alias for :func:`collect_events`."""

    return collect_events(state_dir, key=key)


def append_events(ledger: Any, drafts: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Append already-mapped drafts to a writable ledger; returns receipts."""

    receipts: list[dict[str, Any]] = []
    for draft in drafts:
        receipts.append(
            ledger.append(
                draft["kind"],
                source=draft["source"],
                scope=draft["scope"],
                payload=draft["payload"],
                authority=draft["authority"],
                dedupe_key=draft["dedupe_key"],
                lane=draft.get("lane"),
                occurred_at=draft.get("occurred_at"),
                duration_ms=draft.get("duration_ms"),
                timing_quality=draft.get("timing_quality", "unknown"),
                redaction_policy=draft.get("redaction_policy"),
            )
        )
    return receipts


class BundlebeesSource:
    """Read-only source over a bundlebees state directory or snapshot."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        snapshot: Mapping[str, Any] | None = None,
        key: Any | None = None,
    ) -> None:
        if snapshot is None and state_dir is None:
            raise SourceError("source_invalid", "state_dir or snapshot is required")
        self.state_dir = state_dir
        self.key = key
        self._snapshot = dict(snapshot) if snapshot is not None else None

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is None:
            return collect_snapshot(self.state_dir, key=self.key)
        return dict(self._snapshot)

    def events(self) -> list[dict[str, Any]]:
        return events_from_snapshot(self.snapshot())

    emit = events

    def __iter__(self):
        return iter(self.events())


BundlebeesSourceAdapter = BundlebeesSource
