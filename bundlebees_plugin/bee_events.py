"""Durable landing-event outbox and file-subscriber delivery.

A bee "lands" when its attempt reaches a state transition that the coordinator
wants other local processes to observe.  This module is deliberately small and
filesystem-only: there is no broker, database, daemon or outbound network call.
The operator (or a later tick) calls :func:`deliver_pending`, which copies every
undelivered event into each subscriber's inbox.

Layout under ``state_dir``::

    events/outbox/<event_id>.json
    events/subscribers/<identity>.json
    events/inbox/<identity>/<event_id>.json
    events/acks/<identity>/<event_id>.json
    events/delivery/<identity>/<event_id>.json   # attempt counters

Every publish is create-only and fsynced (or atomically replaced where a replay
must replace an earlier delivery).  Delivery is at-least-once: an event is only
considered delivered after its inbox record is on disk, and a retry uses the
same deterministic ``event_id``/``delivery_id`` so a consumer can deduplicate.
Acknowledgements are durable, separate from delivery, and only an acknowledged
(or never-delivered) event may be replayed.

The CloudEvents payload never contains the state directory, a local absolute
path, a URL with credentials, or a detected secret.  :func:`validate_event`
additionally checks the whole envelope against the pinned workflow schema when
``WORKFLOW_SRC`` is configured; without it validation fails closed.

This module uses only the Python standard library at runtime.  If the optional
``jsonschema`` package is importable it is used for the pinned schema; otherwise
a conservative subset validator handles the keywords landing schemas use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .security import contains_address, looks_like_secret, scrub

__all__ = [
    "SCHEMA",
    "MAX_EVENT_BYTES",
    "EventError",
    "event_id_for",
    "build_event",
    "validate_event",
    "record_landing",
    "subscribe",
    "unsubscribe",
    "deliver_pending",
    "acknowledge",
    "replay",
    "pending_deliveries",
    "scan_pending",
    "recover_pending",
    "dedupe_by_event_id",
]

SCHEMA = "rizzstack.bee-landed/1"
DELIVERY_SCHEMA = "rizzstack.bee-delivery/1"
ACK_SCHEMA = "rizzstack.bee-ack/1"
SUBSCRIBER_SCHEMA = "rizzstack.bee-subscriber/1"

OUTBOX_DIR = "outbox"
SUBSCRIBERS_DIR = "subscribers"
INBOX_DIR = "inbox"
ACKS_DIR = "acks"
DELIVERY_DIR = "delivery"

MAX_EVENT_BYTES = 64 * 1024
MAX_IDENTIFIER = 128
_MAX_READ_BYTES = 2 * MAX_EVENT_BYTES

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIGEST_RE = re.compile(r"^(?:[0-9a-fA-F]{64})$")
_BACKEND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")
_PRIVATE_PATH_RE = re.compile(
    r"(?:^|[/\\])(?:home|Users|root|tmp|var|etc|private|proc|sys|dev)(?:[/\\]|$)"
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|passw|auth|api[_-]?key|private[_-]?key|credential)"
)

_FIX_EVENT = "Build the event with bee_events.build_event and record the exact object."
_FIX_SCHEMA = (
    "Check out the pinned workflow revision, set WORKFLOW_SRC, and run "
    "validate_event again; validation never falls back to a local guess."
)
_FIX_SUBSCRIBER = (
    "Subscribe with a safe identity and either an http(s) url (no credentials) "
    "or a file target."
)


class EventError(Exception):
    """A landing event, subscription, delivery or acknowledgement was refused.

    ``code`` is a stable machine string.  Error text is scrubbed before it is
    exposed, so a bad payload cannot echo a secret back to the caller.
    """

    def __init__(self, code: str, error: str, fix: str = "") -> None:
        self.code = str(code)
        self.error = str(scrub(str(error)))
        self.fix = str(scrub(str(fix))) if fix else ""
        super().__init__(f"{self.code}: {self.error}")

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.error, "fix": self.fix}


# ---------------------------------------------------------------------------
# Time and canonical bytes
# ---------------------------------------------------------------------------


def _iso(epoch: float | None = None, *, microseconds: bool = False) -> str:
    moment = datetime.fromtimestamp(
        time.time() if epoch is None else float(epoch), tz=timezone.utc
    )
    if not microseconds:
        moment = moment.replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def _clock(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise EventError("clock_invalid", "now must be epoch seconds or an ISO timestamp") from exc


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            .encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise EventError("event_invalid", "value is not JSON-serializable", _FIX_EVENT) from exc


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------


def _safe_text(value: Any, name: str, pattern: re.Pattern[str], *, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise EventError("event_invalid", f"{name} is required", _FIX_EVENT)
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_IDENTIFIER or not pattern.match(text):
        raise EventError(
            "event_invalid",
            f"{name} is not a safe identifier",
            "Use only letters, digits, dot, underscore, colon, at, plus or dash.",
        )
    return text


def _safe_identity(subscriber: Any) -> str:
    if isinstance(subscriber, Mapping):
        value = subscriber.get("identity")
    else:
        value = subscriber
    text = value.strip() if isinstance(value, str) else ""
    if not text or not _IDENTITY_RE.match(text):
        raise EventError(
            "subscriber_invalid",
            "subscriber identity is not a safe name",
            _FIX_SUBSCRIBER,
        )
    return text


def _safe_event_id(event_id: Any) -> str:
    text = event_id.strip() if isinstance(event_id, str) else ""
    if not text or not _EVENT_ID_RE.match(text):
        raise EventError("event_invalid", "event id is not a safe file name", _FIX_EVENT)
    return text


def _safe_digest(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise EventError("event_invalid", f"{name} is required", _FIX_EVENT)
        return None
    text = value.strip() if isinstance(value, str) else ""
    if not _DIGEST_RE.match(text):
        raise EventError(
            "event_invalid",
            f"{name} must be a 64-character hex SHA-256 digest",
            _FIX_EVENT,
        )
    return text.lower()


def _assert_no_private_text(value: Any, path: str = "data") -> None:
    """Refuse secrets, addresses, control characters and private paths."""

    if isinstance(value, str):
        if "\x00" in value or any(ord(ch) < 0x20 and ch not in "\t\n\r" for ch in value):
            raise EventError("event_invalid", f"{path} contains a control character", _FIX_EVENT)
        if len(value) > MAX_EVENT_BYTES:
            raise EventError("event_oversized", f"{path} is too large", _FIX_EVENT)
        if looks_like_secret(value):
            raise EventError("event_secret", f"{path} looks like a credential", _FIX_EVENT)
        if contains_address(value):
            raise EventError(
                "event_private",
                f"{path} contains a URL, host or address",
                "Pass a name or a repository-relative reference, never an endpoint.",
            )
        stripped = value.strip()
        if (
            stripped.startswith(("/", "~", "\\\\"))
            or _DRIVE_RE.match(stripped)
            or _PRIVATE_PATH_RE.search(stripped)
            or "/../" in stripped
            or stripped.startswith("../")
            or stripped == ".."
        ):
            raise EventError(
                "event_private",
                f"{path} contains a private or absolute path",
                "Pass a repository-relative reference only.",
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError("event_invalid", f"{path} has a non-string key", _FIX_EVENT)
            if _SENSITIVE_KEY_RE.search(key):
                raise EventError(
                    "event_secret",
                    f"{path}.{key} names a credential",
                    "Landing payloads carry identifiers and digests, never credentials.",
                )
            _assert_no_private_text(item, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_private_text(item, f"{path}[{index}]")


def _attempt_fields(
    attempt: Any, state: Any = None, transition_id: Any = None
) -> tuple[str, str, str]:
    if isinstance(attempt, Mapping):
        attempt_id = attempt.get("attempt_id") or attempt.get("id")
        state = attempt.get("state") if state is None else state
        transition_id = attempt.get("transition_id") if transition_id is None else transition_id
        if transition_id is None and isinstance(attempt.get("transition"), Mapping):
            transition_id = attempt["transition"].get("transition_id") or attempt["transition"].get("id")
    elif attempt is not None and not isinstance(attempt, (str, bytes)):
        attempt_id = getattr(attempt, "attempt_id", None) or getattr(attempt, "id", None)
        state = getattr(attempt, "state", None) if state is None else state
        transition_id = (
            getattr(attempt, "transition_id", None) if transition_id is None else transition_id
        )
    else:
        attempt_id = attempt
    return (
        _safe_text(attempt_id, "attempt_id", _ID_RE) or "",
        _safe_text(state, "state", _ID_RE) or "",
        _safe_text(transition_id, "transition_id", _ID_RE) or "",
    )


# ---------------------------------------------------------------------------
# Identity and CloudEvents construction
# ---------------------------------------------------------------------------


def event_id_for(attempt_id: str, state: str, transition_id: str) -> str:
    """Deterministic, filesystem-safe event identity.

    The same attempt/state/transition always maps to the same id, which is what
    makes recording and delivery idempotent across retries and crashes.
    """

    left = _safe_text(attempt_id, "attempt_id", _ID_RE)
    middle = _safe_text(state, "state", _ID_RE)
    right = _safe_text(transition_id, "transition_id", _ID_RE)
    seed = "\x00".join((left or "", middle or "", right or "")).encode("utf-8")
    return "evt_" + hashlib.sha256(seed).hexdigest()[:32]


def build_event(
    attempt: Any = None,
    feature_id: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    coordinator: str | None = None,
    lease_sha256: str | None = None,
    artifact_sha256: str | None = None,
    reference: Any = None,
    backend: str | None = None,
    *,
    state: str | None = None,
    transition_id: str | None = None,
    attempt_id: str | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Build a CloudEvents 1.0 landing event from coordinator facts.

    ``attempt`` may be a mapping/object (with ``attempt_id``, ``state`` and
    ``transition_id``) or an attempt id when ``state``/``transition_id`` are
    supplied explicitly.  Everything retained in ``data`` is scrubbed by
    refusal: no secret-shaped value, URL/host/address or private path is
    allowed.
    """

    if attempt is None and attempt_id is not None:
        attempt = attempt_id
    elif attempt is not None and attempt_id is not None:
        raise EventError("event_invalid", "pass attempt or attempt_id, not both", _FIX_EVENT)
    attempt_text, state_text, transition_text = _attempt_fields(attempt, state, transition_id)
    event_id = event_id_for(attempt_text, state_text, transition_text)

    feature = _safe_text(feature_id, "feature_id", _ID_RE, required=False)
    job = _safe_text(job_id, "job_id", _ID_RE, required=False)
    task = _safe_text(task_id, "task_id", _ID_RE, required=False)
    coord = _safe_text(coordinator, "coordinator", _ID_RE, required=False)
    lease = _safe_digest(lease_sha256, "lease_sha256")
    artifact = _safe_digest(artifact_sha256, "artifact_sha256")
    back = _safe_text(backend, "backend", _BACKEND_RE, required=False)

    if reference is not None and not isinstance(reference, (str, Mapping, list, tuple)):
        raise EventError("event_invalid", "reference must be text or a small object", _FIX_EVENT)
    _assert_no_private_text(reference, "reference")

    data: dict[str, Any] = {
        "schema": SCHEMA,
        "attempt_id": attempt_text,
        "state": state_text,
        "transition_id": transition_text,
        "feature_id": feature,
        "job_id": job,
        "task_id": task,
        "coordinator": coord,
        "lease_sha256": lease,
        "artifact_sha256": artifact,
        "reference": reference,
        "backend": back,
    }
    _assert_no_private_text(data, "data")

    event: dict[str, Any] = {
        "specversion": "1.0",
        "id": event_id,
        "source": "rizzstack://bundlebees",
        "type": SCHEMA,
        "subject": f"attempt/{attempt_text}",
        "time": _iso(_clock(now)),
        "datacontenttype": "application/json",
        "data": data,
    }
    _validate_event_local(event)
    return event


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _event_mapping(event: Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        return dict(event)
    if isinstance(event, bytes):
        try:
            event = event.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EventError("event_invalid", "event is not UTF-8 JSON", _FIX_EVENT) from exc
    if isinstance(event, str):
        try:
            loaded = json.loads(event)
        except json.JSONDecodeError as exc:
            raise EventError("event_invalid", "event is not JSON", _FIX_EVENT) from exc
        if not isinstance(loaded, Mapping):
            raise EventError("event_invalid", "event must be a JSON object", _FIX_EVENT)
        return dict(loaded)
    raise EventError("event_invalid", "event must be a mapping or JSON object", _FIX_EVENT)


def _validate_source(source: Any) -> None:
    if not isinstance(source, str) or not source.strip() or len(source) > 256:
        raise EventError("event_invalid", "source is required and must be short text", _FIX_EVENT)
    if "\x00" in source or any(ord(ch) < 0x20 for ch in source):
        raise EventError("event_invalid", "source contains a control character", _FIX_EVENT)
    if looks_like_secret(source):
        raise EventError("event_secret", "source looks like a credential", _FIX_EVENT)
    if source == "rizzstack://bundlebees":
        return
    if "@" in source or contains_address(source):
        raise EventError(
            "event_private",
            "source contains an endpoint or embedded credentials",
            "Use the bundlebees source URI emitted by build_event.",
        )
    if _PRIVATE_PATH_RE.search(source) or _DRIVE_RE.match(source) or "/../" in source:
        raise EventError("event_private", "source contains a private path", _FIX_EVENT)


def _validate_event_time(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise EventError("event_invalid", "time must be an ISO-8601 timestamp", _FIX_EVENT)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventError("event_invalid", "time must be an ISO-8601 timestamp", _FIX_EVENT) from exc


def _validate_event_local(event: Any) -> dict[str, Any]:
    value = _event_mapping(event)
    payload = _canonical(value)
    if len(payload) > MAX_EVENT_BYTES:
        raise EventError(
            "event_oversized",
            f"event is {len(payload)} bytes; the limit is {MAX_EVENT_BYTES}",
            "Keep free-form references and notes small; the outbox is metadata, not a log.",
        )

    if value.get("specversion") != "1.0":
        raise EventError("event_invalid", "specversion must be 1.0", _FIX_EVENT)
    if value.get("type") != SCHEMA:
        raise EventError("event_invalid", f"type must be {SCHEMA}", _FIX_EVENT)
    source = value.get("source")
    _validate_source(source)
    _safe_event_id(value.get("id"))
    if value.get("datacontenttype") not in (None, "application/json"):
        raise EventError("event_invalid", "datacontenttype must be application/json", _FIX_EVENT)
    _validate_event_time(value.get("time"))

    data = value.get("data")
    if not isinstance(data, Mapping):
        raise EventError("event_invalid", "data must be an object", _FIX_EVENT)
    if data.get("schema") != SCHEMA:
        raise EventError("event_invalid", f"data.schema must be {SCHEMA}", _FIX_EVENT)

    attempt, state_text, transition = _attempt_fields(
        data.get("attempt_id"), data.get("state"), data.get("transition_id")
    )
    expected = event_id_for(attempt, state_text, transition)
    if value.get("id") != expected:
        raise EventError(
            "event_id_mismatch",
            "event id does not match attempt/state/transition",
            "Do not edit an event id; rebuild the event.",
        )
    if data.get("event_id") not in (None, expected):
        raise EventError("event_id_mismatch", "data.event_id does not match the envelope id", _FIX_EVENT)

    for name in ("feature_id", "job_id", "task_id", "coordinator", "backend"):
        item = data.get(name)
        if item is not None and not isinstance(item, str):
            raise EventError("event_invalid", f"data.{name} must be text or null", _FIX_EVENT)
    _safe_digest(data.get("lease_sha256"), "lease_sha256")
    _safe_digest(data.get("artifact_sha256"), "artifact_sha256")

    subject = value.get("subject")
    if subject is not None and (not isinstance(subject, str) or len(subject) > 256):
        raise EventError("event_invalid", "subject is not safe text", _FIX_EVENT)

    _assert_no_private_text(data, "data")
    for key, item in value.items():
        if key in ("source", "data"):
            continue
        _assert_no_private_text(item, str(key))
    return value


def _workflow_schema_path() -> Path:
    raw = os.environ.get("WORKFLOW_SRC", "")
    if not raw:
        raise EventError(
            "workflow_schema_unavailable",
            "WORKFLOW_SRC is not set",
            _FIX_SCHEMA,
        )
    root = Path(raw).expanduser()
    names = (
        "bee-landed.schema.json",
        "bee-landed-event.schema.json",
        "bee_landed.schema.json",
        "bee-events.schema.json",
        "landing-event.schema.json",
        "landing.schema.json",
        "bee-event.schema.json",
        "event.schema.json",
        "lifecycle.schema.json",
    )
    candidates = [root / "schemas" / name for name in names]
    candidates += [root / "schema" / name for name in names]
    candidates += [root / "observability" / "schemas" / name for name in names]
    for candidate in candidates:
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    raise EventError(
        "workflow_schema_unavailable",
        "the pinned workflow checkout has no bee-landed schema",
        _FIX_SCHEMA,
    )


def _load_json_file(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EventError("workflow_schema_unavailable", f"cannot read {path.name}", _FIX_SCHEMA) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventError("workflow_schema_unavailable", f"{path.name} is not JSON", _FIX_SCHEMA) from exc


def _resolve_ref(root: dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise EventError("workflow_schema_unavailable", "external $ref is unsupported", _FIX_SCHEMA)
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            raise EventError("workflow_schema_unavailable", "schema $ref does not resolve", _FIX_SCHEMA)
        node = node[part]
    return node


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _schema_errors(value: Any, schema: Any, root: dict[str, Any], path: str = "") -> list[str]:
    """Small draft-2020-12 subset validator (stdlib fallback)."""

    if schema is True:
        return []
    if schema is False:
        return [f"{path or 'event'} is not allowed"]
    if not isinstance(schema, Mapping):
        return []

    if "$ref" in schema:
        try:
            target = _resolve_ref(root, str(schema["$ref"]))
        except EventError as exc:
            return [str(exc)]
        return _schema_errors(value, target, root, path)

    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_ok(value, expected):
        return [f"{path or 'event'} must be {expected}"]
    if isinstance(expected, list) and not any(
        isinstance(item, str) and _type_ok(value, item) for item in expected
    ):
        return [f"{path or 'event'} must be one of {expected}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path or 'event'} must equal {schema['const']!r}")
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path or 'event'} is not an allowed value")

    if isinstance(value, Mapping):
        required = schema.get("required") or []
        for name in required if isinstance(required, list) else []:
            if name not in value:
                errors.append(f"{path or 'event'} is missing {name}")
        properties = schema.get("properties") or {}
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                if name in value:
                    child_errors = _schema_errors(value[name], child, root, f"{path}.{name}" if path else str(name))
                    errors.extend(child_errors)
        additional = schema.get("additionalProperties", True)
        known = set(properties) if isinstance(properties, Mapping) else set()
        patterns = schema.get("patternProperties") or {}
        for name, item in value.items():
            if name in known:
                continue
            matched = False
            if isinstance(patterns, Mapping):
                for pattern, child in patterns.items():
                    try:
                        if re.search(pattern, str(name)):
                            matched = True
                            errors.extend(
                                _schema_errors(item, child, root, f"{path}.{name}" if path else str(name))
                            )
                    except re.error:
                        return [f"{path or 'event'} has an invalid schema pattern"]
            if not matched and additional is False:
                errors.append(f"{path or 'event'} has an unexpected field {name}")
            elif not matched and isinstance(additional, Mapping):
                errors.extend(
                    _schema_errors(item, additional, root, f"{path}.{name}" if path else str(name))
                )
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            errors.append(f"{path or 'event'} has too few fields")
        if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
            errors.append(f"{path or 'event'} has too many fields")

    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{path or 'event'} has too few items")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{path or 'event'} has too many items")
        if schema.get("uniqueItems") is True:
            seen: list[str] = []
            for item in value:
                marker = json.dumps(item, sort_keys=True, default=str)
                if marker in seen:
                    errors.append(f"{path or 'event'} has duplicate items")
                    break
                seen.append(marker)
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, items, root, f"{path}[{index}]" if path else f"[{index}]"))

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{path or 'event'} is too short")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{path or 'event'} is too long")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    errors.append(f"{path or 'event'} does not match the required pattern")
            except re.error:
                return [f"{path or 'event'} has an invalid schema pattern"]

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            errors.append(f"{path or 'event'} is below the minimum")
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            errors.append(f"{path or 'event'} is above the maximum")
        if isinstance(schema.get("exclusiveMinimum"), (int, float)) and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path or 'event'} is not above the exclusive minimum")
        if isinstance(schema.get("exclusiveMaximum"), (int, float)) and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path or 'event'} is not below the exclusive maximum")

    for keyword in ("allOf",):
        children = schema.get(keyword)
        if isinstance(children, list):
            for child in children:
                errors.extend(_schema_errors(value, child, root, path))
    if isinstance(schema.get("anyOf"), list):
        branches = [_schema_errors(value, child, root, path) for child in schema["anyOf"]]
        if all(branch for branch in branches):
            errors.append(f"{path or 'event'} does not match any allowed shape")
    if isinstance(schema.get("oneOf"), list):
        branches = [_schema_errors(value, child, root, path) for child in schema["oneOf"]]
        if sum(1 for branch in branches if not branch) != 1:
            errors.append(f"{path or 'event'} must match exactly one allowed shape")
    if "not" in schema and not _schema_errors(value, schema["not"], root, path):
        errors.append(f"{path or 'event'} matches a forbidden shape")
    return errors


def validate_event(event: Any) -> bool:
    """Validate an event against the pinned workflow schema.

    Fails closed when ``WORKFLOW_SRC`` is unset, unreadable, or does not contain
    the bee-landed schema.  Callers that only need local durability can use
    :func:`record_landing`, which performs the structural/security checks but
    does not pretend to have the pinned validator.
    """

    value = _validate_event_local(event)
    schema_path = _workflow_schema_path()
    schema = _load_json_file(schema_path)
    if not isinstance(schema, Mapping):
        raise EventError("workflow_schema_unavailable", "schema must be a JSON object", _FIX_SCHEMA)

    try:
        import jsonschema  # type: ignore
    except Exception:  # noqa: BLE001 - optional dependency; stdlib fallback below
        jsonschema = None

    if jsonschema is not None:
        try:
            validator_cls = jsonschema.validators.validator_for(schema)
            validator_cls.check_schema(schema)
            errors = sorted(validator_cls(schema).iter_errors(value), key=lambda item: list(item.path))
        except Exception as exc:  # noqa: BLE001 - a broken pinned schema is a refusal
            raise EventError(
                "workflow_schema_unavailable",
                f"the pinned schema cannot be loaded: {type(exc).__name__}",
                _FIX_SCHEMA,
            ) from exc
        if errors:
            first = errors[0]
            where = ".".join(str(part) for part in first.path) or "event"
            # Do not echo the instance value: a worker-supplied field may be a
            # credential or a private path that the schema happened to reject.
            raise EventError(
                "event_schema_invalid",
                f"{where} does not satisfy the pinned workflow schema",
                "Fix the event to satisfy the pinned workflow schema.",
            )
    else:
        problems = _schema_errors(value, schema, schema)
        if problems:
            raise EventError(
                "event_schema_invalid",
                problems[0],
                "Fix the event to satisfy the pinned workflow schema.",
            )
    return True


# ---------------------------------------------------------------------------
# Durable file helpers
# ---------------------------------------------------------------------------


def _events_dir(state_dir: Any) -> Path:
    return Path(state_dir) / "events"


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_regular(path: Path, *, limit: int = _MAX_READ_BYTES) -> bytes | None:
    """Read a regular non-symlink file, or return None when it does not exist."""

    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EventError(
            "event_write_failed",
            f"cannot read {path.name}: {type(exc).__name__}",
            "Remove the unsafe file and retry from a healthy state directory.",
        ) from exc
    try:
        info = os.fstat(fd)
        if not (info.st_mode & 0o170000) == 0o100000:
            raise EventError("event_invalid", f"{path.name} is not a regular file", _FIX_EVENT)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise EventError("event_oversized", f"{path.name} is too large", _FIX_EVENT)
        return raw
    finally:
        os.close(fd)


def _create_only_payload(path: Path, payload: bytes) -> bool:
    """Publish *payload* at *path* once; fsynced before and after the link."""

    _ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        _fsync_dir(path.parent)
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _replace_payload(path: Path, payload: bytes) -> None:
    """Atomically replace *path*; used only for replaying a delivery."""

    _ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
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

    Identical bytes are idempotent.  A different object at the same path is a
    conflict and is never overwritten.
    """

    payload = _canonical(value)
    existing = _read_regular(path)
    if existing is not None:
        if existing == payload:
            return _decode(existing, path), False
        raise EventError(
            "event_conflict",
            f"{path.name} already holds different bytes",
            "Keep the original event/subscriber record; do not rewrite durable events.",
        )
    created = _create_only_payload(path, payload)
    raw = _read_regular(path)
    if raw is None:
        raise EventError("event_write_failed", f"{path.name} was not published", "Retry the operation.")
    if raw != payload:
        raise EventError(
            "event_conflict",
            f"{path.name} changed during publication",
            "Inspect the state directory; another writer produced a different record.",
        )
    return _decode(raw, path), created


def _decode(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventError("event_invalid", f"{path.name} is not JSON", _FIX_EVENT) from exc
    if not isinstance(value, Mapping):
        raise EventError("event_invalid", f"{path.name} is not a JSON object", _FIX_EVENT)
    return dict(value)


def _replace_json(path: Path, value: Mapping[str, Any]) -> dict[str, Any]:
    payload = _canonical(value)
    _replace_payload(path, payload)
    return dict(value)


def _remove_file(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise EventError("event_write_failed", f"cannot remove {path.name}", "Retry the operation.") from exc
    _fsync_dir(path.parent)
    return True


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


def _outbox_path(state_dir: Any, event_id: str) -> Path:
    return _events_dir(state_dir) / OUTBOX_DIR / f"{_safe_event_id(event_id)}.json"


def record_landing(state_dir: Any, event: Any) -> dict[str, Any]:
    """Persist one landing event in the create-only, fsynced outbox.

    Returns the stored event plus ``created`` (False when the identical event
    was already present).  A different event under the same id is refused.
    """

    value = _validate_event_local(event)
    event_id = _safe_event_id(value.get("id"))
    path = _outbox_path(state_dir, event_id)
    try:
        record, created = _create_only_json(path, value)
    except EventError:
        raise
    except OSError as exc:
        raise EventError(
            "event_write_failed",
            f"outbox write failed: {type(exc).__name__}",
            "Check the state directory is writable and retry.",
        ) from exc
    if record.get("id") != event_id:
        raise EventError("event_conflict", "stored event id does not match its path", "Inspect the outbox.")
    return {**record, "created": created}


def _iter_outbox(state_dir: Any) -> list[tuple[str, dict[str, Any]]]:
    directory = _events_dir(state_dir) / OUTBOX_DIR
    if not directory.is_dir():
        return []
    events: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or path.suffix != ".json":
            continue
        try:
            raw = _read_regular(path)
        except EventError:
            continue
        if raw is None:
            continue
        try:
            value = _validate_event_local(_decode(raw, path))
        except EventError:
            continue
        events.append((_safe_event_id(value["id"]), value))
    return events


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------


def _subscriber_path(state_dir: Any, identity: str) -> Path:
    return _events_dir(state_dir) / SUBSCRIBERS_DIR / f"{_safe_identity(identity)}.json"


def _normalize_subscriber(subscriber: Any) -> dict[str, Any]:
    if not isinstance(subscriber, Mapping):
        raise EventError("subscriber_invalid", "subscriber must be an object", _FIX_SUBSCRIBER)
    identity = _safe_identity(subscriber)
    url = subscriber.get("url")
    file_target = subscriber.get("file")
    session = subscriber.get("session")

    if (url is None) == (file_target is None):
        raise EventError(
            "subscriber_invalid",
            "subscriber needs exactly one of url or file",
            _FIX_SUBSCRIBER,
        )
    if url is not None:
        if not isinstance(url, str):
            raise EventError("subscriber_invalid", "url must be text", _FIX_SUBSCRIBER)
        text = url.strip()
        if len(text) > 2048 or not re.match(r"^https?://", text, re.IGNORECASE):
            raise EventError("subscriber_invalid", "url must be http(s)", _FIX_SUBSCRIBER)
        # A URL carrying userinfo is a credential in the subscription record.
        if re.match(r"^https?://[^/]*@", text, re.IGNORECASE):
            raise EventError("subscriber_invalid", "url must not embed credentials", _FIX_SUBSCRIBER)
        if looks_like_secret(text):
            raise EventError("subscriber_invalid", "url looks like a credential", _FIX_SUBSCRIBER)
        return {
            "schema": SUBSCRIBER_SCHEMA,
            "identity": identity,
            "url": text,
            "file": None,
            "session": _optional_session(session),
        }
    if not isinstance(file_target, str) or not file_target.strip():
        raise EventError("subscriber_invalid", "file must be non-empty text", _FIX_SUBSCRIBER)
    if "\x00" in file_target or len(file_target) > 2048 or looks_like_secret(file_target):
        raise EventError("subscriber_invalid", "file target is not safe", _FIX_SUBSCRIBER)
    return {
        "schema": SUBSCRIBER_SCHEMA,
        "identity": identity,
        "url": None,
        "file": file_target,
        "session": _optional_session(session),
    }


def _optional_session(session: Any) -> str | None:
    if session is None:
        return None
    text = session.strip() if isinstance(session, str) else ""
    if not text or len(text) > MAX_IDENTIFIER or not _ID_RE.match(text):
        raise EventError("subscriber_invalid", "session is not a safe identifier", _FIX_SUBSCRIBER)
    return text


def subscribe(state_dir: Any, subscriber: Mapping[str, Any]) -> dict[str, Any]:
    """Record a subscriber durably; identical re-subscription is idempotent."""

    value = _normalize_subscriber(subscriber)
    path = _subscriber_path(state_dir, value["identity"])
    existing = _read_regular(path)
    if existing is not None:
        record = _decode(existing, path)
        comparable = {key: record.get(key) for key in ("identity", "url", "file", "session")}
        wanted = {key: value[key] for key in ("identity", "url", "file", "session")}
        if comparable != wanted:
            raise EventError(
                "subscriber_conflict",
                f"subscriber {value['identity']} is already subscribed with different settings",
                "Unsubscribe first if the target must change.",
            )
        return {**record, "created": False}
    value["subscribed_at"] = _iso(_clock(None), microseconds=True)
    record, created = _create_only_json(path, value)
    return {**record, "created": created}


def unsubscribe(state_dir: Any, subscriber: Any) -> bool:
    """Remove one subscription; missing is not an error."""

    identity = _safe_identity(subscriber)
    return _remove_file(_subscriber_path(state_dir, identity))


def _iter_subscribers(state_dir: Any) -> list[dict[str, Any]]:
    directory = _events_dir(state_dir) / SUBSCRIBERS_DIR
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or path.suffix != ".json":
            continue
        raw = _read_regular(path)
        if raw is None:
            continue
        try:
            record = _decode(raw, path)
            if _safe_identity(record) != path.stem:
                continue
        except EventError:
            continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _delivery_id(identity: str, event_id: str) -> str:
    seed = f"{identity}\x00{event_id}".encode("utf-8")
    return "dlv_" + hashlib.sha256(seed).hexdigest()[:32]


def _delivery_path(state_dir: Any, identity: str, event_id: str) -> Path:
    return _events_dir(state_dir) / INBOX_DIR / identity / f"{_safe_event_id(event_id)}.json"


def _ack_path(state_dir: Any, identity: str, event_id: str) -> Path:
    return _events_dir(state_dir) / ACKS_DIR / identity / f"{_safe_event_id(event_id)}.json"


def _attempt_path(state_dir: Any, identity: str, event_id: str) -> Path:
    return _events_dir(state_dir) / DELIVERY_DIR / identity / f"{_safe_event_id(event_id)}.json"


def _next_attempt(state_dir: Any, identity: str, event_id: str, *, base: int = 0) -> int:
    path = _attempt_path(state_dir, identity, event_id)
    current = base
    if path.exists():
        try:
            existing = _decode(_read_regular(path) or b"{}", path)
            current = max(current, int(existing.get("attempts") or 0))
        except (EventError, TypeError, ValueError):
            current = max(current, base)
    attempts = current + 1
    _replace_json(
        path,
        {
            "schema": "rizzstack.bee-delivery-attempts/1",
            "event_id": event_id,
            "subscriber": identity,
            "attempts": attempts,
            "last_attempt_at": _iso(time.time(), microseconds=True),
        },
    )
    return attempts


def _delivery_record(
    identity: str, event: Mapping[str, Any], attempts: int, *, at: str | None = None
) -> dict[str, Any]:
    event_id = _safe_event_id(event.get("id"))
    return {
        "schema": DELIVERY_SCHEMA,
        "delivery_id": _delivery_id(identity, event_id),
        "event_id": event_id,
        "subscriber": identity,
        "attempts": max(1, int(attempts)),
        "delivered_at": at or _iso(time.time(), microseconds=True),
        "event_sha256": _digest_bytes(_canonical(event)),
        "event": dict(event),
    }


def _publish_delivery(
    state_dir: Any, identity: str, event: Mapping[str, Any], *, replace: bool = False
) -> dict[str, Any]:
    event_id = _safe_event_id(event.get("id"))
    path = _delivery_path(state_dir, identity, event_id)
    existing = _read_regular(path)
    if existing is not None and not replace:
        return _decode(existing, path)

    base = 0
    if existing is not None:
        try:
            base = int(_decode(existing, path).get("attempts") or 0)
        except (EventError, TypeError, ValueError):
            base = 0
    attempts = _next_attempt(state_dir, identity, event_id, base=base)
    record = _delivery_record(identity, event, attempts)
    if existing is not None and replace:
        _replace_json(path, record)
        return record
    stored, _created = _create_only_json(path, record)
    return stored


def _delivery_exists(state_dir: Any, identity: str, event_id: str) -> bool:
    return _read_regular(_delivery_path(state_dir, identity, event_id)) is not None


def _ack_exists(state_dir: Any, identity: str, event_id: str) -> bool:
    return _read_regular(_ack_path(state_dir, identity, event_id)) is not None


def pending_deliveries(state_dir: Any, subscriber: Any = None) -> list[dict[str, str]]:
    """Crash-recovery scan: outbox events with no inbox record for a subscriber.

    This is read-only and is the same predicate :func:`deliver_pending` uses, so
    an operator can inspect what a restart will recover.
    """

    events = _iter_outbox(state_dir)
    if subscriber is None:
        subscribers = [_safe_identity(item) for item in _iter_subscribers(state_dir)]
    else:
        subscribers = [_safe_identity(subscriber)]
    pending: list[dict[str, str]] = []
    for identity in subscribers:
        for event_id, _event in events:
            if not _delivery_exists(state_dir, identity, event_id):
                pending.append({"subscriber": identity, "event_id": event_id})
    return pending


def deliver_pending(state_dir: Any, subscriber: Any = None) -> dict[str, Any]:
    """Copy every undelivered outbox event into subscriber inboxes.

    Delivery is at-least-once.  A failed write leaves the event pending and the
    next call retries it with the same ids; an existing delivery is never
    rewritten (except through :func:`replay`).
    """

    events = _iter_outbox(state_dir)
    if subscriber is None:
        subscribers = [_safe_identity(item) for item in _iter_subscribers(state_dir)]
    else:
        subscribers = [_safe_identity(subscriber)]

    delivered: list[dict[str, Any]] = []
    skipped = 0
    failures: list[dict[str, str]] = []
    for identity in subscribers:
        for event_id, event in events:
            if _delivery_exists(state_dir, identity, event_id):
                skipped += 1
                continue
            try:
                record = _publish_delivery(state_dir, identity, event)
            except Exception as exc:  # noqa: BLE001 - one subscriber must not stop the scan
                failures.append(
                    {
                        "subscriber": identity,
                        "event_id": event_id,
                        "error": str(getattr(exc, "code", "delivery_failed")),
                    }
                )
                continue
            delivered.append(record)

    return {
        "delivered": len(delivered),
        "skipped": skipped,
        "failed": len(failures),
        "pending": len(pending_deliveries(state_dir, subscriber)),
        "deliveries": delivered,
        "failures": failures,
    }


def recover_pending(state_dir: Any, subscriber: Any = None) -> dict[str, Any]:
    """Alias for the crash-recovery scan plus delivery pass."""

    return deliver_pending(state_dir, subscriber)


#: Compatibility alias for callers that name the crash-recovery scan explicitly.
scan_pending = pending_deliveries


def dedupe_by_event_id(items: Iterable[Any]) -> list[Any]:
    """Return *items* once per event id, preserving the first occurrence.

    Items may be event mappings or bare event ids.  This mirrors the identity
    used by the outbox and inbox paths, so callers do not have to invent a
    second deduplication rule.
    """

    seen: set[str] = set()
    unique: list[Any] = []
    for item in items:
        event_id = item.get("id") if isinstance(item, Mapping) else item
        identifier = _safe_event_id(event_id)
        if identifier in seen:
            continue
        seen.add(identifier)
        unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# Acknowledgement and replay
# ---------------------------------------------------------------------------


def acknowledge(
    state_dir: Any, subscriber: Any, event_id: str, *, now: Any = None
) -> dict[str, Any]:
    """Record a durable ack for an already-delivered event."""

    identity = _safe_identity(subscriber)
    identifier = _safe_event_id(event_id)
    delivery_raw = _read_regular(_delivery_path(state_dir, identity, identifier))
    if delivery_raw is None:
        raise EventError(
            "delivery_missing",
            f"{identity} has no delivery for {identifier}",
            "Deliver the event first; an ack can never precede a delivery.",
        )
    delivery = _decode(delivery_raw, _delivery_path(state_dir, identity, identifier))
    path = _ack_path(state_dir, identity, identifier)
    existing = _read_regular(path)
    if existing is not None:
        record = _decode(existing, path)
        return {**record, "created": False}
    record = {
        "schema": ACK_SCHEMA,
        "event_id": identifier,
        "subscriber": identity,
        "delivery_id": delivery.get("delivery_id") or _delivery_id(identity, identifier),
        "event_sha256": delivery.get("event_sha256"),
        "acknowledged_at": _iso(_clock(now), microseconds=True),
    }
    stored, created = _create_only_json(path, record)
    return {**stored, "created": created}


def replay(
    state_dir: Any, event_id: str, subscriber: Any, *, now: Any = None
) -> dict[str, Any]:
    """Re-deliver an event only when its delivery is acked or missing.

    An outstanding (delivered but unacknowledged) inbox record is refused, so a
    replay cannot silently create a duplicate race for a consumer that is still
    processing the first copy.  A successful replay clears the ack; the consumer
    must acknowledge the new delivery again.
    """

    identity = _safe_identity(subscriber)
    identifier = _safe_event_id(event_id)
    event = None
    for candidate_id, candidate in _iter_outbox(state_dir):
        if candidate_id == identifier:
            event = candidate
            break
    if event is None:
        raise EventError(
            "event_missing",
            f"the outbox has no event {identifier}",
            "Replay only an event that was recorded durably.",
        )

    delivery_raw = _read_regular(_delivery_path(state_dir, identity, identifier))
    acked = _ack_exists(state_dir, identity, identifier)
    if delivery_raw is not None and not acked:
        raise EventError(
            "replay_refused",
            f"{identity} has an unacknowledged delivery for {identifier}",
            "Acknowledge the existing delivery before replaying it.",
        )

    _remove_file(_ack_path(state_dir, identity, identifier))
    record = _publish_delivery(state_dir, identity, event, replace=delivery_raw is not None)
    if now is not None:
        # Make replay timestamps deterministic for callers that pass a clock.
        record = {**record, "delivered_at": _iso(_clock(now), microseconds=True)}
        _replace_json(_delivery_path(state_dir, identity, identifier), record)
    return record
