#!/usr/bin/env python3
"""Check that a workflow file has a ci-gate job that needs every other job.

The repository ruleset requires the single `ci-gate` check, so a job that is
missing from its `needs:` list could fail without blocking a merge. This
script fails closed: no ci-gate job, a missing dependency, a missing
`if: always()` or a missing result check is an error.

Standard library only (no PyYAML on the runner). It reads the two-space
indented layout GitHub workflow files use.

Usage: python3 .github/scripts/check_ci_gate.py .github/workflows/ci.yml
"""
import re
import sys

JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(#.*)?$")
GATE = "ci-gate"


def job_blocks(lines):
    """Return {job_id: [lines]} for the top-level `jobs:` mapping."""
    jobs, current, inside = {}, None, False
    for line in lines:
        if re.match(r"^jobs:\s*$", line):
            inside = True
            continue
        if inside and re.match(r"^\S", line):
            inside = False
        if not inside:
            continue
        match = JOB_KEY.match(line)
        if match:
            current = match.group(1)
            jobs[current] = []
        elif current:
            jobs[current].append(line)
    return jobs


def gate_needs(block):
    """Return the job ids listed under the gate's `needs:` key."""
    needs = []
    for index, line in enumerate(block):
        match = re.match(r"^    needs:\s*(.*)$", line)
        if not match:
            continue
        inline = match.group(1).split("#")[0].strip()
        if inline.startswith("["):
            return [item.strip() for item in inline.strip("[]").split(",") if item.strip()]
        if inline:
            return [inline]
        for item in block[index + 1:]:
            entry = re.match(r"^      - ([A-Za-z0-9_-]+)\s*(#.*)?$", item)
            if not entry:
                break
            needs.append(entry.group(1))
    return needs


def problems(path):
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    jobs = job_blocks(lines)
    if GATE not in jobs:
        return [f"{path}: no '{GATE}' job"]
    block = jobs[GATE]
    errors = []
    missing = sorted(set(jobs) - {GATE} - set(gate_needs(block)))
    if missing:
        errors.append(f"{path}: '{GATE}' does not need: {', '.join(missing)}")
    text = "\n".join(block)
    if not re.search(r"^    name:\s*['\"]?ci-gate['\"]?\s*$", text, re.M):
        errors.append(f"{path}: '{GATE}' job must be named ci-gate (the required check name)")
    if not re.search(r"^    if:\s*\$\{\{\s*always\(\)\s*\}\}\s*$", text, re.M):
        errors.append(f"{path}: '{GATE}' must run with if: ${{{{ always() }}}}")
    if "toJSON(needs)" not in text:
        errors.append(f"{path}: '{GATE}' must check every result in toJSON(needs)")
    return errors


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    errors = [error for path in argv[1:] for error in problems(path)]
    for error in errors:
        print(f"FAIL {error}")
    if not errors:
        print("OK ci-gate needs every job in: " + ", ".join(argv[1:]))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
