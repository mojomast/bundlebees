"""Out-of-band decision consumer for durable agent work items (M4.3 lane A).

This is a separate process, not an inference call inside the queue tick::

    python -m bundlebees_plugin.agent_consumer --state DIR --consumer NAME \
        [--adapter hermes|deterministic] [--once | --interval N]

One invocation claims at most one item and then:

1. if a canonical decision already exists, completes the item idempotently;
2. else if a proposal is already persisted, asks
   ``hermes_adapter.adopt_proposal`` to compile it — the model is never asked
   again for a request that already has an answer;
3. else invokes the selected adapter (Hermes or the deterministic adapter).

A failure releases the item for one bounded retry; the second failed attempt
refuses it (``MAX_ATTEMPTS`` claims in total). The consumer never dispatches a
bee and never touches a job's task state; applying a decision stays with the
mechanical reconciler.

Test-only crash boundary
------------------------
When ``BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL=1`` is set in the consumer, the
Hermes path installs a guard around ``recovery.record_decision``. The guard runs
after ``decide_with_hermes`` has persisted the proposal and immediately before
the decision would be recorded: it writes a fsynced marker under ``recovery/``
and exits with status 97. A restart therefore sees a proposal with no decision,
adopts it, and never re-invokes the model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import agent_work, hermes_adapter, job as jobs, recovery
from . import bee_events, leases

CRASH_ENV = "BUNDLEBEES_AGENT_CRASH_AFTER_PROPOSAL"
CRASH_EXIT_CODE = 97
CRASH_SCHEMA = "rizzstack.agent-crash/1"

ADAPTERS: tuple[str, ...] = ("hermes", "deterministic", "authorized")

# ---------------------------------------------------------------------------
# Tool-path write-back
# ---------------------------------------------------------------------------
#
# Attempts dispatched through the tool path (``bundlebees_dispatch``) are
# completed on the launcher, never inside a job: nothing in the job lifecycle
# ever emits their landing event, so the attempt store keeps dispatch-time
# states forever, the usage ingest never sees them, and the reconciliation
# sweep cannot tell completed from unpushed. When such an attempt reaches a
# terminal state, the tool path writes the same canonical
# ``rizzstack.bee.landed.v1`` event through the same outbox write path —
# attempt id, state, feature/branch and the usage payload the result envelope
# carries. The write is best-effort by contract: a failure is reported as a
# value and never raised into the attempt's completion path.

#: The terminal attempt states the write-back emits. ``landed`` is the
#: launcher's own completion word; the job path maps terminal task states
#: instead, which is why tool-path attempts were invisible.
TOOL_TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted"}
)

#: Stable per-(attempt, state) transition id: the deterministic event id makes
#: replays and crash retries of the same completion idempotent.
TOOL_TRANSITION_ID = "tool-write-back"

TOOL_COMPLETION_SCHEMA = "rizzstack.tool-completion/1"

_TOOL_IDENTITY = "bundlebees-tools"
_TOOL_HARNESS = "hyvv"
_TOOL_COMPLETIONS_DIR = "tool-completions"
_TOOL_COMPLETION_MAX_BYTES = 65536

_ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX64_RE = re.compile(r"^(?:[0-9a-fA-F]{64})$")
_USAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _tool_id(value: Any) -> str | None:
    """A canonical landing identifier, or None when the value is unsafe."""

    text = str(value if value is not None else "").strip()
    if text and bee_events._ID_RE.match(text):
        return text
    return None


def _tool_clock(now: Any) -> float:
    """The caller's clock, tolerantly; a bad clock must not stop the write."""

    if now is None:
        return time.time()
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return float(now)
    if callable(now):
        try:
            return _tool_clock(now())
        except Exception:  # noqa: BLE001 - best-effort clock
            return time.time()
    try:
        return datetime.fromisoformat(str(now).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _clean_usage(usage: Any) -> dict[str, Any]:
    """The result envelope's usage, numbers only; never a credential shape."""

    if not isinstance(usage, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, value in usage.items():
        name = str(key or "").strip()
        if not _USAGE_KEY_RE.fullmatch(name):
            continue
        if name == "source":
            if isinstance(value, str) and value.strip():
                clean[name] = value.strip()[:40]
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            clean[name] = value
    return clean


def _lease_facts(state_dir: Path, attempt_id: str) -> dict[str, Any]:
    """The coordinator lease ``bundlebees_dispatch`` recorded, best-effort.

    A missing, tampered or unreadable lease is not an error here: the caller
    may carry the facts itself. It only means the write-back cannot fill them.
    """

    try:
        record = leases.load_lease(state_dir, attempt_id)
    except Exception:  # noqa: BLE001 - an unusable lease is only missing facts
        return {}
    return record if isinstance(record, Mapping) else {}


def _stored_outbox_event(state_path: Path, event_id: str) -> dict[str, Any] | None:
    """The outbox record an event id already holds, best-effort."""

    try:
        raw = bee_events._read_regular(bee_events._outbox_path(state_path, event_id))
    except Exception:  # noqa: BLE001 - an unreadable outbox is only a refusal
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _replay_matches(stored: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    """Whether a stored outbox event is this same transition replayed.

    The only legitimate differences between two observations of the same
    ``(attempt, state, transition)`` are the envelope ``time`` (the outbox
    keeps the first-seen time) and ``data.result`` evidence an earlier
    observation fetched (the artifact digest) while the replay did not. Any
    other difference means the id holds different facts.
    """

    if set(stored) - {"time"} != set(event) - {"time"}:
        return False
    for key, value in event.items():
        if key == "time":
            continue
        if key == "data":
            if not _data_replay_matches(stored.get("data"), value):
                return False
            continue
        if stored.get(key) != value:
            return False
    return True


def _data_replay_matches(stored_data: Any, event_data: Any) -> bool:
    if not isinstance(stored_data, Mapping) or not isinstance(event_data, Mapping):
        return stored_data == event_data
    if set(stored_data) - {"result"} != set(event_data) - {"result"}:
        return False
    for key, value in event_data.items():
        if key == "result":
            continue
        if stored_data.get(key) != value:
            return False
    stored_result = stored_data.get("result")
    event_result = event_data.get("result")
    if not isinstance(stored_result, Mapping) or not isinstance(event_result, Mapping):
        return stored_result == event_result
    # The stored result may carry more evidence than the replay; the replay
    # must not contradict anything the stored result asserts.
    return all(stored_result.get(key) == value for key, value in event_result.items())


def _read_completion_record_fd(dir_fd: int, attempt: str) -> bytes | None:
    """Read ``<attempt>.json`` relative to an already-guarded directory fd.

    The record must be a regular, single-link file of at most 65536 bytes: a
    hardlink is not evidence this writer produced the record.  The open is
    non-blocking so a FIFO or other special file can never wedge the caller.
    Any refusal, an unreadable record included, is ``None``.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    fd: int | None = None
    try:
        try:
            fd = os.open(
                f"{attempt}.json",
                os.O_RDONLY | nofollow | nonblock,
                dir_fd=dir_fd,
            )
        except (OSError, TypeError, NotImplementedError):
            return None
        try:
            info = os.fstat(fd)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_nlink != 1:
            return None
        if info.st_size > _TOOL_COMPLETION_MAX_BYTES:
            return None
        try:
            raw = os.read(fd, _TOOL_COMPLETION_MAX_BYTES + 1)
        except OSError:
            return None
        if len(raw) > _TOOL_COMPLETION_MAX_BYTES:
            return None
        return raw
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _read_completion_record(state_path: Path, attempt: str) -> bytes | None:
    """Read ``<attempt>.json`` under the tool-completions directory, safely.

    The directory itself is opened with a no-symlink guard and the record is
    opened relative to that directory descriptor, so a ``tool-completions``
    entry that is a symlink (or not a directory) is refused and nothing
    outside the state directory is read.  The record must be a regular,
    single-link file of at most 65536 bytes: a hardlink is not evidence this
    writer produced the record.  Any refusal is ``None``.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_fd: int | None = None
    try:
        try:
            dir_fd = os.open(
                state_path / _TOOL_COMPLETIONS_DIR,
                os.O_RDONLY | directory | nofollow,
            )
        except (OSError, TypeError, NotImplementedError):
            return None
        return _read_completion_record_fd(dir_fd, attempt)
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _write_completion_record(
    state_path: Path, attempt: str, record: Mapping[str, Any]
) -> bool:
    """Publish a tool-completion record create-only through a guarded dir fd.

    The ``tool-completions`` directory is opened by this function with
    ``O_NOFOLLOW | O_DIRECTORY`` (and created ``0700`` when absent), and every
    read, temp create and link happens relative to that descriptor: a
    symlinked or non-directory parent is refused before anything outside the
    state directory is read or written.  Returns True when this call published
    the record, False when an identical record already exists or the parent
    cannot be safely used.  A readable record with different bytes is a
    conflict and is never rewritten.
    """

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    path = state_path / _TOOL_COMPLETIONS_DIR
    dir_fd: int | None = None
    tmp_fd: int | None = None
    tmp_name: str | None = None
    try:
        try:
            dir_fd = os.open(path, os.O_RDONLY | directory | nofollow)
        except FileNotFoundError:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            except (OSError, TypeError, NotImplementedError):
                return False
            try:
                dir_fd = os.open(path, os.O_RDONLY | directory | nofollow)
            except (OSError, TypeError, NotImplementedError):
                return False
        except (OSError, TypeError, NotImplementedError):
            return False

        try:
            os.fchmod(dir_fd, 0o700)
        except OSError:
            pass

        payload = bee_events._canonical(record)
        existing = _read_completion_record_fd(dir_fd, attempt)
        if existing is not None:
            if existing == payload:
                return False
            raise bee_events.EventError(
                "event_conflict",
                f"{attempt}.json already holds different bytes",
                "Keep the original completion record; do not rewrite durable evidence.",
            )

        tmp_name = f".{attempt}.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            tmp_fd = os.open(
                tmp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
                0o600,
                dir_fd=dir_fd,
            )
        except (OSError, TypeError, NotImplementedError):
            return False
        try:
            view = memoryview(payload)
            while view:
                written = os.write(tmp_fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(tmp_fd)
        finally:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            tmp_fd = None

        try:
            os.link(
                tmp_name,
                f"{attempt}.json",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            current = _read_completion_record_fd(dir_fd, attempt)
            if current == payload:
                return False
            raise bee_events.EventError(
                "event_conflict",
                f"{attempt}.json already holds different bytes",
                "Keep the original completion record; do not rewrite durable evidence.",
            )
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        return True
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except (OSError, TypeError):
                pass
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _prior_completion(state_path: Path, attempt: str) -> dict[str, Any] | None:
    """The durable tool-completion record an attempt already has, if any.

    The ``tool-completions`` directory is opened only through a
    directory-level no-symlink guard, and the record must be a single-link
    regular file of at most 65536 bytes.  Any refusal, an unreadable record
    included, means "no prior record", so the completion is emitted rather
    than suppressed.
    """

    raw = _read_completion_record(state_path, attempt)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def record_tool_completion(
    state_dir: Path,
    attempt_id: str,
    *,
    state: str,
    branch: str | None = None,
    feature_id: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    transition_id: str | None = None,
    source_rev: str | None = None,
    lease_sha256: str | None = None,
    assigned_files: Any = None,
    model: str | None = None,
    harness: str | None = None,
    coordinator_identity: str | None = None,
    artifact_sha256: str | None = None,
    result_available: bool | None = None,
    usage: Mapping[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Write one tool-path attempt's terminal completion to the landing outbox.

    Only terminal states (``TOOL_TERMINAL_STATES``) emit; anything else is a
    reported no-op that touches no state. Facts the caller does not carry are
    filled from the immutable coordinator lease the dispatch recorded, so a
    completion that only knows the attempt id still produces the full
    canonical event. On success a create-only tool-completion record beside
    the outbox keeps the usage payload the result envelope carried, which the
    canonical landing schema itself has no slot for. The emission never
    raises: every refusal comes back as an ``{"emitted": False, ...}`` value
    so the attempt's completion path cannot be broken by an outbox write
    failure.
    """

    if state_dir is None:
        return {"emitted": False, "code": "state_dir_missing",
                "error": "no plugin state directory; the landing outbox is unavailable"}
    attempt = str(attempt_id or "").strip()
    if not attempt or not _ATTEMPT_ID_RE.match(attempt):
        return {"emitted": False, "code": "attempt_id_invalid",
                "error": "attempt id is not a safe identifier"}
    state_name = str(state or "").strip()
    if state_name not in TOOL_TERMINAL_STATES:
        return {
            "emitted": False,
            "reason": "state_not_terminal",
            "state": state_name,
        }

    state_path = Path(state_dir)
    transition = _tool_id(transition_id) or TOOL_TRANSITION_ID
    # Primary dedupe: the durable completion record is the per-attempt marker
    # a prior emission of this exact terminal state left behind. A duplicate
    # observation never rebuilds or re-records the event at all.
    prior = _prior_completion(state_path, attempt)
    if prior is not None and str(prior.get("state") or "") == state_name:
        event_id = str(prior.get("event_id") or "")
        if _tool_id(event_id):
            return {
                "emitted": True,
                "created": False,
                "event_id": event_id,
                "transition_id": transition,
                "state": state_name,
                "usage_recorded": bool(prior.get("usage")),
                "completion_record": f"{_TOOL_COMPLETIONS_DIR}/{attempt}.json",
            }

    lease = _lease_facts(state_path, attempt)
    brief = lease.get("brief") if isinstance(lease.get("brief"), Mapping) else {}
    digest = lease_sha256 if lease_sha256 is not None else lease.get("brief_sha256")
    if not isinstance(digest, str) or not _HEX64_RE.match(digest):
        return {
            "emitted": False,
            "code": "lease_facts_missing",
            "error": "no verified lease facts for this attempt; the canonical "
                     "landing event requires the coordinator lease digest",
            "fix": "Dispatch through bundlebees_dispatch, or carry "
                   "lease_sha256 and source_rev in the completion.",
        }
    source = source_rev if source_rev is not None else lease.get("source_rev")
    files = assigned_files if assigned_files is not None else lease.get("assigned_files")
    branch = branch if branch else lease.get("branch")
    feature = (
        _tool_id(feature_id)
        or _tool_id(brief.get("project"))
        or attempt
    )
    job = _tool_id(job_id if job_id else brief.get("job_id"))
    task = _tool_id(task_id if task_id else brief.get("task_id"))
    identity = _tool_id(coordinator_identity) or _TOOL_IDENTITY
    harness_text = _tool_id(harness) or _TOOL_HARNESS
    model_text = _tool_id(model)
    if model_text is not None and not bee_events._BACKEND_RE.match(model_text):
        model_text = None
    source_text = (
        source
        if isinstance(source, str) and bee_events._SOURCE_REV_RE.match(source.strip())
        else None
    )
    artifact = (
        artifact_sha256
        if isinstance(artifact_sha256, str) and _HEX64_RE.match(artifact_sha256)
        else None
    )
    reference = str(branch).strip() if isinstance(branch, str) and str(branch).strip() else None
    available = (
        result_available
        if isinstance(result_available, bool)
        else state_name == "succeeded"
    )

    try:
        event = bee_events.build_event(
            attempt_id=attempt,
            state=state_name,
            transition_id=transition,
            feature_id=feature,
            job_id=job,
            task_id=task,
            coordinator_identity=identity,
            harness=harness_text,
            model=model_text,
            source_rev=source_text,
            lease_sha256=digest,
            assigned_files=files if files is not None else [],
            result_available=available,
            artifact_sha256=artifact,
            reference=reference,
        )
    except bee_events.EventError as exc:
        return {"emitted": False, "code": exc.code, "error": exc.error, "fix": exc.fix}
    except Exception as exc:  # noqa: BLE001 - the completion path must survive any write failure
        return {"emitted": False, "code": "write_back_failed", "error": type(exc).__name__}

    try:
        stored = bee_events.record_landing(state_path, event)
    except bee_events.EventError as exc:
        # A create-only outbox refuses a rewrite; a replay of this exact
        # transition (identical except the first-seen time) is the duplicate
        # observation case, not a failure: the event already landed once.
        stored_event = (
            _stored_outbox_event(state_path, str(event.get("id") or ""))
            if exc.code == "event_conflict"
            else None
        )
        if stored_event is None or not _replay_matches(stored_event, event):
            return {"emitted": False, "code": exc.code, "error": exc.error, "fix": exc.fix}
        stored = {"created": False}
    except Exception as exc:  # noqa: BLE001 - the completion path must survive any write failure
        return {"emitted": False, "code": "write_back_failed", "error": type(exc).__name__}

    # The tool-path completion record: durable, create-only evidence keyed by
    # the attempt, carrying the usage payload the result envelope had (when it
    # did) — the canonical landing schema itself carries no usage field.
    clean_usage = _clean_usage(usage)
    usage_recorded = False
    completion_record: str | None = None
    record: dict[str, Any] = {
        "schema": TOOL_COMPLETION_SCHEMA,
        "attempt_id": attempt,
        "state": state_name,
        "branch": reference,
        "feature_id": feature,
        "event_id": event["id"],
        "recorded_at": datetime.fromtimestamp(
            _tool_clock(now), tz=timezone.utc
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if clean_usage:
        record["usage"] = clean_usage
    try:
        created_record = _write_completion_record(state_path, attempt, record)
        if not created_record:
            created_record = (
                _read_completion_record(state_path, attempt)
                == bee_events._canonical(record)
            )
        if created_record:
            usage_recorded = bool(clean_usage)
            completion_record = f"{_TOOL_COMPLETIONS_DIR}/{attempt}.json"
        else:
            usage_recorded = False
            completion_record = None
    except Exception:  # noqa: BLE001 - completion evidence is best-effort too
        usage_recorded = False
        completion_record = None

    return {
        "emitted": True,
        "created": bool(stored.get("created")),
        "event_id": event["id"],
        "transition_id": transition,
        "state": state_name,
        "usage_recorded": usage_recorded,
        "completion_record": completion_record,
    }


class ConsumerError(Exception):
    """The consumer could not finish an item; the caller bounds the retry."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.fix = str(fix)


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
        raise ConsumerError("clock_invalid", "now must be epoch seconds or an ISO timestamp") from exc


def _error_text(exc: BaseException) -> str:
    """A short, agent-agnostic refusal: a code, never a prompt or transcript."""

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    if isinstance(exc, ConsumerError) and exc.code:
        return exc.code
    return type(exc).__name__


def _relative(state_dir: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(state_dir).resolve()).as_posix()
    except ValueError:
        return Path(path).name


def _request_for(state_dir: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    request = recovery.load_request(Path(state_dir), str(item.get("request_id") or ""))
    if str(request.get("request_id") or "") != str(item.get("request_id") or ""):
        raise ConsumerError("work_request_mismatch", "the request id does not match the work item")
    if str(request.get("request_digest") or "") != str(item.get("request_digest") or ""):
        raise ConsumerError(
            "work_request_mismatch",
            "the durable request digest does not match the work item",
            "Refuse: a work item that points at another request must not be decided.",
        )
    return request


def _task_for(state_dir: Path, request: Mapping[str, Any]) -> dict[str, Any]:
    job = jobs.load_job(Path(state_dir), str(request.get("job_id") or ""), recover=False)
    task_id = str(request.get("task_id") or "")
    for task in job.get("tasks") or []:
        if str(task.get("task_id") or "") == task_id:
            return dict(task)
    raise ConsumerError("work_request_mismatch", f"job {request.get('job_id')!r} has no task {task_id!r}")


def _proposal_path(state_dir: Path, request: Mapping[str, Any]) -> Path:
    return hermes_adapter.proposal_path(Path(state_dir), request)


def _marker_path(state_dir: Path, request: Mapping[str, Any]) -> Path:
    job_id = str(request.get("job_id") or "")
    task_id = str(request.get("task_id") or "")
    ordinal = recovery.request_ordinal(Path(state_dir), str(request.get("request_id") or ""))
    return recovery.recovery_dir(Path(state_dir), job_id) / f"{task_id}.{ordinal}.proposal.crash.json"


def _write_fsync(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    payload = json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dfd = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _crash_guard(state_dir: Path, item: Mapping[str, Any]):
    """The test-only boundary: persist a marker, then die before the decision."""

    state = Path(state_dir)
    work_id = str(item.get("work_id") or "")

    def guarded(
        call_state: Path,
        request_id: str,
        decision: Mapping[str, Any],
        *,
        adapter: str = "deterministic",
        now: Any = None,
    ) -> dict[str, Any]:
        del decision, adapter, now
        request = recovery.load_request(Path(call_state), str(request_id))
        proposal = _proposal_path(Path(call_state), request)
        marker = _marker_path(Path(call_state), request)
        try:
            _write_fsync(
                marker,
                {
                    "schema": CRASH_SCHEMA,
                    "work_id": work_id,
                    "request_id": str(request.get("request_id") or ""),
                    "request_digest": str(request.get("request_digest") or ""),
                    "proposal": _relative(state, proposal),
                    "decision_recorded": False,
                    "pid": os.getpid(),
                    "at": _iso(time.time()),
                },
            )
        finally:
            # The crash is the point of the hook: never fall through to the
            # real recorder, whether or not the marker reached the disk.
            os._exit(CRASH_EXIT_CODE)
        raise AssertionError("unreachable")  # pragma: no cover

    return guarded


def _decide_with_hermes(
    state_dir: Path,
    item: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    repo: Path | None,
    timeout_s: int,
    model: str | None,
    provider: str | None,
    now: Any,
) -> dict[str, Any]:
    original = recovery.record_decision
    if os.environ.get(CRASH_ENV) == "1":
        recovery.record_decision = _crash_guard(Path(state_dir), item)  # type: ignore[assignment]
    try:
        return hermes_adapter.decide_with_hermes(
            Path(state_dir),
            str(request.get("request_id") or ""),
            repo=repo,
            timeout_s=timeout_s,
            model=model,
            provider=provider,
            now=now,
        )
    finally:
        recovery.record_decision = original


def _decision_from(outcome: Any, fallback: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], Any]:
    """Accept the adapter's documented outcome shape without guessing semantics."""

    if isinstance(outcome, Mapping):
        decision = outcome.get("decision")
        if isinstance(decision, Mapping):
            return dict(decision), outcome.get("proposal") or (fallback or {}).get("proposal")
        if "decision" in outcome and "schema" in outcome and "adapter" in outcome:
            return dict(outcome), (fallback or {}).get("proposal")
    raise ConsumerError(
        "adapter_outcome_invalid",
        "the adapter returned no canonical decision",
        "The adapter must return {'decision': <canonical decision>, 'proposal': <path>}.",
    )


def _adopt(
    state_dir: Path,
    item: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    repo: Path | None,
    now: Any,
) -> dict[str, Any]:
    """Adopt a persisted proposal; this function must never ask the model."""

    adopt = getattr(hermes_adapter, "adopt_proposal", None)
    if not callable(adopt):
        raise ConsumerError(
            "adopt_unavailable",
            "hermes_adapter.adopt_proposal is not available; a persisted proposal cannot be adopted",
            "Install the lane-B adapter that adopts a recorded proposal before running the consumer.",
        )
    return adopt(Path(state_dir), str(request.get("request_id") or ""), repo=repo, now=now)


def _proposal_ref(state_dir: Path, value: Any, fallback: Path | None = None) -> str | None:
    """Store only a state-relative proposal path, whatever the adapter returned."""

    if value not in (None, ""):
        path = Path(str(value))
        return _relative(state_dir, path) if path.is_absolute() else str(value)
    if fallback is not None and Path(fallback).exists():
        return _relative(state_dir, Path(fallback))
    return None


def _summary(
    action: str,
    item: Mapping[str, Any],
    *,
    consumer: str = "",
    decision: Mapping[str, Any] | None = None,
    proposal: Any = None,
) -> dict[str, Any]:
    return {
        "consumer": consumer,
        "work_id": str(item.get("work_id") or ""),
        "request_id": str(item.get("request_id") or ""),
        "state": str(item.get("state") or ""),
        "action": action,
        "proposal": proposal,
        "decision": dict(decision) if isinstance(decision, Mapping) else None,
    }


def _run_item(
    state_dir: Path,
    item: Mapping[str, Any],
    *,
    consumer: str,
    adapter: str | None,
    repo: Path | None,
    timeout_s: int,
    model: str | None,
    provider: str | None,
    now: Any,
) -> dict[str, Any]:
    state = Path(state_dir)
    work_id = str(item.get("work_id") or "")
    pid = os.getpid()
    request = _request_for(state, item)
    proposal_path = _proposal_path(state, request)
    proposal_exists = proposal_path.exists()
    decision = recovery.read_decision(state, request)

    if decision is not None:
        completed = agent_work.complete(
            state,
            work_id,
            consumer=consumer,
            pid=pid,
            proposal=_relative(state, proposal_path) if proposal_exists else None,
            decision=decision,
            now=now,
        )
        return _summary(
            "already_decided",
            completed,
            consumer=consumer,
            decision=decision,
            proposal=_relative(state, proposal_path) if proposal_exists else None,
        )

    if proposal_exists:
        outcome = _adopt(state, item, request, repo=repo, now=now)
        adopted, proposal_ref = _decision_from(outcome, {"proposal": _relative(state, proposal_path)})
        proposal_ref = _proposal_ref(state, proposal_ref, proposal_path)
        completed = agent_work.complete(
            state,
            work_id,
            consumer=consumer,
            pid=pid,
            proposal=proposal_ref,
            decision=adopted,
            now=now,
        )
        return _summary("adopted", completed, consumer=consumer, decision=adopted, proposal=proposal_ref)

    chosen = str(adapter or item.get("adapter") or "hermes")
    if chosen not in ADAPTERS:
        raise ConsumerError(
            "adapter_unknown",
            f"adapter {chosen!r} is not one of {', '.join(ADAPTERS)}",
            "Use --adapter hermes, --adapter deterministic or --adapter authorized.",
        )
    # A claim beyond the budget may still adopt a proposal or observe a decision
    # (both are inference-free), but it must never call an adapter again.
    claims = int(item.get("claims") or item.get("attempts") or 0)
    if claims > agent_work.MAX_ATTEMPTS:
        raise ConsumerError(
            "attempts_exhausted",
            f"work item {work_id!r} has been claimed {claims} times; no adapter is invoked",
        )

    if chosen == "authorized":
        # Test-only control adapter: mechanically selects the single authorised
        # replacement when the protocol permits exactly one. It never widens the
        # envelope and is never used for a real-model claim.
        task = _task_for(state, request)
        proposed = recovery.deterministic_adapter(request, task=task)
        allowed = list((request.get("envelope") or {}).get("corrected_tests_allowed") or [])
        failure_class = str((request.get("failure") or {}).get("class") or "")
        fixture_selector = os.environ.get("BUNDLEBEES_AUTHORIZED_SELECTOR", "").strip()
        chosen_tests = ([fixture_selector] if fixture_selector in allowed else
                        (allowed if len(allowed) == 1 else []))
        if (proposed.get("decision") == "escalate" and failure_class == "specification"
                and request.get("supersession_permitted") and chosen_tests
                and int((request.get("envelope") or {}).get("contract_corrections_allowed") or 0) == 1):
            at = recovery._iso(float(now) if now is not None else time.time())
            contract = recovery.derive_superseding_contract(
                task, chosen_tests, adapter="authorized-candidate", decided_at=at,
                contract_id=str((request.get("contract") or {}).get("id") or ""),
            )
            from . import contract as contract_mod
            sha = contract_mod.check_frozen(contract)
            proposed = {
                "schema": recovery.SCHEMA,
                "request_id": str(request.get("request_id") or ""),
                "request_digest": str(request.get("request_digest") or ""),
                "adapter": "authorized-candidate",
                "decision": "supersede",
                "corrected_tests": chosen_tests,
                "supersedes": {
                    "contract_id": str((request.get("contract") or {}).get("id") or ""),
                    "contract_sha256": str((request.get("contract") or {}).get("sha256") or ""),
                    "new_contract_id": str(contract.get("id") or ""),
                    "new_contract_sha256": sha,
                },
                "rationale": ("the protocol authorises exactly one replacement selector for this "
                              "specification failure; the test-only adapter selects it mechanically"),
                "decided_at": at,
            }
        stored = recovery.record_decision(
            state, str(request.get("request_id") or ""), proposed, adapter=str(proposed.get("adapter") or "authorized"), now=now
        )
        completed = agent_work.complete(
            state, work_id, consumer=consumer, pid=pid, decision=stored, now=now
        )
        return _summary("decided", completed, consumer=consumer, decision=stored)

    if chosen == "deterministic":
        task = _task_for(state, request)
        proposed = recovery.deterministic_adapter(request, task=task)
        stored = recovery.record_decision(
            state, str(request.get("request_id") or ""), proposed, adapter="deterministic", now=now
        )
        completed = agent_work.complete(
            state, work_id, consumer=consumer, pid=pid, decision=stored, now=now
        )
        return _summary("decided", completed, consumer=consumer, decision=stored)

    outcome = _decide_with_hermes(
        state,
        item,
        request,
        repo=repo,
        timeout_s=timeout_s,
        model=model,
        provider=provider,
        now=now,
    )
    decided, proposal_ref = _decision_from(outcome, {"proposal": _relative(state, proposal_path)})
    proposal_ref = _proposal_ref(state, proposal_ref, proposal_path)
    completed = agent_work.complete(
        state,
        work_id,
        consumer=consumer,
        pid=pid,
        proposal=proposal_ref,
        decision=decided,
        now=now,
    )
    return _summary("decided", completed, consumer=consumer, decision=decided, proposal=proposal_ref)


def consume_once(
    state_dir: Path,
    *,
    consumer: str,
    adapter: str | None = None,
    repo: Path | None = None,
    timeout_s: int = 300,
    model: str | None = None,
    provider: str | None = None,
    now: Any = None,
) -> dict[str, Any] | None:
    """Claim and process at most one item. ``None`` means nothing was claimable."""

    name = str(consumer or "").strip()
    if not name:
        raise ConsumerError("consumer_invalid", "the consumer must have a name")
    pid = os.getpid()
    for candidate in agent_work.list(Path(state_dir)):
        if str(candidate.get("state") or "") not in {"pending", "claimed"}:
            continue
        claimed = agent_work.claim(
            Path(state_dir),
            str(candidate.get("work_id") or ""),
            consumer=name,
            pid=pid,
            now=now,
        )
        if claimed is None:
            continue
        try:
            return _run_item(
                Path(state_dir),
                claimed,
                consumer=name,
                adapter=adapter,
                repo=Path(repo) if repo is not None else None,
                timeout_s=int(timeout_s),
                model=model,
                provider=provider,
                now=now,
            )
        except agent_work.WorkError as exc:
            if exc.code == "claim_not_held":
                # Another consumer adopted the stale claim; its turn owns the item.
                return {"consumer": name, "work_id": claimed.get("work_id"), "state": "lost_claim", "error": exc.code}
            return _fail(Path(state_dir), claimed, name, pid, exc, now)
        except Exception as exc:  # noqa: BLE001 - every adapter refusal is bounded
            return _fail(Path(state_dir), claimed, name, pid, exc, now)
    return None


def _fail(
    state_dir: Path,
    item: Mapping[str, Any],
    consumer: str,
    pid: int,
    exc: BaseException,
    now: Any,
) -> dict[str, Any]:
    """Release the first failure, refuse the last; never retry unbounded."""

    work_id = str(item.get("work_id") or "")
    error = _error_text(exc)
    attempts = int(item.get("attempts") or 0)
    try:
        if attempts >= agent_work.MAX_ATTEMPTS:
            stored = agent_work.refuse(
                state_dir, work_id, consumer=consumer, pid=pid, error=error, now=now
            )
            action = "refused"
        else:
            stored = agent_work.release(
                state_dir, work_id, consumer=consumer, pid=pid, error=error, now=now
            )
            action = "released"
    except agent_work.WorkError as lost:
        return {
            "consumer": consumer,
            "work_id": work_id,
            "state": "lost_claim" if lost.code == "claim_not_held" else "error",
            "action": "none",
            "error": lost.code,
        }
    return {
        "consumer": consumer,
        "work_id": work_id,
        "request_id": str(item.get("request_id") or ""),
        "state": str(stored.get("state") or ""),
        "action": action,
        "error": error,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bundlebees_plugin.agent_consumer",
        description="Consume recovery work items outside the mechanical tick.",
    )
    parser.add_argument("--state", required=True, help="Bundlebees state directory.")
    parser.add_argument("--consumer", required=True, help="Consumer name recorded on the claim.")
    parser.add_argument("--adapter", choices=ADAPTERS, default=None,
                        help="Decision adapter; defaults to the item's hint, then hermes.")
    parser.add_argument("--once", action="store_true", help="Process at most one item and exit.")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between attempts; without --once the consumer loops.")
    parser.add_argument("--repo", default=None, help="Repository for the Hermes evidence package.")
    parser.add_argument("--timeout-s", type=int, default=300, help="One Hermes turn timeout (default 300).")
    parser.add_argument("--model", default=None, help="Hermes model override.")
    parser.add_argument("--provider", default=None, help="Hermes provider override.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.once and args.interval is not None:
        print(json.dumps({"error": "argument_invalid", "message": "--once and --interval are mutually exclusive"}))
        return 2
    interval = args.interval
    if interval is not None and int(interval) <= 0:
        print(json.dumps({"error": "argument_invalid", "message": "--interval must be positive"}))
        return 2
    if interval is None and not args.once:
        args.once = True

    state = Path(args.state)
    while True:
        try:
            result = consume_once(
                state,
                consumer=str(args.consumer),
                adapter=args.adapter,
                repo=Path(args.repo) if args.repo else None,
                timeout_s=int(args.timeout_s),
                model=args.model,
                provider=args.provider,
            )
        except Exception as exc:  # noqa: BLE001 - the CLI reports a refusal, never a traceback
            print(json.dumps({"error": _error_text(exc), "message": str(exc)}))
            return 1
        if result is not None:
            print(json.dumps(result, sort_keys=True))
        if args.once:
            return 0
        time.sleep(float(interval))


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
