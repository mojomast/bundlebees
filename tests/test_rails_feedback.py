"""Rails R11 acceptance tests (RED-first): feedback overlay and CSV export."""

import csv
import io
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
if CONTRIB_DIR not in sys.path:
    sys.path.insert(0, CONTRIB_DIR)

from rizzstack_rails import contracts, export as rails_export, feedback as rails_feedback
from rizzstack_rails import ledger as rails_ledger


SCOPE = {"task_id": "task-r11"}


def _source(record_id):
    return {
        "adapter": "unittest",
        "instance": "inst-r11",
        "record_id": record_id,
        "revision": "rev-1",
    }


def _second_writer(db_path, operation, identifier, kwargs, ready, start, attempting, results):
    """Independent process/connection; signal exactly when SQLite begins its write."""
    ledger = rails_ledger.RailsLedger(db_path)
    try:
        ledger._conn.set_trace_callback(
            lambda sql: attempting.set() if sql == "BEGIN IMMEDIATE" else None)
        ready.set()
        if not start.wait(10):
            raise AssertionError("first writer did not reach commit")
        receipt = getattr(rails_feedback, operation)(ledger, identifier, **kwargs)
        results.put(receipt)
    except contracts.RailsRefusal as exc:
        results.put((exc.status_code, exc.code))
    finally:
        ledger.close()


class FeedbackCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rails-r11-")
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "rails.sqlite3")
        self.ledger = rails_ledger.RailsLedger(self.db_path)
        self.addCleanup(self.ledger.close)

    def target(self, dedupe_key="target-1"):
        return self.ledger.append(
            kind="decision.recorded",
            source=_source("record-" + dedupe_key),
            scope=SCOPE,
            payload={"decision": "proceed"},
            authority="worker_report",
            dedupe_key=dedupe_key,
        )["event_id"]

    def record(self, target=None, **over):
        args = {
            "idempotency_key": "fb-key-1",
            "scope": SCOPE,
            "source": _source("feedback-source"),
            "rating": 1,
            "category": "quality",
            "note": "useful",
        }
        args.update(over)
        return rails_feedback.record(self.ledger, target or self.target(), **args)

    def append_intervention(self, request_id, kind, dedupe_key, target):
        payload = {
            "request_id": request_id,
            "target_event_id": target,
            "reason": "owner retry",
            "authority_ref": "owner-record",
            "operation": "retry",
            "decision": "approved",
            "queue_action_id": None,
        }
        return self.ledger.append(
            kind=kind,
            source=_source("int-source-" + dedupe_key),
            scope=SCOPE,
            payload=payload,
            authority="owner_record",
            dedupe_key=dedupe_key,
        )


class FeedbackRetryTests(FeedbackCase):
    def test_commit_guard_applies_to_new_record_only(self):
        target = self.target()
        def refuse():
            self.assertTrue(self.ledger._conn.in_transaction)
            raise PermissionError('grant ended')

        with self.assertRaises(PermissionError):
            self.record(target=target, authorize_new_record=refuse)
        first = self.record(target=target)
        self.assertFalse(first['duplicated'])
        self.assertEqual(self.record(target=target, authorize_new_record=refuse), dict(first, duplicated=True))
        with self.assertRaises(rails_feedback.FeedbackConflictRefusal):
            self.record(target=target, rating=0, authorize_new_record=refuse)
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)

    def test_retry_duplicate_yields_one_feedback_row(self):
        target = self.target()
        first = self.record(target=target)
        second = self.record(target=target)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertFalse(first["duplicated"])
        self.assertTrue(second["duplicated"])
        rows = rails_feedback.history(self.ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feedback_id"], first["feedback_id"])
        self.assertEqual(rows[0]["revision"], 1)

    def test_same_key_with_different_feedback_is_refused(self):
        target = self.target()
        self.record(target=target, note="first")
        with self.assertRaises(rails_feedback.FeedbackConflictRefusal):
            self.record(target=target, note="second")
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)


class FeedbackEditTests(FeedbackCase):
    def test_stale_edit_refused_with_typed_error(self):
        recorded = self.record(note="revision one")
        edited = rails_feedback.edit(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="edit-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
            note="revision two",
        )
        self.assertEqual(edited["revision"], 2)
        self.assertFalse(edited["duplicated"])
        with self.assertRaises(rails_feedback.StaleEditRefusal):
            rails_feedback.edit(
                self.ledger,
                recorded["feedback_id"],
                base_revision=1,
                idempotency_key="edit-key-2",
                scope=SCOPE,
                source=_source("feedback-source"),
                note="stale write",
            )
        rows = rails_feedback.history(self.ledger)
        self.assertEqual(len(rows), 2)
        latest = rails_feedback.current(self.ledger, recorded["feedback_id"])
        self.assertEqual(latest["note"], "revision two")

    def test_edit_retry_with_same_key_is_idempotent(self):
        recorded = self.record(note="revision one")
        first = rails_feedback.edit(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="edit-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
            note="revision two",
        )
        retried = rails_feedback.edit(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="edit-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
            note="revision two",
        )
        self.assertTrue(retried["duplicated"])
        self.assertEqual(first["event_id"], retried["event_id"])
        self.assertEqual(len(rails_feedback.history(self.ledger)), 2)

    def test_missing_feedback_target_refused(self):
        with self.assertRaises(rails_feedback.TargetMissingRefusal):
            rails_feedback.edit(
                self.ledger,
                "fb-missing",
                base_revision=1,
                idempotency_key="edit-key-missing",
                scope=SCOPE,
                source=_source("feedback-source"),
                note="nowhere",
            )


class FeedbackTargetTests(FeedbackCase):
    def test_record_against_missing_rail_event_refused(self):
        with self.assertRaises(rails_feedback.TargetMissingRefusal):
            self.record(target=self.ledger.chain_id + ":999")
        self.assertEqual(len(rails_feedback.history(self.ledger)), 0)

    def test_imported_origin_target_with_local_edit_clear_and_delayed_retry(self):
        origin = rails_ledger.RailsLedger(os.path.join(self._tmp.name, "origin.sqlite"))
        self.addCleanup(origin.close)
        target = origin.append("decision.recorded", source=_source("obs-1"), scope=SCOPE,
                               payload={"decision": "ok"}, authority="worker_report",
                               dedupe_key="origin-1")["event_id"]
        page = origin.query("SELECT envelope_json FROM event_rows")[0]["envelope_json"]
        self.ledger.import_origin_page(origin.chain_id, [page])
        recorded = self.record(target=target)
        fb = recorded["feedback_id"]
        edit = dict(scope=SCOPE, base_revision=1, idempotency_key="edit", note="better")
        second = rails_feedback.edit(self.ledger, fb, **edit)
        third = rails_feedback.clear(self.ledger, fb, scope=SCOPE, base_revision=2,
                                     idempotency_key="clear")
        fresh = rails_ledger.RailsLedger(self.db_path)
        self.addCleanup(fresh.close)
        self.assertEqual(rails_feedback.edit(fresh, fb, **edit), dict(second, duplicated=True))
        self.assertEqual(self.record(target=target), dict(recorded, duplicated=True))
        rows = rails_feedback.history(fresh, target_event_id=target)
        self.assertEqual([r["supersedes_event_id"] for r in rows],
                         [None, recorded["event_id"], second["event_id"]])
        self.assertEqual(rows[-1]["event_id"], third["event_id"])
        self.assertTrue(fresh.verify_chain(origin.chain_id)["ok"])
        self.assertTrue(fresh.verify_chain()["legacy_ok"])


class AtomicFeedbackTests(FeedbackCase):
    def race(self, method, identifier, first, competing_method, competing):
        ctx = multiprocessing.get_context("spawn")
        ready, start, attempting = (ctx.Event() for _ in range(3))
        results = ctx.Queue()
        proc = ctx.Process(target=_second_writer, args=(
            self.db_path, competing_method, identifier, dict(scope=SCOPE, **competing),
            ready, start, attempting, results))
        proc.start()
        try:
            self.assertTrue(ready.wait(10))

            def before_commit():
                # The second writer begins while our mutation is still uncommitted.
                # A SELECT done outside its transaction would see the old head.
                start.set()
                self.assertTrue(attempting.wait(10))

            self.ledger.hooks["before_commit"] = before_commit
            winner = getattr(rails_feedback, method)(self.ledger, identifier, scope=SCOPE, **first)
            loser = results.get(timeout=10)
            proc.join(10)
            self.assertEqual(proc.exitcode, 0)
            return winner, loser
        finally:
            self.ledger.hooks.pop("before_commit", None)
            if proc.is_alive():
                proc.terminate()
            proc.join(10)
            results.close()
            results.join_thread()

    def test_concurrent_record_of_explicit_id_creates_one_revision(self):
        target = self.target()
        one = dict(feedback_id="fb:chosen", idempotency_key="one")
        _, loser = self.race("record", target, one, "record", dict(one, idempotency_key="two"))
        self.assertEqual(loser, (409, "feedback_conflict"))
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)

    def test_concurrent_clear_and_edit_cannot_fork_revision(self):
        fb = self.record()["feedback_id"]
        first = dict(base_revision=1, idempotency_key="one")
        _, loser = self.race("clear", fb, first, "edit", dict(first, idempotency_key="two"))
        self.assertEqual(loser, (409, "stale_edit"))
        rows = rails_feedback.history(self.ledger)
        self.assertEqual([(r["revision"], r["operation"]) for r in rows], [(1, "record"), (2, "clear")])
        self.assertEqual(rows[1]["supersedes_event_id"], rows[0]["event_id"])

    def test_concurrent_replay_returns_original_receipt(self):
        fb = self.record()["feedback_id"]
        args = dict(base_revision=1, idempotency_key="one", rating=-1)
        winner, replay = self.race("edit", fb, args, "edit", args)
        self.assertFalse(winner["duplicated"])
        self.assertEqual(replay, dict(winner, duplicated=True))
        self.assertEqual(len(rails_feedback.history(self.ledger)), 2)
        self.assertTrue(self.ledger.verify_chain()["ok"])

    def test_key_compares_edit_and_clear_semantic_bytes_even_after_head_moves(self):
        fb = self.record()["feedback_id"]
        args = dict(scope=SCOPE, base_revision=1, idempotency_key="edit", rating=0,
                    source=_source("src"), lane={"lane_id": "lane-a"})
        rails_feedback.edit(self.ledger, fb, **args)
        marker = dict(scope=SCOPE, base_revision=2, idempotency_key="clear")
        rails_feedback.clear(self.ledger, fb, **marker)
        for change in ({"rating": 1}, {"base_revision": 2}, {"note": "different"},
                       {"category": "both"}, {"authority": "worker_report"},
                       {"scope": {"task_id": "other"}}, {"source": _source("other")},
                       {"lane": {"lane_id": "lane-b"}}):
            with self.subTest(change=change), self.assertRaises(rails_feedback.FeedbackConflictRefusal):
                rails_feedback.edit(self.ledger, fb, **dict(args, **change))
        for call, params in ((rails_feedback.clear, dict(marker, note="other")),
                             (rails_feedback.edit, marker),
                             (rails_feedback.clear, dict(marker, idempotency_key="fb-key-1"))):
            with self.assertRaises(rails_feedback.FeedbackConflictRefusal):
                call(self.ledger, fb, **params)
        self.assertEqual(len(rails_feedback.history(self.ledger)), 3)

    def test_validation_and_failures_never_append_or_bypass_ledger_guardrails(self):
        target = self.target()
        fb = self.record(target=target)["feedback_id"]
        args = dict(scope=SCOPE, idempotency_key="bad")
        for invalid in (True, "1", 1.0, None, 0, 2**60):
            with self.assertRaises(rails_feedback.FeedbackRefusal):
                rails_feedback.clear(self.ledger, fb, base_revision=invalid, **args)
        for invalid in ({"source": []}, {"scope": []}, {"note": "a" * 2001},
                        {"note": {"raw_prompt": "no"}}, {"lane": []}, {"rating": True},
                        {"idempotency_key": "unsafe/key"}):
            with self.assertRaises(contracts.RailsRefusal):
                self.record(target=target, **invalid)
        readonly = rails_ledger.RailsLedger(self.db_path, read_only=True)
        self.addCleanup(readonly.close)
        with self.assertRaises(rails_ledger.ReadonlyRefusal):
            rails_feedback.clear(readonly, fb, base_revision=1, **args)
        limited = rails_ledger.RailsLedger(self.db_path, allowed_authorities=["worker_report"])
        self.addCleanup(limited.close)
        with self.assertRaises(rails_ledger.AuthorityRefusal):
            rails_feedback.clear(limited, fb, base_revision=1, **args)
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)

    def test_commit_failure_rolls_back_dedupe_and_lost_ack_can_replay(self):
        fb = self.record()["feedback_id"]
        args = dict(scope=SCOPE, idempotency_key="edit", base_revision=1, note="update")

        def fail():
            raise RuntimeError("interrupted")

        self.ledger.hooks["before_commit"] = fail
        with self.assertRaises(RuntimeError):
            rails_feedback.edit(self.ledger, fb, **args)
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)
        del self.ledger.hooks["before_commit"]
        self.ledger.hooks["after_commit"] = fail
        with self.assertRaises(RuntimeError):
            rails_feedback.edit(self.ledger, fb, **args)
        del self.ledger.hooks["after_commit"]
        retry = rails_feedback.edit(self.ledger, fb, **args)
        self.assertTrue(retry["duplicated"])
        self.assertEqual(len(rails_feedback.history(self.ledger)), 2)

    def test_contended_db_returns_retryable_typed_refusal_then_accepts_same_key(self):
        fb = self.record()["feedback_id"]
        args = dict(scope=SCOPE, idempotency_key="blocked-edit", base_revision=1)
        self.ledger._conn.execute("PRAGMA busy_timeout=1")
        other = sqlite3.connect(self.db_path)
        try:
            other.execute("BEGIN IMMEDIATE")
            with self.assertRaises(rails_feedback.FeedbackBusyRefusal) as error:
                rails_feedback.edit(self.ledger, fb, **args)
            self.assertEqual((error.exception.status_code, error.exception.code), (503, "feedback_busy"))
            self.assertEqual(len(rails_feedback.history(self.ledger)), 1)
        finally:
            other.close()
        self.assertFalse(rails_feedback.edit(self.ledger, fb, **args)["duplicated"])


class FeedbackClearTests(FeedbackCase):
    def test_clear_writes_marker_and_preserves_history(self):
        recorded = self.record(note="keep me")
        rails_feedback.edit(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="edit-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
            note="keep me too",
        )
        marker = rails_feedback.clear(
            self.ledger,
            recorded["feedback_id"],
            base_revision=2,
            idempotency_key="clear-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
            note="cleared by owner",
        )
        self.assertEqual(marker["operation"], "clear")
        self.assertTrue(marker["cleared"])
        rows = rails_feedback.history(self.ledger)
        self.assertEqual([row["operation"] for row in rows], ["record", "edit", "clear"])
        self.assertEqual([row["revision"] for row in rows], [1, 2, 3])
        self.assertEqual(len({row["event_id"] for row in rows}), 3)
        self.assertEqual(rows[0]["note"], "keep me")
        self.assertEqual(rows[1]["note"], "keep me too")
        latest = rails_feedback.current(self.ledger, recorded["feedback_id"])
        self.assertTrue(latest["cleared"])
        self.assertTrue(self.ledger.verify_chain()["ok"])

    def test_clear_against_stale_revision_refused(self):
        recorded = self.record()
        with self.assertRaises(rails_feedback.StaleEditRefusal):
            rails_feedback.clear(
                self.ledger,
                recorded["feedback_id"],
                base_revision=7,
                idempotency_key="clear-key-stale",
                scope=SCOPE,
                source=_source("feedback-source"),
            )
        self.assertEqual(len(rails_feedback.history(self.ledger)), 1)

    def test_clear_retry_with_same_key_appends_no_second_marker(self):
        recorded = self.record()
        first = rails_feedback.clear(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="clear-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
        )
        retried = rails_feedback.clear(
            self.ledger,
            recorded["feedback_id"],
            base_revision=1,
            idempotency_key="clear-key-1",
            scope=SCOPE,
            source=_source("feedback-source"),
        )
        self.assertTrue(retried["duplicated"])
        self.assertEqual(first["event_id"], retried["event_id"])
        self.assertEqual(len(rails_feedback.history(self.ledger)), 2)


class InterventionCountTests(FeedbackCase):
    def test_intervention_count_is_distinct_by_request_id(self):
        target = self.target()
        self.append_intervention("int-1", "intervention.requested", "int-a", target)
        self.append_intervention("int-1", "intervention.result", "int-b", target)
        self.append_intervention("int-1", "intervention.requested", "int-c", target)
        self.append_intervention("int-2", "intervention.requested", "int-d", target)
        self.assertEqual(rails_feedback.distinct_interventions(self.ledger), 2)
        counted = rails_feedback.interventions_by_id(self.ledger)
        self.assertEqual(sorted(counted), ["int-1", "int-2"])
        self.assertEqual(counted["int-1"]["event_count"], 3)
        self.assertEqual(counted["int-2"]["event_count"], 1)


class CsvExportTests(FeedbackCase):
    def test_note_privacy_at_rest_and_in_rendered_pages(self):
        private = "/tmp/synthetic-owner-path"
        recorded = self.record(note="open " + private)
        row = self.ledger.query("SELECT envelope_json FROM event_rows WHERE event_id = ?",
                                (recorded["event_id"],))[0]["envelope_json"]
        payload = json.loads(row)["payload"]
        self.assertNotIn(private, row)
        self.assertEqual(set(payload), {"rating", "note", "category", "target_event_id",
                                        "supersedes_event_id", "actor"})
        self.assertEqual(payload["note"], "[redacted]")
        for note in (private, "https://node.invalid/secret", "test.internal", "pass" + "word=planted",
                     "prefix\x1b[31m", "test@example.org"):
            self.assertEqual(rails_feedback.preview_note(note), "[redacted]")
            text = rails_export.feedback_csv([{"note": note}], ["note"])
            self.assertEqual(text, "note\n[redacted]\n")
        for field in ("payload", "scope", "source", "=bad"):
            with self.assertRaises(rails_feedback.FeedbackRefusal):
                rails_export.feedback_csv([{}], [field])

    def test_byte_cap_row_cap_and_no_partial_export(self):
        row = {"note": "éé"}
        self.assertEqual(rails_export.feedback_csv([row], ["note"], max_bytes=10), "note\néé\n")
        with self.assertRaises(rails_feedback.FeedbackSizeRefusal):
            rails_export.feedback_csv([row], ["note"], max_bytes=9)
        target = self.target()
        self.record(target=target)
        self.record(target=target, idempotency_key="other-record")
        dest = os.path.join(self._tmp.name, "feedback.csv")
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write("original")
        with self.assertRaises(rails_feedback.FeedbackSizeRefusal) as err:
            rails_export.export_feedback(self.ledger, path=dest, limit=1)
        self.assertEqual((err.exception.status_code, err.exception.code), (413, "too_large"))
        with open(dest, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "original")

    def test_r12_projected_page_columns_and_spaced_formula(self):
        fields = ("event_id", "kind", "lane_id", "target_event_id", "outcome", "rating", "category")
        row = {"event_id": "e1", "kind": "feedback.recorded", "target_event_id": "e0", "rating": 1,
               "category": "both", "note": "not-selected"}
        text = rails_export.feedback_csv([row], fields)
        self.assertEqual(text, ",".join(fields) + "\ne1,feedback.recorded,,e0,,1,both\n")
        self.assertEqual(rails_export.feedback_csv([{"note": "  =1+1"}], ["note"]), "note\n'  =1+1\n")

    def _row(self, **over):
        row = {
            "event_id": "evt-1",
            "recorded_at": "2026-01-01T00:00:00.000000Z",
            "feedback_id": "fb-1",
            "target_event_id": "evt-0",
            "revision": 1,
            "operation": "record",
            "rating": 1,
            "category": "quality",
            "note": "plain note",
            "cleared": False,
            "supersedes_event_id": "",
        }
        row.update(over)
        return row

    def test_formula_prefixes_are_escaped(self):
        from rizzstack_rails import export as rails_export

        for prefix in ("=", "+", "-", "@"):
            self.assertEqual(rails_export.escape_cell(prefix + "cmd"), "'" + prefix + "cmd")
        self.assertEqual(rails_export.escape_cell("plain"), "plain")
        self.assertEqual(rails_export.escape_cell(-5), "'-5")
        self.assertEqual(rails_export.escape_cell(5), "5")
        self.assertEqual(rails_export.escape_cell(None), "")

    def test_feedback_csv_escapes_every_text_cell(self):
        from rizzstack_rails import export as rails_export

        rows = [
            self._row(note="=1+1", feedback_id="+fb", target_event_id="@evt"),
        ]
        text = rails_export.feedback_csv(rows)
        parsed = list(csv.reader(io.StringIO(text)))
        header = parsed[0]
        body = dict(zip(header, parsed[1]))
        self.assertEqual(body["note"], "'=1+1")
        self.assertEqual(body["feedback_id"], "'+fb")
        self.assertEqual(body["target_event_id"], "'@evt")
        self.assertNotEqual(parsed[1][header.index("note")], "=1+1")

    def test_export_feedback_writes_escaped_history_to_file(self):
        from rizzstack_rails import export as rails_export

        recorded = self.record(note="=danger")
        dest = os.path.join(self._tmp.name, "feedback.csv")
        text = rails_export.export_feedback(self.ledger, path=dest)
        with open(dest, "r", encoding="utf-8", newline="") as handle:
            written = handle.read()
        self.assertEqual(text, written)
        parsed = list(csv.reader(io.StringIO(written)))
        self.assertEqual(parsed[0][0], "event_id")
        body = dict(zip(parsed[0], parsed[1]))
        self.assertEqual(body["feedback_id"], recorded["feedback_id"])
        self.assertEqual(body["note"], "'=danger")


if __name__ == "__main__":
    unittest.main()
