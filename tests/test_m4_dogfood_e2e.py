"""M4 dogfood acceptance: bounded recovery inside one authorised objective.

One authorisation, two tasks, the real acdd-lint tree, a real git origin and
bare mirror, a real hyvv Provisioner over HTTP, the real queue and the real
verifier. The controlled failure is declared before dispatch: T2's *first*
frozen contract names ``…::Reachability::test_ancestor_ok``, while the approved
fixture method is ``test_reachable_commit_is_silent``. The worker's patch is
genuine, but no patch can make a selector that does not exist pass, so the
verifier rejects it. The production loop must classify the rejection as
``specification`` from the verifier's own evidence, persist a durable request,
obtain a validated decision, freeze a superseding contract
(``supersedes``/``supersedes_sha256``), dispatch exactly one corrective attempt,
verify and integrate it, and write a RETURN that names all three attempts.

Run (the pinned environment; the test fails, never skips, when a component is
missing):

    CI=1 \\
    HYVV_SRC=/tmp/verify/hyvv-provision \\
    WORKFLOW_SRC=/tmp/verify/wf-packet \\
    GITLEAKS=/home/clanker01/.local/bin/gitleaks \\
    /tmp/verify/bb15-venv/bin/python -m pytest tests/test_m4_dogfood_e2e.py -q

Nothing here pushes, merges or deploys. Every repository is local.
"""

from __future__ import annotations

import fnmatch
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
from bundlebees_plugin import integrate_job, job as jobs, job_cli, packets, queue, recovery, return_report, verify_local
from bundlebees_plugin import provisioning as P

from . import _packet_fixtures as fx
from . import m4_worker

PY = sys.executable

HYVV_SRC = os.environ.get("HYVV_SRC", "")
WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
GITLEAKS = os.environ.get("GITLEAKS") or shutil.which("gitleaks") or "/home/clanker01/.local/bin/gitleaks"

REPO = "mojomast/workflow"
FEATURE = "acdd-source-rev"
JOB = "job_m4_dogfood"
T1 = "T1"
T2 = "T2"

CONTRACT_1 = "T1-SOURCE-REV"
CONTRACT_2 = "T2-REACHABILITY"
MODULE_1 = "tools/acdd-lint/acdd_lint/checks_source_rev.py"
TEST_1 = "tools/acdd-lint/tests/test_check_source_rev.py"
MODULE_2 = "tools/acdd-lint/acdd_lint/checks_reachability.py"
TEST_2 = "tools/acdd-lint/tests/test_check_reachability.py"
T1_TEST = f"{TEST_1}::SourceRev::test_malformed_source_rev_is_refused"
T2_BAD_TEST = f"{TEST_2}::Reachability::test_ancestor_ok"          # does not exist: the injected defect
T2_GOOD_TEST = f"{TEST_2}::Reachability::test_reachable_commit_is_silent"  # the approved fixture method
T2_CANDIDATES = [
    T2_GOOD_TEST,
    f"{TEST_2}::Reachability::test_absent_commit_is_refused",
    f"{TEST_2}::Reachability::test_no_repository_is_explicitly_not_checked",
]

DECIDED_AT = "2026-11-13T20:00:00Z"
PROFILE = "factory"
PROJECT_1 = "acdd-source-rev"
PROJECT_2 = "acdd-reachability"

SUITE = [PY, "-m", "pytest", "tools/acdd-lint/tests", "-q"]
GATES = ("merge", "release", "deploy", "publish")

STATEMENT = (
    "Add contract source-revision validation to acdd-lint: T1 refuses a source_rev that is not "
    "40 hex, T2 refuses a source_rev unreachable from the repository head (not checked when no "
    "repository is supplied). One contract correction for T2 is authorised; no push, merge, "
    "release or deploy."
)

#: The approved tests the coordinator already committed. They are RED at the
#: pinned base because the two check modules do not exist yet.
APPROVED_TESTS = {
    "tools/acdd-lint/tests/test_check_source_rev.py": '''"""Approved test: a contract whose source_rev is not a full sha must be refused."""

from __future__ import annotations

import unittest

from acdd_lint.checks_source_rev import check_source_rev

SHA = "0123456789abcdef0123456789abcdef01234567"


def contract(source_rev, contract_id="T1-source-rev"):
    return {"contract_version": "acdd/1", "id": contract_id, "source_rev": source_rev}


class SourceRev(unittest.TestCase):
    def test_malformed_source_rev_is_refused(self):
        for value in ("", "abc", "0123456789abcdef0123456789abcdef0123456", SHA[:-1], "g" * 40, " " + SHA, None, 42):
            with self.subTest(source_rev=value):
                problems = check_source_rev(contract(value))
                self.assertEqual(len(problems), 1, problems)
                problem = problems[0]
                self.assertEqual(set(problem), {"code", "where", "message"})
                self.assertEqual(problem["code"], "contract_source_rev_malformed")
                self.assertEqual(problem["where"], "T1-source-rev")
                self.assertIn("source_rev", problem["message"])

    def test_valid_source_rev_is_silent(self):
        self.assertEqual(check_source_rev(contract(SHA)), [])
        self.assertEqual(check_source_rev(contract(SHA.upper())), [])

    def test_non_mapping_is_silent(self):
        self.assertEqual(check_source_rev(None), [])
        self.assertEqual(check_source_rev([]), [])


if __name__ == "__main__":
    unittest.main()
''',
    "tools/acdd-lint/tests/test_check_reachability.py": '''"""Approved test: source_rev must be reachable from the repository head."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from acdd_lint.checks_reachability import check_reachability


def git(repo, *args):
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    if done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done.stdout.strip()


def contract(source_rev, contract_id="T2-reachability"):
    return {"contract_version": "acdd/1", "id": contract_id, "source_rev": source_rev}


class Reachability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def test_unreachable_commit_is_refused(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        (self.repo / "side.txt").write_text("side\\n")
        git(self.repo, "add", "side.txt")
        git(self.repo, "commit", "-q", "-m", "side")
        side = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")

        result = check_reachability(contract(side), repo=self.repo)

        self.assertTrue(result["checked"])
        self.assertEqual(len(result["problems"]), 1, result)
        self.assertEqual(result["problems"][0]["code"], "contract_source_rev_unreachable")
        self.assertEqual(result["problems"][0]["where"], "T2-reachability")

    def test_reachable_commit_is_silent(self):
        result = check_reachability(contract(self.base), repo=self.repo)

        self.assertTrue(result["checked"])
        self.assertEqual(result["problems"], [])

    def test_absent_commit_is_refused(self):
        result = check_reachability(contract("e" * 40), repo=self.repo)

        self.assertTrue(result["checked"])
        self.assertEqual([p["code"] for p in result["problems"]], ["contract_source_rev_unreachable"])

    def test_no_repository_is_explicitly_not_checked(self):
        result = check_reachability(contract(self.base), repo=None)

        self.assertFalse(result["checked"])
        self.assertEqual(result["problems"], [])
        self.assertIn("not checked", result["reason"])


if __name__ == "__main__":
    unittest.main()
''',
}


def git(args, cwd, check=True):
    done = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
             # A fixed clock makes the fixture commit (and therefore every
             # contract hash that pins it) reproducible across runs.
             "GIT_AUTHOR_DATE": fx.APPROVED_AT, "GIT_COMMITTER_DATE": fx.APPROVED_AT},
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {args} in {cwd}: {done.stderr}")
    return done


#: The acdd-lint tree is taken from the pinned protocol commit named in the
#: objective, even when the checkout has advanced past it.
ACDD_LINT_AT = "cf7786d"


def run_suite(cwd, tests=("tools/acdd-lint/tests",)):
    return subprocess.run([PY, "-m", "pytest", *tests, "-q"], cwd=str(cwd),
                          capture_output=True, text=True)


class DeterministicAgent:
    """The one decision the envelope pre-authorises, reached without judgement.

    It reads the durable request and the frozen contract it names, and returns
    the single correction the approval already allows: replace the unusable
    selector with the approved fixture method. No clock, no randomness, no
    widening: the same request always yields the same decision.
    """

    def __init__(self, state_dir, replacement, decided_at=DECIDED_AT):
        self.state_dir = Path(state_dir)
        self.replacement = replacement
        self.decided_at = decided_at

    def __call__(self, request):
        job = jobs.load_job(self.state_dir, str(request["job_id"]), recover=False)
        task = next(task for task in job["tasks"] if task["task_id"] == request["task_id"])
        new_id = str(request["contract"]["id"])
        corrected = recovery.derive_superseding_contract(
            task, [self.replacement], adapter="deterministic-agent",
            decided_at=self.decided_at, contract_id=new_id,
        )
        new_sha = contract_mod.check_frozen(corrected)
        return {
            "schema": "rizzstack.recovery/1",
            "request_id": str(request["request_id"]),
            "request_digest": str(request["request_digest"]),
            "adapter": "deterministic-agent",
            "decision": "supersede",
            "corrected_tests": [self.replacement],
            "supersedes": {
                "contract_id": str(request["contract"]["id"]),
                "contract_sha256": str(request["contract"]["sha256"]),
                "new_contract_id": new_id,
                "new_contract_sha256": new_sha,
            },
            "rationale": (
                "The frozen contract names a selector that does not exist at the pinned revision, so the "
                "acceptance selection is unusable rather than the patch. The approved fixture method selects "
                "the same behaviour the criterion states, so one supersession inside the envelope is enough."
            ),
            "decided_at": self.decided_at,
        }


class M4Dogfood(unittest.TestCase):
    maxDiff = None

    # ------------------------------------------------------------------ setup
    def setUp(self):
        self._require_components()
        sys.path.insert(0, HYVV_SRC)
        self.addCleanup(lambda: sys.path.remove(HYVV_SRC))
        self.hyvv = importlib.import_module("provisioning")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.host = socket.gethostname()

        self.workflow_ref = fx.workflow_ref()
        self.assertEqual(len(self.workflow_ref), 40)

        # ---- the real acdd-lint tree, plus the two approved tests -----------
        self.work = self.tmp / "work"
        (self.work / "tools").mkdir(parents=True)
        self._extract_acdd_lint()
        (self.work / "schemas").mkdir()
        for name in ("acdd-contract.schema.json", "tdd-evidence.schema.json"):
            shutil.copy2(Path(WORKFLOW_SRC) / "schemas" / name, self.work / "schemas" / name)
        for relpath, body in APPROVED_TESTS.items():
            (self.work / relpath).write_text(body, encoding="utf-8")
        (self.work / "README.md").write_text("acdd-lint at the pinned protocol revision\n")
        git(["init", "-q", "-b", "main"], self.work)
        git(["add", "--", "README.md", "schemas", "tools"], self.work)
        git(["commit", "-q", "-m", "acdd-lint tree at cf7786d with the approved source-rev and reachability tests"], self.work)
        self.base = git(["rev-parse", "HEAD"], self.work).stdout.strip()

        # The baseline is honestly RED for both approved selectors and green for
        # the pre-existing acdd-lint suite. The approved files are RED because
        # the two check modules do not exist yet.
        suite = self._baseline_suite()
        self.assertEqual(suite.returncode, 0, "the untouched acdd-lint suite must pass at the base:\n"
                         + suite.stdout + suite.stderr)
        for selector in (T1_TEST, T2_GOOD_TEST):
            red = run_suite(self.work, (selector,))
            self.assertNotEqual(red.returncode, 0, f"{selector} must be RED at the pinned base")
        bad = run_suite(self.work, (T2_BAD_TEST,))
        self.assertNotEqual(bad.returncode, 0, "T2's injected selector must not exist")

        self.origin = self.tmp / "origin.git"
        git(["init", "-q", "--bare", str(self.origin)], self.tmp)
        git(["push", "-q", str(self.origin), f"{self.base}:refs/heads/main"], self.work)
        self.mirror = self.tmp / "mirror.git"
        git(["init", "-q", "--bare", str(self.mirror)], self.tmp)
        git(["push", "-q", str(self.mirror), f"{self.base}:refs/heads/main"], self.work)

        # ---- the deterministic worker's genuine patches ---------------------
        self.patches = m4_worker.build_patches(self.work, self.base, self.tmp)
        self.assertEqual(sorted(m4_worker.patch_files(self.patches[PROJECT_1])), [MODULE_1])
        self.assertEqual(sorted(m4_worker.patch_files(self.patches[PROJECT_2])), [MODULE_2])
        self.worker = m4_worker.DeterministicWorker(self.patches)

        # ---- two separate frozen ACDD contracts -----------------------------
        self.contract_1 = fx.acdd_contract(
            contract_id=CONTRACT_1, criterion_id="AC-001",
            statement="a contract whose source_rev is not a full 40-hex sha is refused",
            assigned_file=MODULE_1, test_file=TEST_1, test_id=T1_TEST, source_rev=self.base)
        self.contract_2 = fx.acdd_contract(
            contract_id=CONTRACT_2, criterion_id="AC-001",
            statement="a contract whose source_rev is reachable from the repository head is accepted without a problem",
            assigned_file=MODULE_2, test_file=TEST_2, test_id=T2_BAD_TEST, source_rev=self.base)
        self.frozen_1 = contract_mod.freeze_contract(self.contract_1, approved_by="reviewer", approved_at=fx.APPROVED_AT)
        self.frozen_2 = contract_mod.freeze_contract(self.contract_2, approved_by="reviewer", approved_at=fx.APPROVED_AT)
        self.sha_1 = contract_mod.check_frozen(self.frozen_1)
        self.sha_2 = contract_mod.check_frozen(self.frozen_2)

        # ---- authorisation: one envelope, written before dispatch -----------
        self.state = self.tmp / "state"
        self.state.mkdir()
        # A glob scope is the approved intent; production compares provisioning
        # scopes literally, so the job narrows it to the four exact files.
        for relpath in (MODULE_1, TEST_1, MODULE_2, TEST_2):
            self.assertTrue(fnmatch.fnmatch(relpath, "tools/acdd-lint/**"))
        self.authorization = jobs.record_authorization(
            self.state, job_id=JOB, statement=STATEMENT, repo=REPO, base_rev=self.base,
            scope=[MODULE_1, TEST_1, MODULE_2, TEST_2], bees=4, wall_clock_s=5400,
            gates=GATES, require_tty=False, interactive=False, by="reviewer",
            recovery={
                "max_attempts_per_task": 3,
                "contract_corrections_allowed": 1,
                "corrected_tests_allowed": list(T2_CANDIDATES),
                "file_scope": [MODULE_1, TEST_1, MODULE_2, TEST_2],
                "test_scope": [TEST_1, TEST_2],
                "gates": list(GATES),
            })

        self.packet = fx.exact_packet(
            contract=self.frozen_1, contract_sha256=self.sha_1, workflow_ref=self.workflow_ref,
            feature_id=FEATURE, job_id=JOB, read_only=(TEST_1,),
            summary="contract source-revision validation for acdd-lint",
            provenance="test:m4-dogfood", budget=(4, 5400))
        self.packet["repository"]["identity"] = REPO
        self.digest = packets.validate(self.packet)

        jobs.create_job(
            self.state, job_id=JOB, summary=STATEMENT, workflow_ref=self.workflow_ref, packet=self.packet,
            tasks=[
                {
                    "task_id": T1, "objective": "Refuse a source_rev that is not a full 40-hex sha",
                    "lease": [MODULE_1],
                    "acceptance": {"criteria": ["a malformed source_rev is refused with contract_source_rev_malformed"],
                                   "tests": [T1_TEST], "commands": ["python -m pytest tools/acdd-lint/tests -q"]},
                    "contract": self.frozen_1, "contract_sha256": self.sha_1,
                    "provision": {"repo": REPO, "source_rev": self.base, "host": self.host,
                                  "profile": PROFILE, "project": PROJECT_1, "contract_sha256": self.sha_1},
                },
                {
                    "task_id": T2, "objective": "Refuse a source_rev unreachable from the repository head",
                    "lease": [MODULE_2],
                    "acceptance": {"criteria": ["an unreachable source_rev is refused, and no repository is explicitly not checked"],
                                   "tests": [T2_BAD_TEST], "commands": ["python -m pytest tools/acdd-lint/tests -q"]},
                    "contract": self.frozen_2, "contract_sha256": self.sha_2,
                    "provision": {"repo": REPO, "source_rev": self.base, "host": self.host,
                                  "profile": PROFILE, "project": PROJECT_2, "contract_sha256": self.sha_2},
                },
            ])

        job = jobs.load_job(self.state, JOB, recover=False)
        job["feature_id"] = FEATURE
        job["injected_failures"] = [{
            "task_id": T2, "class": "specification", "declared": True,
            "detail": (f"The first frozen contract for {T2} names {T2_BAD_TEST}, which does not exist at "
                       f"{self.base[:12]}; the approved fixture method is {T2_GOOD_TEST}. The worker's patch is "
                       "genuine and the selector is unusable, so the verifier must reject it."),
        }]
        job["recovery_policy"] = {"max_attempts_per_task": 3, "contract_corrections_allowed": True}
        job["tasks"][0]["recovery_policy"] = {"max_attempts_per_task": 2, "contract_corrections_allowed": False}
        job["tasks"][1]["recovery_policy"] = {"max_attempts_per_task": 3, "contract_corrections_allowed": True,
                                              "corrected_tests_allowed": list(T2_CANDIDATES)}
        jobs.save_job(self.state, job)

        self.queue_file = self.state / "queue.json"
        self.queue_file.write_text(json.dumps({FEATURE: {"repo": REPO, "job_id": JOB, "status": "queued"}}))

        # ---- a real provisioning host over HTTP, serving the mirror ---------
        fixtures_dir = self.tmp / "fixtures"
        fixtures_dir.mkdir()
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({"schema": "rizzstack.provisioning/1", "profiles": {PROFILE: {
            "repo_patterns": [REPO], "workspace_root": str(fixtures_dir),
            "mirrors": {REPO: f"file://{self.mirror}"}}}}))
        config = self.tmp / "config.json"
        config.write_text(json.dumps({"projects": {}}))
        self.real = self.hyvv.Provisioner(
            policy=self.hyvv.load_policy(policy),
            journal=self.hyvv.Journal(self.tmp / "journal"),
            registry=self.hyvv.RegistryStore(config, cache={}),
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

        self.provisioner = P.HyvvProvisioner(self._transport, expect_host=self.host)
        self.q = queue.JsonQueue(self.queue_file)

    def _extract_acdd_lint(self):
        """The acdd-lint tree exactly as of the pinned commit, or the checkout."""

        target = self.work
        archive = subprocess.run(
            ["git", "-C", WORKFLOW_SRC, "archive", "--format=tar", ACDD_LINT_AT, "tools/acdd-lint"],
            capture_output=True)
        if archive.returncode == 0 and archive.stdout:
            subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
            return
        shutil.copytree(Path(WORKFLOW_SRC) / "tools" / "acdd-lint", target / "acdd-lint",
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))

    def _baseline_suite(self):
        return subprocess.run(
            [PY, "-m", "pytest", "tools/acdd-lint/tests", "-q",
             "--ignore=tools/acdd-lint/tests/test_check_source_rev.py",
             "--ignore=tools/acdd-lint/tests/test_check_reachability.py"],
            cwd=str(self.work), capture_output=True, text=True)

    def _require_components(self):
        missing = []
        if not WORKFLOW_SRC or not (Path(WORKFLOW_SRC) / "tools" / "feature_packet.py").exists():
            missing.append(f"WORKFLOW_SRC={WORKFLOW_SRC!r} (a pinned workflow checkout)")
        if not HYVV_SRC or not (Path(HYVV_SRC) / "provisioning.py").exists():
            missing.append(f"HYVV_SRC={HYVV_SRC!r} (the pinned hyvv provisioning checkout)")
        if not Path(GITLEAKS).exists():
            missing.append(f"GITLEAKS={GITLEAKS!r}")
        if missing:
            self.fail("The M4 dogfood proof fails, never skips: missing " + "; ".join(missing)
                      + ". A run without the pinned protocol, the real provisioner or gitleaks would "
                        "prove nothing about the production loop.")

    # ------------------------------------------------------------- plumbing
    def _transport(self, method, path, body):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def tick(self, job_id=JOB):
        """One mechanical tick, with the per-attempt regression suite scoped.

        A patch is verified against its own contract's approved test file: T1
        and T2 are independent files, and running T2's not-yet-written test
        against T1's patch would reject every first patch for the other task's
        absence. The *combined* suite (both files) runs at integration, which is
        what proves the two accepted patches combine.
        """

        job = jobs.load_job(self.state, job_id, recover=False)
        in_flight = [task for task in job["tasks"]
                     if task.get("state") in {"dispatched", "running", "verifying"} and task.get("attempts")]
        suite = SUITE
        if in_flight:
            files = ((in_flight[0].get("contract") or {}).get("scope") or {}).get("test_files") or []
            if files:
                suite = [PY, "-m", "pytest", *files, "-q"]
        return job_cli.tick(self.state, job_id, client=self.worker, repo=self.work,
                            provisioner=self.provisioner, expect_host=self.host, gitleaks=GITLEAKS,
                            suite_command=suite)

    def run_once(self, adapter=None):
        return queue.run_once(self.state, queue.JsonQueue(self.queue_file),
                              tick=self.tick, handoff_context=None)

    def job(self):
        return jobs.load_job(self.state, JOB, recover=False)

    def task(self, task_id):
        return next(task for task in self.job()["tasks"] if task["task_id"] == task_id)

    def events(self, task_id=None):
        found = self.job().get("events") or []
        return [event for event in found if task_id is None or event.get("task_id") == task_id]

    # ---------------------------------------------------------------- proof
    def test_one_authorised_objective_recovers_the_injected_failure(self):
        log = []
        recovered = False
        for _ in range(40):
            try:
                result = self.run_once()
            except Exception as exc:  # noqa: BLE001 - named refusals are the finding
                code = str(getattr(exc, "code", ""))
                if code == "supersede_binding_conflict":
                    self.fail("the corrective attempt cannot follow the supersession: task T2 is still bound to "
                              "a provisioning receipt that pins the superseded contract. Production must "
                              "re-provision the corrected contract (clear the stale binding/provisioning record) "
                              "before dispatching the corrective attempt, or the M4 loop cannot close.")
                state = [{key: task.get(key) for key in ("task_id", "state", "attempts", "binding", "provisioning")}
                         for task in self.job()["tasks"]]
                self.fail(f"the loop raised {type(exc).__name__}: {exc}; tasks: {json.dumps(state, default=str)}")
            log.append(result)
            if result.get("action") in {"blocked", "failed", "needs_approval"}:
                self.fail(f"the loop stopped before the objective completed: {json.dumps(result, default=str)}; "
                          f"steps: {json.dumps(log, default=str)}")
            job = self.job()
            t2 = next(task for task in job["tasks"] if task["task_id"] == T2)
            if t2["state"] == "rejected" and not recovered:
                self.assertEqual(len(t2.get("attempts") or []), 1, "T2 must be rejected on its first attempt")
                recorded = recovery.list_requests(self.state, JOB)
                self.assertEqual(len(recorded), 1,
                                 f"the rejection must leave exactly one durable request: {recorded}")
                self.request = recorded[0]
                self.assert_failure_is_specification(self.request, t2)
                self.recover()
                recovered = True
            if result.get("action") == "ready_for_review":
                break
        else:
            job = self.job()
            snapshot = {
                "tasks": [{key: task.get(key) for key in ("task_id", "state", "attempts", "evidence")}
                          for task in job["tasks"]],
                "integration": job.get("integration"),
                "approval_gate": job.get("approval_gate"),
                "escalations": job.get("escalations"),
                "recovery": job.get("recovery"),
                "requests": [{key: request.get(key) for key in ("request_id", "task_id", "failure", "supersession_permitted")}
                             for request in recovery.list_requests(self.state, JOB)],
                "decisions": [{key: decision.get(key) for key in ("request_id", "decision", "adapter")}
                              for decision in recovery.list_decisions(self.state, JOB)],
                "worker_posts": [brief.get("task_id") for brief in self.worker.posts],
                "dispatched": [{"task": brief.get("task_id"),
                                "contract": (brief.get("contract") or {}).get("id"),
                                "acceptance_tests": (brief.get("acceptance") or {}).get("tests")}
                               for brief in self.worker.posts],
            }
            self.fail(f"the objective never reached ready_for_review: {json.dumps(log, default=str)}; "
                      f"job: {json.dumps(snapshot, default=str)}")

        self.assertTrue(recovered, "the injected failure never surfaced as a rejection")
        job = self.job()
        self.assertEqual([task["state"] for task in job["tasks"]], ["accepted", "accepted"],
                         json.dumps([{t["task_id"]: t["state"] for t in job["tasks"]}], default=str))
        self.assert_three_attempts_and_one_rejection(job)
        self.assert_supersession_chain(job)
        self.assert_integrated_tree(job)
        self.assert_return_packet(job)
        self.assert_no_duplicates_after_restart_and_extra_ticks()
        self.assert_zero_human_interventions(job)
        self.assert_no_publication()
        integrated = git(["rev-parse", integrate_job.integration_ref(JOB)], self.work).stdout.strip()
        print("M4_EVIDENCE " + json.dumps({
            "base_rev": self.base,
            "workflow_ref": self.workflow_ref,
            "authorization": {"bees": 4, "wall_clock_s": 5400, "scope": ["tools/acdd-lint/**"],
                              "gates": list(GATES), "push": False},
            "contracts": {CONTRACT_1: self.sha_1, CONTRACT_2: self.sha_2},
            "superseding_contract": {
                "id": self.task(T2)["contract"]["id"],
                "sha256": self.task(T2)["contract_sha256"],
                "supersedes": self.task(T2)["contract"]["supersedes"],
                "supersedes_sha256": self.task(T2)["contract"]["supersedes_sha256"],
            },
            "request_id": self.request["request_id"],
            "request_digest": self.request["request_digest"],
            "attempts": [attempt for task in job["tasks"] for attempt in task["attempts"]],
            "integrated_commit": integrated,
            "return": str(self.state / "jobs" / JOB / "return.json"),
            "provisioning_posts": self.host_posts,
        }, sort_keys=True))

    def assert_failure_is_specification(self, request, task):
        failure = request.get("failure") or {}
        evidence = str(failure.get("evidence") or "")
        self.assertEqual(failure.get("class"), "specification",
                         f"the verifier recorded {evidence!r} for the injected selector {T2_BAD_TEST}; "
                         f"classify_failure returned class={failure.get('class')!r} code={failure.get('code')!r}. "
                         "A rejection whose named test cannot be collected is a specification failure, not an "
                         "implementation failure: no patch can make a nonexistent selector pass.")
        self.assertIn(failure.get("code"), recovery.SELECTION_CODES,
                      f"a specification request must carry an unusable-acceptance code, got {failure.get('code')!r}")
        self.assertTrue(request.get("supersession_permitted"),
                        f"the envelope explicitly allows one T2 correction, yet the request forbids it: {request}")
        self.assertEqual(request.get("contract"), {"id": CONTRACT_2, "sha256": self.sha_2},
                         "the request must name the exact frozen contract that failed")
        self.assertEqual(request.get("attempt_id"), (task.get("attempts") or [""])[-1])

    def recover(self):
        signature = None
        try:
            import inspect

            signature = inspect.signature(job_cli.recover)
        except (TypeError, ValueError):
            signature = None
        if signature is None or "decision_file" not in signature.parameters or "adapter" not in signature.parameters:
            self.fail(f"job_cli.recover must expose the M4 signature "
                      f"(state_dir, job_id, *, decision_file=None, adapter=None); found {signature}")
        agent = DeterministicAgent(self.state, T2_GOOD_TEST)
        result = job_cli.recover(self.state, JOB, self.work, adapter=agent)
        self.assertTrue(result.get("decisions_recorded"),
                        f"the validated deterministic decision was not recorded: {json.dumps(result, default=str)}")
        return result

    def assert_three_attempts_and_one_rejection(self, job):
        attempts = [attempt for task in job["tasks"] for attempt in task.get("attempts") or []]
        self.assertEqual(attempts, ["att_m4_1", "att_m4_2", "att_m4_3"], f"worker attempts: {attempts}")
        self.assertEqual(len(self.worker.posts), 3,
                         f"the worker must be dispatched exactly three times, got {len(self.worker.posts)}")
        # Two accepted patches, one preserved rejection.
        self.assertEqual(len(job["integration"].get("accepted_waves") or []), 1)
        wave = json.loads((self.state / "waves" / f"{JOB}.w1.json").read_text())
        self.assertTrue(wave.get("accepted"))
        self.assertEqual(sorted(wave.get("attempts") or []), ["att_m4_1", "att_m4_3"],
                         "the accepted wave must integrate the two accepted attempts only")
        self.assertIsNotNone(verify_local.read_record(self.state, "att_m4_1"))
        self.assertIsNone(verify_local.read_record(self.state, "att_m4_2"),
                          "the rejected attempt must have no verification record and no commit")
        self.assertIsNotNone(verify_local.read_record(self.state, "att_m4_3"))
        rejected = [event for event in self.events(T2) if event.get("transition", "").endswith("->rejected")]
        self.assertEqual(len(rejected), 1, f"exactly one durable rejection, got {rejected}")
        self.assertIn("no tests ran", str(rejected[0].get("reason") or "") + str(self.task(T2).get("evidence") or ""),
                      "the rejection evidence must be the verifier's own record")
        # The rejected attempt's patch was genuine and identical in kind to the
        # accepted one: the correction changed the contract, never the worker.
        self.assertIn("checks_reachability", self.patches[PROJECT_2])
        self.assertEqual(self.worker.artifact("att_m4_2")["patch"], self.worker.artifact("att_m4_3")["patch"])

    def assert_supersession_chain(self, job):
        requests = recovery.list_requests(self.state, JOB)
        decisions = recovery.list_decisions(self.state, JOB)
        self.assertEqual(len(requests), 1, f"exactly one request, got {requests}")
        self.assertEqual(len(decisions), 1, f"exactly one decision, got {decisions}")
        request, decision = requests[0], decisions[0]
        self.assertEqual(decision.get("request_id"), request.get("request_id"))
        self.assertEqual(decision.get("request_digest"), request.get("request_digest"))
        self.assertEqual(decision.get("decision"), "supersede")

        # M4.1 conformance: the records the runtime emitted and consumed are
        # validated, unchanged, by the pinned workflow protocol itself.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wf_recovery_protocol", Path(WORKFLOW_SRC) / "tools" / "recovery.py")
        wf_recovery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wf_recovery)
        validated = wf_recovery.validate_pair(
            recovery.load_request(self.state, str(request["request_id"])),
            recovery.read_decision(self.state, request),
            attempts_used=1, corrections_used=0)
        self.assertEqual(validated["request"]["failure"]["code"], "test_not_found")
        self.assertNotIn("number", request)
        self.assertIsInstance(request["envelope"]["contract_corrections_allowed"], int)
        self.assertEqual(decision["schema"], "rizzstack.recovery/1")
        self.assertNotIn("decision_digest", decision)
        self.assertNotIn("decision_digest", decisions[0])
        self.assertEqual(decision["supersedes"]["new_contract_id"],
                         decision["supersedes"]["contract_id"])

        task = self.task(T2)
        supersession = task.get("contract_supersession") or {}
        corrected = task.get("contract") or {}
        self.assertTrue(supersession, "the superseding contract was not recorded on the task")
        self.assertEqual(supersession.get("supersedes_sha256"), self.sha_2,
                         "the supersession must name the exact frozen contract it replaced")
        self.assertEqual(supersession.get("supersedes"), CONTRACT_2)
        self.assertEqual(corrected.get("supersedes"), CONTRACT_2)
        self.assertEqual(corrected.get("supersedes_sha256"), self.sha_2)
        self.assertEqual(contract_mod.check_frozen(corrected), task.get("contract_sha256"))
        self.assertNotEqual(task.get("contract_sha256"), self.sha_2)
        self.assertEqual(supersession.get("contract_sha256"), task.get("contract_sha256"))
        self.assertEqual((corrected.get("acceptance_criteria") or [{}])[0].get("tests"), [T2_GOOD_TEST],
                         "the correction must replace the unusable selector with the approved fixture method")
        self.assertEqual(corrected.get("scope"), self.contract_2["scope"],
                         "a correction may not widen or change the lease")
        self.assertEqual(corrected.get("id"), "T2-REACHABILITY")
        # The decision named the same pair the task now carries.
        supersedes = decision.get("supersedes") or {}
        self.assertEqual(supersedes.get("contract_sha256"), self.sha_2)
        self.assertEqual(supersedes.get("new_contract_sha256"), task.get("contract_sha256"))
        # The corrective dispatch ran under the corrected contract.
        third = self.worker.posts[2]
        self.assertEqual((third.get("contract") or {}).get("id"), "T2-REACHABILITY")
        self.assertEqual((third.get("contract") or {}).get("acceptance_criteria", [{}])[0].get("tests"), [T2_GOOD_TEST])
        self.assertEqual(third.get("source_rev"), self.base)
        # One supersession chain, recorded on the job too.
        chain = (job.get("recovery") or {}).get("supersessions") or []
        self.assertEqual(len(chain), 1, f"exactly one supersession, got {chain}")
        self.assertEqual(chain[0].get("request_id"), request.get("request_id"))
        # The original packet is preserved byte for byte.
        self.assertEqual((self.state / "packets" / f"{JOB}.json").read_bytes(), fx.canonical_bytes(self.packet))
        self.assertEqual(job["goal"]["packet_digest"], self.digest)

    def assert_integrated_tree(self, job):
        """A fresh clone at the integrated commit passes the combined suite."""

        commit = git(["rev-parse", integrate_job.integration_ref(JOB)], self.work).stdout.strip()
        self.assertEqual(len(commit), 40)
        for task in job["tasks"]:
            verified = verify_local.read_record(self.state, (task.get("attempts") or [""])[-1])
            self.assertIsNotNone(verified)
            self.assertEqual(git(["merge-base", "--is-ancestor", verified["sha"], commit], self.work,
                                 check=False).returncode, 0,
                             f"{task['task_id']}'s verified commit is not an ancestor of the integrated head")
        final = self.tmp / "final"
        git(["clone", "-q", str(self.work), str(final)], self.tmp)
        git(["checkout", "-q", commit], final)
        for relpath in (MODULE_1, MODULE_2):
            self.assertTrue((final / relpath).is_file(), f"{relpath} is missing from the integrated tree")
        self.assertIn("contract_source_rev_malformed", (final / MODULE_1).read_text())
        self.assertIn("contract_source_rev_unreachable", (final / MODULE_2).read_text())
        suite = subprocess.run([PY, "-m", "pytest", "tools/acdd-lint/tests", "-q"], cwd=str(final),
                               capture_output=True, text=True)
        self.assertEqual(suite.returncode, 0, "the combined suite must pass at the integrated commit:\n"
                         + suite.stdout + suite.stderr)
        self.assertIn("63 passed", suite.stdout, suite.stdout)
        return commit

    def assert_return_packet(self, job):
        result = job_cli.report(self.state, JOB, repo=self.work)
        manifest = json.loads((self.state / "jobs" / JOB / "return.json").read_text())
        self.assertEqual(manifest["schema"], "rizzstack.return/1")
        self.assertEqual(manifest["receiver_decision"], {"status": "pending", "decided_by": None,
                                                         "decided_at": None, "note": None})
        statuses = {row["id"]: row["status"] for row in manifest["acceptance"]}
        self.assertTrue(statuses and all(value == "PASS" for value in statuses.values()), statuses)
        named = [row["attempt_id"] for row in manifest["attempts"]]
        self.assertEqual(named, ["att_m4_1", "att_m4_2", "att_m4_3"],
                         f"the RETURN must name all three attempts, got {named}")
        markdown = (self.state / "jobs" / JOB / "RETURN.md").read_text()
        for attempt_id in named:
            self.assertIn(attempt_id, markdown)
        recovery_rows = manifest.get("recovery") or []
        self.assertEqual(len(recovery_rows), 1, f"the RETURN must carry the one recovery: {recovery_rows}")
        row = recovery_rows[0]
        self.assertEqual((row.get("failure") or {}).get("class"), "specification")
        self.assertEqual((row.get("decision") or {}).get("decision"), "supersede")
        self.assertTrue(row.get("applied"))
        supersedes = row.get("supersedes") or {}
        self.assertEqual(supersedes.get("contract_sha256"), self.sha_2)
        self.assertEqual(supersedes.get("supersedes_sha256"), self.task(T2).get("contract_sha256"))
        # Regenerating from the same durable snapshot is byte-stable and verifies.
        again = job_cli.report(self.state, JOB, repo=self.work)
        self.assertEqual(again["return"]["evidence_digest"], result["return"]["evidence_digest"])
        self.assertTrue(return_report.verify(self.state, JOB, repo=self.work)["ok"])
        self.assertFalse((self.state / "jobs" / JOB / "approvals").exists(),
                         "the loop must not resolve a human gate")

    def assert_no_duplicates_after_restart_and_extra_ticks(self):
        before = [attempt for task in self.job()["tasks"] for attempt in task.get("attempts") or []]
        for _ in range(3):
            self.run_once()
        # A restart: fresh queue and job objects, re-read from disk, and a second
        # recovery call with the same deterministic agent.
        self.q = queue.JsonQueue(self.queue_file)
        job_cli.recover(self.state, JOB, self.work, adapter=DeterministicAgent(self.state, T2_GOOD_TEST))
        self.tick()
        after = [attempt for task in self.job()["tasks"] for attempt in task.get("attempts") or []]
        self.assertEqual(after, before, f"an extra tick or restart duplicated an attempt: {before} -> {after}")
        self.assertEqual(len(self.worker.posts), 3)
        self.assertEqual(len(recovery.list_requests(self.state, JOB)), 1)
        self.assertEqual(len(recovery.list_decisions(self.state, JOB)), 1)
        self.assertEqual(len((self.job().get("recovery") or {}).get("supersessions") or []), 1)
        self.assertEqual(self.q.get(FEATURE).status, "ready_for_review")

    def assert_zero_human_interventions(self, job):
        actors = {str(event.get("actor") or "") for event in job.get("events") or []}
        self.assertEqual(actors, {"reconciler", "recovery"},
                         f"a human turn appeared in the loop: {sorted(actors)}")
        self.assertEqual(job.get("approval_gate", {}).get("state"), "waiting",
                         "the positive run must stop at the gate, not resolve it")

    def assert_no_publication(self):
        # Nothing was pushed: the origin only ever had the base commit.
        refs = git(["ls-remote", str(self.origin)], self.tmp).stdout.split()
        self.assertEqual(refs[:2], [self.base, "refs/heads/main"])
        self.assertEqual(git(["remote"], self.work).stdout.strip(), "",
                         "the worktree is local and has no remote")

    # ------------------------------------------------------------ negatives
    def build_rejected_job(self, name, *, corrections: bool, max_attempts: int,
                           evidence: str, code_class: str = "implementation"):
        """A job whose one task is already rejected, with its durable request."""

        state = self.tmp / name
        state.mkdir()
        job_id = f"job_{name}"
        job_id = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in job_id)[:64]
        ceiling = {"max_attempts_per_task": max_attempts,
                   "contract_corrections_allowed": 1 if corrections else 0,
                   "gates": list(GATES)}
        if corrections:
            ceiling.update({"corrected_tests_allowed": [T2_GOOD_TEST],
                            "file_scope": [MODULE_2, TEST_2], "test_scope": [TEST_2]})
        jobs.record_authorization(
            state, job_id=job_id, statement="A bounded recovery negative fixture.", repo=REPO,
            base_rev=self.base, scope=[MODULE_1, TEST_1, MODULE_2, TEST_2], bees=4, wall_clock_s=5400,
            gates=GATES, require_tty=False, interactive=False, by="reviewer",
            recovery=ceiling)
        contract = self.frozen_2 if code_class == "specification" else contract_mod.freeze_contract(
            fx.acdd_contract(contract_id="NEG-IMPL", criterion_id="AC-001", statement="negative fixture",
                             assigned_file=MODULE_2, test_file=TEST_2, test_id=T2_GOOD_TEST,
                             source_rev=self.base),
            approved_by="reviewer", approved_at=fx.APPROVED_AT)
        sha = contract_mod.check_frozen(contract)
        jobs.create_job(state, job_id=job_id, tasks=[{
            "task_id": T2, "objective": "negative fixture", "lease": [MODULE_2],
            "acceptance": {"criteria": ["c"], "tests": [T2_GOOD_TEST], "commands": ["python -m pytest -q"]},
            "contract": contract, "contract_sha256": sha,
        }])
        job = jobs.load_job(state, job_id, recover=False)
        task = job["tasks"][0]
        task["recovery_policy"] = {"max_attempts_per_task": max_attempts,
                                   "contract_corrections_allowed": corrections}
        for to_state in ("dispatching", "dispatched", "running", "verifying", "rejected"):
            jobs.transition(job, T2, to_state, actor="reconciler", reason="negative fixture rejection")
        task["attempts"] = ["att_neg_1"]
        task["evidence"] = evidence
        jobs.save_job(state, job)
        request = recovery.build_request(
            state, job, task,
            failure={"ok": False, "evidence": evidence, "attempt_id": "att_neg_1"},
            authorization=jobs.load_authorization(state, job_id))
        return state, job_id, request

    def test_a_forged_failure_classification_is_refused(self):
        evidence = "tests_failed: named tests failed after the patch: no tests ran in 0.01s"
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.classify_failure({"class": "specification", "code": "tests_failed"}, contract=self.frozen_2)
        self.assertEqual(caught.exception.code, "classification_forged",
                         "a caller-declared class that disagrees with its evidence code must be refused")
        # And the evidence itself cannot be talked into a supersession.
        classified = recovery.classify_failure({"ok": False, "evidence": evidence}, contract=self.frozen_2)
        self.assertNotEqual(classified["class"], "specification",
                            f"a plain tests_failed rejection is not an unusable acceptance selection: {classified}")

    def test_a_contract_altered_after_freeze_cannot_be_superseded(self):
        state, job_id, request = self.build_rejected_job(
            "neg_altered", corrections=True, max_attempts=3,
            evidence="test_not_found: named tests failed after the patch: no tests ran in 0.01s",
            code_class="specification")
        job = jobs.load_job(state, job_id, recover=False)
        task = job["tasks"][0]
        task["contract"]["title"] = "edited after the review hash was taken"
        jobs.save_job(state, job)
        with self.assertRaises(contract_mod.ContractError) as caught:
            contract_mod.check_frozen(task["contract"])
        self.assertEqual(caught.exception.code, "contract_changed")
        agent = DeterministicAgent(state, T2_GOOD_TEST)
        with self.assertRaises((recovery.RecoveryError, contract_mod.ContractError)) as refusal:
            recovery.record_decision(state, request["request_id"], agent(request))
        self.assertIn(getattr(refusal.exception, "code", ""), {"contract_changed", "contract_not_frozen",
                                                               "supersede_mismatch", "request_forged"})
        self.assertEqual(recovery.list_decisions(state, job_id), [],
                         "a decision was recorded against an altered contract")

    def test_a_corrective_test_outside_the_envelope_is_refused(self):
        state, job_id, request = self.build_rejected_job(
            "neg_outside", corrections=True, max_attempts=3,
            evidence="test_not_found: named tests failed after the patch: no tests ran in 0.01s",
            code_class="specification")
        self.assertIn(T2_GOOD_TEST, request["envelope"]["corrected_tests_allowed"])
        outside = f"{TEST_1}::SourceRev::test_valid_source_rev_is_silent"
        decision = {
            "schema": "rizzstack.recovery/1",
            "request_id": request["request_id"],
            "request_digest": request["request_digest"],
            "adapter": "deterministic-agent",
            "decision": "supersede",
            "corrected_tests": [outside],
            "supersedes": {
                "contract_id": request["contract"]["id"],
                "contract_sha256": request["contract"]["sha256"],
                "new_contract_id": request["contract"]["id"],
                "new_contract_sha256": "0" * 64,
            },
            "rationale": "This correction reaches into a different test file, outside the approved envelope.",
            "decided_at": DECIDED_AT,
        }
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.record_decision(state, request["request_id"], decision, adapter="deterministic-agent")
        self.assertEqual(caught.exception.code, "corrected_test_unauthorized",
                         "a correction may only use the tests the envelope already allows")
        self.assertEqual(recovery.list_decisions(state, job_id), [])
        self.assertFalse(any((state / "recovery").rglob("*.decision.json")))

    def test_an_exhausted_budget_becomes_needs_approval(self):
        state, job_id, request = self.build_rejected_job(
            "neg_exhausted", corrections=False, max_attempts=1,
            evidence="tests_failed: named tests failed after the patch: assert 1 == 2")
        self.assertEqual(request["envelope"]["max_attempts_per_task"], 1)
        result = job_cli.tick(state, job_id, client=m4_worker.DeterministicWorker({}), repo=self.work,
                              gitleaks=GITLEAKS, suite_command=SUITE)
        self.assertIn(result["action"], {"needs_approval", "recovery"}, result)
        job = jobs.load_job(state, job_id, recover=False)
        self.assertEqual(job["approval_gate"]["state"], "awaiting_approval",
                         f"an exhausted attempt budget must reach the gate, not a success: {result}")
        task = job["tasks"][0]
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["attempts"], ["att_neg_1"], "no corrective attempt may be dispatched")
        self.assertEqual(recovery.list_decisions(state, job_id), [])

    def test_a_decision_without_evidence_is_refused(self):
        state, job_id, request = self.build_rejected_job(
            "neg_evidence", corrections=True, max_attempts=3,
            evidence="test_not_found: named tests failed after the patch: no tests ran in 0.01s",
            code_class="specification")
        bodies = {
            "no_rationale": {"decision": "escalate", "rationale": ""},
            "wrong_request_digest": {"decision": "escalate", "rationale": "a real reason",
                                      "request_digest": "0" * 64},
        }
        for name, body in bodies.items():
            with self.subTest(case=name):
                with self.assertRaises(recovery.RecoveryError) as caught:
                    recovery.record_decision(state, request["request_id"], body, adapter="deterministic-agent")
                self.assertIn(caught.exception.code,
                              {"decision_invalid", "decision_mismatch", "request_digest_mismatch"},
                              f"{name}: a decision without its request binding and a real rationale is "
                              f"not evidence (the protocol requires rationale minLength 8)")
        self.assertEqual(recovery.list_decisions(state, job_id), [],
                         "an unevidenced decision must never be persisted")

    def test_a_repeated_decision_is_refused_and_never_duplicated(self):
        # acceptance_uncovered is a supersede-eligible selection code, so this
        # exercises decision replay rather than the request's permission.
        state, job_id, request = self.build_rejected_job(
            "neg_repeat", corrections=True, max_attempts=3,
            evidence="acceptance_uncovered: the contract's acceptance selection cannot cover its tests",
            code_class="specification")
        self.assertTrue(request["supersession_permitted"], request)
        decision = DeterministicAgent(state, T2_GOOD_TEST)(request)
        first = recovery.record_decision(state, request["request_id"], decision, adapter="deterministic-agent")
        changed = dict(decision, rationale="a different decision for the same request, which must not win")
        for label, body in (("identical", decision), ("conflicting", changed)):
            with self.subTest(replay=label):
                with self.assertRaises(recovery.RecoveryError) as caught:
                    recovery.record_decision(state, request["request_id"], body, adapter="deterministic-agent")
                self.assertEqual(caught.exception.code, "decision_exists",
                                 "a request is decided exactly once; a replayed decision must never "
                                 "look like a second authorisation")
        decisions = recovery.list_decisions(state, job_id)
        self.assertEqual(len(decisions), 1, f"exactly one decision record, got {decisions}")
        self.assertEqual(recovery.decision_digest(decisions[0]), recovery.decision_digest(first),
                         "the recorded decision was overwritten by the replay")

    def test_a_supersession_bound_to_a_stale_receipt_is_refused(self):
        """A changed contract may never ride a receipt that pinned the old one."""

        state, job_id, request = self.build_rejected_job(
            "neg_binding", corrections=True, max_attempts=3,
            evidence="acceptance_uncovered: the contract's acceptance selection cannot cover its tests",
            code_class="specification")
        job = jobs.load_job(state, job_id, recover=False)
        task = job["tasks"][0]
        task["binding"] = {
            "repo": REPO, "source_rev": self.base, "host": self.host, "profile": PROFILE,
            "project": "neg-binding", "receipt_digest": "f" * 64,
            "contract_sha256": request["contract"]["sha256"],
        }
        jobs.save_job(state, job)
        recovery.record_decision(state, request["request_id"], DeterministicAgent(state, T2_GOOD_TEST)(request),
                                 adapter="deterministic-agent")
        job = jobs.load_job(state, job_id, recover=False)
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.apply_ready_decisions(state, job)
        self.assertEqual(caught.exception.code, "supersede_binding_conflict",
                         "a superseded task whose provisioning receipt pins the old contract must be "
                         "re-provisioned, never dispatched under the receipt it no longer matches")
        after = jobs.load_job(state, job_id, recover=False)["tasks"][0]
        self.assertEqual(after["state"], "rejected")
        self.assertEqual(after["attempts"], ["att_neg_1"])
        self.assertEqual(after.get("contract_supersession"), None)
        self.assertEqual((jobs.load_job(state, job_id, recover=False).get("recovery") or {}).get("supersessions") or [], [])

    def test_supersession_without_authorization_becomes_needs_approval(self):
        state, job_id, request = self.build_rejected_job(
            "neg_unauthorized", corrections=False, max_attempts=3,
            evidence="test_not_found: named tests failed after the patch: no tests ran in 0.01s",
            code_class="specification")
        self.assertFalse(request["supersession_permitted"],
                         "an envelope without contract corrections must not permit a supersession")
        decision = DeterministicAgent(state, T2_GOOD_TEST)(request)
        with self.assertRaises(recovery.RecoveryError) as caught:
            recovery.record_decision(state, request["request_id"], decision, adapter="deterministic-agent")
        self.assertIn(caught.exception.code, {"supersede_forbidden", "contract_correction_forbidden"},
                      "an envelope without contract corrections must refuse a supersession")
        # The mechanical adapter refuses to invent a correction; the tick stops at
        # the gate instead of dispatching.
        recorded = job_cli.recover(state, job_id, adapter="deterministic")
        self.assertTrue(recorded["decisions_recorded"])
        result = job_cli.tick(state, job_id, client=m4_worker.DeterministicWorker({}), repo=self.work,
                              gitleaks=GITLEAKS, suite_command=SUITE)
        self.assertIn(result["action"], {"needs_approval", "recovery"}, result)
        job = jobs.load_job(state, job_id, recover=False)
        self.assertEqual(job["approval_gate"]["state"], "awaiting_approval")
        self.assertEqual(job["tasks"][0]["state"], "failed")
        self.assertEqual(job["tasks"][0]["attempts"], ["att_neg_1"])
        self.assertEqual((job.get("recovery") or {}).get("supersessions") or [], [],
                         "no contract may be superseded without explicit authorization")


# ---------------------------------------------------------------------------
# Supervised real-model experiment (not part of ordinary CI)
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.environ.get("HERMES_M42_REAL") == "1",
                     "supervised real-model run: set HERMES_M42_REAL=1")
class M42SupervisedHermes(unittest.TestCase):
    """One real Hermes decision through the production pipeline.

    The deterministic fixture and adapter remain the CI control. Here the
    fixture's decision step is replaced by the real adapter; nothing else in
    the pipeline changes.
    """

    def setUp(self):
        self.fx = M4Dogfood(
            methodName="test_one_authorised_objective_recovers_the_injected_failure")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

        def recover():
            from bundlebees_plugin import hermes_adapter

            out = hermes_adapter.decide_with_hermes(
                self.fx.state, self.fx.request["request_id"], repo=self.fx.work,
                timeout_s=300)
            print("M42_DECISION " + json.dumps({
                "proposal": out.get("proposal"),
                "stdout_sha256": out.get("stdout_sha256"),
                "invocation": out.get("invocation"),
                "decision": {k: out.get("decision", {}).get(k)
                             for k in ("request_id", "adapter", "decision",
                                       "corrected_tests", "rationale", "decided_at")},
            }, default=str), flush=True)
            self.fx.assertEqual(out["decision"]["schema"], "rizzstack.recovery/1")
            return {"decisions_recorded": True, "decision": out["decision"]}

        self.fx.recover = recover

    def test_real_hermes_makes_the_recovery_decision(self):
        M4Dogfood.test_one_authorised_objective_recovers_the_injected_failure(self.fx)

    def test_real_hermes_escalates_or_is_refused_without_correction_authority(self):
        from bundlebees_plugin import hermes_adapter, recovery

        state, job_id, request = self.fx.build_rejected_job(
            "real_noauth", corrections=False, max_attempts=2,
            evidence="test_not_found: named tests failed after the patch: no tests ran in 0.01s",
            code_class="specification")
        posts_before = len(self.fx.worker.posts)
        try:
            out = hermes_adapter.decide_with_hermes(
                state, request["request_id"], repo=self.fx.work, timeout_s=300)
        except Exception as exc:  # a refused correction is an acceptable outcome
            code = str(getattr(exc, "code", "") or type(exc).__name__)
            print("M42_NOAUTH_REFUSED " + json.dumps({"code": code, "detail": str(exc)[:400]}),
                  flush=True)
            self.assertIsNone(recovery.read_decision(state, request))
        else:
            decision = out["decision"]
            print("M42_NOAUTH_DECISION " + json.dumps({
                "decision": decision.get("decision"),
                "rationale": decision.get("rationale"),
                "adapter": decision.get("adapter"),
            }, default=str), flush=True)
            self.assertEqual(decision.get("decision"), "escalate",
                             "supersession is not authorised; the model must escalate")
        self.assertEqual(len(self.fx.worker.posts), posts_before,
                         "no corrective dispatch may happen without sealed authority")


if __name__ == "__main__":
    unittest.main()
