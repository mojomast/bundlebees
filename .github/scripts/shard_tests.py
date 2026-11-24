#!/usr/bin/env python3
"""Split the pytest files under tests/ into N CI shards (standard library only).

    python3 .github/scripts/shard_tests.py <index> <total>   # print the files for shard <index> (1-based)
    python3 .github/scripts/shard_tests.py --check <total>   # verify the shards partition every test file

Every collectable test file lands in exactly one shard, so sharding never drops a test. The split is
deterministic. Files are balanced greedily by the seconds recorded in .github/test-weights.json; a file
missing from that table counts as the median weight, so a new test file still runs, only less balanced.
"""
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEIGHTS = ROOT / ".github" / "test-weights.json"


def test_files():
    """Every file pytest would collect under testpaths = ["tests"] with the default patterns."""
    found = {p for pat in ("test_*.py", "*_test.py") for p in (ROOT / "tests").rglob(pat)}
    return sorted(p.relative_to(ROOT).as_posix() for p in found if "__pycache__" not in p.parts)


def shards(total):
    files = test_files()
    table = json.loads(WEIGHTS.read_text(encoding="utf-8")) if WEIGHTS.exists() else {}
    known = [w for f, w in table.items() if f in files]
    default = statistics.median(known) if known else 1.0
    loads = [[0.0, i, []] for i in range(total)]
    for f in sorted(files, key=lambda f: (-table.get(f, default), f)):
        lightest = min(loads, key=lambda x: (x[0], x[1]))
        lightest[0] += table.get(f, default)
        lightest[2].append(f)
    return [sorted(x[2]) for x in loads]


def check(total):
    parts = shards(total)
    flat = [f for part in parts for f in part]
    if sorted(flat) != test_files() or len(flat) != len(set(flat)):
        sys.exit("shards do not partition the test files")
    if any(not part for part in parts):
        sys.exit("a shard is empty")
    print(f"{len(flat)} test files in {total} shards: " + ", ".join(str(len(p)) for p in parts))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        check(int(sys.argv[2]))
    elif len(sys.argv) == 3:
        index, total = int(sys.argv[1]), int(sys.argv[2])
        if not 1 <= index <= total:
            sys.exit("index must be in 1..total")
        print("\n".join(shards(total)[index - 1]))
    else:
        sys.exit(__doc__)
