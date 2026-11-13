"""Standing-grant tests: enrollment, denial and atomic budget."""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bundlebees_plugin import grants as G

KEY = b"k" * 32
SHA = "dde2aa42d09c05e43895b3120cb3312f765eb876"


def grant(**overrides) -> G.Grant:
    body = dict(
        grant_id="grant-smoke",
        approved_by="kyle",
        approved_at="2026-11-13T00:00:00Z",
        expires_at=time.time() + 3600,
        repos=("mojomast/rizzstack-smoke",),
        hosts=("worker-1",),
        profiles=("smoke",),
        task_scopes=("smoke/",),
        limits=G.Limits(max_bees=1, max_jobs=2, max_concurrent_jobs=1, wall_clock_s=1800, budget_units=2),
    )
    body.update(overrides)
    return G.Grant(**body)


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.tty = True
        self.store = G.GrantStore(self.root, key=KEY, tty=lambda: self.tty, actor=lambda: "tester")

    def derive(self, **overrides):
        kwargs = dict(job_id="job_1", repo="mojomast/rizzstack-smoke", host="worker-1", profile="smoke",
                      source_rev=SHA, assigned_files=["smoke/answer.py"], bees=1, wall_clock_s=900)
        kwargs.update(overrides)
        return self.store.derive("grant-smoke", **kwargs)


class EnrollmentTests(Harness):
    def test_enrollment_requires_a_real_interactive_terminal(self):
        self.tty = False
        with self.assertRaises(G.GrantError) as caught:
            self.store.enroll(grant(), statement="one smoke job")
        self.assertEqual(caught.exception.code, "interactive_required")
        self.assertFalse((self.root / "grants" / "grant-smoke.json").exists())

    def test_enrollment_requires_the_humans_own_statement(self):
        with self.assertRaises(G.GrantError) as caught:
            self.store.enroll(grant(), statement="  ")
        self.assertEqual(caught.exception.code, "statement_required")

    def test_a_tampered_grant_does_not_verify(self):
        self.store.enroll(grant(), statement="one smoke job")
        path = self.root / "grants" / "grant-smoke.json"
        body = json.loads(path.read_text())
        body["limits"]["max_jobs"] = 99
        path.write_text(json.dumps(body))

        with self.assertRaises(G.GrantError) as caught:
            self.store.load("grant-smoke")
        self.assertEqual(caught.exception.code, "seal_invalid")

    def test_revocation_is_recorded_and_audited(self):
        self.store.enroll(grant(), statement="one smoke job")
        self.store.revoke("grant-smoke", statement="stop")

        with self.assertRaises(G.GrantError) as caught:
            self.derive()
        self.assertEqual(caught.exception.code, "grant_revoked")
        events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events], ["grant.enrolled", "grant.revoked"])


class DerivationTests(Harness):
    def setUp(self):
        super().setUp()
        self.store.enroll(grant(), statement="one smoke job")

    def test_a_covered_job_derives_a_narrow_authorization(self):
        authorization = self.derive()

        self.assertEqual(authorization["authority"], "standing-grant")
        self.assertEqual(authorization["grant_id"], "grant-smoke")
        self.assertEqual(authorization["bees"], 1)
        self.assertEqual(authorization["assigned_files"], ["smoke/answer.py"])
        self.assertEqual(len(authorization["digest"]), 64)
        self.assertTrue(authorization["budget_reservation"].endswith(".json"))

    def test_an_expired_grant_is_refused(self):
        self.store.enroll(grant(), statement="one smoke job")
        later = G.GrantStore(self.root, key=KEY, tty=lambda: True, now=lambda: time.time() + 7200)
        with self.assertRaises(G.GrantError) as caught:
            later.derive("grant-smoke", job_id="job_1", repo="mojomast/rizzstack-smoke", host="worker-1",
                         profile="smoke", source_rev=SHA, assigned_files=["smoke/answer.py"], bees=1, wall_clock_s=60)
        self.assertEqual(caught.exception.code, "grant_expired")

    def test_a_repository_outside_the_grant_is_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(repo="mojomast/workflow")
        self.assertEqual(caught.exception.code, "repo_not_granted")

    def test_a_worker_host_outside_the_grant_is_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(host="worker-2")
        self.assertEqual(caught.exception.code, "host_not_granted")

    def test_a_file_outside_the_granted_scope_is_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(assigned_files=["src/secret.py"])
        self.assertEqual(caught.exception.code, "scope_not_granted")

    def test_exceeding_the_bee_limit_is_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(bees=2)
        self.assertEqual(caught.exception.code, "bees_not_granted")

    def test_exceeding_the_wall_clock_is_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(wall_clock_s=99999)
        self.assertEqual(caught.exception.code, "wall_clock_not_granted")

    def test_the_job_limit_is_enforced_across_derivations(self):
        self.derive(job_id="job_1")
        self.derive(job_id="job_2")
        with self.assertRaises(G.GrantError) as caught:
            self.derive(job_id="job_3")
        self.assertEqual(caught.exception.code, "jobs_exhausted")

    def test_concurrent_derivations_cannot_overspend_the_budget(self):
        results: list[str] = []

        def attempt(index: int):
            try:
                self.derive(job_id=f"job_{index}")
                results.append("ok")
            except G.GrantError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("ok"), 2)
        self.assertEqual(results.count("budget_exhausted") + results.count("jobs_exhausted"), 4)

    def test_releasing_a_reservation_frees_the_budget(self):
        self.derive(job_id="job_1")
        self.derive(job_id="job_2")
        self.store.release("grant-smoke", "job_1")

        authorization = self.derive(job_id="job_3")
        self.assertEqual(authorization["job_id"], "job_3")


if __name__ == "__main__":
    unittest.main()
