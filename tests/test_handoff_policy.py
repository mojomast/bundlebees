"""P2: a human-enrolled policy is the only root of a dependent job's authority.

These tests pin the trust defects closed:

* selection is by the intent packet's explicit ``policy_id`` — never the first
  policy that happens to mention the dependency;
* enrollment is create-only and race-safe, and cannot be renewed or widened;
* enrollment is sealed with a separate human approval key that the coordinator
  may verify but never mint, and the coordinator's job key cannot vouch for it;
* a derived authorization is create-only and re-checks the verified policy on
  every dimension instead of trusting a caller-supplied ceiling;
* ``materialize`` resolves the accepted dependency commit into an immutable
  source-resolution record and leaves the intent packet untouched.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from bundlebees_plugin import contract as contract_mod
from bundlebees_plugin import handoff, job as jobs, packets, verify_local

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

REPO = "mojomast/smoke-real"
ROOT = "a" * 40
OTHER_SHA = "b" * 40
WORKFLOW_REF = "c" * 40


def template(source_rev: str = ROOT) -> dict:
    return {
        "contract_version": "acdd/1", "id": "SMOKE-B", "title": "double_answer() returns 84",
        "outcome": {"summary": "double_answer() returns 84", "observable_behavior": ["double_answer() returns 84"]},
        "source_rev": source_rev,
        "scope": {"assigned_files": ["smoke/double.py"], "test_files": ["tests/test_double.py"]},
        "acceptance_criteria": [{"id": "AC-001", "statement": "double_answer() returns 84",
                                 "tests": ["tests/test_double.py"], "verification": "test"}],
        "tdd": {"applicable": True, "test_first": False, "reason": "approved test fails at the baseline"},
        "verification": {"independent": True}, "evidence_required": ["green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }


def intent_packet(**overrides) -> dict:
    packet = {
        "packet_version": "rizzstack.feature-packet/1",
        "feature_id": "feature-b", "job_id": "job_b",
        "repository": {"identity": REPO, "branch": "main"},
        "source_policy": {"kind": "descendant", "policy_id": "policy-b",
                          "dependencies": ["feature-a"], "root_revision": ROOT},
        "budget": {"bees": 1, "wall_clock_s": 1800},
        "scope": {"lease": ["smoke/double.py"], "read_only": ["tests/test_double.py"]},
        "human_gates": ["merge", "release", "deploy", "publish"],
        "tests": ["tests/test_double.py"],
        "contract_template": template(),
        "workflow_ref": WORKFLOW_REF,
        "authorization": {"approved_by": "reviewer", "reference": "auth:job_b"},
    }
    packet.update(overrides)
    return packet


def policy_body(**overrides) -> dict:
    body = {
        "kind": handoff.POLICY_KIND, "policy_id": "policy-b",
        "statement": "Feature B may build on Feature A's accepted commit",
        "repo": REPO, "root_rev": ROOT,
        "dependencies": ["job_a"], "dependency_features": ["feature-a"],
        "dependent_feature": "feature-b", "dependent_job": "job_b",
        "scope": ["smoke/double.py"],
        "budget": {"bees": 1, "wall_clock_s": 1800},
        "contract_template": template(), "gates": ["merge"],
        "host": "", "profile": "", "project": "", "workflow_ref": "", "at": "2026-11-13T00:00:00Z",
    }
    body.update(overrides)
    return body


def seal_policy(state: Path, body: dict, key: bytes) -> Path:
    path = handoff.policy_path(state, body["policy_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jobs._seal(body, key), indent=2, sort_keys=True))
    return path


def mark_accepted(state: Path, job_id: str, sha: str, attempt: str = "att_1") -> None:
    """Give a job one accepted integration wave backed by a real verification record."""

    record = verify_local.record_path(state, attempt)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"attempt_id": attempt, "ok": True, "sha": sha, "base_rev": ROOT,
                                  "files": ["smoke/answer.py"], "suite": {"exit_code": 0},
                                  "gitleaks": {"exit_code": 0}, "mode": "commit-local"}))
    job = jobs.load_job(state, job_id)
    job["integration"] = {"wave": "w1", "state": "accepted", "last_commit": sha,
                          "accepted_waves": [{"wave": 1, "commit": sha, "attempts": [attempt]}]}
    jobs.save_job(state, job)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()

    def approval(self) -> bytes:
        return handoff.create_approval_key(self.state)

    def enroll(self, **overrides):
        kwargs = dict(
            policy_id="policy-b", statement="Feature B may build on Feature A's accepted commit",
            repo=REPO, root_rev=ROOT, dependency_job="job_a", dependency_feature="feature-a",
            dependent_feature="feature-b", dependent_job="job_b", scope=["smoke/double.py"],
            bees=1, wall_clock_s=1800, contract_template=template(), gates=("merge",),
            workflow_ref=WORKFLOW_REF,
        )
        kwargs.update(overrides)
        return handoff.enroll_policy(self.state, **kwargs)

    def authorize_a(self) -> dict:
        return jobs.record_authorization(
            self.state, job_id="job_a", statement="Feature A: answer() == 42", repo=REPO, base_rev=ROOT,
            scope=["smoke/answer.py"], bees=1, wall_clock_s=1800, require_tty=False,
            interactive=False, by="reviewer")


# ---------------------------------------------------------------------------
# (c) Separate approval authority
# ---------------------------------------------------------------------------


class ApprovalAuthority(Base):
    def test_enrollment_needs_the_approval_key_and_the_coordinator_key_does_not_count(self):
        self.authorize_a()                       # the coordinator's lease.key now exists
        self.assertTrue((self.state / "lease.key").exists())
        self.assertFalse(handoff.approval_key_path(self.state).exists())

        with self.assertRaises(handoff.HandoffError) as caught:
            self.enroll()
        self.assertEqual(caught.exception.code, "approval_key_missing")
        self.assertFalse(handoff.policy_path(self.state, "policy-b").exists())
        self.assertFalse(handoff.approval_key_path(self.state).exists(), "verification must not mint the key")

    def test_a_policy_sealed_with_the_coordinator_job_key_does_not_verify(self):
        self.authorize_a()
        self.approval()                          # a real approval key exists...
        coordinator_key = jobs._load_key(self.state)
        seal_policy(self.state, policy_body(), coordinator_key)   # ...but the policy is sealed with lease.key

        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.load_policy(self.state, "policy-b")
        self.assertEqual(caught.exception.code, "seal_invalid")

        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, intent_packet())
        self.assertEqual(caught.exception.code, "seal_invalid")

    def test_the_approval_key_is_created_once_and_never_replaced(self):
        key = self.approval()
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.create_approval_key(self.state)
        self.assertEqual(caught.exception.code, "approval_key_exists")
        self.assertEqual(handoff.load_approval_key(self.state), key)

    def test_a_group_readable_approval_key_is_refused(self):
        self.approval()
        os.chmod(handoff.approval_key_path(self.state), 0o644)
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.load_policy(self.state, "policy-b")
        self.assertEqual(caught.exception.code, "approval_key_invalid")


# ---------------------------------------------------------------------------
# (b) Create-only enrollment under concurrent writers
# ---------------------------------------------------------------------------


class Enrollment(Base):
    def test_reenrolling_the_same_id_is_refused_and_cannot_widen(self):
        self.approval()
        self.enroll()
        original = handoff.load_policy(self.state, "policy-b")

        with self.assertRaises(handoff.HandoffError) as caught:
            self.enroll(scope=["smoke/double.py", "smoke/everything.py"], bees=9)
        self.assertEqual(caught.exception.code, "policy_exists")
        self.assertEqual(handoff.load_policy(self.state, "policy-b"), original)
        self.assertEqual(original["budget"], {"bees": 1, "wall_clock_s": 1800})

    def test_concurrent_enrollment_creates_exactly_one_policy(self):
        self.approval()
        outcomes: list[str] = []
        lock = threading.Lock()
        start = threading.Barrier(8)

        def writer(index: int) -> None:
            try:
                start.wait(timeout=10)
                self.enroll(statement=f"Feature B, writer {index}, may build on A")
            except handoff.HandoffError as exc:
                with lock:
                    outcomes.append(exc.code)
            else:
                with lock:
                    outcomes.append("created")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertEqual(outcomes.count("created"), 1, outcomes)
        self.assertEqual(outcomes.count("policy_exists"), 7, outcomes)
        handoff.load_policy(self.state, "policy-b")   # the surviving document verifies

    def test_a_second_policy_for_one_dependent_job_is_refused(self):
        self.approval()
        self.enroll()
        with self.assertRaises(handoff.HandoffError) as caught:
            self.enroll(policy_id="policy-b2")
        self.assertEqual(caught.exception.code, "policy_conflict")


# ---------------------------------------------------------------------------
# (a) Explicit selection by the intent packet's policy_id
# ---------------------------------------------------------------------------


class Selection(Base):
    def setUp(self):
        super().setUp()
        self.approval()

    def test_the_packet_policy_id_selects_the_policy_not_the_first_match(self):
        # An alphabetically earlier policy also mentions dependency job_a but
        # governs another dependent job. The packet names policy-b.
        self.enroll(policy_id="policy-a2", dependent_feature="feature-c", dependent_job="job_c")
        self.enroll()

        policy = handoff.policy_for_intent(self.state, intent_packet())
        self.assertEqual(policy["policy_id"], "policy-b")
        self.assertEqual(policy["dependent_job"], "job_b")

        other = handoff.policy_for_intent(
            self.state, intent_packet(feature_id="feature-c", job_id="job_c",
                                      source_policy={"kind": "descendant", "policy_id": "policy-a2",
                                                     "dependencies": ["feature-a"], "root_revision": ROOT}))
        self.assertEqual(other["policy_id"], "policy-a2")

    def test_a_packet_naming_an_unknown_policy_is_refused(self):
        self.enroll()
        packet = intent_packet(source_policy={"kind": "descendant", "policy_id": "policy-z",
                                              "dependencies": ["feature-a"], "root_revision": ROOT})
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, packet)
        self.assertEqual(caught.exception.code, "policy_missing")

    def test_a_packet_without_a_policy_id_is_refused(self):
        self.enroll()
        packet = intent_packet(source_policy={"kind": "descendant", "dependencies": ["feature-a"],
                                              "root_revision": ROOT})
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, packet)
        self.assertEqual(caught.exception.code, "policy_missing")

    def test_a_policy_bound_to_another_job_feature_dependency_or_root_is_refused(self):
        self.enroll()
        cases = (
            (intent_packet(job_id="job_other", feature_id="feature-b"), "policy_job_mismatch"),
            (intent_packet(feature_id="feature-other"), "policy_feature_mismatch"),
            (intent_packet(source_policy={"kind": "descendant", "policy_id": "policy-b",
                                          "dependencies": ["feature-z"], "root_revision": ROOT}),
             "policy_dependency_mismatch"),
            (intent_packet(source_policy={"kind": "descendant", "policy_id": "policy-b",
                                          "dependencies": ["feature-a"], "root_revision": OTHER_SHA}),
             "policy_root_mismatch"),
        )
        for packet, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(handoff.HandoffError) as caught:
                    handoff.policy_for_intent(self.state, packet)
                self.assertEqual(caught.exception.code, code)

    def test_a_packet_wider_than_the_policy_is_refused(self):
        self.enroll()
        wider_scope = intent_packet(scope={"lease": ["smoke/double.py", "smoke/answer.py"],
                                           "read_only": ["tests/test_double.py"]})
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, wider_scope)
        self.assertEqual(caught.exception.code, "policy_scope_widened")

        wider_budget = intent_packet(budget={"bees": 3, "wall_clock_s": 1800})
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, wider_budget)
        self.assertEqual(caught.exception.code, "policy_budget_exceeded")

    def test_a_different_contract_template_is_refused(self):
        self.enroll()
        changed = template()
        changed["title"] = "something else"
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, intent_packet(contract_template=changed))
        self.assertEqual(caught.exception.code, "policy_template_mismatch")

    def test_two_policies_governing_one_dependent_job_are_ambiguous(self):
        self.enroll()
        # A hand-placed second enrollment for the same dependent job (the file is
        # sealed with the real approval key, so only the registry check can catch it).
        seal_policy(self.state, policy_body(policy_id="policy-b2"), handoff.load_approval_key(self.state))
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, intent_packet())
        self.assertEqual(caught.exception.code, "policy_ambiguous")

    def test_a_policy_with_more_than_one_dependency_is_ambiguous(self):
        seal_policy(self.state, policy_body(dependencies=["job_a", "job_c"],
                                            dependency_features=["feature-a", "feature-c"]),
                    handoff.load_approval_key(self.state))
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.policy_for_intent(self.state, intent_packet())
        self.assertEqual(caught.exception.code, "policy_ambiguous")

    def test_dependency_wide_selection_is_forbidden(self):
        self.enroll()
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.find_policy_for_dependency(self.state, "job_a")
        self.assertEqual(caught.exception.code, "policy_selection_forbidden")


# ---------------------------------------------------------------------------
# (d) Derived authorization: create-only, rooted in the verified policy
# ---------------------------------------------------------------------------


class DerivedAuthorization(Base):
    def setUp(self):
        super().setUp()
        self.approval()
        # A broad-but-bounded policy scope lets these tests distinguish "outside
        # the policy" from "inside the policy but different from the first record".
        self.enroll(scope=["smoke/*"])
        self.authorize_a()
        jobs.create_job(self.state, job_id="job_a", tasks=[{"task_id": "TA", "lease": ["smoke/answer.py"]}])
        mark_accepted(self.state, "job_a", OTHER_SHA)

    def derive(self, **overrides):
        kwargs = dict(
            job_id="job_b", parent_job_id="job_a", statement="feature-b under policy policy-b",
            repo=REPO, candidate_rev=OTHER_SHA, scope=["smoke/double.py"], bees=1, wall_clock_s=1800,
            policy_id="policy-b", dependency_feature="feature-a", dependent_feature="feature-b",
            gates=("merge",),
        )
        kwargs.update(overrides)
        return jobs.record_derived_authorization(self.state, **kwargs)

    def test_a_derived_authorization_records_the_policy_identity(self):
        body = self.derive()
        self.assertEqual(body["policy"]["policy_id"], "policy-b")
        self.assertEqual(body["policy"]["policy_digest"], handoff.policy_digest(handoff.load_policy(self.state, "policy-b")))
        self.assertEqual(body["base_rev"], OTHER_SHA)
        self.assertEqual(body["scope"], ["smoke/double.py"])
        self.assertEqual(body["descendant"]["root_rev"], ROOT)

    def test_it_is_create_only_and_a_replay_must_match_every_dimension(self):
        first = self.derive()
        replay = self.derive()
        self.assertEqual(replay["auth_id"], first["auth_id"])

        for change, code in (
            ({"scope": ["smoke/answer.py"]}, "derived_authorization_exists"),
            ({"bees": 1, "wall_clock_s": 900}, "derived_authorization_exists"),
            ({"gates": ("merge", "release")}, "derived_gate_widened"),
            ({"candidate_rev": "d" * 40}, "derived_revision_mismatch"),
            ({"statement": "a different statement"}, "derived_authorization_exists"),
        ):
            with self.subTest(change=change):
                with self.assertRaises(jobs.JobError) as caught:
                    self.derive(**change)
                self.assertEqual(caught.exception.code, code)

    def test_the_policy_is_the_ceiling_not_a_caller_supplied_scope(self):
        parameters = inspect.signature(jobs.record_derived_authorization).parameters
        self.assertNotIn("policy_scope", parameters)
        self.assertNotIn("root_rev", parameters)
        self.assertNotIn("dependencies", parameters)

        with self.assertRaises(jobs.JobError) as caught:
            self.derive(scope=["src/secret.py"])
        self.assertEqual(caught.exception.code, "derived_scope_widened")

        with self.assertRaises(jobs.JobError) as caught:
            self.derive(bees=2)
        self.assertEqual(caught.exception.code, "derived_budget_exceeded")

        with self.assertRaises(jobs.JobError) as caught:
            self.derive(gates=("release",))
        self.assertEqual(caught.exception.code, "derived_gate_widened")

    def test_a_forged_evidence_record_is_refused(self):
        with self.assertRaises(jobs.JobError) as caught:
            self.derive(dependency_evidence={"job_id": "job_a", "accepted_commit": "e" * 40})
        self.assertEqual(caught.exception.code, "derived_evidence_mismatch")


# ---------------------------------------------------------------------------
# (a)-(d) end to end: materialize a dependent job from a real accepted history
# ---------------------------------------------------------------------------


def git(args, cwd):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


@unittest.skipUnless(HAS_WORKFLOW or REQUIRE_WORKFLOW, "set WORKFLOW_SRC to the pinned workflow checkout")
class Materialize(Base):
    def setUp(self):
        if not HAS_WORKFLOW:
            self.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
        super().setUp()
        self.tmp = Path(self._tmp.name)
        self.approval()

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(["init", "-q", "-b", "main"], self.repo)
        (self.repo / "README.md").write_text("base\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "root"], self.repo)
        self.root = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        (self.repo / "smoke").mkdir()
        (self.repo / "smoke" / "answer.py").write_text("def answer():\n    return 42\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "feature A"], self.repo)
        self.accepted = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        self.mirror = self.tmp / "mirror.git"
        git(["init", "-q", "--bare", str(self.mirror)], self.tmp)

        verify = verify_local.record_path(self.state, "att_1")
        verify.parent.mkdir(parents=True, exist_ok=True)
        verify.write_text(json.dumps({"attempt_id": "att_1", "ok": True, "sha": self.accepted,
                                      "base_rev": self.root, "files": ["smoke/answer.py"],
                                      "suite": {"exit_code": 0}, "gitleaks": {"exit_code": 0},
                                      "mode": "commit-local"}))
        self.authorize_a()
        jobs.create_job(self.state, job_id="job_a", summary="Feature A", workflow_ref="d" * 40,
                        tasks=[{"task_id": "TA", "objective": "answer", "lease": ["smoke/answer.py"]}])
        job_a = jobs.load_job(self.state, "job_a")
        job_a["integration"] = {"wave": "w1", "state": "accepted", "last_commit": self.accepted,
                                "accepted_waves": [{"wave": 1, "commit": self.accepted,
                                                    "attempts": ["att_1"]}]}
        jobs.save_job(self.state, job_a)

        head = subprocess.run(["git", "-C", WORKFLOW_SRC, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        example = json.loads((Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-descendant.json")
                             .read_text(encoding="utf-8"))
        self.packet = copy.deepcopy(example)
        self.packet["workflow_ref"] = head
        self.packet["job_id"] = "job_b"
        self.packet["feature_id"] = "feature-b"
        self.packet["repository"]["identity"] = REPO
        self.packet["source_policy"]["root_revision"] = self.root
        self.packet["source_policy"]["policy_id"] = "policy-b"
        self.packet["source_policy"]["dependencies"] = ["feature-a"]
        self.packet["contract_template"]["source_rev"] = self.root
        self.packet["budget"] = {"bees": 1, "wall_clock_s": 1800}
        self.workflow_ref = head

        self.policy = self.enroll(root_rev=self.root, scope=["smoke/double.py"],
                                  contract_template=copy.deepcopy(self.packet["contract_template"]),
                                  workflow_ref=head)

    def run_materialize(self, packet=None):
        return handoff.materialize(
            self.state, feature_id="feature-b", job_id="job_b",
            packet=copy.deepcopy(packet if packet is not None else self.packet),
            worktree=self.repo, mirror=self.mirror, repo_identity=REPO)

    def test_materialize_binds_the_packet_and_resolves_the_accepted_commit(self):
        before = copy.deepcopy(self.packet)
        result = self.run_materialize()

        self.assertTrue(result["created"])
        self.assertEqual(result["source_rev"], self.accepted)
        self.assertEqual(result["accepted_commit"], self.accepted)

        job_b = jobs.load_job(self.state, "job_b")
        self.assertEqual(job_b["goal"]["base_rev"], self.accepted)
        self.assertEqual(job_b["goal"]["packet_digest"], packets.digest(before))
        self.assertEqual(job_b["goal"]["workflow_ref"], self.workflow_ref)

        stored = packets.read_packet(self.state, "job_b")
        self.assertEqual(stored, packets.canonical_bytes(before),
                         "the persisted intent packet must be byte-identical to the approved intent")
        self.assertEqual(hashlib.sha256(stored).hexdigest(), job_b["goal"]["packet_digest"])

        resolution = json.loads((self.state / "resolutions" / "job_b.json").read_text())
        packets.validate_resolution(resolution, packet=before, accepted_commit=self.accepted)
        self.assertEqual(resolution["packet_digest"], job_b["goal"]["packet_digest"])
        self.assertEqual(resolution["policy_id"], "policy-b")
        self.assertEqual(resolution["policy_digest"], result["policy_digest"])
        self.assertEqual(resolution["contract"]["source_rev"], self.accepted)

        authorization = jobs.load_authorization(self.state, "job_b")
        self.assertEqual(authorization["authority"], "derived")
        self.assertEqual(authorization["policy"]["policy_id"], "policy-b")
        self.assertEqual(authorization["base_rev"], self.accepted)
        self.assertEqual(authorization["dependency_evidence"]["attempts"], ["att_1"])

        task = job_b["tasks"][0]
        self.assertEqual(task["contract"]["source_rev"], self.accepted)
        self.assertEqual(contract_mod.check_frozen(task["contract"]), result["contract_sha256"])

    def test_only_the_resolution_and_contract_carry_the_resolved_revision(self):
        self.run_materialize()
        stored = json.loads(packets.read_packet(self.state, "job_b"))
        self.assertEqual(stored["contract_template"]["source_rev"], self.root)
        self.assertNotIn("contract", stored)
        self.assertEqual(stored["source_policy"]["root_revision"], self.root)
        job_b = jobs.load_job(self.state, "job_b")
        self.assertEqual(job_b["tasks"][0]["contract"]["source_rev"], self.accepted)

    def test_a_replay_is_idempotent_and_never_rewrites_the_resolution(self):
        first = self.run_materialize()
        resolution_before = (self.state / "resolutions" / "job_b.json").read_bytes()
        job_before = jobs.job_path(self.state, "job_b").read_bytes()

        again = self.run_materialize()
        self.assertFalse(again["created"])
        self.assertEqual(again["source_rev"], first["source_rev"])
        self.assertEqual((self.state / "resolutions" / "job_b.json").read_bytes(), resolution_before)
        self.assertEqual(jobs.job_path(self.state, "job_b").read_bytes(), job_before)

    def test_a_different_packet_for_an_existing_job_is_refused(self):
        self.run_materialize()
        changed = copy.deepcopy(self.packet)
        changed["budget"] = {"bees": 1, "wall_clock_s": 900}
        with self.assertRaises(handoff.HandoffError) as caught:
            self.run_materialize(changed)
        self.assertEqual(caught.exception.code, "job_packet_mismatch")

    def test_a_crash_before_job_creation_is_recovered_without_new_authority(self):
        self.run_materialize()
        authorization = jobs.load_authorization(self.state, "job_b")
        resolution = (self.state / "resolutions" / "job_b.json").read_bytes()

        jobs.job_path(self.state, "job_b").unlink()      # crash between the two writes
        again = self.run_materialize()

        self.assertTrue(again["created"])
        self.assertEqual(jobs.load_authorization(self.state, "job_b")["auth_id"], authorization["auth_id"])
        self.assertEqual((self.state / "resolutions" / "job_b.json").read_bytes(), resolution)
        self.assertEqual(jobs.load_job(self.state, "job_b")["goal"]["base_rev"], self.accepted)

    def test_a_rewritten_resolution_is_refused(self):
        self.run_materialize()
        path = self.state / "resolutions" / "job_b.json"
        path.chmod(0o600)
        record = json.loads(path.read_text())
        record["accepted_commit"] = "e" * 40
        path.write_text(json.dumps(record))
        jobs.job_path(self.state, "job_b").unlink()

        with self.assertRaises(handoff.HandoffError) as caught:
            self.run_materialize()
        self.assertEqual(caught.exception.code, "resolution_conflict")

    def test_a_job_created_without_a_packet_is_never_replaced(self):
        jobs.record_authorization(self.state, job_id="job_b", statement="legacy job b authorization",
                                  repo=REPO, base_rev=ROOT, scope=["smoke/double.py"], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        jobs.create_job(self.state, job_id="job_b", tasks=[{"task_id": "T", "lease": ["smoke/double.py"]}])
        with self.assertRaises(handoff.HandoffError) as caught:
            self.run_materialize()
        self.assertEqual(caught.exception.code, "job_packet_mismatch")

    def test_a_damaged_existing_job_raises_instead_of_being_overwritten(self):
        self.run_materialize()
        path = jobs.job_path(self.state, "job_b")
        path.chmod(0o600)
        path.write_text("not json")
        with self.assertRaises(handoff.HandoffError) as caught:
            self.run_materialize()
        self.assertEqual(caught.exception.code, "job_unreadable")
        self.assertEqual(path.read_text(), "not json")


if __name__ == "__main__":
    unittest.main()
