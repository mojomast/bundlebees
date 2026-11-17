"""Rails R05 acceptance tests: bundlebees source adapter (RED-first).

The adapter is read-only: it reads bundlebees' own durable verify, review and
merge records and emits rizzstack.rails/1 event drafts. It never invents a
transition, and an unsigned worker PASS remains a claim.

All digests are computed at runtime; the module is scanned for literal
secret-shaped or long hex/base64 strings.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
if CONTRIB_DIR not in sys.path:
    sys.path.insert(0, CONTRIB_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from rizzstack_rails import contracts as rails_contracts
from rizzstack_rails import ledger as rails_ledger
from bundlebees_plugin import rails_bundlebees_source as source

STAMP = "2026-01-02T03:04:05.000000Z"
HEX40 = lambda label: hashlib.sha1(label.encode("utf-8")).hexdigest()
DIGEST = lambda label: hashlib.sha256(label.encode("utf-8")).hexdigest()


def _validate(draft):
    rails_contracts.validate_event(
        draft["kind"],
        draft["source"],
        draft["scope"],
        draft["payload"],
        draft["authority"],
        draft.get("lane"),
        draft.get("occurred_at"),
        draft.get("duration_ms"),
        draft.get("timing_quality", "unknown"),
        draft.get("redaction_policy"),
    )


def _verify_record(attempt="att-1", *, ok=True, at="2026-01-02T03:04:05Z"):
    sha = HEX40("sha-" + attempt)
    return {
        "attempt_id": attempt,
        "ok": ok,
        "sha": sha,
        "branch": "bb-verify/" + attempt,
        "base_rev": HEX40("base-" + attempt),
        "files": ["bundlebees_plugin/x.py"],
        "tests": ["tests/test_x.py::test_x"],
        "red": {"exit_code": 1, "timed_out": False},
        "suite": {"exit_code": 0 if ok else 1, "timed_out": False},
        "gitleaks": {"exit_code": 0},
        "mode": "commit-local",
        "patch_sha256": DIGEST("patch-" + attempt),
        "resumed": False,
        "test_cache": {},
        "at": at,
    }


def _evidence_record(phase="green", *, classification="passed", role="verifier",
                     name="bundlebees verify", contract_id="contract-1", at="2026-01-02T03:04:05Z"):
    sha = HEX40("rev-" + phase)
    record = {
        "evidence_version": "tdd-evidence/1",
        "contract_id": contract_id,
        "contract_sha256": DIGEST("contract-" + contract_id),
        "phase": phase,
        "revision": sha,
        "base_rev": HEX40("base-" + contract_id),
        "test_fingerprints": {"tests/test_x.py": DIGEST("tests/test_x.py")},
        "command": ["pytest", "tests/test_x.py"],
        "result": {
            "exit_code": 0 if classification == "passed" else 1,
            "tests": 1,
            "failures": 0 if classification == "passed" else 1,
            "errors": 0,
            "skipped": 0,
            "timed_out": False,
        },
        "classification": classification,
        "criteria": {"AC-001": "passed" if classification == "passed" else "failed"},
        "produced_by": {"role": role, "name": name},
        "test_first": True,
        "at": at,
    }
    return record


def _review_record(*, accepted=True):
    sha = HEX40("review-head")
    return {
        "evidence_version": "tdd-evidence/1",
        "contract_id": "contract-1",
        "contract_sha256": DIGEST("contract-contract-1"),
        "phase": "review",
        "revision": sha,
        "base_rev": HEX40("base-contract-1"),
        "test_fingerprints": {"tests/test_x.py": DIGEST("tests/test_x.py")},
        "command": ["review"],
        "result": {"exit_code": None, "tests": 0, "failures": 0, "errors": 0,
                   "skipped": 0, "timed_out": False},
        "classification": "passed" if accepted else "failed",
        "criteria": {"AC-001": "passed" if accepted else "failed"},
        "produced_by": {"role": "reviewer", "name": "Reviewer One"},
        "test_first": True,
        "at": "2026-01-02T03:05:05Z",
        "notes": "looks correct",
        "attestation": {
            "kind": "coordinator_recorded_review",
            "recorded_by": "coordinator",
            "reviewer_label": "Reviewer One",
            "claimed_reviewer_kind": "reviewer",
            "identity_verified": False,
        },
    }


def _wave_record(*, accepted=True):
    return {
        "wave_id": "w1",
        "accepted": accepted,
        "head_sha": HEX40("wave-head"),
        "attempts": ["att-1"],
        "problems": {} if accepted else {"att-1": "suite failed"},
        "suite": {"exit_code": 0 if accepted else 1, "timed_out": False},
        "regressions": 0,
        "at": "2026-01-02T03:06:05Z",
    }


class SnapshotCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rails-r05-")
        self.addCleanup(self._tmp.cleanup)
        self.state = self._tmp.name

    def write(self, relative, document):
        path = os.path.join(self.state, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True)
        return path


class CommittedRecordTests(SnapshotCase):
    def test_committed_verify_maps_to_one_valid_pass_result(self):
        record = _verify_record()
        self.write("verify-results/att-1.json", record)
        snapshot = source.collect_snapshot(self.state)
        events = source.events_from_snapshot(snapshot)
        verify = [event for event in events if event["kind"] == "verification.result"]
        self.assertEqual(1, len(verify))
        event = verify[0]
        _validate(event)
        self.assertEqual("PASS", event["payload"]["verdict"])
        self.assertEqual(record["sha"], event["payload"]["code_rev"])
        self.assertEqual("att-1", event["payload"]["run_id"])
        self.assertEqual("green", event["payload"]["phase"])
        self.assertEqual("coordinator_record", event["authority"])
        self.assertTrue(event["payload"]["evidence_refs"])
        self.assertEqual("att-1", event["scope"]["attempt_id"])
        self.assertEqual(STAMP, event["occurred_at"])

    def test_failed_verify_is_fail_with_reason(self):
        self.write("verify-results/att-2.json", _verify_record("att-2", ok=False))
        events = source.collect_events(self.state)
        event = [item for item in events if item["kind"] == "verification.result"][0]
        _validate(event)
        self.assertEqual("FAIL", event["payload"]["verdict"])
        self.assertTrue(event["payload"]["reason"])

    def test_review_pass_and_reject_map_to_review_result(self):
        self.write("evidence/contract-1/0003-review.json", _review_record())
        event = source.collect_events(self.state)[0]
        _validate(event)
        self.assertEqual("review.result", event["kind"])
        self.assertEqual("PASS", event["payload"]["verdict"])
        self.assertEqual("Reviewer One", event["payload"]["reviewer"])
        self.assertEqual(HEX40("review-head"), event["payload"]["reviewed_head"])
        self.assertIs(False, event["payload"]["independent"])
        self.assertEqual("reviewer_record", event["authority"])
        self.assertEqual("evidence/contract-1/0003-review.json", event["source"]["record_id"])

        rejected = source.events_from_snapshot(
            {"records": [{"type": "review", "record_id": "r2", "record": _review_record(accepted=False)}]}
        )[0]
        _validate(rejected)
        self.assertEqual("FAIL", rejected["payload"]["verdict"])

    def test_accepted_wave_maps_to_merge_result(self):
        self.write("waves/w1.json", _wave_record())
        event = source.collect_events(self.state)[0]
        _validate(event)
        self.assertEqual("merge.result", event["kind"])
        self.assertEqual("merged", event["payload"]["result"])
        self.assertEqual("w1", event["payload"]["wave_id"])
        self.assertEqual(HEX40("wave-head"), event["payload"]["head_sha"])
        self.assertEqual("coordinator_record", event["authority"])


class ClaimTests(SnapshotCase):
    def test_unsigned_worker_pass_stays_a_claim_not_a_transition(self):
        claim = _evidence_record(role="worker", name="worker bee")
        events = source.events_from_snapshot(
            {"records": [{"type": "claim", "record_id": "evidence/contract-1/0001-green.json",
                          "record": claim}]}
        )
        self.assertEqual(1, len(events))
        event = events[0]
        _validate(event)
        self.assertEqual("verification.result", event["kind"])
        self.assertEqual("UNVERIFIED", event["payload"]["verdict"])
        self.assertNotEqual("PASS", event["payload"]["verdict"])
        self.assertEqual("worker_report", event["authority"])
        self.assertTrue(event["payload"]["claim"])
        self.assertTrue(event["payload"]["reason"])
        self.assertNotIn("job.state", {item["kind"] for item in events})
        self.assertNotIn("attempt.state", {item["kind"] for item in events})

    def test_worker_role_evidence_in_verify_directory_is_a_claim(self):
        self.write("evidence/contract-1/0002-green.json", _evidence_record(role="worker"))
        events = source.collect_events(self.state)
        self.assertEqual(["UNVERIFIED"], [event["payload"]["verdict"] for event in events])
        self.assertEqual(["worker_report"], [event["authority"] for event in events])

    def test_invalid_signature_review_is_a_claim(self):
        record = _review_record()
        record["signature"] = DIGEST("not-the-signature")
        events = source.events_from_snapshot(
            {"records": [{"type": "review", "record_id": "r", "record": record,
                          "signature_valid": False}]}
        )
        event = events[0]
        _validate(event)
        self.assertEqual("UNVERIFIED", event["payload"]["verdict"])
        self.assertEqual("worker_report", event["authority"])


class ReplayTests(SnapshotCase):
    def test_same_snapshot_emits_identical_events(self):
        self.write("verify-results/att-1.json", _verify_record())
        self.write("evidence/contract-1/0003-review.json", _review_record())
        self.write("waves/w1.json", _wave_record())
        snapshot = source.collect_snapshot(self.state)
        first = source.events_from_snapshot(snapshot)
        second = source.events_from_snapshot(json.loads(json.dumps(snapshot)))
        self.assertEqual(first, second)
        self.assertEqual(3, len(first))
        self.assertEqual(
            sorted(["merge.result", "review.result", "verification.result"]),
            sorted(event["kind"] for event in first),
        )

    def test_snapshot_does_not_invent_rows_for_missing_records(self):
        self.assertEqual([], source.collect_events(self.state))
        empty = source.events_from_snapshot({"records": []})
        self.assertEqual([], empty)

    def test_events_do_not_carry_ledger_assigned_fields(self):
        self.write("verify-results/att-1.json", _verify_record())
        event = source.collect_events(self.state)[0]
        for field in rails_contracts.SERVER_ASSIGNED_KEYS:
            self.assertNotIn(field, event)
        self.assertTrue(event["dedupe_key"])

    def test_raw_json_records_and_snapshots_are_accepted(self):
        record = _verify_record()
        as_text = source.events_from_records([json.dumps(record)])
        as_mapping = source.events_from_records([record])
        self.assertEqual(as_mapping, as_text)
        self.assertEqual(
            as_mapping,
            source.events_from_snapshot(json.dumps({"records": [record]})),
        )

    def test_ordering_is_stable_across_input_order(self):
        first = {"type": "verify", "record_id": "b", "record": _verify_record("att-b")}
        second = {"type": "verify", "record_id": "a", "record": _verify_record("att-a")}
        forward = source.events_from_snapshot({"records": [first, second]})
        reverse = source.events_from_snapshot({"records": [second, first]})
        self.assertEqual(forward, reverse)


class RedPhaseTests(SnapshotCase):
    def test_red_evidence_is_expected_red_with_frozen_intent(self):
        record = _evidence_record(phase="red", classification="behavioral_failure")
        events = source.events_from_records([record])
        event = events[0]
        _validate(event)
        self.assertEqual("EXPECTED_RED", event["payload"]["verdict"])
        self.assertTrue(event["payload"]["frozen_test_intent"])
        self.assertEqual("red", event["payload"]["phase"])


class SignatureTests(SnapshotCase):
    def test_snapshot_key_verifies_signed_review_and_rejects_tampering(self):
        record = _review_record()
        body = {key: value for key, value in record.items() if key != "signature"}
        record["signature"] = __import__("hmac").new(
            b"k" * 32,
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.write("evidence/contract-1/0003-review.json", record)
        trusted = source.collect_events(self.state, key=b"k" * 32)[0]
        self.assertEqual("PASS", trusted["payload"]["verdict"])
        self.assertEqual("reviewer_record", trusted["authority"])

        record["notes"] = "edited after signing"
        self.write("evidence/contract-1/0003-review.json", record)
        tampered = source.collect_events(self.state, key=b"k" * 32)[0]
        self.assertEqual("UNVERIFIED", tampered["payload"]["verdict"])
        self.assertEqual("worker_report", tampered["authority"])


class LedgerReplayTests(SnapshotCase):
    def test_replaying_a_snapshot_adds_no_rows(self):
        self.write("verify-results/att-1.json", _verify_record())
        self.write("waves/w1.json", _wave_record())
        drafts = source.collect_events(self.state)
        db = os.path.join(self.state, "rails.sqlite3")
        ledger = rails_ledger.RailsLedger(db)
        self.addCleanup(ledger.close)
        first = source.append_events(ledger, drafts)
        head = ledger.head()["seq"]
        second = source.append_events(ledger, source.collect_events(self.state))
        self.assertEqual(head, ledger.head()["seq"])
        self.assertEqual(len(drafts), head)
        self.assertTrue(all(item["duplicated"] for item in second))
        self.assertFalse(any(item["duplicated"] for item in first))
        rows = ledger.query("SELECT kind FROM event_rows ORDER BY seq")
        self.assertEqual(
            sorted(draft["kind"] for draft in drafts),
            sorted(row["kind"] for row in rows),
        )


class SourceHygieneTests(unittest.TestCase):
    def test_module_has_no_literal_secret_or_long_hex_base64(self):
        path = os.path.join(REPO_ROOT, "bundlebees_plugin", "rails_bundlebees_source.py")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIsNone(re.search(r"[0-9a-fA-F]{21,}", text))
        self.assertIsNone(re.search(r"[A-Za-z0-9+/]{32,}={0,2}", text))
        self.assertIsNone(re.search(r"(key|secret|token)\s*=\s*[\"'][^\"']+[\"']", text, re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
