"""Runtime enforcement of the pinned workflow feature-packet protocol.

The protocol lives in `mojomast/workflow`. Bundlebees does not re-implement it:
it loads the validator from the pinned checkout (`WORKFLOW_SRC`) and refuses to
admit a workflow-native job when that checkout is unavailable. Legacy jobs are
untouched and are never given invented packet evidence.

A job's intent packet is persisted once, byte for byte, at
``<state_dir>/packets/<job_id>.json``; the job document seals the SHA-256 of
exactly those bytes. Re-reading the packet therefore proves what the job was
admitted under, and a later rewrite of the packet file cannot go unnoticed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PACKETS_DIR = "packets"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class PacketError(Exception):
    def __init__(self, code: str, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix

    def to_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message, "fix": self.fix}


def workflow_src() -> Path:
    value = os.environ.get("WORKFLOW_SRC", "")
    path = Path(value) if value else None
    required = os.environ.get("REQUIRE_WORKFLOW") == "1"
    if path is None or not (path / "tools" / "feature_packet.py").exists():
        if required:
            raise PacketError("workflow_unavailable",
                              "the pinned workflow checkout is required but missing",
                              "Check out the pinned workflow revision and set WORKFLOW_SRC.")
        raise PacketError("workflow_unavailable",
                          "no pinned workflow checkout is configured",
                          "Set WORKFLOW_SRC to the pinned workflow revision.")
    return path


def _validator():
    src = workflow_src()
    module_path = src / "tools" / "feature_packet.py"
    spec = importlib.util.spec_from_file_location("workflow_feature_packet", module_path)
    if spec is None or spec.loader is None:
        raise PacketError("workflow_unavailable", f"cannot load {module_path}", "Fix the pinned checkout.")
    module = importlib.util.module_from_spec(spec)
    sys.modules["workflow_feature_packet"] = module
    spec.loader.exec_module(module)
    return module


def canonical_bytes(packet: Mapping[str, Any]) -> bytes:
    """The exact bytes a packet digest is taken over."""

    return json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()


def digest(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(packet)).hexdigest()


def validate(packet: Mapping[str, Any]) -> str:
    """Validate against the pinned protocol and return the packet digest."""

    try:
        module = _validator()
        module.validate(packet, workflow_repo=workflow_src(), check_protocol_commit=True)
    except PacketError:
        raise
    except Exception as exc:  # noqa: BLE001 - the pinned validator's refusal is a refusal
        code = getattr(exc, "code", "packet_invalid")
        raise PacketError(str(code), str(exc), "Refuse the packet; do not relax the protocol.") from exc
    return digest(packet)


def validate_resolution(record: Mapping[str, Any], *, packet: Mapping[str, Any],
                        accepted_commit: str) -> dict[str, Any]:
    """Validate a source-resolution record against the pinned protocol.

    The record is what a dependent feature's baseline resolves to; the intent
    packet it belongs to and the dependency's accepted commit are both inputs,
    so the record cannot name a different policy, dependency or revision.
    """

    try:
        module = _validator()
        out = module.validate_source_resolution(record, packet=packet,
                                                accepted_commit=str(accepted_commit))
    except PacketError:
        raise
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "resolution_invalid")
        raise PacketError(str(code), str(exc), "Refuse the resolution; re-derive it from the policy.") from exc
    if not isinstance(out, Mapping):
        raise PacketError("resolution_invalid", "the pinned validator returned no record", "Refuse.")
    return dict(out)


# ---------------------------------------------------------------------------
# Intent-packet selection helpers (structural only; `validate` is the gate)
# ---------------------------------------------------------------------------


def source_policy(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Return the packet's source policy block, refusing a non-descendant packet."""

    policy = packet.get("source_policy")
    if not isinstance(policy, Mapping):
        raise PacketError("source_policy_invalid", "the packet names no source policy",
                          "Use an exact or descendant packet.")
    if policy.get("kind") != "descendant":
        raise PacketError("source_policy_invalid",
                          f"{policy.get('kind')!r} is not a descendant source policy",
                          "A dependent job needs a descendant intent packet.")
    body = dict(policy)
    if not str(body.get("policy_id") or "").strip():
        raise PacketError("policy_missing", "the intent packet names no enrolled policy id",
                          "Reference the enrolled policy exactly.")
    dependencies: Sequence[Any] = body.get("dependencies") or []
    if len(dependencies) != 1:
        raise PacketError("dependency_unsupported", "exactly one dependency is supported",
                          "Split the feature.")
    return body


def policy_id(packet: Mapping[str, Any]) -> str:
    return str(source_policy(packet)["policy_id"]).strip()


def dependency_feature(packet: Mapping[str, Any]) -> str:
    return str(source_policy(packet)["dependencies"][0])


# ---------------------------------------------------------------------------
# Immutable packet storage
# ---------------------------------------------------------------------------


def _safe_id(job_id: Any) -> str:
    text = str(job_id or "").strip()
    if not text or len(text) > 96 or not all(ch.isalnum() or ch in "-_." for ch in text):
        raise PacketError("packet_invalid", f"{job_id!r} is not a safe job id for a packet path",
                          "Use the job's own id.")
    return text


def packet_path(state_dir: Path, job_id: str) -> Path:
    return Path(state_dir) / PACKETS_DIR / f"{_safe_id(job_id)}.json"


def read_packet(state_dir: Path, job_id: str) -> bytes | None:
    try:
        return packet_path(state_dir, job_id).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PacketError("packet_unreadable", f"the stored packet cannot be read: {exc}",
                          "Refuse: inspect the packet directory.") from exc


def store_packet(state_dir: Path, job_id: str, packet: Mapping[str, Any]) -> tuple[Path, str]:
    """Persist the intent packet once. Returns ``(path, digest)``.

    Create-only: the first writer wins and an existing file is only reused when
    its bytes hash to this exact packet. A different packet for the same job is
    a conflict, never an overwrite.
    """

    payload = canonical_bytes(packet)
    expected = hashlib.sha256(payload).hexdigest()
    path = packet_path(state_dir, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o400)
    except FileExistsError:
        existing = read_packet(state_dir, job_id)
        if existing is None:
            raise PacketError("packet_conflict", f"the packet file for {job_id} vanished during the write",
                              "Retry once; if it recurs, inspect the state directory.") from None
        if hashlib.sha256(existing).hexdigest() != expected:
            raise PacketError("packet_conflict", f"a different packet is already stored for {job_id}",
                              "Refuse: a job's intent packet is immutable.")
        return path, expected
    except OSError as exc:
        raise PacketError("packet_unwritable", f"the packet could not be stored: {exc}",
                          "Fix the state directory permissions.") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path, expected


def verify_stored(state_dir: Path, job_id: str, expected: str) -> str:
    """Prove the stored packet bytes still hash to the sealed digest."""

    payload = read_packet(state_dir, job_id)
    if payload is None:
        raise PacketError("packet_missing", f"the intent packet for {job_id} is missing",
                          "Restore the recorded packet; never recreate a job without it.")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != str(expected):
        raise PacketError("packet_altered", f"the stored packet for {job_id} changed after admission",
                          "Refuse: the job's intent packet is immutable.")
    return actual
