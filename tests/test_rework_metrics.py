"""Approved acceptance tests for bundlebees_plugin.rework_metrics. Coordinator-written; do not edit.
The metrics are advisory: nothing here may make a verification fail."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import rework_metrics as rm


def C(sha, date, **files):
    return {"sha": sha, "date": date, "files": {k: tuple(v) for k, v in files.items()}}


def test_ac001_totals_added_and_removed():
    m = rm.churn([C("1", "2026-11-01T00:00:00Z", a=(10, 2)), C("2", "2026-11-02T00:00:00Z", b=(5, 5))])
    assert (m["lines_added"], m["lines_removed"]) == (15, 7)


def test_ac002_reedit_counts_lines_in_files_touched_again_within_the_window():
    commits = [C("1", "2026-11-01T00:00:00Z", a=(10, 0)),
               C("2", "2026-11-05T00:00:00Z", a=(3, 1)),      # within 14 days of commit 1 -> reedit 4
               C("3", "2026-12-20T00:00:00Z", a=(7, 0))]      # outside the window -> not a reedit
    assert rm.churn(commits, window_days=14)["reedited_lines"] == 4


def test_ac002_reedit_is_zero_when_each_file_is_touched_once():
    assert rm.churn([C("1", "2026-11-01T00:00:00Z", a=(1, 1)), C("2", "2026-11-02T00:00:00Z", b=(1, 1))])["reedited_lines"] == 0


def test_ac003_commit_order_in_the_input_does_not_matter():
    a = [C("1", "2026-11-01T00:00:00Z", a=(10, 0)), C("2", "2026-11-05T00:00:00Z", a=(3, 1))]
    assert rm.churn(a) == rm.churn(list(reversed(a)))


def test_ac004_retry_chains():
    events = [{"attempt_id": "a1", "retry_of": None}, {"attempt_id": "a2", "retry_of": "a1"},
              {"attempt_id": "a3", "retry_of": "a2"}, {"attempt_id": "b1", "retry_of": None}]
    r = rm.retry_chains(events)
    assert r["retries"] == 2 and r["max_chain"] == 3 and r["attempts"] == 4


def test_ac004_retry_cycles_and_dangling_parents_do_not_hang_or_crash():
    r = rm.retry_chains([{"attempt_id": "a", "retry_of": "b"}, {"attempt_id": "b", "retry_of": "a"},
                         {"attempt_id": "c", "retry_of": "ghost"}])
    assert r["attempts"] == 3 and r["max_chain"] >= 1


def test_ac005_review_findings_counts_rejected_review_records_per_contract():
    recs = [{"contract_id": "x", "phase": "review", "classification": "failed"},
            {"contract_id": "x", "phase": "review", "classification": "failed"},
            {"contract_id": "x", "phase": "review", "classification": "passed"},
            {"contract_id": "y", "phase": "green", "classification": "failed"}]
    assert rm.review_findings(recs) == {"x": 2}


def test_ac006_empty_and_malformed_input_never_raises_and_counts_skips():
    s = rm.summarize(commits=[], events=[], evidence=[])
    assert s["lines_added"] == 0 and s["retries"] == 0 and s["review_findings"] == 0 and s["skipped"] == 0
    s = rm.summarize(commits=[{"nope": 1}, None], events=[3], evidence=["x"])
    assert s["skipped"] == 4


def test_ac007_summary_has_the_fields_the_evidence_schema_will_carry():
    s = rm.summarize(commits=[C("1", "2026-11-01T00:00:00Z", a=(1, 2))], events=[], evidence=[])
    assert set(s) >= {"lines_added", "lines_removed", "reedited_lines", "attempts", "retries", "max_chain",
                      "review_findings", "skipped"}
    assert all(isinstance(v, int) and v >= 0 for v in s.values())


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(repo)})


def test_ac008_collect_git_reads_numstat_from_a_real_repo(tmp_path):
    repo = Path(tmp_path)
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("1\n2\n3\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "one")
    (repo / "a.txt").write_text("1\n2\nthree\nfour\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "two")
    commits = rm.collect_git(repo)
    assert len(commits) == 2
    m = rm.churn(commits)
    assert m["lines_added"] == 3 + 2 and m["lines_removed"] == 1
    assert m["reedited_lines"] == 3


def test_ac009_collect_git_on_a_non_repo_returns_empty_and_does_not_raise(tmp_path):
    assert rm.collect_git(Path(tmp_path)) == []
