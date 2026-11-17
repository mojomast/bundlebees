"""Rails R02 acceptance tests (RED-first).

The four-checkout test also runs the real Hyvv emitter/exporter with this
runtime and Workflow's strict validator against both frozen schemas.
"""

import hashlib
import json
import os
import platform
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_old_row_and_retry_are_unchanged_across_new_append(self):
        # Seed precisely the pre-freeze bytes/index, independently of today's
        # emitter. Exercise *both* chains and the original receipt after reopen.
        led = self.ledger()
        args = self.base_args()
        body = {k: v for k, v in args.items() if k != 'dedupe_key'}
        body.update(schema='rizzstack.rails/1', chain_id=led.chain_id,
                    lane=dict(lane_id=None, parent_event_id=None, parent_lane_id=None),
                    occurred_at=None, duration_ms=None, timing_quality='unknown',
                    redaction_policy={'mode':'structured_safe', 'path_detail':'omitted'})
        old_sha = hashlib.sha256(rails_contracts.canonicalize(body)).hexdigest()
        eid = led.chain_id + ':1'
        old = dict(body, lane=dict(body['lane'], orphan=False), event_id=eid, seq=1,
                   prev_hash='genesis', recorded_at='2026-01-01T00:00:01.000000Z')
        old['hash'] = _ref_rails_hash('genesis', old)
        text = rails_contracts.canonicalize(old).decode('ascii')
        row = dict(ingest_seq=1, chain_id=led.chain_id, seq=1, event_id=eid, id=eid,
                   envelope_json=text, ts=old['recorded_at'], task_id='task-1', actor='worker_report',
                   parent='', kind='decision.recorded', ac_id='', result='proceed', why=text,
                   tokens_in='', tokens_out='', prev_hash='genesis', sha=old['hash'], rails_hash=old['hash'])
        row['hash'] = rails_contracts.legacy_hash('genesis', row)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT INTO event_rows ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                         tuple(row.values()))
            conn.execute('INSERT INTO dedupe_index VALUES (?,?,?,?)', (led.chain_id, 'dedupe-1', eid, old_sha))
        led.close()
        led = self.ledger()
        self.addCleanup(led.close)
        replay = led.append(**args)
        self.assertEqual((replay['duplicated'], replay['hash']), (True, old['hash']))
        with self.assertRaises(rails_ledger.IdempotencyConflict):
            led.append(**dict(args, payload={'decision':'changed'}))
        led.append(**self.base_args(kind='usage.observed', dedupe_key='next', lane={'parent_event_id':eid},
            payload=dict(usage_record_id='u1', metric_scope='task-1', basis='final', tokens_in=12,
                         tokens_out=None, tokens_thinking=None, provider='p', model='m')))
        first, second = led.query('SELECT * FROM event_rows ORDER BY seq')
        self.assertEqual(row, first)
        self.assertEqual(json.loads(second['why'])['prev_hash'], old['hash'])
        self.assertEqual(second['prev_hash'], row['hash'])
        self.assertEqual((second['parent'], second['tokens_in'], second['tokens_out']), (eid, 12, ''))
        self.assertTrue(led.verify_chain()['legacy_ok'])
        self.assertTrue(led.verify_chain()['ok'])


class FrozenExportTests(RailsCase):
    def test_actual_worker_export_validates_and_imports_verbatim(self):
        root = Path(os.environ.get('RAILS_R18_ROOT', Path(REPO_ROOT).parent)).resolve()
        checkouts = [root / repo for repo in ('workflow', 'hyvv', 'bundlebees', 'orbitdesktop')]
        if not all(p.is_dir() for p in checkouts):
            if os.environ.get('RAILS_R18_ROOT') or os.environ.get('RAILS_R18_REQUIRED') == '1':
                self.fail('four-checkout fixture incomplete')
            self.skipTest('requires sibling Workflow, Hyvv, BundleBees and Orbit checkouts')
        workflow, hyvv, _, _ = checkouts
        frozen = runpy.run_path(str(workflow / 'tests/test_rails_contract.py'))
        schemas = workflow / 'schemas/rails'
        load, validate = frozen['load_json'], frozen['validate_schema']
        event_schema = load(schemas / 'event.schema.json')
        payloads = load(schemas / 'payload.schema.json')['properties']['kinds']['properties']
        root = Path(self._tmp.name)
        (root / 'attempts').mkdir()
        aid = 'att_1234567890abcdef'
        rows = [dict(lseq=1, kind='worker:tool_call', tool_id='call_1234567890abcdef',
                     line='[tool read] read', text='private fixture prompt'),
                dict(lseq=2, kind='worker:tool_update', tool_id='call_1234567890abcdef', tool_status='completed')]
        (root / 'attempts' / (aid + '.events.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        # Use the checkouts' actual modules, intentionally against the runtime
        # under test (which may not have been vendor-refreshed into Hyvv yet).
        with patch.object(sys, 'path', [str(hyvv), *sys.path]):
            from rails_emitter import RailsEmitter
            from rails_export import export_page
            worker = RailsEmitter(root)
            worker.ingest(aid)
            page = export_page(worker.path)
        self.assertEqual(validate(page, load(schemas / 'http.schema.json')['properties']['export_page']), [])
        events = page['events']
        self.assertEqual([e['kind'] for e in events], ['tool.start','tool.finish','source.checkpoint'])
        origin = [frozen['canonical'](e).decode('ascii') for e in events]
        with sqlite3.connect(worker.path) as conn:
            self.assertEqual(origin, [r[0] for r in conn.execute('SELECT why FROM event_rows ORDER BY seq')])
        db = self.ledger()
        self.addCleanup(db.close)
        db.import_origin_page(page['chain_id'], origin)
        self.assertEqual(db.query('SELECT origin_bytes FROM imported_events ORDER BY seq'),
                         [{'origin_bytes': text} for text in origin])
        checkpoint, = db.query('SELECT envelope_json FROM event_rows')
        for env in [*events, json.loads(checkpoint['envelope_json'])]:
            with self.subTest(kind=env['kind']):
                self.assertEqual(validate(env, event_schema), [])
                self.assertEqual(validate(env['payload'], payloads[env['kind']]), [])
                self.assertEqual(env['hash'], frozen['rails_hash'](env['prev_hash'], frozen['strip_hash'](env)))

    def test_published_identifiers_policy_and_closed_payload(self):
        db = self.ledger()
        self.addCleanup(db.close)
        db.append(**self.base_args(scope={'repo_slug':'a/b', 'job_id':'j'}, timing_quality='reported',
                                   redaction_policy='rails-safe/1'))
        env = json.loads(db.query('SELECT why FROM events')[0]['why'])
        self.assertEqual(env['scope'], dict.fromkeys(rails_contracts.SCOPE_FIELDS) | {'repo':'a/b','job_id':'j'})
        self.assertEqual(env['timing_quality'], 'estimated')
        self.assertEqual(env['dedupe_key'], 'dedupe-1')
        self.assertEqual(env['payload'], {'summary':'proceed'})
        for bad in (dict(dedupe_key=None), dict(dedupe_key='/private/some_id'),
                    dict(scope={'task_id':'/private', 'code_rev':'main'}), dict(scope={'repo':'a/b'}),
                    dict(payload={'summary':'ok', 'unrecognized':'value'}),
                    dict(redaction_policy={'mode':'structured_safe','notes':'unbounded'})):
            with self.subTest(keys=list(bad)), self.assertRaises(rails_contracts.RailsRefusal):
                db.append(**self.base_args(**bad))
        self.assertEqual(db.head()['seq'], 1)


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

    def test_raw_even_correctly_hashed_is_refused_before_persistence(self):
        led = self.ledger()
        self.addCleanup(led.close)
        origin = rails_ledger.RailsLedger(os.path.join(self._tmp.name, 'origin.db'))
        self.addCleanup(origin.close)
        for n in range(2):
            origin.append(**self.base_args(dedupe_key=str(n)))
        first, second = [r['why'] for r in origin.query('SELECT why FROM event_rows ORDER BY seq')]
        env = json.loads(second)
        changes = [dict(payload={'summary':'ok', 'raw_tool_output':'planted-marker'}),
                   dict(payload={'summary':'private words copied verbatim from tool output'}),
                   dict(payload={'summary':PLANTED[0][1]}),
                   dict(source=dict(env['source'], record_id='/private/filename')),
                   dict(redaction_policy={'mode':'structured_safe', 'notes':'planted-marker'}),
                   dict(payload={'summary':'ok', 'authority':'owner_record'}),
                   dict(authority='owner_record'), dict(authority='verifier_record')]
        for override in changes:
            bad = dict(env, **override)
            del bad['hash']
            bad['hash'] = _ref_rails_hash(bad['prev_hash'], bad)
            with self.subTest(fields=list(override)), self.assertRaises(rails_contracts.RailsRefusal):
                led.import_origin_page(origin.chain_id, [first, rails_contracts.canonicalize(bad).decode()])
        for stream in ([first] * 501, ['x' * 65537], ['x' * 65536] * 17, ['[' * 1024 + ']' * 1024]):
            with self.assertRaises(rails_contracts.RailsRefusal):
                led.import_origin_page(origin.chain_id, stream)
        self.assertEqual(led.query('SELECT * FROM imported_origin_rows'), [])
        self.assertEqual(led.query('SELECT * FROM import_cursors'), [])
        self.assertEqual(led.head()['seq'], 0)
        led.import_origin_page(origin.chain_id, [first])
        forged = dict(json.loads(first), payload={'summary':'another'})  # reuse the stored hash
        with self.assertRaises(rails_contracts.RailsRefusal):
            led.import_origin_page(origin.chain_id, [rails_contracts.canonicalize(forged).decode()])
        self.assertEqual(led.import_cursor(origin.chain_id)['next_seq'], 2)


class AuthorityTests(RailsCase):
    def test_self_label_outside_mapping_refused(self):
        led = self.ledger(allowed_authorities={"worker_report"})
        with self.assertRaises(rails_ledger.AuthorityRefusal):
            led.append(**self.base_args(authority="owner_record"))
        led.append(**self.base_args(dedupe_key="ok-worker"))

    def test_default_accepts_full_trust_roster(self):
        led = self.ledger()
        led.append(**self.base_args(authority="verifier_record", dedupe_key="v1"))

    def test_no_imported_or_worker_approval(self):
        led = self.ledger()
        self.addCleanup(led.close)
        target = led.append(**self.base_args())['event_id']
        for authority in ('worker_report', 'imported_observation'):
            request = dict(target_event_id=target, request_id='r', operation='retry',
                           queue_action_id=None, decision='approved', reason='ok', authority_ref='owner')
            acceptance = dict(ac_id='a', run_id='r', phase='integration', code_rev='a'*40,
                              contract_sha256='a'*64, verdict='PASS', evidence_refs=['e'], reason='')
            for kind, payload in [('intervention.result', request), ('acceptance.result', acceptance)]:
                with self.assertRaises(rails_ledger.AuthorityRefusal):
                    led.append(**self.base_args(kind=kind, payload=payload, authority=authority, dedupe_key='claim'))
        self.assertEqual(led.head()['seq'], 1)


class PayloadRuleTests(RailsCase):
    def _ver(self, **over):
        payload = {
            "ac_id": "ac-1",
            "contract_sha256": "a" * 64,
            "code_rev": "b" * 40,
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
                    expected_intent_ref="intent:t1",
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
