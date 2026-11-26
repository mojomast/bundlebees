"""Shared fixtures for the base-merge review carry-forward predicate.

The lease/tamper gate (``mojomast/workflow``, vendored into ``mojomast/hyvv``)
and the merge lane (``mojomast/rizzstack``) both run these exact scenarios
against their own copy of the predicate.  This file is vendored
byte-identically; each test suite asserts its sha256, so a change to one copy
fails the other repository's contract test.

Usage::

    cases = build_all(tmpdir)   # [(name, review_sha, base, head, reason, ok)]
"""

import subprocess

SCENARIOS = (
    "base_merge_only",
    "two_base_merges",
    "authored_commit",
    "unrelated_merge",
    "content_change",
    "rewritten_away",
)

EXPECTED = {
    "base_merge_only": ("review_carry_ok", True),
    "two_base_merges": ("review_carry_ok", True),
    "authored_commit": ("review_carry_authored_commit", False),
    "unrelated_merge": ("review_carry_unrelated_merge", False),
    "content_change": ("review_carry_content_changed", False),
    "rewritten_away": ("review_not_ancestor", False),
}


def _git(repo, *args, check=True):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(
            "git %s failed: %s %s" % (" ".join(args), proc.stdout, proc.stderr)
        )
    return proc


def _write(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _new_repo(root, name):
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "carry@example.invalid")
    _git(repo, "config", "user.name", "carry fixtures")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def build(root, scenario):
    """Build ``scenario`` under ``root``; return ``(review_sha, base, head)``."""
    if scenario not in EXPECTED:
        raise KeyError(scenario)
    repo = _new_repo(root, scenario)
    _write(repo, "src/slice.py", "base\n")
    _write(repo, "src/base.py", "base\n")
    _write(repo, "tests/test_frozen.py", "base\n")
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    _write(repo, "src/slice.py", "reviewed\n")
    reviewed = _commit(repo, "implement the slice")

    def advance(name, text="base advance\n"):
        _git(repo, "checkout", "-q", "main")
        _write(repo, name, text)
        tip = _commit(repo, "main advances: " + name)
        _git(repo, "checkout", "-q", "feat/x")
        return tip

    def merge(ref="main", message="merge the base"):
        _git(repo, "merge", "--no-ff", "-m", message, ref)
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    if scenario == "base_merge_only":
        base = advance("src/base_only.py")
        return reviewed, base, merge()
    if scenario == "two_base_merges":
        advance("src/base_one.py")
        merge()
        base = advance("src/base_two.py")
        return reviewed, base, merge()
    if scenario == "authored_commit":
        _write(repo, "src/slice.py", "changed after the review\n")
        return reviewed, base, _commit(repo, "change it after the review")
    if scenario == "unrelated_merge":
        _git(repo, "checkout", "-q", "-b", "side", "main")
        _write(repo, "src/side.py", "side\n")
        _commit(repo, "side work")
        _git(repo, "checkout", "-q", "feat/x")
        return reviewed, base, merge("side", "merge side")
    if scenario == "content_change":
        _git(repo, "checkout", "-q", "main")
        _write(repo, "src/slice.py", "main side\n")
        base = _commit(repo, "main edits the same line")
        _git(repo, "checkout", "-q", "feat/x")
        conflicted = _git(repo, "merge", "main", check=False)
        if conflicted.returncode == 0:
            raise AssertionError("the content_change fixture must conflict")
        _write(repo, "src/slice.py", "resolution\n")
        return reviewed, base, _commit(repo, "resolve differently")

    _git(repo, "checkout", "-q", "-b", "stale", "main")
    _write(repo, "src/slice.py", "stale\n")
    stale = _commit(repo, "a reviewed variant")
    _git(repo, "checkout", "-q", "feat/x")
    _write(repo, "src/slice.py", "rewritten\n")
    return stale, base, _commit(repo, "rewrite the branch")


def build_all(root):
    """``[(scenario, review_sha, base, head, reason, ok)]`` for every fixture."""
    cases = []
    for scenario in SCENARIOS:
        review_sha, base, head = build(root, scenario)
        reason, ok = EXPECTED[scenario]
        cases.append((scenario, review_sha, base, head, reason, ok))
    return cases
