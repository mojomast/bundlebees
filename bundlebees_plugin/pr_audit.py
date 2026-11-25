"""PR-TRIAGE: read-only multi-repo pull-request audit (RED stub).

This module joins facts collected elsewhere — ``gh pr list --json`` rows and the
``git merge-base`` / patch-id results — into one audit table plus a list of
close COMMANDS for the coordinator to apply. It is read-only by construction:
no network, no subprocess, no mutation of a pull request.

Every public entry point is a stub in this RED commit; tests/test_pr_audit.py
is the approved specification and must not be edited by the implementer.
"""

from __future__ import annotations

DISPOSITIONS = (
    "close-superseded",
    "close-abandoned",
    "keep-building",
    "owner-decide",
)


def audit_rows(prs_by_repo, ancestry_by_repo, *, now_epoch):
    raise NotImplementedError("pr_audit.audit_rows")


def dispositions(rows, *, red_hours=6.0):
    raise NotImplementedError("pr_audit.dispositions")


def build_report(prs_by_repo, ancestry_by_repo, *, now_epoch, red_hours=6.0):
    raise NotImplementedError("pr_audit.build_report")


def main(argv=None):
    raise NotImplementedError("pr_audit.main")


if __name__ == "__main__":
    raise SystemExit(main())
