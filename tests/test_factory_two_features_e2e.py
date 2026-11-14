"""Two independent queued features reach review through the factory driver.

The first feature reuses the Gate C fixture unchanged.  Once it is at
``ready_for_review`` and its RETURN verifies, a second, independent queued
feature is admitted.  From the enqueue onward the driver alone claims, dispatches,
verifies, integrates and acknowledges each feature; the test body never writes a
job or queue state.  Both RETURNs keep receiver approval pending while local
acceptance is PASS.

When the pinned hyvv, workflow-recovery or gitleaks checkouts are absent this
file installs local doubles for those external tools, and when no strong
sandbox backend is available it uses the production unsafe-local mode.  The
Gate C fixture and every production module are used unchanged.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_FAKE_HYVV = r'''
from __future__ import annotations

import json
import socket
import subprocess
import threading
from pathlib import Path

SCHEMA = "rizzstack.provisioning/1"


class ProvisionError(Exception):
    def __init__(self, code, message="", fix=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.fix = fix

    def to_dict(self):
        return {"error": self.code, "message": self.message, "fix": self.fix}


def load_policy(path):
    return json.loads(Path(path).read_text())


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "journal.jsonl"
        self._lock = threading.Lock()
        self._records = []
        if self.file.exists():
            for line in self.file.read_text().splitlines():
                if line.strip():
                    self._records.append(json.loads(line))

    def append(self, record):
        with self._lock:
            self._records.append(record)
            with self.file.open("a") as handle:
                handle.write(json.dumps(record) + "\n")

    def all(self):
        return list(self._records)


class RegistryStore:
    def __init__(self, path, cache=None):
        self.path = Path(path)
        self.cache = cache if cache is not None else {}

    def _load(self):
        try:
            body = json.loads(self.path.read_text())
        except (OSError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        body.setdefault("projects", {})
        return body

    def _save(self, body):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(body, indent=2, sort_keys=True))

    def record(self, project, body):
        doc = self._load()
        doc["projects"][project] = body
        self._save(doc)
        self.cache[project] = body

    def get(self, project):
        return self._load()["projects"].get(project)

    def all(self):
        return self._load()["projects"]


def _git(args, cwd):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if done.returncode != 0:
        raise ProvisionError("git_failed", done.stderr.strip()[:300])
    return done


class Provisioner:
    def __init__(self, policy, journal, registry, credential=None, url_for=None):
        self.policy = policy
        self.journal = journal
        self.registry = registry
        self.credential = credential
        self.url_for = url_for
        self._lock = threading.Lock()
        self._receipts = {}

    def capabilities(self):
        profiles = sorted((self.policy.get("profiles") or {}).keys())
        return {
            "schema": SCHEMA,
            "backend": "fake-hyvv",
            "profiles": profiles,
            "states": ["ready"],
            "supports_cleanup": False,
            "enabled": True,
            "supports_receipt_lookup": True,
        }

    def lookup_receipt(self, key):
        with self._lock:
            receipt = self._receipts.get(str(key))
        if receipt is None:
            raise ProvisionError("receipt_unknown", f"no receipt for {key}")
        return dict(receipt)

    def provision(self, body):
        key = str(body.get("idempotency_key") or "")
        with self._lock:
            existing = self._receipts.get(key) if key else None
        if existing is not None:
            return dict(existing)
        profile = str(body.get("profile") or "")
        profile_policy = (self.policy.get("profiles") or {}).get(profile)
        if not isinstance(profile_policy, dict):
            raise ProvisionError("profile_unknown", f"profile {profile!r} is not enrolled")
        project = str(body.get("project") or "")
        repo = str(body.get("repo") or "")
        source_rev = str(body.get("source_rev") or "")
        workspace = Path(profile_policy.get("workspace_root") or ".") / project
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            url = self.url_for(repo) if self.url_for else repo
            done = subprocess.run(["git", "clone", "-q", url, str(workspace)],
                                  capture_output=True, text=True)
            if done.returncode != 0:
                raise ProvisionError("clone_failed", done.stderr.strip()[:300])
        _git(["checkout", "-q", "--detach", source_rev], workspace)
        head = _git(["rev-parse", "HEAD"], workspace).stdout.strip()
        if head != source_rev:
            raise ProvisionError("revision_mismatch", f"{head} is not {source_rev}")
        receipt = {
            "schema": SCHEMA,
            "status": "ready",
            "project": project,
            "repo": repo,
            "resolved_revision": source_rev,
            "workspace": str(workspace),
            "profile": profile,
            "host": socket.gethostname(),
            "task_scopes": [],
            "adopted": False,
            "expires_at": "",
        }
        with self._lock:
            if key:
                self._receipts[key] = dict(receipt)
        if self.registry is not None:
            self.registry.record(project, {
                "source_rev": source_rev,
                "workspace": str(workspace),
                "repo": repo,
                "profile": profile,
                "project": project,
                "host": socket.gethostname(),
            })
        if self.journal is not None:
            self.journal.append({"project": project, "state": "ready", "repo": repo,
                                 "receipt": dict(receipt)})
        return dict(receipt)
'''


_FAKE_RECOVERY_PROTOCOL = r'''
from __future__ import annotations

SUPPORTED = ("rizzstack.recovery/1",)


def validate_request(request, **kwargs):
    return dict(request)


def validate_decision(decision, **kwargs):
    return dict(decision)


def validate_pair(request, decision, **kwargs):
    return {"request": dict(request), "decision": dict(decision)}
'''


def _install_gatec_dependencies() -> None:
    """Supply local doubles only when the pinned external tools are absent."""

    workflow = os.environ.get("WORKFLOW_SRC", "")
    if not (workflow and (Path(workflow) / "tools" / "recovery.py").is_file()):
        root = Path(tempfile.mkdtemp(prefix="bb-fake-workflow-"))
        atexit.register(shutil.rmtree, root, True)
        (root / "tools").mkdir()
        (root / "schemas").mkdir()
        (root / "tools" / "recovery.py").write_text(_FAKE_RECOVERY_PROTOCOL, encoding="utf-8")
        (root / "schemas" / "recovery.schema.json").write_text("{}\n", encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "pinned recovery protocol"],
                       cwd=str(root), check=True, env=env)
        os.environ["WORKFLOW_SRC"] = str(root)

    hyvv = os.environ.get("HYVV_SRC", "")
    if not (hyvv and (Path(hyvv) / "provisioning.py").exists()):
        root = Path(tempfile.mkdtemp(prefix="bb-fake-hyvv-"))
        atexit.register(shutil.rmtree, root, True)
        (root / "provisioning.py").write_text(_FAKE_HYVV, encoding="utf-8")
        os.environ["HYVV_SRC"] = str(root)

    gitleaks = os.environ.get("GITLEAKS") or shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"
    if not Path(gitleaks).exists():
        root = Path(tempfile.mkdtemp(prefix="bb-fake-gitleaks-"))
        atexit.register(shutil.rmtree, root, True)
        gitleaks = root / "gitleaks"
        gitleaks.write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", encoding="utf-8")
        gitleaks.chmod(0o755)
        os.environ["GITLEAKS"] = str(gitleaks)


_install_gatec_dependencies()

from bundlebees_plugin import contract as contract_mod
from bundlebees_plugin import factory_driver, job as jobs, queue, return_report, sandbox

from tests import test_queue_factory_e2e as gatec

gatec.HYVV_SRC = os.environ.get("HYVV_SRC") or gatec.HYVV_SRC
gatec.HAS_HYVV = bool(gatec.HYVV_SRC) and (Path(gatec.HYVV_SRC) / "provisioning.py").exists()
gatec.GITLEAKS = os.environ.get("GITLEAKS") or gatec.GITLEAKS


class ScriptedWorker:
    """Serves one capability-bound attempt with a real patch and hashes."""

    def __init__(self, patch: str, source_rev: str, attempt_id: str, files: list[str]):
        self.patch = patch
        self.source_rev = source_rev
        self.attempt_id = attempt_id
        self.files = list(files)
        self.posts: list[dict] = []

    def create(self, brief):
        self.posts.append(dict(brief))
        return {"attempt_id": self.attempt_id, "state": "succeeded", "brief": dict(brief)}

    def attempts_raw(self):
        return ([{"attempt_id": self.attempt_id, "state": "succeeded", "brief": self.posts[-1]}]
                if self.posts else [])

    def get_raw(self, attempt_id):
        return {"attempt_id": attempt_id, "state": "succeeded",
                "brief": self.posts[-1] if self.posts else {}}

    def artifact(self, attempt_id):
        return {"patch": self.patch, "source_rev": self.source_rev,
                "patch_sha256": hashlib.sha256(self.patch.encode()).hexdigest(),
                "files": [{"path": path} for path in self.files]}

    @property
    def idempotent(self):
        return False


class TwoIndependentFeaturesThroughDriver(unittest.TestCase):
    FEATURE_A = "f1"
    FEATURE_B = "f2"
    JOB_A = "job_c"
    JOB_B = "job_d"

    def setUp(self):
        self.fx = gatec.GateC(
            "test_the_feature_is_built_verified_integrated_and_waiting_for_review")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        if sandbox.detect_backend("auto") is None:
            original = sandbox.run_in_sandbox

            def unsafe_local(workdir, argv, **kwargs):
                kwargs["allow_unsafe_local"] = True
                return original(workdir, argv, **kwargs)

            patcher = mock.patch.object(sandbox, "run_in_sandbox", new=unsafe_local)
            patcher.start()
            self.addCleanup(patcher.stop)

    def drive_until_review(self, driver, feature: str, passes: list, limit: int = 12) -> None:
        for _ in range(limit):
            result = driver.run_pass()
            passes.append(result)
            item = queue.QueueStore(self.fx.state).read(feature)
            if item and item.get("state") == "ready_for_review":
                return
        self.fail(json.dumps({"feature": feature, "passes": passes}, indent=1, default=str))

    def assert_return(self, job_id: str) -> dict:
        manifest = return_report.build(self.fx.state, job_id, repo=self.fx.tmp / "work")
        verified = return_report.verify(self.fx.state, job_id, repo=self.fx.tmp / "work")
        self.assertTrue(verified["ok"], verified)
        self.assertEqual(manifest["schema"], "rizzstack.return/1")
        self.assertEqual(manifest["receiver_decision"]["status"], "pending")
        self.assertIsNone(manifest["receiver_decision"]["decided_by"])
        statuses = {row["id"]: row["status"] for row in manifest["acceptance"]}
        self.assertTrue(statuses and all(status == "PASS" for status in statuses.values()), statuses)
        self.assertIn("merge", manifest["pending_approvals"])
        return manifest

    def prepare_second_feature(self, base_commit: str) -> tuple[str, str]:
        fx = self.fx
        work = fx.tmp / "work"
        gatec.git(["reset", "--hard", base_commit], work)
        (work / "tests" / "test_double.py").write_text(
            "from smoke.double import double_answer\n\n\n"
            "def test_double_is_84():\n    assert double_answer() == 84\n",
            encoding="utf-8")
        gatec.git(["add", "-A"], work)
        gatec.git(["commit", "-q", "-m", "base: second approved failing test"], work)
        source_rev = gatec.git(["rev-parse", "HEAD"], work).stdout.strip()
        gatec.git(["push", "-q", "--force", str(fx.tmp / "origin.git"), "main"], work)

        scratch = fx.tmp / "second-scratch"
        gatec.git(["clone", "-q", str(work), str(scratch)], fx.tmp)
        (scratch / "smoke" / "double.py").write_text(
            "from smoke.answer import answer\n\n\n"
            "def double_answer():\n    return answer() * 2\n",
            encoding="utf-8")
        gatec.git(["add", "-A"], scratch)
        patch = gatec.git(["diff", "--cached"], scratch).stdout
        return source_rev, patch

    def create_second_job(self, source_rev: str) -> str:
        fx = self.fx
        contract = {
            "contract_version": "acdd/1",
            "id": "SMOKE-double",
            "title": "double_answer() returns 84",
            "outcome": {
                "summary": "smoke.double.double_answer() returns 84",
                "observable_behavior": ["import smoke.double works", "double_answer() == 84"],
            },
            "source_rev": source_rev,
            "scope": {"assigned_files": ["smoke/double.py"],
                      "test_files": ["tests/test_double.py"]},
            "acceptance_criteria": [{
                "id": "AC-001",
                "statement": "double_answer() == 84",
                "tests": ["tests/test_double.py::test_double_is_84"],
                "verification": "test",
            }],
            "tdd": {
                "applicable": True,
                "test_first": False,
                "reason": "the approved failing test exists at the baseline; RED comes from that run",
            },
            "verification": {"independent": True},
            "evidence_required": ["green"],
            "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
        }
        frozen = contract_mod.freeze_contract(contract, approved_by="reviewer",
                                              approved_at="2026-11-13T00:00:00Z")
        contract_sha = contract_mod.check_frozen(frozen)

        jobs.record_authorization(
            fx.state, job_id=self.JOB_B, statement="Build the second independent smoke feature",
            repo="mojomast/smoke-real", base_rev=source_rev, scope=["smoke/double.py"],
            bees=1, wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        jobs.create_job(
            fx.state, job_id=self.JOB_B, summary="second independent smoke feature",
            workflow_ref=gatec.WORKFLOW_REF, tasks=[{
                "task_id": "T2",
                "objective": "Create smoke/double.py returning 84",
                "lease": ["smoke/double.py"],
                "acceptance": {
                    "criteria": ["double_answer() == 84"],
                    "tests": ["tests/test_double.py::test_double_is_84"],
                    "commands": ["python -m pytest tests/test_double.py -q"],
                },
                "contract": frozen,
                "contract_sha256": contract_sha,
                "provision": {
                    "repo": "mojomast/smoke-real",
                    "source_rev": source_rev,
                    "host": fx.host_name,
                    "profile": "smoke",
                    "project": "smoke-double",
                    "contract_sha256": contract_sha,
                },
            }])
        return contract_sha

    def enqueue_second_feature(self) -> None:
        path = self.fx.state / "queue.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data[self.FEATURE_B] = {
            "repo": "mojomast/smoke-real",
            "job_id": self.JOB_B,
            "title": "double_answer()",
            "status": "queued",
        }
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_two_independent_features_progress_through_driver(self):
        fx = self.fx
        driver = factory_driver.FactoryDriver(fx.state, fx.queue, tick=fx.tick, startup=False)
        first_worker = fx.launcher

        first_passes: list[dict] = []
        self.drive_until_review(driver, self.FEATURE_A, first_passes)
        first_item = queue.QueueStore(fx.state).read(self.FEATURE_A)
        self.assertEqual(first_item and first_item.get("state"), "ready_for_review",
                         json.dumps(first_passes, indent=1, default=str))
        self.assertIsNone(queue.QueueStore(fx.state).read(self.FEATURE_B))
        self.assertEqual(fx.queue.get(self.FEATURE_A).status, "ready_for_review")
        self.assertEqual(len(first_worker.posts), 1)
        job_a = jobs.load_job(fx.state, self.JOB_A)
        self.assertEqual(len(job_a["tasks"][0]["attempts"]), 1)
        self.assertEqual(len(job_a["integration"]["accepted_waves"]), 1)
        return_a = self.assert_return(self.JOB_A)
        commit_a = str(job_a["integration"].get("last_commit")
                       or job_a["integration"]["accepted_waves"][-1]["commit"])
        self.assertEqual(len(commit_a), 40)

        source_rev, patch = self.prepare_second_feature(commit_a)
        contract_sha = self.create_second_job(source_rev)
        self.enqueue_second_feature()

        queued = json.loads((fx.state / "queue.json").read_text(encoding="utf-8"))[self.FEATURE_B]
        self.assertEqual(queued["status"], "queued")
        self.assertNotIn("depends_on", queued)
        self.assertIsNone(queue.QueueStore(fx.state).read(self.FEATURE_B))
        job_b_before = jobs.load_job(fx.state, self.JOB_B, recover=False)
        self.assertEqual(len(job_b_before["tasks"][0].get("attempts") or []), 0)
        self.assertIsNone(job_b_before["tasks"][0].get("binding"))
        self.assertIsNone(job_b_before["tasks"][0].get("provisioning"))
        self.assertIsNone(job_b_before["tasks"][0].get("intent"))
        self.assertNotEqual(job_a["tasks"][0]["contract_sha256"], contract_sha)

        second_worker = ScriptedWorker(patch, source_rev, "att_c2", ["smoke/double.py"])
        fx.launcher = second_worker
        second_passes: list[dict] = []
        self.drive_until_review(driver, self.FEATURE_B, second_passes)

        second_item = queue.QueueStore(fx.state).read(self.FEATURE_B)
        self.assertEqual(second_item and second_item.get("state"), "ready_for_review",
                         json.dumps(second_passes, indent=1, default=str))
        self.assertEqual(fx.queue.get(self.FEATURE_B).status, "ready_for_review")
        self.assertEqual(fx.queue.get(self.FEATURE_A).status, "ready_for_review")
        self.assertEqual(len(second_worker.posts), 1)
        self.assertTrue(second_worker.posts[0].get("project") == "smoke-double")
        self.assertEqual(len(first_worker.posts), 1)
        self.assertEqual(fx.host_posts, 2)

        job_b = jobs.load_job(fx.state, self.JOB_B)
        self.assertEqual(job_b["goal"]["base_rev"], source_rev)
        self.assertNotEqual(job_b["goal"]["base_rev"], job_a["goal"]["base_rev"])
        self.assertEqual(len(job_b["tasks"][0]["attempts"]), 1)
        self.assertEqual(len(job_b["integration"]["accepted_waves"]), 1)
        commit_b = str(job_b["integration"].get("last_commit")
                       or job_b["integration"]["accepted_waves"][-1]["commit"])
        self.assertNotEqual(commit_b, commit_a)
        self.assertEqual(gatec.git(["merge-base", "--is-ancestor", commit_a, commit_b],
                                   fx.tmp / "work", check=False).returncode, 0)
        return_b = self.assert_return(self.JOB_B)
        self.assertEqual(return_b["receiver_decision"]["status"], "pending")
        self.assertNotEqual(return_a["contract"]["id"], return_b["contract"]["id"])
        self.assertEqual(return_a["receiver_decision"], return_b["receiver_decision"])
        self.assertEqual(queue.QueueStore(fx.state).active(), [])

        before = (
            len(second_worker.posts),
            len(jobs.load_job(fx.state, self.JOB_B)["tasks"][0]["attempts"]),
            len(jobs.load_job(fx.state, self.JOB_B)["integration"]["accepted_waves"]),
            fx.queue.get(self.FEATURE_A).status,
            fx.queue.get(self.FEATURE_B).status,
        )
        again = driver.run_pass()
        after = (
            len(second_worker.posts),
            len(jobs.load_job(fx.state, self.JOB_B)["tasks"][0]["attempts"]),
            len(jobs.load_job(fx.state, self.JOB_B)["integration"]["accepted_waves"]),
            fx.queue.get(self.FEATURE_A).status,
            fx.queue.get(self.FEATURE_B).status,
        )
        self.assertIn(again["action"], {"idle", "ready_for_review"}, again)
        self.assertEqual(after, before, "a completed second feature must not be dispatched again")
        self.assertEqual(queue.QueueStore(fx.state).active(), [])


if __name__ == "__main__":
    unittest.main()
