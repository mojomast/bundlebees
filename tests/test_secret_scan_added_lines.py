"""Approved acceptance tests (contract BI1-self-integration AC-007, RED first).

verify's secret scan checks only the lines a worker patch adds. Content that already exists on
source_rev, such as bundlebees' own ``print("token: ...")`` lines, must not block a patch that does not
touch it, while an added secret-like line still blocks with ``secret_detected``.
"""

from __future__ import annotations

from tests.test_cli_acdd import (ATTEMPT_ID, BRANCH, FakeClient, _ctx, _git, _invoke, _lease, _repo, _result)

# Built at runtime so the repository's own secret scanners do not flag this file.
PRE_EXISTING = "print(f\"tok" + "en: {'configured'}\")  # " + "secret" + ": documented\n"
ADDED_SECRET = "pass" + "word=" + "Zq9" * 6 + "\n"  # unquoted: the scanner's value pattern stops at quotes


def _repo_with_existing_secret_like_line(tmp_path):
    repo, _ = _repo(tmp_path)
    (repo / "src" / "a.py").write_text("value = 1\n" + PRE_EXISTING)
    _git(repo, "commit", "-qam", "pre-existing secret-like text")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "branch", "-f", BRANCH, "main")
    _git(repo, "push", "-q", "-f", "origin", BRANCH)
    _git(repo, "checkout", "-q", BRANCH)
    return repo, base


def _patch(base_text: str, new_first: str, extra: str = "") -> str:
    lines = base_text.splitlines(keepends=True)
    body = "".join(" " + line for line in lines[1:])
    added = "".join("+" + line for line in extra.splitlines(keepends=True))
    count_new = len(lines) + len(extra.splitlines())
    return (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        f"@@ -1,{len(lines)} +1,{count_new} @@\n-{lines[0]}+{new_first}{body}{added}"
    )


def _verify(repo, base, tmp_path, patch):
    state = tmp_path / "state"
    _lease(state, base, None)
    return _invoke(_ctx(FakeClient(_result(patch, base))),
                   ["verify", ATTEMPT_ID, "--workdir", str(repo), "--branch", BRANCH, "--state-dir", str(state), "--dry-run"])


def test_pre_existing_secret_like_text_does_not_block_an_unrelated_patch(tmp_path, capsys):
    repo, base = _repo_with_existing_secret_like_line(tmp_path)
    rc = _verify(repo, base, tmp_path, _patch("value = 1\n" + PRE_EXISTING, "value = 2\n"))
    out = capsys.readouterr().out
    assert "secret_detected" not in out, out
    assert rc == 0, out


def test_an_added_secret_like_line_still_blocks(tmp_path, capsys):
    repo, base = _repo_with_existing_secret_like_line(tmp_path)
    rc = _verify(repo, base, tmp_path, _patch("value = 1\n" + PRE_EXISTING, "value = 2\n", ADDED_SECRET))
    out = capsys.readouterr().out
    assert rc != 0 and "secret_detected" in out, out
    assert "Zq9Zq9" not in out
