"""Gate 3: two dependent features through the real queue, mirror, verifier, integrator."""

from __future__ import annotations

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

from bundlebees_plugin import descendants, job as jobs, job_cli, queue, transport
from bundlebees_plugin import provisioning as P

HYVV_SRC = os.environ.get("HYVV_SRC", "")
REQUIRE_HYVV = os.environ.get("REQUIRE_HYVV") == "1"
HAS_HYVV = bool(HYVV_SRC) and Path(HYVV_SRC, "provisioning.py").exists()
import shutil as _shutil
GITLEAKS = os.environ.get("GITLEAKS") or _shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "817eac479f441396562962e880c1af596638f399")
REPO = "mojomast/smoke-real"


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


@unittest.skipUnless(HAS_HYVV or REQUIRE_HYVV, "set HYVV_SRC to a hyvv checkout")
class TwoFeatures(unittest.TestCase):
    def setUp(self):
        if not HAS_HYVV:
            self.fail("HYVV_SRC must point at the pinned hyvv checkout when REQUIRE_HYVV=1")
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

        # ---- repository: both approved tests exist and fail at the baseline ----
        self.work = self.tmp / "work"
        self.work.mkdir()
        git(["init", "-q", "-b", "main"], self.work)
        (self.work / "tests").mkdir()
        (self.work / "tests" / "test_smoke.py").write_text(
            "from smoke.answer import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n")
        (self.work / "tests" / "test_double.py").write_text(
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
        # The mirror is the enrolled transport for this repository from the start:
        # it carries the authorized root, and later accepts the accepted refs.
        git(["push", "-q", str(self.mirror), f"{self.root}:refs/heads/main"], self.work)

        # ---- real patches, produced in a scratch clone ----
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

        self.contracts = {}
        for name, scope, test_file, criteria, test in (
            ("A", "smoke/answer.py", "tests/test_smoke.py", "answer() == 42", "tests/test_smoke.py::test_answer_is_42"),
            ("B", "smoke/double.py", "tests/test_double.py", "double_answer() == 84", "tests/test_double.py::test_double_is_84"),
        ):
            idx = 1 if name == "A" else 2
            self.contracts[name] = {
                "contract_version": "acdd/1", "id": f"SMOKE-{name}", "title": criteria,
                "outcome": {"summary": criteria, "observable_behavior": [criteria]},
                "source_rev": self.root,
                "scope": {"assigned_files": [scope], "test_files": [test_file]},
                "acceptance_criteria": [{"id": f"AC-00{idx}", "statement": criteria, "tests": [test], "verification": "test"}],
                "tdd": {"applicable": True, "test_first": False,
                        "reason": "the approved test exists and fails at the baseline"},
                "verification": {"independent": True}, "evidence_required": ["green"],
                "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
            }

        # ---- state: A authorized now; B only after A is accepted ----
        self.state = self.tmp / "state"
        self.state.mkdir()
        jobs.record_authorization(self.state, job_id="job_a", statement="Feature A: answer() == 42",
                                  repo=REPO, base_rev=self.root, scope=["smoke/answer.py"], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        from bundlebees_plugin import contract as contract_mod
        contract_a = contract_mod.freeze_contract(dict(self.contracts["A"]), approved_by="reviewer",
                                                  approved_at="2026-11-13T00:00:00Z")
        self.sha_a = contract_mod.check_frozen(contract_a)
        jobs.create_job(self.state, job_id="job_a", summary="Feature A", workflow_ref=WORKFLOW_REF, tasks=[{
            "task_id": "TA", "objective": "Create smoke/answer.py returning 42", "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["answer() == 42"], "tests": ["tests/test_smoke.py::test_answer_is_42"],
                           "commands": ["python -m pytest tests/test_smoke.py -q"]},
            "contract": contract_a, "contract_sha256": self.sha_a,
            "provision": {"repo": REPO, "source_rev": self.root, "host": self.host, "profile": "smoke",
                          "project": "smoke-answer", "contract_sha256": self.sha_a},
        }])

        self.queue_file = self.state / "queue.json"
        self.queue_file.write_text(json.dumps({
            "feature-a": {"repo": REPO, "job_id": "job_a", "status": "queued"},
            "feature-b": {"repo": REPO, "job_id": "job_b", "status": "queued", "depends_on": ["feature-a"]},
        }))

        # ---- real provisioning host with a governed mirror ----
        fixtures = self.tmp / "fixtures"
        fixtures.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"schema": "rizzstack.provisioning/1", "profiles": {"smoke": {
            "repo_patterns": [REPO], "workspace_root": str(fixtures),
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
        self._b_created = False

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
        suite = (["tests/test_smoke.py", "-q"] if job_id == "job_a" else ["tests/test_double.py", "-q"])
        return job_cli.tick(self.state, job_id, client=self.launcher, repo=self.work,
                            provisioner=self.provisioner, expect_host=self.host, gitleaks=GITLEAKS,
                            suite_command=[sys.executable, "-m", "pytest", *suite])

    def _create_feature_b(self):
        """Derive Feature B's authority from A's accepted commit, once."""
        if self._b_created:
            return
        accepted, _ = transport.accepted_commit(self.state, jobs.load_job(self.state, "job_a"))
        receipt = transport.publish(self.state, "job_a", repo=self.work, mirror=self.mirror,
                                    repo_identity=REPO, root_rev=self.root)
        self.assertEqual(receipt["source_sha"], accepted)

        policy = descendants.DescendantPolicy.parse({
            "repo": REPO, "base_rev": self.root, "scope": ["smoke/double.py"],
            "budget": {"bees": 1, "wall_clock_s": 1800},
            "descendant": {"policy": descendants.POLICY, "dependencies": ["job_a"]},
        })
        derived = descendants.derive(policy, candidate_rev=accepted, repo=REPO, worktree=self.work,
                                     assigned_files=["smoke/double.py"], state_dir=self.state,
                                     bees=1, wall_clock_s=1800)
        jobs.record_derived_authorization(self.state, job_id="job_b", parent_job_id="job_a",
                                          statement="Feature B on A's accepted commit", repo=REPO,
                                          candidate_rev=derived["base_rev"], scope=["smoke/double.py"],
                                          bees=1, wall_clock_s=1800, dependencies=["job_a"], root_rev=self.root,
                                          policy_scope=["smoke/double.py"])
        from bundlebees_plugin import contract as contract_mod
        body = dict(self.contracts["B"], source_rev=accepted)
        contract_b = contract_mod.freeze_contract(body, approved_by="reviewer", approved_at="2026-11-13T00:00:00Z")
        self.sha_b = contract_mod.check_frozen(contract_b)
        jobs.create_job(self.state, job_id="job_b", summary="Feature B", workflow_ref=WORKFLOW_REF, tasks=[{
            "task_id": "TB", "objective": "Create smoke/double.py returning 84", "lease": ["smoke/double.py"],
            "acceptance": {"criteria": ["double_answer() == 84"],
                           "tests": ["tests/test_double.py::test_double_is_84"],
                           "commands": ["python -m pytest tests/test_double.py -q"]},
            "contract": contract_b, "contract_sha256": self.sha_b,
            "provision": {"repo": REPO, "source_rev": accepted, "host": self.host, "profile": "smoke",
                          "project": "smoke-double", "contract_sha256": self.sha_b},
        }])
        self._b_created = True
        self.accepted_a = accepted

    def test_two_dependent_features_reach_one_verified_history(self):
        log = []
        for _ in range(12):
            result = queue.run_once(self.state, self.queue, tick=self.tick)
            log.append(result)
            if result["action"] == "ready_for_review" and result["feature"] == "feature-a":
                self._create_feature_b()
            if result["action"] == "ready_for_review" and result["feature"] == "feature-b":
                break
            if result["action"] == "blocked":
                self.fail(json.dumps(log, indent=1, default=str))

        self.assertEqual(log[-1].get("feature"), "feature-b", json.dumps(log, indent=1, default=str))
        self.assertEqual(self.queue.get("feature-a").status, "ready_for_review")
        self.assertEqual(self.queue.get("feature-b").status, "ready_for_review")

        # Exactly one attempt per feature, and each patch was applied once.
        self.assertEqual(len(self.launcher.posts), 2)
        self.assertEqual(self.host_posts, 2)

        job_a = jobs.load_job(self.state, "job_a")
        job_b = jobs.load_job(self.state, "job_b")
        self.assertEqual(job_b["goal"]["base_rev"], self.accepted_a)
        self.assertEqual(job_a["goal"]["workflow_ref"], WORKFLOW_REF)

        head_b = job_b["integration"]["last_commit"]
        head_a = job_a["integration"]["last_commit"]
        self.assertNotEqual(head_a, head_b)
        self.assertEqual(git(["merge-base", "--is-ancestor", head_a, head_b], self.work, check=False).returncode, 0,
                         "Feature B's history must contain Feature A's accepted commit")

        # A fresh clone at the final SHA passes both approved tests together.
        final = self.tmp / "final"
        git(["clone", "-q", str(self.work), str(final)], self.tmp)
        git(["checkout", "-q", head_b], final)
        self.assertIn("return 42", (final / "smoke" / "answer.py").read_text())
        self.assertIn("answer() * 2", (final / "smoke" / "double.py").read_text())
        done = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=str(final),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        # Wave records are job-scoped and distinct.
        waves = sorted(p.name for p in (self.state / "waves").glob("*.json"))
        self.assertIn("job_a.w1.json", waves)
        self.assertIn("job_b.w1.json", waves)


class Worker:
    """A scripted coding worker: one attempt per project, real patches."""

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
        return {"attempt_id": attempt_id, "state": "succeeded", "brief": {"project": project, "source_rev": rev}}

    def artifact(self, attempt_id):
        project, rev = self.by_attempt[attempt_id]
        patch = self.patches[project]
        return {"patch": patch, "source_rev": rev,
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()}


if __name__ == "__main__":
    unittest.main()
