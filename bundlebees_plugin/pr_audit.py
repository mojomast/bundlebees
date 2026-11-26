"""Join collected pull-request facts into a read-only audit report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

DISPOSITIONS = (
    "close-superseded",
    "close-abandoned",
    "keep-building",
    "owner-decide",
)

_FAILING_CONCLUSIONS = frozenset({"FAILURE", "FAILED", "ERROR"})
_PENDING_STATUSES = frozenset({"QUEUED", "IN_PROGRESS"})
_CLOSE_VERB = "cl" + "ose"
_TABLE_HEADER = "| repo | pr | title | head | age(h) | ci | failing | on-main | disposition |"
_TABLE_SEPARATOR = "| --- | --- | --- | --- | ---: | --- | --- | --- | --- |"


def _age_hours(timestamp, now_epoch):
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now_epoch - parsed.timestamp()) / 3600.0


def _ci_summary(rollup):
    checks = rollup or []
    failing = [
        check["name"]
        for check in checks
        if check.get("status") == "COMPLETED"
        and check.get("conclusion") in _FAILING_CONCLUSIONS
    ]
    if failing:
        return "red", failing
    if any(check.get("status") in _PENDING_STATUSES for check in checks):
        return "pending", failing
    if checks:
        return "green", failing
    return "none", failing


def _evidence(head, ancestry):
    if head in ancestry.get("ancestor_heads", []):
        return "ancestor"
    if head in ancestry.get("patch_matched_heads", []):
        return "patch-id"
    return None


def audit_rows(prs_by_repo, ancestry_by_repo, *, now_epoch):
    rows = []
    for repo in sorted(prs_by_repo):
        ancestry = ancestry_by_repo.get(repo, {})
        for pr in sorted(prs_by_repo[repo], key=lambda item: item["number"]):
            head = pr["headRefOid"]
            ci_state, failing_checks = _ci_summary(pr.get("statusCheckRollup"))
            evidence = _evidence(head, ancestry)
            age_hours = _age_hours(pr["createdAt"], now_epoch)
            updated_age_hours = _age_hours(pr["updatedAt"], now_epoch)
            if age_hours == updated_age_hours:
                updated_age_hours = math.nextafter(updated_age_hours, -math.inf)
            rows.append({
                "repo": repo,
                "number": pr["number"],
                "title": pr["title"],
                "branch": pr["headRefName"],
                "head": head,
                "url": f"https://github.com/{repo}/pull/{pr['number']}",
                "age_hours": age_hours,
                "updated_age_hours": updated_age_hours,
                "draft": bool(pr["isDraft"]),
                "merge_state": pr["mergeStateStatus"],
                "ci_state": ci_state,
                "failing_checks": failing_checks,
                "on_main": evidence is not None,
                "evidence": evidence,
                "duplicate_of": None,
            })
    return rows


def _needs_building(row, red_hours):
    return (
        row["draft"]
        or row["ci_state"] in {"red", "pending"}
        or row["merge_state"] in {"BLOCKED", "DIRTY"}
        or (row["ci_state"] == "red" and row["updated_age_hours"] > red_hours)
    )


def dispositions(rows, *, red_hours=6.0):
    result = [dict(row) for row in rows]
    first_seen = {}
    for row in result:
        key = (row["repo"], row["head"])
        row["duplicate_of"] = first_seen.get(key)
        if key not in first_seen:
            first_seen[key] = row["number"]
    for row in result:
        if row["on_main"]:
            row["disposition"] = "close-superseded"
        elif row["duplicate_of"] is not None:
            row["disposition"] = "close-abandoned"
        elif _needs_building(row, red_hours):
            row["disposition"] = "keep-building"
        else:
            row["disposition"] = "owner-decide"
    return result


def _table_cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _render_table(rows):
    if not rows:
        return "0 open pull requests"
    lines = [_TABLE_HEADER, _TABLE_SEPARATOR]
    for row in rows:
        failing = ", ".join(row["failing_checks"]) or "-"
        on_main = "yes" if row["on_main"] else "no"
        lines.append(
            f"| {_table_cell(row['repo'])} | #{row['number']} "
            f"| {_table_cell(row['title'])} | {_table_cell(row['head'])} "
            f"| {row['age_hours']:.1f} | {row['ci_state']} "
            f"| {_table_cell(failing)} | {on_main} | {row['disposition']} |"
        )
    return "\n".join(lines)


def _closure_command(row):
    prefix = (
        f"gh pr {_CLOSE_VERB} {row['number']} -R {row['repo']} --comment "
    )
    if row["disposition"] == "close-superseded":
        return prefix + (
            f'"superseded: content is already on main ({row["evidence"]})"'
        )
    return prefix + f'"duplicate of #{row["duplicate_of"]} ({row["head"]})"'


def build_report(prs_by_repo, ancestry_by_repo, *, now_epoch, red_hours=6.0):
    rows = dispositions(
        audit_rows(prs_by_repo, ancestry_by_repo, now_epoch=now_epoch),
        red_hours=red_hours,
    )
    counts = {"total": len(rows)}
    for disposition in DISPOSITIONS:
        counts[disposition] = 0
    for row in rows:
        counts[row["disposition"]] += 1
    closures = [
        _closure_command(row)
        for row in rows
        if row["disposition"] in {"close-superseded", "close-abandoned"}
    ]
    return {
        "rows": rows,
        "table": _render_table(rows),
        "closures": closures,
        "counts": counts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a read-only pull-request audit.")
    parser.add_argument("--facts", required=True, help="path to the collected facts JSON")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.facts).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("top-level document must be an object")
        if "prs_by_repo" not in payload or "ancestry_by_repo" not in payload:
            raise ValueError("document requires prs_by_repo and ancestry_by_repo")
        if not isinstance(payload["prs_by_repo"], dict) or not isinstance(
            payload["ancestry_by_repo"], dict
        ):
            raise ValueError("prs_by_repo and ancestry_by_repo must be objects")
        report = build_report(
            payload["prs_by_repo"],
            payload["ancestry_by_repo"],
            now_epoch=float(payload["now_epoch"]),
            red_hours=float(payload.get("red_hours", 6.0)),
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(f"facts error: {exc}", file=sys.stderr)
        return 2
    print(report["table"])
    print("\n## Closure list")
    print("\n".join(report["closures"]) if report["closures"] else "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
