"""Rails R02 acceptance tests (RED-first).

Hash vectors below are self-derived in this workspace from the frozen formula
in spec section 4; R01's schemas/rails/fixtures/hash-vectors.json is not
readable here, so R17/R18 integration cross-applies R01's vectors later.
"""

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
if CONTRIB_DIR not in sys.path:
    sys.path.insert(0, CONTRIB_DIR)

import rizzstack_rails
from rizzstack_rails import contracts as rails_contracts
from rizzstack_rails import ledger as rails_ledger
from rizzstack_rails import privacy as rails_privacy


def _ref_rails_hash(prev, envelope):
    blob = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(b"rizzstack.rails/1\n" + prev.encode("ascii") + b"\n" + blob).hexdigest()


def _origin_stream(chain_alias, chain_uuid, n):
    chain_id = chain_alias + "-" + chain_uuid
    prev = "genesis"
    pages = []
    for seq in range(1, n + 1):
        env = {
            "schema": rails_contracts.SCHEMA,
            "chain_id": chain_id,
            "seq": seq,
            "event_id": chain_id + ":" + str(seq),
            "prev_hash": prev,
            "kind": "decision.recorded",
            "source": {
                "adapter": "origin",
                "instance": "inst-origin",
                "record_id": "origin-rec-" + str(seq),
                "revision": "rev-origin-1",
            },
            "scope": {"task_id": "origin-task"},
            "lane": {"lane_id": None, "parent_lane_id": None, "parent_event_id": None},
            "occurred_at": None,
            "recorded_at": "2026-01-01T00:00:00.000001Z",
            "duration_ms": None,
            "timing_quality": "unknown",
            "authority": "imported_observation",
            "payload": {"decision": "origin-" + str(seq)},
            "redaction_policy": {"mode": "structured_safe", "path_detail": "omitted"},
        }
        env["hash"] = rails_contracts.rails_hash(prev, env)
        prev = env["hash"]
        pages.append(rails_contracts.canonicalize(env).decode("ascii"))
    return chain_id, pages


PLANTED = (
    ("assign_style", "pass" + "word=hunter2example", "hunter2example"),
    ("assigned_key_style", "api" + "_key=" + "A" * 24, "A" * 24),
    ("prefix_style", "sk" + "-" + "Z" * 24, "Z" * 24),
    ("cloud_style", "AK" + "IA" + "IOSFODNN7EXAMPLE", "IOSFODNN7EXAMPLE"),
    ("claim_style", "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxIn0.c2ln", "hbGciOiJIUzI1NiJ9"),
    ("carry_style", "bea" + "rer " + "q" * 12 + "yW5kIHN0YXRl", "yW5kIHN0YXRl"),
)

CHILD_AFTER_COMMIT = "\n".join(
    [
        "import os, sys",
        "sys.path.insert(0, sys.argv[1])",
        "import rizzstack_rails",
        "led = rizzstack_rails.RailsLedger(sys.argv[2], hooks={'after_commit': lambda: os._exit(9)})",
        "led.append(kind='decision.recorded',"
        " source={'adapter':'c','instance':'i','record_id':'crash-rec','revision':'rev-1'},"
        " scope={'task_id':'crash-task'}, payload={'decision':'committed'},"
        " authority='worker_report', dedupe_key='crash-key')",
        "print('acked')",
    ]
)

CHILD_BEFORE_COMMIT = CHILD_AFTER_COMMIT.replace("'after_commit'", "'before_commit'")


class RailsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rails-r02-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "rails.sqlite3")

    def ledger(self, **kwargs):
        return rails_ledger.RailsLedger(self.db_path, **kwargs)

    def base_args(self, **over):
        args = {
            "kind": "decision.recorded",
            "source": {
                "adapter": "unittest",
                "instance": "inst-1",
                "record_id": "rec-1",
                "revision": "rev-1",
            },
            "scope": {"task_id": "task-1"},
            "payload": {"decision": "proceed"},
            "authority": "worker_report",
            "dedupe_key": "dedupe-1",
        }
        args.update(over)
        return args


class IdentityTests(RailsCase):
    def test_identity_is_random_alias_and_uuid_never_hostname(self):
        led = self.ledger()
        alias, uid = led.machine_alias, led.ledger_uuid
        self.assertTrue(alias and uid)
        host = platform.node()
        if host:
            self.assertNotEqual(alias, host)
            raw = open(self.db_path, "rb").read()
            self.assertNotIn(host.encode("utf-8"), raw)
        again = self.ledger()
        self.assertEqual(alias, again.machine_alias)
        self.assertEqual(uid, again.ledger_uuid)
        self.assertEqual(alias + "-" + uid, led.chain_id)

    def test_event_ids_and_contiguous_seq_from_one(self):
        led = self.ledger()
        for i in (1, 2, 3):
            r = led.append(**self.base_args(dedupe_key="k%d" % i, payload={"decision": "d%d" % i}))
            self.assertEqual(r["event_id"], led.chain_id + ":" + str(i))
            self.assertEqual(r["seq"], i)
        v = led.verify_chain()
        self.assertTrue(v["ok"])
        self.assertTrue(v["legacy_ok"])

    def test_scope_requires_at_least_one_grouping_id(self):
        led = self.ledger()
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(**self.base_args(scope={}))

    def test_record_id_path_never_persisted_raw(self):
        led = self.ledger()
        led.append(
            **self.base_args(
                source={
                    "adapter": "a",
                    "instance": "i",
                    "record_id": "/var/tmp/report.json",
                    "revision": "r1",
                }
            )
        )
        row = led.query("SELECT envelope_json FROM event_rows")[0]
        self.assertNotIn("/var/tmp", row["envelope_json"])
        self.assertTrue(
            json.loads(row["envelope_json"])["source"]["record_id"].startswith("obs:")
        )


class HashVectorTests(RailsCase):
    def _vec(self, **over):
        env = {
            "schema": rails_contracts.SCHEMA,
            "chain_id": "chain-x",
            "seq": 1,
            "event_id": "chain-x:1",
            "prev_hash": "genesis",
            "kind": "decision.recorded",
            "payload": {"decision": "go"},
            "occurred_at": "2026-01-01T00:00:00.000000Z",
        }
        env.update(over)
        return env

    def test_positive_vectors(self):
        cases = (
            ("unicode_text", {"payload": {"decision": "caf\xc3\xa9 \xe2\x98\x95".encode("utf-8").decode("utf-8")}}),
            ("null_occurred_at", {"occurred_at": None}),
            ("integer_boundary", {"payload": {"n": 9007199254740991}}),
            ("negative_boundary", {"payload": {"n": -9007199254740991}}),
            ("unexpected_extra_field", {"extension_note": {"committed": [1, True, None]}}),
        )
        for name, over in cases:
            env = self._vec(**over)
            digest = rails_contracts.rails_hash("genesis", env)
            self.assertEqual(digest, _ref_rails_hash("genesis", env), name)
            reordered = {k: env[k] for k in reversed(list(env.keys()))}
            self.assertEqual(digest, rails_contracts.rails_hash("genesis", reordered), name)
        self.assertIn("extension_note", self._vec(**cases[4][1]))
        stripped = {k: v for k, v in self._vec(**cases[4][1]).items() if k != "extension_note"}
        self.assertNotEqual(
            rails_contracts.rails_hash("genesis", self._vec(**cases[4][1])),
            rails_contracts.rails_hash("genesis", stripped),
        )

    def test_negative_vectors(self):
        env = self._vec(payload={"decision": "caf\xc3\xa9 \xe2\x98\x95".encode("utf-8").decode("utf-8")})
        mutated = self._vec(payload={"decision": "cafe"})
        self.assertNotEqual(
            rails_contracts.rails_hash("genesis", env),
            rails_contracts.rails_hash("genesis", mutated),
        )
        for bad in (
            {"x": 1.5},
            {"x": 9007199254740992},
            {"x": -9007199254740992},
            {"x": "ok", "y": "\ud800"},
        ):
            with self.assertRaises(rails_contracts.RailsRefusal):
                rails_contracts.canonicalize(bad)
        with self.assertRaises(rails_contracts.RailsRefusal):
            rails_contracts.canonicalize_text('{"a":1,"a":2}')

    def test_ledger_row_hash_matches_formula(self):
        led = self.ledger()
        r = led.append(**self.base_args(payload={"decision": "go"}))
        row = led.query(
            "SELECT envelope_json, rails_hash, prev_hash, seq FROM event_rows WHERE seq = 1"
        )[0]
        env = json.loads(row["envelope_json"])
        without_hash = {k: v for k, v in env.items() if k != "hash"}
        self.assertEqual(row["rails_hash"], _ref_rails_hash(row["prev_hash"], without_hash))
        self.assertEqual(row["rails_hash"], r["hash"])
        self.assertEqual(row["prev_hash"], "genesis")

    def test_legacy_hash_matches_upstream_formula(self):
        led = self.ledger()
        led.append(**self.base_args(payload={"decision": "go"}))
        row = led.query("SELECT * FROM event_rows WHERE seq = 1")[0]
        manual = {k: row[k] for k in rails_contracts.LEGACY_CHAIN_KEYS}
        expect = hashlib.sha256(
            row["prev_hash"].encode() + json.dumps(manual, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.assertEqual(row["hash"], expect)
        self.assertEqual(row["why"], row["envelope_json"])

    def test_events_view_columns(self):
        led = self.ledger()
        led.append(**self.base_args())
        cols = [r["name"] for r in led.query("PRAGMA table_info(events)")]
        self.assertEqual(
            cols,
            [
                "id",
                "ts",
                "task_id",
                "actor",
                "parent",
                "kind",
                "ac_id",
                "result",
                "why",
                "tokens_in",
                "tokens_out",
                "sha",
                "prev_hash",
                "hash",
            ],
        )
        view_row = led.query("SELECT id, ts, kind, why, sha, prev_hash, hash FROM events")[0]
        self.assertEqual(view_row["id"], led.chain_id + ":1")


class ConcurrencyTests(RailsCase):
    def test_ten_concurrent_writers_serialize_with_commit_before_ack(self):
        n_threads, per = 10, 5
        barrier = threading.Barrier(n_threads)
        errors = []

        def worker(t):
            try:
                led = self.ledger()
                barrier.wait(timeout=30)
                for i in range(per):
                    led.append(
                        **self.base_args(
                            dedupe_key="t%d-%d" % (t, i),
                            payload={"decision": "t%d-%d" % (t, i)},
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errors, [])
        led = self.ledger()
        rows = led.query("SELECT seq FROM event_rows ORDER BY seq")
        self.assertEqual([r["seq"] for r in rows], list(range(1, n_threads * per + 1)))
        self.assertTrue(led.verify_chain()["ok"])

    def test_identical_retry_returns_original_receipt(self):
        led = self.ledger()
        first = led.append(**self.base_args())
        again = led.append(**self.base_args())
        self.assertEqual(first["event_id"], again["event_id"])
        self.assertEqual(first["hash"], again["hash"])
        self.assertTrue(again["duplicated"])
        self.assertFalse(first["duplicated"])
        self.assertEqual(len(led.query("SELECT seq FROM event_rows")), 1)

    def test_conflicting_retry_is_409(self):
        led = self.ledger()
        led.append(**self.base_args())
        with self.assertRaises(rails_ledger.IdempotencyConflict) as ctx:
            led.append(**self.base_args(payload={"decision": "changed"}))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(len(led.query("SELECT seq FROM event_rows")), 1)

    def _run_child(self, code):
        return subprocess.run(
            [sys.executable, "-c", code, CONTRIB_DIR, self.db_path],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_crash_after_commit_loses_nothing_acknowledged(self):
        led = self.ledger()
        led.append(**self.base_args(dedupe_key="seed"))
        proc = self._run_child(CHILD_AFTER_COMMIT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("acked", proc.stdout)
        reopened = self.ledger()
        self.assertEqual(len(reopened.query("SELECT seq FROM event_rows")), 2)
        retry = reopened.append(
            **self.base_args(
                dedupe_key="crash-key",
                source={
                    "adapter": "c",
                    "instance": "i",
                    "record_id": "crash-rec",
                    "revision": "rev-1",
                },
                scope={"task_id": "crash-task"},
                payload={"decision": "committed"},
            )
        )
        self.assertTrue(retry["duplicated"])
        self.assertEqual(retry["event_id"], reopened.chain_id + ":2")
        self.assertTrue(reopened.verify_chain()["ok"])

    def test_crash_before_commit_leaves_head_unchanged(self):
        led = self.ledger()
        led.append(**self.base_args(dedupe_key="seed"))
        proc = self._run_child(CHILD_BEFORE_COMMIT)
        self.assertNotEqual(proc.returncode, 0)
        reopened = self.ledger()
        rows = reopened.query("SELECT envelope_json FROM event_rows")
        self.assertEqual(len(rows), 1)
        self.assertNotIn("crash-key", json.dumps(rows))
        self.assertTrue(reopened.verify_chain()["ok"])


class HealthTests(RailsCase):
    def test_full_disk_surfaces_unhealthy_and_rejects_appends(self):
        led = self.ledger(min_free_bytes=10 ** 15)
        with self.assertRaises(rails_ledger.LedgerUnavailable):
            led.append(**self.base_args())
        self.assertFalse(led.health()["healthy"])
        self.assertEqual(len(led.query("SELECT seq FROM event_rows")), 0)
        working = self.ledger()
        working.append(**self.base_args())
        self.assertTrue(working.health()["healthy"])


class BackupTests(RailsCase):
    def test_backup_restore_validates_both_chains(self):
        led = self.ledger()
        for i in range(3):
            led.append(
                **self.base_args(dedupe_key="k%d" % i, payload={"decision": "d%d" % i})
            )
        restore_path = os.path.join(self._tmp.name, "restore.sqlite3")
        report = led.backup(restore_path)
        self.assertTrue(report["ok"])
        restored = rails_ledger.RailsLedger(restore_path, read_only=True)
        v = restored.verify_chain()
        self.assertTrue(v["ok"])
        self.assertTrue(v["legacy_ok"])
        conn = sqlite3.connect(restore_path)
        conn.executescript(
            "DROP TRIGGER event_rows_no_update;"
            " DROP TRIGGER event_rows_no_delete;"
        )
        conn.execute(
            "UPDATE event_rows SET envelope_json = replace(envelope_json, 'd1', 'dX')"
            " WHERE seq = 2"
        )
        conn.commit()
        conn.close()
        v2 = rails_ledger.RailsLedger(restore_path, read_only=True).verify_chain()
        self.assertFalse(v2["ok"])
        conn = sqlite3.connect(restore_path)
        conn.execute(
            "UPDATE event_rows SET envelope_json = replace(envelope_json, 'dX', 'd1')"
            " WHERE seq = 2"
        )
        conn.execute("UPDATE event_rows SET why = why || ' ' WHERE seq = 2")
        conn.commit()
        conn.close()
        v3 = rails_ledger.RailsLedger(restore_path, read_only=True).verify_chain()
        self.assertTrue(v3["ok"])
        self.assertFalse(v3["legacy_ok"])


class ReadonlyTests(RailsCase):
    def test_missing_db_refused(self):
        with self.assertRaises(rails_ledger.ReadonlyRefusal):
            rails_ledger.RailsLedger(
                os.path.join(self._tmp.name, "absent.sqlite3"), read_only=True
            )

    def test_readonly_connection_is_query_only(self):
        led = self.ledger()
        led.append(**self.base_args())
        ro = rails_ledger.RailsLedger(self.db_path, read_only=True)
        self.assertEqual(ro.query("PRAGMA query_only")[0]["query_only"], 1)
        with self.assertRaises(rails_ledger.ReadonlyRefusal):
            ro.append(**self.base_args())
        self.assertEqual(ro.query("SELECT COUNT(*) AS c FROM events")[0]["c"], 1)


class PrivacyTests(RailsCase):
    def test_scrub_text_removes_every_shape(self):
        for name, planted, marker in PLANTED:
            out = rails_privacy.scrub_text(planted)
            self.assertNotIn(marker, out, name)
            self.assertIn("[redacted]", out, name)

    def test_planted_strings_never_reach_db_or_output(self):
        led = self.ledger()
        for name, planted, marker in PLANTED:
            led.append(
                **self.base_args(
                    dedupe_key="pk-" + name,
                    payload={"decision": "note", "summary": "before " + planted + " after"},
                )
            )
        raw = open(self.db_path, "rb").read()
        for name, planted, marker in PLANTED:
            self.assertNotIn(marker.encode("utf-8"), raw, name)
        for row in led.query("SELECT envelope_json, why FROM event_rows"):
            for name, planted, marker in PLANTED:
                self.assertNotIn(marker, row["envelope_json"], name)
                self.assertNotIn(marker, row["why"], name)

    def test_raw_transcript_ingestion_refused(self):
        led = self.ledger()
        for key in ("raw_prompt", "raw_response", "reasoning_transcript", "tool_argv"):
            with self.assertRaises(rails_ledger.RawIngestionRefusal):
                led.append(
                    **self.base_args(
                        dedupe_key="raw-" + key, payload={"decision": "x", key: "text"}
                    )
                )
        self.assertEqual(len(led.query("SELECT seq FROM event_rows")), 0)


class ImportTests(RailsCase):
    def _stream(self, n, chain_alias="peer-alias"):
        return _origin_stream(chain_alias, "11111111-2222-3333-4444-555555555555", n)

    def test_pages_import_queryable_and_checkpointed(self):
        led = self.ledger()
        chain_id, pages = self._stream(3)
        out = led.import_origin_page(chain_id, pages[:2])
        self.assertEqual(out["imported"], 2)
        self.assertEqual(out["through_seq"], 2)
        again = led.import_origin_page(chain_id, pages[:2])
        self.assertEqual(again["imported"], 0)
        self.assertTrue(again["duplicated"])
        out2 = led.import_origin_page(chain_id, pages[2:])
        self.assertEqual(out2["through_seq"], 3)
        rows = led.query(
            "SELECT event_id FROM imported_events WHERE chain_id = ? ORDER BY seq",
            (chain_id,),
        )
        self.assertEqual(
            [r["event_id"] for r in rows], [chain_id + ":" + str(i) for i in (1, 2, 3)]
        )
        checks = led.query(
            "SELECT envelope_json FROM event_rows WHERE kind = 'source.checkpoint' ORDER BY seq"
        )
        self.assertEqual(len(checks), 2)
        self.assertTrue(led.verify_chain()["ok"])

    def test_out_of_order_page_requests_missing_prefix(self):
        led = self.ledger()
        chain_id, pages = self._stream(4)
        led.import_origin_page(chain_id, pages[:2])
        with self.assertRaises(rails_ledger.SourceGap):
            led.import_origin_page(chain_id, pages[3:])
        self.assertEqual(led.import_cursor(chain_id)["next_seq"], 3)
        out = led.import_origin_page(chain_id, pages[2:])
        self.assertEqual(out["through_seq"], 4)

    def test_fork_halts_source(self):
        led = self.ledger()
        chain_id, pages = self._stream(2)
        led.import_origin_page(chain_id, pages)
        forked = json.loads(pages[1])
        forked["payload"] = {"decision": "tampered"}
        forked.pop("hash")
        forked["hash"] = rails_contracts.rails_hash(forked["prev_hash"], forked)
        fork_text = rails_contracts.canonicalize(forked).decode("ascii")
        with self.assertRaises(rails_ledger.SourceFork):
            led.import_origin_page(chain_id, [fork_text])
        self.assertEqual(led.import_cursor(chain_id)["state"], "halted")
        with self.assertRaises(rails_ledger.SourceFork):
            led.import_origin_page(chain_id, pages)

    def test_broken_chain_bytes_refused(self):
        led = self.ledger()
        chain_id, pages = self._stream(3)
        bad = json.loads(pages[1])
        bad["payload"] = {"decision": "mutated"}
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.import_origin_page(
                chain_id, [pages[0], rails_contracts.canonicalize(bad).decode("ascii")]
            )
        self.assertEqual(led.import_cursor(chain_id)["next_seq"], 1)
        self.assertEqual(len(led.query("SELECT seq FROM event_rows")), 0)


class AuthorityTests(RailsCase):
    def test_self_label_outside_mapping_refused(self):
        led = self.ledger(allowed_authorities={"worker_report"})
        with self.assertRaises(rails_ledger.AuthorityRefusal):
            led.append(**self.base_args(authority="owner_record"))
        led.append(**self.base_args(dedupe_key="ok-worker"))

    def test_default_accepts_full_trust_roster(self):
        led = self.ledger()
        led.append(**self.base_args(authority="verifier_record", dedupe_key="v1"))


class PayloadRuleTests(RailsCase):
    def _ver(self, **over):
        payload = {
            "ac_id": "ac-1",
            "contract_sha256": "a" * 64,
            "code_rev": "r1",
            "run_id": "run-1",
            "phase": "green",
            "verdict": "PASS",
            "evidence_refs": ["run-1:ok"],
            "reason": None,
        }
        payload.update(over)
        return payload

    def test_pass_requires_evidence(self):
        led = self.ledger()
        led.append(
            **self.base_args(kind="verification.result", payload=self._ver(), dedupe_key="ok")
        )
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="verification.result",
                    payload=self._ver(evidence_refs=[]),
                    dedupe_key="bad",
                )
            )

    def test_non_pass_requires_reason(self):
        led = self.ledger()
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="verification.result",
                    payload=self._ver(verdict="FAIL", evidence_refs=[], reason=None),
                    dedupe_key="f1",
                )
            )
        led.append(
            **self.base_args(
                kind="verification.result",
                payload=self._ver(verdict="FAIL", evidence_refs=[], reason="regression"),
                dedupe_key="f2",
            )
        )

    def test_expected_red_requires_frozen_intent(self):
        led = self.ledger()
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="verification.result",
                    payload=self._ver(
                        phase="red", verdict="EXPECTED_RED", evidence_refs=[], reason="planned"
                    ),
                    dedupe_key="er1",
                )
            )
        led.append(
            **self.base_args(
                kind="verification.result",
                payload=self._ver(
                    phase="red",
                    verdict="EXPECTED_RED",
                    evidence_refs=[],
                    reason="planned",
                    frozen_test_intent={"test_ids": ["t1"], "frozen": True},
                ),
                dedupe_key="er2",
            )
        )


class FeedbackTests(RailsCase):
    def test_note_limit_rating_domain_and_unknown_target(self):
        led = self.ledger()
        target = led.append(**self.base_args(dedupe_key="t0"))["event_id"]
        led.append(
            **self.base_args(
                kind="feedback.recorded",
                dedupe_key="fb1",
                payload={
                    "target_event_id": target,
                    "rating": 1,
                    "note": "ok",
                    "category": "quality",
                    "supersedes_event_id": None,
                },
            )
        )
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="feedback.recorded",
                    dedupe_key="fb2",
                    payload={
                        "target_event_id": target,
                        "rating": 2,
                        "note": "x",
                        "category": "quality",
                    },
                )
            )
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="feedback.recorded",
                    dedupe_key="fb3",
                    payload={
                        "target_event_id": target,
                        "rating": 0,
                        "note": "n" * 2001,
                        "category": "quality",
                    },
                )
            )
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.append(
                **self.base_args(
                    kind="feedback.recorded",
                    dedupe_key="fb4",
                    payload={
                        "target_event_id": led.chain_id + ":99",
                        "rating": 0,
                        "note": "x",
                        "category": "quality",
                    },
                )
            )


class VendorTests(RailsCase):
    def setUp(self):
        super().setUp()
        self.dest = os.path.join(self._tmp.name, "peer")
        self.manifest_path = os.path.join(
            REPO_ROOT, "contrib", "rizzstack_rails", "vendor-manifest.json"
        )

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "vendor_rails.py")]
            + list(args),
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=120,
        )

    def _manifest(self):
        with open(self.manifest_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_copy_is_pinned_and_deterministic(self):
        proc = self._run("--dest", self.dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for entry in self._manifest()["files"]:
            target = os.path.join(self.dest, entry["path"])
            self.assertTrue(os.path.exists(target), entry["path"])
            with open(target, "rb") as fh:
                self.assertEqual(
                    hashlib.sha256(fh.read()).hexdigest(), entry["sha256"], entry["path"]
                )
        self.assertEqual(self._run("--dest", self.dest).returncode, 0)

    def test_rerun_restores_tampered_copy(self):
        self.assertEqual(self._run("--dest", self.dest).returncode, 0)
        target = os.path.join(self.dest, "contrib", "rizzstack_rails", "ledger.py")
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\nstray line\n")
        self.assertEqual(self._run("--dest", self.dest).returncode, 0)
        entry = [e for e in self._manifest()["files"] if e["path"].endswith("ledger.py")][0]
        with open(target, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), entry["sha256"])

    def test_check_mode_passes(self):
        self.assertEqual(self._run("--check").returncode, 0)

    def test_manifest_covers_package(self):
        names = sorted(
            os.path.basename(e["path"]) for e in self._manifest()["files"]
        )
        package_dir = os.path.join(REPO_ROOT, "contrib", "rizzstack_rails")
        package = sorted(
            n for n in os.listdir(package_dir) if n.endswith(".py")
        )
        self.assertEqual(names, package)


if __name__ == "__main__":
    unittest.main()
