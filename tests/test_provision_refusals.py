"""A known refusal must survive another tick without becoming a lost POST."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bundlebees_plugin import provision_bind as B, provisioning as P


class ProvisionRefusals(unittest.TestCase):
    def test_known_refusal_stays_denied_and_never_reposts(self):
        for error in (P.ProvisionDenied("profile_not_enrolled", "not enrolled"),
                      P.ProvisionUnavailable("provisioning_disabled", "disabled"),
                      P.ProvisionError("provision_conflict", "conflict")):
            with self.subTest(code=error.code), tempfile.TemporaryDirectory() as tmp:
                job = {"job_id": "j", "tasks": [{"task_id": "T1", "provision": {
                    "repo": "mojomast/x", "source_rev": "a" * 40,
                    "profile": "smoke", "project": "smoke"}}]}
                saved = []
                class Refusing:
                    posts = 0
                    lookups = 0
                    def provision(self, request):
                        self.posts += 1
                        raise error
                    def authoritative_receipt(self, *args, **kwargs):
                        self.lookups += 1
                        raise P.ProvisionError("receipt_unknown", "absent")
                host = Refusing()
                with patch.object(B, "_authorization_matches"), patch.object(
                    B.jobs, "save_job", side_effect=lambda state, doc: saved.append(copy.deepcopy(doc))
                ):
                    with self.assertRaises(P.ProvisionError):
                        B._advance_locked(Path(tmp), job, provisioner=host)
                    restarted = saved[-1]
                    self.assertEqual(restarted["tasks"][0]["provisioning"]["state"], "denied")
                    self.assertEqual(restarted["tasks"][0]["provisioning"]["error_code"], error.code)
                    with self.assertRaises(B.ProvisionBindError) as caught:
                        B._advance_locked(Path(tmp), restarted, provisioner=host)
                    self.assertEqual(caught.exception.code, error.code)
                self.assertEqual(host.posts, 1)
                self.assertEqual(host.lookups, 0)

    def test_uncertain_post_keeps_read_only_reconciliation(self):
        job = {"job_id": "j", "tasks": [{"task_id": "T1", "provision": {
            "repo": "mojomast/x", "source_rev": "a" * 40,
            "profile": "smoke", "project": "smoke"}}]}
        class Lost:
            posts = 0
            lookups = 0
            def provision(self, request):
                self.posts += 1
                raise P.ProvisionUncertain("provision_uncertain", "lost response")
            def authoritative_receipt(self, *args, **kwargs):
                self.lookups += 1
                raise P.ProvisionError("receipt_unknown", "absent")
        host = Lost()
        with patch.object(B, "_authorization_matches"), patch.object(B.jobs, "save_job"):
            for _ in range(2):
                self.assertTrue(B._advance_locked(Path("."), job, provisioner=host)["blocked"])
        self.assertEqual(host.posts, 1)
        self.assertEqual(host.lookups, 2)
