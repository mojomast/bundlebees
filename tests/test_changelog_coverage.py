"""Offline coverage guard: every merged campaign PR since #43 must be documented.

Owner standing rule (docs-sync, §66): when a campaign feature PR merges into
main, the ``[0.2.0] - Unreleased`` section of ``CHANGELOG.md`` must document
it — user-facing wording about what changed, not a PR-number list. This test
parses ``CHANGELOG.md`` and asserts discoverable coverage per merged PR via a
keyword check drawn from what the PR actually changed (verified against the
merged diffs on main), so the gap class of "merged but undocumented" is
caught mechanically the next time it happens.

Mechanics: the merge history is read from this checkout's git log when it
carries real merge commits (``Merge pull request #N from ...``). A merged PR
number must have a registry row here — or an explicit exclusion with a
reason — and every row's keywords must appear in the Unreleased section. When
the checkout's history is squashed (a snapshot without merge commits), the
pinned snapshot set below stands in for the log. Either way, the next
campaign PR that merges without a row (or whose row's keywords are absent
from the changelog) fails this test. No network, launcher, token, Docker or
SSH access.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_MD = ROOT / "CHANGELOG.md"

#: The first PR the docs-sync duty applies to: everything merged since #43
#: must be discoverable in the Unreleased section (earlier PRs shipped in
#: released sections).
FIRST_COVERED_PR = 43

#: PRs merged into main since #43 that are not campaign features and carry no
#: changelog duty. Every exclusion states why; a merge number missing from
#: both the registry and this mapping is a test failure.
EXCLUDED_PRS: dict[int, str] = {
    46: "docs/changelog-43-44 — the changelog update itself, not a campaign feature",
    50: "docs/changelog-post46 - the changelog update itself, not a campaign feature",
}

#: The merge numbers verified against main's first-parent history
#: (b0cfecd #43, c6fa4a7 #44, 0416fac #47, 1e6adf8 #48, 4be4e7b #45,
#: 3eef3b5 #49) for checkouts whose log carries no merge commits. Only a
#: fallback: real history always wins.
SNAPSHOT_MERGED_PRS: tuple[int, ...] = (43, 44, 45, 47, 48, 49)

MERGE_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) from ", re.MULTILINE)

#: One row per merged campaign PR since #43. Keywords are exact substrings a
#: faithful Unreleased entry contains; they name what the merged diff actually
#: introduced, not the PR number. Extend this registry when the next campaign
#: PR merges.
CAMPAIGN_PRS: tuple[dict[str, object], ...] = (
    {
        # fix/provision-reconcile-repost: one idempotent re-POST on a
        # host-confirmed receipt_unknown; ProvisionUncertain stays read-only;
        # a definitive refusal is terminal; the first-POST refusal is persisted.
        "pr": 43,
        "name": "provision-reconcile recovery",
        "keywords": ("idempotent re-POST", "ProvisionUncertain", "first-POST refusal"),
    },
    {
        # fix/session-audit-reliability: one configured runner over the
        # existing sandbox, receipt identity over the full execution policy,
        # declared scanner discovery, structured refusals, CLI flags for the
        # sealed recovery ceiling.
        "pr": 44,
        "name": "verifier/session reliability",
        "keywords": ("configured runner", "receipt identity", "gitleaks_path"),
    },
    {
        # feat/git-pr-observer: read-only Git/PR campaign-drift observer
        # (workflow-F3) — reconcile.campaign_git_drift / git_observer, drift
        # kinds for branch and PR state, strictly report-only, scrubbed.
        "pr": 45,
        "name": "read-only Git/PR drift observer",
        "keywords": (
            "campaign_git_drift",
            "GitObserverError",
            "branch_moved",
            "unexpected_pr_head",
            "report-only",
            "`scrub`",
        ),
    },
    {
        # fix/queue-refusal-reporting: queue runs report typed provisioning
        # refusals instead of crashing, and the host's typed 409 code
        # (quota_exhausted) survives the authenticated transport.
        "pr": 47,
        "name": "queue refusal reporting + typed quota_exhausted",
        "keywords": (
            "instead of crashing",
            "`ProvisionBindError`",
            "`queue.run_once`",
            "quota_exhausted",
        ),
    },
    {
        # feat/regression-attribution: causal regression attribution — the
        # offending attempt named from lease coverage of the failing files,
        # or no attempt named with the reason; recovery-ready failure record.
        "pr": 48,
        "name": "causal regression attribution",
        "keywords": (
            "offending attempt",
            "wave_rejected",
            "combined_suite_failed",
            "failed_tests",
            "survives supersession",
        ),
    },
    {
        # docs/w6-completion-design: design document only — the native,
        # durable bee-completion signal (bundlebees_attempt op=await).
        "pr": 49,
        "name": "W6 native completion design document",
        "keywords": (
            "w6-native-completion-design.md",
            "op=await",
            "bee-completion",
            "10-minute heartbeat",
        ),
    },
    {
        # fix/integrated-not-pr-merged-detection: the read-only observer
        # classifies a PR whose exact head is already an ancestor of
        # authoritative main while the PR metadata still reports open, and the
        # reconciliation report stops the wait and bans re-integration.
        "pr": 51,
        "name": "integrated_not_pr_merged detection",
        "keywords": (
            "integrated_not_pr_merged",
            "reconciliation report",
            "bans re-integration",
        ),
    },
    {
        # feat/cli-attempts-status: read-only attempts/status CLI subcommands
        # for the observatory fleet view (M1 packet 1).
        "pr": 52,
        "name": "attempts and status CLI",
        "keywords": (
            "attempts and status CLI",
            "bee cap",
        ),
    },
    {
        # feat/coordinator-action-queue: durable per-item coordinator action
        # queue with fencing values, stale refusals, idempotent replay and an
        # AST guard proving it never executes anything.
        "pr": 54,
        "name": "coordinator action queue",
        "keywords": (
            "coordinator action queue",
            "fence_seq",
            "idempotent replay",
            "stale-owner adoption",
            "never executes",
        ),
    },
    {
        # feat/rizzstack-doctor: the rizzstack doctor subcommand - stack parity
        # and health diagnostic across checkout, origin main, installed package,
        # gateway-loaded plugin and deployed runtime revisions.
        "pr": 53,
        "name": "rizzstack doctor",
        "keywords": (
            "rizzstack doctor",
            "check-parity",
            "gateway-loaded plugin",
            "incompatible",
        ),
    },
)


def _read_changelog() -> str:
    return CHANGELOG_MD.read_text(encoding="utf-8")


def _unreleased_section() -> str:
    match = re.search(
        r"(?ms)^## \[0\.2\.0\] - Unreleased\s*$\n(.*?)(?=^## \[|\Z)",
        _read_changelog(),
    )
    assert match is not None, "CHANGELOG.md must keep a '## [0.2.0] - Unreleased' section"
    return match.group(1)


def _merged_pr_numbers() -> tuple[int, ...] | None:
    """PR numbers merged in this checkout's history, or ``None`` if unknowable.

    Reads only the local git log (no network). A squashed snapshot without
    merge commits returns ``None`` and the pinned fallback applies.
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--merges", "--pretty=%s"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    numbers = sorted({int(match) for match in MERGE_SUBJECT_RE.findall(result.stdout)})
    return tuple(numbers) or None


def _required_pr_numbers() -> tuple[int, ...]:
    merged = _merged_pr_numbers()
    if merged is None:
        merged = SNAPSHOT_MERGED_PRS
    return tuple(n for n in merged if n >= FIRST_COVERED_PR)


def test_registry_covers_every_merged_campaign_pr_since_43() -> None:
    required = _required_pr_numbers()
    assert required, "the merge history (or its snapshot fallback) must be non-empty"
    registered = {int(entry["pr"]) for entry in CAMPAIGN_PRS}
    unregistered = [
        n for n in required
        if n not in registered and n not in EXCLUDED_PRS
    ]
    assert not unregistered, (
        f"merged campaign PR(s) {unregistered} have no coverage registry entry: "
        "add a CAMPAIGN_PRS row whose keywords the changelog must contain, "
        "or an explicit EXCLUDED_PRS reason for a non-feature merge "
        "(docs-sync duty: merged work must be discoverable in the changelog)"
    )
    for number, reason in EXCLUDED_PRS.items():
        assert reason.strip(), f"exclusion for PR #{number} must state why"


def test_registry_rows_are_well_formed() -> None:
    numbers = [int(entry["pr"]) for entry in CAMPAIGN_PRS]
    assert len(numbers) == len(set(numbers)), "CAMPAIGN_PRS must not repeat a PR number"
    for entry in CAMPAIGN_PRS:
        assert int(entry["pr"]) >= FIRST_COVERED_PR, entry
        assert str(entry["name"]).strip(), entry
        keywords = tuple(entry["keywords"])
        assert len(keywords) >= 2, f"PR #{entry['pr']} needs at least two keywords"
        assert all(isinstance(k, str) and k.strip() for k in keywords), entry


@pytest.mark.parametrize("entry", CAMPAIGN_PRS, ids=lambda entry: f"pr-{entry['pr']}")
def test_merged_campaign_pr_has_discoverable_changelog_coverage(
    entry: dict[str, object],
) -> None:
    section = _unreleased_section()
    missing = [keyword for keyword in entry["keywords"] if keyword not in section]
    assert not missing, (
        f"[0.2.0] - Unreleased must document PR #{entry['pr']} "
        f"({entry['name']}): missing keyword(s) {missing}. The Unreleased "
        "section must describe what the merged PR changed, in user-facing "
        "wording — not a bare PR-number list."
    )
