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
#: 3eef3b5 #49) plus the B18 docs-sync companion #104, the verify-output
#: literal redaction #110 (merged at 2c14cc5), the research-before-retry gate
#: #107, kanban board sync #108, the factory-hook bridge #105, the durable
#: completion inbox hand-off #106, factory service mode #109, board-stock
#: comment-on-change #111 and the #114 docs-sync companion #113, for checkouts
#: whose log carries no merge commits. Only a fallback: real history always
#: wins.
SNAPSHOT_MERGED_PRS: tuple[int, ...] = (
    43,
    44,
    45,
    47,
    48,
    49,
    104,
    105,
    106,
    107,
    108,
    109,
    110,
    111,
    113,
)

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
    {
        # feat/doctor-launcher-identity: the doctor reports the running
        # launcher's build identity in three evidence tiers and classifies a
        # read-only capabilities probe; a version string without a deployment
        # receipt is always flagged (owner directive 2026-11-14).
        "pr": 57,
        "name": "doctor launcher build identity",
        "keywords": (
            "launcher build-identity",
            "evidence tiers",
            "not deployment evidence",
            "provision/capabilities",
        ),
    },
    {
        # fix/cli-attempt-scrub-bypass-and-probe-docs: a secret-shaped attempt
        # id keeps its scrubbed form (narrow scrub bypass closed with a
        # regression test); the status token probe is documented as exactly one
        # presence-only secret-store read.
        "pr": 55,
        "name": "CLI scrub-bypass fix + status token probe docs",
        "keywords": (
            "scrubbed form",
            "scrub bypass",
            "launcher-token presence probe",
        ),
    },
    {
        # feat/event-consumer-actions-r2: the landing-event consumer composes
        # the coordinator action queue through an explicit adapter seam at an
        # isolated directory; exactly-once via acks, create-only keys and a
        # completion ledger; fenced claims, bounded failure modes.
        "pr": 58,
        "name": "completion events drive the coordinator action queue",
        "keywords": (
            "consume_completions",
            "completion ledger",
            "stale_fence",
            "requeue_failed",
        ),
    },
    {
        # feat/lease-payload-pinned-sources: the staging half - payload sources
        # copied into the sandbox under .bundlebees-payload/ with relative
        # naming, env transport, byte identity; masking skip is measurement-gated.
        "pr": 59,
        "name": "lease payload staging for sandboxes",
        "keywords": (
            "byte identity",
            ".bundlebees-payload",
            "measurement-gated",
        ),
    },
    {
        # feat/gate-watcher-planned-to-queued: backlog-driven gate watcher -
        # planned-to-queued transitions with gate evidence, lane budget clamp,
        # owner-gated slices never auto-dispatched, stale gates block.
        "pr": 61,
        "name": "gate-watcher planned-to-queued",
        "keywords": (
            "bundlebees.gate-backlog/1",
            "planned-to-queued",
            "owner-gated",
            "5-7 lanes",
        ),
    },
    {
        # fix/attempt-reconciliation-sweep: first-class beat reconciliation over
        # terminal attempts; durable ledger, skips.json, last-sweep.json.
        "pr": 62,
        "name": "attempt reconciliation sweep",
        "keywords": (
            "attempt-reconciliation/1",
            "skips.json",
            "last-sweep.json",
        ),
    },
    {
        # feat/graceful-recovery: graceful/ state directory - stream, digest,
        # resume brief; durable coordinator claim with TTL, lock, release log.
        "pr": 63,
        "name": "graceful recovery records",
        "keywords": (
            "stream.jsonl",
            "resume.json",
            "beat.claim.json",
        ),
    },
    {
        # feat/verify-workers-shadow: parallel verify workers behind the
        # durable verify_workers setting - in-beat (default), shadow, on.
        "pr": 64,
        "name": "parallel verify workers (shadow first)",
        "keywords": (
            "verify_workers",
            "verify-workers-shadow/1",
            "in-beat",
        ),
    },
    {
        # feat/contract-gate (supersedes #60): the require_contract dispatch
        # gate in contract_gate.py - durable settings, ContractRefusal.
        "pr": 65,
        "name": "require-contract dispatch gate",
        "keywords": (
            "contract_gate.py",
            "ContractRefusal",
            "require_contract",
        ),
    },
    {
        # feat/serve-lease-payload: the payload digest manifest the coordinator
        # serves is recorded into the brief at dispatch and bound by verify
        # byte for byte; every runner-driven sandbox run serves the payload.
        "pr": 66,
        "name": "lease payload served on every run",
        "keywords": (
            "payload digest",
            "PayloadError",
            "--commit-local",
        ),
    },
    {
        # feat/contract-followups: the F5 regression net for the d8c2356
        # import-alias defect class plus settings precedence and scaffold
        # lease/refusal followups.
        "pr": 67,
        "name": "contract-enforcement regression net",
        "keywords": (
            "import-identity",
            "explicit `false`",
            "scaffold_invalid",
            "directory-style",
        ),
    },
    {
        # feat/tool-path-write-back: terminal-state write-back so tool-path
        # attempts record real terminal states instead of staying unknown.
        "pr": 68,
        "name": "tool-path terminal-state write-back",
        "keywords": ("terminal states", "unknown", "agent_consumer"),
    },
    {
        # feat/event-consumer-actions-r2: record_tool_completion wired into
        # tools.py's terminal transitions (best-effort, duplicate-safe).
        "pr": 69,
        "name": "completion events wired into terminal transitions",
        "keywords": ("record_tool_completion", "duplicate-terminal-safe", "best-effort"),
    },
    {
        # rails/r02-ledger: RAILS-R02 usage-ledger contract rails in
        # contrib/rizzstack_rails (att_f67c7573 r3, spec @ workflow 9850efa).
        "pr": 70,
        "name": "rizzstack-rails usage-ledger contract rails",
        "keywords": ("contrib/rizzstack_rails", "vendor-manifest", "test_rails_ledger"),
    },
    {
        # feat/verify-binary-payload: binary worker payloads (att_9c70bf84)
        # verified end-to-end - envelope marker bundlebees-binary-payload/1,
        # fail-closed adapter validation, lossless git-base85 patch apply.
        "pr": 72,
        "name": "binary payload verification",
        "keywords": ("binary-payload", "base85", "fail-closed"),
    },
    {
        # fix/secret-predicate-value-shape: scrub predicate value-shape
        # repair (att_9b81c201) - tight k=v + joined-fragment overmatch,
        # mandated negative-control exemption; test_client.py unmodified.
        "pr": 71,
        "name": "scrub predicate value-shape repair",
        "keywords": ("value-shape", "negative-control", "test_security"),
    },
    {
        # fix/verify-refusal-evidence: verify secret-scan refusal evidence
        # (att_6b546cbf) - rule id + 1-based line + masked excerpt, enrichment
        # only on the unchanged fail-closed secret_detected decision; the
        # uv.lock suite side-effect was excluded from the delivered delta
        # (coordinator-apply split, disclosed in the commit and PR bodies).
        "pr": 73,
        "name": "verify refusal evidence enrichment",
        "keywords": ("refusal evidence", "masked excerpt", "rule id"),
    },
    {
        # fix/gw-landed-delivery-gate: gate-watcher dependency evidence must
        # require an origin-verified landed delivery (att_036f1bcd) - the
        # defect-f fix after six zero-delivery succeeded attempts falsely
        # cleared dependency edges; fail-closed origin_landed probe.
        "pr": 74,
        "name": "gate-watcher landed-delivery gate",
        "keywords": ("landed-delivery", "origin_landed", "fail-closed"),
    },
    {
        # fix/verify-full-checkout-sparse: verify worktree integrity
        # (att_17f6b893 + review repair rounds r1/r2) - all three
        # worktree-add sites force a full checkout (worktree-scoped sparse
        # disable with config fallback), fail closed on a missing schema,
        # doctor no-git-context note + tests-gate git-context hint;
        # GitContextGap collected via __test__ = True with the GREEN-run
        # stub fix so the asserted suite_failed path is reachable.
        "pr": 75,
        "name": "verify worktree integrity",
        "keywords": ("full checkout", "git-context hint", "GitContextGap"),
    },
    {
        # fix/attempts-in-flight-live-only: a dispatched attempt past the
        # 12-hour idle window is expired rather than in flight, and a verified
        # result is no longer counted as live work (297 -> 6 on the real store).
        "pr": 96,
        "name": "live-only in-flight status",
        "keywords": ("in-flight", "expired", "live work", "12-hour idle"),
    },
    {
        # feat/attempts-job-id: every attempt row carries the dispatch job id
        # from the brief (else the lease record) plus the repo those records
        # name, so a row rendered as "Job unknown" traces back to its job.
        "pr": 97,
        "name": "dispatch job id on attempt rows",
        "keywords": ("dispatch `job_id`", "Job unknown", "project label"),
    },
    {
        # fix/verify-delivery-base: verify defines the lease source_rev as the
        # base revision before writing the delivery intent; the old name
        # reference raised NameError after the push and suppressed PR creation.
        "pr": 99,
        "name": "delivery intent base revision",
        "keywords": ("base revision", "delivery intent", "NameError", "PR creation"),
    },
    {
        # fix/verbose-attempt-results: one attempt result (and its detail) is
        # read under MAX_RESULT_BYTES (24 MiB) instead of the 1 MiB client cap;
        # ordinary responses and the response_too_large ceiling are unchanged.
        "pr": 100,
        "name": "verbose attempt results to 24 MiB",
        "keywords": ("24 MiB", "`MAX_RESULT_BYTES`", "1 MiB", "`response_too_large`"),
    },
    {
        # fix/secret-scan-archives: an oversize tar is parsed and only added
        # member lines are scanned, with unsafe paths and parse failures still
        # failing closed and a 64 MiB decompression bound.
        "pr": 101,
        "name": "tar-aware secret scan",
        "keywords": (
            "tar archives",
            "added member lines",
            "2 MB",
            "decompression bound",
            "fails closed",
        ),
    },
    {
        # fix/verify-refusal-diagnostics: a verify refusal writes its machine
        # code and a scrubbed, path-reduced reason into the attempt record
        # instead of ok:false with empty evidence.
        "pr": 102,
        "name": "verify refusal diagnostics",
        "keywords": (
            "machine code",
            "scrubbed reason",
            "verify-results/<attempt>.json",
            "all-null evidence",
        ),
    },
    {
        # feat/verify-node-targets: .mjs/.cjs/.js targets run under node --test,
        # pytest stays the default, mixed sets refuse, a missing Node refuses,
        # and the runner argv is recorded.
        "pr": 103,
        "name": "Node test runner selection",
        "keywords": (
            "Node runner",
            "`node --test`",
            "mixed_test_targets",
            "node_runner_unavailable",
            "evidence.runner/test_argv",
        ),
    },
    {
        # B18 docs-sync companion: registers the coverage guard itself. A
        # full-history run fails when a merged campaign PR since #43 has no
        # registry row (or a row's keywords are absent from the changelog).
        "pr": 104,
        "name": "changelog coverage guard",
        "keywords": (
            "Changelog coverage guard",
            "full-history run",
            "merged campaign PR since #43",
            "no registry row",
        ),
    },
    {
        # Verify-output literal redaction: verify output and durable verify
        # records print the CLI's own validated branch, the verify-result path
        # and the commit sha literally instead of [redacted]; untrusted text
        # stays scrubbed.
        "pr": 110,
        "name": "verify output literal redaction",
        "keywords": (
            "validated branch",
            "verify-result path",
            "commit sha",
            "stays scrubbed",
        ),
    },
    {
        # feat/research-before-retry-gate (M12 trigger + gate): a retry of a
        # recorded failure is refused before any launcher call until accepted
        # findings exist for that failure id; duplicate failures enqueue
        # exactly one research item and env verdicts go to the decision queue.
        "pr": 107,
        "name": "research-before-retry gate",
        "keywords": (
            "Research-before-retry",
            "research_gate.py",
            "failure signature",
            "require_findings",
        ),
    },
    {
        # feat/kanban-board-sync (M13): reflect bee/attempt and GitHub PR
        # lifecycle events onto the Hermes kanban board through the public
        # CLI, idempotent by PR link or attempt id, with a reconcile pass and
        # a verified backup before the first mutation.
        "pr": 108,
        "name": "kanban board sync",
        "keywords": (
            "board-sync",
            "board_sync.py",
            "reconcile pass",
            "verified backup",
        ),
    },
    {
        # docs/changelog-107-108: this companion itself - the coverage rows and
        # Unreleased bullets for #107 and #108.
        "pr": 113,
        "name": "docs-sync companion for #107 and #108",
        "keywords": (
            "Docs-sync companion",
            "#107 and #108",
        ),
    },
    {
        # fix/factory-dispatch-wiring: the live docket envelope is readable and
        # unknown shapes are a typed refusal; the shipped unit carries the
        # dispatch settings from the environment file and fails closed.
        "pr": 118,
        "name": "factory dispatch wiring",
        "keywords": (
            "live docket envelope",
            "queue_invalid",
            "require-dispatch-config",
            "WORKFLOW_SRC",
        ),
    },
    {
        # feat/board-dispatch-bridge: a ready card with a durable approval record
        # that carries a strict-valid brief and an existing job is stocked as one
        # queue feature plus one dispatch pre-approval, idempotently; no record,
        # no stocking.
        "pr": 122,
        "name": "board to dispatcher bridge",
        "keywords": (
            "board-dispatch",
            "approval record",
            "idempotently",
        ),
    },
    {
        # fix/served-payload-relative-source: a sandboxed run records the lease
        # from the payload it was actually served. The one accepted relative
        # value is the staged entry for that payload name under the process's
        # own .bundlebees-payload directory; every other relative path keeps
        # the absolute-path refusal.
        "pr": 123,
        "name": "served relative payload resolves before the refusal",
        "keywords": (
            "record the lease for the payload it was served",
            "absolute-path requirement is unchanged",
            "staged entry for that payload name",
            "symlink out of the copy",
        ),
    },
    # The docs-sync companion that adds the four rows below is intended for a
    # SQUASH merge. A squash subject ("...(#N)") never matches
    # MERGE_SUBJECT_RE, so the guard does not count that merge and no registry
    # row for this branch's own PR number is required or added here.
    {
        # feat/factory-hook (PR #105): optional scout -> factory-driver bridge
        # - one pass per newly observed terminal attempt, a durable handled-id
        # watermark, a non-blocking flock that reports `coalesced` to the
        # loser, an unadvanced watermark on failure, the 30-minute heartbeat
        # retained as backstop, and live-scout wiring left to an owner action.
        "pr": 105,
        "name": "factory-hook bridge",
        "keywords": (
            "factory-hook",
            "durable watermark",
            "flock",
            "heartbeat backstop",
        ),
    },
    {
        # feat/completion-inbox-durability (PR #106): the terminal landing
        # publishes one durable completion event to the coordinator inbox
        # before the source acknowledgement; deduplicated by the deterministic
        # event id, at-least-once with one accepted decision, and one bounded
        # retry before an event is marked failed.
        "pr": 106,
        "name": "durable completion inbox hand-off",
        "keywords": (
            "before the source acknowledgement",
            "event id",
            "at-least-once",
            "one bounded retry",
        ),
    },
    {
        # feat/factory-service-mode (PR #109): the resident service wakes on
        # the inbox, dispatches only with a durable pre-approval plus scope,
        # shared cap and planning limits, and emits one coalesced wake; the
        # shipped unit is a template only.
        "pr": 109,
        "name": "factory driver service mode",
        "keywords": (
            "WakeCoalescer",
            "planning limits",
            "durable pre-approval",
        ),
    },
    {
        # fix/board-stock-comment-on-change (PR #111): a low observation
        # comments whenever it changes (state, ready count, free slots,
        # shortfall or board); identical repeats are silent again and a
        # recovery followed by a new shortfall starts a new episode.
        "pr": 111,
        "name": "board-stock comment on change",
        "keywords": (
            "board comment",
            "identical repeats",
            "new episode",
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
