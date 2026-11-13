"""Gate C: queued, preauthorized feature -> provisioned -> verified -> integrated.

Real temporary Git repositories, the real hyvv provisioning module behind HTTP,
the real queue consumer, the real verifier and integrator. Only the worker is
simulated, by returning a genuine patch with correct hashes.

Run with HYVV_SRC=<hyvv checkout> (CI sets it and REQUIRE_HYVV=1).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
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
from types import SimpleNamespace

from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli, leases, queue, provisioning as P

HYVV_SRC = os.environ.get("HYVV_SRC", "")
REQUIRE_HYVV = os.environ.get("REQUIRE_HYVV") == "1"
HAS_HYVV = bool(HYVV_SRC) and Path(HYVV_SRC, "provisioning.py").exists()
import shutil
WORKFLOW_REF = os.environ.get("WORKFLOW_REF", "817eac479f441396562962e880c1af596638f399")
GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


@unittest.skipUnless(HAS_HYVV or REQUIRE_HYVV, "set HYVV_SRC to a hyvv checkout")
class GateC(unittest.TestCase):
    def setUp(self):
        if not HAS_HYVV:
            self.fail("HYVV_SRC must point at the pinned hyvv checkout when REQUIRE_HYVV=1")
        if not Path(GITLEAKS).exists():
            self.skipTest(f"gitleaks not found at {GITLEAKS}")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC))
        hyvv = importlib.import_module("provisioning")

        # --- repo: an approved test that fails at the baseline -----------------
        work = self.tmp / "work"
        work.mkdir()
        git(["init", "-q", "-b", "main"], work)
        (work / "tests").mkdir()
        (work / "tests" / "test_smoke.py").write_text(
            "from smoke.answer import answer\n\n\ndef test_answer_is_42():\n    assert answer() == 42\n")
        (work / "README.md").write_text("base\n")
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", "base: approved failing test"], work)
        self.sha = git(["rev-parse", "HEAD"], work).stdout.strip()
        origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(origin)], self.tmp)
        git(["push", "-q", str(origin), "main"], work)

        # --- the worker's real patch -------------------------------------------
        worker = self.tmp / "worker"
        git(["clone", "-q", str(work), str(worker)], self.tmp)
        (worker / "smoke").mkdir()
        (worker / "smoke" / "__init__.py").write_text("")
        (worker / "smoke" / "answer.py").write_text("def answer():\n    return 42\n")
        git(["add", "-A"], worker)
        self.patch = git(["diff", "--cached"], worker).stdout

        # --- workflow-shaped frozen contract -----------------------------------
        contract = {
            "contract_version": "acdd/1", "id": "SMOKE-answer", "title": "answer() returns 42",
            "outcome": {"summary": "smoke.answer.answer() returns 42",
                        "observable_behavior": ["import smoke.answer works", "answer() == 42"]},
            "source_rev": self.sha,
            "scope": {"assigned_files": ["smoke/answer.py", "smoke/__init__.py"],
                      "test_files": ["tests/test_smoke.py"]},
            "acceptance_criteria": [{"id": "AC-001", "statement": "answer() == 42",
                                     "tests": ["tests/test_smoke.py::test_answer_is_42"],
                                     "verification": "test"}],
            "tdd": {"applicable": True, "test_first": False,
                    "reason": "the approved test exists at the baseline and is failing; RED is recorded by the baseline run, not signed here"},
            "verification": {"independent": True},
            "evidence_required": ["green"],
            "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
        }
        self.contract = contract
        from bundlebees_plugin import acdd_cli, contract as contract_mod

        contract = contract_mod.freeze_contract(contract, approved_by="reviewer",
                                                approved_at="2026-11-13T00:00:00Z")
        self.contract_sha = contract_mod.check_frozen(contract)

        # --- provisioning host (real hyvv implementation) ----------------------
        self.fixtures = self.tmp / "fixtures"
        self.fixtures.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"schema": "rizzstack.provisioning/1", "profiles": {
            "smoke": {"repo_patterns": ["mojomast/smoke-real"], "workspace_root": str(self.fixtures)}}}))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"projects": {}}))
        registry: dict = {}
        real = hyvv.Provisioner(policy=hyvv.load_policy(policy), journal=hyvv.Journal(self.tmp / "journal"),
                                registry=hyvv.RegistryStore(self.config, cache=registry),
                                credential=lambda repo: "helper", url_for=lambda repo: f"file://{origin}")
        self.host_posts = 0
        fixture = self
        host_name = socket.gethostname()

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
                    return self._send(200, real.capabilities())
                if self.path.startswith("/v1/provision/receipts/"):
                    try:
                        return self._send(200, real.lookup_receipt(self.path.rsplit("/", 1)[-1]))
                    except hyvv.ProvisionError as exc:
                        return self._send(404 if exc.code == "receipt_unknown" else 409, exc.to_dict())
                self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    fixture.host_posts += 1
                    return self._send(200, real.provision(body))
                except hyvv.ProvisionError as exc:
                    return self._send(409, exc.to_dict())

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.url = f"http://127.0.0.1:{httpd.server_port}"

        # --- M1 authorization and job -----------------------------------------
        self.state = self.tmp / "state"
        self.state.mkdir()
        jobs.record_authorization(self.state, job_id="job_c", statement="Build the smoke feature",
                                  repo="mojomast/smoke-real", base_rev=self.sha,
                                  scope=["smoke/answer.py", "smoke/__init__.py"], bees=1, wall_clock_s=1800,
                                  require_tty=False, interactive=False, by="reviewer")
        jobs.create_job(self.state, job_id="job_c", summary="smoke feature",
                        workflow_ref=WORKFLOW_REF, tasks=[{
            "task_id": "T1", "objective": "Create smoke/answer.py returning 42",
            "lease": ["smoke/answer.py", "smoke/__init__.py"],
            "acceptance": {"criteria": ["answer() == 42"],
                           "tests": ["tests/test_smoke.py::test_answer_is_42"],
                           "commands": ["python -m pytest tests/test_smoke.py -q"]},
            "contract": contract, "contract_sha256": self.contract_sha,
            "provision": {"repo": "mojomast/smoke-real", "source_rev": self.sha, "host": host_name,
                          "profile": "smoke", "project": "smoke-real", "contract_sha256": self.contract_sha},
        }])

        queue_file = self.state / "queue.json"
        queue_file.write_text(json.dumps({"f1": {"repo": "mojomast/smoke-real", "job_id": "job_c",
                                                 "title": "answer()", "status": "queued"}}))
        self.queue = queue.JsonQueue(queue_file)

        self.launcher = SimulatedWorker(self.patch, self.sha)
        self.provisioner = P.HyvvProvisioner(lambda m, p, b: self._transport(m, p, b), expect_host=host_name)
        self.host_name = host_name

    def _transport(self, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def tick(self, job_id: str):
        return job_cli.tick(self.state, job_id, client=self.launcher, repo=self.tmp / "work",
                            provisioner=self.provisioner, expect_host=self.host_name, gitleaks=GITLEAKS,
                            suite_command=[sys.executable, "-m", "pytest", "-q"])

    def test_the_feature_is_built_verified_integrated_and_waiting_for_review(self):
        passes = []
        for _ in range(6):
            result = queue.run_once(self.state, self.queue, tick=self.tick)
            passes.append(result)
            if result["action"] == "ready_for_review":
                break

        # AC-008: only now is the feature complete.
        job_now = jobs.load_job(self.state, "job_c")
        debug = {"passes": passes, "tasks": job_now["tasks"],
                 "integration": job_now.get("integration"),
                 "task_states": [t.get("state") for t in job_now["tasks"]],
                 "accepted": queue._job_accepted(self.state, "job_c"),
                 "reasons": queue._job_accepted_reasons(self.state, "job_c"),
                 "binding": job_now["tasks"][0].get("binding")}
        self.assertEqual(passes[-1]["action"], "ready_for_review", json.dumps(debug, indent=1, default=str))
        self.assertEqual(queue.QueueStore(self.state).read("f1")["state"], "ready_for_review")
        self.assertEqual(jobs.load_job(self.state, "job_c")["goal"]["workflow_ref"], WORKFLOW_REF)

        # AC-003: the project was provisioned at the pinned revision.
        registry = json.loads(self.config.read_text())["projects"]
        self.assertEqual(registry["smoke-real"]["source_rev"], self.sha)

        # AC-004/AC-016: exactly one dispatch, and the scanner was in play.
        self.assertEqual(len(self.launcher.posts), 1)
        self.assertGreaterEqual(self.host_posts, 1)

        # AC-006: a real verification record with a local commit.
        from bundlebees_plugin import verify_local

        job = jobs.load_job(self.state, "job_c")
        record = verify_local.read_record(self.state, job["tasks"][0]["attempts"][-1])
        self.assertTrue(record.get("ok"))
        verified_sha = record.get("sha")
        self.assertTrue(verified_sha)
        self.assertEqual(record["suite"]["exit_code"], 0)
        self.assertEqual(record["gitleaks"]["exit_code"], 0)

        # AC-007: integration produced real history containing the feature.
        integration = job["integration"]
        self.assertTrue(integration.get("accepted_waves"))
        head = integration.get("last_commit") or integration["accepted_waves"][-1]["commit"]
        tree = git(["show", f"{head}:smoke/answer.py"], self.tmp / "work").stdout
        self.assertIn("return 42", tree)
        # The approved test passes at that integrated revision.
        checkout = self.tmp / "inspect"
        git(["clone", "-q", str(self.tmp / "work"), str(checkout)], self.tmp)
        git(["checkout", "-q", head], checkout)
        done = subprocess.run([sys.executable, "-m", "pytest", "tests/test_smoke.py", "-q"],
                              cwd=str(checkout), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

        # AC-015: another pass changes nothing.
        before = (len(self.launcher.posts), job["tasks"][0]["state"])
        again = queue.run_once(self.state, self.queue, tick=self.tick)
        self.assertIn(again["action"], {"idle", "ready_for_review"}, "a completed pass must do no further work")
        self.assertEqual((len(self.launcher.posts), jobs.load_job(self.state, "job_c")["tasks"][0]["state"]), before)


class SimulatedWorker:
    """Serves the artifact API for one attempt; produces no side effects."""

    def __init__(self, patch: str, source_rev: str):
        self.patch = patch
        self.source_rev = source_rev
        self.posts: list[dict] = []
        self.attempt_id = "att_c1"

    def create(self, brief):
        self.posts.append(dict(brief))
        return {"attempt_id": self.attempt_id, "state": "succeeded", "brief": dict(brief)}

    def attempts_raw(self):
        return [{"attempt_id": self.attempt_id, "state": "succeeded", "brief": self.posts[-1]}] if self.posts else []

    def get_raw(self, attempt_id):
        return {"attempt_id": attempt_id, "state": "succeeded", "brief": self.posts[-1] if self.posts else {}}

    def artifact(self, attempt_id):
        return {"patch": self.patch, "source_rev": self.source_rev,
                "patch_sha256": hashlib.sha256(self.patch.encode()).hexdigest(),
                "files": [{"path": "smoke/answer.py"}, {"path": "smoke/__init__.py"}]}

    @property
    def idempotent(self):
        return False


if __name__ == "__main__":
    unittest.main()
