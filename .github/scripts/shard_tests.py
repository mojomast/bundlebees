#!/usr/bin/env python3
"""Split the pytest files under tests/ into N CI shards (standard library only).

    python3 .github/scripts/shard_tests.py <index> <total>   # print the files for shard <index> (1-based)
    python3 .github/scripts/shard_tests.py --check <total>   # verify the shards partition every test file
    python3 .github/scripts/shard_tests.py --check <total> --against-pytest   # also compare with pytest's own collection

Every collectable test file lands in exactly one shard, so sharding never drops a test. The split is
deterministic. Files are balanced greedily by the seconds recorded in .github/test-weights.json; a file
missing from that table counts as the median weight, so a new test file still runs, only less balanced.
"""
import json
import re
import statistics
import subprocess
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


def _load_main_oracle_lock(main_ref):
    """Load oracle_lock from *main_ref*'s tree, never from the PR checkout.

    Bootstrap exception: until this PR lands, origin/main has no oracle_lock.py;
    only then does the loader fall back to the checkout's copy.
    """
    import importlib.util
    import tempfile

    shown = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{main_ref}:bundlebees_plugin/oracle_lock.py"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if shown.returncode == 0:
        source = shown.stdout
    else:
        sys.path.insert(0, str(ROOT))
        from bundlebees_plugin import oracle_lock as checkout_lock

        return checkout_lock
    handle = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    try:
        handle.write(source)
        handle.close()
        spec = importlib.util.spec_from_file_location(
            "bundlebees_main_oracle_lock", handle.name
        )
        if spec is None or spec.loader is None:
            raise ImportError("cannot load oracle_lock from main")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        Path(handle.name).unlink(missing_ok=True)


def pytest_files(main_ref=None):
    """The test files pytest itself collects for the whole suite (independent of test_files()).

    With *main_ref*, collection also runs under the oracle lock: main's pytest
    configuration and plugin allowlist, never the checkout's own config.
    """
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"]
    env = None
    if main_ref:
        oracle_lock = _load_main_oracle_lock(main_ref)

        lock = oracle_lock.from_git(ROOT, main_ref)
        argv = oracle_lock.locked_argv(lock, argv, rootdir=ROOT)
        env = oracle_lock.locked_env()
        env["PYTHONSAFEPATH"] = "1"
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
    if out.returncode not in (0, 5):
        sys.exit("pytest collection failed:\n" + out.stdout[-2000:] + out.stderr[-2000:])
    files = set()
    for line in out.stdout.splitlines():
        m = re.match(r"^([^\s:]+\.py)(::|: \d+$)", line.strip())
        if m:
            files.add(m.group(1))
    if not files:
        sys.exit("pytest collected no test files")
    return files


def check(total, against_pytest=False, main_ref=None):
    if against_pytest:
        missing = sorted(pytest_files(main_ref=main_ref) - set(test_files()))
        if missing:
            sys.exit("pytest collects files the shards would not run: " + ", ".join(missing))
    parts = shards(total)
    flat = [f for part in parts for f in part]
    if sorted(flat) != test_files() or len(flat) != len(set(flat)):
        sys.exit("shards do not partition the test files")
    if any(not part for part in parts):
        sys.exit("a shard is empty")
    print(f"{len(flat)} test files in {total} shards: " + ", ".join(str(len(p)) for p in parts))


if __name__ == "__main__":
    args = list(sys.argv[1:])
    main_ref = None
    if "--main-ref" in args:
        position = args.index("--main-ref")
        try:
            main_ref = args[position + 1]
        except IndexError:
            sys.exit("--main-ref needs a git ref")
        del args[position : position + 2]
    against_pytest = "--against-pytest" in args
    args = [argument for argument in args if argument != "--against-pytest"]
    if len(args) == 2 and args[0] == "--check":
        check(int(args[1]), against_pytest=against_pytest, main_ref=main_ref)
    elif len(args) == 2 and main_ref is None and not against_pytest:
        index, total = int(args[0]), int(args[1])
        if not 1 <= index <= total:
            sys.exit("index must be in 1..total")
        print("\n".join(shards(total)[index - 1]))
    else:
        sys.exit(__doc__)
