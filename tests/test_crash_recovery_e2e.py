"""P3: the packet-native two-feature factory survives a real process crash.

Every scenario runs the production loop (``queue.run_once`` → ``job_cli.tick`` →
``reconcile.tick`` → ``integrate_job`` → ``handoff.materialize``) in a **separate
OS process** and kills it at one named checkpoint with ``os._exit(97)``. The
restart is a fresh process, so nothing in memory can paper over a missing durable
record.

The worker is not a fake object in the coordinator: it is a standalone HTTP
service (``tests/worker_service.py``) in its own process with a JSON ledger on
disk, so the attempts it accepted outlive the coordinator that dispatched them.

Scenarios (all on the same two dependent features as
``test_queue_two_features_e2e``):

* **Crash A** — ``tick.after_dispatch``: the worker already succeeded, the
  coordinator died before it could acknowledge. Restart must adopt that exact
  attempt, verify it and integrate it, with exactly one dispatch.
* **Crash B** — ``wave.after_persist``: the accepted wave record is on disk but
  the job never accepted it. Restart must recover that wave and its commit
  without integrating a second time.
* **Crash C** — ``transport.after_publish``: the accepted commit is already on the
  mirror, but no resolution was written. Restart must reconcile the existing ref,
  packet, resolution and derived authority, then advance Feature B without a
  changed source and without publishing twice.
* **Control** — no fault, the same fixture through the same worker service.

After *every* scenario the same proof is required: one dispatch per feature, the
final tree contains both features, a fresh clone at B's integrated commit passes
both approved tests, one wave record per job, one verification commit per
feature, and the mirror's accepted ref is the same one it was (never moved).

Run::

    WORKFLOW_SRC=/tmp/verify/wf-packet REQUIRE_WORKFLOW=1 \\
    HYVV_SRC=/tmp/verify/hyvv-provision REQUIRE_HYVV=1 \\
    GITLEAKS=/home/clanker01/.local/bin/gitleaks \\
    python -m pytest tests/test_crash_recovery_e2e.py -q
"""

from __future__ import annotations

import collections
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bundlebees_plugin import contract as contract_mod
from bundlebees_plugin import descendants, handoff as handoff_mod, job as jobs, job_cli, packets, queue
from bundlebees_plugin import provisioning as P
from bundlebees_plugin import transport as transport_mod
from bundlebees_plugin import verify_local

from . import _packet_fixtures as fx
from .worker_service import WorkerServiceClient

ROOT = Path(__file__).resolve().parents[1]
REPO = fx.REPO

HYVV_SRC = os.environ.get("HYVV_SRC", "")
REQUIRE_HYVV = os.environ.get("REQUIRE_HYVV") == "1"
HAS_HYVV = bool(HYVV_SRC) and Path(HYVV_SRC, "provisioning.py").exists()

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"

#: In CI the fixture is mandatory: a missing checkout is a failure, not a skip.
CI = str(os.environ.get("CI") or "").strip().lower() not in ("", "0", "false")
REQUIRE = CI or REQUIRE_HYVV or REQUIRE_WORKFLOW

CRASH_EXIT = 97
CHECKPOINTS = {
    "A": "tick.after_dispatch",
    "B": "wave.after_persist",
    "C": "transport.after_publish",
}


def git(args, cwd, check=True):
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


class ChildRun:
    """One crash-child execution and the durable evidence it left behind."""

    def __init__(self, returncode: int, marker: dict | None, log: list[dict],
                 stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.marker = marker
        self.log = log
        self.stdout = stdout
        self.stderr = stderr

    def tail(self, limit: int = 2500) -> str:
        body = json.dumps(self.log[-8:], indent=1, default=str) if self.log else "(no run_once log)"
        return f"rc={self.returncode}\nlog tail:\n{body}\nstderr:\n{self.stderr[-limit:]}\nstdout:\n{self.stdout[-limit:]}"


class CrashRecoveryTwoFeatures(unittest.TestCase):
    """The packet-native two-feature fixture, driven with a real worker service."""

    maxDiff = 6000

    # ------------------------------------------------------------------ setup
    def setUp(self) -> None:
        missing = []
        if not HAS_HYVV:
            missing.append("HYVV_SRC (pinned hyvv checkout with provisioning.py)")
        if not HAS_WORKFLOW:
            missing.append("WORKFLOW_SRC (pinned workflow checkout with tools/feature_packet.py)")
        if not Path(GITLEAKS).exists():
            missing.append(f"gitleaks ({GITLEAKS})")
        if missing:
            message = "crash-recovery e2e fixture is incomplete: " + "; ".join(missing)
            if REQUIRE:
                self.fail(message + " — this test must run in CI, not skip")
            self.skipTest(message)

        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC) if HYVV_SRC in sys.path else None)
        self.hyvv = importlib.import_module("provisioning")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.host = socket.gethostname()

        self.workflow_ref = fx.workflow_ref()
        self.assertEqual(len(self.workflow_ref), 40)

        # -- repository: both approved tests exist and fail at the baseline ----
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
        git(["push", "-q", str(self.mirror), f"{self.root}:refs/heads/main"], self.work)

        # -- real patches, produced in a scratch clone -------------------------
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

        # -- contracts: A frozen now; B only an unfrozen template --------------
        self.contract_a = fx.acdd_contract(
            contract_id="SMOKE-A", criterion_id="AC-001", statement="answer() == 42",
            assigned_file=fx.ANSWER_FILE, test_file=fx.ANSWER_TEST_FILE, test_id=fx.ANSWER_TEST,
            source_rev=self.root)
        self.template_b = fx.acdd_contract(
            contract_id="SMOKE-B", criterion_id="AC-002", statement="double_answer() == 84",
            assigned_file=fx.DOUBLE_FILE, test_file=fx.DOUBLE_TEST_FILE, test_id=fx.DOUBLE_TEST,
            source_rev=self.root)

        # -- state: A authorized now; B only through the enrolled policy -------
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
        self.digest_a = packets.validate(self.packet_a)
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

        self.packet_b = fx.intent_packet(template=self.template_b, workflow_ref=self.workflow_ref)
        self.digest_b = packets.validate(self.packet_b)
        self.intent_path = fx.write_packet(self.state, self.packet_b)
        self.intent_file_sha = fx.file_sha256(self.intent_path)

        self.queue_file = self.state / "queue.json"
        self.queue_file.write_text(json.dumps({
            fx.FEATURE_A: {"repo": REPO, "job_id": fx.JOB_A, "status": "queued"},
            fx.FEATURE_B: {"repo": REPO, "job_id": fx.JOB_B, "status": "queued",
                           "depends_on": [fx.FEATURE_A]},
        }))

        # -- real provisioning host, served over HTTP -------------------------
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
        self.host_posts: list[dict] = []
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
                    fixture.host_posts.append(dict(body))
                    return self._send(200, fixture.real.provision(body))
                except fixture.hyvv.ProvisionError as exc:
                    return self._send(409, exc.to_dict())

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

        # -- the worker: its own process, its own durable ledger ---------------
        script = self.tmp / "worker-script.json"
        script.write_text(json.dumps({"projects": {name: {"patch": patch}
                                                   for name, patch in self.patches.items()}}))
        self.worker_ledger = self.tmp / "worker-ledger.json"
        ready = self.tmp / "worker-ready.json"
        self.worker_proc = subprocess.Popen(
            [sys.executable, "-m", "tests.worker_service", "--ledger", str(self.worker_ledger),
             "--script", str(script), "--ready-file", str(ready)],
            cwd=str(ROOT), env=self.child_env(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_worker)
        self.worker_url = self._wait_for_worker(ready)
        self.client = WorkerServiceClient(self.worker_url)

        self.queue = queue.JsonQueue(self.queue_file)

    def child_env(self) -> dict[str, str]:
        """A clean environment: the repo on PYTHONPATH, no ambient interpreter paths."""

        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _wait_for_worker(self, ready: Path) -> str:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ready.exists():
                try:
                    return str(json.loads(ready.read_text())["url"])
                except (OSError, ValueError, KeyError):
                    pass
            if self.worker_proc.poll() is not None:
                out, err = self.worker_proc.communicate(timeout=5)
                self.fail(f"the worker service exited before it was ready: rc={self.worker_proc.returncode}\n"
                          f"stdout:\n{out}\nstderr:\n{err}")
            time.sleep(0.05)
        out, err = "", ""
        try:
            self.worker_proc.kill()
            out, err = self.worker_proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self.fail(f"the worker service never announced itself\nstdout:\n{out}\nstderr:\n{err}")

    def _stop_worker(self) -> None:
        proc = getattr(self, "worker_proc", None)
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    # ---------------------------------------------------------------- driving
    def tick(self, job_id):
        suite = ([fx.ANSWER_TEST_FILE, "-q"] if job_id == fx.JOB_A else [fx.DOUBLE_TEST_FILE, "-q"])
        return job_cli.tick(self.state, job_id, client=self.client, repo=self.work,
                            provisioner=P.HyvvProvisioner(self._transport, expect_host=self.host),
                            expect_host=self.host, gitleaks=GITLEAKS,
                            suite_command=[sys.executable, "-m", "pytest", *suite])

    def _transport(self, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def handoff_context(self):
        return {"worktree": str(self.work), "mirror": str(self.mirror), "repo_identity": REPO}

    def run_once(self):
        return queue.run_once(self.state, self.queue, tick=self.tick,
                              handoff_context=self.handoff_context())

    def drive(self, until=fx.FEATURE_B, limit=24):
        """Consume the queue until *until* is ready for review; return every step."""

        log = []
        errors = (handoff_mod.HandoffError, packets.PacketError, jobs.JobError,
                  queue.QueueError, descendants.DescendantError)
        for _ in range(limit):
            try:
                result = self.run_once()
            except errors as exc:
                self.fail(f"run_once refused the restart instead of recovering: "
                          f"{getattr(exc, 'code', '')}: {exc}\nsteps so far: "
                          f"{json.dumps(log, indent=1, default=str)}")
            log.append(result)
            self.assertNotIn(result.get("action"), ("blocked", "failed", "needs_approval"),
                             f"the restart did not recover: {json.dumps(result, default=str)}\n"
                             f"steps so far: {json.dumps(log, indent=1, default=str)}")
            if result.get("action") == "ready_for_review" and result.get("feature") == until:
                return log
        self.fail(f"{until} never reached ready_for_review: {json.dumps(log, indent=1, default=str)}")

    def run_child(self, checkpoint: str) -> ChildRun:
        marker = self.tmp / f"crash-{checkpoint.replace('.', '-')}.json"
        log_path = self.tmp / f"child-{checkpoint.replace('.', '-')}.jsonl"
        suites = {
            fx.JOB_A: [sys.executable, "-m", "pytest", fx.ANSWER_TEST_FILE, "-q"],
            fx.JOB_B: [sys.executable, "-m", "pytest", fx.DOUBLE_TEST_FILE, "-q"],
        }
        config = {
            "state_dir": str(self.state), "queue_file": str(self.queue_file),
            "worktree": str(self.work), "mirror": str(self.mirror), "repo_identity": REPO,
            "provision_url": self.url, "worker_url": self.worker_url,
            "hyvv_src": HYVV_SRC, "expect_host": self.host, "gitleaks": GITLEAKS,
            "feature_b": fx.FEATURE_B, "suites": suites, "log": str(log_path), "max_ticks": 8,
        }
        config_path = self.tmp / f"child-{checkpoint.replace('.', '-')}.json"
        config_path.write_text(json.dumps(config))
        env = self.child_env()
        env["BUNDLEBEES_CRASH_AT"] = checkpoint
        env["BUNDLEBEES_CRASH_MARKER"] = str(marker)
        done = subprocess.run(
            [sys.executable, "-m", "tests.crash_child", "packet", str(config_path)],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300,
        )
        entries = []
        if log_path.exists():
            entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        body = None
        if marker.exists():
            body = json.loads(marker.read_text())
        return ChildRun(done.returncode, body, entries, done.stdout, done.stderr)

    def assert_crashed(self, child: ChildRun, name: str) -> dict:
        if child.returncode == 0:
            self.fail(
                f"checkpoint {name!r} never fired: the child ran to completion and exited 0.\n"
                f"That means no faults.checkpoint({name!r}) call site is reachable on this path — "
                f"the production checkpoint is missing (this harness must not fake it).\n{child.tail()}")
        self.assertEqual(child.returncode, CRASH_EXIT,
                         f"the child died for another reason than the requested checkpoint {name!r}\n{child.tail()}")
        self.assertIsNotNone(child.marker, f"the child exited {CRASH_EXIT} without writing its marker\n{child.tail()}")
        self.assertEqual(child.marker.get("checkpoint"), name, child.tail())
        self.assertNotEqual(child.marker.get("pid"), os.getpid(), "the crash must happen in a separate process")
        self.assertIsInstance(child.marker.get("payload"), dict)
        pid = int(child.marker["pid"])
        with self.assertRaises(ProcessLookupError, msg=f"the crashed coordinator {pid} is still alive"):
            os.kill(pid, 0)
        # The worker is a different process and did not die with the coordinator:
        # its ledger, not the coordinator's memory, is what the restart reads.
        self.assertNotEqual(pid, self.worker_proc.pid, "the worker ran inside the coordinator process")
        self.assertIsNone(self.worker_proc.poll(), "the worker service died with the coordinator")
        self.assertTrue(self.worker_ledger.exists(), "the worker ledger is not durable")
        return child.marker

    # -------------------------------------------------------------- evidence
    def attempts_from_service(self) -> list[dict]:
        return self.client.attempts_raw()

    def ledger_attempts(self) -> list[dict]:
        body = json.loads(self.worker_ledger.read_text())
        return list(body["attempts"])

    def mirror_accepted(self) -> dict[str, str]:
        out = git(["ls-remote", str(self.mirror), "refs/bb/accepted/*"], self.tmp).stdout
        found = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found[parts[1]] = parts[0]
        return found

    def wave_files(self) -> dict[str, Path]:
        return {path.name: path for path in sorted((self.state / "waves").glob("*.json"))}

    def integration_commit(self, job_id: str) -> str:
        job = jobs.load_job(self.state, job_id)
        commit = str((job.get("integration") or {}).get("last_commit") or "")
        self.assertEqual(len(commit), 40, f"{job_id} has no integrated commit: {job.get('integration')}")
        return commit

    def verify_records(self) -> dict[str, dict]:
        root = self.state / verify_local.RECORD_DIR
        found = {}
        for path in sorted(root.glob("*.json")):
            found[path.stem] = json.loads(path.read_text())
        return found

    # ------------------------------------------------------------- assertions
    def assert_final_proof(self, *, mirror_before: dict[str, str], wave_hashes: dict[str, str] | None = None):
        job_a = jobs.load_job(self.state, fx.JOB_A)
        job_b = jobs.load_job(self.state, fx.JOB_B)
        commit_a = self.integration_commit(fx.JOB_A)
        commit_b = self.integration_commit(fx.JOB_B)
        self.assertNotEqual(commit_a, commit_b)

        # 1. Exactly one dispatch per feature — from the worker's durable ledger
        #    and from the coordinator's own task records.
        ledger = self.ledger_attempts()
        self.assertEqual(collections.Counter(str(e["project"]) for e in ledger),
                         {"smoke-answer": 1, "smoke-double": 1},
                         f"the worker accepted a duplicate dispatch: {json.dumps(ledger, indent=1)[:3000]}")
        served = collections.Counter(str(e.get("brief", {}).get("project")) for e in self.attempts_from_service())
        self.assertEqual(served, {"smoke-answer": 1, "smoke-double": 1},
                         "the worker service's HTTP view disagrees with its ledger")
        self.assertEqual(len(job_a["tasks"][0].get("attempts") or []), 1, "Feature A was dispatched twice")
        self.assertEqual(len(job_b["tasks"][0].get("attempts") or []), 1, "Feature B was dispatched twice")
        self.assertEqual(len(self.host_posts), 2,
                         f"each feature must be provisioned exactly once: {self.host_posts}")

        # 2/3. A fresh clone at B's integrated commit contains both features and
        #      passes both approved tests.
        self.assertEqual(job_b["goal"].get("base_rev"), commit_a,
                         "Feature B is not bound to Feature A's accepted commit")
        final = self.tmp / "final-clone"
        git(["clone", "-q", str(self.work), str(final)], self.tmp)
        git(["checkout", "-q", commit_b], final)
        for name in (fx.ANSWER_FILE, fx.DOUBLE_FILE):
            self.assertTrue((final / name).is_file(), f"{name} is missing from the integrated tree")
        self.assertIn("return 42", (final / fx.ANSWER_FILE).read_text())
        self.assertIn("answer() * 2", (final / fx.DOUBLE_FILE).read_text())
        clean = {key: value for key, value in self.child_env().items() if key != "PYTHONPATH"}
        done = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=str(final),
                              capture_output=True, text=True, env=clean)
        self.assertEqual(done.returncode, 0,
                         f"a fresh clone at {commit_b[:8]} does not pass:\n{done.stdout}\n{done.stderr}")
        self.assertIn("2 passed", done.stdout)

        # 4. One wave record per job, each accepted at its integrated commit.
        waves = self.wave_files()
        for job_id, commit in ((fx.JOB_A, commit_a), (fx.JOB_B, commit_b)):
            matching = [name for name in waves if name.startswith(f"{job_id}.w")]
            self.assertEqual(len(matching), 1, f"expected one wave record for {job_id}, found {matching}")
            record = json.loads(waves[matching[0]].read_text())
            self.assertTrue(record.get("accepted"), f"{matching[0]} is not accepted: {record}")
            self.assertEqual(record.get("head_sha"), commit, f"{matching[0]} names another commit")
            self.assertEqual(record.get("wave_id"), matching[0][:-5])
            if wave_hashes and matching[0] in wave_hashes:
                self.assertEqual(fx.file_sha256(waves[matching[0]]), wave_hashes[matching[0]],
                                 f"{matching[0]} was rewritten by the restart (a second integration)")
        self.assertEqual(len(waves), 2, f"unexpected wave records: {sorted(waves)}")

        # 5. One verification commit per feature.
        records = self.verify_records()
        attempt_a = job_a["tasks"][0]["attempts"][-1]
        attempt_b = job_b["tasks"][0]["attempts"][-1]
        self.assertEqual(set(records), {attempt_a, attempt_b},
                         f"expected exactly one verification per feature, found {sorted(records)}")
        for attempt_id, commit in ((attempt_a, commit_b), (attempt_b, commit_b)):
            record = records[attempt_id]
            self.assertIs(record.get("ok"), True, f"{attempt_id} did not verify: {record}")
            self.assertEqual(len(str(record.get("sha") or "")), 40)
            sha = str(record["sha"])
            self.assertEqual(git(["merge-base", "--is-ancestor", sha, commit],
                                 self.work, check=False).returncode, 0,
                             f"{attempt_id}'s verified commit is not in the final history")
        self.assertEqual(len(git(["branch", "--list", "bb-verify/*"], self.work).stdout.splitlines()), 2,
                         "a feature was verified more than once")

        # 6. The mirror's accepted ref: exactly one, never moved, never duplicated.
        after = self.mirror_accepted()
        for ref, sha in mirror_before.items():
            self.assertEqual(after.get(ref), sha, f"the mirror ref {ref} moved during recovery")
        expected_ref = transport_mod.ref_for(fx.JOB_A, commit_a)
        self.assertEqual(after, {expected_ref: commit_a},
                         f"the mirror does not hold exactly the one accepted ref: {after}")
        self.assertNotEqual(git(["cat-file", "-e", f"{commit_a}^{{commit}}"], self.origin,
                                check=False).returncode, 0,
                            "the origin has A's commit: the mirror claim is not proven")
        receipt = json.loads((self.state / "transport" / f"{fx.JOB_A}.{commit_a[:12]}.json").read_text())
        self.assertEqual(receipt.get("source_sha"), commit_a)
        self.assertEqual(receipt.get("mirror_ref"), expected_ref)

        # The queue itself must agree that both features are reviewable.
        for feature in (fx.FEATURE_A, fx.FEATURE_B):
            item = self.queue.get(feature)
            self.assertIsNotNone(item, f"{feature} vanished from the queue")
            self.assertEqual(item.status, "ready_for_review")

        # P4 restart proof: reports are rebuilt from durable state only, and a
        # second rebuild of the same snapshot is byte-identical.
        from bundlebees_plugin import job_cli as _jc

        for job_id in (fx.JOB_A, fx.JOB_B):
            first = _jc.report(self.state, job_id, repo=self.work)
            manifest = json.loads((self.state / "jobs" / job_id / "return.json").read_text())
            self.assertEqual(manifest["receiver_decision"]["status"], "pending")
            statuses = {a["id"]: a["status"] for a in manifest["acceptance"]}
            self.assertTrue(statuses and all(v == "PASS" for v in statuses.values()), statuses)
            second = _jc.report(self.state, job_id, repo=self.work)
            self.assertEqual(second["return"]["evidence_digest"], first["return"]["evidence_digest"],
                             "a rebuilt RETURN after restart changed its evidence identity")

    def assert_intent_untouched(self):
        self.assertEqual(fx.file_sha256(self.intent_path), self.intent_file_sha,
                         "the immutable intent packet was rewritten")
        self.assertEqual(fx.digest(fx.read_packet(self.state, fx.JOB_B)), self.digest_b)

    # ------------------------------------------------------------------ tests
    def test_control_runs_both_features_without_a_fault(self):
        """The same fixture and worker service, no crash: the baseline of the proof."""

        mirror_before = self.mirror_accepted()
        self.assertEqual(mirror_before, {})
        self.drive()
        self.assert_final_proof(mirror_before=mirror_before)
        self.assert_intent_untouched()

    def test_crash_a_after_dispatch_adopts_the_finished_attempt(self):
        """Kill after the worker succeeded and before the coordinator acknowledged."""

        margin = self.mirror_accepted()
        child = self.run_child(CHECKPOINTS["A"])
        self.assert_crashed(child, CHECKPOINTS["A"])

        # Crash-time evidence: the worker really finished; the coordinator did not
        # verify, integrate, publish or record a wave yet.
        ledger = self.ledger_attempts()
        self.assertEqual(len(ledger), 1, f"expected the one dispatched attempt: {ledger}")
        self.assertEqual(ledger[0]["project"], "smoke-answer")
        self.assertEqual(ledger[0]["state"], "succeeded")
        self.assertEqual(ledger[0]["brief"]["job_id"], fx.JOB_A)
        attempt_id = ledger[0]["attempt_id"]
        self.assertIsNone(verify_local.read_record(self.state, attempt_id),
                          "the crashed child verified the attempt before dying after dispatch")
        self.assertEqual(self.wave_files(), {}, "the crashed child integrated before dying after dispatch")
        self.assertEqual(self.mirror_accepted(), margin)

        self.drive()
        self.assert_final_proof(mirror_before=margin)
        job_a = jobs.load_job(self.state, fx.JOB_A)
        self.assertEqual(job_a["tasks"][0]["attempts"], [attempt_id],
                         "the restart re-dispatched instead of adopting the existing attempt")

    def test_crash_b_after_wave_persist_recovers_without_reintegrating(self):
        """Kill after the accepted wave record is on disk and before it is accepted."""

        margin = self.mirror_accepted()
        child = self.run_child(CHECKPOINTS["B"])
        self.assert_crashed(child, CHECKPOINTS["B"])

        waves = self.wave_files()
        self.assertEqual(sorted(waves), [f"{fx.JOB_A}.w1.json"],
                         f"expected the wave record to be persisted before the crash: {sorted(waves)}")
        record = json.loads(waves[f"{fx.JOB_A}.w1.json"].read_text())
        self.assertTrue(record.get("accepted"),
                        f"the crash was supposed to happen after the accepted wave persisted: {record}")
        self.assertEqual(len(str(record.get("head_sha") or "")), 40)
        wave_hashes = {name: fx.file_sha256(path) for name, path in waves.items()}
        commit_a = record["head_sha"]
        # The commit is real and the ref may or may not have been updated yet;
        # either way the restart must not integrate again.
        self.assertEqual(git(["cat-file", "-e", f"{commit_a}^{{commit}}"], self.work,
                             check=False).returncode, 0, "the accepted wave names a commit that is absent")
        # The job document has not accepted the wave yet: that is the window the
        # restart must close from the record alone.
        on_disk = jobs.load_job(self.state, fx.JOB_A, recover=False)
        self.assertNotEqual((on_disk.get("integration") or {}).get("last_commit"), commit_a,
                            "the job already recorded the wave before the crash; the checkpoint is too "
                            "late to prove recovery")

        self.drive()
        self.assert_final_proof(mirror_before=margin, wave_hashes=wave_hashes)
        self.assertEqual(self.integration_commit(fx.JOB_A), commit_a,
                         "the restart integrated a second time instead of recovering the accepted wave")
        self.assertEqual(git(["rev-parse", "refs/bb/integrated/job_a"], self.work).stdout.strip(), commit_a)
        self.assert_intent_untouched()

    def test_crash_c_after_publish_reconciles_the_existing_ref(self):
        """Kill after the accepted commit is published and before any resolution exists."""

        child = self.run_child(CHECKPOINTS["C"])
        self.assert_crashed(child, CHECKPOINTS["C"])

        job_a = jobs.load_job(self.state, fx.JOB_A)
        commit_a = self.integration_commit(fx.JOB_A)
        margin = self.mirror_accepted()
        self.assertEqual(margin, {transport_mod.ref_for(fx.JOB_A, commit_a): commit_a},
                         f"the accepted commit was not published before the crash: {margin}")
        self.assertIsNone(fx.resolution_for(self.state, self.digest_b),
                          "the crashed child wrote the resolution; the checkpoint is past the boundary")
        self.assertFalse(jobs.job_path(self.state, fx.JOB_B).exists(),
                         "the crashed child materialized Feature B before dying after publish")
        queued_b = self.queue.get(fx.FEATURE_B)
        self.assertIsNotNone(queued_b)
        self.assertEqual(queued_b.status, "queued")
        self.assert_intent_untouched()
        self.assertEqual((job_a.get("integration") or {}).get("last_commit"), commit_a)

        self.drive()
        self.assert_final_proof(mirror_before=margin)
        self.assert_intent_untouched()
        found = fx.resolution_for(self.state, self.digest_b)
        self.assertIsNotNone(found, "the restart did not write a resolution record")
        path, resolution = found
        self.assertEqual(path, self.state / "resolutions" / f"{fx.JOB_B}.json")
        self.assertEqual(resolution.get("accepted_commit"), commit_a)
        self.assertEqual(resolution.get("derived_source_rev"), commit_a)
        self.assertEqual(resolution.get("packet_digest"), self.digest_b)
        self.assertEqual(resolution.get("policy_id"), fx.POLICY_B)
        self.assertEqual(resolution.get("policy_digest"),
                         handoff_mod.policy_digest(handoff_mod.load_policy(self.state, fx.POLICY_B)))
        job_b = jobs.load_job(self.state, fx.JOB_B)
        self.assertEqual(job_b["goal"].get("base_rev"), commit_a)
        self.assertEqual(job_b["goal"].get("packet_digest"), self.digest_b)
        # One publication, not two: the receipt for A's commit is a recovery.
        receipt = json.loads((self.state / "transport" / f"{fx.JOB_A}.{commit_a[:12]}.json").read_text())
        self.assertTrue(receipt.get("recovered"),
                        "the restart re-published instead of reconciling the existing mirror ref")
        self.assertEqual(
            len([ref for ref in self.mirror_accepted() if ref.startswith("refs/bb/accepted/job_a/")]), 1)


if __name__ == "__main__":
    unittest.main()
