"""Durable per-item coordinator action queue: records, claims and fences.

The queue is the coordinator's durable memory for actions that must happen at
most once per action key and never unsupervised: merge a landing, deploy a
release, record a report. It answers three questions and nothing else:

1. Which actions are queued, claimed or done right now?
2. Who owns each claimed action, and until when?
3. Is a caller still the fenced owner of an action, or has it been superseded?

It is deliberately *not* an executor. The queue never runs a command, never
spawns a process, never calls a model and never dispatches work; execution
stays with the caller. An AST guard in the test suite pins this: the module
imports only stdlib bookkeeping, :mod:`bundlebees_plugin.security` and the
inert fault-checkpoint lane, and it names no execution verb.

Layout, one directory per coordinator state directory:

``<state>/coordinator/actions/<action_id>.json``
    the per-item record, replaced atomically (write temp file, ``fsync``,
    ``os.replace``, fsync the directory) and created ``0600`` in a ``0700``
    directory.
``<state>/coordinator/actions/fence.seq``
    the durable fencing counter, one JSON document holding the highest
    allocated value. It only ever increases: a value burned by a crash
    between minting and claiming is never reused.
``<state>/coordinator/actions/actions.lock``
    an exclusive ``flock`` serialises every read-modify-write across
    processes, not merely threads.

Fencing follows the classic Kleppmann lease-and-fence design. Every claim
mints the next ``fence_seq`` from the durable counter and stores it on the
record; the caller must present that exact value for every later mutation
(:func:`renew`, :func:`complete`). A presented value below the current one is
refused as ``stale_fence`` — the precise refusal a paused owner deserves after
another owner adopted the item — while any other mismatch, including a value
that has never been minted, is refused as ``fence_mismatch``. The lease expiry
is the adoption hint, the fence is the guarantee: an owner whose lease lapsed
but who was never adopted can still complete on its current ``fence_seq``,
because nobody else holds a higher one.

Per-repo serialization is deliberately narrow. Only ``merge`` and ``deploy``
actions hold repo leases, and only overlapping ones — same repository and
compatible targets — refuse a second live claim. The gate runs wherever a
lease could become live — at :func:`claim`, at :func:`adopt` and equally at
:func:`renew` — so a lapsed lease is never extended across an overlapping
live claim and two live overlapping leases on one repository can never
exist. Everything else, including two actions in different repositories or
on different targets, proceeds in parallel; the queue records, it does not
arbitrate more than asked.

Replay is idempotent by ``action_key``: re-enqueueing an existing action id
with its original key and shape returns the stored record untouched — no new
file, no burned fence value, no timestamp churn. The same key under a
different id, or a different key under the same id, is refused precisely.

Error text is passed through :func:`bundlebees_plugin.security.scrub`. The
clock is injectable (``now``) in every mutating call, so ordering and
timestamps are deterministic under test.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

try:  # POSIX advisory locks; bundlebees targets Linux and CI is ubuntu
    import fcntl
except ImportError:  # pragma: no cover - a platform without flock cannot prove a single claim
    fcntl = None  # type: ignore[assignment]

from .security import scrub

__all__ = [
    "SCHEMA",
    "FENCE_SCHEMA",
    "LEASE_KINDS",
    "DEFAULT_TTL_S",
    "ActionError",
    "actions_dir",
    "action_path",
    "fence_path",
    "enqueue",
    "claim",
    "adopt",
    "renew",
    "complete",
    "load",
    "list_actions",
    "fence_value",
]

SCHEMA = "bundlebees.coordinator-actions/1"
FENCE_SCHEMA = "bundlebees.coordinator-actions-fence/1"

#: Kinds that hold a repo lease. Only these serialize per repository, and
#: only while their claims overlap; every other kind never blocks anything.
LEASE_KINDS = frozenset({"merge", "deploy"})

DEFAULT_TTL_S = 900.0

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_KIND_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")
_MAX_LABEL = 200

_FIX_ID = "Use letters, digits, '-', '_' and '.' only, at most 128 characters."
_FIX_LABEL = "Use printable text without control characters, at most 200 characters."
_FIX_STATE = "Inspect the record by hand; a corrupt record may hold a live lease."
_FIX_FENCE = (
    "Do not edit the fencing counter. Restore the queue from its last durable "
    "copy; guessing a fencing value would defeat the fence."
)


class ActionError(Exception):
    """A queue operation was refused. ``code`` names the precise refusal."""

    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(scrub(message))
        self.code, self.message, self.fix = code, scrub(message), scrub(fix)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


# ---------------------------------------------------------------------------
# validation and clocks
# ---------------------------------------------------------------------------


def _epoch(now: Any) -> float:
    if now is None:
        return time.time()
    value = now() if callable(now) else now
    try:
        epoch = float(value)
    except (TypeError, ValueError) as exc:
        raise ActionError(
            "clock_invalid", f"now {value!r} is not epoch seconds",
            "Pass a clock the queue can read.",
        ) from exc
    if epoch != epoch or epoch in (float("inf"), float("-inf")):
        raise ActionError("clock_invalid", f"now {value!r} is not a finite epoch", "Pass a real clock.")
    return epoch


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _action_id(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _ID_RE.fullmatch(text):
        raise ActionError("invalid_action_id", f"action id {text!r} is not a safe path component", _FIX_ID)
    return text


def _label(value: Any, what: str, *, allow_empty: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if not text and allow_empty:
        return ""
    if not text or len(text) > _MAX_LABEL or any(not (32 <= ord(ch) < 127) for ch in text):
        raise ActionError(f"invalid_{what}", f"{what} {text[:40]!r} is not safe record text", _FIX_LABEL)
    return text


def _kind(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _KIND_RE.fullmatch(text):
        raise ActionError(
            "invalid_kind", f"kind {text[:40]!r} is not a safe label",
            "Use letters, digits, '-' and '_', starting with a letter, at most 32 characters.",
        )
    return text


def _ttl(ttl_s: Any) -> float:
    try:
        value = float(ttl_s)
    except (TypeError, ValueError) as exc:
        raise ActionError("invalid_ttl", f"ttl_s {ttl_s!r} is not seconds", "Pass a positive number of seconds.") from exc
    if not (value > 0.0) or value == float("inf"):
        raise ActionError("invalid_ttl", f"ttl_s {ttl_s!r} is not a positive finite lease", "Pass a positive number of seconds.")
    return value


def _fence(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionError(
            "fence_invalid", f"fence_seq {value!r} is not an int",
            "Present the exact fence_seq the claim returned.",
        )
    return int(value)


def _jsonable(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ActionError(
            f"{what}_invalid", f"{what} is not JSON-serializable: {exc}",
            f"Pass a {what} that survives a JSON round trip.",
        ) from exc


# ---------------------------------------------------------------------------
# durable paths and atomic writes
# ---------------------------------------------------------------------------


def actions_dir(state: Any) -> Path:
    """The per-item action directory under an existing coordinator state dir."""

    return Path(state) / "coordinator" / "actions"


def action_path(state: Any, action_id: str) -> Path:
    return actions_dir(state) / f"{_action_id(action_id)}.json"


def fence_path(state: Any) -> Path:
    """The durable fencing counter for a coordinator state directory."""

    return actions_dir(state) / "fence.seq"


def _lock_path(state: Any) -> Path:
    return actions_dir(state) / "actions.lock"


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
        try:
            os.close(fd)
        except OSError:
            pass


def _payload_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically replace *path* and fsync the file and its directory."""

    _private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_payload_bytes(value))
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


@contextmanager
def _locked(state: Any) -> Iterator[None]:
    """Serialise read-modify-write of the queue with a kernel-held lock."""

    if fcntl is None:  # pragma: no cover - no portable fallback for a proof of one claim
        raise ActionError(
            "locking_unavailable",
            "this platform has no flock, so a single active claim cannot be proven",
            "Run the coordinator on POSIX (Linux/macOS); an unlocked claim is not a claim.",
        )
    lock = _lock_path(state)
    _private_dir(lock.parent)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _checkpoint(name: str, **payload: Any) -> None:
    """Fire a fault-injection checkpoint, if the faults lane installed one.

    Imported lazily on purpose: ``bundlebees_plugin.faults`` is owned by another
    lane and may be absent, and its absence must never break this module's
    import. A hook that raises is *not* caught here — that raise is the
    simulated crash.
    """
    try:
        import importlib

        faults = importlib.import_module(f"{__package__}.faults")
    except ImportError:
        return
    fire = getattr(faults, "checkpoint", None)
    if callable(fire):
        fire(name, **payload)


# ---------------------------------------------------------------------------
# record reads
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ActionError("state_unreadable", f"{path.name} cannot be read: {exc}", _FIX_STATE) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ActionError("state_corrupt", f"{path.name} is not valid JSON: {exc}", _FIX_STATE) from exc
    if not isinstance(data, dict):
        raise ActionError("state_corrupt", f"{path.name} is not a JSON object", _FIX_STATE)
    return data


def _read_record(state: Any, action_id: str) -> dict[str, Any]:
    path = action_path(state, action_id)
    try:
        record = _read_json(path)
    except FileNotFoundError:
        return None  # type: ignore[return-value]
    if record.get("schema") != SCHEMA or record.get("action_id") != action_id:
        raise ActionError(
            "state_corrupt", f"{path.name} is not schema {SCHEMA!r} for action {action_id}", _FIX_STATE
        )
    return record


def _read_record_or_raise(state: Any, action_id: str) -> dict[str, Any]:
    record = _read_record(state, action_id)
    if record is None:
        raise ActionError(
            "unknown_action", f"no action {action_id} under {actions_dir(state)}",
            "Enqueue the action first.",
        )
    return record


def load(state: Any, action_id: str) -> dict[str, Any] | None:
    """The stored record for *action_id*, or ``None`` when it was never queued."""

    return _read_record(state, _action_id(action_id))


def list_actions(state: Any) -> list[dict[str, Any]]:
    """Every stored record, sorted by action id, refusing unreadable ones.

    A record that cannot be read might hold a live lease, so it is a named
    refusal rather than a silently dropped item.
    """

    root = actions_dir(state)
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        record = _read_json(path)
        if record.get("schema") != SCHEMA or record.get("action_id") != path.stem:
            raise ActionError(
                "state_corrupt", f"{path.name} is not schema {SCHEMA!r} for action {path.stem}", _FIX_STATE
            )
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# the durable fencing counter
# ---------------------------------------------------------------------------


def _read_fence_counter(state: Any) -> int:
    path = fence_path(state)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ActionError(
            "fence_counter_corrupt", f"{path.name} cannot be read: {exc}", _FIX_FENCE
        ) from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ActionError(
            "fence_counter_corrupt",
            f"{path.name} is not valid JSON, so the next fencing value is unknown",
            _FIX_FENCE,
        ) from exc
    value = doc.get("fence_seq") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("schema") != FENCE_SCHEMA:
        raise ActionError(
            "fence_counter_corrupt",
            f"{path.name} does not record a valid fencing counter",
            _FIX_FENCE,
        )
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionError(
            "fence_counter_corrupt",
            f"{path.name} does not record a valid fencing counter",
            _FIX_FENCE,
        )
    return int(value)


def fence_value(state: Any) -> int:
    """The highest allocated fencing value, ``0`` before the first claim."""

    with _locked(state):
        return _read_fence_counter(state)


def _allocate_fence_seq(state: Any) -> int:
    """Mint the next fencing value durably; call only while holding the lock.

    The counter is replaced and fsynced *before* the claim record is written,
    so a crash in between burns the value instead of ever reusing one.
    """

    value = _read_fence_counter(state) + 1
    _write_json(fence_path(state), {"schema": FENCE_SCHEMA, "fence_seq": value})
    _checkpoint("coordinator_actions_fence_allocated", fence_seq=value)
    return value


# ---------------------------------------------------------------------------
# enqueue: create-only intake with idempotent replay by action key
# ---------------------------------------------------------------------------


def enqueue(
    state: Any,
    action_id: str,
    *,
    action_key: str,
    kind: str,
    repo: str = "",
    target: str = "",
    payload: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Queue one coordinator action. Create-only; replays return the record.

    Re-enqueueing an existing ``action_id`` with its original ``action_key``
    and shape (kind, repo, target) is an idempotent replay: the stored record
    is returned unchanged, including its original payload, and nothing else
    happens — no new file, no fencing value, no timestamp churn. A different
    key under the same id, or the same key under a different id, is refused.
    """

    action_id = _action_id(action_id)
    action_key = _label(action_key, "action_key")
    kind = _kind(kind)
    repo = _label(repo, "repo", allow_empty=True)
    target = _label(target, "target", allow_empty=True)
    body = {} if payload is None else _jsonable(payload, "payload")
    epoch = _epoch(now)

    record: dict[str, Any] = {
        "schema": SCHEMA,
        "action_id": action_id,
        "action_key": action_key,
        "kind": kind,
        "repo": repo,
        "target": target,
        "payload": body,
        "status": "queued",
        "fence_seq": 0,
        "owner": "",
        "claimed_at": None,
        "lease_expires_at": None,
        "adopted_from": None,
        "result": None,
        "created_at": _iso(epoch),
        "updated_at": _iso(epoch),
    }

    with _locked(state):
        existing = _read_record(state, action_id)
        if existing is not None:
            if (
                existing.get("action_key") == action_key
                and existing.get("kind") == kind
                and existing.get("repo") == repo
                and existing.get("target") == target
            ):
                return existing
            raise ActionError(
                "action_id_conflict",
                f"action {action_id} already exists with action_key {existing.get('action_key')!r}",
                "Use a new action id, or replay the original enqueue.",
            )
        for other in list_actions(state):
            if other.get("action_key") == action_key:
                raise ActionError(
                    "action_key_conflict",
                    f"action_key {action_key!r} already belongs to action {other.get('action_id')}",
                    "One action key names one logical action; enqueue the replay under its own id.",
                )
        _write_json(action_path(state, action_id), record)
    return record


# ---------------------------------------------------------------------------
# claims, adoption, renewal, completion
# ---------------------------------------------------------------------------


def _live_lease(record: Mapping[str, Any], epoch: float) -> bool:
    expires = record.get("lease_expires_at")
    return (
        record.get("status") == "claimed"
        and isinstance(expires, (int, float))
        and not isinstance(expires, bool)
        and float(expires) > epoch
    )


def _leases_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two records hold the same overlapping merge or deploy lease."""

    repo = str(left.get("repo") or "")
    if not repo or repo != str(right.get("repo") or ""):
        return False
    if left.get("kind") not in LEASE_KINDS or right.get("kind") not in LEASE_KINDS:
        return False
    own, theirs = str(left.get("target") or ""), str(right.get("target") or "")
    return not own or not theirs or own == theirs


def _refuse_overlapping_leases(state: Any, record: Mapping[str, Any], epoch: float) -> None:
    for other in list_actions(state):
        if other.get("action_id") == record.get("action_id") or not _live_lease(other, epoch):
            continue
        if _leases_overlap(record, other):
            raise ActionError(
                "lease_conflict",
                f"{record.get('kind')} lease on {record.get('repo') or '-'} overlaps "
                f"{other.get('action_id')}: {other.get('kind')} held by {other.get('owner')} "
                f"until {float(other.get('lease_expires_at')):.0f}",
                "Wait for the overlapping lease to complete, expire or be adopted.",
            )


def _take(
    state: Any,
    action_id: str,
    owner: str,
    *,
    ttl_s: float,
    now: Any,
    adopt_expired: bool,
) -> dict[str, Any]:
    action_id = _action_id(action_id)
    owner = _label(owner, "owner")
    ttl = _ttl(ttl_s)
    epoch = _epoch(now)

    with _locked(state):
        record = _read_record_or_raise(state, action_id)
        status = record.get("status")
        if status == "done":
            raise ActionError("action_done", f"action {action_id} is already done", "Enqueue a new action.")
        if status == "claimed":
            if _live_lease(record, epoch):
                raise ActionError(
                    "claim_active",
                    f"action {action_id} is claimed by {record.get('owner')} "
                    f"until {float(record.get('lease_expires_at')):.0f}",
                    "Wait for the lease to expire, then adopt the action.",
                )
            if not adopt_expired:
                raise ActionError(
                    "claim_expired",
                    f"the claim on action {action_id} by {record.get('owner')} expired "
                    f"at {float(record.get('lease_expires_at') or 0):.0f}",
                    "Adopt the action with coordinator_actions.adopt; a fresh claim is not a takeover.",
                )
        elif adopt_expired:
            raise ActionError(
                "nothing_to_adopt",
                f"action {action_id} is {status}; only an expired claim can be adopted",
                "Claim the queued action with coordinator_actions.claim.",
            )

        _refuse_overlapping_leases(state, record, epoch)
        record["fence_seq"] = _allocate_fence_seq(state)
        record.update(
            status="claimed",
            owner=owner,
            claimed_at=_iso(epoch),
            lease_expires_at=epoch + ttl,
            adopted_from=record.get("owner") if adopt_expired and status == "claimed" else None,
            updated_at=_iso(epoch),
        )
        _write_json(action_path(state, action_id), record)
    return record


def claim(state: Any, action_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S, now: Any = None) -> dict[str, Any]:
    """Atomically claim a queued action and mint its fencing value.

    Two racing claims cannot both win: the queue serialises on
    ``actions.lock`` and refuses the second with ``claim_active`` naming the
    winner and the expiry. An overlapping merge or deploy lease on the same
    repository refuses the claim with ``lease_conflict`` before any fencing
    value is minted.
    """

    return _take(state, action_id, owner, ttl_s=ttl_s, now=now, adopt_expired=False)


def adopt(state: Any, action_id: str, owner: str, *, ttl_s: float = DEFAULT_TTL_S, now: Any = None) -> dict[str, Any]:
    """Take over an action whose claim expired, minting a higher fence value.

    This is the stale-owner path: the previous owner stays recorded as
    ``adopted_from``, and from now on every mutation it presents with its old
    ``fence_seq`` is refused as ``stale_fence``.
    """

    return _take(state, action_id, owner, ttl_s=ttl_s, now=now, adopt_expired=True)


def _require_current_fence(record: Mapping[str, Any], presented: int) -> None:
    current = record.get("fence_seq")
    action_id = record.get("action_id")
    if presented == current:
        return
    if isinstance(current, int) and presented < current:
        raise ActionError(
            "stale_fence",
            f"stale fencing value {presented} for action {action_id}: the current "
            f"fence_seq is {current}, held by {record.get('owner')}",
            "The owner was superseded; re-read the record and let the current owner proceed.",
        )
    raise ActionError(
        "fence_mismatch",
        f"fence_seq {presented} does not match the current fence_seq {current} "
        f"for action {action_id} (held by {record.get('owner')})",
        "Present the exact fence_seq the claim returned.",
    )


def renew(
    state: Any,
    action_id: str,
    fence_seq: int,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    now: Any = None,
) -> dict[str, Any]:
    """Extend the lease; only the current fenced owner may renew.

    The per-repo gate runs here too: a renewal that would make a lapsed
    lease live again across an overlapping live merge or deploy lease on
    the same repository is refused with ``lease_conflict`` and extends
    nothing, so two live overlapping leases on one repository can never
    exist. With no overlapping claim live, a lapsed lease renews on its
    still-current fence; adoption remains the route back when a newer
    owner holds a higher fence.
    """

    action_id = _action_id(action_id)
    fence = _fence(fence_seq)
    ttl = _ttl(ttl_s)
    epoch = _epoch(now)

    with _locked(state):
        record = _read_record_or_raise(state, action_id)
        status = record.get("status")
        if status == "done":
            raise ActionError("action_done", f"action {action_id} is already done", "Enqueue a new action.")
        if status != "claimed":
            raise ActionError(
                "not_claimed", f"action {action_id} is {status}; a current fence_seq is required",
                "Claim the action first.",
            )
        _require_current_fence(record, fence)
        _refuse_overlapping_leases(state, record, epoch)
        record["lease_expires_at"] = epoch + ttl
        record["updated_at"] = _iso(epoch)
        _write_json(action_path(state, action_id), record)
    return record


def complete(
    state: Any,
    action_id: str,
    fence_seq: int,
    *,
    result: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    """Mark the action done on the current fencing value and release its lease.

    A stale ``fence_seq`` — below the current one — is refused as
    ``stale_fence``; any other mismatch is refused as ``fence_mismatch``.
    Completion releases the repo lease, so a waiting merge or deploy on the
    same repository may claim immediately.
    """

    action_id = _action_id(action_id)
    fence = _fence(fence_seq)
    epoch = _epoch(now)

    with _locked(state):
        record = _read_record_or_raise(state, action_id)
        status = record.get("status")
        if status == "done":
            raise ActionError("action_done", f"action {action_id} is already done", "Enqueue a new action.")
        if status != "claimed":
            raise ActionError(
                "not_claimed", f"action {action_id} is {status}; a current fence_seq is required",
                "Claim the action first.",
            )
        _require_current_fence(record, fence)
        record.update(
            status="done",
            result=_jsonable(result, "result"),
            lease_expires_at=None,
            updated_at=_iso(epoch),
        )
        _write_json(action_path(state, action_id), record)
    return record
