"""Fixtures for the packet-native two-feature proof (P2).

Pure builders and readers: nothing here asserts, and nothing here creates,
derives or authorizes anything. The documents produced here are the ones the
pinned workflow protocol at ``WORKFLOW_SRC`` validates.

State layout used by the test (and expected by the production handoff):

* ``<state>/packets/<job_id>.json`` — the immutable intent packet, stored once
  as canonical bytes and keyed by the job it admits.
* ``<state>/resolutions/<job_id>.json`` — the
  ``rizzstack.source-resolution/1`` record written by production. The test does
  not depend on its filename, only on its content.

Nothing in this module may contain a contract hash for an unfrozen template,
and an intent packet must never carry ``contract``, ``contract_sha256`` or
``source_resolution_ref``: those belong to the resolution stage only.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Mapping

PACKET_VERSION = "rizzstack.feature-packet/1"
RESOLUTION_VERSION = "rizzstack.source-resolution/1"
CONTRACT_VERSION = "acdd/1"

REPO = "mojomast/smoke-real"
FEATURE_A = "feature-a"
FEATURE_B = "feature-b"
JOB_A = "job_a"
JOB_B = "job_b"
POLICY_B = "policy-b"

ANSWER_FILE = "smoke/answer.py"
DOUBLE_FILE = "smoke/double.py"
ANSWER_TEST_FILE = "tests/test_smoke.py"
DOUBLE_TEST_FILE = "tests/test_double.py"
ANSWER_TEST = "tests/test_smoke.py::test_answer_is_42"
DOUBLE_TEST = "tests/test_double.py::test_double_is_84"

APPROVED_AT = "2026-11-13T00:00:00Z"
EVIDENCE = ("red", "green", "regression", "gitleaks", "integration")
GATES = ("merge", "release", "deploy", "publish")
PROHIBITED = ("push to origin", "merge", "deploy", "edit approved tests")


def workflow_src() -> Path | None:
    value = os.environ.get("WORKFLOW_SRC", "")
    path = Path(value) if value else None
    if path is None or not (path / "tools" / "feature_packet.py").exists():
        return None
    return path


def workflow_ref(src: Path | None = None) -> str:
    """The exact protocol commit the packets are validated against."""

    source = Path(src) if src is not None else workflow_src()
    if source is None:
        raise RuntimeError("WORKFLOW_SRC is not a pinned workflow checkout")
    done = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                          capture_output=True, text=True)
    ref = done.stdout.strip()
    if done.returncode != 0 or len(ref) != 40 or any(ch not in "0123456789abcdef" for ch in ref):
        raise RuntimeError(f"cannot resolve the protocol commit in {source}: {done.stderr.strip()}")
    return ref


def acdd_contract(*, contract_id: str, criterion_id: str, statement: str,
                  assigned_file: str, test_file: str, test_id: str,
                  source_rev: str) -> dict[str, Any]:
    """A complete, *unfrozen* acdd/1 contract body."""

    return {
        "contract_version": CONTRACT_VERSION,
        "id": contract_id,
        "title": statement,
        "outcome": {"summary": statement, "observable_behavior": [statement]},
        "source_rev": source_rev,
        "scope": {"assigned_files": [assigned_file], "test_files": [test_file]},
        "acceptance_criteria": [{
            "id": criterion_id,
            "statement": statement,
            "tests": [test_id],
            "verification": "test",
        }],
        "tdd": {"applicable": True, "test_first": False,
                "reason": "the approved test exists and fails at the baseline"},
        "verification": {"independent": True},
        "evidence_required": ["green"],
        "authorization": {"human_gates": list(GATES)},
    }


def _contract_facts(contract: Mapping[str, Any]) -> dict[str, Any]:
    criteria = [c for c in contract.get("acceptance_criteria") or [] if isinstance(c, Mapping)]
    return {
        "acceptance_ids": [str(c.get("id")) for c in criteria],
        "tests": sorted({str(t) for c in criteria for t in c.get("tests") or []}),
        "lease": [str(p) for p in (contract.get("scope") or {}).get("assigned_files") or []],
        "human_gates": [str(g) for g in (contract.get("authorization") or {}).get("human_gates") or []],
    }


def _auth(*, reference: str, approved_by: str, approved_at: str,
          budget: tuple[int, int]) -> dict[str, Any]:
    return {
        "reference": reference,
        "approved_by": approved_by,
        "approved_at": approved_at,
        "limits": {"bees": budget[0], "wall_clock_s": budget[1]},
    }


def _budget(bees: int, wall_clock_s: int) -> dict[str, int]:
    return {"bees": bees, "wall_clock_s": wall_clock_s}


def exact_packet(*, contract: Mapping[str, Any], contract_sha256: str, workflow_ref: str,
                 feature_id: str = FEATURE_A, job_id: str = JOB_A,
                 read_only: tuple[str, ...] = (ANSWER_TEST_FILE,),
                 summary: str = "answer() returns 42",
                 provenance: str = "test:feature-a",
                 approved_by: str = "reviewer", approved_at: str = APPROVED_AT,
                 budget: tuple[int, int] = (1, 1800)) -> dict[str, Any]:
    """An exact-policy packet: a frozen contract and the authorized revision."""

    facts = _contract_facts(contract)
    return {
        "packet_version": PACKET_VERSION,
        "feature_id": feature_id,
        "job_id": job_id,
        "request": {"summary": summary, "provenance": provenance},
        "repository": {"identity": REPO, "branch": "main"},
        "workflow_ref": workflow_ref,
        "source_policy": {"kind": "exact", "revision": str(contract.get("source_rev") or "")},
        "authorization": _auth(reference=f"auth:{job_id}", approved_by=approved_by,
                               approved_at=approved_at, budget=budget),
        "contract": dict(contract),
        "contract_sha256": contract_sha256,
        "acceptance_ids": facts["acceptance_ids"],
        "tests": facts["tests"],
        "scope": {"lease": facts["lease"], "read_only": list(read_only)},
        "budget": _budget(*budget),
        "required_evidence": list(EVIDENCE),
        "prohibited": list(PROHIBITED),
        "human_gates": facts["human_gates"],
    }


def intent_packet(*, template: Mapping[str, Any], workflow_ref: str,
                  feature_id: str = FEATURE_B, job_id: str = JOB_B,
                  dependency: str = FEATURE_A, policy_id: str = POLICY_B,
                  read_only: tuple[str, ...] = (DOUBLE_TEST_FILE,),
                  summary: str = "double_answer() returns 84",
                  provenance: str = "test:feature-b",
                  approved_by: str = "reviewer", approved_at: str = APPROVED_AT,
                  budget: tuple[int, int] = (1, 1800)) -> dict[str, Any]:
    """An immutable intent packet: an unfrozen template and a descendant policy.

    Deliberately carries no ``contract``, ``contract_sha256`` or
    ``source_resolution_ref``: at intake the dependent revision does not exist.
    """

    facts = _contract_facts(template)
    return {
        "packet_version": PACKET_VERSION,
        "feature_id": feature_id,
        "job_id": job_id,
        "request": {"summary": summary, "provenance": provenance},
        "repository": {"identity": REPO, "branch": "main"},
        "workflow_ref": workflow_ref,
        "source_policy": {
            "kind": "descendant",
            "root_revision": str(template.get("source_rev") or ""),
            "dependencies": [dependency],
            "policy_id": policy_id,
        },
        "authorization": _auth(reference=f"policy:{policy_id}", approved_by=approved_by,
                               approved_at=approved_at, budget=budget),
        "contract_template": dict(template),
        "acceptance_ids": facts["acceptance_ids"],
        "tests": facts["tests"],
        "scope": {"lease": facts["lease"], "read_only": list(read_only)},
        "budget": _budget(*budget),
        "required_evidence": list(EVIDENCE),
        "prohibited": list(PROHIBITED),
        "human_gates": facts["human_gates"],
    }


def canonical_bytes(packet: Mapping[str, Any]) -> bytes:
    """The exact bytes the packet digest is taken over (and what is stored)."""

    return json.dumps(packet, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(packet)).hexdigest()


def packet_store(state_dir: Path) -> Path:
    return Path(state_dir) / "packets"


def packet_path(state_dir: Path, job_id: str) -> Path:
    """The coordinator's packet store is keyed by the job the packet admits."""

    return packet_store(state_dir) / f"{job_id}.json"


def write_packet(state_dir: Path, packet: Mapping[str, Any]) -> Path:
    """Store one intent packet exactly as the coordinator stores it: create-only."""

    path = packet_path(state_dir, str(packet["job_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(packet)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise AssertionError(f"a different packet is already stored at {path}")
        return path
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def read_packet(state_dir: Path, job_id: str) -> dict[str, Any]:
    return json.loads(packet_path(state_dir, job_id).read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolutions(state_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every source-resolution record under the state directory, in path order."""

    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(Path(state_dir).rglob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(body, dict) and body.get("resolution_version") == RESOLUTION_VERSION:
            found.append((path, body))
    return found


def resolution_for(state_dir: Path, packet_digest: str) -> tuple[Path, dict[str, Any]] | None:
    for path, body in resolutions(state_dir):
        if body.get("packet_digest") == packet_digest:
            return path, body
    return None
