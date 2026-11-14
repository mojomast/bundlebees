"""P2: two dependent features through the real queue, driven only by packets.

Feature A is an **exact** feature packet: a frozen ``acdd/1`` contract, validated
against the pinned workflow protocol (``WORKFLOW_SRC``) and admitted through
``jobs.create_job(packet=...)``, which records the packet digest in the sealed
goal.

Feature B is an **intent** feature packet: an unfrozen ``contract_template``
whose ``source_rev`` is the authorized root, a ``descendant`` source policy that
names exactly one dependency (``feature-a``) and the enrolled policy id, and no
``contract``/``contract_sha256``/``source_resolution_ref``. It is stored at
``<state>/packets/job_b.json`` before execution and is never rewritten: the
resolved revision and the source-bound frozen contract belong to a separate
``rizzstack.source-resolution/1`` record.

Permission is not in either packet. A human enrolls one bounded descendant policy
(``handoff.enroll_policy``, sealed with the state directory's approval key);
production resolves it after A is accepted and materializes job_b.

This test never creates Feature B, never derives, never records derived
authorization and never publishes. It only calls ``queue.run_once`` with the
handoff context (worktree, mirror, repo identity) and then asserts what the
durable records prove.

Run (real git, real hyvv over HTTP, real gitleaks, the pinned workflow checkout):

    HYVV_SRC=/tmp/verify/hyvv-provision \\
    WORKFLOW_SRC=<pristine checkout of the pinned protocol commit> \\
    REQUIRE_WORKFLOW=1 REQUIRE_HYVV=1 \\
    GITLEAKS=/home/clanker01/.local/bin/gitleaks \\
    python -m pytest tests/test_queue_two_features_e2e.py -q
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bundlebees_plugin import contract as contract_mod
from bundlebees_plugin import descendants, handoff as handoff_mod, job as jobs, job_cli, packets, queue
from bundlebees_plugin import provisioning as P

from . import _packet_fixtures as fx

HYVV_SRC = os.environ.get("HYVV_SRC", "")
REQUIRE_HYVV = os.environ.get("REQUIRE_HYVV") == "1"
HAS_HYVV = bool(HYVV_SRC) and Path(HYVV_SRC, "provisioning.py").exists()

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"
REPO = fx.REPO


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


REFUSAL_TYPES = (handoff_mod.HandoffError, packets.PacketError, jobs.JobError,
                 queue.QueueError, descendants.DescendantError)


@unittest.skipUnless((HAS_HYVV or REQUIRE_HYVV) and (HAS_WORKFLOW or REQUIRE_WORKFLOW),
                     "set HYVV_SRC and WORKFLOW_SRC to the pinned checkouts")
class TwoFeaturesPacketNative(unittest.TestCase):
    def setUp(self):
        if not HAS_HYVV:
            self.fail("HYVV_SRC must point at the pinned hyvv checkout when REQUIRE_HYVV=1")
        if not HAS_WORKFLOW:
            self.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
        if not Path(GITLEAKS).exists():
            if REQUIRE_HYVV:
                self.fail("gitleaks is required for the factory run")
            self.skipTest("gitleaks not installed")
        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC))
        self.hyvv = importlib.import_module("provisioning")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.host = socket.gethostname()

        # ---- the protocol this run is pinned to ---------------------------------
        self.workflow_ref = fx.workflow_ref()
        self.assertEqual(len(self.workflow_ref), 40, "the protocol commit must be a full SHA")

        # ---- repository: both approved tests exist and fail at the baseline ----
        self.work = self.tmp / "work"
        self.work.mkdir()
        git(["init", "-q", "-b", "main"], self.work)
        (self.work / "tests").mkdir()
        (self.work / fx.ANSWER_TEST_FILE).write_text(
            "from smoke.answer import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n")
        (self.work / fx.DOUBLE_TEST_FILE).write_text(
            "from smoke.double import double_answer\n\n\ndef test_double_is_84():\n    assert double_answer() == 84\n")
        (self.work / "README.md").write_text("base\n")
        git(["add", "-A"], self.work)
        git(["commit", "-q", "-m", "baseline with both approved failing tests"], self.work)
        self.root = git(["rev-parse", "HEAD"], self.work).stdout.strip()
        self.origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(self.origin)], self.tmp)
        git(["push", "-q", str(self.origin), f"{self.root}:refs/heads/main"], self.work)
        self.mirror = self.tmp / "mirror.git"
        git(["init", "-q", "--bare", str(self.mirror)], self.tmp)
        # The mirror is the enrolled transport from the start: it carries the
        # authorized root and later accepts the job-scoped accepted refs.
        git(["push", "-q", str(self.mirror), f"{self.root}:refs/heads/main"], self.work)

        # ---- real patches, produced in a scratch clone -------------------------
        self.patches = {}
        scratch = self.tmp / "scratch"
        git(["clone", "-q", str(self.work), str(scratch)], self.tmp)
        (scratch / "smoke").mkdir()
        (scratch / "smoke" / "answer.py").write_text("def answer():\n    return 42\n")
        git(["add", "-A"], scratch)
        self.patches["smoke-answer"] = git(["diff", "--cached"], scratch).stdout
        git(["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "A"], scratch)
        (scratch / "smoke" / "double.py").write_text(
            "from smoke.answer import answer\n\n\ndef double_answer():\n    return answer() * 2\n")
        git(["add", "-A"], scratch)
        self.patches["smoke-double"] = git(["diff", "--cached"], scratch).stdout

        # ---- contracts: A frozen now; B only an unfrozen template -------------
        self.contract_a = fx.acdd_contract(
            contract_id="SMOKE-A", criterion_id="AC-001", statement="answer() == 42",
            assigned_file=fx.ANSWER_FILE, test_file=fx.ANSWER_TEST_FILE, test_id=fx.ANSWER_TEST,
            source_rev=self.root)
        self.template_b = fx.acdd_contract(
            contract_id="SMOKE-B", criterion_id="AC-002", statement="double_answer() == 84",
            assigned_file=fx.DOUBLE_FILE, test_file=fx.DOUBLE_TEST_FILE, test_id=fx.DOUBLE_TEST,
            source_rev=self.root)
        self.assertNotIn("review", self.template_b, "an intent template must never be frozen")

        # ---- state: A authorized now; B only through the enrolled policy ------
        self.state = self.tmp / "state"
        self.state.mkdir()
        jobs.record_authorization(self.state, job_id=fx.JOB_A, statement="Feature A: answer() == 42",
                                  repo=REPO, base_rev=self.root, scope=[fx.ANSWER_FILE], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")

        frozen_a = contract_mod.freeze_contract(dict(self.contract_a), approved_by="reviewer",
                                                approved_at=fx.APPROVED_AT)
        self.contract_a_sha = contract_mod.check_frozen(frozen_a)
        self.packet_a = fx.exact_packet(contract=frozen_a, contract_sha256=self.contract_a_sha,
                                        workflow_ref=self.workflow_ref)
        # The exact packet is validated by the pinned protocol before admission.
        self.digest_a = packets.validate(self.packet_a)
        self.assertEqual(packets.digest(self.packet_a), self.digest_a)
        jobs.create_job(self.state, job_id=fx.JOB_A, summary="Feature A", workflow_ref=self.workflow_ref,
                        packet=self.packet_a, tasks=[{
                            "task_id": "TA", "objective": "Create smoke/answer.py returning 42",
                            "lease": [fx.ANSWER_FILE],
                            "acceptance": {"criteria": ["answer() == 42"], "tests": [fx.ANSWER_TEST],
                                           "commands": ["python -m pytest tests/test_smoke.py -q"]},
                            "contract": frozen_a, "contract_sha256": self.contract_a_sha,
                            "provision": {"repo": REPO, "source_rev": self.root, "host": self.host,
                                          "profile": "smoke", "project": "smoke-answer",
                                          "contract_sha256": self.contract_a_sha},
                        }])

        # One human enrollment: bounded, one dependency, one dependent job, the
        # approved template. The approval key is the approver's own setup step.
        handoff_mod.create_approval_key(self.state)
        handoff_mod.enroll_policy(
            self.state, policy_id=fx.POLICY_B,
            statement=f"Feature B ({fx.JOB_B}) may build on Feature A's accepted commit",
            repo=REPO, root_rev=self.root,
            dependency_job=fx.JOB_A, dependency_feature=fx.FEATURE_A,
            dependent_feature=fx.FEATURE_B, dependent_job=fx.JOB_B,
            scope=[fx.DOUBLE_FILE], bees=1, wall_clock_s=1800,
            contract_template=self.template_b, gates=("merge",),
            host=self.host, profile="smoke", project="smoke-double", workflow_ref=self.workflow_ref,
        )

        # ---- the immutable intent packet, in the state packet store -----------
        self.packet_b = fx.intent_packet(template=self.template_b, workflow_ref=self.workflow_ref)
        for forbidden in ("contract", "contract_sha256", "source_resolution_ref"):
            self.assertNotIn(forbidden, self.packet_b, f"an intent packet must not carry {forbidden}")
        self.digest_b = packets.validate(self.packet_b)
        self.intent_path = fx.write_packet(self.state, self.packet_b)
        self.intent_file_sha = fx.file_sha256(self.intent_path)
        self.assertEqual(self.intent_path, self.state / "packets" / f"{fx.JOB_B}.json")

        self.queue_file = self.state / "queue.json"
        self.queue_file.write_text(json.dumps({
            fx.FEATURE_A: {"repo": REPO, "job_id": fx.JOB_A, "status": "queued"},
            fx.FEATURE_B: {"repo": REPO, "job_id": fx.JOB_B, "status": "queued",
                           "depends_on": [fx.FEATURE_A]},
        }))

        # ---- real provisioning host with a governed mirror --------------------
        fixtures_dir = self.tmp / "fixtures"
        fixtures_dir.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"schema": "rizzstack.provisioning/1", "profiles": {"smoke": {
            "repo_patterns": [REPO], "workspace_root": str(fixtures_dir),
            "mirrors": {REPO: f"file://{self.mirror}"}}}}))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"projects": {}}))
        self.real = self.hyvv.Provisioner(policy=self.hyvv.load_policy(policy),
                                          journal=self.hyvv.Journal(self.tmp / "journal"),
                                          registry=self.hyvv.RegistryStore(self.config, cache={}),
                                          credential=lambda repo: "host-helper")
        self.host_posts = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _send(self, status, body):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/v1/provision/capabilities":
                    return self._send(200, fixture.real.capabilities())
                if self.path.startswith("/v1/provision/receipts/"):
                    try:
                        return self._send(200, fixture.real.lookup_receipt(self.path.rsplit("/", 1)[-1]))
                    except fixture.hyvv.ProvisionError as exc:
                        return self._send(404 if exc.code == "receipt_unknown" else 409, exc.to_dict())
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    fixture.host_posts += 1
                    return self._send(200, fixture.real.provision(body))
                except fixture.hyvv.ProvisionError as exc:
                    return self._send(409, exc.to_dict())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

        self.launcher = Worker(self.patches)
        self.provisioner = P.HyvvProvisioner(self._transport, expect_host=self.host)
        self.queue = queue.JsonQueue(self.queue_file)

    # ------------------------------------------------------------------ plumbing
    def _transport(self, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def tick(self, job_id):
        suite = ([fx.ANSWER_TEST_FILE, "-q"] if job_id == fx.JOB_A else [fx.DOUBLE_TEST_FILE, "-q"])
        return job_cli.tick(self.state, job_id, client=self.launcher, repo=self.work,
                            provisioner=self.provisioner, expect_host=self.host, gitleaks=GITLEAKS,
                            suite_command=[sys.executable, "-m", "pytest", *suite])

    def handoff_context(self):
        return {"worktree": str(self.work), "mirror": str(self.mirror), "repo_identity": REPO}

    def run_once(self):
        return queue.run_once(self.state, self.queue, tick=self.tick,
                              handoff_context=self.handoff_context())

    def drive(self, until=fx.FEATURE_B, limit=20):
        """Consume the queue until *until* is ready for review; return every step."""

        log = []
        for _ in range(limit):
            try:
                result = self.run_once()
            except REFUSAL_TYPES as exc:
                self.fail(f"run_once refused the handoff instead of materializing the dependent "
                          f"feature from its intent packet: {getattr(exc, 'code', '')}: {exc}; "
                          f"steps so far: {json.dumps(log, indent=1, default=str)}")
            log.append(result)
            if result.get("action") in ("blocked", "failed", "needs_approval"):
                self.fail("expected production to materialize the dependent feature from its intent "
                          "packet and enrolled policy; instead run_once reported "
                          f"{json.dumps(result, default=str)} after {json.dumps(log, indent=1, default=str)}")
            if result.get("action") == "ready_for_review" and result.get("feature") == until:
                break
        else:
            self.fail(f"{until} never reached ready_for_review: {json.dumps(log, indent=1, default=str)}")
        return log

    def refusal(self, limit=5):
        """Drive the queue expecting a refusal; a missing interface is not a refusal."""

        for _ in range(limit):
            try:
                result = self.run_once()
            except REFUSAL_TYPES as exc:
                code = str(getattr(exc, "code", ""))
                if code == "policy_selection_forbidden":
                    self.fail("queue.run_once still selects a policy by dependency; it must read the "
                              "intent packet's policy_id and materialize the dependent job from the store")
                return exc
            if result.get("action") in ("blocked", "failed", "needs_approval"):
                return result
            if result.get("action") == "ready_for_review" and result.get("feature") == fx.FEATURE_B:
                self.fail("the dependent feature was admitted when production should have refused")
        return None

    def assert_refused_for_the_right_reason(self, refusal):
        """A refusal that only says job_b is absent proves nothing about the packet gate."""

        if isinstance(refusal, dict):
            detail = str(refusal.get("detail") or refusal.get("status") or "")
            self.assertNotIn("does not exist", detail,
                             "the queue blocked because job_b was never materialized, so the mutation "
                             "was not what was refused: production must attempt the packet-driven "
                             f"handoff and refuse it (got {json.dumps(refusal, default=str)})")
            self.assertNotIn("no authorized job", detail,
                             "the queue blocked on a missing job instead of refusing the packet")

    def integration_commit(self, job_id):
        job = jobs.load_job(self.state, job_id)
        commit = str((job.get("integration") or {}).get("last_commit") or "")
        self.assertEqual(len(commit), 40, f"{job_id} has no integrated commit: {job.get('integration')}")
        return commit

    def provisioned(self, project):
        """The one ready provisioning record for *project* on the host."""

        records = [record for record in self.real.journal.all()
                   if record.get("project") == project and record.get("state") == "ready"
                   and isinstance(record.get("receipt"), dict)]
        self.assertEqual(len(records), 1, f"expected one ready provisioning record for {project}")
        return records[0]["receipt"]

    # --------------------------------------------------------------- the proof
    def test_packets_carry_features_a_and_b_to_one_verified_history(self):
        log = self.drive()
        self.assertEqual(log[-1].get("feature"), fx.FEATURE_B, json.dumps(log, indent=1, default=str))
        self.assertEqual(self.queue.get(fx.FEATURE_A).status, "ready_for_review")
        self.assertEqual(self.queue.get(fx.FEATURE_B).status, "ready_for_review")

        # P4: RETURN packets are built from durable evidence, deterministically.
        from bundlebees_plugin import job_cli as _jc

        manifests = {}
        for job_id in (fx.JOB_A, fx.JOB_B):
            first = _jc.report(self.state, job_id, repo=self.work)
            manifest = json.loads((self.state / "jobs" / job_id / "return.json").read_text())
            manifests[job_id] = manifest
            self.assertEqual(manifest["schema"], "rizzstack.return/1")
            self.assertEqual(manifest["receiver_decision"]["status"], "pending")
            self.assertIsNone(manifest["receiver_decision"]["decided_by"])
            self.assertTrue((self.state / "jobs" / job_id / "RETURN.md").read_text().strip())
            statuses = {a["id"]: a["status"] for a in manifest["acceptance"]}
            self.assertTrue(statuses and all(v == "PASS" for v in statuses.values()), statuses)
            again = _jc.report(self.state, job_id, repo=self.work)
            self.assertEqual(again["return"]["evidence_digest"], first["return"]["evidence_digest"],
                             "regenerating a RETURN from the same evidence changed its identity")

        accepted_a = jobs.load_job(self.state, fx.JOB_A)["integration"]["last_commit"]
        self.assertEqual(manifests[fx.JOB_B]["descendant"]["accepted_commit"], accepted_a)
        self.assertEqual(manifests[fx.JOB_B]["repository"]["source_rev"], accepted_a)
        self.assertEqual(manifests[fx.JOB_B]["descendant"]["dependency_feature"], "feature-a")
        self.assertEqual(manifests[fx.JOB_A]["contract"]["id"], "SMOKE-A")
        self.assertEqual(manifests[fx.JOB_B]["contract"]["id"], "SMOKE-B")
        self.assert_the_proof()

    def assert_the_proof(self):
        """Everything the durable records must prove after both features ran."""

        job_a = jobs.load_job(self.state, fx.JOB_A)
        job_b = jobs.load_job(self.state, fx.JOB_B)
        commit_a = self.integration_commit(fx.JOB_A)
        commit_b = self.integration_commit(fx.JOB_B)

        # 1. Both sealed goals carry the packet identity they were admitted under.
        self.assertEqual((self.state / "packets" / f"{fx.JOB_A}.json").read_bytes(),
                         fx.canonical_bytes(self.packet_a),
                         "Feature A's exact packet must be stored beside its job")
        self.assertEqual((self.state / "packets" / f"{fx.JOB_B}.json").read_bytes(),
                         fx.canonical_bytes(self.packet_b))
        self.assertEqual(job_a["goal"].get("packet_digest"), self.digest_a,
                         "Feature A's goal must seal the exact packet digest")
        self.assertEqual(job_b["goal"].get("packet_digest"), self.digest_b,
                         "Feature B's goal must seal the intent packet digest")
        self.assertEqual(job_a["goal"].get("workflow_ref"), self.workflow_ref)
        self.assertEqual(job_b["goal"].get("workflow_ref"), self.workflow_ref)
        self.assertEqual(job_b["goal"].get("base_rev"), commit_a,
                         "Feature B must be bound to A's accepted commit, not the authorized root")

        # 2. A's contract is frozen; B's intent digest is unchanged by resolution.
        task_a = job_a["tasks"][0]
        self.assertEqual((task_a.get("contract") or {}).get("review", {}).get("status"), "approved")
        self.assertEqual(contract_mod.check_frozen(task_a["contract"]), task_a.get("contract_sha256"))
        self.assertEqual(task_a.get("contract_sha256"), self.packet_a["contract_sha256"])

        self.assertEqual(fx.file_sha256(self.intent_path), self.intent_file_sha,
                         "the intent packet was rewritten during resolution")
        resolved_intent = fx.read_packet(self.state, fx.JOB_B)
        self.assertEqual(fx.digest(resolved_intent), self.digest_b,
                         "the intent packet digest changed after resolution")
        for forbidden in ("contract", "contract_sha256", "source_resolution_ref"):
            self.assertNotIn(forbidden, resolved_intent)
        self.assertEqual(resolved_intent["source_policy"]["kind"], "descendant")
        self.assertEqual(resolved_intent["source_policy"]["root_revision"], self.root)
        self.assertEqual(resolved_intent["source_policy"]["dependencies"], [fx.FEATURE_A])
        self.assertEqual(resolved_intent["source_policy"]["policy_id"], fx.POLICY_B)
        self.assertEqual(resolved_intent["contract_template"]["source_rev"], self.root,
                         "an intent packet may never be rewritten to the resolved revision")

        # 3. The resolution record binds the intent to A's accepted commit.
        found = fx.resolution_for(self.state, self.digest_b)
        self.assertIsNotNone(found, "no rizzstack.source-resolution/1 record binds job_b's intent digest")
        resolution_path, resolution = found
        self.assertEqual(resolution_path, self.state / "resolutions" / f"{fx.JOB_B}.json")
        self.assertEqual(resolution.get("accepted_commit"), commit_a, str(resolution_path))
        self.assertEqual(resolution.get("derived_source_rev"), commit_a, str(resolution_path))
        self.assertEqual(resolution.get("packet_digest"), self.digest_b)
        self.assertEqual(resolution.get("policy_id"), fx.POLICY_B)
        self.assertEqual(resolution.get("policy_digest"),
                         handoff_mod.policy_digest(handoff_mod.load_policy(self.state, fx.POLICY_B)))
        self.assertEqual(resolution.get("dependency_job"), fx.JOB_A)
        self.assertEqual(resolution.get("dependency_feature"), fx.FEATURE_A)

        task_b = job_b["tasks"][0]
        self.assertEqual(resolution.get("contract_sha256"), task_b.get("contract_sha256"),
                         "the resolution's frozen contract hash must match job_b's task contract")
        self.assertEqual(contract_mod.check_frozen(resolution["contract"]), task_b.get("contract_sha256"))
        self.assertEqual(resolution["contract"], task_b.get("contract"),
                         "job_b must run exactly the source-bound frozen contract")
        self.assertEqual(resolution["contract"].get("source_rev"), commit_a)
        self.assertEqual(resolution["contract"].get("scope"), self.template_b["scope"])
        self.assertEqual(resolution["contract"].get("acceptance_criteria"),
                         self.template_b["acceptance_criteria"])

        # The resolution record conforms to the pinned protocol, too.
        packets.validate_resolution(resolution, packet=resolved_intent, accepted_commit=commit_a)

        # 4. Feature B was provisioned through the governed mirror, at A's commit.
        receipt = self.provisioned("smoke-double")
        self.assertEqual(receipt.get("transport"), "mirror",
                         f"job_b was not provisioned through the mirror: {receipt}")
        self.assertEqual(receipt.get("repo"), REPO)
        self.assertEqual(receipt.get("resolved_revision"), commit_a)
        workspace = Path(str(receipt.get("workspace")))
        self.assertEqual(git(["rev-parse", "HEAD"], workspace).stdout.strip(), commit_a)
        self.assertEqual(git(["config", "--get", "remote.origin.url"], workspace).stdout.strip(),
                         f"file://{self.mirror}")
        # The transport really was the mirror: A's accepted commit is public there
        # and absent from the origin, so only the mirror could have served it.
        published = git(["ls-remote", str(self.mirror), f"refs/bb/accepted/{fx.JOB_A}/{commit_a}"],
                        self.tmp).stdout.split()
        self.assertEqual(published[:1], [commit_a])
        self.assertNotEqual(git(["cat-file", "-e", f"{commit_a}^{{commit}}"], self.origin,
                                check=False).returncode, 0,
                            "the origin has A's commit: the mirror claim is not proven")
        self.assertEqual(self.provisioned("smoke-answer").get("transport"), "mirror")

        # 5. One verified history containing both features; a fresh clone passes.
        self.assertNotEqual(commit_a, commit_b)
        self.assertEqual(git(["merge-base", "--is-ancestor", commit_a, commit_b], self.work,
                             check=False).returncode, 0,
                         "Feature B's history must contain Feature A's accepted commit")
        final = self.tmp / "final"
        git(["clone", "-q", str(self.work), str(final)], self.tmp)
        git(["checkout", "-q", commit_b], final)
        self.assertIn("return 42", (final / fx.ANSWER_FILE).read_text())
        self.assertIn("answer() * 2", (final / fx.DOUBLE_FILE).read_text())
        done = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=str(final),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        # 6. Exactly one attempt per feature, and one provisioning POST each.
        self.assertEqual(self.host_posts, 2, "each feature must be provisioned exactly once")
        self.assertEqual(len(self.launcher.posts), 2, "each feature must be attempted exactly once")
        self.assertEqual(len(task_a.get("attempts") or []), 1)
        self.assertEqual(len(task_b.get("attempts") or []), 1)
        self.assertEqual(len(job_a["integration"].get("accepted_waves") or []), 1)
        self.assertEqual(len(job_b["integration"].get("accepted_waves") or []), 1)

        # 7. Wave records are job-scoped and distinct.
        wave_a = json.loads((self.state / "waves" / f"{fx.JOB_A}.w1.json").read_text())
        wave_b = json.loads((self.state / "waves" / f"{fx.JOB_B}.w1.json").read_text())
        self.assertEqual(wave_a.get("wave_id"), f"{fx.JOB_A}.w1")
        self.assertEqual(wave_b.get("wave_id"), f"{fx.JOB_B}.w1")
        self.assertTrue(wave_a.get("accepted") and wave_b.get("accepted"))
        self.assertEqual(wave_a.get("head_sha"), commit_a)
        self.assertEqual(wave_b.get("head_sha"), commit_b)
        self.assertNotEqual(wave_a.get("head_sha"), wave_b.get("head_sha"))

    # ------------------------------------------------------------ negative tests
    def test_a_mutated_exact_contract_is_refused_before_the_job_exists(self):
        state = self.tmp / "mutated-a"
        state.mkdir()
        jobs.record_authorization(state, job_id="job_a2", statement="Feature A again", repo=REPO,
                                  base_rev=self.root, scope=[fx.ANSWER_FILE], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        packet = copy.deepcopy(self.packet_a)
        packet["contract"]["outcome"]["summary"] = "mutated after approval"
        with self.assertRaises(packets.PacketError) as caught:
            packets.validate(packet)
        self.assertIn(caught.exception.code, {"contract_hash_mismatch", "contract_changed"})
        with self.assertRaises(packets.PacketError) as caught:
            jobs.create_job(state, job_id="job_a2", packet=packet, tasks=[{
                "task_id": "TA", "objective": "x", "lease": [fx.ANSWER_FILE],
                "acceptance": {"criteria": ["c"], "tests": [fx.ANSWER_TEST], "commands": ["pytest"]}}])
        self.assertIn(caught.exception.code, {"contract_hash_mismatch", "contract_changed"})
        self.assertFalse(jobs.job_path(state, "job_a2").exists(),
                         "a mutated exact contract must never create a job")

    def test_a_mutated_intent_packet_after_enrollment_is_refused(self):
        self.drive(until=fx.FEATURE_A)
        self.assertEqual(self.queue.get(fx.FEATURE_A).status, "ready_for_review")

        mutated = json.loads(self.intent_path.read_text(encoding="utf-8"))
        mutated["scope"]["lease"] = [fx.DOUBLE_FILE, fx.ANSWER_FILE]
        mutated["contract_template"]["scope"]["assigned_files"] = [fx.DOUBLE_FILE, fx.ANSWER_FILE]
        self.assertNotEqual(fx.digest(mutated), self.digest_b)
        # The mutated packet is still internally valid: the refusal must come from
        # the enrolled policy, not from a schema accident.
        self.assertEqual(packets.validate(mutated), fx.digest(mutated))
        os.chmod(self.intent_path, 0o600)
        self.intent_path.write_bytes(fx.canonical_bytes(mutated))
        self.addCleanup(os.chmod, self.intent_path, 0o400)

        refusal = self.refusal()
        self.assertIsNotNone(refusal, "a mutated intent packet was accepted silently")
        self.assert_refused_for_the_right_reason(refusal)
        self.assertFalse(jobs.job_path(self.state, fx.JOB_B).exists(),
                         "production materialized job_b from a mutated intent packet")
        self.assertNotEqual(self.queue.get(fx.FEATURE_B).status, "ready_for_review")
        self.assertIsNone(fx.resolution_for(self.state, self.digest_b),
                          "a resolution record was written for a mutated intent")

    def test_a_second_policy_for_the_same_dependency_cannot_govern_job_b(self):
        """One dependent job, one policy; a same-dependency policy elsewhere is inert."""

        competing = f"{fx.POLICY_B}2"
        with self.assertRaises(handoff_mod.HandoffError) as caught:
            handoff_mod.enroll_policy(
                self.state, policy_id=competing,
                statement="a competing authority for job_b",
                repo=REPO, root_rev=self.root,
                dependency_job=fx.JOB_A, dependency_feature=fx.FEATURE_A,
                dependent_feature=fx.FEATURE_B, dependent_job=fx.JOB_B,
                scope=[fx.DOUBLE_FILE], bees=1, wall_clock_s=1800,
                contract_template=self.template_b, gates=("merge",),
                host=self.host, profile="smoke", project="smoke-double-rival",
                workflow_ref=self.workflow_ref)
        self.assertEqual(getattr(caught.exception, "code", ""), "policy_conflict")
        self.assertFalse(handoff_mod.policy_path(self.state, competing).exists())

        # A second policy on the same dependency, for another dependent job, can
        # never be selected by this intent packet: the packet names policy-b.
        other = f"{fx.POLICY_B}3"
        handoff_mod.enroll_policy(
            self.state, policy_id=other,
            statement="a separate dependent feature on the same dependency",
            repo=REPO, root_rev=self.root,
            dependency_job=fx.JOB_A, dependency_feature=fx.FEATURE_A,
            dependent_feature="feature-other", dependent_job="job_other",
            scope=[fx.DOUBLE_FILE], bees=1, wall_clock_s=1800,
            contract_template=self.template_b, gates=("merge",),
            host=self.host, profile="smoke", project="smoke-other",
            workflow_ref=self.workflow_ref)

        self.drive()
        resolution_path, resolution = fx.resolution_for(self.state, self.digest_b)
        self.assertEqual(resolution.get("policy_id"), fx.POLICY_B, str(resolution_path))
        self.assertEqual(resolution["contract"].get("id"), "SMOKE-B")
        self.assertFalse(jobs.job_path(self.state, "job_other").exists(),
                         "the never-named competing policy was materialized")
        self.assertFalse(handoff_mod.policy_path(self.state, competing).exists())

    def test_a_forged_queue_card_does_not_admit_the_dependent(self):
        forged = {
            fx.FEATURE_A: {"repo": REPO, "job_id": fx.JOB_A, "status": "ready_for_review",
                           "evidence": {"job_id": fx.JOB_A, "wave": "w1", "head_sha": "f" * 40,
                                        "verified": True}},
            fx.FEATURE_B: {"repo": REPO, "job_id": fx.JOB_B, "status": "queued",
                           "depends_on": [fx.FEATURE_A]},
        }
        self.queue_file.write_text(json.dumps(forged))
        # job_a exists but has no attempts and no accepted wave: the card lies.
        self.assertFalse((self.state / "waves" / f"{fx.JOB_A}.w1.json").exists())

        for _ in range(3):
            result = self.run_once()
            self.assertNotEqual(result.get("feature"), fx.FEATURE_B, str(result))
            self.assertNotEqual(result.get("action"), "ready_for_review", str(result))
        self.assertFalse(jobs.job_path(self.state, fx.JOB_B).exists(),
                         "a forged queue card admitted the dependent feature")
        self.assertNotEqual(self.queue.get(fx.FEATURE_B).status, "ready_for_review")
        self.assertIsNone(fx.resolution_for(self.state, self.digest_b))


class Worker:
    """A scripted coding worker: one attempt per project, real diffs."""

    def __init__(self, patches):
        self.patches = patches
        self.posts = []
        self.by_attempt = {}
        self.counter = 0

    def create(self, brief):
        self.counter += 1
        attempt_id = f"att_{self.counter}"
        self.posts.append(dict(brief))
        self.by_attempt[attempt_id] = (brief.get("project"), brief.get("source_rev"))
        return {"attempt_id": attempt_id, "state": "succeeded", "brief": dict(brief)}

    def attempts_raw(self):
        return [{"attempt_id": a, "state": "succeeded", "brief": {"project": p}}
                for a, (p, _rev) in self.by_attempt.items()]

    def get_raw(self, attempt_id):
        project, rev = self.by_attempt[attempt_id]
        return {"attempt_id": attempt_id, "state": "succeeded",
                "brief": {"project": project, "source_rev": rev}}

    def artifact(self, attempt_id):
        project, rev = self.by_attempt[attempt_id]
        patch = self.patches[project]
        return {"patch": patch, "source_rev": rev,
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()}


if __name__ == "__main__":
    unittest.main()
