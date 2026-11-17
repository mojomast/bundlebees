"""Rails R11 acceptance tests (RED-first): feedback overlay and CSV export."""

import csv
import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
if CONTRIB_DIR not in sys.path:
    sys.path.insert(0, CONTRIB_DIR)

from rizzstack_rails import feedback as rails_feedback
from rizzstack_rails import ledger as rails_ledger


SCOPE = {"task_id": "task-r11"}


def _source(record_id):
    return {
        "adapter": "unittest",
        "instance": "inst-r11",
        "record_id": record_id,
        "revision": "rev-1",
    }


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
