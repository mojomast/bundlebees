"""PR-TRIAGE: the read-only multi-repo pull-request audit.

The factory needs one table a coordinator can act on: every open pull request
across the campaign repos, with its head, age, CI state, failing checks, whether
its content is already on main, whether another open PR carries the same commit,
and one recommended disposition. The tool is READ-ONLY by construction: it
joins facts that were collected elsewhere (``gh pr list --json`` rows and
``git merge-base`` / patch-id results) and prints a table plus a closure list of
``gh pr close`` COMMANDS. It never runs them, never opens a socket and never
mutates a pull request.

Input shapes (both JSON-serialisable):

``prs_by_repo``
    ``{"<owner>/<repo>": [row, ...]}`` where each row is the subset of
    ``gh pr list --json`` this audit consumes: ``number``, ``title``,
    ``headRefName``, ``headRefOid``, ``createdAt``, ``updatedAt``,
    ``mergeStateStatus``, ``isDraft`` and ``statusCheckRollup`` (list of
    ``{"name", "status", "conclusion"}``).

``ancestry_by_repo``
    ``{"<owner>/<repo>": {"main_sha": "<40-hex>",
    "ancestor_heads": ["<head>", ...], "patch_matched_heads": ["<head>", ...]}}``
    — the measured facts: heads that are ancestors of main, and heads whose
    patch is already on main under another commit id.

Disposition rules, in this precedence order:

1. content already on main (ancestor or patch-id) -> ``close-superseded``
2. the same head commit is proposed by an older open PR -> ``close-abandoned``
3. a draft, red CI, or a BLOCKED/DIRTY merge state -> ``keep-building``
4. green, mergeable, not on main -> ``owner-decide``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import pr_audit

NOW = 1_800_000_000.0


def check(name="ci-gate", conclusion="SUCCESS", status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion}


def pr(number, *, head, created, updated=None, branch=None, checks=None, draft=False,
       merge_state="CLEAN", title=None):
    return {
        "number": number,
        "title": title or f"pull request {number}",
        "headRefName": branch or f"feat/pr-{number}",
        "headRefOid": head,
        "createdAt": created,
        "updatedAt": updated or created,
        "mergeStateStatus": merge_state,
        "isDraft": draft,
        "statusCheckRollup": checks if checks is not None else [check()],
    }


def facts(**over):
    base = {
        "mojomast/bundlebees": {
            "main_sha": "1" * 40,
            "ancestor_heads": [],
            "patch_matched_heads": [],
        }
    }
    base.update(over)
    return base


# -- AC-001: one normalised row per PR, ordered repo then number --------------


def test_every_open_pr_gets_one_row_ordered_by_repo_then_number() -> None:
    prs = {
        "mojomast/hyvv": [
            pr(70, head="a" * 40, created="2027-01-04T00:00:00Z"),
            pr(9, head="b" * 40, created="2027-01-01T00:00:00Z"),
        ],
        "mojomast/workflow": [pr(177, head="c" * 40, created="2027-01-02T00:00:00Z")],
    }

    rows = pr_audit.audit_rows(prs, facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []},
        "mojomast/workflow": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []},
    }), now_epoch=NOW)

    assert [(row["repo"], row["number"]) for row in rows] == [
        ("mojomast/hyvv", 9),
        ("mojomast/hyvv", 70),
        ("mojomast/workflow", 177),
    ]
    first = rows[0]
    assert first["head"] == "b" * 40
    assert first["branch"] == "feat/pr-9"
    assert first["url"] == "https://github.com/mojomast/hyvv/pull/9"
    assert first["ci_state"] == "green"
    assert first["failing_checks"] == []
    assert first["merge_state"] == "CLEAN"
    assert first["on_main"] is False
    assert first["age_hours"] > first["updated_age_hours"]


# -- AC-002: content on main is close-superseded ------------------------------


def test_a_head_that_is_an_ancestor_of_main_is_close_superseded() -> None:
    prs = {"mojomast/bundlebees": [pr(300, head="d" * 40, created="2027-01-01T00:00:00Z")]}
    ancestry = facts(**{
        "mojomast/bundlebees": {
            "main_sha": "1" * 40,
            "ancestor_heads": ["d" * 40],
            "patch_matched_heads": [],
        }
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["on_main"] is True
    assert rows[0]["evidence"] == "ancestor"
    assert rows[0]["disposition"] == "close-superseded"


def test_a_head_whose_patch_is_on_main_is_close_superseded_with_patch_evidence() -> None:
    prs = {"mojomast/bundlebees": [pr(301, head="e" * 40, created="2027-01-01T00:00:00Z")]}
    ancestry = facts(**{
        "mojomast/bundlebees": {
            "main_sha": "1" * 40,
            "ancestor_heads": [],
            "patch_matched_heads": ["e" * 40],
        }
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["evidence"] == "patch-id"
    assert rows[0]["disposition"] == "close-superseded"


# -- AC-003: the same head proposed twice -------------------------------------


def test_a_second_pr_with_the_same_head_is_close_abandoned() -> None:
    prs = {
        "mojomast/hyvv": [
            pr(65, head="f" * 40, created="2027-01-01T00:00:00Z"),
            pr(66, head="f" * 40, created="2027-01-03T00:00:00Z"),
        ]
    }
    ancestry = facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["disposition"] == "owner-decide"
    assert rows[0]["duplicate_of"] is None
    assert rows[1]["disposition"] == "close-abandoned"
    assert rows[1]["duplicate_of"] == 65


# -- AC-004: red or unmergeable work is never closed --------------------------


def test_a_red_pr_is_keep_building_and_keeps_its_failing_check_names() -> None:
    prs = {
        "mojomast/bundlebees": [
            pr(310, head="a" * 40, created="2027-01-01T00:00:00Z", checks=[
                check("slop-lint", "FAILURE"),
                check("tests (3/4)", "FAILURE"),
                check("gitleaks", "SUCCESS"),
            ]),
        ]
    }
    ancestry = facts(**{
        "mojomast/bundlebees": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["ci_state"] == "red"
    assert rows[0]["failing_checks"] == ["slop-lint", "tests (3/4)"]
    assert rows[0]["disposition"] == "keep-building"


def test_a_blocked_or_dirty_pr_is_keep_building_even_when_green() -> None:
    prs = {
        "mojomast/hyvv": [
            pr(70, head="b" * 40, created="2027-01-01T00:00:00Z", merge_state="DIRTY"),
            pr(71, head="c" * 40, created="2027-01-01T00:00:00Z", merge_state="BLOCKED"),
            pr(72, head="d" * 40, created="2027-01-01T00:00:00Z", draft=True),
        ]
    }
    ancestry = facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert [row["disposition"] for row in rows] == [
        "keep-building", "keep-building", "keep-building",
    ]


# -- AC-005: green + mergeable + not on main is the owner's call --------------


def test_a_green_mergeable_pr_not_on_main_is_owner_decide() -> None:
    prs = {"mojomast/hyvv": [pr(68, head="a" * 40, created="2027-01-01T00:00:00Z")]}
    ancestry = facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["disposition"] == "owner-decide"


def test_a_pending_check_is_keep_building_not_green() -> None:
    prs = {
        "mojomast/hyvv": [
            pr(79, head="a" * 40, created="2027-01-01T00:00:00Z", checks=[
                check("ci-gate", None, status="IN_PROGRESS"),
                check("gitleaks", "SUCCESS"),
            ]),
        ]
    }
    ancestry = facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })

    rows = pr_audit.dispositions(pr_audit.audit_rows(prs, ancestry, now_epoch=NOW))

    assert rows[0]["ci_state"] == "pending"
    assert rows[0]["disposition"] == "keep-building"


# -- AC-006: the closure list is commands, never executed ---------------------


def test_the_report_renders_a_table_and_a_closure_command_per_close_row() -> None:
    prs = {
        "mojomast/bundlebees": [
            pr(300, head="a" * 40, created="2027-01-01T00:00:00Z"),
            pr(310, head="b" * 40, created="2027-01-01T00:00:00Z", checks=[
                check("slop-lint", "FAILURE"),
            ]),
            pr(311, head="c" * 40, created="2027-01-01T00:00:00Z"),
        ]
    }
    ancestry = facts(**{
        "mojomast/bundlebees": {
            "main_sha": "1" * 40,
            "ancestor_heads": ["a" * 40],
            "patch_matched_heads": [],
        }
    })

    report = pr_audit.build_report(prs, ancestry, now_epoch=NOW)

    assert report["counts"] == {
        "total": 3,
        "close-superseded": 1,
        "close-abandoned": 0,
        "keep-building": 1,
        "owner-decide": 1,
    }
    assert len(report["rows"]) == 3
    table = report["table"]
    assert "| repo | pr |" in table
    assert "mojomast/bundlebees" in table
    assert "#310" in table
    assert report["closures"] == [
        'gh pr close 300 -R mojomast/bundlebees --comment '
        '"superseded: content is already on main (ancestor)"'
    ]


def test_the_module_is_read_only_by_construction() -> None:
    tree = pr_audit.__file__
    source = Path(tree).read_text(encoding="utf-8")
    for forbidden in (
        "import subprocess",
        "from subprocess",
        "import socket",
        "import urllib",
        "from urllib",
        "import http.client",
        "import requests",
        "os.system",
        "os.popen",
        "gh pr close",
        "gh pr merge",
    ):
        assert forbidden not in source, f"pr_audit must not contain {forbidden!r}"


# -- AC-007: the command-line entry point prints the report -------------------


def test_main_prints_the_report_and_exits_zero(tmp_path: Path, capsys) -> None:
    prs = {"mojomast/hyvv": [pr(70, head="a" * 40, created="2027-01-01T00:00:00Z")]}
    ancestry = facts(**{
        "mojomast/hyvv": {"main_sha": "1" * 40, "ancestor_heads": [], "patch_matched_heads": []}
    })
    payload = {"now_epoch": NOW, "prs_by_repo": prs, "ancestry_by_repo": ancestry}
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert pr_audit.main(["--facts", str(path)]) == 0
    out = capsys.readouterr().out
    assert "owner-decide" in out
    assert "| repo | pr |" in out


def test_main_refuses_an_unreadable_or_malformed_facts_file(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing.json"
    assert pr_audit.main(["--facts", str(missing)]) == 2
    assert "facts" in capsys.readouterr().err.lower()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert pr_audit.main(["--facts", str(broken)]) == 2
    assert "facts" in capsys.readouterr().err.lower()

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"prs_by_repo": []}), encoding="utf-8")
    assert pr_audit.main(["--facts", str(wrong)]) == 2


def test_an_empty_audit_says_so_and_emits_no_commands() -> None:
    report = pr_audit.build_report({}, {}, now_epoch=NOW)

    assert report["counts"]["total"] == 0
    assert report["closures"] == []
    assert "0 open pull requests" in report["table"]
