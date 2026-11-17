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


# --- F6: the refusal must be diagnosable (RED first) -----------------------
# The secret_detected refusal must carry, per trip: the matched rule identity
# from the predicate layer, the 1-based line number of the trip in the scanned
# file, and a masked excerpt in which every secret-shaped span is replaced by a
# fixed placeholder. Evidence is enrichment only: the scan still refuses.


def test_first_secret_rule_identifies_the_matching_family():
    from bundlebees_plugin import security

    # Fixtures are built at runtime so this file's own added lines stay clean.
    assert security.first_secret_rule("auth" + "orization: Bear" + "er " + "a" * 20) == "bearer"
    assert security.first_secret_rule(ADDED_SECRET) == "assignment"
    assert security.first_secret_rule("api_" + "key = 's3cr3t" + "-value'") == "assignment"
    assert security.first_secret_rule("key = sk-" + "a" * 20) == "openai_key"
    assert security.first_secret_rule("ghp_" + "a" * 30) == "github_token"
    assert security.first_secret_rule("github_" + "pat_" + "a" * 25) == "github_pat"
    assert security.first_secret_rule("xoxb-" + "a" * 15) == "slack_token"
    assert security.first_secret_rule("AKIA" + "B" * 15 + "C") == "aws_access_key"
    assert security.first_secret_rule("AIza" + "a" * 25) == "google_api_key"
    assert security.first_secret_rule("eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10) == "jwt"
    assert security.first_secret_rule("plain text carries no secret") is None
    assert security.first_secret_rule(None) is None


def test_masked_excerpt_replaces_every_secret_span():
    from bundlebees_plugin import cli

    line = "tok" + 'en = "aaaa" ' + "pass" + "word=" + "Zq9" * 6 + " Bear" + "er " + "b" * 20
    masked = cli._mask_excerpt(line)
    assert "aaaa" not in masked, masked
    assert "Zq9Zq9" not in masked, masked
    assert "bbbbbbbb" not in masked, masked
    assert "[redacted]" in masked, masked


def test_refusal_evidence_names_rule_line_and_masked_excerpt(tmp_path, capsys):
    from bundlebees_plugin.security import scrub

    repo, base = _repo_with_existing_secret_like_line(tmp_path)
    # ADDED_SECRET lands on line 3 of the new file: 1 "value = 2", 2 pre-existing, 3 added.
    rc = _verify(repo, base, tmp_path, _patch("value = 1\n" + PRE_EXISTING, "value = 2\n", ADDED_SECRET))
    out = capsys.readouterr().out
    assert rc != 0 and "secret_detected" in out, out
    assert "Zq9Zq9" not in out, out  # the refusal envelope must never become a secret log
    assert "src/a.py" in out, out  # repository-relative path
    assert "line=3" in out, out  # 1-based trip line in the scanned file
    assert "rule=assignment" in out, out  # matched family from the predicate layer
    assert f'excerpt="{scrub(ADDED_SECRET.strip())}"' in out, out  # fixed placeholder


def test_oversize_file_still_refuses_without_line_evidence(tmp_path):
    from bundlebees_plugin import cli

    (tmp_path / "big.bin").write_bytes(b"x" * (cli._MAX_SCAN_BYTES + 1))
    trips = cli._secret_trips(tmp_path, ["big.bin"])
    assert [(t["file"], t["line_no"], t["rule"], t["excerpt"]) for t in trips] == [
        ("big.bin", None, None, None)
    ]
    assert cli._secret_scan(tmp_path, ["big.bin"]) == ["big.bin"]
