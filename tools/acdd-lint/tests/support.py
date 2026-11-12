"""Shared fixtures for the approved acdd-lint tests (coordinator-written)."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))

SHA = "a" * 40
GREEN_REV = "b" * 40
FP = {"tests/test_tax.py": "d" * 64}
KEY = b"k" * 32


def find_schemas() -> tuple[dict, dict]:
    """Load the shared schemas from the repo this tool lives in (workflow or bundlebees)."""
    for parent in [TOOL, *TOOL.parents]:
        for c, e in (("schemas/acdd-contract.schema.json", "schemas/tdd-evidence.schema.json"),
                     ("bundlebees_plugin/schemas/contract.schema.json", "bundlebees_plugin/schemas/evidence.schema.json")):
            if (parent / c).is_file() and (parent / e).is_file():
                return json.loads((parent / c).read_text()), json.loads((parent / e).read_text())
    raise RuntimeError("shared schemas not found above the tool")


def canonical_sha(contract: dict) -> str:
    body = {k: v for k, v in contract.items() if k != "review"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def contract(**over) -> dict:
    c = {
        "contract_version": "acdd/1", "id": "T1-tax", "title": "apply_tax",
        "outcome": {"summary": "tax", "observable_behavior": ["apply_tax(1000, 10) == 1100"]},
        "interfaces": [{"name": "apply_tax", "kind": "function", "change": "add"}],
        "acceptance_criteria": [
            {"id": "AC-001", "given": "1000", "when": "apply_tax(1000, 10)", "then": "1100", "verification": "test",
             "tests": ["tests/test_tax.py::test_adds"]},
            {"id": "AC-002", "statement": "suite passes", "verification": "integration"},
        ],
        "scope": {"assigned_files": ["shop/tax.py"], "test_files": ["tests/test_tax.py"], "prohibited": []},
        "source_rev": SHA, "tdd": {"applicable": True, "test_first": True},
        "verification": {"independent": True}, "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "tag", "release", "deploy", "publish"]},
    }
    c.update(over)
    return c


def frozen(**over) -> dict:
    c = contract(**over)
    c["review"] = {"status": "approved", "approved_by": "kyle", "approved_at": "2026-11-12T18:00:00Z",
                   "contract_sha256": canonical_sha(c)}
    return c


def record(c: dict, phase: str, classification: str, *, at: str, revision: str | None = None, fp: dict | None = None,
           role: str = "verifier", sign: bool = True, **over) -> dict:
    passed = classification == "passed"
    r = {
        "evidence_version": "tdd-evidence/1", "contract_id": c["id"],
        "contract_sha256": c.get("review", {}).get("contract_sha256", "0" * 64),
        "phase": phase, "revision": revision or (SHA if phase == "red" else GREEN_REV), "base_rev": c["source_rev"],
        "test_fingerprints": dict(fp or FP), "command": ["python3", "-m", "pytest"],
        "result": {"exit_code": 0 if passed else 1, "tests": 1, "failures": 0 if passed else 1, "errors": 0, "skipped": 0,
                   "timed_out": False},
        "classification": classification, "criteria": {"AC-001": "passed" if passed else "failed", "AC-002": "not_run"},
        "produced_by": {"role": role, "name": "bundlebees"}, "test_first": True, "at": at,
    }
    r.update(over)
    if sign:
        body = {k: v for k, v in r.items() if k not in ("signature", "_path")}
        r["signature"] = hmac.new(KEY, json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(),
                                  hashlib.sha256).hexdigest()
    return r


def red(c, **kw):
    return record(c, "red", "behavioral_failure", at="2026-11-12T18:01:00Z", **kw)


def green(c, **kw):
    return record(c, "green", "passed", at="2026-11-12T18:02:00Z", **kw)


def clone(obj):
    return copy.deepcopy(obj)
