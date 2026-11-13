"""P3: create-only publication at Git's update boundary, and crash recovery.

The P2 transport did ``ls-remote`` and then a plain ``git push``. That is a
check-then-push window: two coordinators (or one coordinator twice) could both
observe an absent ref and the second push would move it. These tests pin the
replacement behaviour:

* the ref is created with ``git push --force-with-lease=<ref>:`` — the update
  command carries the null object id as its expected old value, so Git's own
  update transaction refuses to touch a ref that already exists;
* two concurrent OS processes publishing the same accepted commit converge on
  one ref, and two publishing conflicting commits have exactly one winner and
  the ref never moves afterwards;
* ``handoff.materialize`` fires the ``transport.after_publish`` fault checkpoint
  immediately after the publication receipt is validated and before the source
  resolution is persisted, so a crash there is recoverable;
* a restart after such a crash reconciles the existing ref (no second
  publication), reuses the immutable resolution only when it still validates
  against the intent packet and accepted commit, reuses a matching derived
  authorization and job, and refuses conflicting records or a moved dependency
  source — the derived source revision never changes across a restart.

Everything runs against real temporary repositories and real bare Git mirrors;
the concurrency and crash cases are separate OS processes, not threads or mocks
of the transport.

Run:

    WORKFLOW_SRC=/tmp/verify/wf-packet REQUIRE_WORKFLOW=1 \\
    python -m pytest -q tests/test_mirror_concurrency.py
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from bundlebees_plugin import handoff, job as jobs, packets, transport, verify_local

WORKFLOW_SRC = os.environ.get("WORKFLOW_SRC", "")
REQUIRE_WORKFLOW = os.environ.get("REQUIRE_WORKFLOW") == "1"
HAS_WORKFLOW = bool(WORKFLOW_SRC) and Path(WORKFLOW_SRC, "tools", "feature_packet.py").exists()

REPO = "mojomast/smoke-real"
PKG_ROOT = Path(__file__).resolve().parents[1]


def _faults():
    try:
        return importlib.import_module("bundlebees_plugin.faults")
    except ImportError:
        return None


def git(args, cwd, check=True):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                               "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    if check and done.returncode != 0:
        raise AssertionError(f"git {args}: {done.stderr}")
    return done


def accepted_refs(mirror: Path) -> list[str]:
    """Every ``refs/bb/accepted/*`` line the mirror advertises, as ``<sha> <ref>``."""

    out = git(["for-each-ref", "--format=%(objectname) %(refname)"], mirror).stdout
    return [line.strip() for line in out.splitlines() if " refs/bb/accepted/" in line]


def resolution_digest(state: Path, job_id: str = "job_b") -> str:
    return hashlib.sha256(handoff.resolution_path(state, job_id).read_bytes()).hexdigest()


class Fixture:
    """A real repository, a real bare mirror, and one enrolled descendant policy."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir(parents=True, exist_ok=True)

        self.repo = tmp / "repo"
        self.repo.mkdir()
        git(["init", "-q", "-b", "main"], self.repo)
        (self.repo / "README.md").write_text("base\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "root"], self.repo)
        self.root_rev = git(["rev-parse", "HEAD"], self.repo).stdout.strip()

        (self.repo / "smoke").mkdir()
        (self.repo / "smoke" / "answer.py").write_text("def answer():\n    return 42\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "feature A"], self.repo)
        self.accepted = git(["rev-parse", "HEAD"], self.repo).stdout.strip()

        self.mirror = tmp / "mirror.git"
        git(["init", "-q", "--bare", str(self.mirror)], tmp)
        git(["push", "-q", str(self.mirror), f"{self.root_rev}:refs/heads/main"], self.repo)

        self._mark_verified("att_1", self.accepted, "smoke/answer.py")
        jobs.record_authorization(self.state, job_id="job_a", statement="Feature A: answer() == 42",
                                  repo=REPO, base_rev=self.root_rev, scope=["smoke/answer.py"], bees=1,
                                  wall_clock_s=1800, require_tty=False, interactive=False, by="reviewer")
        jobs.create_job(self.state, job_id="job_a", summary="Feature A",
                        tasks=[{"task_id": "TA", "objective": "answer", "lease": ["smoke/answer.py"]}])
        job_a = jobs.load_job(self.state, "job_a")
        job_a["integration"] = {"wave": "w1", "state": "accepted", "last_commit": self.accepted,
                                "accepted_waves": [{"wave": 1, "commit": self.accepted,
                                                    "attempts": ["att_1"]}]}
        jobs.save_job(self.state, job_a)

        head = git(["-C", WORKFLOW_SRC, "rev-parse", "HEAD"], tmp).stdout.strip()
        example = json.loads((Path(WORKFLOW_SRC) / "schemas" / "examples" / "feature-descendant.json")
                             .read_text(encoding="utf-8"))
        self.packet = copy.deepcopy(example)
        self.packet["workflow_ref"] = head
        self.packet["job_id"] = "job_b"
        self.packet["feature_id"] = "feature-b"
        self.packet["repository"]["identity"] = REPO
        self.packet["source_policy"]["root_revision"] = self.root_rev
        self.packet["source_policy"]["policy_id"] = "policy-b"
        self.packet["source_policy"]["dependencies"] = ["feature-a"]
        self.packet["contract_template"]["source_rev"] = self.root_rev
        self.packet["budget"] = {"bees": 1, "wall_clock_s": 1800}

        handoff.create_approval_key(self.state)
        handoff.enroll_policy(
            self.state, policy_id="policy-b",
            statement="Feature B may build on Feature A's accepted commit",
            repo=REPO, root_rev=self.root_rev, dependency_job="job_a", dependency_feature="feature-a",
            dependent_feature="feature-b", dependent_job="job_b", scope=["smoke/double.py"],
            bees=1, wall_clock_s=1800, contract_template=copy.deepcopy(self.packet["contract_template"]),
            gates=("merge",), workflow_ref=head)

    def _mark_verified(self, attempt_id: str, sha: str, files: str, base_rev: str | None = None) -> None:
        record = verify_local.record_path(self.state, attempt_id)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"attempt_id": attempt_id, "ok": True, "sha": sha,
                                      "base_rev": base_rev or self.root_rev, "files": [files],
                                      "suite": {"exit_code": 0}, "gitleaks": {"exit_code": 0},
                                      "mode": "commit-local"}))

    def materialize(self, packet: dict | None = None):
        return handoff.materialize(
            self.state, feature_id="feature-b", job_id="job_b",
            packet=copy.deepcopy(packet if packet is not None else self.packet),
            worktree=self.repo, mirror=self.mirror, repo_identity=REPO)

    # ------------------------------------------------------------------ helpers
    def move_dependency_source(self) -> str:
        """Accept a second wave in Feature A, moving the dependency's accepted commit."""

        (self.repo / "NEXT").write_text("next\n")
        git(["add", "-A"], self.repo)
        git(["commit", "-q", "-m", "feature A follow-up"], self.repo)
        new = git(["rev-parse", "HEAD"], self.repo).stdout.strip()
        self._mark_verified("att_2", new, "NEXT", base_rev=self.accepted)
        job = jobs.load_job(self.state, "job_a")
        job["integration"] = {"wave": "w2", "state": "accepted", "last_commit": new,
                              "accepted_waves": [{"wave": 1, "commit": self.accepted, "attempts": ["att_1"]},
                                                 {"wave": 2, "commit": new, "attempts": ["att_2"]}]}
        jobs.save_job(self.state, job)
        return new


CHILD_PUBLISH = '''\
import json, os, sys, time
from pathlib import Path
from bundlebees_plugin import transport

state, job_id, repo, mirror, root_rev, repo_identity, start = sys.argv[1:8]
start_path = Path(start)
Path(f"{start}.ready.{os.getpid()}").write_text("ready")
deadline = time.time() + 60
while not start_path.exists():
    if time.time() > deadline:
        print(json.dumps({"error": "barrier_timeout"}))
        sys.exit(4)
    time.sleep(0.002)
try:
    receipt = transport.publish(Path(state), job_id, repo=Path(repo), mirror=Path(mirror),
                                repo_identity=repo_identity, root_rev=root_rev)
except transport.TransportError as exc:
    print(json.dumps({"error": exc.code, "message": exc.message}))
    sys.exit(0)
print(json.dumps({"ok": True, "recovered": receipt["recovered"],
                  "source_sha": receipt["source_sha"], "mirror_ref": receipt["mirror_ref"]}))
'''

CHILD_PUSH = '''\
import json, os, sys, time
from pathlib import Path
from bundlebees_plugin import transport

repo, mirror, sha, ref, start = sys.argv[1:6]
start_path = Path(start)
Path(f"{start}.ready.{os.getpid()}").write_text("ready")
deadline = time.time() + 60
while not start_path.exists():
    if time.time() > deadline:
        print(json.dumps({"error": "barrier_timeout"}))
        sys.exit(4)
    time.sleep(0.002)
try:
    created = transport.create_only_push(Path(repo), Path(mirror), sha=sha, ref=ref)
except transport.TransportError as exc:
    print(json.dumps({"error": exc.code, "message": exc.message}))
    sys.exit(0)
print(json.dumps({"ok": True, "created": created}))
'''

CHILD_CRASH = '''\
import json, os, sys
from pathlib import Path
from bundlebees_plugin import handoff, job as jobs

state, feature_id, job_id, repo, mirror, packet_path, marker = sys.argv[1:8]
state = Path(state)

def crash(name, **payload):
    Path(marker).write_text(json.dumps({
        "name": name,
        "payload": payload,
        "resolution_exists": handoff.resolution_path(state, job_id).exists(),
        "job_exists": jobs.job_path(state, job_id).exists(),
    }, sort_keys=True))
    os._exit(9)

handoff._checkpoint = crash
handoff.materialize(state, feature_id=feature_id, job_id=job_id,
                    packet=json.loads(Path(packet_path).read_text()),
                    worktree=Path(repo), mirror=Path(mirror), repo_identity="mojomast/smoke-real")
print(json.dumps({"ok": True}))
'''


@unittest.skipUnless(HAS_WORKFLOW or REQUIRE_WORKFLOW, "set WORKFLOW_SRC to the pinned workflow checkout")
class MirrorCase(unittest.TestCase):
    def setUp(self):
        if not HAS_WORKFLOW:
            self.fail("WORKFLOW_SRC must point at the pinned workflow checkout when REQUIRE_WORKFLOW=1")
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_obj.cleanup)
        self.tmp = Path(self._tmp_obj.name)
        self.fx = Fixture(self.tmp)
        self.state = self.fx.state
        self.mirror = self.fx.mirror
        self.repo = self.fx.repo
        self.ref = transport.ref_for("job_a", self.fx.accepted)

        self.scripts = self.tmp / "scripts"
        self.scripts.mkdir()
        self.child_publish = self._script("child_publish.py", CHILD_PUBLISH)
        self.child_push = self._script("child_push.py", CHILD_PUSH)
        self.child_crash = self._script("child_crash.py", CHILD_CRASH)

    def _script(self, name: str, body: str) -> Path:
        path = self.scripts / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ children
    def run_children(self, script: Path, arg_lists: list[list[str]]) -> list[tuple[int, str, str]]:
        """Start every child at one barrier, then release them together."""

        start = self.tmp / "START"
        env = {**os.environ, "PYTHONPATH": str(PKG_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        procs = [subprocess.Popen([sys.executable, str(script), *args, str(start)], cwd=str(self.repo),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for args in arg_lists]
        deadline = time.time() + 60
        while time.time() < deadline:
            if len([p for p in self.tmp.glob("START.ready.*")]) >= len(procs):
                break
            if any(proc.poll() is not None for proc in procs):
                break                      # a child died early; communicate() reports why
            time.sleep(0.005)
        start.write_text("go\n")
        finished = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=120)
            finished.append((proc.returncode, stdout, stderr))
        return finished

    @staticmethod
    def last_json(stdout: str) -> dict:
        for line in reversed([line for line in stdout.splitlines() if line.strip()]):
            return json.loads(line)
        raise AssertionError(f"child printed no JSON: {stdout!r}")

    def assert_single_ref(self, sha: str | None = None) -> None:
        refs = accepted_refs(self.mirror)
        self.assertEqual(len(refs), 1, refs)
        self.assertEqual(refs[0], f"{sha or self.fx.accepted} {self.ref}")


# ---------------------------------------------------------------------------
# (a) Create-only publication at Git's update boundary
# ---------------------------------------------------------------------------


class CreateOnlyPush(MirrorCase):
    def test_the_primitive_creates_once_and_refuses_to_move_the_ref(self):
        self.assertTrue(transport.create_only_push(self.repo, self.mirror,
                                                   sha=self.fx.accepted, ref=self.ref))
        self.assert_single_ref()

        # The same commit again is a no-op, not a second update.
        self.assertFalse(transport.create_only_push(self.repo, self.mirror,
                                                    sha=self.fx.accepted, ref=self.ref))

        # A different commit at the same ref is refused by the update transaction.
        with self.assertRaises(transport.TransportError) as caught:
            transport.create_only_push(self.repo, self.mirror, sha=self.fx.root_rev, ref=self.ref)
        self.assertEqual(caught.exception.code, "ref_conflict")
        self.assert_single_ref()

    def test_publish_never_moves_a_ref_even_when_its_precheck_misses_it(self):
        # Somebody else already created the ref at another commit.
        git(["push", "-q", str(self.mirror), f"{self.fx.root_rev}:{self.ref}"], self.repo)
        self.assertEqual(accepted_refs(self.mirror), [f"{self.fx.root_rev} {self.ref}"])

        real = transport._remote_ref
        calls = {"n": 0}

        def blind(mirror, ref):
            calls["n"] += 1
            if calls["n"] == 1:
                return None                # lie: "the ref is absent, go ahead and push"
            return real(mirror, ref)

        transport._remote_ref = blind
        try:
            with self.assertRaises(transport.TransportError) as caught:
                transport.publish(self.state, "job_a", repo=self.repo, mirror=self.mirror,
                                  repo_identity=REPO, root_rev=self.fx.root_rev)
        finally:
            transport._remote_ref = real

        self.assertEqual(caught.exception.code, "ref_conflict")
        self.assertEqual(accepted_refs(self.mirror), [f"{self.fx.root_rev} {self.ref}"])
        self.assertFalse(handoff.resolution_path(self.state, "job_b").exists())
        self.assertFalse(jobs.job_path(self.state, "job_b").exists())


# ---------------------------------------------------------------------------
# (d) Two OS processes at once
# ---------------------------------------------------------------------------


class ConcurrentPublishers(MirrorCase):
    def test_two_processes_publishing_the_same_sha_converge_on_one_ref(self):
        runs = self.run_children(self.child_publish, [
            [str(self.state), "job_a", str(self.repo), str(self.mirror), self.fx.root_rev, REPO],
            [str(self.state), "job_a", str(self.repo), str(self.mirror), self.fx.root_rev, REPO],
        ])

        results = []
        for code, stdout, stderr in runs:
            self.assertEqual(code, 0, stderr)
            results.append(self.last_json(stdout))
        for result in results:
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["source_sha"], self.fx.accepted)
            self.assertEqual(result["mirror_ref"], self.ref)
        self.assertEqual(sum(1 for r in results if not r["recovered"]), 1,
                         f"exactly one process must have created the ref: {results}")
        self.assert_single_ref()

        # Even after both processes, a third publication is a recovery, not a push.
        receipt = self.fx.materialize()             # job_b does not exist yet: publishes + resolves
        self.assertTrue(receipt["created"])
        self.assert_single_ref()

    def test_two_processes_publishing_conflicting_shas_have_exactly_one_winner(self):
        other = self.fx.root_rev                     # a real commit that is not the accepted one

        runs = self.run_children(self.child_push, [
            [str(self.repo), str(self.mirror), self.fx.accepted, self.ref],
            [str(self.repo), str(self.mirror), other, self.ref],
        ])

        results = []
        for code, stdout, stderr in runs:
            self.assertEqual(code, 0, stderr)
            results.append(self.last_json(stdout))

        winners = [r for r in results if r.get("created")]
        losers = [r for r in results if r.get("error")]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertEqual(losers[0]["error"], "ref_conflict", results)

        # The ref holds exactly the winner's commit -- and never moves afterwards.
        stored = accepted_refs(self.mirror)
        self.assertEqual(len(stored), 1, stored)
        stored_sha = stored[0].split()[0]
        self.assertIn(stored_sha, {self.fx.accepted, other})

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                loser_sha = other if stored_sha == self.fx.accepted else self.fx.accepted
                with self.assertRaises(transport.TransportError) as caught:
                    transport.create_only_push(self.repo, self.mirror, sha=loser_sha, ref=self.ref)
                self.assertEqual(caught.exception.code, "ref_conflict")
                self.assertEqual(accepted_refs(self.mirror)[0].split()[0], stored_sha)
        self.assert_single_ref(stored_sha)


# ---------------------------------------------------------------------------
# (b)+(c) The post-publication crash boundary and its recovery
# ---------------------------------------------------------------------------


class CrashAfterPublication(MirrorCase):
    def crash_at_checkpoint(self) -> dict:
        """Run materialize with the checkpoint raising; return what the hook observed."""

        observed: dict = {}
        real = handoff._checkpoint

        def crash(name, **payload):
            observed["name"] = name
            observed["payload"] = payload
            observed["ref"] = git(["ls-remote", str(self.mirror), payload["mirror_ref"]],
                                  self.tmp).stdout.split()
            observed["resolution_exists"] = handoff.resolution_path(self.state, "job_b").exists()
            raise RuntimeError("simulated crash at transport.after_publish")

        handoff._checkpoint = crash
        try:
            with self.assertRaises(RuntimeError):
                self.fx.materialize()
        finally:
            handoff._checkpoint = real
        return observed

    def test_a_crash_at_the_checkpoint_recovers_without_a_second_publication(self):
        observed = self.crash_at_checkpoint()

        self.assertEqual(observed["name"], "transport.after_publish")
        self.assertEqual(observed["payload"], {"job_id": "job_b", "mirror_ref": self.ref,
                                               "source_sha": self.fx.accepted})
        # The publication is already visible, and nothing after it was persisted.
        self.assertEqual(observed["ref"], [self.fx.accepted, self.ref])
        self.assertFalse(observed["resolution_exists"])
        self.assertFalse(handoff.resolution_path(self.state, "job_b").exists())
        self.assertFalse(jobs.authorization_path(self.state, "job_b").exists())
        self.assertFalse(jobs.job_path(self.state, "job_b").exists())
        self.assert_single_ref()
        self.assertFalse(json.loads(transport.receipt_path(self.state, "job_a", self.fx.accepted)
                                    .read_text())["recovered"])

        # Restart: the existing accepted ref is reconciled, not re-pushed.
        pushes: list[str] = []
        real_push = transport.create_only_push

        def counting(*args, **kwargs):
            pushes.append(kwargs.get("ref") or "")
            return real_push(*args, **kwargs)

        transport.create_only_push = counting
        try:
            result = self.fx.materialize()
        finally:
            transport.create_only_push = real_push

        self.assertTrue(result["created"])
        self.assertEqual(result["source_rev"], self.fx.accepted)
        self.assertEqual(pushes, [], "recovery must not push the ref a second time")
        self.assertTrue(json.loads(transport.receipt_path(self.state, "job_a", self.fx.accepted)
                                   .read_text())["recovered"])
        self.assert_single_ref()

        resolved = json.loads(handoff.resolution_path(self.state, "job_b").read_text())
        packets.validate_resolution(resolved, packet=self.fx.packet, accepted_commit=self.fx.accepted)
        self.assertEqual(resolved["derived_source_rev"], self.fx.accepted)
        self.assertEqual(jobs.load_job(self.state, "job_b")["goal"]["base_rev"], self.fx.accepted)
        authorization = jobs.load_authorization(self.state, "job_b")
        self.assertEqual(authorization["base_rev"], self.fx.accepted)
        self.assertEqual(authorization["policy"]["policy_id"], "policy-b")

        # Replaying the same intent changes neither the resolution bytes nor the ref.
        digest = resolution_digest(self.state)
        again = self.fx.materialize()
        self.assertFalse(again["created"])
        self.assertEqual(resolution_digest(self.state), digest)
        self.assert_single_ref()

    def test_a_killed_child_process_after_publication_resumes_to_one_complete_job(self):
        packet_path = self.tmp / "packet.json"
        packet_path.write_text(json.dumps(self.fx.packet))
        marker = self.tmp / "crashed.json"

        runs = self.run_children(self.child_crash, [
            [str(self.state), "feature-b", "job_b", str(self.repo), str(self.mirror),
             str(packet_path), str(marker)],
        ])
        code, stdout, stderr = runs[0]
        self.assertEqual(code, 9, f"the child must die at the checkpoint: {stdout} {stderr}")

        observed = json.loads(marker.read_text())
        self.assertEqual(observed["name"], "transport.after_publish")
        self.assertEqual(observed["payload"]["source_sha"], self.fx.accepted)
        self.assertEqual(observed["payload"]["mirror_ref"], self.ref)
        self.assertFalse(observed["resolution_exists"])
        self.assertFalse(observed["job_exists"])
        self.assert_single_ref()

        result = self.fx.materialize()
        self.assertTrue(result["created"])
        self.assertEqual(result["source_rev"], self.fx.accepted)
        self.assert_single_ref()

        digest = resolution_digest(self.state)
        resolved = json.loads(handoff.resolution_path(self.state, "job_b").read_text())
        self.assertEqual(resolved["derived_source_rev"], self.fx.accepted)
        job_b = jobs.load_job(self.state, "job_b")
        self.assertEqual(job_b["goal"]["base_rev"], self.fx.accepted)
        self.assertEqual(job_b["tasks"][0]["provision"]["source_rev"], self.fx.accepted)

        self.fx.materialize()
        self.assertEqual(resolution_digest(self.state), digest, "the resolution changed on replay")
        self.assert_single_ref()

    def test_a_resolution_and_authorization_survive_the_job_crash_window(self):
        first = self.fx.materialize()
        authorization = jobs.load_authorization(self.state, "job_b")
        digest = resolution_digest(self.state)
        jobs.job_path(self.state, "job_b").unlink()     # crash between the records and the job

        pushes: list[str] = []
        real_push = transport.create_only_push

        def counting(*args, **kwargs):
            pushes.append(kwargs.get("ref") or "")
            return real_push(*args, **kwargs)

        transport.create_only_push = counting
        try:
            again = self.fx.materialize()
        finally:
            transport.create_only_push = real_push

        self.assertTrue(again["created"])
        self.assertEqual(pushes, [], "a recovered job must not re-publish the accepted ref")
        self.assertEqual(jobs.load_authorization(self.state, "job_b")["auth_id"], authorization["auth_id"])
        self.assertEqual(resolution_digest(self.state), digest)
        self.assertEqual(jobs.load_job(self.state, "job_b")["goal"]["base_rev"], self.fx.accepted)
        self.assertEqual(again["source_rev"], first["source_rev"])
        self.assert_single_ref()

    def test_a_conflicting_resolution_record_is_refused_on_restart(self):
        self.fx.materialize()
        path = handoff.resolution_path(self.state, "job_b")
        record = json.loads(path.read_text())
        record["derived_source_rev"] = "e" * 40        # a different source, same identity fields
        path.chmod(0o600)
        path.write_text(json.dumps(record))
        jobs.job_path(self.state, "job_b").unlink()    # the crash window before job creation
        tampered = path.read_bytes()

        with self.assertRaises(handoff.HandoffError) as caught:
            self.fx.materialize()
        self.assertEqual(caught.exception.code, "resolution_conflict")
        self.assertEqual(path.read_bytes(), tampered, "an immutable resolution must not be rewritten")
        self.assertFalse(jobs.job_path(self.state, "job_b").exists())
        self.assert_single_ref()

    def test_a_conflicting_authorization_record_is_refused_on_restart(self):
        self.fx.materialize()
        auth_path = jobs.authorization_path(self.state, "job_b")
        auth_path.chmod(0o600)
        auth_path.unlink()
        jobs.record_derived_authorization(
            self.state, job_id="job_b", parent_job_id="job_a",
            statement="a conflicting derived authority for the same job",
            repo=REPO, candidate_rev=self.fx.accepted, scope=["smoke/double.py"], bees=1,
            wall_clock_s=1800, policy_id="policy-b", dependency_feature="feature-a",
            dependent_feature="feature-b", gates=("merge",))
        digest = resolution_digest(self.state)
        jobs.job_path(self.state, "job_b").unlink()

        with self.assertRaises(handoff.HandoffError) as caught:
            self.fx.materialize()
        self.assertEqual(caught.exception.code, "derived_authorization_exists")
        self.assertFalse(jobs.job_path(self.state, "job_b").exists(),
                         "a conflicting authorization must never produce a job")
        self.assertEqual(resolution_digest(self.state), digest)
        self.assert_single_ref()

    def test_a_moved_dependency_source_is_refused_on_restart(self):
        self.fx.materialize()
        digest = resolution_digest(self.state)
        resolved = json.loads(handoff.resolution_path(self.state, "job_b").read_text())
        jobs.job_path(self.state, "job_b").unlink()    # the crash window before job creation

        moved = self.fx.move_dependency_source()
        self.assertNotEqual(moved, self.fx.accepted)

        with self.assertRaises(handoff.HandoffError) as caught:
            self.fx.materialize()
        self.assertEqual(caught.exception.code, "source_changed")
        self.assert_single_ref()                       # nothing new was published
        self.assertEqual(resolution_digest(self.state), digest)
        self.assertEqual(json.loads(handoff.resolution_path(self.state, "job_b").read_text()),
                         resolved)
        self.assertFalse(jobs.job_path(self.state, "job_b").exists())


# ---------------------------------------------------------------------------
# (b) The checkpoint is the shared fault interface when that lane is present
# ---------------------------------------------------------------------------


@unittest.skipUnless(_faults() is not None, "bundlebees_plugin.faults is not installed")
class SharedFaultInterface(MirrorCase):
    def test_the_checkpoint_fires_through_the_shared_fault_interface(self):
        faults = _faults()
        assert faults is not None                  # the class is skipped without the module
        seen: list[dict] = []
        faults.unregister("transport.after_publish")     # never inherit another lane's hook
        faults.register("transport.after_publish", lambda **payload: seen.append(dict(payload)))
        self.addCleanup(faults.unregister, "transport.after_publish")

        self.fx.materialize()

        self.assertEqual(len(seen), 1, seen)
        self.assertEqual(seen[0], {"job_id": "job_b", "mirror_ref": self.ref,
                                   "source_sha": self.fx.accepted})


if __name__ == "__main__":
    unittest.main()
