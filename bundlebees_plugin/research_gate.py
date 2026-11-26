"""Mechanical research-before-retry gate.

This makes the RESEARCH-BEFORE-RETRY rule in the coordinator playbook
executable: a failed verify (or a recurrence of the same failure shape)
enqueues exactly one research item, and a retry of that failure is refused
until an accepted findings artifact exists for the failure id.

Prior art this module deliberately reuses instead of replacing:

* :mod:`bundlebees_plugin.coordinator_actions` — the durable, create-only,
  idempotent action queue. A research action is one record there, keyed by the
  research key; replay returns the stored record.
* :mod:`bundlebees_plugin.work_scheduler` — the durable work queue whose
  ``research`` planning kind already has its own bounded slots. The companion
  work item is added with ``add_item`` (create-only), never written by hand.
* :mod:`bundlebees_plugin.gate_watcher` — the decision queue. An
  environment-problem verdict becomes a backlog slice plus an owner decision
  record there; it never becomes a retry.
* :mod:`bundlebees_plugin.verify_workers.publish_verify_result` — the intended
  trigger point for a non-accepted verify outcome, though that path is not
  wired into any production flow yet.
* :meth:`bundlebees_plugin.tools._dispatch` — the retry gate, after
  ``validate_brief`` and before any launcher call.

Research state is evidence, not a second queue: one JSON failure record per
attempt/failure and one findings artifact per failure id. The queues above
remain the only queues. Every text field is passed through
:func:`bundlebees_plugin.security.scrub`, and failure shapes exclude
timestamps, attempt ids, run ids and absolute paths so a recurrence hashes the
same.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from . import coordinator_actions
from .security import scrub

__all__ = [
    "VERDICTS",
    "ResearchGateError",
    "research_dir",
    "failure_path",
    "findings_path",
    "failure_signature",
    "research_key",
    "failure_id",
    "record_failure",
    "enqueue_research",
    "validate_findings",
    "attachment_cap",
    "record_findings",
    "require_findings",
    "route_verdict",
]

SCHEMA = "bundlebees.research-failure/1"
FINDINGS_SCHEMA = "bundlebees.research-findings/1"

#: Findings verdicts. ``unknown`` is accepted as an artifact but never
#: unblocks a retry; ``environment-problem`` routes to the decision queue.
VERDICTS: tuple[str, ...] = ("patch-bug", "environment-problem", "unknown")

_RESEARCH_KIND = "research"
_REPAIR_KIND = "repair"
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_ATTEMPT_RE = re.compile(r"\batt[_-][A-Za-z0-9._-]+", re.IGNORECASE)
_RUN_RE = re.compile(r"\brun[_-][A-Za-z0-9._-]+", re.IGNORECASE)
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|/)(?:[^\s:;\"']+[\\/])+(?P<base>[^\s:;\"']+)"
)
_TEST_TOKEN_RE = re.compile(r"[\w./\\-]+\.py(?:::[^\s,;]+)?")
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


class ResearchGateError(Exception):
    """A research-state read/write was refused or failed closed."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(scrub(message))
        self.code = str(code)
        self.message = str(scrub(message))
        self.fix = str(scrub(fix))

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


def _refusal(code: str, error: str, fix: str, **extra: Any) -> dict[str, Any]:
    body = {
        "ok": False,
        "code": str(code),
        "error": str(scrub(error)),
        "fix": str(scrub(fix)),
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Research-state paths (evidence only; the queues stay the queues)
# ---------------------------------------------------------------------------


def research_dir(state: Any) -> Path:
    return Path(state) / "research"


def failure_path(state: Any, identifier: Any) -> Path:
    return research_dir(state) / "failures" / f"{_safe_component(identifier, 'failure id')}.json"


def findings_path(state: Any, failure_id_value: Any) -> Path:
    return research_dir(state) / "findings" / f"{_safe_component(failure_id_value, 'failure id')}.json"


def _safe_component(value: Any, what: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _SAFE_ID_RE.fullmatch(text):
        raise ResearchGateError(
            "identifier_invalid",
            f"{what} {text[:40]!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 128 characters.",
        )
    return text


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically write a private JSON evidence record."""

    _private_dir(path.parent)
    body = json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=".research-", dir=str(path.parent))
    try:
        os.write(fd, body)
        os.fsync(fd)
        os.close(fd)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ResearchGateError(
            "research_unreadable",
            f"research record {path.name} cannot be read: {exc}",
            "Restore the research record before retrying; a blind retry is refused.",
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ResearchGateError(
            "research_corrupt",
            f"research record {path.name} is not valid JSON: {exc}",
            "Restore the research record before retrying; a blind retry is refused.",
        ) from exc
    if not isinstance(data, dict):
        raise ResearchGateError(
            "research_corrupt",
            f"research record {path.name} is not an object",
            "Restore the research record before retrying; a blind retry is refused.",
        )
    return data


# ---------------------------------------------------------------------------
# Failure normalisation: the stable shape and its idempotency key
# ---------------------------------------------------------------------------


def _normalise_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    text = _ATTEMPT_RE.sub("<attempt>", text)
    text = _RUN_RE.sub("<run>", text)
    text = _ABS_PATH_RE.sub(lambda match: match.group("base"), text)
    return str(scrub(text))


def _normalise_test_id(value: Any) -> str:
    text = _normalise_text(value).strip()
    if not text:
        return ""
    parts = text.replace("\\", "/").split("::")
    path = parts[0]
    if path.startswith("/") or _DRIVE_RE.match(path):
        path = "/".join(path.split("/")[-2:])
    return "::".join([path, *parts[1:]])


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _failing_tests(failure: Mapping[str, Any]) -> list[str]:
    raw: list[Any] = []
    for key in ("failing_tests", "failed_tests", "tests"):
        raw.extend(_as_list(failure.get(key)))
    record = failure.get("record")
    if isinstance(record, Mapping):
        for key in ("failing_tests", "tests"):
            raw.extend(_as_list(record.get(key)))
        suite = record.get("suite")
        if isinstance(suite, Mapping):
            raw.extend(_as_list(suite.get("failing_tests") or suite.get("tests")))
    message = failure.get("message") or failure.get("error") or failure.get("key_error_line") or ""
    raw.extend(_TEST_TOKEN_RE.findall(str(message)))
    result: set[str] = set()
    for item in raw:
        normalised = _normalise_test_id(item)
        if normalised:
            result.add(normalised)
    return sorted(result)


def _key_error_line(failure: Mapping[str, Any]) -> str:
    explicit = failure.get("key_error_line")
    if isinstance(explicit, str) and explicit.strip():
        return _single_line(explicit)
    for key in ("message", "error", "stderr", "detail"):
        text = failure.get(key)
        if isinstance(text, str) and text.strip():
            return _single_line(text)
    return ""


def _single_line(value: Any) -> str:
    text = _normalise_text(value)
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:500]
    return ""


def _failure_kind(failure: Mapping[str, Any]) -> str:
    return _single_line(
        failure.get("kind") or failure.get("failure_kind") or failure.get("stage") or "verify"
    )[:40]


def _error_code(failure: Mapping[str, Any]) -> str:
    value = failure.get("code") or failure.get("error_code") or failure.get("reason") or ""
    return _single_line(value)[:80]


def failure_signature(failure: Mapping[str, Any]) -> str:
    """Stable sha256 over the normalised failure shape.

    Includes kind, error code, sorted failing test ids and the key error line;
    excludes timestamps, attempt ids, run ids and absolute path prefixes so a
    recurrence of the same failure produces the same signature.
    """

    if not isinstance(failure, Mapping):
        failure = {"message": failure}
    shape = {
        "kind": _failure_kind(failure),
        "code": _error_code(failure),
        "failing_tests": _failing_tests(failure),
        "key_error_line": _key_error_line(failure),
    }
    body = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _lineage_value(value: Any) -> str:
    """Normalise a lineage identifier without erasing the attempt id itself."""

    text = "" if value is None else str(value)
    text = _ABS_PATH_RE.sub(lambda match: match.group("base"), text)
    return str(scrub(text)).strip()[:160]


def _lineage_text(failure: Mapping[str, Any], lineage: Any = None) -> str:
    if lineage is None:
        lineage = failure.get("lineage")
    if isinstance(lineage, str) and lineage.strip():
        return _lineage_value(lineage)
    if isinstance(lineage, Mapping):
        for key in ("retry_of", "chain", "attempt_id"):
            value = lineage.get(key)
            if value:
                return _lineage_value(value)
    if isinstance(lineage, (list, tuple)):
        parts = [_lineage_value(item) for item in lineage]
        return ">".join(part for part in parts if part)
    retry_of = failure.get("retry_of")
    if isinstance(retry_of, str) and retry_of.strip():
        return "retry:" + _lineage_value(retry_of)
    attempt = failure.get("attempt_id") or failure.get("attempt") or "first"
    return "attempt:" + _lineage_value(attempt)


def research_key(failure: Mapping[str, Any], lineage: Any = None) -> str:
    """Idempotency key: failure signature plus attempt lineage.

    The signature alone would swallow a genuinely new lineage; the attempt id
    alone would enqueue a second item for a recurrence. Both are required.
    """

    body = {
        "signature": failure_signature(failure),
        "lineage": _lineage_text(failure, lineage),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def failure_id(failure: Mapping[str, Any], lineage: Any = None) -> str:
    """Deterministic failure id shared by the action, work item and findings."""

    digest = hashlib.sha256(
        ("bundlebees-failure:" + research_key(failure, lineage)).encode("utf-8")
    ).hexdigest()
    return "f" + digest[:20]


def _versions(failure: Mapping[str, Any]) -> dict[str, str]:
    value = failure.get("versions")
    if isinstance(value, Mapping):
        result: dict[str, str] = {}
        for key in sorted(value):
            item = value[key]
            if isinstance(item, (str, int, float, bool)):
                result[str(key)[:40]] = _single_line(item)[:80]
        if result:
            return result
    return {
        "tool": _single_line(failure.get("tool_version") or "unknown")[:80],
        "runtime": _single_line(failure.get("runtime_version") or platform.python_version())[:80],
    }


def _failure_record(
    failure: Mapping[str, Any], *, signature: str, key: str, fid: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "failure_id": fid,
        "signature": signature,
        "research_key": key,
        "kind": _failure_kind(failure),
        "code": _error_code(failure),
        "key_error_line": _key_error_line(failure),
        "failing_tests": _failing_tests(failure),
        "versions": _versions(failure),
        "attempt_id": _lineage_value(failure.get("attempt_id") or ""),
        "retry_of": _lineage_value(failure.get("retry_of") or ""),
        "lineage": _lineage_text(failure),
        "needs_research": True,
    }


def record_failure(
    state_dir: Any, failure: Mapping[str, Any], *, lineage: Any = None
) -> dict[str, Any]:
    """Persist the normalised failure evidence for an attempt/failure id.

    This is evidence the retry gate reads; it is not another queue. It is
    written even when enqueueing the research action fails, so the failure is
    still marked as needing research.
    """

    signature = failure_signature(failure)
    key = research_key(failure, lineage)
    fid = failure_id(failure, lineage)
    record = _failure_record(failure, signature=signature, key=key, fid=fid)
    _write_json(failure_path(state_dir, fid), record)
    attempt = record.get("attempt_id")
    if attempt:
        try:
            _write_json(failure_path(state_dir, attempt), record)
        except ResearchGateError:
            pass
    return record


# ---------------------------------------------------------------------------
# Enqueue exactly one research action and one research work item
# ---------------------------------------------------------------------------


def enqueue_research(
    state_dir: Any,
    failure: Mapping[str, Any],
    *,
    repo: str = "",
    workflow_ref: str | None = None,
) -> dict[str, Any]:
    """Enqueue one research action and one work-scheduler research item.

    Both queues are create-only and idempotent, so a replay of the same
    failure returns the stored records and creates nothing. Any queue failure
    is a typed ``queue_unavailable`` refusal, never a silent success.
    """

    if not isinstance(failure, Mapping):
        failure = {"message": failure}
    signature = failure_signature(failure)
    key = research_key(failure)
    fid = failure_id(failure)
    action_id = "research-" + key[:40]
    work_id = "research-" + key[:40]
    action_key = "research:" + key
    versions = _versions(failure)
    title = (
        f"research {fid}: signature={signature} code={_error_code(failure) or '-'} "
        f"versions={json.dumps(versions, sort_keys=True)} failure_id={fid}"
    )
    record = _failure_record(failure, signature=signature, key=key, fid=fid)
    payload = dict(record)
    payload["repo"] = _single_line(repo)[:200]

    failure_recorded = False
    try:
        record_failure(state_dir, failure)
        failure_recorded = True
    except Exception:  # noqa: BLE001 - queue refusal below is the typed answer
        pass

    try:
        from . import work_scheduler  # lazy: keeps the module import graph inert

        existing_action = coordinator_actions.load(state_dir, action_id)
        existing_item = None
        try:
            for item in work_scheduler.list_items(state_dir):
                if item.get("work_id") == work_id:
                    existing_item = item
                    break
        except Exception:
            existing_item = None

        enqueue_kwargs: dict[str, Any] = {
            "action_key": action_key,
            "kind": _RESEARCH_KIND,
            "repo": str(repo or ""),
            "target": fid,
            "payload": payload,
        }
        if workflow_ref:
            enqueue_kwargs["workflow_ref"] = workflow_ref
        action = coordinator_actions.enqueue(state_dir, action_id, **enqueue_kwargs)
        work_item = work_scheduler.add_item(
            state_dir,
            work_id=work_id,
            title=title,
            kind=_RESEARCH_KIND,
            priority=2,
        )
    except Exception as exc:  # noqa: BLE001 - never let a queue error escape the trigger
        return _refusal(
            "queue_unavailable",
            f"research queue is unavailable for failure {fid}: {exc}",
            "Restore the coordinator action and work-scheduler queues; the failure still needs research.",
            failure_id=fid,
            signature=signature,
            research_key=key,
            failure_recorded=failure_recorded,
        )

    return {
        "ok": True,
        "code": "queued",
        "failure_id": fid,
        "signature": signature,
        "research_key": key,
        "created": existing_action is None and existing_item is None,
        "action": action,
        "work_item": work_item,
        "failure_recorded": failure_recorded,
    }


# ---------------------------------------------------------------------------
# Findings artifacts and the retry gate
# ---------------------------------------------------------------------------


def validate_findings(artifact: Any, failure_id: str) -> list[str]:
    """Return every problem with a findings artifact; empty means accepted."""

    problems: list[str] = []
    if not isinstance(artifact, Mapping):
        return ["findings artifact must be an object"]
    if not isinstance(failure_id, str) or not failure_id:
        problems.append("failure id is required")
    elif str(artifact.get("failure_id") or "") != failure_id:
        problems.append(
            f"failure_id {artifact.get('failure_id')!r} does not match {failure_id!r}"
        )
    sources = artifact.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append("sources must be a non-empty list of {kind, ref}")
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                problems.append(f"sources[{index}] must be an object with kind and ref")
                continue
            kind = source.get("kind")
            ref = source.get("ref")
            if not isinstance(kind, str) or not kind.strip():
                problems.append(f"sources[{index}].kind must be a non-empty string")
            if not isinstance(ref, str) or not ref.strip():
                problems.append(f"sources[{index}].ref must be a non-empty string")
    rejected = artifact.get("rejected_alternatives")
    if not isinstance(rejected, list) or not rejected:
        problems.append("rejected_alternatives must have at least one entry")
    verdict = artifact.get("verdict")
    if verdict not in VERDICTS:
        problems.append(
            "verdict must be one of: " + ", ".join(VERDICTS)
        )
    return problems


#: Caps on a findings artifact carried by a retry dispatch. A research record
#: is evidence, not a payload: it must stay small enough to render into a
#: worker prompt. Both limits are fixed, so the same artifact always gets the
#: same verdict.
ATTACH_MAX_ENTRIES = 20
ATTACH_MAX_BYTES = 16 * 1024


def attachment_cap(artifact: Any) -> dict[str, Any] | None:
    """Why *artifact* cannot be attached as-is, or ``None`` when it fits.

    Entries are the contract lists (``sources`` plus
    ``rejected_alternatives``); bytes are the artifact's canonical JSON
    encoding. The entry limit is checked first, so an artifact over both
    limits reports ``entries``. The caller refuses the dispatch with this
    recorded reason instead of trimming the record, so a worker never receives
    a silently shortened research artifact.
    """

    limits = {"max_entries": ATTACH_MAX_ENTRIES, "max_bytes": ATTACH_MAX_BYTES}
    if not isinstance(artifact, Mapping):
        return {"reason": "not_an_object", "entries": 0, "bytes": 0, **limits}
    entries = 0
    for name in ("sources", "rejected_alternatives"):
        value = artifact.get(name)
        if isinstance(value, list):
            entries += len(value)
    try:
        size = len(
            json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        return {"reason": "unserializable", "entries": entries, "bytes": 0, **limits}
    if entries > ATTACH_MAX_ENTRIES:
        return {"reason": "entries", "entries": entries, "bytes": size, **limits}
    if size > ATTACH_MAX_BYTES:
        return {"reason": "bytes", "entries": entries, "bytes": size, **limits}
    return None


def record_findings(state_dir: Any, artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Store one findings artifact under its failure id (evidence, not a queue)."""

    fid = artifact.get("failure_id") if isinstance(artifact, Mapping) else None
    path = findings_path(state_dir, fid)
    body = dict(artifact)
    body.setdefault("schema", FINDINGS_SCHEMA)
    _write_json(path, body)
    return body


def _load_failure(state_dir: Any, identifier: Any) -> dict[str, Any] | None:
    return _read_json(failure_path(state_dir, identifier))


def _artifact_label(state: Path | None, fid: str) -> str:
    """A display path for a refusal even when the failure id itself is invalid."""

    try:
        return findings_path(state if state is not None else Path("."), fid).as_posix()
    except ResearchGateError:
        return "research/findings/<invalid-id>.json"


def require_findings(state_dir: Any, brief: Mapping[str, Any]) -> dict[str, Any] | None:
    """Fail closed unless accepted findings exist for every gated failure id.

    A brief is gated when it carries ``failure_id`` or when its ``retry_of``
    names an attempt with a recorded failure. Any read/write problem refuses
    the retry; it never becomes permission to dispatch blind.
    """

    if not isinstance(brief, Mapping):
        return None
    explicit = brief.get("failure_id")
    retry_of = brief.get("retry_of")
    if not explicit and not retry_of:
        return None

    state = Path(state_dir) if state_dir is not None else None
    failure_ids: list[str] = []
    failure_records: list[dict[str, Any]] = []
    if explicit:
        if not isinstance(explicit, str) or not explicit.strip():
            return _refusal(
                "research_required",
                "failure_id must be a non-empty string",
                "Name the recorded failure this retry is for.",
                failure_id=str(explicit),
                artifact="",
            )
        failure_ids.append(explicit.strip())
    if retry_of:
        if state is None:
            return _refusal(
                "research_required",
                "the research state is unavailable, so the retry cannot be allowed",
                "Restore the plugin state directory before retrying; a blind retry is refused.",
                failure_id="",
                artifact="",
            )
        if not isinstance(retry_of, str) or not retry_of.strip():
            return _refusal(
                "research_required",
                "retry_of must be a non-empty attempt id",
                "Name the failed attempt this brief retries.",
                failure_id="",
                artifact="",
            )
        try:
            failure = _load_failure(state, retry_of.strip())
        except ResearchGateError as exc:
            return _refusal(
                "research_required",
                f"research state for {retry_of.strip()} cannot be read: {exc.message}",
                "Restore the research state before retrying; a blind retry is refused.",
                failure_id="",
                artifact="",
            )
        if failure is not None:
            fid = str(failure.get("failure_id") or "")
            if fid:
                failure_ids.append(fid)
                failure_records.append(failure)

    for fid in dict.fromkeys(failure_ids):
        for prior in failure_records:
            if str(prior.get("failure_id") or "") == fid and str(prior.get("verdict") or "") == "environment-problem":
                return _refusal(
                    "environment_problem",
                    f"failure {fid} was routed as an environment problem; no retry is allowed",
                    "Fix the environment through the decision queue, then dispatch new work.",
                    failure_id=fid,
                    artifact=_artifact_label(state, fid),
                )
        artifact: Any = None
        direct = brief.get("findings")
        if isinstance(direct, Mapping) and str(direct.get("failure_id") or "") == fid:
            artifact = direct
        if artifact is None:
            if state is None:
                return _refusal(
                    "research_required",
                    f"the research state is unavailable; no findings for failure {fid}",
                    "Restore the plugin state directory before retrying; a blind retry is refused.",
                    failure_id=fid,
                    artifact=_artifact_label(None, fid),
                )
            path = findings_path(state, fid)
            try:
                artifact = _read_json(path)
            except ResearchGateError as exc:
                return _refusal(
                    "research_required",
                    f"findings for failure {fid} cannot be read: {exc.message}",
                    f"Restore {path.as_posix()} before retrying; a blind retry is refused.",
                    failure_id=fid,
                    artifact=path.as_posix(),
                )
            if artifact is None:
                return _refusal(
                    "research_required",
                    f"no accepted findings artifact for failure {fid}",
                    f"Record accepted findings at {path.as_posix()} before retrying.",
                    failure_id=fid,
                    artifact=path.as_posix(),
                )
        problems = validate_findings(artifact, fid)
        if problems:
            return _refusal(
                "research_required",
                f"findings for failure {fid} are not accepted: " + "; ".join(problems),
                "Fix the findings artifact; a blind retry is refused.",
                failure_id=fid,
                artifact=str(artifact.get("artifact") or _artifact_label(None, fid)),
            )
        verdict = str(artifact.get("verdict") or "")
        if verdict == "environment-problem":
            return _refusal(
                "environment_problem",
                f"failure {fid} is an environment problem; no retry is allowed",
                "Fix the environment through the decision queue, then dispatch new work.",
                failure_id=fid,
                artifact=_artifact_label(None, fid),
            )
        if verdict == "unknown":
            return _refusal(
                "research_inconclusive",
                f"failure {fid} has no conclusive findings (verdict unknown); no retry is allowed",
                "Resolve the unknown verdict with sources and a rejected alternative.",
                failure_id=fid,
                artifact=_artifact_label(None, fid),
            )

    for failure in failure_records:
        if str(failure.get("verdict") or "") == "environment-problem":
            fid = str(failure.get("failure_id") or "")
            return _refusal(
                "environment_problem",
                f"failure {fid} was routed as an environment problem; no retry is allowed",
                "Fix the environment through the decision queue, then dispatch new work.",
                failure_id=fid,
                artifact=_artifact_label(None, fid),
            )
    return None


def _persist_failure_copies(state: Path, fid: str, failure: Mapping[str, Any]) -> None:
    """Keep the failure-id and attempt-id evidence copies in step."""

    try:
        _write_json(failure_path(state, fid), failure)
        attempt = failure.get("attempt_id")
        if isinstance(attempt, str) and attempt:
            _write_json(failure_path(state, attempt), failure)
    except Exception:  # noqa: BLE001 - routing already succeeded
        pass


# ---------------------------------------------------------------------------
# Verdict routing
# ---------------------------------------------------------------------------


def _env_slice(fid: str) -> dict[str, Any]:
    source_rev = hashlib.sha256(("environment:" + fid).encode("utf-8")).hexdigest()[:40]
    return {
        "id": "env-" + fid,
        "title": f"environment problem for failure {fid}",
        "project": "research",
        "lane": "research",
        "priority": 2,
        "gates": [{"type": "none"}],
        "brief": {
            "objective": f"Resolve the environment problem behind failure {fid}",
            "source_rev": source_rev,
            "assigned_files": [f"research/{fid}.md"],
            "acceptance": {
                "criteria": ["the environment problem is fixed or escalated with evidence"],
                "tests": [],
            },
        },
    }


def route_verdict(state_dir: Any, failure_id: str, verdict: str) -> dict[str, Any]:
    """Route a findings verdict; only a patch-bug verdict can lead to a retry.

    ``environment-problem`` writes a backlog slice plus an owner decision in
    :mod:`gate_watcher` and returns ``retry_allowed: False``. ``patch-bug``
    records a create-only repair intent in the coordinator action queue.
    ``unknown`` writes nothing and stays blocked.
    """

    fid = str(failure_id or "").strip()
    if not _SAFE_ID_RE.fullmatch(fid):
        return _refusal(
            "identifier_invalid",
            f"failure id {fid[:40]!r} is not a safe identifier",
            "Pass the failure id returned by enqueue_research.",
        )
    value = str(verdict or "").strip().lower()
    if value not in VERDICTS:
        return _refusal(
            "verdict_invalid",
            f"verdict {verdict!r} is not one of {VERDICTS}",
            "Record patch-bug, environment-problem or unknown.",
        )

    state = Path(state_dir)
    try:
        failure = _load_failure(state, fid)
    except ResearchGateError as exc:
        return _refusal(
            "research_unreadable",
            f"failure {fid} cannot be read: {exc.message}",
            "Restore the research record before routing a verdict.",
        )

    if value == "environment-problem":
        try:
            from . import gate_watcher as gw  # lazy: no module-import cycle

            try:
                gw.seed_backlog(state, slices=[])
            except gw.GateWatcherError as exc:
                if exc.code != "backlog_exists":
                    raise
            slice_record = gw.add_slice(state, _env_slice(fid))
            note = f"environment-problem for failure {fid}; retry refused until the environment is fixed"
            decision = None
            for existing in gw.load_backlog(state).get("decisions") or ():
                if (
                    existing.get("slice_id") == slice_record["id"]
                    and existing.get("decision") == "decline"
                    and existing.get("by") == "research-gate"
                    and existing.get("note") == note
                ):
                    decision = dict(existing)
                    break
            if decision is None:
                decision = gw.record_owner_decision(
                    state,
                    slice_record["id"],
                    decision="decline",
                    by="research-gate",
                    note=note,
                )
        except Exception as exc:  # noqa: BLE001 - typed refusal, never a silent route
            return _refusal(
                "decision_queue_unavailable",
                f"the decision queue is unavailable for failure {fid}: {exc}",
                "Restore the gate-watcher backlog; the retry stays blocked.",
                failure_id=fid,
            )
        if failure is not None:
            failure["verdict"] = value
            failure["decision_slice_id"] = slice_record["id"]
            _persist_failure_copies(state, fid, failure)
        return {
            "ok": True,
            "verdict": value,
            "failure_id": fid,
            "retry_allowed": False,
            "slice_id": slice_record["id"],
            "decision": decision,
        }

    if value == "patch-bug":
        try:
            kwargs: dict[str, Any] = {
                "action_key": "repair:" + fid,
                "kind": _REPAIR_KIND,
                "target": fid,
                "payload": {
                    "failure_id": fid,
                    "verdict": value,
                    "intent": "repair the bug named by accepted findings",
                },
            }
            ref = str((failure or {}).get("workflow_ref") or "")
            if re.fullmatch(r"[0-9a-fA-F]{40}", ref):
                kwargs["workflow_ref"] = ref
            action = coordinator_actions.enqueue(state, "repair-" + fid, **kwargs)
        except Exception as exc:  # noqa: BLE001 - typed refusal
            return _refusal(
                "queue_unavailable",
                f"the repair intent for failure {fid} could not be queued: {exc}",
                "Restore the coordinator action queue; the retry stays blocked.",
                failure_id=fid,
            )
        if failure is not None:
            failure["verdict"] = value
            _persist_failure_copies(state, fid, failure)
        return {
            "ok": True,
            "verdict": value,
            "failure_id": fid,
            "retry_allowed": True,
            "repair_action": action,
        }

    if failure is not None:
        failure["verdict"] = value
        _persist_failure_copies(state, fid, failure)
    return {
        "ok": True,
        "verdict": value,
        "failure_id": fid,
        "retry_allowed": False,
    }
