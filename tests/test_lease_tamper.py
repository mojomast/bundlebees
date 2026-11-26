"""Tests for .github/scripts/lease_tamper.py.

Every case builds a real temporary git repository and drives the real CLI, so
the checks are proven against git history rather than mocked diffs.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "lease_tamper.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: sha256 of the shared policy block and the shared fixtures.  Both repositories
#: that vendor this gate (workflow and hyvv) and the merge lane (rizzstack)
#: assert the same values, so the copies cannot drift silently.
SHARED_POLICY_SHA256 = "f749821a6170379743a7e440edcb0d90141de8d04bf22ef9a3b6d28a78b83ec8"
REVIEW_LINES_SHA256 = "ac2de684dd22fceed5aa2f18ac6d7d6f8c101c0fd67a619d2a2c6a663642311b"
CARRY_FIXTURES_SHA256 = "33435d738b84742c8e2b5cfa3e153cf20921fb7a1985f808bbde68603941165e"
SHARED_BLOCK_BEGIN = "# --- BEGIN SHARED GATE POLICY"
SHARED_BLOCK_END = "# --- END SHARED GATE POLICY -------------------------------------------------"


def load_gate_module():
    """Import the gate script as a module (it has no import-time side effects)."""
    spec = importlib.util.spec_from_file_location("lease_tamper_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shared_policy_block():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index(SHARED_BLOCK_BEGIN)
    end = text.index(SHARED_BLOCK_END) + len(SHARED_BLOCK_END)
    return text[start:end]


class GitRepo:
    def __init__(self, path):
        self.path = Path(path)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "lease-tamper@example.invalid")
        self.git("config", "user.name", "Lease Tamper Tests")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise AssertionError(
                "git %s failed: %s\n%s" % (" ".join(args), proc.stdout, proc.stderr)
            )
        return proc

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.rev()

    def rev(self, ref="HEAD"):
        return self.git("rev-parse", ref).stdout.strip()

    def checkout(self, *args):
        self.git("checkout", *args)


def new_git_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return GitRepo(path)


def contract_document(base, assigned, test_files, prohibited=(), read_only=(), approved=True):
    """A minimal approved ``acdd/1``-shaped contract for the fixture."""
    return {
        "contract_version": "acdd/1",
        "id": "wave-test",
        "source_rev": base,
        "scope": {
            "assigned_files": list(assigned),
            "test_files": list(test_files),
            "prohibited": list(prohibited),
            "read_only": list(read_only),
        },
        "review": {
            "status": "approved" if approved else "draft",
            "approved_by": "owner (test fixture)" if approved else "",
        },
    }


def contract_hash(document):
    """The gate's contract identity: compact key-sorted JSON without ``review``."""
    body = {key: value for key, value in document.items() if key != "review"}
    text = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lease_document(document, sha, source_rev, frozen=True, branch="slice"):
    """The lease a coordinator derives verbatim from the approved contract."""
    lease = {
        "id": document.get("id") or "wave-test",
        "lease_version": "rizzstack.lease/1",
        "contract_id": document.get("id"),
        "contract_sha256": sha,
        "source_rev": source_rev,
        "branch": branch,
        "scope": {key: list(value) for key, value in document["scope"].items()},
        "review": {
            "status": "frozen" if frozen else "draft",
            "approved_by": "coordinator (test fixture)" if frozen else "",
        },
    }
    return lease


def build_slice(
    root,
    verify=True,
    assigned=("src/slice.py",),
    test_files=("tests/test_frozen.py",),
    prohibited=(),
    contract_path=".rizzstack/leases/slice.json",
    approved=True,
    branch="slice",
):
    """Build a base commit carrying an approved contract + lease, then a slice branch.

    The contract and the lease live on ``main`` (the gate never reads them from
    the pull request).  The implementation commit is returned as ``verify`` so a
    local run can pin the verify boundary explicitly with ``--verify-sha``.
    """
    repo = new_git_repo(root)
    repo.write("src/slice.py", "base\n")
    repo.write("tests/test_frozen.py", "base\n")
    repo.write("src/unleased.py", "base\n")
    repo.write("README.md", "base\n")
    source_rev = repo.commit("base")

    document = contract_document(source_rev, assigned, test_files, prohibited, approved=approved)
    sha = contract_hash(document)
    repo.write(
        ".rizzstack/contracts/%s.json" % sha,
        json.dumps(document, indent=2, sort_keys=True) + "\n",
    )
    repo.write(
        contract_path,
        json.dumps(lease_document(document, sha, source_rev, branch=branch), indent=2) + "\n",
    )
    base = repo.commit("land the approved contract and its lease on main")

    repo.checkout("-b", branch)
    repo.write("src/slice.py", "implementation\n")
    verification = repo.commit("implement slice")

    return repo, base, verification


class LeaseTamperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def new_repo(self, name="repo"):
        return new_git_repo(self.tmp / name)

    def make_slice(
        self,
        name="repo",
        verify=True,
        assigned=("src/slice.py",),
        test_files=("tests/test_frozen.py",),
        prohibited=(),
        contract_path=".rizzstack/leases/slice.json",
    ):
        return build_slice(
            self.tmp / name,
            verify=verify,
            assigned=assigned,
            test_files=test_files,
            prohibited=prohibited,
            contract_path=contract_path,
        )

    def run_gate(
        self,
        repo,
        base,
        head,
        verify=None,
        contract=".rizzstack/leases/slice.json",
        labels=None,
        body=None,
    ):
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--branch",
            "slice",
        ]
        if verify:
            command += ["--verify-sha", verify]
        if contract:
            command += ["--contract", contract]
        if labels:
            command += ["--labels", labels]
        if body:
            command += ["--body", body]
        return subprocess.run(command, capture_output=True, text=True)

    # -- clean history ----------------------------------------------------

    def test_clean_history_passes(self):
        repo, base, verify = self.make_slice()
        repo.write("src/slice.py", "follow-up inside the lease\n")
        head = repo.commit("follow-up inside the lease")

        proc = self.run_gate(repo, base, head, verify=verify)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)
        self.assertIn("no frozen test file touched", proc.stdout)

    def test_no_commit_after_verify_passes(self):
        repo, base, verify = self.make_slice(verify=False)
        head = repo.rev()

        proc = self.run_gate(repo, base, head, verify=head)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 commit(s) after verify sha", proc.stdout)

    # -- tamper cases -----------------------------------------------------

    def test_commit_touching_a_frozen_test_fails(self):
        repo, base, verify = self.make_slice()
        repo.write("tests/test_frozen.py", "tampered\n")
        head = repo.commit("quietly relax the frozen test")

        proc = self.run_gate(repo, base, head, verify=verify)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            "lease-tamper: FAILED: frozen test file 'tests/test_frozen.py' (scope.test_files) "
            "modified after verify sha " + verify,
            proc.stdout,
        )
        self.assertIn("by commit " + head[:12], proc.stdout)
        self.assertIn("frozen tests may never change", proc.stdout)

    def test_commit_outside_the_lease_fails(self):
        repo, base, verify = self.make_slice()
        repo.write("src/unleased.py", "sneaky\n")
        head = repo.commit("touch a file outside the lease")

        proc = self.run_gate(repo, base, head, verify=verify)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            "lease-tamper: FAILED: 'src/unleased.py' is outside the slice lease "
            "(scope.assigned_files)",
            proc.stdout,
        )
        self.assertIn("by commit " + head[:12], proc.stdout)

    def test_frozen_test_listed_in_assigned_files_still_fails(self):
        repo, base, verify = self.make_slice(
            assigned=("src/slice.py", "tests/test_frozen.py")
        )
        repo.write("tests/test_frozen.py", "tampered anyway\n")
        head = repo.commit("assigned but still frozen")

        proc = self.run_gate(repo, base, head, verify=verify)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("frozen test file 'tests/test_frozen.py'", proc.stdout)

    def test_prohibited_glob_fails(self):
        repo, base, verify = self.make_slice(prohibited=("docs/**",))
        repo.write("docs/notes.md", "not allowed\n")
        head = repo.commit("write a prohibited path")

        proc = self.run_gate(repo, base, head, verify=verify)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("frozen test file 'docs/notes.md' (scope.prohibited)", proc.stdout)

    # -- fail-closed cases ------------------------------------------------

    def test_missing_contract_fails_closed(self):
        repo = self.new_repo()
        repo.write("src/slice.py", "base\n")
        base = repo.commit("base")
        repo.write("src/slice.py", "change\n")
        head = repo.commit("change without any lease")

        proc = self.run_gate(repo, base, head, verify=head, contract=None)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("no frozen lease on base", proc.stdout)

    def test_malformed_contract_fails_closed(self):
        repo = self.new_repo()
        repo.write("src/slice.py", "base\n")
        source_rev = repo.commit("base")
        repo.write(".rizzstack/leases/slice.json", "{not json")
        base = repo.commit("broken lease on main")
        repo.checkout("-b", "slice")
        repo.write("src/slice.py", "change\n")
        head = repo.commit("broken lease")

        proc = self.run_gate(repo, base, head, verify=head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("is not valid JSON", proc.stdout)

    def test_missing_assigned_files_fails_closed(self):
        repo = self.new_repo()
        repo.write("src/slice.py", "base\n")
        source_rev = repo.commit("base")
        document = contract_document(source_rev, [], [])
        sha = contract_hash(document)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        repo.write(
            ".rizzstack/leases/slice.json",
            json.dumps(lease_document(document, sha, source_rev), indent=2) + "\n",
        )
        base = repo.commit("lease without assigned files")
        repo.checkout("-b", "slice")
        repo.write("src/slice.py", "change\n")
        head = repo.commit("change")

        proc = self.run_gate(repo, base, head, verify=head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("scope.assigned_files must be a non-empty list", proc.stdout)

    def test_verify_sha_that_is_not_an_ancestor_fails_closed(self):
        repo, base, verify = self.make_slice()
        repo.checkout("main")
        repo.write("README.md", "main moved\n")
        side = repo.commit("main moves on")
        repo.checkout("slice")
        head = repo.rev()

        proc = self.run_gate(repo, base, head, verify=side)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("is not an ancestor of PR head", proc.stdout)

    # -- nothing on the pull request selects the lease or the boundary ------

    def add_broad_lease(self, repo, base):
        """Land a second, broader approved lease on main that names another branch."""
        repo.checkout("main")
        document = contract_document(base, ["src/unleased.py"], [])
        document["id"] = "wave-broad"
        sha = contract_hash(document)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        repo.write(
            ".rizzstack/leases/broad.json",
            json.dumps(
                lease_document(document, sha, base, branch="feat/broad"), indent=2
            )
            + "\n",
        )
        new_base = repo.commit("land a broad lease that names another branch")
        repo.checkout("slice")
        return new_base

    def test_label_cannot_select_another_base_lease(self):
        repo, base, verify = self.make_slice(name="label-select")
        base = self.add_broad_lease(repo, base)
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("step outside the lease that names this branch")

        proc = self.run_gate(
            repo,
            base,
            head,
            contract=None,
            labels="lease-contract:.rizzstack/leases/broad.json",
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("'src/unleased.py' is outside the slice lease", proc.stdout)

    def test_body_line_cannot_select_another_base_lease(self):
        repo, base, verify = self.make_slice(name="body-select")
        base = self.add_broad_lease(repo, base)
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("step outside the lease that names this branch")

        proc = self.run_gate(
            repo,
            base,
            head,
            contract=None,
            body="Lease-Contract: .rizzstack/leases/broad.json\n",
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("'src/unleased.py' is outside the slice lease", proc.stdout)

    def test_body_line_cannot_move_the_verify_boundary(self):
        repo, base, verify = self.make_slice(name="body-verify")
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("tamper after the reviewed head")

        proc = self.run_gate(
            repo, base, head, contract=None, body="Lease-Verify: " + head + "\n"
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("'src/unleased.py' is outside the slice lease", proc.stdout)

    def test_label_cannot_move_the_verify_boundary(self):
        repo, base, verify = self.make_slice(name="label-verify")
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("tamper after the reviewed head")

        proc = self.run_gate(
            repo, base, head, contract=None, labels="lease-verify:" + head
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("'src/unleased.py' is outside the slice lease", proc.stdout)

    def test_base_lease_judges_a_slice_that_carries_no_lease(self):
        repo, base, verify = self.make_slice(name="base-lease")
        head = repo.rev()

        proc = self.run_gate(repo, base, head, contract=None)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)

    # -- merge handling ---------------------------------------------------

    def test_merge_of_the_base_is_ignored(self):
        repo, base, verify = self.make_slice(name="merge")
        branch_tip = repo.rev()

        repo.checkout("main")
        repo.write("src/unleased.py", "main moved this file\n")
        new_base = repo.commit("main moves a file outside the lease")

        repo.checkout("slice")
        repo.git("merge", "--no-ff", "-m", "merge main into slice", "main")
        head = repo.rev()
        self.assertEqual(repo.rev("HEAD^1"), branch_tip)

        proc = self.run_gate(repo, new_base, head, verify=verify)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)

    def test_conflict_resolution_in_a_frozen_test_fails(self):
        repo, base, _verify = self.make_slice(name="conflict")

        repo.write("tests/test_frozen.py", "slice side\n")
        verify = repo.commit("slice edits the test before verify")

        repo.checkout("main")
        repo.write("tests/test_frozen.py", "main side\n")
        new_base = repo.commit("main edits the same test")

        repo.checkout("slice")
        proc = subprocess.run(
            ["git", "-C", str(repo.path), "merge", "main"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, "the fixture must produce a conflict")
        repo.write("tests/test_frozen.py", "resolution smuggled in\n")
        head = repo.commit("resolve the conflict by rewriting the frozen test")

        gate = self.run_gate(repo, new_base, head, verify=verify)

        self.assertEqual(gate.returncode, 1, gate.stdout + gate.stderr)
        self.assertIn("frozen test file 'tests/test_frozen.py'", gate.stdout)
        self.assertIn("by merge commit " + head[:12], gate.stdout)

    # -- event handling ---------------------------------------------------

    def test_push_event_is_a_no_op(self):
        repo = self.new_repo("push")
        repo.write("src/slice.py", "base\n")
        base = repo.commit("base")
        event = self.tmp / "push-event.json"
        event.write_text(json.dumps({"ref": "refs/heads/main", "after": base}))

        env = dict(os.environ)
        env.pop("GITHUB_EVENT_NAME", None)
        env.pop("GITHUB_EVENT_PATH", None)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(repo.path), str(event)],
            capture_output=True,
            text=True,
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no pull request in this event", proc.stdout)


class SliceClassificationTests(unittest.TestCase):
    """A PR is a slice iff a frozen lease on BASE names it; else it needs a review at head."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-class-"))
        self.api = FakeGitHubApi()
        self.api.pulls[12] = {"number": 12, "head": {}, "base": {}, "labels": [], "body": ""}

    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def build(self, name="class", lease_extra=None, store_name="slice.json", on_base=True):
        """Base carries an approved contract and a frozen lease naming the branch.

        Returns (repo, base, head, lease_path).
        """
        repo = new_git_repo(self.tmp / name)
        repo.write("src/slice.py", "base\n")
        repo.write("src/unleased.py", "base\n")
        repo.write("tests/test_frozen.py", "base\n")
        source_rev = repo.commit("base")
        document = contract_document(source_rev, ["src/slice.py"], ["tests/test_frozen.py"])
        sha = contract_hash(document)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        lease = lease_document(document, sha, source_rev, branch="feat/wave-x-x1")
        lease["id"] = "wave-x-x1"
        lease.update(lease_extra or {})
        path = ".rizzstack/leases/" + store_name
        if on_base:
            repo.write(path, json.dumps(lease, indent=2) + "\n")
            base = repo.commit("land the approved contract and its lease on main")
            repo.checkout("-b", "feat/wave-x-x1")
            repo.write("src/slice.py", "implementation\n")
            head = repo.commit("implement the slice")
        else:
            # The lease exists only on the pull request: a metadata change.
            base = repo.commit("base without a lease")
            repo.checkout("-b", "feat/wave-x-x1")
            repo.write(path, json.dumps(lease, indent=2) + "\n")
            repo.write("src/slice.py", "implementation\n")
            head = repo.commit("add a lease and implement")
        return repo, base, head, path

    def run_gate(
        self, repo, base, head, number="12", branch="feat/wave-x-x1", env=None, labels=None, body=None
    ):
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--branch",
            branch,
        ]
        if number:
            command += ["--pull-request", str(number)]
        if labels:
            command += ["--labels", labels]
        if body:
            command += ["--body", body]
        run_env = dict(os.environ)
        run_env.pop("GITHUB_REPOSITORY", None)
        run_env.pop("GITHUB_TOKEN", None)
        run_env.update(env or {})
        return subprocess.run(command, capture_output=True, text=True, env=run_env)

    def api_env(self):
        return {
            "GITHUB_REPOSITORY": QUEUE_REPO,
            "GITHUB_API_URL": self.api.url,
            "GITHUB_TOKEN": "test-token",
        }

    # -- slice classification ---------------------------------------------

    def test_lease_naming_the_pull_request_classifies_a_slice(self):
        repo, base, head, _path = self.build(lease_extra={"pull_request": 12})

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)

    def test_lease_naming_the_pull_request_enforces_the_lease(self):
        repo, base, head, _path = self.build(lease_extra={"pull_request": 12})
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("step outside the lease")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("'src/unleased.py' is outside the slice lease", proc.stdout)

    def test_lease_naming_the_branch_classifies_a_slice(self):
        repo, base, head, _path = self.build(lease_extra={"branch": "feat/wave-x-x1"})

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)

    def test_lease_naming_the_slice_case_insensitively_classifies_a_slice(self):
        repo, base, head, _path = self.build(lease_extra={"id": "WAVE-X-X1"})

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("lease-tamper: OK:", proc.stdout)

    def test_branch_shape_alone_is_not_a_classifier(self):
        # A feat/ branch and a store lease that names a DIFFERENT slice: non-slice.
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "slice": "wave-other-slice"})

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)

    def test_draft_lease_does_not_classify_a_slice(self):
        repo, base, head, _path = self.build(
            lease_extra={"pull_request": 12, "review": {"status": "draft"}}
        )

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)

    def test_lease_that_names_the_pr_only_on_the_head_is_a_metadata_change(self):
        repo, base, head, _path = self.build(lease_extra={"pull_request": 12}, on_base=False)

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("it changes gate metadata", proc.stdout)
        self.assertIn(".rizzstack/leases/slice.json", proc.stdout)

    def test_label_pointing_at_a_head_lease_is_ignored(self):
        # The base already carries the gate, so a branch-supplied lease is refused.
        repo = new_git_repo(self.tmp / "label-head")
        repo.write("src/slice.py", "base\n")
        repo.write(".github/scripts/lease_tamper.py", "# the gate, already on main\n")
        base = repo.commit("base carries the gate")
        repo.checkout("-b", "feat/wave-x-x1")
        repo.write(
            ".rizzstack/leases/slice.json",
            json.dumps(
                {
                    "id": "wave-x-x1",
                    "source_rev": base,
                    "scope": {"assigned_files": ["src/slice.py"], "test_files": []},
                }
            ),
        )
        repo.write("src/slice.py", "implementation\n")
        head = repo.commit("add a lease and implement")

        proc = self.run_gate(
            repo, base, head, number=None, labels="lease-contract:.rizzstack/leases/slice.json"
        )

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)

    def test_lease_landing_is_non_slice_and_needs_a_review(self):
        # Landing a lease changes only the store, but the store is the gate's
        # input: a lease landing always needs an independent review at its head.
        repo, base, head, _path = self.build(lease_extra={"pull_request": 12})
        repo.write(
            ".rizzstack/leases/second.json",
            json.dumps(lease_document(contract_document(base, ["other.py"], []), "0" * 64, base)),
        )
        head = repo.commit("land another slice lease")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("it changes gate metadata (.rizzstack/leases/second.json)", proc.stdout)

    def test_a_store_edit_does_not_exempt_code(self):
        repo, base, head, _path = self.build(lease_extra={"pull_request": 12})
        repo.write(".rizzstack/leases/second.json", "{}\n")
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("smuggle a code change next to a lease")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("it changes gate metadata", proc.stdout)

    # -- non-slice review rule --------------------------------------------

    def test_non_slice_pr_without_a_review_fails_named(self):
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "pull_request": 99})

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("None found. Post the review at " + head[:12], proc.stdout)

    def test_non_slice_pr_with_an_approve_at_head_passes(self):
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "pull_request": 99})
        self.api.reviews[12] = [
            {
                "state": "COMMENTED",
                "commit_id": head,
                "body": "APPROVE (independent review at %s)\n\nVERDICT: APPROVE" % head,
            }
        ]

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("is not a slice", proc.stdout)
        self.assertIn("independent review (COMMENTED) at head " + head[:12], proc.stdout)

    def test_non_slice_pr_with_an_approved_review_at_head_passes(self):
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "pull_request": 99})
        self.api.reviews[12] = [{"state": "APPROVED", "commit_id": head, "body": ""}]

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("independent review (APPROVED)", proc.stdout)

    def test_non_slice_pr_with_a_review_at_an_older_head_fails(self):
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "pull_request": 99})
        repo.write("src/slice.py", "one more commit\n")
        newer = repo.commit("the head moves after review")
        self.api.reviews[12] = [{"state": "APPROVED", "commit_id": head, "body": ""}]

        proc = self.run_gate(repo, base, newer, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)

    def test_non_slice_changes_requested_is_not_an_approval(self):
        repo, base, head, _path = self.build(lease_extra={"id": "wave-other-slice", "branch": "feat/other", "pull_request": 99})
        self.api.reviews[12] = [
            {"state": "COMMENTED", "commit_id": head, "body": "CHANGES REQUESTED: fix the doc"}
        ]

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)


# -- base-merge review carry-forward ---------------------------------------


class CarryFixtureTests(unittest.TestCase):
    """Temporary repositories for the carry-forward and rejection tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-carry-"))
        self.api = FakeGitHubApi()
        self.api.pulls[12] = {"number": 12, "head": {}, "base": {}, "labels": [], "body": ""}

    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures ---------------------------------------------------------

    def build(self):
        """A non-slice pull request: the base lease names a different slice.

        Returns ``(repo, base, reviewed_head)``.  The review is posted at
        ``reviewed_head``; a later update is what the carry-forward rule judges.
        """
        repo = new_git_repo(self.tmp / "carry")
        repo.write("src/slice.py", "base\n")
        repo.write("src/unleased.py", "base\n")
        repo.write("tests/test_frozen.py", "base\n")
        source_rev = repo.commit("base")
        document = contract_document(source_rev, ["src/slice.py"], ["tests/test_frozen.py"])
        sha = contract_hash(document)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        lease = lease_document(document, sha, source_rev, branch="feat/wave-x-x1")
        lease["id"] = "wave-other-slice"
        lease["pull_request"] = 99
        lease["branch"] = "feat/other"
        repo.write(".rizzstack/leases/slice.json", json.dumps(lease, indent=2) + "\n")
        base = repo.commit("land the contract and another slice's lease on main")
        repo.checkout("-b", "feat/wave-x-x1")
        repo.write("src/slice.py", "reviewed implementation\n")
        reviewed = repo.commit("implement the slice")
        return repo, base, reviewed

    def review_at(self, sha):
        self.api.reviews[12] = [
            {
                "state": "COMMENTED",
                "commit_id": sha,
                "body": "APPROVE (independent review at %s)\n\nVERDICT: approve" % sha,
            }
        ]

    def advance_base(self, repo, name="src/base_only.py", text="base advance\n"):
        """Commit on main, then return to the pull-request branch."""
        repo.checkout("main")
        repo.write(name, text)
        tip = repo.commit("main advances: " + name)
        repo.checkout("feat/wave-x-x1")
        return tip

    def merge(self, repo, ref="main", message="merge main into the branch"):
        repo.git("merge", "--no-ff", "-m", message, ref)
        return repo.rev()

    def run_gate(self, repo, base, head, env=None):
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo", str(repo.path),
            "--base", base,
            "--head", head,
            "--branch", "feat/wave-x-x1",
            "--pull-request", "12",
        ]
        run_env = dict(os.environ)
        run_env.pop("GITHUB_REPOSITORY", None)
        run_env.pop("GITHUB_TOKEN", None)
        run_env.update(env or {})
        return subprocess.run(command, capture_output=True, text=True, env=run_env)

    def api_env(self):
        return {
            "GITHUB_REPOSITORY": QUEUE_REPO,
            "GITHUB_API_URL": self.api.url,
            "GITHUB_TOKEN": "test-token",
        }


class BaseMergeReviewCarryTests(CarryFixtureTests):
    """Owner rule: a review at an earlier head carries over a base-merge-only update.

    Accepted only when (a) the review commit is an ancestor of the head, (b)
    every commit after it that is not on the base is a merge whose non-first
    parents are on the base, and (c) the pull request's diff against the base is
    byte-identical (same paths, same blob shas) to the reviewed diff. The four
    refusal cases are asserted with their named reason.
    """

    # -- the rule ---------------------------------------------------------

    def test_base_merge_only_update_after_review_passes(self):
        repo, _base, reviewed = self.build()
        self.review_at(reviewed)
        new_base = self.advance_base(repo)
        head = self.merge(repo)

        proc = self.run_gate(repo, new_base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("carries forward to head " + head[:12], proc.stdout)
        self.assertIn("review_carry_ok", proc.stdout)
        self.assertIn(reviewed[:12], proc.stdout)

    def test_two_base_merges_in_a_row_still_pass(self):
        # The base may advance more than once; each merge's second parent is an
        # ancestor of the current base tip, so the review still carries.
        repo, _base, reviewed = self.build()
        self.review_at(reviewed)
        self.advance_base(repo, "src/base_one.py")
        self.merge(repo)
        new_base = self.advance_base(repo, "src/base_two.py")
        head = self.merge(repo)

        proc = self.run_gate(repo, new_base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("carries forward", proc.stdout)

    def test_authored_commit_after_review_fails_named(self):
        repo, base, reviewed = self.build()
        self.review_at(reviewed)
        repo.write("src/slice.py", "changed after the review\n")
        head = repo.commit("change the code after the review")

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_carry_authored_commit", proc.stdout)
        self.assertIn("was authored after the review", proc.stdout)

    def test_content_change_resolved_into_a_base_merge_fails_named(self):
        # Every commit after the review is a merge of the base, but the conflict
        # resolution changes the pull request's diff: the reviewed bytes are not
        # the bytes that would merge, so the review cannot carry.
        repo, _base, reviewed = self.build()
        self.review_at(reviewed)
        repo.checkout("main")
        repo.write("src/slice.py", "main side\n")
        new_base = repo.commit("main edits the same line")
        repo.checkout("feat/wave-x-x1")
        conflicted = subprocess.run(
            ["git", "-C", str(repo.path), "merge", "main"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(conflicted.returncode, 0, "the fixture must produce a conflict")
        repo.write("src/slice.py", "resolution neither side reviewed\n")
        head = repo.commit("resolve the conflict with content nobody reviewed")

        proc = self.run_gate(repo, new_base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_carry_content_changed", proc.stdout)
        self.assertIn("src/slice.py", proc.stdout)

    def test_merge_of_an_unrelated_branch_fails_named(self):
        repo, base, reviewed = self.build()
        self.review_at(reviewed)
        repo.checkout("-b", "side", "main")
        repo.write("src/side.py", "side work\n")
        repo.commit("work on a branch that is not the base")
        repo.checkout("feat/wave-x-x1")
        head = self.merge(repo, "side", "merge an unrelated branch")

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_carry_unrelated_merge", proc.stdout)
        self.assertIn("which is not on base", proc.stdout)

    def test_force_pushed_away_review_fails_named(self):
        # The reviewed commit exists but is not in the head's history: the
        # reviewed code was rewritten, so nothing carries.
        repo, base, _reviewed = self.build()
        repo.checkout("-b", "stale", "main")
        repo.write("src/slice.py", "a variant that is later dropped\n")
        stale = repo.commit("a reviewed variant that is later dropped")
        repo.checkout("feat/wave-x-x1")
        repo.write("src/slice.py", "a rewritten implementation\n")
        head = repo.commit("rewrite the branch after the review")
        self.review_at(stale)

        proc = self.run_gate(repo, base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_not_ancestor", proc.stdout)


# -- newest decisive review governs ----------------------------------------


class RejectionGovernsTests(CarryFixtureTests):
    """A rejection submitted after the approval being used refuses it.

    The three reproduction cases from the Phase 2 review: a rejection at the
    approved commit, a rejection at the new head after a base-merge update, and
    a rejection at the same head as the approval.  A later re-approval
    supersedes the rejection, so the rule is "newest decisive review", not
    "any rejection ever".
    """

    def review(self, sha, state="COMMENTED", body=None, submitted_at="2026-11-26T00:00:00Z"):
        return {
            "state": state,
            "commit_id": sha,
            "body": body if body is not None else "APPROVE (independent review) at %s" % sha,
            "submitted_at": submitted_at,
        }

    def test_rejection_at_the_approved_commit_refuses_the_carried_review(self):
        repo, _base, reviewed = self.build()
        self.api.reviews[12] = [
            self.review(reviewed),
            self.review(reviewed, state="CHANGES_REQUESTED", body="no",
                        submitted_at="2026-11-26T01:00:00Z"),
        ]
        new_base = self.advance_base(repo)
        head = self.merge(repo)

        proc = self.run_gate(repo, new_base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_changes_requested", proc.stdout)

    def test_rejection_at_the_new_head_refuses_the_carried_review(self):
        repo, _base, reviewed = self.build()
        new_base = self.advance_base(repo)
        head = self.merge(repo)
        self.api.reviews[12] = [
            self.review(reviewed),
            self.review(head, body="CHANGES REQUESTED (the carry is unsound) at %s" % head,
                        submitted_at="2026-11-26T01:00:00Z"),
        ]

        proc = self.run_gate(repo, new_base, head, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_changes_requested", proc.stdout)

    def test_rejection_at_the_same_head_refuses_the_exact_review(self):
        repo, base, reviewed = self.build()
        self.api.reviews[12] = [
            self.review(reviewed),
            self.review(reviewed, state="CHANGES_REQUESTED", body="no",
                        submitted_at="2026-11-26T01:00:00Z"),
        ]

        proc = self.run_gate(repo, base, reviewed, env=self.api_env())

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("review_changes_requested", proc.stdout)

    def test_a_later_reapproval_supersedes_the_rejection(self):
        repo, base, reviewed = self.build()
        self.api.reviews[12] = [
            self.review(reviewed),
            self.review(reviewed, state="CHANGES_REQUESTED", body="no",
                        submitted_at="2026-11-26T01:00:00Z"),
            self.review(reviewed, submitted_at="2026-11-26T02:00:00Z"),
        ]

        proc = self.run_gate(repo, base, reviewed, env=self.api_env())

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


# -- lease hardening -------------------------------------------------------


class LeaseHardeningTests(unittest.TestCase):
    """A lease is usable only when it names an approved frozen contract on base."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-hardening-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_gate(self, repo, base, head, env=None):
        command = [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo.path),
            "--base",
            base,
            "--head",
            head,
            "--branch",
            "feat/wave-x-x1",
            "--pull-request",
            "12",
        ]
        run_env = dict(os.environ)
        run_env.pop("GITHUB_REPOSITORY", None)
        run_env.pop("GITHUB_TOKEN", None)
        run_env.update(env or {})
        return subprocess.run(command, capture_output=True, text=True, env=run_env)

    def build(self, lease_factory, contract_factory=None, name="h"):
        """Base carries a contract and a lease; the branch tampers outside them."""
        repo = new_git_repo(self.tmp / name)
        repo.write("src/slice.py", "base\n")
        repo.write("src/unleased.py", "base\n")
        source_rev = repo.commit("base")
        contract = (
            contract_factory(source_rev)
            if contract_factory
            else contract_document(source_rev, ["src/slice.py"], [])
        )
        sha = contract_hash(contract)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(contract, indent=2, sort_keys=True) + "\n",
        )
        lease = lease_factory(source_rev, sha, contract)
        repo.write(".rizzstack/leases/slice.json", json.dumps(lease, indent=2) + "\n")
        base = repo.commit("land the contract and the lease on main")
        repo.checkout("-b", "feat/wave-x-x1")
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("step outside the lease")
        return repo, base, head, source_rev, sha

    def default_lease(self, source_rev, sha, contract):
        return lease_document(contract, sha, source_rev, branch="feat/wave-x-x1")

    def test_lease_without_a_contract_hash_is_rejected(self):
        def lease_factory(source_rev, sha, contract):
            lease = self.default_lease(source_rev, sha, contract)
            del lease["contract_sha256"]
            return lease

        repo, base, head, _source_rev, _sha = self.build(lease_factory, name="no-hash")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=lease_contract_missing", proc.stdout)

    def test_lease_naming_an_unapproved_contract_is_rejected(self):
        def contract_factory(source_rev):
            return contract_document(source_rev, ["src/slice.py"], [], approved=False)

        repo, base, head, _source_rev, _sha = self.build(
            self.default_lease, contract_factory=contract_factory, name="draft-contract"
        )

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=lease_contract_not_approved", proc.stdout)

    def test_contract_text_changed_after_freezing_is_rejected(self):
        repo, base, head, source_rev, sha = self.build(self.default_lease, name="edited-contract")

        repo.checkout("main")
        widened = contract_document(source_rev, ["src/slice.py", "src/unleased.py"], [])
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(widened, indent=2, sort_keys=True) + "\n",
        )
        base = repo.commit("edit the frozen contract text in place")
        repo.checkout("feat/wave-x-x1")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=lease_contract_hash_mismatch", proc.stdout)

    def test_lease_scope_wider_than_the_contract_is_rejected(self):
        def lease_factory(source_rev, sha, contract):
            lease = self.default_lease(source_rev, sha, contract)
            lease["scope"]["assigned_files"].append("src/unleased.py")
            return lease

        repo, base, head, _source_rev, _sha = self.build(lease_factory, name="widened")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=lease_contract_mismatch", proc.stdout)

    def test_lease_without_a_review_block_is_not_frozen(self):
        def lease_factory(source_rev, sha, contract):
            lease = self.default_lease(source_rev, sha, contract)
            del lease["review"]
            return lease

        repo, base, head, _source_rev, _sha = self.build(lease_factory, name="no-review")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)

    def test_head_lease_json_is_a_metadata_change(self):
        repo = new_git_repo(self.tmp / "head-lease-json")
        repo.write("src/slice.py", "base\n")
        repo.write("src/unleased.py", "base\n")
        source_rev = repo.commit("base")
        base = source_rev
        repo.checkout("-b", "feat/wave-x-x1")
        repo.write(
            ".rizzstack/lease.json",
            json.dumps(
                {
                    "source_rev": source_rev,
                    "scope": {"assigned_files": ["*"], "test_files": []},
                }
            ),
        )
        repo.write("src/unleased.py", "tamper\n")
        head = repo.commit("bring a lease and tamper")

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=non_slice_requires_independent_review", proc.stdout)
        self.assertIn("it changes gate metadata (.rizzstack/lease.json)", proc.stdout)

    def test_glob_in_assigned_files_is_rejected(self):
        def contract_factory(source_rev):
            return contract_document(source_rev, ["*"], [])

        repo, base, head, _source_rev, _sha = self.build(
            self.default_lease, contract_factory=contract_factory, name="glob"
        )

        proc = self.run_gate(repo, base, head)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=lease_glob_scope", proc.stdout)
        self.assertIn("wildcard entries are rejected", proc.stdout)

    def test_lease_landing_with_an_independent_review_at_head_passes(self):
        api = FakeGitHubApi()
        self.addCleanup(api.close)
        repo, base, head, source_rev, _sha = self.build(
            self.default_lease, name="reviewed-landing"
        )
        document = contract_document(source_rev, ["src/other.py"], [])
        sha = contract_hash(document)
        repo.write(
            ".rizzstack/contracts/%s.json" % sha,
            json.dumps(document, indent=2, sort_keys=True) + "\n",
        )
        head_with_lease = repo.commit("land another approved contract")
        api.reviews[12] = [
            {
                "state": "COMMENTED",
                "commit_id": head_with_lease,
                "body": "APPROVE (independent review at %s)" % head_with_lease,
            }
        ]

        proc = self.run_gate(
            repo,
            base,
            head_with_lease,
            env={
                "GITHUB_REPOSITORY": QUEUE_REPO,
                "GITHUB_API_URL": api.url,
                "GITHUB_TOKEN": "test-token",
            },
        )

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("is not a slice", proc.stdout)
        self.assertIn("independent review (COMMENTED)", proc.stdout)


# -- merge_queue -----------------------------------------------------------

QUEUE_REPO = "owner/repo"


class FakeGitHubApi:
    """A loopback stand-in for the GitHub endpoints the gate uses.

    Real HTTP through urllib is exercised, so the tests cover the path a runner
    takes: authorization header, JSON decoding, 404/500 handling.
    """

    def __init__(self):
        self.pulls = {}
        self.reviews = {}
        self.commit_pulls = {}
        self.check_runs = {}
        self.status = {}
        self.requests = []
        self.authorizations = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                api.requests.append(self.path)
                api.authorizations.append(self.headers.get("Authorization") or "")
                status, payload = api.respond(self.path)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def add_check_run(self, sha, conclusion="success", status="completed"):
        self.check_runs.setdefault(sha, []).append(
            {"name": "lease-tamper", "head_sha": sha, "status": status, "conclusion": conclusion}
        )

    def respond(self, path):
        clean = (path or "").split("?")[0]
        if clean in self.status:
            return self.status[clean], {"message": "forced by the test"}
        parts = clean.strip("/").split("/")
        if len(parts) < 5 or parts[0] != "repos":
            return 404, {"message": "no route for " + clean}
        if parts[3] == "pulls":
            return self._pull(parts)
        if parts[3] == "commits":
            return self._commit(parts)
        return 404, {"message": "no route for " + clean}

    def _pull(self, parts):
        if len(parts) == 6 and parts[5] == "reviews":
            number = int(parts[4])
            return 200, self.reviews.get(number, [])
        number = int(parts[4]) if len(parts) == 5 else 0
        if number in self.pulls:
            return 200, self.pulls[number]
        return 404, {"message": "Not Found"}

    def _commit(self, parts):
        if len(parts) != 6:
            return 404, {"message": "bad commit path"}
        sha, kind = parts[4], parts[5]
        if kind == "pulls":
            return 200, self.commit_pulls.get(sha, [])
        if kind == "check-runs":
            runs = self.check_runs.get(sha, [])
            return 200, {"total_count": len(runs), "check_runs": runs}
        return 404, {"message": "bad commit route"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MergeGroupTests(unittest.TestCase):
    """merge_group: prove per pull request that the reviewed head passed here."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-queue-"))
        self.api = FakeGitHubApi()

    def tearDown(self):
        self.api.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build_stack(self, numbers=(69,), red=(), head_ref=None, move_base=True):
        """Build a queue group stacking ``numbers`` in order, newest last.

        The group is a real merge chain: base (moved on), merge entry 1, merge
        entry 2 -- the shape GitHub builds for a stacked queue group.
        """
        repo = new_git_repo(self.tmp / "queue")
        repo.write("README.md", "base\n")
        repo.write("src/unleased.py", "base\n")
        base = repo.commit("base")
        current = base
        entries = []
        for number in numbers:
            repo.git("checkout", "-b", "pr-%d" % number, current)
            repo.write("src/slice_%d.py" % number, "implementation %d\n" % number)
            pr_head = repo.commit("implement #%d" % number)
            pull = {
                "number": number,
                "head": {"sha": pr_head},
                "base": {"sha": current},
                "labels": [],
                "body": "",
            }
            self.api.pulls[number] = pull
            self.api.commit_pulls[pr_head] = [pull]
            self.api.add_check_run(pr_head, conclusion="failure" if number in red else "success")
            entries.append({"number": number, "head": pr_head})
            current = pr_head

        repo.git("checkout", "main")
        if move_base:
            repo.write("README.md", "main moved after the pull requests opened\n")
            repo.commit("main moves on")
        base_tip = repo.rev()
        for entry in entries:
            repo.git(
                "merge", "--no-ff", "-m", "merge group entry #%d" % entry["number"], entry["head"]
            )
        head = repo.rev()

        if head_ref is None:
            head_ref = "refs/heads/gh-readonly-queue/main/pr-%d-%s" % (numbers[-1], head)
        event = self.tmp / "merge-group.json"
        event.write_text(
            json.dumps(
                {
                    "merge_group": {
                        "head_sha": head,
                        "head_ref": head_ref,
                        "base_sha": base_tip,
                        "base_ref": "refs/heads/main",
                    }
                }
            )
        )
        return types.SimpleNamespace(repo=repo, event=event, head=head, base=base_tip, entries=entries)

    def run_gate(self, queue, token="test-token"):
        env = dict(os.environ)
        env.pop("GITHUB_EVENT_PATH", None)
        env.pop("GH_TOKEN", None)
        env["GITHUB_EVENT_NAME"] = "merge_group"
        env["GITHUB_REPOSITORY"] = QUEUE_REPO
        env["GITHUB_API_URL"] = self.api.url
        if token is None:
            env.pop("GITHUB_TOKEN", None)
        else:
            env["GITHUB_TOKEN"] = token
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(queue.repo.path), str(queue.event)],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_single_pr_group_passes(self):
        queue = self.build_stack(numbers=(69,))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            "merge_group: PR #69 head %s is in group head %s"
            % (queue.entries[0]["head"][:12], queue.head[:12]),
            proc.stdout,
        )
        self.assertIn("lease-tamper: OK: 1 pull request(s)", proc.stdout)
        self.assertNotIn("::notice", proc.stdout)

    def test_stacked_group_checks_every_pull_request(self):
        queue = self.build_stack(numbers=(69, 70))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PR #69", proc.stdout)
        self.assertIn("PR #70", proc.stdout)
        self.assertIn("2 pull request(s)", proc.stdout)
        self.assertTrue(any("pulls/69" in request for request in self.api.requests))
        self.assertTrue(any("pulls/70" in request for request in self.api.requests))

    def test_stacked_group_fails_when_an_earlier_entry_is_not_green(self):
        queue = self.build_stack(numbers=(69, 70), red=(69,))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)
        self.assertIn("[not-green-at-head] pull request #69", proc.stdout)

    def test_unresolvable_branch_name_fails_merge_group_pr_unresolved(self):
        queue = self.build_stack(
            numbers=(69,), head_ref="refs/heads/gh-readonly-queue/main/candidate-deadbeef"
        )

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)
        self.assertIn("[no-pr-in-ref]", proc.stdout)
        self.assertIn("::error title=lease-tamper merge_group_pr_unresolved::", proc.stdout)
        self.assertNotIn("SKIPPED", proc.stdout)
        self.assertNotIn("::notice", proc.stdout)

    def test_pull_request_head_moved_after_its_pull_request_run_fails(self):
        queue = self.build_stack(numbers=(69,))
        repo = queue.repo
        repo.git("checkout", "pr-69")
        repo.write("src/slice_69.py", "pushed after the pull_request run\n")
        moved = repo.commit("push after the gate ran")
        self.api.pulls[69]["head"]["sha"] = moved

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[head-moved] stacked entry", proc.stdout)
        self.assertIn("belongs to pull request #69", proc.stdout)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)

    def test_no_green_lease_tamper_at_the_reviewed_head_fails(self):
        queue = self.build_stack(numbers=(69,), red=(69,))

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[not-green-at-head] pull request #69", proc.stdout)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)

    def test_api_failure_is_a_failure_not_a_skip(self):
        queue = self.build_stack(numbers=(69,))
        self.api.status["/repos/%s/pulls/69" % QUEUE_REPO] = 500
        self.api.status[
            "/repos/%s/commits/%s/pulls" % (QUEUE_REPO, queue.entries[0]["head"])
        ] = 502

        proc = self.run_gate(queue)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)
        self.assertNotIn("SKIPPED", proc.stdout)

    def test_missing_token_fails_merge_group_pr_unresolved(self):
        queue = self.build_stack(numbers=(69,))

        proc = self.run_gate(queue, token=None)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("[no-credentials]", proc.stdout)
        self.assertIn("reason=merge_group_pr_unresolved", proc.stdout)


# -- shared policy and fixtures --------------------------------------------


class SharedPolicyContractTests(unittest.TestCase):
    """The gate and the lane must run the same parser, carry and path rules."""

    def test_shared_policy_block_is_byte_identical(self):
        self.assertEqual(
            hashlib.sha256(shared_policy_block().encode("utf-8")).hexdigest(),
            SHARED_POLICY_SHA256,
            "the shared policy block changed; vendor the identical block into "
            "rizzstack scripts/merge_lane_v5.py and update the hash in both suites",
        )
        module = load_gate_module()
        for name in (
            "parse_review_line",
            "review_form",
            "governing_rejection",
            "approval_chain",
            "review_carry_forward",
            "sensitive_paths",
            "check_run_is_success",
        ):
            self.assertTrue(hasattr(module, name), name)

    def test_shared_review_line_fixtures(self):
        fixture = FIXTURES / "review_lines.json"
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), REVIEW_LINES_SHA256)
        module = load_gate_module()
        document = json.loads(fixture.read_text(encoding="utf-8"))
        for case in document["cases"]:
            kind, sha = module.parse_review_line(case["body"])
            self.assertEqual(kind, case["kind"], case["name"])
            self.assertEqual(sha, case["sha"], case["name"])

    def test_only_a_completed_success_is_green(self):
        module = load_gate_module()
        self.assertTrue(
            module.check_run_is_success({"status": "completed", "conclusion": "success"})
        )
        for conclusion in ("neutral", "skipped", "failure", "cancelled", "stale", "action_required", ""):
            self.assertFalse(
                module.check_run_is_success({"status": "completed", "conclusion": conclusion}),
                conclusion,
            )
        self.assertFalse(
            module.check_run_is_success({"status": "in_progress", "conclusion": None})
        )

    def test_sensitive_paths_include_the_explicit_list(self):
        module = load_gate_module()
        required = {
            "bundlebees_plugin/verify_local.py",
            "bundlebees_plugin/contract.py",
            "bundlebees_plugin/approval.py",
            "bundlebees_plugin/tdd.py",
            "bundlebees_plugin/acdd_cli.py",
            "bundlebees_plugin/subversion_scan.py",
            "bundlebees_plugin/test_receipts.py",
            "bundlebees_plugin/transport.py",
            "bundlebees_plugin/return_report.py",
            "tests/test_lease_tamper.py",
        }
        self.assertLessEqual(required, set(module.SENSITIVE_PATHS))
        for path in required:
            self.assertEqual(module.sensitive_paths([path]), [path], path)
        self.assertEqual(module.sensitive_paths(["bundlebees_plugin/delegate.py"]), [])


class SharedCarryFixtureTests(unittest.TestCase):
    """Both implementations must agree with one set of git fixtures."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-shared-carry-"))
        self.module = load_gate_module()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_shared_carry_scenarios(self):
        fixture = FIXTURES / "carry_scenarios.py"
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), CARRY_FIXTURES_SHA256)
        spec = importlib.util.spec_from_file_location("carry_scenarios", fixture)
        scenarios = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scenarios)
        for name, review_sha, base, head, reason, ok in scenarios.build_all(self.tmp):
            got_ok, got_reason, detail = self.module.review_carry_forward(
                str(self.tmp / name), review_sha, base, head
            )
            self.assertEqual(
                (got_ok, got_reason), (ok, reason), "%s: %s" % (name, detail)
            )


if __name__ == "__main__":
    unittest.main()
