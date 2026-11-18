"""Pre-dispatch worker attribution (release gate E9).

Requirement (release decision): record endpoint, enrolled worker/ledger chain and
job/project/contract/revision BEFORE the POST, send through that same client, bind the returned
attempt in the MAC-covered lease, and refuse mismatches, ambiguous routes and adopted attempts.

Run: python -m unittest tests.test_dispatch_attribution -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bundlebees_plugin import attribution  # noqa: E402

FACTS = {
    "endpoint": "http://127.0.0.1:47922",
    "worker_id": "brrrrrrrr",
    "ledger_chain": "bb-ledger-7f3c9d2a",
    "job_id": "rizzstack-m2-canary-2",
    "project": "rizzstack-m2-canary",
}


def make_intent(state, **overrides):
    fields = dict(facts=FACTS)
    merged = dict(FACTS, contract="M2-canary-wave-tally", revision="84fb4e2" + "0" * 33)
    merged.update(overrides.pop("facts_override", {}))
    intent = attribution.intent_for(
        endpoint=merged["endpoint"], worker_id=merged["worker_id"],
        ledger_chain=merged["ledger_chain"], job_id=merged["job_id"],
        project=merged["project"], contract=merged.get("contract"), revision=merged.get("revision"))
    return attribution.record_intent(state, intent)


class IntentRecording(unittest.TestCase):
    def test_intent_carries_the_full_tuple_and_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            intent = make_intent(d)
            self.assertEqual(intent["endpoint"], FACTS["endpoint"])
            self.assertEqual(intent["worker_id"], FACTS["worker_id"])
            self.assertEqual(intent["ledger_chain"], FACTS["ledger_chain"])
            self.assertTrue(intent["intent_id"])
            attribution.bind_attempt(d, intent["intent_id"], "att_live_1")
            verdict = attribution.verify_attribution(d, attempt_id="att_live_1", facts=FACTS)
            self.assertTrue(verdict["ok"])
            self.assertEqual(len(verdict["digest"]), 64)

    def test_incomplete_intent_is_refused(self):
        with self.assertRaises(attribution.AttributionError) as ctx:
            attribution.intent_for(endpoint="", worker_id="w", ledger_chain="l",
                                   job_id="j", project="p", contract="", revision="")
        self.assertEqual(ctx.exception.code, "attribution_incomplete")

    def test_tampered_intent_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            intent = make_intent(d)
            attribution.bind_attempt(d, intent["intent_id"], "att_live_2")
            path = Path(d) / "attribution" / f"{intent['intent_id']}.json"
            record = json.loads(path.read_text())
            record["endpoint"] = "http://evil.example:1"
            path.write_text(json.dumps(record))
            with self.assertRaises(attribution.AttributionError) as ctx:
                attribution.verify_attribution(d, attempt_id="att_live_2", facts=FACTS)
            self.assertEqual(ctx.exception.code, "intent_signature_invalid")


class Refusals(unittest.TestCase):
    def test_adopted_attempt_is_refused(self):
        """An attempt with no pre-POST intent cannot be trusted, even if the route looks right."""
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(attribution.AttributionError) as ctx:
                attribution.verify_attribution(d, attempt_id="att_orphan", facts=FACTS)
            self.assertEqual(ctx.exception.code, "adopted_attempt")

    def test_two_intents_for_one_attempt_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as d:
            first = make_intent(d)
            second = make_intent(d)
            attribution.bind_attempt(d, first["intent_id"], "att_dup")
            attribution.bind_attempt(d, second["intent_id"], "att_dup")
            with self.assertRaises(attribution.AttributionError) as ctx:
                attribution.verify_attribution(d, attempt_id="att_dup", facts=FACTS)
            self.assertEqual(ctx.exception.code, "ambiguous_route")

    def test_endpoint_mismatch_names_the_field(self):
        with tempfile.TemporaryDirectory() as d:
            intent = make_intent(d)
            attribution.bind_attempt(d, intent["intent_id"], "att_ep")
            wrong = dict(FACTS, endpoint="http://127.0.0.1:47821")
            with self.assertRaises(attribution.AttributionError) as ctx:
                attribution.verify_attribution(d, attempt_id="att_ep", facts=wrong)
            self.assertEqual(ctx.exception.code, "attribution_mismatch")
            self.assertEqual(ctx.exception.detail["fields"], ["endpoint"])

    def test_worker_ledger_chain_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            intent = make_intent(d)
            attribution.bind_attempt(d, intent["intent_id"], "att_chain")
            wrong = dict(FACTS, ledger_chain="bb-ledger-other")
            with self.assertRaises(attribution.AttributionError) as ctx:
                attribution.verify_attribution(d, attempt_id="att_chain", facts=wrong)
            self.assertEqual(ctx.exception.code, "attribution_mismatch")
            self.assertIn("ledger_chain", ctx.exception.detail["fields"])


class Scope(unittest.TestCase):
    def test_enforcement_is_canary_project_only(self):
        settings = {"attribution_canary_projects": ["rizzstack-m2-canary"]}
        self.assertTrue(attribution.applies_to(settings, "rizzstack-m2-canary"))
        self.assertFalse(attribution.applies_to(settings, "workflow"))
        self.assertFalse(attribution.applies_to({}, "rizzstack-m2-canary"))


class DispatchGate(unittest.TestCase):
    """The gate must refuse INSIDE the canary project and stay inert everywhere else."""

    def _client(self):
        class Stub:
            base_url = ""
            def __init__(self):
                self.created = []
            def health(self):
                return {}
            def attempts(self):
                return []
            def attempts_raw(self):
                return []
            def create(self, brief, model=None, kanban_task_id=None):
                self.created.append(brief)
                return {"attempt_id": "att_gate", "state": "queued"}
        return Stub()

    def _brief(self, project):
        return {
            "objective": "Implement the feature", "project": project, "branch": "feat/x",
            "source_rev": "a" * 40, "assigned_files": ["src/a.py"],
            "commands": ["python3 -m pytest -q"], "acceptance": ["tests pass"],
            "budget": {"timeout_s": 600, "max_tool_calls": 20},
        }

    def test_canary_project_without_route_facts_is_refused_and_never_posted(self):
        with tempfile.TemporaryDirectory() as d:
            from bundlebees_plugin import tools
            client = self._client()
            out = tools._dispatch(client, {"brief": self._brief("rizzstack-m2-canary"),
                                          "_settings": {"attribution_canary_projects": ["rizzstack-m2-canary"]}},
                                  Path(d))
            payload = json.loads(out)
            self.assertEqual(payload.get("code"), "attribution_incomplete")
            self.assertIn("endpoint", payload.get("error", ""))
            self.assertEqual(client.created, [], "nothing may be POSTed without an intent")

    def test_other_projects_are_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            from bundlebees_plugin import tools
            client = self._client()
            attr = tools._attribution
            attr.intent_for(  # prove the module is importable but not consulted here
                endpoint="http://127.0.0.1:47922", worker_id="w", ledger_chain="l",
                job_id="j", project="p", contract="", revision="")
            out = tools._dispatch(client, {"brief": self._brief("workflow"),
                                          "_settings": {"attribution_canary_projects": ["rizzstack-m2-canary"]}},
                                  Path(d))
            payload = json.loads(out)
            self.assertNotIn("attribution", json.dumps(payload))
            self.assertEqual(len(client.created), 1, "non-canary projects dispatch as before")


if __name__ == "__main__":
    unittest.main()
