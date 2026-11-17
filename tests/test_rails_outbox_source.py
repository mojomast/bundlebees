"""Rails R06 outbox source tests (RED-first).

The outbox source is read-only over ``events/outbox``. A landing event is
mapped to one ``rizzstack.rails/1`` ``attempt.state`` rail event. Replaying
the same event id and digest must dedupe, a contradictory digest for the same
id must fail closed before emitting anything, and a landing after the attempt
is terminal must be refused rather than silently ignored.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRIB_DIR = os.path.join(REPO_ROOT, "contrib")
if CONTRIB_DIR not in sys.path:
    sys.path.insert(0, CONTRIB_DIR)

from bundlebees_plugin import bee_events, rails_outbox_source
from rizzstack_rails import contracts as rails_contracts
from rizzstack_rails import ledger as rails_ledger

T0 = 1_800_000_000.0
SOURCE_REV = "a" * 40
LEASE_SHA = "b" * 64


def landing(
    attempt_id="att-1",
    state="running",
    transition_id="tr-1",
    now=T0,
    job_id="job-1",
    task_id="task-1",
):
    return bee_events.build_event(
        {"attempt_id": attempt_id, "state": state, "transition_id": transition_id},
        feature_id="BEE-1",
        job_id=job_id,
        task_id=task_id,
        coordinator={"identity": "coordinator-1", "session": "run-1"},
        backend={"harness": "hermes", "model": "deepseek-v4.1-flash"},
        source_rev=SOURCE_REV,
        lease={"sha256": LEASE_SHA, "assigned_files": ["src/a.py"]},
        result={"available": False},
        now=now,
    )


def outbox_dir(state):
    return Path(state) / "events" / "outbox"


def record(state, event):
    return bee_events.record_landing(state, event)


def outbox_files(state):
    root = outbox_dir(state)
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.suffix == ".json")


class OutboxSourceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rails-r06-")
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)

    def source(self, **kwargs):
        return rails_outbox_source.RailsOutboxSource(self.state, **kwargs)


class ReplayTests(OutboxSourceCase):
    def test_replay_emits_one_attempt_state_rail_event(self):
        event = landing()
        record(self.state, event)

        result = self.source().replay()

        self.assertEqual(result["emitted"], 1)
        self.assertEqual(len(result["rail_events"]), 1)
        rail = result["rail_events"][0]
        self.assertEqual(rail["schema"], rails_outbox_source.SCHEMA)
        self.assertEqual(rail["kind"], "attempt.state")
        self.assertEqual(rail["scope"]["attempt_id"], "att-1")
        self.assertEqual(rail["scope"]["job_id"], "job-1")
        self.assertEqual(rail["source"]["record_id"], event["id"])
        self.assertEqual(rail["payload"]["to_state"], "running")
        self.assertEqual(rail["payload"]["from_state"], "unknown")
        self.assertEqual(rail["payload"]["evidence_ref"], rails_outbox_source.landing_digest(event))
        self.assertEqual(rail["authority"], "worker_report")

    def test_replaying_a_landing_dedupes_to_one_rail_event(self):
        record(self.state, landing())

        source = self.source()
        first = source.replay()
        second = source.replay()

        self.assertEqual(first["emitted"], 1)
        self.assertEqual(second["emitted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(source.rail_events), 1)

    def test_emitted_body_matches_the_frozen_rails_envelope(self):
        record(self.state, landing())
        rail = self.source().replay()["rail_events"][0]

        rails_contracts.validate_event(
            rail["kind"],
            rail["source"],
            rail["scope"],
            rail["payload"],
            rail["authority"],
            rail["lane"],
            rail["occurred_at"],
            rail["duration_ms"],
            rail["timing_quality"],
            rail["redaction_policy"],
        )

    def test_replay_does_not_modify_the_source_outbox(self):
        record(self.state, landing())
        before = {path.name: path.read_bytes() for path in outbox_files(self.state)}

        self.source().replay()

        after = {path.name: path.read_bytes() for path in outbox_files(self.state)}
        self.assertEqual(after, before)


class ContradictionTests(OutboxSourceCase):
    def test_contradictory_digest_for_same_id_fails_closed(self):
        event = landing()
        record(self.state, event)
        source = self.source()
        source.replay()

        path = outbox_files(self.state)[0]
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["data"]["state"] = "failed"
        path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")

        with self.assertRaises(rails_outbox_source.OutboxDigestConflict) as caught:
            source.replay()

        self.assertEqual(caught.exception.code, "digest_conflict")
        self.assertEqual(len(source.rail_events), 1)

    def test_contradiction_is_detected_before_any_event_is_emitted(self):
        first = landing(attempt_id="att-a", state="running", transition_id="tr-a", now=T0)
        second = landing(attempt_id="att-b", state="running", transition_id="tr-b", now=T0 + 1)
        record(self.state, first)
        record(self.state, second)
        source = self.source()
        source.replay()

        conflicted = outbox_files(self.state)[1]
        value = json.loads(conflicted.read_text(encoding="utf-8"))
        value["data"]["state"] = "succeeded"
        conflicted.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

        before = len(source.rail_events)
        with self.assertRaises(rails_outbox_source.OutboxDigestConflict):
            source.replay()
        self.assertEqual(len(source.rail_events), before)


class TerminalTests(OutboxSourceCase):
    def test_a_landing_after_terminal_is_refused_and_emits_nothing(self):
        terminal = landing(attempt_id="att-t", state="succeeded", transition_id="tr-1", now=T0)
        later = landing(attempt_id="att-t", state="running", transition_id="tr-2", now=T0 + 1)
        record(self.state, terminal)
        record(self.state, later)

        source = self.source()
        with self.assertRaises(rails_outbox_source.TerminalStateRefused) as caught:
            source.replay()

        self.assertEqual(caught.exception.code, "terminal_state")
        self.assertEqual(source.rail_events, [])

    def test_replaying_the_same_terminal_landing_is_a_dedupe_not_a_refusal(self):
        terminal = landing(attempt_id="att-t", state="failed", transition_id="tr-1")
        record(self.state, terminal)
        source = self.source()

        self.assertEqual(source.replay()["emitted"], 1)
        self.assertEqual(source.replay()["duplicates"], 1)
        self.assertEqual(len(source.rail_events), 1)


class LedgerBackedTests(OutboxSourceCase):
    def ledger(self):
        path = Path(self._tmp.name) / "rails.sqlite3"
        led = rails_ledger.RailsLedger(str(path))
        self.addCleanup(led.close)
        return led

    def rows(self, led):
        return led.query("SELECT event_id, envelope_json FROM event_rows ORDER BY seq")

    def test_dedupe_survives_a_fresh_source_instance(self):
        record(self.state, landing())
        led = self.ledger()

        first = rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()
        second = rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()

        self.assertEqual(first["emitted"], 1)
        self.assertEqual(second["emitted"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.rows(led)), 1)

    def test_contradictory_digest_against_the_ledger_emits_nothing(self):
        event = landing()
        record(self.state, event)
        led = self.ledger()
        rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()

        path = outbox_files(self.state)[0]
        value = json.loads(path.read_text(encoding="utf-8"))
        value["data"]["state"] = "failed"
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

        with self.assertRaises(rails_outbox_source.OutboxDigestConflict):
            rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()
        self.assertEqual(len(self.rows(led)), 1)

    def test_terminal_state_refuses_a_fresh_source_instance(self):
        terminal = landing(attempt_id="att-t", state="cancelled", transition_id="tr-1", now=T0)
        later = landing(attempt_id="att-t", state="running", transition_id="tr-2", now=T0 + 1)
        led = self.ledger()
        record(self.state, terminal)
        rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()

        record(self.state, later)
        with self.assertRaises(rails_outbox_source.TerminalStateRefused):
            rails_outbox_source.RailsOutboxSource(self.state, ledger=led).replay()
        self.assertEqual(len(self.rows(led)), 1)


if __name__ == "__main__":
    unittest.main()
