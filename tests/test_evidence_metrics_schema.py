"""Approved acceptance tests for the optional `metrics` object in evidence.schema.json. Coordinator-written; do not edit."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA = Path(__file__).resolve().parents[1] / "bundlebees_plugin" / "schemas" / "evidence.schema.json"
V = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
SHA = "a" * 40


def record(**over):
    r = {"evidence_version": "tdd-evidence/1", "contract_id": "W1-x", "contract_sha256": "b" * 64,
         "phase": "green", "revision": SHA, "base_rev": SHA, "test_fingerprints": {"t.py": "c" * 64},
         "command": ["pytest"],
         "result": {"exit_code": 0, "tests": 1, "failures": 0, "errors": 0, "skipped": 0, "timed_out": False},
         "classification": "passed", "criteria": {"AC-001": "passed"},
         "produced_by": {"role": "verifier"}, "test_first": True, "at": "2026-11-23T10:00:00Z"}
    r.update(over)
    return r


def errors(r):
    return list(V.iter_errors(r))


def test_ac001_baseline_record_without_metrics_is_valid():
    assert not errors(record()), [e.message for e in errors(record())]


FULL = {"lines_added": 10, "lines_removed": 2, "reedited_lines": 0, "attempts": 3, "retries": 2,
        "max_chain": 3, "review_findings": 1}


def test_ac002_full_metrics_object_is_valid():
    assert not errors(record(metrics=FULL))


def test_ac002_partial_metrics_object_is_valid():
    assert not errors(record(metrics={"retries": 1}))


@pytest.mark.parametrize("bad", [{"retries": -1}, {"lines_added": "10"}, {"retries": 1.5}, {"retries": True},
                                 {"unknown_field": 1}, [], "x", None])
def test_ac003_malformed_metrics_are_rejected(bad):
    assert errors(record(metrics=bad))


def test_ac004_metrics_do_not_relax_any_existing_requirement():
    r = record(metrics=FULL)
    del r["classification"]
    assert errors(r)


def test_ac005_existing_phase_and_classification_enums_are_unchanged():
    props = json.loads(SCHEMA.read_text())["properties"]
    assert props["phase"]["enum"] == ["red", "green", "refactor", "integration", "review"]
    assert "behavioral_failure" in props["classification"]["enum"]
