#!/usr/bin/env python3
"""Reproduce the five lease-tamper bypasses against a given ``lease_tamper.py``.

Each case builds a throwaway git repository with the exact history the bypass
needs and runs the gate on it.  The expected result depends on the gate:

* ``--expect bypass`` (the pre-fix gate): every numbered case must exit 0,
  because the pull request gets its own lease or its own verify boundary.
* ``--expect fail-closed`` (the fixed gate, the default): every numbered case
  must exit 1, and the control (a legitimate slice) must still exit 0.

The control is not optional: a gate that fails everything is not a fix.

Usage:
    python3 .github/scripts/lease_tamper_poc.py [--script PATH]
                                                [--expect bypass|fail-closed]
                                                [--json]

Exit status: 0 when every case matches the expectation, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CASES = (
    "0 body Lease-Verify: <head> moves the verify boundary to the head",
    "1 label lease-verify:<head> moves the verify boundary to the head",
    "2 the PR adds .rizzstack/lease.json on its own branch and is judged by it",
    "3 a base lease without review or contract_sha256 grants '*'",
    "4 a body Lease-Contract: line selects a broader base lease",
)


def git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit("git %s failed: %s%s" % (" ".join(args), proc.stdout, proc.stderr))
    return proc


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        git(self.path, "init", "-b", "main")
        git(self.path, "config", "user.email", "poc@example.invalid")
        git(self.path, "config", "user.name", "Lease Tamper PoC")
        git(self.path, "config", "commit.gpgsign", "false")

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message):
        git(self.path, "add", "-A")
        git(self.path, "commit", "-m", message)
        return self.rev()

    def rev(self, ref="HEAD"):
        return git(self.path, "rev-parse", ref).stdout.strip()

    def checkout(self, *args):
        git(self.path, "checkout", *args)


def contract_document(source_rev, assigned, test_files=()):
    return {
        "contract_version": "acdd/1",
        "id": "poc-slice",
        "source_rev": source_rev,
        "scope": {
            "assigned_files": list(assigned),
            "test_files": list(test_files),
            "prohibited": [],
            "read_only": [],
        },
        "review": {"status": "approved", "approved_by": "owner (poc fixture)"},
    }


def contract_hash(document):
    body = {key: value for key, value in document.items() if key != "review"}
    text = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def lease_document(document, sha, source_rev, branch):
    return {
        "id": "poc-slice",
        "lease_version": "rizzstack.lease/1",
        "contract_sha256": sha,
        "source_rev": source_rev,
        "branch": branch,
        "scope": {key: list(value) for key, value in document["scope"].items()},
        "review": {"status": "frozen", "approved_by": "coordinator (poc fixture)"},
    }


def land_contract_and_lease(repo, source_rev, branch, assigned=("src/slice.py",)):
    document = contract_document(source_rev, assigned)
    sha = contract_hash(document)
    repo.write(
        ".rizzstack/contracts/%s.json" % sha,
        json.dumps(document, indent=2, sort_keys=True) + "\n",
    )
    repo.write(
        ".rizzstack/leases/slice.json",
        json.dumps(lease_document(document, sha, source_rev, branch), indent=2) + "\n",
    )
    return repo.commit("land the approved contract and its lease on main")


def base_repo(root, branch="feat/poc"):
    repo = Repo(root)
    repo.write("src/slice.py", "base\n")
    repo.write("src/unleased.py", "base\n")
    source_rev = repo.commit("base")
    base = land_contract_and_lease(repo, source_rev, branch)
    return repo, base, source_rev


def case_control(root):
    """A legitimate slice: leased file changes after the contract's source_rev."""
    repo, base, source_rev = base_repo(root)
    repo.checkout("-b", "feat/poc")
    repo.write("src/slice.py", "implementation\n")
    head = repo.commit("implement the slice")
    return repo, base, head


def case_body_verify(root):
    """0: the body moves the boundary to the head, hiding a tamper."""
    repo, base, source_rev = base_repo(root)
    repo.checkout("-b", "feat/poc")
    repo.write("src/unleased.py", "tamper\n")
    head = repo.commit("tamper")
    return repo, base, head, {"body": "Lease-Verify: %s\n" % head}


def case_label_verify(root):
    """1: a label moves the boundary to the head, hiding a tamper."""
    repo, base, source_rev = base_repo(root)
    repo.checkout("-b", "feat/poc")
    repo.write("src/unleased.py", "tamper\n")
    head = repo.commit("tamper")
    return repo, base, head, {"labels": "lease-verify:%s" % head}


def case_head_lease_json(root):
    """2: the PR brings its own .rizzstack/lease.json granting everything."""
    repo = Repo(root)
    repo.write("src/slice.py", "base\n")
    repo.write("src/unleased.py", "base\n")
    source_rev = repo.commit("base")
    base = source_rev
    repo.checkout("-b", "feat/poc")
    repo.write(
        ".rizzstack/lease.json",
        json.dumps(
            {
                "source_rev": source_rev,
                "scope": {"assigned_files": ["*"], "test_files": []},
            },
            indent=2,
        )
        + "\n",
    )
    repo.write("src/unleased.py", "tamper\n")
    head = repo.commit("bring a lease and tamper")
    return repo, base, head


def case_self_issued_lease(root):
    """3: a self-issued base lease with no review and no contract hash."""
    repo = Repo(root)
    repo.write("src/slice.py", "base\n")
    repo.write("src/unleased.py", "base\n")
    source_rev = repo.commit("base")
    repo.write(
        ".rizzstack/leases/evil.json",
        json.dumps(
            {
                "id": "poc-self-issued",
                "lease_version": "rizzstack.lease/1",
                "branch": "feat/poc",
                "source_rev": source_rev,
                "scope": {"assigned_files": ["*"], "test_files": []},
            },
            indent=2,
        )
        + "\n",
    )
    base = repo.commit("land a self-issued lease on main")
    repo.checkout("-b", "feat/poc")
    repo.write("src/unleased.py", "tamper\n")
    head = repo.commit("tamper")
    return repo, base, head


def case_body_selects_broad_lease(root):
    """4: the body selects a broader base lease than the one that names the PR."""
    repo, base, source_rev = base_repo(root)
    repo.checkout("-b", "feat/poc")
    repo.checkout("main")
    broad = contract_document(source_rev, ["src/unleased.py"])
    broad["id"] = "poc-broad"
    sha = contract_hash(broad)
    repo.write(
        ".rizzstack/contracts/%s.json" % sha,
        json.dumps(broad, indent=2, sort_keys=True) + "\n",
    )
    repo.write(
        ".rizzstack/leases/broad.json",
        json.dumps(lease_document(broad, sha, source_rev, "feat/broad"), indent=2) + "\n",
    )
    base = repo.commit("land a broader lease for another branch")
    repo.checkout("feat/poc")
    repo.write("src/unleased.py", "tamper\n")
    head = repo.commit("tamper")
    return repo, base, head, {"body": "Lease-Contract: .rizzstack/leases/broad.json\n"}


BUILDERS = (
    case_body_verify,
    case_label_verify,
    case_head_lease_json,
    case_self_issued_lease,
    case_body_selects_broad_lease,
)


def run_gate(script, repo, base, head, extra):
    command = [
        sys.executable,
        str(script),
        "--repo",
        str(repo.path),
        "--base",
        base,
        "--head",
        head,
        "--branch",
        "feat/poc",
    ]
    if extra.get("labels"):
        command += ["--labels", extra["labels"]]
    if extra.get("body"):
        command += ["--body", extra["body"]]
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GH_TOKEN")
    }
    return subprocess.run(command, capture_output=True, text=True, env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reproduce the lease-tamper bypasses.")
    parser.add_argument(
        "--script",
        default=str(Path(__file__).resolve().with_name("lease_tamper.py")),
        help="lease_tamper.py to exercise (default: the one beside this script)",
    )
    parser.add_argument(
        "--expect",
        choices=("bypass", "fail-closed"),
        default="fail-closed",
        help="'bypass' expects every case to pass the gate (pre-fix); 'fail-closed' "
        "expects every case to be refused (fixed).",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--keep", action="store_true", help="keep the temp repositories")
    args = parser.parse_args(argv)

    script = Path(args.script).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="lease-tamper-poc-"))
    expected = 0 if args.expect == "bypass" else 1
    results = []
    failures = 0
    try:
        control = case_control(tmp / "control")
        proc = run_gate(script, control[0], control[1], control[2], {})
        results.append(
            {
                "case": "control",
                "what": "a legitimate slice (approved base lease, leased file changed)",
                "exit": proc.returncode,
                "expected": 0,
                "ok": proc.returncode == 0,
                "tail": (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""],
            }
        )
        if proc.returncode != 0:
            failures += 1

        for index, builder in enumerate(BUILDERS):
            built = builder(tmp / ("case-%d" % index))
            extra = built[3] if len(built) > 3 else {}
            proc = run_gate(script, built[0], built[1], built[2], extra)
            ok = proc.returncode == expected
            failures += 0 if ok else 1
            results.append(
                {
                    "case": index,
                    "what": CASES[index],
                    "exit": proc.returncode,
                    "expected": expected,
                    "ok": ok,
                    "tail": (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""],
                }
            )
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    if args.json:
        print(json.dumps({"script": str(script), "expect": args.expect, "results": results}, indent=2))
    else:
        print("script: %s" % script)
        print("expectation: %s" % args.expect)
        for item in results:
            label = "control" if item["case"] == "control" else "case %d" % item["case"]
            verdict = "OK" if item["ok"] else "MISMATCH"
            print("%-8s exit=%d expected=%d %-8s %s" % (label, item["exit"], item["expected"], verdict, item["what"]))
            print("         %s" % item["tail"][0])
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
