"""The classify-only sweep recognises work that already landed under another name.

Hand check on moltussy-1 (Nov 19 2026, STATE.md "Sweep-flagged never-landed
candidates"): 8 of the 9 feat/fix/docs branches the sweep flagged as
"succeeded_unpushed_no_pr" had landed — through a retry branch ("-2"), a
coordinator apply that names the attempt id, a squash merge whose branch was
deleted, or a merged PR from the same branch. Only feat/hive-release-record
(att_f3c0051c) was a real gap.

AC-001: each of the 8 false positives is skipped with a landed_* reason and its evidence.
AC-002: the real gap (and any attempt without landed evidence) is still detected.
AC-003: matching is strict: retry suffix is exactly -N, ids need 8+ hex digits,
        branch rules need the attempt's own repository; a different repo's
        branch never hides a gap.
AC-004: an unreadable repository view never hides a gap (fail closed) and is reported.
AC-005: without a landed view the sweep behaves exactly as before.
AC-006: the driver's view reads merged PRs + commits on main through gh, read-only,
        and the driver passes it to the classify-only sweep.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import beat_reconciliation as br
from bundlebees_plugin import factory_driver

T0 = 1_800_000_000.0


def att(entry_id, branch, project):
    return {"attempt_id": entry_id, "state": "succeeded", "branch": branch,
            "finished_at": "2027-01-15T07:27:30Z", "brief": {"project": project}}


# Attempt ids from the hand check (short forms padded to full 16-hex ids).
FP = {
    "verify_refusal": att("att_55bbab04a1b2c3d4", "fix/verify-refusal-diagnostics", "bundlebees"),
    "payload_ceiling": att("att_af454bab0f0e0d0c", "fix/result-payload-ceiling", "bundlebees"),
    "lease_scope": att("att_835c154e11223344", "fix/patch-collect-lease-scope", "hyvv"),
    "decisions": att("att_295ff16255667788", "feat/snapshot-decisions-section", "workflow"),
    "memory_index": att("att_82b7644999aabbcc", "feat/memory-index", "workflow"),
    "r4_design": att("att_967e3d39ddeeff00", "docs/r4-run-command-design-v2", "hyvv"),
    "trial_harness": att("att_e1f3fb747b354685", "docs/hindsight-trial-harness", "workflow"),
    "trial_arms": att("att_6c5169e712345678", "docs/hindsight-trial-harness-arms", "workflow"),
}
GAP = att("att_f3c0051c35e248a8", "feat/hive-release-record", "orbitdesktop")

INDEXES = {
    "bundlebees": br.landed_index(
        prs=[
            # retry branch, body does not name the original attempt
            {"number": 102, "headRefName": "fix/verify-refusal-diagnostics-2", "title": "verify: refusal diagnostics",
             "body": "Retry of the verify refusal work (att_468bd852cafe0001)."},
            # retry branch whose body names the original attempt (coordinator-applied)
            {"number": 100, "headRefName": "fix/result-payload-ceiling-2", "title": "result payload ceiling",
             "body": "Coordinator-applied 15e4ee9. Supersedes att_af454bab (secret-scan refusal)."},
        ],
        commits=[{"sha": "647df9d" + "0" * 33, "message": "bundlebees: apply verified attempt att_a1c390bd7779419f (#178)"}],
    ),
    "hyvv": br.landed_index(
        prs=[{"number": 29, "headRefName": "docs/r4-v2-squash-src", "title": "docs: R4 run command design v2 (att_967e3d39)", "body": ""}],
        commits=[{"sha": "129191f2" + "0" * 32, "message": "Merge pull request #24 from mojomast/fix/patch-collect-lease-scope\n\nfix(hyvv): lease scope"}],
    ),
    "workflow": br.landed_index(
        prs=[
            {"number": 96, "headRefName": "feat/snapshot-decisions-section", "title": "snapshot decisions", "body": ""},
            {"number": 102, "headRefName": "feat/memory-index-impl", "title": "memory index", "body": "Verified attempt att_82b76449."},
            {"number": 82, "headRefName": "docs/trial-part1",
             "title": "PART 1 fixtures + evaluator + tests (att_e1f3fb747b354685, coordinator-reconciled)", "body": ""},
            {"number": 88, "headRefName": "docs/hindsight-trial-harness-arms", "title": "arms.py/report.py", "body": ""},
        ],
        commits=[],
    ),
    "orbitdesktop": br.landed_index(
        prs=[{"number": 48, "headRefName": "feat/hive-release-slot", "title": "Hive reports release slot", "body": ""}],
        commits=[{"sha": "a3332a44" + "0" * 32, "message": "Merge pull request #78 from mojomast/cleanup/main"}],
    ),
}


def run(tmp_path, entries, landed=INDEXES, origin=()):
    return br.sweep(tmp_path / "state", launcher=list(entries), origin_branches=list(origin), open_prs={},
                    processor=None, now=T0, record=False, claim=False, landed=landed)


def test_ac001_the_eight_false_positives_are_skipped_with_evidence(tmp_path):
    report = run(tmp_path, [*FP.values(), GAP])
    landed = {item["attempt_id"]: item for item in report["landed"]}
    assert set(landed) == {e["attempt_id"] for e in FP.values()}
    expect = {
        "verify_refusal": (br.LANDED_RETRY_BRANCH, "PR #102"),
        "payload_ceiling": (br.LANDED_ATTEMPT_ID, "PR #100"),
        "lease_scope": (br.LANDED_MERGED_BRANCH, "PR #24"),
        "decisions": (br.LANDED_MERGED_BRANCH, "PR #96"),
        "memory_index": (br.LANDED_ATTEMPT_ID, "PR #102"),
        "r4_design": (br.LANDED_ATTEMPT_ID, "PR #29"),
        "trial_harness": (br.LANDED_ATTEMPT_ID, "PR #82"),
        "trial_arms": (br.LANDED_MERGED_BRANCH, "PR #88"),
    }
    for key, (reason, where) in expect.items():
        item = landed[FP[key]["attempt_id"]]
        assert item["reason"] == reason, key
        assert item["evidence"].startswith(where), (key, item["evidence"])
        assert item["project"] == FP[key]["brief"]["project"]
    assert report["counts"]["landed"] == 8
    assert report["landed_view"] == "ok"
    for reason in br.LANDED_REASONS - {br.LANDED_RETRY_BRANCH}:
        assert report["counts"]["by_reason"].get(reason)


def test_ac002_the_real_gap_is_still_detected(tmp_path):
    report = run(tmp_path, [*FP.values(), GAP])
    assert [d["attempt_id"] for d in report["detected"]] == [GAP["attempt_id"]]
    assert report["detected"][0]["reason"] == br.REASON_UNPUSHED
    # Once a merged PR names it (e.g. a coordinator port), it stops being a gap.
    landed = dict(INDEXES, orbitdesktop=br.landed_index(prs=[{"number": 80, "headRefName": "feat/hive-release-record-2",
        "title": "Hive: wire the release reader (att_f3c0051c)", "body": ""}]))
    assert run(tmp_path, [GAP], landed=landed)["counts"]["detected"] == 0


@pytest.mark.parametrize("merged_head", ["feat/x-two", "feat/xy", "feat/x-2/y", "feat/x-0", "feat/x-123", "other/feat/x-2"])
def test_ac003_retry_suffix_is_strict(tmp_path, merged_head):
    index = {"p": br.landed_index(prs=[{"number": 5, "headRefName": merged_head, "title": "t", "body": ""}])}
    report = run(tmp_path, [att("att_0123456789abcdef", "feat/x", "p")], landed=index)
    assert report["counts"]["detected"] == 1, merged_head


def test_ac003_retry_suffixes_that_count(tmp_path):
    for head in ("feat/x-2", "feat/x-3", "feat/x-12"):
        index = {"p": br.landed_index(prs=[{"number": 5, "headRefName": head, "title": "t", "body": ""}])}
        assert run(tmp_path, [att("att_0123456789abcdef", "feat/x", "p")], landed=index)["landed"][0]["reason"] == br.LANDED_RETRY_BRANCH


def test_ac003_ids_need_eight_hex_digits_and_a_prefix_match(tmp_path):
    entry = att("att_55bbab04a1b2c3d4", "feat/none", "p")
    for body, hit in [("att_55bb", False), ("att_55bbab0", False), ("att_55bbab04", True), ("att_55bbab04a1b2c3d4", True),
                      ("att_55bbab05", False), ("xatt_55bbab04", False), ("att_55bbab04a1b2c3d4ff", False)]:
        index = {"p": br.landed_index(prs=[{"number": 9, "headRefName": "zzz", "title": "", "body": body}])}
        assert (run(tmp_path, [entry], landed=index)["counts"]["detected"] == 0) is hit, body


def test_ac003_branch_rules_need_the_attempts_own_repository(tmp_path):
    # Same branch merged in a different repo: still a gap.
    entry = att("att_0123456789abcdef", "feat/snapshot-decisions-section", "bundlebees")
    assert run(tmp_path, [entry])["counts"]["detected"] == 1
    # No project at all: only an id reference can match, never a branch.
    bare = {"attempt_id": "att_0123456789abcdef", "state": "succeeded", "branch": "feat/snapshot-decisions-section"}
    report = run(tmp_path, [bare, FP["decisions"]])
    assert [d["attempt_id"] for d in report["detected"]] == ["att_0123456789abcdef"]
    by_id = {"attempt_id": "att_82b7644999aabbcc", "state": "succeeded", "branch": "feat/whatever"}
    report = run(tmp_path, [FP["memory_index"], by_id])
    assert report["counts"]["detected"] == 0


def test_ac004_an_unreadable_repository_never_hides_a_gap(tmp_path):
    def view(project):
        if project == "workflow":
            raise RuntimeError("gh failed (1): HTTP 502 token=ghp_secretsecretsecretsecret")
        return INDEXES.get(project)
    report = run(tmp_path, [FP["decisions"], FP["lease_scope"]], landed=view)
    assert [d["attempt_id"] for d in report["detected"]] == [FP["decisions"]["attempt_id"]]
    assert report["landed_view"] == "partial"
    assert "workflow" in report["landed_errors"]
    assert "ghp_secret" not in json.dumps(report)
    def down(project):
        raise RuntimeError("offline")
    report = run(tmp_path, list(FP.values()), landed=down)
    assert report["counts"]["detected"] == 8 and report["landed_view"] == "unavailable"
    report = run(tmp_path, [FP["decisions"]], landed=lambda project: ["not", "a", "mapping"])
    assert report["counts"]["detected"] == 1


def test_ac004_recorded_and_origin_rules_are_unchanged(tmp_path):
    report = run(tmp_path, [FP["decisions"]], origin=["feat/snapshot-decisions-section"])
    assert report["counts"]["by_reason"] == {br.BRANCH_ON_ORIGIN: 1}
    assert report["landed"] == []


def test_ac005_without_a_landed_view_nothing_changes(tmp_path):
    report = br.sweep(tmp_path / "state", launcher=[*FP.values(), GAP], origin_branches=[], open_prs={},
                      processor=None, now=T0, record=False, claim=False)
    assert report["counts"]["detected"] == 9
    assert "landed" not in report and "landed_view" not in report


def test_ac006_driver_view_reads_merged_prs_and_main_commits(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/mojomast/bundlebees.git"], check=True)
    calls = []

    def gh(argv):
        calls.append(list(argv))
        if argv[0] == "pr":
            return [{"number": 88, "headRefName": "docs/hindsight-trial-harness-arms", "title": "arms", "body": ""}]
        return [{"sha": "1" * 40, "message": "workflow: apply verified attempt att_e1f3fb747b354685"}]

    view = factory_driver.landed_view_for(repo, gh_json=gh)
    index = view("workflow")
    assert calls[0][:4] == ["pr", "list", "--repo", "mojomast/workflow"]
    assert "--state" in calls[0] and calls[0][calls[0].index("--state") + 1] == "merged"
    assert calls[1][0] == "api" and calls[1][1].startswith("repos/mojomast/workflow/commits?sha=main")
    assert all(c[0] in ("pr", "api") and "--method" not in c and "-X" not in c for c in calls), "read-only gh calls only"
    assert br.landed_match(FP["trial_arms"]["attempt_id"], "docs/hindsight-trial-harness-arms", index)["reason"] == br.LANDED_MERGED_BRANCH
    assert br.landed_match(FP["trial_harness"]["attempt_id"], "x", index)["reason"] == br.LANDED_ATTEMPT_ID


def test_ac006_non_github_origin_fails_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://example.com/x/y.git"], check=True)
    view = factory_driver.landed_view_for(repo, gh_json=lambda argv: [])
    report = run(tmp_path, [FP["decisions"]], landed=view)
    assert report["counts"]["detected"] == 1 and report["landed_view"] == "unavailable"


def test_ac006_driver_passes_the_landed_view_to_the_sweep(tmp_path):
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"items": {}}), encoding="utf-8")

    class Launcher:
        def list_attempts(self):
            return [FP["trial_arms"], GAP]

    driver = factory_driver.FactoryDriver(tmp_path / "state", queue, startup=False, now=lambda: T0,
                                          sweep_views={"launcher": Launcher(), "origin_branches": [], "landed": INDEXES})
    result = driver._sweep_result()
    sweep = result["duties"]["sweep"] if "sweep" in result["duties"] else next(iter(result["duties"].values()))
    assert [d["attempt_id"] for d in sweep["detected"]] == [GAP["attempt_id"]]
    assert sweep["landed"][0]["attempt_id"] == FP["trial_arms"]["attempt_id"]
    assert result["mode"] == "classify_only"
