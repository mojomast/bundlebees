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
from bundlebees_plugin import tdd
from pathlib import Path

STAMP = "2026-01-02T03:04:05.000000Z"
TEST_SEAL = os.urandom(32)
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


def _wire_rows(s):
    rows = []
    for row in s.events():
        body = rails_contracts.event_body(
            row["kind"], row["source"], row["scope"], row["payload"], row["authority"],
            row.get("lane"), row.get("occurred_at"), row.get("duration_ms"),
            row.get("timing_quality", "unknown"), row.get("redaction_policy"),
        )
        rows.append(dict(body, schema="rizzstack.rails-candidate/1", dedupe_key=row["dedupe_key"]))
    return rows


def _legacy_cursor(s, n, stop):
    hashes = [source._hash([s.adapter_id, s.instance])]
    for row in _wire_rows(s):
        hashes.append(source._hash([hashes[-1], row]))
    return f"{n}:{stop}:{hashes[n]}:{hashes[stop]}"


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
    def test_commit_local_without_contract_is_an_observation_not_a_verified_ac(self):
        # verify_local._record does not bind a contract (patch_sha256 is not one).
        self.write('verify-results/att-1.json', _verify_record())
        event, = source.collect_events(self.state)
        self.assertEqual('source.error', event['kind'])
        self.assertEqual('missing_contract_sha256', event['payload']['code'])
        self.assertIsNone(event['scope'].get('contract_sha256'))

    def test_contract_bound_committed_verify_maps_to_one_valid_pass_result(self):
        record = dict(_verify_record(), contract_sha256=DIGEST('bound contract'), ac_id='AC-001')
        path = Path(self.write("verify-results/att-1.json", record))
        events = source.collect_events(self.state)
        verify = [event for event in events if event["kind"] == "verification.result"]
        self.assertEqual(1, len(verify))
        event = verify[0]
        _validate(event)
        self.assertEqual("PASS", event["payload"]["verdict"])
        self.assertEqual(record["sha"], event["payload"]["code_rev"])
        self.assertEqual("att-1", event["payload"]["run_id"])
        self.assertEqual("green", event["payload"]["phase"])
        self.assertEqual("coordinator_record", event["authority"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), event['source']['revision'])
        self.assertTrue(event["payload"]["evidence_refs"])
        self.assertEqual("att-1", event["scope"]["attempt_id"])
        self.assertEqual(STAMP, event["occurred_at"])

    def test_failed_verify_is_fail_with_reason(self):
        self.write("verify-results/att-2.json", dict(_verify_record("att-2", ok=False),
                                                    contract_sha256=DIGEST('bound'), ac_id='AC-001'))
        events = source.collect_events(self.state)
        event = [item for item in events if item["kind"] == "verification.result"][0]
        _validate(event)
        self.assertEqual("FAIL", event["payload"]["verdict"])
        self.assertTrue(event["payload"]["reason"])

    def test_unsigned_review_cannot_attest_a_decision(self):
        self.write("evidence/contract-1/0003-review.json", _review_record())
        event = source.collect_events(self.state)[0]
        _validate(event)
        self.assertEqual("review.result", event["kind"])
        self.assertEqual("UNVERIFIED", event["payload"]["verdict"])
        self.assertEqual(HEX40("review-head"), event["payload"]["reviewed_head"])
        self.assertIs(False, event["payload"]["independent"])
        self.assertEqual("worker_report", event["authority"])
        self.assertNotIn('/', event['source']['record_id'])

        rejected = source.events_from_snapshot(
            {"records": [{"type": "review", "record_id": "r2", "record": _review_record(accepted=False)}]}
        )[0]
        _validate(rejected)
        self.assertEqual("UNVERIFIED", rejected["payload"]["verdict"])

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
            sorted(["source.error", "review.result", "source.error"]),
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
        events = source.events_from_records([tdd.sign_record(record, TEST_SEAL)], key=TEST_SEAL)
        event = events[0]
        _validate(event)
        self.assertEqual("EXPECTED_RED", event["payload"]["verdict"])
        self.assertTrue(event["payload"]["frozen_test_intent"])
        self.assertEqual("red", event["payload"]["phase"])


class SignatureTests(SnapshotCase):
    def test_read_only_key_derivation_matches_producer(self):
        from bundlebees_plugin import leases

        root = Path(self.state)
        # A missing key is not an invitation for a reader to initialize one.
        s = source.BundlebeesSource.from_existing_state(root)
        self.assertEqual([], s.read_batch()['records'])
        self.assertEqual([], list(root.iterdir()))

        signing_key = leases.evidence_key(root)
        tdd.write_record(root, _evidence_record(), signing_key)
        event, = source.BundlebeesSource.from_existing_state(root).read_batch()['records']
        self.assertEqual(('verifier_record', 'PASS'), (event['authority'], event['payload']['verdict']))
        (root / 'lease.key').unlink()
        event, = source.BundlebeesSource.from_existing_state(root).read_batch()['records']
        self.assertEqual(('worker_report', 'UNVERIFIED'), (event['authority'], event['payload']['verdict']))
        self.assertFalse((root / 'lease.key').exists())

    def test_copied_authority_flags_are_not_proof(self):
        r = _review_record()
        r['attestation']['kind'] = 'human_authorization'
        r['independent'] = True
        r['authority'] = 'owner_record'
        signed = tdd.sign_record(r, TEST_SEAL)
        for value, key in [(signed, None), (r, TEST_SEAL), (signed, b'wrong')]:
            with self.subTest(key=bool(key), signed='signature' in value):
                event, = source.events_from_snapshot({'records': [dict(type='review', record=value,
                                                         signature_valid=True)]}, key=key)
                self.assertEqual('UNVERIFIED', event['payload']['verdict'])
                self.assertFalse(event['payload']['independent'])
                self.assertEqual('worker_report', event['authority'])
        self.write('evidence/contract-1/0003-review.json', signed)
        event, = source.collect_events(self.state, key=TEST_SEAL)
        self.assertEqual('coordinator_record', event['authority'])
        self.assertFalse(event['payload']['independent'])

    def test_verifier_pass_requires_a_real_run_at_a_bound_criterion(self):
        for overrides, expected in [({}, 'PASS'), ({'tests': 0}, 'UNVERIFIED'),
                                    ({'exit_code': None}, 'UNVERIFIED'), ({'skipped': 1}, 'UNVERIFIED'),
                                    ({'timed_out': True}, 'UNVERIFIED'), ({'errors': 1}, 'UNVERIFIED')]:
            r = _evidence_record()
            r['result'].update(overrides)
            self.write('evidence/contract-1/0002-green.json', tdd.sign_record(r, TEST_SEAL))
            event, = source.collect_events(self.state, key=TEST_SEAL)
            self.assertEqual(expected, event['payload']['verdict'], overrides)
            self.assertEqual('verifier_record', event['authority'])

    def test_unsupported_positive_wave_evidence_is_not_a_decision(self):
        for changes in [dict(suite=None), dict(suite={'exit_code': False}),
                        dict(suite={'exit_code': 0, 'blocking': ['regression']}),
                        dict(problems={'att-1': 'private source details'}), dict(head_sha=None),
                        dict(regressions=2), dict(accepted=None), dict(attempts=[])]:
            self.write('waves/w1.json', dict(_wave_record(), **changes))
            event, = source.collect_events(self.state)
            self.assertEqual('source.error', event['kind'], changes)
            self.assertNotIn('private source details', json.dumps(event))

    def test_commit_local_pass_requires_positive_execution(self):
        for changes in [dict(suite=None), dict(suite={}), dict(suite={'exit_code': False}),
                        dict(suite={'timed_out': True, 'exit_code': 0}), dict(resumed=True),
                        dict(red=None), dict(gitleaks={'exit_code': 1})]:
            self.write('verify-results/att-1.json', dict(_verify_record(), ac_id='AC-001',
                                                         contract_sha256=DIGEST('bound'), **changes))
            event, = source.collect_events(self.state)
            self.assertEqual('UNVERIFIED', event['payload']['verdict'], changes)

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
        # tdd.review_identity: the coordinator's HMAC seals the observation,
        # not the named reviewer's identity, credentials or independent run.
        self.assertEqual("coordinator_record", trusted["authority"])
        self.assertFalse(trusted['payload']['independent'])

        record["notes"] = "edited after signing"
        self.write("evidence/contract-1/0003-review.json", record)
        tampered = source.collect_events(self.state, key=b"k" * 32)[0]
        self.assertEqual("UNVERIFIED", tampered["payload"]["verdict"])
        self.assertEqual("worker_report", tampered["authority"])


class LedgerReplayTests(SnapshotCase):
    def test_prefix_insertion_resume_dedupes_full_import(self):
        from rizzstack_rails.importer import Importer

        for attempt, order in (("att-b", 2), ("att-d", 4), ("att-f", 6)):
            path = self.write(f"verify-results/{attempt}.json", _verify_record(attempt))
            os.utime(path, (order, order))
        ledger = rails_ledger.RailsLedger(os.path.join(self.state, "rails.sqlite"))
        self.addCleanup(ledger.close)
        imp = Importer(ledger, authorities=("coordinator_record", "imported_observation"))
        first = imp.import_once(source.BundlebeesSource(self.state), limit=1, byte_limit=4096)
        second = imp.import_once(source.BundlebeesSource(self.state), limit=1, byte_limit=4096)
        desc = source.BundlebeesSource(self.state).describe()
        cursor = imp.cursor(desc["adapter_id"], desc["source_instance"])

        inserted = self.write("verify-results/att-a.json", _verify_record("att-a"))
        os.utime(inserted, (1, 1))
        page = source.BundlebeesSource(self.state).read_batch(cursor, 1, 4096)
        self.assertEqual(["att-f"], [row["scope"]["attempt_id"] for row in page["records"]])
        self.assertFalse(page["has_more"])
        self.assertEqual("r1." + page["records"][-1]["dedupe_key"], page["next_cursor"])

        resumed = imp.import_once(source.BundlebeesSource(self.state), limit=1, byte_limit=4096)
        replay = imp.import_once(source.BundlebeesSource(self.state), limit=1, byte_limit=4096)
        self.assertEqual([1, 1, 1, 0], [first["imported"], second["imported"],
                                       resumed["imported"], replay["imported"]])
        keys = [row["dedupe_key"] for row in ledger.query(
            "SELECT dedupe_key FROM dedupe_index WHERE dedupe_key LIKE 'bb:%'")]
        self.assertEqual(3, len(keys))
        self.assertEqual(3, len(set(keys)))

    def test_noncheckpoint_prefix_deletion_resume(self):
        paths = []
        for attempt, order in (("att-a", 1), ("att-b", 2), ("att-c", 3)):
            path = self.write(f"verify-results/{attempt}.json", _verify_record(attempt))
            os.utime(path, (order, order))
            paths.append(path)
        s = source.BundlebeesSource(self.state)
        page1 = s.read_batch(None, 1, 4096)
        page2 = s.read_batch(page1["next_cursor"], 1, 4096)
        Path(paths[0]).unlink()
        resumed = source.BundlebeesSource(self.state).read_batch(page2["next_cursor"], 1, 4096)
        self.assertEqual(["att-c"], [row["scope"]["attempt_id"] for row in resumed["records"]])
        self.assertFalse(resumed["has_more"])

    def test_checkpoint_record_deleted_is_source_gap(self):
        checkpoint = self.write("verify-results/att-a.json", _verify_record("att-a"))
        self.write("verify-results/att-b.json", _verify_record("att-b"))
        s = source.BundlebeesSource(self.state)
        page = s.read_batch(None, 1, 4096)
        self.assertEqual("r1." + page["records"][-1]["dedupe_key"], page["next_cursor"])
        Path(checkpoint).unlink()
        with self.assertRaises(source.SourceError) as err:
            source.BundlebeesSource(self.state).read_batch(page["next_cursor"], 1, 4096)
        self.assertEqual("source_gap", err.exception.code)

    def test_unknown_identity_is_source_gap(self):
        self.write("verify-results/att-a.json", _verify_record("att-a"))
        s = source.BundlebeesSource(self.state)
        with self.assertRaises(source.SourceError) as err:
            s.read_batch("r1.unknown", 1, 4096)
        self.assertEqual("source_gap", err.exception.code)
        for cursor in ("r1.", "r1.bad/path"):
            with self.subTest(cursor=cursor), self.assertRaises(source.SourceError) as invalid:
                s.read_batch(cursor, 1, 4096)
            self.assertEqual("invalid_cursor", invalid.exception.code)

    def test_legacy_exact_replay(self):
        for attempt, order in (("att-a", 1), ("att-b", 2), ("att-c", 3)):
            path = self.write(f"verify-results/{attempt}.json", _verify_record(attempt))
            os.utime(path, (order, order))
        s = source.BundlebeesSource(self.state)
        legacy = _legacy_cursor(s, 1, 3)
        expected = dict(schema="rizzstack.rails-read-batch-result/1", records=[_wire_rows(s)[1]],
                        next_cursor=_legacy_cursor(s, 2, 3), has_more=True,
                        source_head=_legacy_cursor(s, 3, 3), diagnostics=[])
        self.assertEqual(expected, s.read_batch(legacy, 1, 4096))
        self.assertEqual(expected, source.BundlebeesSource(self.state).read_batch(legacy, 1, 4096))

    def test_legacy_chain_mismatch_is_source_gap(self):
        for attempt, order in (("att-b", 2), ("att-c", 3)):
            path = self.write(f"verify-results/{attempt}.json", _verify_record(attempt))
            os.utime(path, (order, order))
        s = source.BundlebeesSource(self.state)
        legacy = _legacy_cursor(s, 1, 2)
        inserted = self.write("verify-results/att-a.json", _verify_record("att-a"))
        os.utime(inserted, (1, 1))
        with self.assertRaises(source.SourceError) as err:
            source.BundlebeesSource(self.state).read_batch(legacy, 1, 4096)
        self.assertEqual("source_gap", err.exception.code)

    def test_identity_cursor_charset_length_and_importer_validation(self):
        from rizzstack_rails.importer import Importer

        self.write("verify-results/att-a.json", _verify_record("att-a"))
        s = source.BundlebeesSource(self.state)
        page = s.read_batch(None, 1, 4096)
        cursor = page["next_cursor"]
        self.assertEqual("r1." + page["records"][-1]["dedupe_key"], cursor)
        self.assertLessEqual(len(cursor), 1024)
        self.assertRegex(cursor, r"\Ar1\.[A-Za-z0-9._:=+~-]+\Z")
        self.assertEqual(cursor, page["source_head"])

        ledger = rails_ledger.RailsLedger(os.path.join(self.state, "rails.sqlite"))
        self.addCleanup(ledger.close)
        imp = Importer(ledger)
        result = imp.commit_batch(s.adapter_id, s.instance, [], expected_cursor=None, next_cursor=cursor)
        self.assertEqual(0, result["imported"])
        self.assertEqual(cursor, imp.cursor(s.adapter_id, s.instance))

    def test_appends_during_paging_wait_for_next_head(self):
        def put(n):
            attempt = f'att-{n}'
            path = self.write(f'verify-results/{attempt}.json', _verify_record(attempt))
            os.utime(path, (n, n))

        put(1)
        put(2)
        s = source.BundlebeesSource(self.state)
        page1 = s.read_batch(None, 1, 4096)
        page1 = dict(page1, next_cursor=_legacy_cursor(s, 1, 2),
                     source_head=_legacy_cursor(s, 2, 2))
        page2 = s.read_batch(page1['next_cursor'], 1, 4096)
        put(3)
        restarted = source.BundlebeesSource(self.state)
        self.assertEqual(page2, restarted.read_batch(page1['next_cursor'], 1, 4096))
        self.assertFalse(page2['has_more'])
        self.assertEqual(page1['source_head'], page2['next_cursor'])
        page3 = restarted.read_batch(page2['next_cursor'], 1, 4096)
        self.assertEqual('att-3', page3['records'][0]['scope']['attempt_id'])
        self.assertNotEqual(page3['source_head'], page1['source_head'])

    def test_identity_resume_uses_current_snapshot_head(self):
        def put(n):
            attempt = f'att-{n}'
            path = self.write(f'verify-results/{attempt}.json', _verify_record(attempt))
            os.utime(path, (n, n))

        put(1)
        put(2)
        s = source.BundlebeesSource(self.state)
        page1 = s.read_batch(None, 1, 4096)
        put(3)
        restarted = source.BundlebeesSource(self.state)
        page2 = restarted.read_batch(page1['next_cursor'], 1, 4096)
        self.assertEqual('att-2', page2['records'][0]['scope']['attempt_id'])
        self.assertTrue(page2['has_more'])
        page3 = restarted.read_batch(page2['next_cursor'], 1, 4096)
        self.assertEqual('att-3', page3['records'][0]['scope']['attempt_id'])
        self.assertFalse(page3['has_more'])
        self.assertEqual(page3['next_cursor'], page2['source_head'])

    def test_restart_after_committed_page_and_prefix_loss(self):
        from rizzstack_rails.importer import Importer

        self.write('verify-results/att-1.json', _verify_record())
        path = self.write('waves/w1.json', _wave_record())
        s = source.BundlebeesSource(self.state)
        desc = s.describe()
        page = s.read_batch(None, 1, 4096)
        self.assertTrue(page['has_more'])
        self.assertEqual(page, s.read_batch(None, 1, 4096))
        db = os.path.join(self.state, 'rails.sqlite')
        for expected in [1, 1, 0]:
            ledger = rails_ledger.RailsLedger(db)
            try:
                imp = Importer(ledger, authorities=('coordinator_record', 'imported_observation'))
                result = imp.import_once(source.BundlebeesSource(self.state), limit=1)
                self.assertEqual(expected, result['imported'])
                cursor = imp.cursor(desc['adapter_id'], desc['source_instance'])
            finally:
                ledger.close()
        Path(path).unlink()
        with self.assertRaises(source.SourceError) as err:
            source.BundlebeesSource(self.state).read_batch(cursor, 1, 4096)
        self.assertEqual('source_gap', err.exception.code)

    def test_projection_and_diagnostics_omit_private_source_content(self):
        r = _review_record()
        secret = 'private review on workstation'
        r['notes'] = r['produced_by']['name'] = r['attestation']['reviewer_label'] = secret
        self.write('evidence/contract-1/0003-review.json', tdd.sign_record(r, TEST_SEAL))
        s = source.BundlebeesSource(self.state, key=TEST_SEAL)
        self.assertEqual('live', s.health()['state'])
        for obj in (s.read_batch(), s.describe(), s.health()):
            text = json.dumps(obj)
            for needle in (secret, 'evidence/contract-1', 'tests/test_x', self.state):
                self.assertNotIn(needle, text)
        bad = self.write('verify-results/private name.json', {})
        Path(bad).write_text(secret)
        page = s.read_batch()
        self.assertIn('source.error', [r['kind'] for r in page['records']])
        self.assertNotIn(secret, json.dumps(page))
        self.assertNotIn(DIGEST(secret), json.dumps(page))
        stat = Path(bad).stat()
        Path(bad).write_text('another private fixture')
        os.utime(bad, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(page, s.read_batch())  # Includes source_head and next_cursor.
        self.assertNotIn('private name', json.dumps(page))
        for limit, cap in [(0, 4096), (501, 4096), (True, 4096), (5, 2**21), (1, 100)]:
            with self.assertRaises(source.SourceError):
                s.read_batch(None, limit, cap)

    def test_anonymous_snapshot_error_does_not_fingerprint_rejected_fields(self):
        def project(note):
            return source.BundlebeesSource(snapshot=[dict(note=note)]).read_batch()

        first = project('fixture one')
        self.assertEqual('missing_revision', first['records'][0]['payload']['code'])
        self.assertIsNone(first['records'][0]['payload']['digest'])
        self.assertEqual(first, project('fixture two'))

    def test_symlinked_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as other:
            Path(self.state, 'waves').symlink_to(other, target_is_directory=True)
            s = source.BundlebeesSource(self.state)
            self.assertEqual('unavailable', s.health()['state'])
            with self.assertRaises(source.SourceError):
                s.read_batch()

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
