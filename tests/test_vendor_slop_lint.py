"""Contract tests for scripts/vendor_slop_lint.py (rizzstack follow-ups item 24).

The vendored slop-lint copy in tools/slop-lint must be byte-identical to a
pinned commit of mojomast/workflow. `--check` proves it offline against a
committed manifest; `--refresh --source DIR` re-vendors from a workflow
checkout and records its commit; `--compare DIR` reports drift. Modeled on
scripts/vendor_rails.py --check.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "vendor_slop_lint.py"
VENDOR = REPO / "tools" / "slop-lint"
MANIFEST = VENDOR / "vendor-manifest.json"
WORKFLOW = REPO / ".github" / "workflows" / "slop-lint.yml"


def run(*args, root=None):
    cmd = [sys.executable, str(SCRIPT)]
    if root is not None:
        cmd += ["--root", str(root)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=60, check=True).stdout.strip()


class VendorSlopLintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vendor-slop-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def copy_repo(self):
        """A throwaway root holding a copy of the committed vendor tree."""
        root = self.tmp / "repo"
        shutil.copytree(VENDOR, root / "tools" / "slop-lint",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return root

    def make_source(self):
        """A throwaway workflow checkout with tools/slop-lint committed."""
        src = self.tmp / "workflow"
        (src / "tools" / "slop-lint").mkdir(parents=True)
        for rel in ("README.md", "slop_lint/__init__.py", "slop_lint/cli.py"):
            dst = src / "tools" / "slop-lint" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("# %s\n" % rel)
        (src / "tools" / "slop-lint" / "tests").mkdir()
        (src / "tools" / "slop-lint" / "tests" / "test_x.py").write_text("# not vendored\n")
        git(src, "init", "-q")
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", "seed")
        return src, git(src, "rev-parse", "HEAD")

    # --- the committed copy -------------------------------------------------

    def test_check_passes_on_the_committed_vendor_tree(self):
        r = run("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_manifest_pins_a_full_workflow_commit_and_covers_every_file(self):
        m = json.loads(MANIFEST.read_text())
        self.assertEqual(m["schema"], "rizzstack.slop-lint-vendor/1")
        self.assertEqual(m["source_repo"], "mojomast/workflow")
        self.assertEqual(m["source_path"], "tools/slop-lint")
        self.assertRegex(m["source_commit"], r"^[0-9a-f]{40}$")
        on_disk = sorted(
            p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.name != "vendor-manifest.json"
        )
        self.assertEqual(sorted(e["path"] for e in m["files"]), on_disk)

    def test_ci_runs_the_check_before_linting(self):
        t = WORKFLOW.read_text()
        self.assertIn("python3 scripts/vendor_slop_lint.py --check", t)
        self.assertLess(t.index("vendor_slop_lint.py --check"), t.index("python3 -m slop_lint check"))

    # --- tamper detection ---------------------------------------------------

    def test_check_fails_on_a_changed_byte(self):
        root = self.copy_repo()
        f = root / "tools" / "slop-lint" / "slop_lint" / "rules.py"
        f.write_bytes(f.read_bytes() + b"\n")
        r = run("--check", root=root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("slop_lint/rules.py", r.stderr)

    def test_check_fails_on_an_extra_or_missing_file(self):
        root = self.copy_repo()
        (root / "tools" / "slop-lint" / "slop_lint" / "extra.py").write_text("x = 1\n")
        self.assertEqual(run("--check", root=root).returncode, 1)
        root2 = self.tmp / "r2"
        shutil.copytree(root, root2)
        (root2 / "tools" / "slop-lint" / "slop_lint" / "extra.py").unlink()
        (root2 / "tools" / "slop-lint" / "slop_lint" / "cli.py").unlink()
        r = run("--check", root=root2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cli.py", r.stderr)

    def test_check_fails_on_a_missing_or_invalid_manifest(self):
        root = self.copy_repo()
        man = root / "tools" / "slop-lint" / "vendor-manifest.json"
        good = json.loads(man.read_text())
        for bad in ({**good, "schema": "other/1"}, {**good, "source_commit": "main"},
                    {**good, "files": []}):
            man.write_text(json.dumps(bad))
            self.assertEqual(run("--check", root=root).returncode, 1, bad)
        man.write_text("{not json")
        self.assertEqual(run("--check", root=root).returncode, 1)
        man.unlink()
        self.assertEqual(run("--check", root=root).returncode, 1)

    def test_check_refuses_symlinked_files(self):
        root = self.copy_repo()
        f = root / "tools" / "slop-lint" / "slop_lint" / "rules.py"
        real = self.tmp / "rules.py"
        shutil.copy(f, real)
        f.unlink()
        f.symlink_to(real)
        self.assertEqual(run("--check", root=root).returncode, 1)

    # --- refresh and compare ------------------------------------------------

    def test_refresh_vendors_package_and_readme_and_records_the_commit(self):
        src, sha = self.make_source()
        root = self.tmp / "dest"
        (root / "tools" / "slop-lint" / "slop_lint").mkdir(parents=True)
        (root / "tools" / "slop-lint" / "slop_lint" / "stale.py").write_text("old\n")
        r = run("--refresh", "--source", str(src), root=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        vend = root / "tools" / "slop-lint"
        self.assertFalse((vend / "slop_lint" / "stale.py").exists())
        self.assertFalse((vend / "tests").exists())
        self.assertEqual((vend / "slop_lint" / "cli.py").read_text(), "# slop_lint/cli.py\n")
        m = json.loads((vend / "vendor-manifest.json").read_text())
        self.assertEqual(m["source_commit"], sha)
        self.assertEqual(sorted(e["path"] for e in m["files"]),
                         ["README.md", "slop_lint/__init__.py", "slop_lint/cli.py"])
        self.assertEqual(run("--check", root=root).returncode, 0)
        self.assertEqual(run("--compare", str(src), root=root).returncode, 0)

    def test_refresh_refuses_a_dirty_source(self):
        src, _ = self.make_source()
        (src / "tools" / "slop-lint" / "slop_lint" / "cli.py").write_text("# edited\n")
        root = self.tmp / "dest"
        root.mkdir()
        r = run("--refresh", "--source", str(src), root=root)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((root / "tools" / "slop-lint" / "vendor-manifest.json").exists())

    def test_compare_reports_drift_from_a_newer_source(self):
        src, _ = self.make_source()
        root = self.tmp / "dest"
        root.mkdir()
        self.assertEqual(run("--refresh", "--source", str(src), root=root).returncode, 0)
        (src / "tools" / "slop-lint" / "slop_lint" / "cli.py").write_text("# upstream moved\n")
        git(src, "commit", "-qam", "move")
        r = run("--compare", str(src), root=root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("slop_lint/cli.py", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
