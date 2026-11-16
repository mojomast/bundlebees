"""Graceful recovery for attempts that die on environment faults.

Today's incident, which this module exists to never repeat: a 2.6-hour
debugging bee died on a worker transport fault and its diagnosis was
recovered only by manually mining the durable event stream; a failed
manual fire had taken the coordinator claim and its death poisoned the
scheduler for ~14 minutes.  Owner directive: *make sure next time this
happens we recover from it gracefully.*

Four mechanisms, all durable and all mechanical — no model is consulted
and no prose note ever authorises a dispatch:

* **Env-fault detection** (:func:`classify_env_fault`).  A failed attempt
  whose evidence names the worker transport/workspace substrate — shell
  transport failed, ``workspace_invalid``, stream closed, container
  engine EOF — is classified an *environment* failure.  That is the
  class that must never blind-retry: the worker never returned a
  verdict, so there is no evidence the patch was wrong, and there is no
  evidence the environment is healed either.
* **Event-stream recovery digest** (:func:`recovery_digest`,
  :func:`save_digest`, :func:`load_digest`).  For an env-failed attempt
  the recovery pass extracts the worker narration (``worker:message``
  texts) and key findings from the durable event stream into a
  structured digest: what was diagnosed, the repro commands, the
  environment lessons.  The digest is create-only, hashed, and verified
  on every load, so the resume step can cite evidence, not memory.
* **Resume-with-evidence re-dispatch** (:func:`compose_resume_brief`,
  :func:`validate_resume_brief`, :func:`verify_resume_brief`,
  :func:`record_resume`).  The recovery pass composes a resume brief
  that embeds the digest explicitly labelled ``recovered-findings`` and
  re-dispatches the *same* lease and branch.  A blind retry without a
  digest is refused — by the validator, not merely by the composer, so a
  hand-built brief cannot launder a blind retry either.  The resume
  records provenance: the dead attempt id and the digest source.
* **Claim release on death** (:func:`take_claim`, :func:`release_claim`,
  :func:`release_claim_on_death`, :func:`takeover_claim`).  A beat or
  attempt that dies after taking the coordinator claim does not keep
  holding it: once death is proven — the env-fault classification of the
  attempt, a dead holder pid, or an expired TTL — the claim file is
  released and the release is logged.  A stale-claim takeover after the
  documented threshold (:data:`CLAIM_STALE_THRESHOLD_S`) is the
  fallback for a holder that cannot be proven alive.  The claim file
  lives beside the scheduler queue and never touches it:
  :func:`recover_scheduler_claim` un-poisons a queue item a dead beat
  left claimed through ``work_scheduler``'s own public API only.

Durable layout under the plugin state directory::

    graceful/<attempt_id>/stream.jsonl            # durable event stream (append-only)
    graceful/<attempt_id>/digest.json             # create-only recovery digest
    graceful/<attempt_id>/resume.json             # create-only resume record
    coordinator/beat.claim.json                   # the coordinator claim file
    coordinator/beat.claim.releases.jsonl         # append-only release log

The reconciliation sweep and the gate-watcher duties read the durable
queue and the scheduler claim; this module adds the beat claim file and
the recovery pass beside them and mutates the queue only through
``work_scheduler``'s public, owner-checked API.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .security import scrub

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot prove one claim
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "SCHEMA",
    "DIGEST_SCHEMA",
    "RESUME_SCHEMA",
    "CLAIM_SCHEMA",
    "GRACEFUL_DIR",
    "CLAIM_TTL_S",
    "CLAIM_STALE_THRESHOLD_S",
    "ENV_FAULT_CODES",
    "ENV_FAULT_CLASS",
    "GracefulRecoveryError",
    "classify_env_fault",
    "is_env_fault",
    "stream_path",
    "record_stream_event",
    "read_stream",
    "recovery_digest",
    "digest_path",
    "save_digest",
    "load_digest",
    "compose_resume_brief",
    "validate_resume_brief",
    "verify_resume_brief",
    "record_resume",
    "resume_path",
    "claim_path",
    "take_claim",
    "release_claim",
    "release_claim_on_death",
    "takeover_claim",
    "claim_state",
    "read_release_log",
    "recover_scheduler_claim",
]

#: The one protocol version this module emits.
SCHEMA = "bundlebees.graceful-recovery/1"
DIGEST_SCHEMA = "bundlebees.recovery-digest/1"
RESUME_SCHEMA = "bundlebees.resume-brief/1"
CLAIM_SCHEMA = "bundlebees.coordinator-claim/1"

#: Recovery digests live under ``graceful/<attempt_id>/``.
GRACEFUL_DIR = "graceful"
STREAM_NAME = "stream.jsonl"
DIGEST_NAME = "digest.json"
RESUME_NAME = "resume.json"

#: The coordinator claim file, beside the scheduler queue in ``coordinator/``.
CLAIM_FILE = "beat.claim.json"
CLAIM_RELEASE_LOG = "beat.claim.releases.jsonl"
CLAIM_LOCK = "beat.claim.lock"

#: How long a claim stays live without renewal: one heartbeat period.  A
#: living beat renews by taking its own claim again (idempotent).
CLAIM_TTL_S = 600.0

#: The documented stale threshold for the takeover fallback.  Today's
#: failed manual fire poisoned the scheduler for ~14 minutes because
#: nothing could take its claim; this threshold bounds that class at one
#: heartbeat period even when the holder cannot be proven dead.  The
#: primary path is :func:`release_claim_on_death`, which un-poisons
#: immediately once death is proven.
CLAIM_STALE_THRESHOLD_S = 600.0

#: The only class an env-faulted attempt may carry, and the class that
#: must never blind-retry.
ENV_FAULT_CLASS = "environment"

#: The stable codes for the transport/workspace substrate failures.
ENV_FAULT_CODES: tuple[str, ...] = (
    "shell_transport_failed",
    "workspace_invalid",
    "stream_closed",
    "container_engine_eof",
)

#: Phrase matchers, most specific first; the first match wins, so a
#: detail that names the shell transport is ``shell_transport_failed``
#: even when the same text also mentions a closed stream.
_FAULT_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "workspace_invalid",
        (
            "workspace_invalid",
            "workspace invalid",
            "workspace missing",
            "workspace vanished",
            "workspace unusable",
            "worktree missing",
            "no workdir",
        ),
    ),
    (
        "shell_transport_failed",
        (
            "shell transport failed",
            "shell transport",
            "worker transport failed",
            "transport failed",
            "pty hit eof",
            "pty read eof",
            "pty died",
            "pty closed",
            "terminal died",
            "shell died",
        ),
    ),
    (
        "container_engine_eof",
        (
            "container engine eof",
            "container_engine_eof",
            "container engine",
            "engine eof",
            "dockerd",
            "containerd",
        ),
    ),
    (
        "stream_closed",
        (
            "stream closed",
            "stream_closed",
            "output stream ended",
            "session stream closed",
            "stream ended before",
        ),
    ),
)

#: Observation codes that name the transport substrate directly.  A bare
#: ``worker_failed`` is deliberately absent: without detail it could as
#: easily be a patch failure, and this classifier never guesses in favour
#: of the never-blind-retry class.
_DIRECT_CODES = frozenset(
    {
        "shell_transport_failed",
        "workspace_invalid",
        "stream_closed",
        "container_engine_eof",
        "transport_error",
        "transport_failed",
        "worker_lost",
        "worker_crashed",
        "connection_lost",
        "broken_pipe",
    }
)

_GENERIC_PHRASES: tuple[str, ...] = ("connection lost", "broken pipe", "transport error")
_EOF_CONTEXT = ("transport", "stream", "pty", "shell", "container", "worker", "session", "console")

_MAX_TEXT = 2000
_MAX_BUCKET = 32
_MAX_NARRATION = 64
_MAX_DETAIL = 400

_NARRATION_KINDS = frozenset({"worker:message", "worker_message", "message"})
_FINDING_KINDS = frozenset({"finding", "worker:finding", "worker_finding"})

#: The leading labels a worker uses to flag a key finding in narration,
#: mapped onto the digest's buckets.
_BUCKET_LABELS: dict[str, tuple[str, ...]] = {
    "root_cause": ("root cause", "root-cause", "cause:"),
    "suspects": ("suspect", "suspicion", "possibly caused"),
    "repro": ("repro", "reproduce", "reproduction"),
    "lessons": ("lesson", "gotcha", "for next time", "next time"),
}

#: The ``kind`` an explicit finding record carries, mapped onto the
#: digest's buckets.
_BUCKET_KEY: dict[str, str] = {
    "root_cause": "root_cause",
    "cause": "root_cause",
    "suspect": "suspects",
    "suspects": "suspects",
    "repro": "repro",
    "repro_command": "repro",
    "lesson": "lessons",
    "lessons": "lessons",
    "env_lesson": "lessons",
    "environment_lesson": "lessons",
}

_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")

_FIX_DIGEST = (
    "Re-run the recovery pass so the durable stream under graceful/<attempt_id>/ "
    "is digested again; do not hand-edit digest.json."
)
_FIX_CLAIM = (
    "Inspect coordinator/beat.claim.json; release it only with proof of the "
    "holder's death, or take it over after the documented stale threshold."
)


class GracefulRecoveryError(Exception):
    """A graceful-recovery step was refused.

    ``code`` is a stable machine string so a caller can tell a blind-retry
    refusal from a live claim.  Error text is scrubbed before exposure.
    """

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(scrub(str(message)))
        self.fix = str(scrub(str(fix))) if fix else ""

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# Time, identifiers and small helpers
# ---------------------------------------------------------------------------


def _safe_id(value: Any, what: str, code: str = "attempt_invalid") -> str:
    text = str(value if value is not None else "").strip()
    if (
        not text
        or len(text) > 96
        or text in {".", ".."}
        or any(ch not in _ALLOWED_ID for ch in text)
    ):
        raise GracefulRecoveryError(
            code,
            f"{what} {text!r} is not a safe path component",
            "Use letters, digits, '-', '_' and '.' only, at most 96 characters.",
        )
    return text


def _moment(now: Any) -> tuple[float, str]:
    """``now`` as ``(epoch seconds, ISO-8601 Z)``; ``None`` means real time."""

    if now is None:
        epoch = time.time()
    elif callable(now):
        epoch = float(now())
    elif isinstance(now, (int, float)) and not isinstance(now, bool):
        epoch = float(now)
    else:
        text = str(now).strip()
        try:
            epoch = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise GracefulRecoveryError(
                "clock_invalid", f"now {text!r} is not epoch seconds or an ISO timestamp"
            ) from exc
    stamp = (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return epoch, stamp


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _record_text(record: Any) -> str:
    """The flattened evidence text of a failure record."""

    if isinstance(record, Mapping):
        parts: list[str] = []
        for key in (
            "code",
            "error",
            "failure_code",
            "event",
            "state",
            "status",
            "reason",
            "evidence",
            "detail",
            "message",
            "text",
        ):
            item = record.get(key)
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return " | ".join(parts)
    if record is None:
        return ""
    return str(record)


def _pid_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        os.kill(number, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 1. Env-fault detection
# ---------------------------------------------------------------------------


def classify_env_fault(record: Any) -> dict[str, Any]:
    """Classify a failed attempt's evidence as an environment fault or not.

    Environment faults are the transport/workspace substrate failures —
    the shell transport died, the workspace went invalid, the stream
    closed, the container engine hit EOF.  The worker never returned a
    verdict, so the patch is neither exonerated nor convicted: the only
    mechanical fact is that the attempt's diagnosis lives in its durable
    event stream and a blind retry is refused.

    ``record`` may be a failure mapping (``code``/``error``/``detail``/...),
    a plain string, or an already-computed verdict.  The specific phrase
    beats the declared generic code, so ``{"code": "worker_failed",
    "detail": "shell transport failed: pty EOF"}`` is classified by its
    detail.  A bare ``worker_failed`` without transport detail is *not* an
    environment fault: this classifier never guesses in favour of the
    never-blind-retry class.
    """

    evidence = _record_text(record)
    detail = _text(scrub(evidence), _MAX_DETAIL)
    lowered = evidence.lower()

    # Specific substrate phrases first: the detail beats the declared code.
    for code, phrases in _FAULT_MATCHERS:
        matched = [phrase for phrase in phrases if phrase in lowered]
        if matched:
            return _verdict(code, detail, matched)

    # An explicit transport-class observation code, declared or inlined.
    declared = ""
    if isinstance(record, Mapping):
        for key in ("code", "error", "failure_code"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                declared = value.strip().lower()
                break
    if declared in _DIRECT_CODES:
        return _verdict(declared, detail, [declared])
    for candidate in sorted(_DIRECT_CODES):
        if candidate in lowered:
            return _verdict(candidate, detail, [candidate])

    for phrase in _GENERIC_PHRASES:
        if phrase in lowered:
            return _verdict("transport_error", detail, [phrase])

    if "eof" in lowered and any(word in lowered for word in _EOF_CONTEXT):
        return _verdict("stream_closed", detail, ["eof"])

    return {
        "env_fault": False,
        "class": None,
        "code": None,
        "detail": detail,
        "matched": [],
        "blind_retry": "not_refused_by_this_classifier",
    }


def _verdict(code: str, detail: str, matched: list[str]) -> dict[str, Any]:
    return {
        "env_fault": True,
        "class": ENV_FAULT_CLASS,
        "code": code,
        "detail": detail,
        "matched": matched,
        "blind_retry": "refused",
    }


def is_env_fault(record: Any) -> bool:
    """True when the record classifies as an environment fault."""

    verdict = record if _is_verdict(record) else classify_env_fault(record)
    return verdict.get("env_fault") is True


def _is_verdict(record: Any) -> bool:
    return isinstance(record, Mapping) and "env_fault" in record and "class" in record


# ---------------------------------------------------------------------------
# Durable record helpers
# ---------------------------------------------------------------------------


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace *path* and fsync the file and its directory."""

    _private_dir(path.parent)
    payload = _canonical(dict(value)) + b"\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _create_only_json(path: Path, value: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create a JSON object once; return ``(record, created)``.

    Identical bytes are idempotent; different bytes at the same path are a
    conflict and are never overwritten.
    """

    _private_dir(path.parent)
    payload = _canonical(dict(value)) + b"\n"
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise GracefulRecoveryError(
            "record_unreadable", f"{path.name} cannot be read safely: {exc}", _FIX_DIGEST
        ) from exc
    if existing is not None:
        if existing == payload:
            return _decode(existing, path), False
        raise GracefulRecoveryError(
            "record_exists",
            f"{path.name} already holds different bytes",
            "Keep the original durable record; do not rewrite evidence.",
        )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _decode(path.read_bytes(), path), False
        _fsync_dir(path.parent)
        return dict(value), True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _decode(raw: bytes, path: Path, code: str = "record_corrupt") -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GracefulRecoveryError(
            code, f"{path.name} is not valid JSON", _FIX_DIGEST
        ) from exc
    if not isinstance(value, dict):
        raise GracefulRecoveryError(code, f"{path.name} is not a JSON object", _FIX_DIGEST)
    return value


def _attempt_dir(state_dir: Path, attempt_id: str) -> tuple[Path, str]:
    attempt = _safe_id(attempt_id, "attempt_id")
    return Path(state_dir) / GRACEFUL_DIR / attempt, attempt


# ---------------------------------------------------------------------------
# 2. The durable event stream
# ---------------------------------------------------------------------------


def stream_path(state_dir: Path, attempt_id: str) -> Path:
    """The durable event stream of one attempt."""

    directory, _ = _attempt_dir(state_dir, attempt_id)
    return directory / STREAM_NAME


def record_stream_event(
    state_dir: Path, attempt_id: str, record: Mapping[str, Any], *, now: Any = None
) -> dict[str, Any]:
    """Append one event to the attempt's durable stream (fsynced, one line).

    The record is scrubbed before it is stored: a narration text may quote
    worker output, and the stream is durable evidence, not a secret store.
    """

    directory, _ = _attempt_dir(state_dir, attempt_id)
    if not isinstance(record, Mapping):
        raise GracefulRecoveryError(
            "stream_record_invalid",
            "a stream event must be a JSON object",
            "Record the event as a mapping; the stream is machine-mined evidence.",
        )
    entry = dict(scrub(dict(record)))
    entry.setdefault("ts", _moment(now)[1])
    line = _canonical(entry) + b"\n"
    _private_dir(directory)
    fd = os.open(
        directory / STREAM_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
    )
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(directory)
    return entry


def read_stream(state_dir: Path, attempt_id: str) -> list[dict[str, Any]]:
    """Every record of the attempt's durable stream, in stream order."""

    path = stream_path(state_dir, attempt_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise GracefulRecoveryError(
            "stream_unreadable", f"the event stream cannot be read: {exc}", _FIX_DIGEST
        ) from exc
    records: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GracefulRecoveryError(
                "stream_corrupt", f"{STREAM_NAME} line {number} is not valid JSON", _FIX_DIGEST
            ) from exc
        if not isinstance(record, dict):
            raise GracefulRecoveryError(
                "stream_corrupt", f"{STREAM_NAME} line {number} is not a JSON object", _FIX_DIGEST
            )
        records.append(record)
    return records


def _kind_of(record: Mapping[str, Any]) -> str:
    for key in ("type", "kind", "event"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def _text_of(record: Mapping[str, Any]) -> str:
    for key in ("text", "message", "detail", "finding"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = record.get("data")
    if isinstance(data, Mapping):
        nested = data.get("text")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _bucket_for_text(text: str) -> str | None:
    """The finding bucket a narration message leads with, or ``None``."""

    head = text.strip().lower()
    if not head:
        return None
    lead = head.split(":", 1)[0]
    for bucket, labels in _BUCKET_LABELS.items():
        for label in labels:
            if head.startswith(label) or (label in lead and len(lead) <= 24):
                return bucket
    return None


def _repro_command(text: str) -> str:
    """The shell command a repro message names, or the message itself."""

    if "$ " in text:
        tail = text.rsplit("$ ", 1)[-1]
        return _text(tail.split("#", 1)[0].strip())
    return _text(text)


def _dedupe(items: Iterable[str], limit: int) -> list[str]:
    """First occurrence wins, in order, capped at ``limit``."""

    ordered: list[str] = []
    markers: set[str] = set()
    for item in items:
        text = _text(item)
        key = text.lower()
        if not text or key in markers:
            continue
        markers.add(key)
        ordered.append(text)
        if len(ordered) >= limit:
            break
    return ordered


def recovery_digest(
    state_dir: Path,
    attempt_id: str,
    *,
    stream: Iterable[Mapping[str, Any]] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Extract the structured recovery digest from an attempt's event stream.

    The digest is what the next dispatch must know and what a human would
    have had to mine by hand: the worker narration (``worker:message``
    texts) in stream order, plus the key findings bucketed into root
    cause, suspects, repro commands and environment lessons — inferred
    from the narration's own leading labels, or taken verbatim from
    explicit ``finding`` records (explicit beats inferred).  The stream's
    terminal transport fault is classified with :func:`classify_env_fault`.

    ``stream`` replaces the durable read (used by callers that already
    hold the records); by default the durable ``stream.jsonl`` is read.
    """

    _, attempt = _attempt_dir(state_dir, attempt_id)
    records = [
        dict(record)
        for record in (stream if stream is not None else read_stream(state_dir, attempt))
    ]

    narration: list[dict[str, Any]] = []
    explicit: dict[str, list[str]] = {"root_cause": [], "suspects": [], "repro": [], "lessons": []}
    inferred: dict[str, list[str]] = {"root_cause": [], "suspects": [], "repro": [], "lessons": []}
    env_fault: dict[str, Any] | None = None

    for record in records:
        kind = _kind_of(record)
        text = _text_of(record)
        if kind in _NARRATION_KINDS and text:
            if len(narration) < _MAX_NARRATION:
                narration.append({"index": len(narration), "text": text[:_MAX_TEXT]})
        if kind in _FINDING_KINDS and text:
            bucket = _BUCKET_KEY.get(str(record.get("kind") or "").lower())
            if bucket is not None:
                explicit[bucket].append(text[:_MAX_TEXT])
        elif text:
            bucket = _bucket_for_text(text)
            if bucket is not None:
                inferred[bucket].append(text[:_MAX_TEXT])
        verdict = classify_env_fault(record)
        if verdict["env_fault"]:
            env_fault = verdict

    for bucket, found in inferred.items():
        explicit[bucket] = _dedupe(explicit[bucket] + found, _MAX_BUCKET)

    body = {
        "schema": DIGEST_SCHEMA,
        "attempt_id": attempt,
        "created_at": _moment(now)[1],
        "usable": bool(narration or any(explicit.values())),
        "narration_count": len(narration),
        "narration": narration,
        "root_cause": explicit["root_cause"],
        "suspects": explicit["suspects"],
        "repro": [_repro_command(text) for text in explicit["repro"]],
        "lessons": explicit["lessons"],
        "env_fault": env_fault,
        "source": {
            "stream": f"{GRACEFUL_DIR}/{attempt}/{STREAM_NAME}",
            "records": len(records),
            "messages": len(narration),
        },
    }
    body["digest_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def digest_path(state_dir: Path, attempt_id: str) -> Path:
    """The durable recovery digest of one attempt."""

    directory, _ = _attempt_dir(state_dir, attempt_id)
    return directory / DIGEST_NAME


def save_digest(
    state_dir: Path, attempt_id: str, digest: Mapping[str, Any], *, now: Any = None
) -> dict[str, Any]:
    """Persist the recovery digest create-only, with its self-digest.

    The stored record's ``digest_sha256`` is recomputed over the stored
    body, so what is hashed is exactly what is on disk.  A replay of the
    identical digest is idempotent; different bytes at the same path are
    refused.
    """

    directory, attempt = _attempt_dir(state_dir, attempt_id)
    if not isinstance(digest, Mapping) or str(digest.get("schema") or "") != DIGEST_SCHEMA:
        raise GracefulRecoveryError(
            "digest_invalid",
            "a recovery digest must carry schema " + DIGEST_SCHEMA,
            _FIX_DIGEST,
        )
    if str(digest.get("attempt_id") or "") != attempt:
        raise GracefulRecoveryError(
            "digest_invalid",
            "the digest names a different attempt than it is saved under",
            _FIX_DIGEST,
        )
    body = {key: value for key, value in digest.items() if key != "digest_sha256"}
    record = dict(body)
    record["digest_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    stored, created = _create_only_json(directory / DIGEST_NAME, record)
    return {
        "created": created,
        "record": stored,
        "path": f"{GRACEFUL_DIR}/{attempt}/{DIGEST_NAME}",
    }


def load_digest(state_dir: Path, attempt_id: str) -> dict[str, Any]:
    """Load and verify the durable recovery digest of one attempt."""

    directory, attempt = _attempt_dir(state_dir, attempt_id)
    path = directory / DIGEST_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise GracefulRecoveryError(
            "digest_missing",
            f"no recovery digest exists for {attempt}",
            "Run the recovery pass: digest the durable stream before resuming.",
        ) from exc
    except OSError as exc:
        raise GracefulRecoveryError(
            "digest_unreadable", f"{DIGEST_NAME} cannot be read: {exc}", _FIX_DIGEST
        ) from exc
    record = _decode(raw, path, code="digest_corrupt")
    stored = record.get("digest_sha256")
    body = {key: value for key, value in record.items() if key != "digest_sha256"}
    expected = hashlib.sha256(_canonical(body)).hexdigest()
    if not isinstance(stored, str) or not hmac.compare_digest(stored, expected):
        raise GracefulRecoveryError(
            "digest_mismatch",
            f"{DIGEST_NAME} does not match its self-digest",
            "The digest was edited after it was saved; re-run the recovery pass.",
        )
    return record


# ---------------------------------------------------------------------------
# 3. Resume-with-evidence re-dispatch
# ---------------------------------------------------------------------------


def resume_path(state_dir: Path, attempt_id: str) -> Path:
    """The durable resume record of one dead attempt."""

    directory, _ = _attempt_dir(state_dir, attempt_id)
    return directory / RESUME_NAME


def compose_resume_brief(
    state_dir: Path,
    *,
    dead_attempt_id: str,
    job_id: Any = None,
    task_id: Any = None,
    base_brief: Mapping[str, Any] | None = None,
    branch: Any = None,
    lease_files: Iterable[Any] | None = None,
    source_rev: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Compose the resume-with-evidence brief that re-dispatches the attempt.

    The brief embeds the dead attempt's recovery digest explicitly
    labelled ``recovered-findings`` and re-dispatches the *same* lease and
    branch: scope comes from the explicit arguments, the dead attempt's
    brief, or — last — its verified lease record.  A blind retry is
    refused when no durable digest exists, when the digest carries no
    evidence, or when the dead attempt's fault was not an environment
    fault (a patch failure belongs to the pinned recovery protocol, not
    here).
    """

    _, attempt = _attempt_dir(state_dir, dead_attempt_id)
    stamp = _moment(now)[1]

    try:
        digest = load_digest(state_dir, attempt)
    except GracefulRecoveryError as exc:
        if exc.code == "digest_missing":
            raise GracefulRecoveryError(
                "resume_without_evidence",
                f"no recovery digest exists for {attempt}: a blind retry is refused",
                "Run the recovery pass over the durable event stream first; never "
                "re-dispatch an env-faulted attempt without its digest.",
            ) from exc
        raise
    if digest.get("usable") is not True:
        raise GracefulRecoveryError(
            "resume_without_evidence",
            f"the recovery digest of {attempt} carries no narration or findings",
            "A digest without evidence is a blind retry; mine the durable stream "
            "for the worker's narration first.",
        )

    env_fault = digest.get("env_fault")
    if not isinstance(env_fault, Mapping) or env_fault.get("env_fault") is not True:
        raise GracefulRecoveryError(
            "resume_not_env_fault",
            f"the recorded fault of {attempt} is not an environment fault",
            "Non-environmental failures recover through the pinned recovery "
            "protocol (recovery/<job>/), not through a resume-with-evidence dispatch.",
        )

    base = dict(base_brief) if isinstance(base_brief, Mapping) else {}
    files = [
        str(item).strip()
        for item in (lease_files if lease_files is not None else base.get("lease_files") or ())
        if str(item).strip()
    ]
    branch_text = str(branch if branch is not None else base.get("branch") or "").strip()
    rev = str(source_rev if source_rev is not None else base.get("source_rev") or "").strip()
    if not files:
        try:
            from . import leases as _leases

            record = _leases.load_lease(Path(state_dir), attempt)
            files = [str(item) for item in _leases.lease_files(record)]
            branch_text = branch_text or str(record.get("branch") or "")
            rev = rev or str(record.get("source_rev") or "")
        except Exception:  # noqa: BLE001 - the lease is a fallback, never an authority here
            files = []
    if not files:
        raise GracefulRecoveryError(
            "resume_scope_missing",
            f"the resume of {attempt} has no lease scope to re-dispatch",
            "Pass the dead attempt's lease_files (or its brief); a resume re-dispatches "
            "the same lease, never a new one.",
        )

    findings = {
        "label": "recovered-findings",
        "digest_sha256": digest.get("digest_sha256"),
        "narration_count": digest.get("narration_count") or 0,
        "root_cause": list(digest.get("root_cause") or []),
        "suspects": list(digest.get("suspects") or []),
        "repro": list(digest.get("repro") or []),
        "lessons": list(digest.get("lessons") or []),
    }
    brief: dict[str, Any] = {
        "schema": RESUME_SCHEMA,
        "kind": "resume_with_evidence",
        "created_at": stamp,
        "dead_attempt_id": attempt,
        "dispatch": {
            "branch": branch_text or None,
            "lease_files": files,
            "source_rev": rev or None,
        },
        "recovered_findings": findings,
        "provenance": {
            "dead_attempt_id": attempt,
            "digest_source": f"{GRACEFUL_DIR}/{attempt}/{DIGEST_NAME}",
            "digest_sha256": digest.get("digest_sha256"),
            "stream_source": f"{GRACEFUL_DIR}/{attempt}/{STREAM_NAME}",
            "env_fault_code": env_fault.get("code"),
            "resumed_at": stamp,
        },
    }
    if job_id is not None and str(job_id).strip():
        brief["job_id"] = _safe_id(job_id, "job_id")
    if task_id is not None and str(task_id).strip():
        brief["task_id"] = _safe_id(task_id, "task_id")
    return brief


def validate_resume_brief(brief: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse any brief that is not a complete resume-with-evidence brief.

    This is the mechanism's blind-retry gate, not a courtesy check: a
    hand-composed brief without ``recovered_findings``, without provenance
    or without a lease scope is refused with the same codes the composer
    uses, so a blind retry cannot be laundered around the composer.
    """

    if not isinstance(brief, Mapping) or str(brief.get("schema") or "") != RESUME_SCHEMA:
        raise GracefulRecoveryError(
            "resume_invalid",
            f"a resume brief must carry schema {RESUME_SCHEMA}",
            "Compose the brief with compose_resume_brief.",
        )
    if str(brief.get("kind") or "") != "resume_with_evidence":
        raise GracefulRecoveryError(
            "resume_invalid", "a resume brief must be a resume_with_evidence dispatch"
        )
    attempt = _safe_id(brief.get("dead_attempt_id"), "dead_attempt_id")

    findings = brief.get("recovered_findings")
    if not isinstance(findings, Mapping) or str(findings.get("label") or "") != "recovered-findings":
        raise GracefulRecoveryError(
            "resume_without_evidence",
            f"the resume of {attempt} embeds no recovered-findings block",
            "A blind retry is refused: embed the recovery digest, labelled "
            "recovered-findings, or do not re-dispatch.",
        )
    evidence = any(
        isinstance(findings.get(key), (list, tuple)) and findings.get(key)
        for key in ("root_cause", "suspects", "repro", "lessons")
    ) or int(_count_or_zero(findings.get("narration_count"))) > 0
    if not evidence:
        raise GracefulRecoveryError(
            "resume_without_evidence",
            f"the recovered-findings block of {attempt} carries no evidence",
            "An empty digest is a blind retry; mine the durable stream first.",
        )

    provenance = brief.get("provenance")
    if not isinstance(provenance, Mapping):
        raise GracefulRecoveryError(
            "resume_provenance_missing",
            f"the resume of {attempt} records no provenance",
            "A resume names the dead attempt and the digest it recovered from.",
        )
    for key in ("dead_attempt_id", "digest_source", "digest_sha256"):
        if not str(provenance.get(key) or "").strip():
            raise GracefulRecoveryError(
                "resume_provenance_missing",
                f"the resume provenance of {attempt} is missing {key}",
                "A resume names the dead attempt and the digest it recovered from.",
            )
    if str(provenance.get("dead_attempt_id") or "") != attempt:
        raise GracefulRecoveryError(
            "resume_provenance_missing",
            "the resume provenance names a different dead attempt",
        )

    dispatch = brief.get("dispatch")
    files = dispatch.get("lease_files") if isinstance(dispatch, Mapping) else None
    if (
        not isinstance(dispatch, Mapping)
        or not isinstance(files, (list, tuple))
        or not files
        or not all(isinstance(item, str) and item.strip() for item in files)
    ):
        raise GracefulRecoveryError(
            "resume_scope_missing",
            f"the resume of {attempt} re-dispatches no lease scope",
            "A resume re-dispatches the same lease; name lease_files.",
        )

    normalized = dict(brief)
    normalized["dead_attempt_id"] = attempt
    return normalized


def _count_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def verify_resume_brief(state_dir: Path, brief: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a resume brief against the durable digest it claims to embed.

    The embedded findings must hash to the durable digest's self-digest
    and carry the same buckets; a brief whose evidence does not match what
    is on disk is refused.
    """

    normalized = validate_resume_brief(brief)
    attempt = str(normalized["dead_attempt_id"])
    digest = load_digest(state_dir, attempt)
    findings = normalized["recovered_findings"]
    provenance = normalized["provenance"]
    if str(provenance.get("digest_sha256") or "") != str(digest.get("digest_sha256") or ""):
        raise GracefulRecoveryError(
            "resume_evidence_mismatch",
            f"the resume of {attempt} cites a digest that is not the durable one",
            "Re-compose the brief from the durable digest; never edit findings in place.",
        )
    for key in ("root_cause", "suspects", "repro", "lessons"):
        embedded = list(findings.get(key) or [])
        durable = list(digest.get(key) or [])
        if embedded != durable:
            raise GracefulRecoveryError(
                "resume_evidence_mismatch",
                f"the {key} findings of {attempt} do not match the durable digest",
                "Re-compose the brief from the durable digest; never edit findings in place.",
            )
    if _count_or_zero(findings.get("narration_count")) != _count_or_zero(
        digest.get("narration_count")
    ):
        raise GracefulRecoveryError(
            "resume_evidence_mismatch",
            f"the narration count of {attempt} does not match the durable digest",
        )
    return digest


def record_resume(
    state_dir: Path, dead_attempt_id: str, brief: Mapping[str, Any], *, now: Any = None
) -> dict[str, Any]:
    """Persist the resume record create-only; replaying it is idempotent."""

    directory, attempt = _attempt_dir(state_dir, dead_attempt_id)
    normalized = validate_resume_brief(brief)
    verify_resume_brief(state_dir, normalized)
    stored, created = _create_only_json(directory / RESUME_NAME, normalized)
    return {
        "created": created,
        "record": stored,
        "path": f"{GRACEFUL_DIR}/{attempt}/{RESUME_NAME}",
    }


# ---------------------------------------------------------------------------
# 4. Claim release on death
# ---------------------------------------------------------------------------


def claim_path(state_dir: Path) -> Path:
    """The coordinator claim file, beside the scheduler queue."""

    return Path(state_dir) / "coordinator" / CLAIM_FILE


def _claim_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "coordinator" / CLAIM_LOCK


def _release_log_path(state_dir: Path) -> Path:
    return Path(state_dir) / "coordinator" / CLAIM_RELEASE_LOG


def _owner_id(owner: Any) -> str:
    return _safe_id(owner, "owner", code="owner_invalid")


def _read_claim(state_dir: Path) -> dict[str, Any] | None:
    path = claim_path(state_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GracefulRecoveryError(
            "claim_unreadable", f"{CLAIM_FILE} cannot be read: {exc}", _FIX_CLAIM
        ) from exc
    record = _decode(raw, path, code="claim_corrupt")
    if str(record.get("schema") or "") != CLAIM_SCHEMA:
        raise GracefulRecoveryError(
            "claim_corrupt",
            f"{CLAIM_FILE} is not schema {CLAIM_SCHEMA}",
            "Restore the claim file from its last durable copy; do not hand-edit it.",
        )
    return record


def _holder_state(existing: Mapping[str, Any], now_epoch: float) -> str:
    """``expired``, ``holder_dead`` or ``live`` for an existing claim."""

    expires = existing.get("expires_epoch")
    if isinstance(expires, (int, float)) and now_epoch >= float(expires):
        return "expired"
    pid = existing.get("pid")
    if pid is not None and not _pid_alive(pid):
        return "holder_dead"
    return "live"


def _append_release(state_dir: Path, entry: Mapping[str, Any]) -> None:
    path = _release_log_path(state_dir)
    _private_dir(path.parent)
    line = _canonical(dict(entry)) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _expires_iso(now_epoch: float, ttl_s: float) -> str:
    return (
        datetime.fromtimestamp(now_epoch + float(ttl_s), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_claim(
    state_dir: Path,
    *,
    owner: str,
    ttl_s: float,
    pid: int | None,
    now_epoch: float,
    stamp: str,
    generation: int,
    took_over_from: Mapping[str, Any] | None,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Publish the claim file; ``previous`` carries the renewal identity."""

    record: dict[str, Any] = {
        "schema": CLAIM_SCHEMA,
        "owner": owner,
        "pid": int(pid) if pid else None,
        "generation": int(generation),
        "taken_at": stamp if previous is None else str(previous.get("taken_at") or stamp),
        "renewed_at": stamp,
        "taken_epoch": (
            now_epoch if previous is None else float(previous.get("taken_epoch") or now_epoch)
        ),
        "renewed_epoch": now_epoch,
        "ttl_s": float(ttl_s),
        "expires_at": _expires_iso(now_epoch, ttl_s),
        "expires_epoch": now_epoch + float(ttl_s),
        "took_over_from": dict(took_over_from) if took_over_from else None,
    }
    _write_json(claim_path(state_dir), record)
    return record


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    """Serialise claim-file mutations with a kernel-held flock."""

    if fcntl is None:  # pragma: no cover - no portable fallback for one claim
        raise GracefulRecoveryError(
            "locking_unavailable",
            "this platform has no flock, so a single coordinator claim cannot be proven",
            "Run the coordinator on POSIX (Linux/macOS).",
        )
    lock = _claim_lock_path(state_dir)
    _private_dir(lock.parent)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def take_claim(
    state_dir: Path,
    owner: Any,
    *,
    ttl_s: float | None = None,
    pid: int | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Take the coordinator claim file for ``owner``.

    Refused while a live claim of another owner exists.  A claim whose TTL
    expired, or whose holder pid is dead, is replaced with provenance in
    ``took_over_from``.  Taking your own claim again is the renewal path:
    it extends the TTL and keeps the claim's identity and generation.
    """

    owner_text = _owner_id(owner)
    ttl = float(ttl_s) if ttl_s is not None else CLAIM_TTL_S
    if ttl <= 0:
        raise GracefulRecoveryError("ttl_invalid", "the claim TTL must be positive")
    holder_pid = int(pid) if pid else os.getpid()
    now_epoch, stamp = _moment(now)
    with _locked(state_dir):
        existing = _read_claim(state_dir)
        if existing is not None and str(existing.get("owner") or "") == owner_text:
            return _write_claim(
                state_dir,
                owner=owner_text,
                ttl_s=ttl,
                pid=holder_pid,
                now_epoch=now_epoch,
                stamp=stamp,
                generation=int(existing.get("generation") or 1),
                took_over_from=existing.get("took_over_from"),
                previous=existing,
            )
        if existing is not None:
            state = _holder_state(existing, now_epoch)
            if state == "live":
                raise GracefulRecoveryError(
                    "claim_held",
                    f"the coordinator claim is held by {existing.get('owner')!r}",
                    _FIX_CLAIM,
                )
            took_over_from = {
                "owner": str(existing.get("owner") or ""),
                "reason": state,
                "previous_taken_at": str(existing.get("taken_at") or ""),
            }
            generation = int(existing.get("generation") or 0) + 1
        else:
            took_over_from = None
            generation = 1
        return _write_claim(
            state_dir,
            owner=owner_text,
            ttl_s=ttl,
            pid=holder_pid,
            now_epoch=now_epoch,
            stamp=stamp,
            generation=generation,
            took_over_from=took_over_from,
            previous=None,
        )


def release_claim(state_dir: Path, *, owner: Any, now: Any = None) -> dict[str, Any]:
    """Cooperatively release the coordinator claim; only the owner may."""

    owner_text = _owner_id(owner)
    now_epoch, stamp = _moment(now)
    with _locked(state_dir):
        existing = _read_claim(state_dir)
        if existing is None:
            return {"released": False, "reason": "no_claim", "at": stamp}
        if str(existing.get("owner") or "") != owner_text:
            raise GracefulRecoveryError(
                "claim_not_held",
                f"the coordinator claim is held by {existing.get('owner')!r}, not {owner_text!r}",
                "Only the current owner releases the claim; use "
                "release_claim_on_death with proof, or takeover_claim after the threshold.",
            )
        _unlink_claim(state_dir)
        _append_release(
            state_dir,
            {
                "ts": stamp,
                "owner": owner_text,
                "reason": "released_by_owner",
                "proof": "owner",
                "generation": existing.get("generation"),
            },
        )
        return {"released": True, "reason": "released_by_owner", "at": stamp}


def _unlink_claim(state_dir: Path) -> None:
    try:
        claim_path(state_dir).unlink()
    except FileNotFoundError:
        return
    _fsync_dir(claim_path(state_dir).parent)


def _death_proof(
    existing: Mapping[str, Any], death_evidence: Any, now_epoch: float
) -> str | None:
    """The proof that the claim's holder died, or ``None``."""

    if isinstance(death_evidence, Mapping):
        if death_evidence.get("env_fault") is True:
            return "env_fault_evidence"
        if death_evidence.get("dead") is True:
            return "death_evidence"
    pid = existing.get("pid")
    if pid is not None and not _pid_alive(pid):
        return "holder_pid_dead"
    expires = existing.get("expires_epoch")
    if isinstance(expires, (int, float)) and now_epoch >= float(expires):
        return "ttl_expired"
    return None


def release_claim_on_death(
    state_dir: Path,
    *,
    owner: Any = None,
    death_evidence: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Release the coordinator claim once the holder's death is proven.

    The recovery pass calls this when a beat or attempt died after taking
    the claim — today's failed manual fire held the claim for ~14 minutes
    because nothing released it.  Proof of death is any one of: the
    env-fault classification of the attempt that died (``death_evidence``
    from :func:`classify_env_fault`), a dead holder pid, or an expired
    TTL.  A live claim is never released without proof.  The release is
    logged to the append-only release log.
    """

    now_epoch, stamp = _moment(now)
    owner_text = _owner_id(owner) if owner not in (None, "") else None
    with _locked(state_dir):
        existing = _read_claim(state_dir)
        if existing is None:
            return {"released": False, "reason": "no_claim", "at": stamp}
        holder = str(existing.get("owner") or "")
        if owner_text is not None and owner_text != holder:
            raise GracefulRecoveryError(
                "claim_not_held",
                f"the coordinator claim is held by {holder!r}, not {owner_text!r}",
                _FIX_CLAIM,
            )
        proof = _death_proof(existing, death_evidence, now_epoch)
        if proof is None:
            raise GracefulRecoveryError(
                "claim_release_refused",
                f"the claim of {holder!r} is live and no proof of death was given",
                "Pass the env-fault classification of the attempt that died, or "
                "take the claim over after the documented stale threshold.",
            )
        taken = existing.get("taken_epoch")
        held_for = now_epoch - float(taken) if isinstance(taken, (int, float)) else None
        _unlink_claim(state_dir)
        entry: dict[str, Any] = {
            "ts": stamp,
            "owner": holder,
            "reason": "released_on_death",
            "proof": proof,
            "generation": existing.get("generation"),
            "by": "graceful_recovery",
        }
        if held_for is not None:
            entry["held_for_s"] = round(max(0.0, held_for), 3)
        if isinstance(death_evidence, Mapping) and death_evidence.get("code"):
            entry["evidence_code"] = str(death_evidence["code"])
        _append_release(state_dir, entry)
        return {"released": True, "proof": proof, "owner": holder, "at": stamp}


def takeover_claim(
    state_dir: Path,
    owner: Any,
    *,
    ttl_s: float | None = None,
    threshold_s: float | None = None,
    pid: int | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Take the coordinator claim over from a stale holder — the fallback.

    The documented threshold (:data:`CLAIM_STALE_THRESHOLD_S`) bounds how
    long a claim whose holder cannot be proven alive may poison the
    scheduler: before the threshold the takeover is refused, after it the
    claim is taken with provenance.  A dead holder or an expired TTL is
    replaced immediately, like :func:`take_claim`.
    """

    owner_text = _owner_id(owner)
    ttl = float(ttl_s) if ttl_s is not None else CLAIM_TTL_S
    threshold = float(threshold_s) if threshold_s is not None else CLAIM_STALE_THRESHOLD_S
    if ttl <= 0 or threshold <= 0:
        raise GracefulRecoveryError("threshold_invalid", "the TTL and threshold must be positive")
    holder_pid = int(pid) if pid else os.getpid()
    now_epoch, stamp = _moment(now)
    with _locked(state_dir):
        existing = _read_claim(state_dir)
        if existing is None:
            return _write_claim(
                state_dir,
                owner=owner_text,
                ttl_s=ttl,
                pid=holder_pid,
                now_epoch=now_epoch,
                stamp=stamp,
                generation=1,
                took_over_from=None,
                previous=None,
            )
        if str(existing.get("owner") or "") == owner_text:
            return _write_claim(
                state_dir,
                owner=owner_text,
                ttl_s=ttl,
                pid=holder_pid,
                now_epoch=now_epoch,
                stamp=stamp,
                generation=int(existing.get("generation") or 1),
                took_over_from=existing.get("took_over_from"),
                previous=existing,
            )
        state = _holder_state(existing, now_epoch)
        if state == "live":
            renewed = existing.get("renewed_epoch") or existing.get("taken_epoch")
            age = now_epoch - float(renewed) if isinstance(renewed, (int, float)) else None
            if age is None or age < threshold:
                raise GracefulRecoveryError(
                    "claim_not_stale",
                    f"the claim of {existing.get('owner')!r} is not stale "
                    f"(age {age if age is not None else 'unknown'}s, threshold {threshold}s)",
                    _FIX_CLAIM,
                )
            reason = "stale_threshold"
        else:
            reason = state
        took_over_from = {
            "owner": str(existing.get("owner") or ""),
            "reason": reason,
            "threshold_s": threshold if reason == "stale_threshold" else None,
            "previous_taken_at": str(existing.get("taken_at") or ""),
        }
        return _write_claim(
            state_dir,
            owner=owner_text,
            ttl_s=ttl,
            pid=holder_pid,
            now_epoch=now_epoch,
            stamp=stamp,
            generation=int(existing.get("generation") or 0) + 1,
            took_over_from=took_over_from,
            previous=None,
        )


def claim_state(state_dir: Path, *, now: Any = None) -> dict[str, Any]:
    """Read-only report on the coordinator claim file."""

    now_epoch, stamp = _moment(now)
    existing = _read_claim(state_dir)
    if existing is None:
        return {
            "path": f"coordinator/{CLAIM_FILE}",
            "exists": False,
            "claim": None,
            "owner": "",
            "live": False,
            "holder_alive": None,
            "age_s": None,
            "expired": False,
            "stale": False,
            "at": stamp,
        }
    state = _holder_state(existing, now_epoch)
    renewed = existing.get("renewed_epoch") or existing.get("taken_epoch")
    age = now_epoch - float(renewed) if isinstance(renewed, (int, float)) else None
    return {
        "path": f"coordinator/{CLAIM_FILE}",
        "exists": True,
        "claim": dict(existing),
        "owner": str(existing.get("owner") or ""),
        "live": state == "live",
        "holder_alive": (
            _pid_alive(existing.get("pid")) if existing.get("pid") is not None else None
        ),
        "age_s": round(age, 3) if age is not None else None,
        "expired": state == "expired",
        "stale": bool(age is not None and age >= CLAIM_STALE_THRESHOLD_S),
        "at": stamp,
    }


def read_release_log(state_dir: Path) -> list[dict[str, Any]]:
    """The append-only log of claim releases (audit aid; malformed lines skipped)."""

    path = _release_log_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise GracefulRecoveryError(
            "release_log_unreadable", f"{CLAIM_RELEASE_LOG} cannot be read: {exc}", _FIX_CLAIM
        ) from exc
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# 5. The scheduler is un-poisoned through its own public API
# ---------------------------------------------------------------------------


def recover_scheduler_claim(
    state_dir: Path,
    work_id: Any,
    *,
    dead_owner: Any,
    death_evidence: Any = None,
    new_owner: Any = None,
    threshold_s: float | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Release a scheduler item a dead beat left claimed, then hand it over.

    This is the queue-side twin of :func:`release_claim_on_death`: a
    manual fire claimed a work item, died on a transport fault, and the
    item stayed claimed — poisoning the scheduler.  The item is released
    through :mod:`bundlebees_plugin.work_scheduler`'s public,
    owner-checked API only, and only once the dead owner's death is
    proven: the env-fault classification of the attempt, or the
    documented stale threshold measured from the item's claim stamp.
    """

    from . import work_scheduler as work_queue

    wid = _safe_id(work_id, "work_id")
    dead = _owner_id(dead_owner)
    threshold = float(threshold_s) if threshold_s is not None else CLAIM_STALE_THRESHOLD_S
    now_epoch, stamp = _moment(now)

    items = work_queue.list_items(state_dir)
    item = next((entry for entry in items if entry["work_id"] == wid), None)
    if item is None:
        raise GracefulRecoveryError(
            "work_not_found", f"no scheduler item {wid!r}", "List the queue and use an exact work_id."
        )
    if str(item.get("status") or "") != "claimed" or str(item.get("owner") or "") != dead:
        raise GracefulRecoveryError(
            "claim_not_held",
            f"item {wid!r} is {item.get('status')!r}, held by {item.get('owner')!r}",
            _FIX_CLAIM,
        )

    proof: str | None = None
    if isinstance(death_evidence, Mapping):
        if death_evidence.get("env_fault") is True:
            proof = "env_fault_evidence"
        elif death_evidence.get("dead") is True:
            proof = "death_evidence"
    if proof is None:
        claimed_at = _epoch_of(item.get("updated_at"))
        if claimed_at is not None and now_epoch - claimed_at >= threshold:
            proof = "stale_threshold"
    if proof is None:
        raise GracefulRecoveryError(
            "claim_release_refused",
            f"item {wid!r} is claimed by {dead!r}, who is not proven dead",
            "Pass the env-fault classification of the attempt that died, or wait "
            "for the documented stale threshold.",
        )

    work_queue.release(state_dir, wid, owner=dead, now=now_epoch)
    claimed_by = ""
    if new_owner not in (None, ""):
        takeover = _owner_id(new_owner)
        work_queue.claim(state_dir, wid, owner=takeover, now=now_epoch)
        claimed_by = takeover
    return {
        "released": True,
        "proof": proof,
        "work_id": wid,
        "released_owner": dead,
        "claimed_by": claimed_by,
        "checkpoint_kept": item.get("checkpoint") is not None,
        "at": stamp,
    }


def _epoch_of(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
