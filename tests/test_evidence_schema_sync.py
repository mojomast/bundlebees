"""Phase 1 (rizzstack cleanup): the bundled TDD evidence schema matches workflow's.

workflow (the protocol owner, same $id) declares two OPTIONAL fields the bundlebees copy lacks:
`sequence` (signer-assigned position, integer >= 1) and `prev_sha256` (sha256 of the previous
record, 64 lowercase hex or null). Both schemas allow additional properties and neither field is
required, so records bundlebees writes today stay valid and the evidence format does not change;
the sync only stops bundlebees from accepting malformed values for those two fields.

AC-001: the bundled schema declares sequence and prev_sha256 exactly as workflow does, and
        neither is required.
AC-002: a record with a malformed prev_sha256 or sequence is refused by tdd validation; the
        same record with well-formed values (or without them) is accepted.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.test_tdd import RED_CASES, _record

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "bundlebees_plugin" / "schemas" / "evidence.schema.json"

WORKFLOW_PREV = {
    "type": ["string", "null"],
    "pattern": "^[0-9a-f]{64}$",
}
WORKFLOW_SEQUENCE = {"type": "integer", "minimum": 1}


class TestEvidenceSchemaSync:
    def test_optional_chain_fields_match_workflow(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        props = schema["properties"]
        for name, expected in (("prev_sha256", WORKFLOW_PREV), ("sequence", WORKFLOW_SEQUENCE)):
            assert name in props, f"{name} missing from the bundled evidence schema"
            got = {k: v for k, v in props[name].items() if k != "description"}
            assert got == expected, (name, got)
            assert props[name].get("description")
            assert name not in schema["required"]

    def test_malformed_chain_fields_are_refused(self):
        from bundlebees_plugin import tdd

        good = _record(tdd, "red", RED_CASES)
        tdd._validate_record(good)
        ok = dict(copy.deepcopy(good), sequence=1, prev_sha256=None)
        tdd._validate_record(ok)
        tdd._validate_record(dict(ok, sequence=2, prev_sha256="a" * 64))
        for bad in (dict(ok, prev_sha256="not-a-hash"), dict(ok, prev_sha256="A" * 64), dict(ok, sequence=0), dict(ok, sequence="1")):
            with pytest.raises(tdd.TddError):
                tdd._validate_record(bad)
