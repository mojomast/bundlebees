"""Standing-grant tests: enrollment, denial and atomic budget."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
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
        limits=G.Limits(max_bees=1, max_jobs=2, max_concurrent_jobs=2, wall_clock_s=1800, budget_units=2),
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

    def test_active_concurrency_is_limited_independently_of_lifetime_jobs(self):
        self.store.enroll(grant(grant_id="grant-wide", limits=G.Limits(
            max_bees=1, max_jobs=5, max_concurrent_jobs=1, wall_clock_s=1800, budget_units=5), task_scopes=("smoke/",)),
            statement="wide")
        self.store.derive("grant-wide", job_id="job_1", repo="mojomast/rizzstack-smoke", host="worker-1",
                          profile="smoke", source_rev=SHA, assigned_files=["smoke/a.py"], bees=1, wall_clock_s=60)

        with self.assertRaises(G.GrantError) as caught:
            self.store.derive("grant-wide", job_id="job_2", repo="mojomast/rizzstack-smoke", host="worker-1",
                              profile="smoke", source_rev=SHA, assigned_files=["smoke/a.py"], bees=1, wall_clock_s=60)
        self.assertEqual(caught.exception.code, "concurrency_exhausted")

        self.store.complete("grant-wide", "job_1")
        second = self.store.derive("grant-wide", job_id="job_2", repo="mojomast/rizzstack-smoke", host="worker-1",
                                   profile="smoke", source_rev=SHA, assigned_files=["smoke/a.py"], bees=1, wall_clock_s=60)
        self.assertEqual(second["job_id"], "job_2")

    def test_completing_a_job_does_not_replenish_the_cumulative_budget(self):
        self.store.enroll(grant(grant_id="grant-budget", limits=G.Limits(
            max_bees=1, max_jobs=5, max_concurrent_jobs=5, wall_clock_s=1800, budget_units=2)), statement="budget")
        kwargs = dict(repo="mojomast/rizzstack-smoke", host="worker-1", profile="smoke",
                      source_rev=SHA, assigned_files=["smoke/a.py"], bees=1, wall_clock_s=60)
        self.store.derive("grant-budget", job_id="job_1", **kwargs)
        self.store.derive("grant-budget", job_id="job_2", **kwargs)
        self.store.complete("grant-budget", "job_1")

        with self.assertRaises(G.GrantError) as caught:
            self.store.derive("grant-budget", job_id="job_3", **kwargs)
        self.assertEqual(caught.exception.code, "budget_exhausted")

    def test_an_empty_scope_is_never_unrestricted(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(assigned_files=[])
        self.assertEqual(caught.exception.code, "scope_empty")

    def test_a_grant_with_no_scopes_refuses_derivation_unless_explicitly_unrestricted(self):
        self.store.enroll(grant(grant_id="grant-narrow", task_scopes=()), statement="no scopes")
        with self.assertRaises(G.GrantError) as caught:
            self.store.derive("grant-narrow", job_id="job_1", repo="mojomast/rizzstack-smoke", host="worker-1",
                              profile="smoke", source_rev=SHA, assigned_files=["smoke/a.py"], bees=1, wall_clock_s=60)
        self.assertEqual(caught.exception.code, "scope_not_granted")

        self.store.enroll(grant(grant_id="grant-any", task_scopes=(), unrestricted_scope=True), statement="any file")
        authorization = self.store.derive("grant-any", job_id="job_2", repo="mojomast/rizzstack-smoke", host="worker-1",
                                          profile="smoke", source_rev=SHA, assigned_files=["src/anything.py"], bees=1, wall_clock_s=60)
        self.assertEqual(authorization["assigned_files"], ["src/anything.py"])

    def test_a_reservation_is_idempotent_for_the_same_request(self):
        first = self.derive(job_id="job_1")
        again = self.derive(job_id="job_1")
        self.assertEqual(first["request_digest"], again["request_digest"])
        self.assertEqual(first["budget_reservation"], again["budget_reservation"])

        with self.assertRaises(G.GrantError) as caught:
            self.derive(job_id="job_1", assigned_files=["smoke/other.py"])
        self.assertEqual(caught.exception.code, "reservation_conflict")

    def test_invalid_job_ids_and_absolute_scopes_are_refused(self):
        with self.assertRaises(G.GrantError) as caught:
            self.derive(job_id="../escape")
        self.assertEqual(caught.exception.code, "job_id_invalid")

        with self.assertRaises(G.GrantError) as caught:
            self.derive(assigned_files=["/etc/passwd"])
        self.assertEqual(caught.exception.code, "scope_invalid")


class OwnerLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_an_active_lock_is_never_stolen_for_being_old(self):
        lock = self.root / "g.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": time.time() - 99999}))

        with self.assertRaises(G.GrantError) as caught:
            with G.owner_lock(lock):
                pass
        self.assertEqual(caught.exception.code, "grant_locked")

    def test_a_dead_owners_lock_is_recovered(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        lock = self.root / "g.lock"
        lock.write_text(json.dumps({"pid": child.pid, "host": socket.gethostname(), "at": time.time()}))
        child.kill()
        child.wait(timeout=10)

        with G.owner_lock(lock):
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_a_lock_from_another_host_is_waited_on_not_stolen(self):
        lock = self.root / "g.lock"
        lock.write_text(json.dumps({"pid": 1, "host": "some-other-host", "at": time.time() - 99999}))

        with self.assertRaises(G.GrantError):
            with G.owner_lock(lock):
                pass


if __name__ == "__main__":
    unittest.main()
