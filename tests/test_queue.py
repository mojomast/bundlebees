"""Queue intake, durable claim and one-pass consumption."""

from __future__ import annotations

import hashlib
import json
import re
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli
from bundlebees_plugin import provision_bind as B
from bundlebees_plugin import provisioning as P
from bundlebees_plugin import queue as Q


def card(feature_id, job_id="job_1", deps=(), status="queued", repo="mojomast/smoke-real"):
    return {"repo": repo, "job_id": job_id, "depends_on": list(deps), "status": status}

class QueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "queue.json"
        self.path.write_text(json.dumps({"f1": card("f1"), "f2": card("f2", deps=["f1"], status="queued")}))
        self.queue = Q.JsonQueue(self.path)

    def test_a_claim_is_exclusive(self):
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        with self.assertRaises(Q.QueueError) as caught:
            store.claim("f1", job_id="job_1")
        self.assertEqual(caught.exception.code, "already_claimed")

    def test_eligibility_respects_dependencies(self):
        items = self.queue.list_items()
        self.assertEqual(Q.eligible(items, done=set()).feature_id, "f1")
        self.assertIsNone(Q.eligible([i for i in items if i.feature_id == "f2"], done=set()))
        self.assertEqual(Q.eligible(items, done={"f1"}).feature_id, "f2")

    def test_run_once_is_idle_without_queued_work(self):
        for item in self.queue.list_items():
            self.queue.set_status(item.feature_id, "ready_for_review")
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["action"], "idle")

    # -- live queue document shape ------------------------------------------
    #
    # The live queue file is written by an external producer that is not
    # identified in this repository; the hive docket publisher only emits its
    # separate hive.queue/1 document, never this file. Both a features envelope
    # and the legacy flat map (feature_id -> card) are accepted. The features
    # value may be a list or an object mapping feature ids to cards, and a list
    # card may use "id" in place of "feature_id". A flat map whose sole feature
    # id is literally "features" is indistinguishable from an envelope, so that
    # shape stays unsupported rather than guessed at.

    def _write_live_envelope(self, features):
        self.path.write_text(
            json.dumps(
                {
                    "schema": "bundlebees.queue/1",
                    "generated_at": "2026-11-18T04:27:23Z",
                    "features": features,
                    "note": "live-shaped queue",
                }
            )
        )

    def test_an_empty_live_envelope_is_an_empty_queue_not_an_attribute_error(self):
        self._write_live_envelope([])

        self.assertEqual(Q.JsonQueue(self.path).list_items(), [])

    def test_list_items_reads_the_features_list_of_the_live_envelope(self):
        self._write_live_envelope(
            [
                {
                    "feature_id": "f1",
                    "repo": "mojomast/bundlebees",
                    "job_id": "job_1",
                    "status": "queued",
                    "depends_on": [],
                }
            ]
        )

        items = Q.JsonQueue(self.path).list_items()

        self.assertEqual([item.feature_id for item in items], ["f1"])
        self.assertEqual(items[0].repo, "mojomast/bundlebees")
        self.assertEqual(items[0].job_id, "job_1")
        self.assertEqual(items[0].status, "queued")

    def test_set_status_updates_the_feature_and_preserves_the_live_envelope(self):
        self._write_live_envelope(
            [
                {
                    "feature_id": "f1",
                    "repo": "mojomast/bundlebees",
                    "job_id": "job_1",
                    "status": "queued",
                    "depends_on": [],
                }
            ]
        )
        queue = Q.JsonQueue(self.path)

        queue.set_status("f1", "running", {"job_id": "job_1"})

        body = json.loads(self.path.read_text())
        self.assertEqual(body["schema"], "bundlebees.queue/1")
        self.assertEqual(body["note"], "live-shaped queue")
        self.assertNotIn("f1", body, "a document-shaped queue must not be rewritten as a flat map")
        self.assertEqual(body["features"][0]["status"], "running")
        self.assertEqual(body["features"][0]["evidence"], {"job_id": "job_1"})
        self.assertEqual(queue.get("f1").status, "running")

    def test_features_as_an_object_maps_ids_to_the_same_items_as_the_list_shape(self):
        cards = [
            {
                "feature_id": "f2",
                "repo": "mojomast/b",
                "job_id": "job_2",
                "title": "Second",
                "depends_on": ["f1"],
                "status": "queued",
            },
            {
                "feature_id": "f1",
                "repo": "mojomast/a",
                "job_id": "job_1",
                "title": "First",
                "depends_on": [],
                "status": "blocked",
            },
        ]
        self._write_live_envelope(cards)
        expected = Q.JsonQueue(self.path).list_items()

        self._write_live_envelope({card["feature_id"]: card for card in cards})

        mapped = Q.JsonQueue(self.path)
        self.assertEqual(mapped.list_items(), expected)
        self.assertEqual(mapped.get("f1").title, "First")
        self.assertEqual(mapped.get("f1").depends_on, ())

    def test_a_features_list_card_may_use_id_instead_of_feature_id(self):
        self._write_live_envelope(
            [
                {
                    "id": "f1",
                    "repo": "mojomast/bundlebees",
                    "job_id": "job_1",
                    "title": "Feature",
                    "depends_on": [],
                    "status": "queued",
                }
            ]
        )

        queue = Q.JsonQueue(self.path)
        items = queue.list_items()

        self.assertEqual(
            [(item.feature_id, item.repo, item.job_id, item.title, item.status) for item in items],
            [("f1", "mojomast/bundlebees", "job_1", "Feature", "queued")],
        )
        self.assertEqual(queue.get("f1").job_id, "job_1")

    # -- one id, one card ---------------------------------------------------

    def test_a_features_map_card_body_id_must_match_its_map_key(self):
        for field in ("feature_id", "id"):
            with self.subTest(field=field):
                self._write_live_envelope(
                    {"f1": {field: "f2", "repo": "mojomast/a", "job_id": "job_1"}}
                )

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertIn("f1", str(caught.exception))
                self.assertIn("f2", str(caught.exception))
                self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_a_features_map_card_body_id_may_restate_its_key_after_stripping(self):
        for body in (
            {"repo": "mojomast/a", "job_id": "job_1"},
            {"feature_id": " f1 ", "repo": "mojomast/a", "job_id": "job_1"},
            {"id": "f1 ", "repo": "mojomast/a", "job_id": "job_1"},
            {"feature_id": " f1 ", "id": "f1", "repo": "mojomast/a", "job_id": "job_1"},
        ):
            with self.subTest(body=body):
                self._write_live_envelope({"f1": body})

                items = Q.JsonQueue(self.path).list_items()

                self.assertEqual([item.feature_id for item in items], ["f1"])
                self.assertEqual(items[0].job_id, "job_1")

    def test_a_features_list_refuses_two_cards_with_the_same_feature_id(self):
        for cards in (
            [{"feature_id": "f1", "job_id": "j1"}, {"feature_id": "f1", "job_id": "j2"}],
            [{"feature_id": "f1", "job_id": "j1"}, {"id": " f1 ", "job_id": "j2"}],
            [{"id": "f1", "job_id": "j1"}, {"id": "f1", "job_id": "j2"}],
        ):
            with self.subTest(cards=cards):
                self._write_live_envelope(cards)

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertIn("f1", str(caught.exception))
                self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_a_features_list_with_unique_ids_still_accepts_the_id_fallback(self):
        self._write_live_envelope(
            [
                {"feature_id": "f1", "job_id": "j1"},
                {"id": "f2", "job_id": "j2"},
            ]
        )

        items = Q.JsonQueue(self.path).list_items()

        self.assertEqual([item.feature_id for item in items], ["f1", "f2"])

    def test_a_features_list_card_with_conflicting_feature_id_and_id_is_refused(self):
        conflicting = {"feature_id": "f1", "id": "f2", "repo": "mojomast/a", "job_id": "job_1"}
        self._write_live_envelope([conflicting])

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).list_items()

        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertIn("f1", str(caught.exception))
        self.assertIn("f2", str(caught.exception))
        self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

        document = {
            "schema": "bundlebees.queue/1",
            "generated_at": "2026-11-18T04:27:23Z",
            "note": "live-shaped queue",
            "features": [conflicting],
        }
        self.path.write_text(json.dumps(document))
        before = self.path.read_bytes()

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).set_status("f1", "running")

        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertEqual(self.path.read_bytes(), before, "a refused document must not be rewritten")

    def test_a_features_list_card_with_agreeing_or_single_ids_still_resolves(self):
        for body in (
            {"feature_id": "f1", "job_id": "job_1"},
            {"id": "f1", "job_id": "job_1"},
            {"feature_id": " f1 ", "id": "f1", "job_id": "job_1"},
            {"feature_id": "f1", "id": "   ", "job_id": "job_1"},
            {"feature_id": "   ", "id": "f1", "job_id": "job_1"},
        ):
            with self.subTest(body=body):
                self._write_live_envelope([body])

                items = Q.JsonQueue(self.path).list_items()

                self.assertEqual([item.feature_id for item in items], ["f1"])
                self.assertEqual(items[0].job_id, "job_1")

    def test_set_status_refuses_an_identity_violation_without_writing(self):
        documents = (
            {
                "schema": "bundlebees.queue/1",
                "generated_at": "2026-11-18T04:27:23Z",
                "note": "live-shaped queue",
                "features": {"f1": {"feature_id": "f2", "job_id": "job_1"}},
            },
            {
                "schema": "bundlebees.queue/1",
                "generated_at": "2026-11-18T04:27:23Z",
                "note": "live-shaped queue",
                "features": [
                    {"feature_id": "f1", "job_id": "job_1"},
                    {"feature_id": "f1", "job_id": "job_2"},
                ],
            },
        )
        for document in documents:
            with self.subTest(features=document["features"]):
                self.path.write_text(json.dumps(document))
                before = self.path.read_bytes()

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).set_status("f1", "running")

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(self.path.read_bytes(), before, "a refused document must not be rewritten")

    # -- flat map identity --------------------------------------------------

    def test_a_flat_map_card_body_id_must_match_its_map_key(self):
        for field in ("feature_id", "id"):
            with self.subTest(field=field):
                self.path.write_text(
                    json.dumps({"f1": {field: "f2", "repo": "mojomast/a", "job_id": "job_1"}})
                )

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertIn("f1", str(caught.exception))
                self.assertIn("f2", str(caught.exception))
                self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_a_flat_map_card_with_both_body_ids_differing_from_its_key_is_refused(self):
        self.path.write_text(
            json.dumps(
                {
                    "f1": {
                        "feature_id": "f2",
                        "id": "f3",
                        "repo": "mojomast/a",
                        "job_id": "job_1",
                    }
                }
            )
        )

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).list_items()

        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertIn("f1", str(caught.exception))
        self.assertIn("f2", str(caught.exception))
        self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_set_status_refuses_a_flat_map_identity_violation_without_writing(self):
        documents = (
            {"f1": {"feature_id": "f2", "repo": "mojomast/a", "job_id": "job_1"}},
            {"f1": {"id": "f2", "repo": "mojomast/a", "job_id": "job_1"}},
            {"f1": {"feature_id": "f2", "id": "f3", "job_id": "job_1"}},
        )
        for document in documents:
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                before = hashlib.sha256(self.path.read_bytes()).hexdigest()

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).set_status("f1", "running")

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(
                    hashlib.sha256(self.path.read_bytes()).hexdigest(),
                    before,
                    "a refused document must not be rewritten",
                )
                self.assertFalse((self.tmp / "queue.json.tmp").exists())

    def test_a_flat_map_blank_or_whitespace_key_is_refused(self):
        for document in (
            {"": {"feature_id": "f1", "repo": "r", "job_id": "j1"}},
            {"   ": {"feature_id": "f1", "repo": "r", "job_id": "j1"}},
        ):
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                before = hashlib.sha256(self.path.read_bytes()).hexdigest()
                queue = Q.JsonQueue(self.path)

                for action in ("list", "set_status"):
                    with self.subTest(action=action):
                        with self.assertRaises(Q.QueueError) as caught:
                            if action == "list":
                                queue.list_items()
                            else:
                                queue.set_status("f1", "running")

                        self.assertEqual(caught.exception.code, "queue_invalid")
                        self.assertEqual(str(caught.exception), "a queue feature id is blank")
                        self.assertEqual(
                            caught.exception.fix,
                            "Repair the queue file: every feature needs a feature_id or id.",
                        )
                        self.assertEqual(
                            hashlib.sha256(self.path.read_bytes()).hexdigest(),
                            before,
                            "a refused document must not be rewritten",
                        )
                        self.assertFalse((self.tmp / "queue.json.tmp").exists())

    def test_a_flat_map_identity_refusal_quotes_raw_key_and_raw_body_id(self):
        for field in ("feature_id", "id"):
            with self.subTest(field=field):
                self.path.write_text(
                    json.dumps(
                        {" f1 ": {field: " f2 ", "repo": "r", "job_id": "j1"}}
                    )
                )

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(str(caught.exception), "queue feature ' f1 ' carries body id ' f2 '")
                self.assertIn("' f1 '", caught.exception.fix)
                self.assertIn("' f2 '", caught.exception.fix)

    def test_a_flat_map_refusal_names_both_differing_declared_ids(self):
        for document, expected_message, expected_ids in (
            (
                {"f1": {"feature_id": "f2", "id": "f3", "repo": "r", "job_id": "j1"}},
                "queue feature 'f1' carries body ids 'f2' and 'f3'",
                ("'f2'", "'f3'"),
            ),
            (
                {" f1 ": {"feature_id": " f2 ", "id": " f3 ", "repo": "r", "job_id": "j1"}},
                "queue feature ' f1 ' carries body ids ' f2 ' and ' f3 '",
                ("' f2 '", "' f3 '"),
            ),
        ):
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(str(caught.exception), expected_message)
                for expected_id in expected_ids:
                    self.assertIn(expected_id, caught.exception.fix)

    def test_flat_map_identity_comparison_stays_stripped_and_ignores_non_strings(self):
        for document in (
            {" f1 ": {"feature_id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
            {"f1": {"feature_id": 7, "id": None, "repo": "r", "job_id": "j1", "status": "queued"}},
            {"f1": {"feature_id": "", "id": 7, "repo": "r", "job_id": "j1", "status": "queued"}},
        ):
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))
                queue = Q.JsonQueue(self.path)

                items = queue.list_items()

                self.assertEqual([item.feature_id for item in items], [next(iter(document))])

                queue.set_status(next(iter(document)), "running", {"job_id": "j1"})
                self.assertEqual(queue.list_items()[0].status, "running")

    def test_flat_map_accepted_documents_match_the_3932e10_baseline(self):
        baseline = {
            "no_body_ids": (
                {"f1": {"repo": "r", "job_id": "j1", "depends_on": [], "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "depends_on": [],
                        "evidence": {"job_id": "j1"},
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "both_equal": (
                {"f1": {"feature_id": "f1", "id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": "f1",
                        "id": "f1",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "both_equal_padded_body": (
                {
                    "f1": {
                        "feature_id": " f1 ",
                        "id": "f1  ",
                        "repo": "r",
                        "job_id": "j1",
                        "status": "queued",
                    }
                },
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": " f1 ",
                        "id": "f1  ",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "padded_key_no_body": (
                {" f1 ": {"repo": "r", "job_id": "j1", "status": "queued"}},
                [(" f1 ", "r", "j1", "", (), "queued")],
                {
                    " f1 ": {
                        "evidence": {"job_id": "j1"},
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "padded_key_both_equal": (
                {" f1 ": {"feature_id": "f1", "id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
                [(" f1 ", "r", "j1", "", (), "queued")],
                {
                    " f1 ": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": "f1",
                        "id": "f1",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "only_feature_id": (
                {"f1": {"feature_id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": "f1",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "only_id": (
                {"f1": {"id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "id": "f1",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "blank_feature_id_id_match": (
                {"f1": {"feature_id": "   ", "id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": "   ",
                        "id": "f1",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
            "blank_id_feature_id_match": (
                {"f1": {"feature_id": "f1", "id": "   ", "repo": "r", "job_id": "j1", "status": "queued"}},
                [("f1", "r", "j1", "", (), "queued")],
                {
                    "f1": {
                        "evidence": {"job_id": "j1"},
                        "feature_id": "f1",
                        "id": "   ",
                        "job_id": "j1",
                        "repo": "r",
                        "status": "running",
                        "updated_at": "<ts>",
                    }
                },
            ),
        }
        for name, (document, expected_items, expected_written) in baseline.items():
            with self.subTest(name=name):
                self.path.write_text(json.dumps(document))
                queue = Q.JsonQueue(self.path)

                self.assertEqual(
                    [
                        (item.feature_id, item.repo, item.job_id, item.title, item.depends_on, item.status)
                        for item in queue.list_items()
                    ],
                    expected_items,
                )

                queue.set_status(next(iter(document)), "running", {"job_id": "j1"})
                written = re.sub(
                    r'"updated_at": "[^"]+"', '"updated_at": "<ts>"', self.path.read_text()
                )
                self.assertEqual(written, json.dumps(expected_written, indent=2, sort_keys=True))

    def test_nested_feature_shapes_match_the_3932e10_baseline(self):
        accepted = {
            "map_no_body": (
                {
                    "schema": "bundlebees.queue/1",
                    "generated_at": "g",
                    "note": "n",
                    "features": {
                        "f1": {"repo": "r", "job_id": "j1", "status": "queued", "depends_on": []}
                    },
                },
                {
                    "schema": "bundlebees.queue/1",
                    "generated_at": "g",
                    "note": "n",
                    "features": {
                        "f1": {
                            "depends_on": [],
                            "evidence": {"job_id": "j1"},
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    },
                },
            ),
            "map_restate": (
                {
                    "schema": "bundlebees.queue/1",
                    "features": {
                        "f1": {
                            "feature_id": " f1 ",
                            "id": "f1",
                            "repo": "r",
                            "job_id": "j1",
                            "status": "queued",
                        }
                    },
                },
                {
                    "schema": "bundlebees.queue/1",
                    "features": {
                        "f1": {
                            "evidence": {"job_id": "j1"},
                            "feature_id": " f1 ",
                            "id": "f1",
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    },
                },
            ),
            "list_feature_id": (
                {"features": [{"feature_id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}]},
                {
                    "features": [
                        {
                            "evidence": {"job_id": "j1"},
                            "feature_id": "f1",
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    ]
                },
            ),
            "list_id": (
                {"features": [{"id": "f1", "repo": "r", "job_id": "j1", "status": "queued"}]},
                {
                    "features": [
                        {
                            "evidence": {"job_id": "j1"},
                            "id": "f1",
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    ]
                },
            ),
            "list_agreeing": (
                {
                    "features": [
                        {
                            "feature_id": " f1 ",
                            "id": "f1",
                            "repo": "r",
                            "job_id": "j1",
                            "status": "queued",
                        }
                    ]
                },
                {
                    "features": [
                        {
                            "evidence": {"job_id": "j1"},
                            "feature_id": " f1 ",
                            "id": "f1",
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    ]
                },
            ),
            "list_blank_one": (
                {
                    "features": [
                        {
                            "feature_id": "f1",
                            "id": "   ",
                            "repo": "r",
                            "job_id": "j1",
                            "status": "queued",
                        }
                    ]
                },
                {
                    "features": [
                        {
                            "evidence": {"job_id": "j1"},
                            "feature_id": "f1",
                            "id": "   ",
                            "job_id": "j1",
                            "repo": "r",
                            "status": "running",
                            "updated_at": "<ts>",
                        }
                    ]
                },
            ),
        }
        for name, (document, expected_written) in accepted.items():
            with self.subTest(name=name):
                self.path.write_text(json.dumps(document))
                queue = Q.JsonQueue(self.path)

                self.assertEqual(
                    [
                        (item.feature_id, item.repo, item.job_id, item.title, item.depends_on, item.status)
                        for item in queue.list_items()
                    ],
                    [("f1", "r", "j1", "", (), "queued")],
                )

                queue.set_status("f1", "running", {"job_id": "j1"})
                written = re.sub(
                    r'"updated_at": "[^"]+"', '"updated_at": "<ts>"', self.path.read_text()
                )
                self.assertEqual(written, json.dumps(expected_written, indent=2, sort_keys=True))

        refused = {
            "map_mismatch": (
                {"features": {"f1": {"feature_id": "f2", "repo": "r", "job_id": "j1"}}},
                "queue feature 'f1' carries body id 'f2'",
                "Repair the queue file: the 'f1' card must use 'f1' as its feature_id/id, "
                "or move the 'f2' card under that key.",
            ),
            "list_conflicting_ids": (
                {"features": [{"feature_id": "f1", "id": "f2", "repo": "r", "job_id": "j1"}]},
                "queue feature at index 0 declares feature_id 'f1' and id 'f2'",
                "Repair the queue file: make the card's feature_id and id name the same "
                "feature ('f1' or 'f2'), or remove one.",
            ),
            "list_duplicate_ids": (
                {
                    "features": [
                        {"feature_id": "f1", "job_id": "j1"},
                        {"id": " f1 ", "job_id": "j2"},
                    ]
                },
                "queue feature 'f1' appears more than once (indices 0 and 1)",
                "Repair the queue file: remove or rename the duplicate 'f1' card so each "
                "feature id appears once.",
            ),
        }
        for name, (document, expected_message, expected_fix) in refused.items():
            with self.subTest(name=name):
                self.path.write_text(json.dumps(document))

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertEqual(str(caught.exception), expected_message)
                self.assertEqual(caught.exception.fix, expected_fix)

    def test_a_features_list_card_without_a_usable_id_is_a_typed_refusal(self):
        for body in ({"repo": "r", "job_id": "j"}, {"feature_id": "", "id": "  "}):
            with self.subTest(card=body):
                self._write_live_envelope([body])

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")
                self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_set_status_updates_only_the_target_card_of_a_features_object_map(self):
        self.path.write_text(
            json.dumps(
                {
                    "schema": "bundlebees.queue/1",
                    "generated_at": "2026-11-18T04:27:23Z",
                    "note": "live-shaped queue",
                    "extra": {"kept": True},
                    "features": {
                        "f1": {"repo": "mojomast/a", "job_id": "job_1", "status": "queued"},
                        "f2": {"repo": "mojomast/b", "job_id": "job_2", "status": "queued"},
                    },
                }
            )
        )
        queue = Q.JsonQueue(self.path)

        queue.set_status("f1", "running", {"job_id": "job_1"})

        body = json.loads(self.path.read_text())
        self.assertEqual(body["schema"], "bundlebees.queue/1")
        self.assertEqual(body["generated_at"], "2026-11-18T04:27:23Z")
        self.assertEqual(body["note"], "live-shaped queue")
        self.assertEqual(body["extra"], {"kept": True})
        self.assertNotIn("f1", body, "a document-shaped queue must not be rewritten as a flat map")
        self.assertEqual(body["features"]["f1"]["status"], "running")
        self.assertEqual(body["features"]["f1"]["evidence"], {"job_id": "job_1"})
        self.assertEqual(
            body["features"]["f2"],
            {"repo": "mojomast/b", "job_id": "job_2", "status": "queued"},
            "a non-target card must be untouched",
        )
        self.assertEqual(queue.get("f1").status, "running")

    def test_features_object_map_with_a_non_object_card_is_a_typed_error(self):
        self._write_live_envelope({"f1": ["not", "an", "object"]})

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).list_items()

        self.assertEqual(caught.exception.code, "queue_invalid")

    def test_non_mapping_top_level_queue_documents_are_typed_errors(self):
        for document in ([], "queue", 3, None):
            with self.subTest(document=document):
                self.path.write_text(json.dumps(document))

                with self.assertRaises(Q.QueueError) as caught:
                    Q.JsonQueue(self.path).list_items()

                self.assertEqual(caught.exception.code, "queue_invalid")

    def test_malformed_json_is_a_typed_error(self):
        self.path.write_text("{")

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).list_items()

        self.assertEqual(caught.exception.code, "queue_invalid")
        self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_an_unreadable_queue_file_is_a_typed_error(self):
        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.tmp).list_items()

        self.assertEqual(caught.exception.code, "queue_unreadable")
        self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")

    def test_an_unknown_queue_shape_is_a_typed_error_not_an_attribute_error(self):
        self.path.write_text(json.dumps([{"feature_id": "f1", "job_id": "job_1"}]))

        with self.assertRaises(Q.QueueError) as caught:
            Q.JsonQueue(self.path).list_items()

        self.assertEqual(caught.exception.code, "queue_invalid")

    def test_run_once_claims_and_calls_the_production_tick(self):
        called = []
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)

        def tick(job_id):
            called.append(job_id)
            return {"status": "ok", "transitions": ["T1->dispatched"]}

        result = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(result["feature"], "f1")
        self.assertEqual(called, ["job_1"])
        record = Q.QueueStore(self.tmp).read("f1")
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["job_id"], "job_1")
        self.assertEqual(self.queue.get("f1").status, "running")

    def test_a_claimed_card_whose_job_is_missing_blocks_instead_of_dispatching(self):
        Q.QueueStore(self.tmp).claim("f1", job_id="job_missing")
        called = []
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id))

        self.assertEqual(result["action"], "blocked")
        self.assertEqual(called, [])
        self.assertFalse(self.queue.get("f1").status == "ready_for_review")

    def test_a_second_pass_does_not_duplicate_the_tick_for_a_claimed_card(self):
        calls = []
        Q.QueueStore(self.tmp).write("f1", {"state": "running", "job_id": "job_1"})
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)

        def tick(job_id):
            calls.append(job_id)
            return {"status": "ok"}

        Q.run_once(self.tmp, self.queue, tick=tick)
        self.assertEqual(len(calls), 1, "a claimed card must not be claimed again")

    def test_a_queue_status_alone_is_not_completion_evidence(self):
        # The card says ready_for_review, but the job has no accepted evidence:
        # queue.run_once never treats that as completion for another item.
        self.queue.set_status("f1", "ready_for_review")
        self.path.write_text(json.dumps({"f2": card("f2", status="queued")}))
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["feature"], "f2")

    def test_the_kanban_adapter_parses_supported_json(self):
        class Out:
            returncode = 0
            stdout = json.dumps([{"id": "f9", "title": "Feature", "status": "queued",
                                  "metadata": {"repo": "mojomast/smoke-real", "job_id": "job_9"}}])
            stderr = ""

        adapter = Q.KanbanQueue(lambda args: Out())
        item = adapter.list_items()[0]
        self.assertEqual((item.feature_id, item.job_id), ("f9", "job_9"))

    def test_an_unparseable_kanban_response_is_an_error_not_an_empty_queue(self):
        class Out:
            returncode = 0
            stdout = "not json"
            stderr = ""

        with self.assertRaises(Q.QueueError) as caught:
            Q.KanbanQueue(lambda args: Out()).list_items()
        self.assertEqual(caught.exception.code, "kanban_invalid")

    def test_a_running_feature_continues_to_be_advanced(self):
        Q.QueueStore(self.tmp).write("f1", {"state": "running", "job_id": "job_1"})
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)
        accepted = mock.patch.object(Q, "_job_accepted", lambda state_dir, job_id: True)
        accepted.start()
        self.addCleanup(accepted.stop)
        called = []

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, ["job_1"])
        self.assertEqual(result["action"], "ready_for_review")
        self.assertEqual(self.queue.get("f1").status, "ready_for_review")

    def test_dependencies_unlock_on_job_evidence_not_card_status(self):
        self.queue.set_status("f1", "ready_for_review")   # card says done
        accepted = mock.patch.object(Q, "_job_accepted", lambda state_dir, job_id: False)
        accepted.start()
        self.addCleanup(accepted.stop)
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)
        called = []

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertNotIn("job_2", called, "f2 must not run while f1 has no accepted evidence")
        self.assertIn(result["action"], {"idle", "blocked"})

    def test_completion_requires_real_verification_and_integration_evidence(self):
        from bundlebees_plugin import verify_local

        record = {"attempt_id": "att_1", "ok": True, "sha": "a" * 40, "base_rev": "b" * 40,
                  "files": ["smoke/answer.py"], "tests": ["tests/test_smoke.py"],
                  "suite": {"exit_code": 0}, "gitleaks": {"exit_code": 0}, "mode": "commit-local"}
        path = verify_local.record_path(self.tmp / "state", "att_1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

        task = {"task_id": "T1", "state": "accepted", "attempts": ["att_1"],
                "contract_sha256": "c" * 64,
                "binding": {"source_rev": "b" * 40, "contract_sha256": "c" * 64}}
        job = {"tasks": [task], "integration": {"accepted_waves": [{"wave": 1, "commit": "d" * 40}],
                                                "last_commit": "d" * 40}}

        with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id: job):
            self.assertTrue(Q._job_accepted(self.tmp / "state", "j"))
            for broken in (
                {**job, "integration": {"accepted_waves": []}},
                {**job, "tasks": [{**task, "state": "verified"}]},
                {**job, "tasks": [{**task, "binding": {"source_rev": "e" * 40, "contract_sha256": "c" * 64}}]},
            ):
                with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id, b=broken: b):
                    self.assertFalse(Q._job_accepted(self.tmp / "state", "j"), broken)

        path.write_text(json.dumps({**record, "gitleaks": {"exit_code": 1}}))
        with mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id: job):
            self.assertFalse(Q._job_accepted(self.tmp / "state", "j"), "a failed secret scan cannot complete")

    def test_queue_run_refuses_to_start_without_a_scanner(self):
        with TemporaryDirectory() as tmp:
            args = type("A", (), {"command": "queue", "queue_handler": "run", "once": True, "repo": tmp,
                                  "state_dir": tmp, "backend": "json", "gitleaks": None})()
            ctx = type("C", (), {"config": {}})()
            with mock.patch("shutil.which", lambda name: None), mock.patch.dict("os.environ", {"GITLEAKS": ""}):
                code = job_cli.cmd_queue(ctx, args, state_dir=Path(tmp), client_factory=lambda: None)
        self.assertEqual(code, 2)

    # -- a tick's own status is not progress evidence ----------------------

    def _claimed(self, tick, *, task_state="dispatched"):
        """Claim f1, then run one pass with a job row in ``task_state``."""
        job = {"job_id": "job_1", "tasks": [{"task_id": "T1", "state": task_state}]}
        patcher = mock.patch.object(Q.jobs, "load_job", lambda state_dir, job_id, *, recover=True: job)
        patcher.start()
        self.addCleanup(patcher.stop)
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        self.queue.set_status("f1", "claimed")
        return store, Q.run_once(self.tmp, self.queue, tick=tick)

    def test_an_escalated_tick_blocks_the_card_instead_of_marking_it_running(self):
        store, result = self._claimed(lambda job_id: {"status": "escalated", "action": "integrate",
                                                      "detail": "two integration attempts failed"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")
        self.assertIn("integration attempts failed", store.read("f1")["evidence"]["detail"])

    def test_an_awaiting_decision_tick_blocks_the_card(self):
        store, result = self._claimed(lambda job_id: {"status": "awaiting_decision", "action": "recovery"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_locked_tick_defers_without_rewriting_the_claim(self):
        store, result = self._claimed(lambda job_id: {"status": "locked", "detail": "another live tick"})
        self.assertEqual(result["action"], "deferred")
        self.assertEqual(self.queue.get("f1").status, "claimed", "a pass that did not run must not re-status the card")
        self.assertEqual(store.read("f1")["state"], "claimed")

    def test_an_idle_tick_with_a_worker_in_flight_keeps_the_card_running(self):
        store, result = self._claimed(lambda job_id: {"status": "idle", "detail": "nothing to do"})
        self.assertEqual(result["action"], "running")
        self.assertEqual(self.queue.get("f1").status, "running")

    def test_an_idle_tick_with_no_worker_in_flight_blocks_the_card(self):
        # A verified row is integration work, not a worker; the tick said idle,
        # so integration declined to advance and nothing will move this job
        # without a decision.
        store, result = self._claimed(lambda job_id: {"status": "idle", "detail": "integration budget exhausted"},
                                      task_state="verified")
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")
        self.assertIn("integration budget exhausted", store.read("f1")["evidence"]["detail"])

    def test_an_idle_tick_on_a_rejected_job_blocks_the_card(self):
        store, result = self._claimed(lambda job_id: {"status": "idle"}, task_state="rejected")
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_requeued_card_names_its_existing_claim_instead_of_raising(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1"})
        called = []
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: called.append(job_id) or {"status": "ok"})
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(called, [])
        self.assertIn("blocked", result["detail"])
        # The stale record is not deleted; the card is made visibly blocked so
        # it cannot be re-admitted on every pass and starve later work.
        self.assertEqual(self.queue.get("f1").status, "blocked", "a refused admission must move the card out of queued")

    def test_a_refused_card_does_not_starve_the_next_eligible_card(self):
        self.path.write_text(json.dumps({
            "f1": card("f1"),
            "f2": card("f2", job_id="job_2"),
        }))
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1"})
        calls = []

        def tick(job_id):
            calls.append(job_id)
            return {"status": "ok"}

        with mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id}):
            first = Q.run_once(self.tmp, self.queue, tick=tick)
            second = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(first["action"], "blocked")
        self.assertEqual(first["feature"], "f1")
        self.assertEqual(second["feature"], "f2")
        self.assertEqual(calls, ["job_2"])

    def test_a_claim_held_for_another_job_is_refused(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "running", "job_id": "job_other"})
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(result["action"], "blocked")
        self.assertIn("job_other", result["detail"])
        self.assertIn("job_1", result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_a_failed_card_write_leaves_the_claim_active_for_a_retry(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "running", "job_id": "job_1"})
        self.queue.set_status("f1", "claimed")

        class BrokenQueue:
            def list_items(self):
                return self._queue.list_items()

            def set_status(self, *args, **kwargs):
                raise RuntimeError("card write failed")

        broken = BrokenQueue()
        broken._queue = self.queue

        with self.assertRaises(RuntimeError):
            Q.run_once(self.tmp, broken, tick=lambda job_id: {"status": "escalated", "detail": "terminal"})

        # The record must remain active when the card could not be acknowledged;
        # otherwise the next pass reports idle and the card stays claimed forever.
        self.assertEqual(store.read("f1")["state"], "running")
        self.assertEqual(self.queue.get("f1").status, "claimed")

    def test_a_pass_without_a_dispatch_capable_tick_claims_nothing(self):
        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "idle"}, admit=False)
        self.assertEqual(result["action"], "observation_only")
        self.assertIsNone(Q.QueueStore(self.tmp).read("f1"))
        self.assertEqual(self.queue.get("f1").status, "queued")

    def test_a_non_job_error_from_tick_propagates_and_leaves_both_claimed(self):
        for active in (False, True):
            with self.subTest(active=active), TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                path = tmp / "queue.json"
                card_status = "claimed" if active else "queued"
                path.write_text(json.dumps({"f1": card("f1", status=card_status)}))
                queue = Q.JsonQueue(path)
                store = Q.QueueStore(tmp)
                if active:
                    store.claim("f1", job_id="job_1")

                def tick(job_id):
                    raise RuntimeError("worker crashed")

                with mock.patch.object(Q, "_load_job_or_none",
                                       lambda state_dir, job_id: {"job_id": job_id}):
                    with self.assertRaises(RuntimeError) as caught:
                        Q.run_once(tmp, queue, tick=tick)

                self.assertEqual(str(caught.exception), "worker crashed")
                self.assertEqual(queue.get("f1").status, "claimed")
                record = store.read("f1")
                self.assertIsNotNone(record)
                self.assertEqual(record["state"], "claimed")

    # -- a typed provisioning refusal blocks the card instead of crashing ---

    def test_a_provisioning_refusal_on_a_new_card_reports_and_blocks_it(self):
        # Branch 2 (fresh admission): the tick refuses with the host's typed
        # quota denial; the run must report it and block the card, not crash.
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)

        def refuse(job_id):
            raise P.ProvisionDenied("quota_exhausted", "profile smoke already holds 5 provisioned projects (max 5)",
                                    "Inspect existing provisioned workspaces.")

        result = Q.run_once(self.tmp, self.queue, tick=refuse)

        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["feature"], "f1")
        self.assertEqual(result["job"], "job_1")
        self.assertEqual(result["code"], "quota_exhausted")
        self.assertIn("quota_exhausted", result["detail"])
        self.assertIn("max 5", result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        record = Q.QueueStore(self.tmp).read("f1")
        self.assertEqual(record["state"], "blocked")
        self.assertEqual(record["evidence"]["error"], "quota_exhausted")

    def test_a_provisioning_refusal_on_an_active_card_reports_and_blocks_it(self):
        # Branch 1 (already claimed): the same refusal must not escape run_once.
        def refuse(job_id):
            raise P.ProvisionError("provision_conflict", "conflicting provisioning request",
                                   "Inspect the journal; do not retry with the same key.")

        store, result = self._claimed(refuse)

        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["code"], "provision_conflict")
        self.assertIn("conflicting provisioning request", result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")
        self.assertEqual(store.read("f1")["evidence"]["error"], "provision_conflict")

    def test_a_denied_provisioning_state_short_circuits_the_queue_run(self):
        # A refusal ends the pass where the job CLI's exit does: nothing else
        # is admitted behind it, and the refused card is visibly blocked.
        self.path.write_text(json.dumps({
            "f1": card("f1"),
            "f2": card("f2", job_id="job_2"),
        }))
        patcher = mock.patch.object(Q, "_load_job_or_none", lambda state_dir, job_id: {"job_id": job_id})
        patcher.start()
        self.addCleanup(patcher.stop)
        calls = []

        def refuse(job_id):
            calls.append(job_id)
            raise B.ProvisionBindError("binding_refused", "receipt_revision: the checkout is not at the pinned revision",
                                       "Do not dispatch; inspect the host.")

        result = Q.run_once(self.tmp, self.queue, tick=refuse)

        self.assertEqual(calls, ["job_1"], "the run must stop at the refusal, not admit the next card")
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["feature"], "f1")
        self.assertEqual(result["code"], "binding_refused")
        self.assertIn("not at the pinned revision", result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(self.queue.get("f2").status, "queued")
        self.assertEqual(Q.QueueStore(self.tmp).read("f1")["state"], "blocked")

    # -- a card may diverge from its durable record -------------------------

    def _diverged(self, record_state, *, detail="tick refused: JobError wave_reconciliation_required",
                  card_status="claimed", record_extra=None):
        """A card status that disagrees with its already-written store record."""
        record = {"state": record_state, "job_id": "job_1", "detail": detail}
        record.update(record_extra or {})
        store = Q.QueueStore(self.tmp)
        store.write("f1", record)
        self.queue.set_status("f1", card_status, {"job_id": "job_1"})
        return store

    def _card_body(self):
        return json.loads(self.path.read_text())["f1"]

    def test_a_diverged_claimed_card_is_reconciled_from_its_blocked_record(self):
        store = self._diverged("blocked")
        called = []
        result = Q.run_once(self.tmp, self.queue,
                            tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, [], "reconciling a terminal record must not tick it")
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(result["feature"], "f1")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(result["reconciled"][0]["feature"], "f1")
        self.assertEqual(result["reconciled"][0]["state"], "blocked")
        self.assertIn("wave_reconciliation_required", result["detail"])
        self.assertIn("wave_reconciliation_required", self._card_body()["evidence"]["detail"])
        self.assertEqual(store.read("f1")["state"], "blocked",
                         "the reconciliation reports the existing record; it does not rewrite it")

        before = self.path.read_text()
        again = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})
        self.assertEqual(again["action"], "idle")
        self.assertEqual(self.path.read_text(), before,
                         "a second pass must not rewrite an already-reconciled card")

    def test_reconciliation_bounds_the_record_detail_on_the_card(self):
        detail = "D" * 1000
        self._diverged("blocked", detail=detail)

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})

        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        card_detail = self._card_body()["evidence"]["detail"]
        self.assertEqual(len(card_detail), 400)
        self.assertEqual(card_detail, detail[:400])

    def test_reconciliation_reports_failed_and_needs_approval_record_states(self):
        for state in ("failed", "needs_approval"):
            with self.subTest(state=state), TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                path = tmp / "queue.json"
                path.write_text(json.dumps({"f1": card("f1", status="claimed")}))
                queue = Q.JsonQueue(path)
                store = Q.QueueStore(tmp)
                store.write("f1", {"state": state, "job_id": "job_1",
                                   "detail": f"{state} by contract"})

                result = Q.run_once(tmp, queue, tick=lambda job_id: {"status": "ok"})

                self.assertEqual(result["action"], "reconciled")
                self.assertEqual(result["status"], state)
                self.assertEqual(result["reconciled"][0]["state"], state)
                self.assertIn("by contract", result["detail"])
                self.assertEqual(queue.get("f1").status, state)
                self.assertEqual(store.read("f1")["state"], state,
                                 "the record state remains the source of truth")

    def test_a_ready_for_review_record_only_reviews_the_card_when_the_job_is_accepted(self):
        # Choice: a ready_for_review record is trusted only when the existing
        # _job_accepted rules agree. When they do not, the card is blocked (not
        # left at claimed) and the record is left untouched; it is never moved
        # to review on the record's word alone.
        for accepted in (True, False):
            with self.subTest(accepted=accepted), TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                path = tmp / "queue.json"
                path.write_text(json.dumps({"f1": card("f1", status="claimed")}))
                queue = Q.JsonQueue(path)
                store = Q.QueueStore(tmp)
                store.write("f1", {"state": "ready_for_review", "job_id": "job_1",
                                   "detail": "review requested"})

                with mock.patch.object(Q, "_job_accepted",
                                       lambda state_dir, job_id, accepted=accepted: accepted):
                    result = Q.run_once(tmp, queue, tick=lambda job_id: {"status": "ok"})

                expected = "ready_for_review" if accepted else "blocked"
                self.assertEqual(result["action"], "reconciled")
                self.assertEqual(result["status"], expected)
                self.assertEqual(queue.get("f1").status, expected)
                self.assertEqual(store.read("f1")["state"], "ready_for_review")
                if accepted:
                    self.assertIn("review requested", result["detail"])
                else:
                    self.assertIn("not accepted", result["detail"])

    def test_reconciliation_leaves_a_genuinely_active_record_alone(self):
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "running", "job_id": "job_1"})
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})
        called = []

        with mock.patch.object(Q, "_load_job_or_none",
                               lambda state_dir, job_id: {"job_id": job_id}):
            result = Q.run_once(self.tmp, self.queue,
                                tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, ["job_1"], "an active record is advanced, never reconciled")
        self.assertEqual(result["action"], "running")
        self.assertNotIn("reconciled", result)
        self.assertEqual(self.queue.get("f1").status, "running")

    def test_a_terminal_record_is_not_ticked_while_active_work_advances(self):
        self.path.write_text(json.dumps({
            "f1": card("f1", status="claimed"),
            "f2": card("f2", job_id="job_2", status="claimed"),
        }))
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1",
                           "detail": "tick refused: JobError wave_reconciliation_required"})
        store.write("f2", {"state": "running", "job_id": "job_2"})
        called = []

        with mock.patch.object(Q, "_load_job_or_none",
                               lambda state_dir, job_id: {"job_id": job_id}):
            result = Q.run_once(self.tmp, self.queue,
                                tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, ["job_2"], "only the genuinely active job is ticked")
        self.assertEqual(result["action"], "running")
        self.assertEqual(result["feature"], "f2")
        self.assertEqual(result["reconciled"][0]["feature"], "f1")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        self.assertEqual(store.read("f1")["state"], "blocked")

    def test_reconciliation_does_not_repair_an_unreadable_record(self):
        store = Q.QueueStore(self.tmp)
        store.path("f1").write_text("{")
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})

        with self.assertRaises(Q.QueueError) as caught:
            Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})

        self.assertEqual(caught.exception.code, "queue_state_corrupt")
        self.assertEqual(self.queue.get("f1").status, "claimed")

    def test_reconciliation_treats_a_permission_denied_record_as_state_unreadable(self):
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})
        record_path = store.path("f1")
        real_read_text = Path.read_text

        def deny_record_read(path, *args, **kwargs):
            if path == record_path:
                raise PermissionError(13, "Permission denied", str(path))
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny_record_read):
            with self.assertRaises(Q.QueueError) as caught:
                Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})

        self.assertEqual(caught.exception.code, "queue_state_unreadable")
        self.assertTrue(caught.exception.fix, "a refusal must carry a repair hint")
        self.assertEqual(self.queue.get("f1").status, "claimed")
        self.assertEqual(store.read("f1")["state"], "claimed")

    def test_reconciliation_maps_an_unknown_record_state_to_blocked(self):
        store = self._diverged("done")
        called = []

        result = Q.run_once(self.tmp, self.queue,
                            tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, [])
        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reconciled"][0]["record_state"], "done")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        card_body = self._card_body()
        self.assertEqual(card_body["status"], "blocked")
        self.assertIn(card_body["status"], Q.STATES)
        self.assertIn("done", card_body["evidence"]["detail"])
        self.assertIn("not copied", card_body["evidence"]["detail"])
        self.assertLessEqual(len(card_body["evidence"]["detail"]), 400)
        self.assertEqual(store.read("f1")["state"], "done",
                         "the reconciliation reports the existing record; it does not rewrite it")

    def test_reconciliation_bounds_an_unknown_record_state_detail(self):
        unknown_state = "Z" * 1000
        self._diverged(unknown_state)

        result = Q.run_once(self.tmp, self.queue, tick=lambda job_id: {"status": "ok"})

        self.assertEqual(result["action"], "reconciled")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.queue.get("f1").status, "blocked")
        detail = self._card_body()["evidence"]["detail"]
        self.assertEqual(len(detail), 400)
        self.assertIn("not copied", detail)

    def test_reconciliation_preserves_a_features_envelope_and_its_other_cards(self):
        target = {"feature_id": "f1", "repo": "mojomast/a", "job_id": "job_1",
                  "status": "claimed"}
        other = {"feature_id": "f2", "repo": "mojomast/b", "job_id": "job_2",
                 "title": "Second", "status": "ready_for_review"}
        document = {
            "schema": "bundlebees.queue/1",
            "generated_at": "2026-11-18T04:27:23Z",
            "note": "live-shaped queue",
            "unknown": {"keep": ["these", "values"]},
            "features": [target, other],
        }
        self.path.write_text(json.dumps(document))
        store = Q.QueueStore(self.tmp)
        store.write("f1", {"state": "blocked", "job_id": "job_1",
                           "detail": "tick refused: wave reconciliation required"})
        called = []

        result = Q.run_once(self.tmp, self.queue,
                            tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, [])
        self.assertEqual(result["action"], "reconciled")
        body = json.loads(self.path.read_text())
        self.assertEqual(body["schema"], document["schema"])
        self.assertEqual(body["generated_at"], document["generated_at"])
        self.assertEqual(body["note"], document["note"])
        self.assertEqual(body["unknown"], document["unknown"])
        self.assertNotIn("f1", body, "a document-shaped queue must not be rewritten as a flat map")
        self.assertEqual(body["features"][1], other, "a non-target card must be untouched")
        self.assertEqual(json.dumps(body["features"][1], sort_keys=True),
                         json.dumps(other, sort_keys=True))
        self.assertEqual(body["features"][0]["status"], "blocked")
        self.assertEqual(body["features"][0]["evidence"]["detail"],
                         "tick refused: wave reconciliation required")

    def test_a_claimed_card_without_a_record_keeps_its_existing_outcome(self):
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})
        called = []

        result = Q.run_once(self.tmp, self.queue,
                            tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(result["action"], "idle")
        self.assertEqual(called, [])
        self.assertEqual(self.queue.get("f1").status, "claimed",
                         "a missing record is not a divergence to repair")

    def test_a_claimed_card_with_an_unrepresentable_id_has_no_record_to_reconcile(self):
        self.path.write_text(json.dumps({"feat/1": card("feat/1", status="claimed")}))
        queue = Q.JsonQueue(self.path)

        result = Q.run_once(self.tmp, queue, tick=lambda job_id: {"status": "ok"})

        self.assertEqual(result["action"], "idle")
        self.assertEqual(queue.get("feat/1").status, "claimed")

    # -- a typed job refusal settles the card and its record ----------------

    def _assert_blocked_by_job_error(self, result, store, *, code, message):
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["code"], code)
        self.assertIn(code, result["detail"])
        self.assertIn(message, result["detail"])
        self.assertEqual(self.queue.get("f1").status, "blocked")
        record = store.read("f1")
        self.assertEqual(record["state"], "blocked")
        self.assertEqual(record["evidence"]["error"], code)
        self.assertIn(message, record["detail"])
        self.assertIn(message, record["evidence"]["detail"])

    def test_a_job_error_from_an_active_tick_blocks_the_card_and_record(self):
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})
        called = []

        def tick(job_id):
            called.append(job_id)
            raise jobs.JobError("wave_reconciliation_required",
                                "the wave record diverged", "Reconcile the wave.")

        with mock.patch.object(Q, "_load_job_or_none",
                               lambda state_dir, job_id: {"job_id": job_id}):
            result = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(called, ["job_1"])
        self._assert_blocked_by_job_error(
            result, store, code="wave_reconciliation_required",
            message="the wave record diverged")

    def test_a_job_error_from_the_fresh_claim_tick_blocks_the_card_and_record(self):
        called = []

        def tick(job_id):
            called.append(job_id)
            raise jobs.JobError("wave_reconciliation_required",
                                "the wave record diverged", "Reconcile the wave.")

        with mock.patch.object(Q, "_load_job_or_none",
                               lambda state_dir, job_id: {"job_id": job_id}):
            result = Q.run_once(self.tmp, self.queue, tick=tick)

        self.assertEqual(called, ["job_1"])
        self._assert_blocked_by_job_error(
            result, Q.QueueStore(self.tmp), code="wave_reconciliation_required",
            message="the wave record diverged")

    def test_a_job_error_from_the_active_claim_load_blocks_the_card_and_record(self):
        store = Q.QueueStore(self.tmp)
        store.claim("f1", job_id="job_1")
        self.queue.set_status("f1", "claimed", {"job_id": "job_1"})
        called = []

        def corrupt(state_dir, job_id):
            raise jobs.JobError("job_invalid", "job document could not be read",
                                "Inspect it by hand.")

        with mock.patch.object(Q, "_load_job_or_none", corrupt):
            result = Q.run_once(self.tmp, self.queue,
                                tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, [], "a corrupt active job is not ticked")
        self._assert_blocked_by_job_error(
            result, store, code="job_invalid", message="job document could not be read")

    def test_a_job_error_from_the_fresh_claim_load_blocks_the_card_and_record(self):
        called = []

        def corrupt(state_dir, job_id):
            raise jobs.JobError("job_invalid", "job document could not be read",
                                "Inspect it by hand.")

        with mock.patch.object(Q, "_load_job_or_none", corrupt):
            result = Q.run_once(self.tmp, self.queue,
                                tick=lambda job_id: called.append(job_id) or {"status": "ok"})

        self.assertEqual(called, [], "a corrupt fresh job is not ticked")
        self._assert_blocked_by_job_error(
            result, Q.QueueStore(self.tmp), code="job_invalid",
            message="job document could not be read")


if __name__ == "__main__":
    unittest.main()
