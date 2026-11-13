"""Vertical slice: authorized job -> missing project -> provisioned -> dispatched.

Runs the *real* hyvv provisioning implementation (imported from HYVV_SRC) behind a
local HTTP fixture, against a real temporary Git repository. Skips when hyvv is
not checked out beside this repo; the fixture-based client tests still run in CI.
"""

from __future__ import annotations

import importlib
import json
import socket
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bundlebees_plugin import job as jobs
from bundlebees_plugin import job_cli, leases, provisioning as P

HYVV_SRC = os.environ.get("HYVV_SRC", "")
SHA = ""


def git(args, cwd):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


@unittest.skipUnless(HYVV_SRC and Path(HYVV_SRC, "provisioning.py").exists(),
                     "set HYVV_SRC to a hyvv checkout to run the real-implementation slice")
class VerticalSlice(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC))
        hyvv = importlib.import_module("provisioning")

        work = self.tmp / "work"
        work.mkdir()
        git(["init", "-q", "-b", "main"], work)
        (work / "README.md").write_text("base\n")
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", "base"], work)
        self.sha = git(["rev-parse", "HEAD"], work).stdout.strip()
        origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(origin)], self.tmp)
        git(["push", "-q", str(origin), "main"], work)

        self.fixtures = self.tmp / "fixtures"
        self.fixtures.mkdir()
        policy_path = self.tmp / "policy.json"
        policy_path.write_text(json.dumps({
            "schema": "rizzstack.provisioning/1",
            "profiles": {"smoke": {"repo_patterns": ["mojomast/smoke-*"],
                                   "workspace_root": str(self.fixtures), "max_provisioned": 2}},
        }))
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"projects": {}}))
        projects: dict = {}

        real = hyvv.Provisioner(
            policy=hyvv.load_policy(policy_path),
            journal=hyvv.Journal(self.tmp / "journal"),
            registry=hyvv.RegistryStore(self.config, cache=projects),
            credential=lambda repo: "host-credential-helper",
            url_for=lambda repo: f"file://{origin}",
        )
        self.real = real

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
                    return self._send(200, real.capabilities())
                if self.path.startswith("/v1/provision/receipts/"):
                    key = self.path.rsplit("/", 1)[-1]
                    try:
                        return self._send(200, real.lookup_receipt(key))
                    except hyvv.ProvisionError as exc:
                        return self._send(404 if exc.code == "receipt_unknown" else 409, exc.to_dict())
                self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    return self._send(200, real.provision(body))
                except hyvv.ProvisionError as exc:
                    return self._send(409 if exc.code in ("journal_conflict", "provision_in_progress") else 400, exc.to_dict())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

        self.state = self.tmp / "state"
        self.state.mkdir()
        jobs.record_authorization(
            self.state, job_id="job_slice", statement="Execute the smoke task in the disposable repo",
            repo="mojomast/smoke-real", base_rev=self.sha, scope=["smoke/answer.py"],
            bees=1, wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer",
        )
        jobs.create_job(self.state, job_id="job_slice", summary="provision then dispatch", tasks=[{
            "task_id": "T1",
            "objective": "Create smoke/answer.py returning 42",
            "assigned_files": ["smoke/answer.py"],
            "lease": ["smoke/answer.py"],
            "acceptance": {"criteria": ["answer() == 42"],
                           "tests": ["tests/test_smoke.py::test_answer_is_42"],
                           "commands": ["python -m pytest -q"]},
            "provision": {"repo": "mojomast/smoke-real", "source_rev": self.sha, "host": socket.gethostname(),
                          "profile": "smoke", "project": "smoke-real", "contract_sha256": "c" * 64},
        }])

    def transport(self, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_an_unprovisioned_authorized_job_is_provisioned_then_dispatched(self):
        launcher = FakeLauncher()
        provisioner = P.HyvvProvisioner(self.transport, expect_host=socket.gethostname())

        first = job_cli.tick(self.state, "job_slice", client=launcher, repo=self.tmp / "work",
                             provisioner=provisioner, expect_host=socket.gethostname())

        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(len(launcher.posts), 1)
        brief = launcher.posts[0]
        self.assertEqual(brief["project"], "smoke-real")
        self.assertEqual(brief["source_rev"], self.sha)
        registry = json.loads(self.config.read_text())["projects"]
        self.assertEqual(registry["smoke-real"]["source_rev"], self.sha)

        job = jobs.load_job(self.state, "job_slice")
        binding = job["tasks"][0]["binding"]
        self.assertEqual(binding["project"], "smoke-real")
        self.assertEqual(len(binding["receipt_digest"]), 64)
        lease = leases.load_lease(self.state, launcher.attempt_id)
        self.assertEqual(lease["brief"]["project"], "smoke-real")
        self.assertEqual(lease["brief"]["source_rev"], self.sha)

        second = job_cli.tick(self.state, "job_slice", client=launcher, repo=self.tmp / "work",
                              provisioner=provisioner, expect_host=socket.gethostname())
        self.assertEqual(len(launcher.posts), 1, "a repeated tick must not dispatch again")
        self.assertIn(second["status"], {"ok", "waiting", "blocked", "idle"})

    def test_a_lost_acknowledgement_recovers_without_a_second_post(self):
        launcher = FakeLauncher()
        provisioner = P.HyvvProvisioner(self.transport, expect_host=socket.gethostname())

        original = provisioner.transport
        calls = {"posted": 0}

        def flaky(method, path, body):
            if method == "POST" and path == P.PROVISION_PATH and calls["posted"] == 0:
                calls["posted"] += 1
                original(method, path, body)          # the host applies it...
                return 503, {"error": "gateway timeout"}   # ...but the ack is lost
            return original(method, path, body)

        provisioner.transport = flaky
        outcome = job_cli.tick(self.state, "job_slice", client=launcher, repo=self.tmp / "work",
                               provisioner=provisioner, expect_host=socket.gethostname())
        self.assertEqual(outcome["status"], "ok", outcome)

        job = jobs.load_job(self.state, "job_slice")
        self.assertEqual(job["tasks"][0]["provisioning"]["state"], "bound")
        self.assertEqual(len(launcher.posts), 1)


class FakeLauncher:
    def __init__(self):
        self.posts: list[dict] = []
        self.attempt_id = "att_slice"

    def post_attempt(self, brief):
        return self.create(brief)

    def create(self, brief):
        self.posts.append(dict(brief))
        return {"attempt_id": self.attempt_id, "state": "queued", "brief": dict(brief)}

    def list_attempts(self):
        return self.attempts_raw()

    def attempts_raw(self):
        return []

    def get_attempt(self, attempt_id):
        return self.get_raw(attempt_id)

    def get_raw(self, attempt_id):
        return {"attempt_id": attempt_id, "state": "running", "brief": self.posts[-1] if self.posts else {}}

    @property
    def idempotent(self):
        return False


if __name__ == "__main__":
    unittest.main()
