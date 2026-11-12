"""Approved tests for the acdd-lint command line (slice L3, written first, RED).

`python3 -m acdd_lint [PATH ...] [--schemas DIR] [--key-file FILE] [--json]`
walks the paths, finds acdd/1 contracts and tdd-evidence/1 records, runs the
schema and protocol checks, prints a clear report and exits 0 (clean),
1 (problems) or 2 (usage: bad path, schemas not found, bad key file).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.support import KEY, TOOL, frozen, green, red


def write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2), encoding="utf-8")
    return path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.c = frozen()
        write(self.tmp / "contracts" / "T1-tax.contract.json", self.c)
        write(self.tmp / "evidence" / "T1-tax" / "0001-red.json", red(self.c))
        write(self.tmp / "evidence" / "T1-tax" / "0002-green.json", green(self.c))
        write(self.tmp / "package.json", {"name": "not-acdd"})  # unrelated JSON is ignored

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        from acdd_lint import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main([str(a) for a in argv])
        return rc, out.getvalue(), err.getvalue()

    def test_clean_repo_exits_zero_with_a_summary(self):
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("1 contract", out)
        self.assertIn("2 evidence records", out)
        self.assertIn("0 problems", out)
        self.assertIn("signatures: not checked", out)

    def test_problems_exit_one_and_name_file_code_and_fix(self):
        bad = frozen()
        bad["acceptance_criteria"][0]["tests"] = []
        write(self.tmp / "contracts" / "T1-tax.contract.json", bad)
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("ac_untested", out)
        self.assertIn("T1-tax.contract.json", out)
        self.assertIn("contract_changed", out)  # editing after freezing is caught too

    def test_schema_errors_are_reported(self):
        c = frozen()
        del c["outcome"]
        write(self.tmp / "contracts" / "T2.contract.json", {**c, "id": "T2"})
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("contract_invalid", out)
        write(self.tmp / "evidence" / "T1-tax" / "0003-green.json", {"evidence_version": "tdd-evidence/1", "phase": "green"})
        rc, out, _ = self.run_cli(self.tmp)
        self.assertIn("evidence_invalid", out)

    def test_history_problems_are_reported(self):
        os.remove(self.tmp / "evidence" / "T1-tax" / "0001-red.json")
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("red_missing", out)

    def test_key_file_checks_signatures(self):
        key = write(self.tmp / "k.hex", KEY.hex() + "\n")
        rc, out, _ = self.run_cli(self.tmp, "--key-file", key)
        self.assertEqual(rc, 0, out)
        self.assertIn("signatures: checked", out)
        raw = self.tmp / "k.bin"
        raw.write_bytes(KEY)
        self.assertEqual(self.run_cli(self.tmp, "--key-file", raw)[0], 0)
        forged = green(self.c)
        forged["result"]["tests"] = 9
        write(self.tmp / "evidence" / "T1-tax" / "0002-green.json", forged)
        rc, out, _ = self.run_cli(self.tmp, "--key-file", key)
        self.assertEqual(rc, 1)
        self.assertIn("evidence_tampered", out)
        self.assertNotIn(KEY.hex(), out)

    def test_json_output(self):
        rc, out, _ = self.run_cli(self.tmp, "--json")
        data = json.loads(out)
        self.assertEqual(rc, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["contracts"], 1)
        self.assertEqual(data["evidence"], 2)
        self.assertEqual(data["problems"], [])
        self.assertIn("signatures", data["unrun"])
        os.remove(self.tmp / "evidence" / "T1-tax" / "0001-red.json")
        data = json.loads(self.run_cli(self.tmp, "--json")[1])
        self.assertFalse(data["ok"])
        self.assertEqual({"code", "file", "where", "message"}, set(data["problems"][0]))

    def test_usage_errors_exit_two(self):
        self.assertEqual(self.run_cli(self.tmp / "missing")[0], 2)
        self.assertEqual(self.run_cli(self.tmp, "--schemas", self.tmp)[0], 2)
        bad = write(self.tmp / "short.key", "abc")
        self.assertEqual(self.run_cli(self.tmp, "--key-file", bad)[0], 2)

    def test_unsafe_and_huge_files_are_skipped(self):
        (self.tmp / "evidence" / "T1-tax" / "0009-link.json").symlink_to(self.tmp / "evidence" / "T1-tax" / "0002-green.json")
        write(self.tmp / ".git" / "x.contract.json", "{not json")
        write(self.tmp / "node_modules" / "y.contract.json", "{not json")
        write(self.tmp / "big.json", "[" + "0," * 600000 + "0]")
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 0, out)
        self.assertIn("2 evidence records", out)

    def test_broken_contract_json_is_a_problem(self):
        write(self.tmp / "contracts" / "broken.contract.json", "{not json")
        rc, out, _ = self.run_cli(self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("json_invalid", out)

    def test_module_entry_point_runs_with_python3(self):
        proc = subprocess.run([sys.executable, "-m", "acdd_lint", str(self.tmp)], cwd=str(TOOL), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("acdd-lint", proc.stdout)


if __name__ == "__main__":
    unittest.main()
