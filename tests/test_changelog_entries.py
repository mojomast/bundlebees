"""Offline doc-guard for the [0.2.0] - Unreleased changelog entries.

The test reads only ``CHANGELOG.md`` and asserts that the unreleased section
documents the two merged behaviors: the provision-reconcile recovery (PR #43,
``fix/provision-reconcile-repost``) and the verifier/session reliability work
(PR #44, ``fix/session-audit-reliability``). No network, launcher, token,
Docker, or SSH access.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_MD = ROOT / "CHANGELOG.md"

# The queue-run crash on provisioning refusal (queue.py:462/503) is a known,
# still-open follow-up: the changelog must never claim it fixed here.
FORBIDDEN_FIXED_CLAIMS = (
    "queue.py:462",
    "queue.py:503",
    "queue-run crash",
)

PROVISION_RECONCILE_PHRASES = (
    # PR #43: host-confirmed receipt_unknown recovers with exactly one
    # idempotent re-POST under the attempts <= 3 guard; ProvisionUncertain
    # stays read-only; a definitive refusal is terminal; a first-POST
    # refusal detail is persisted before the re-raise.
    "idempotent re-POST",
    "`receipt_unknown`",
    "attempts <= 3",
    "ProvisionUncertain",
    "terminal `denied`",
    "first-POST refusal",
)

SESSION_RELIABILITY_PHRASES = (
    # PR #44: one configured runner over the existing sandbox, receipt
    # identity over the full execution policy, declared scanner discovery,
    # required scanning in the factory adapter, structured refusals,
    # repeated --tests flags, sealed recovery ceiling via CLI flags.
    "configured runner",
    "existing sandbox",
    "receipt identity",
    "execution policy",
    "dependency manifests",
    "cache bytes",
    "through `PATH`",
    "`gitleaks_path`",
    "before launcher access",
    "programmatic factory adapter",
    "structured CLI errors",
    "`--tests`",
    "recovery ceiling",
)


def _read_changelog() -> str:
    text = CHANGELOG_MD.read_text(encoding="utf-8")
    assert "\x00" not in text, "CHANGELOG.md must be plain UTF-8 text"
    return text


def _unreleased_section() -> str:
    match = re.search(
        r"(?ms)^## \[0\.2\.0\] - Unreleased\s*$\n(.*?)(?=^## \[|\Z)",
        _read_changelog(),
    )
    assert match is not None, "CHANGELOG.md must keep a '## [0.2.0] - Unreleased' section"
    return match.group(1)


def test_unreleased_section_exists() -> None:
    section = _unreleased_section()
    assert section.strip(), "[0.2.0] - Unreleased must not be empty"


def test_unreleased_section_documents_provision_reconcile_recovery() -> None:
    section = _unreleased_section()
    for phrase in PROVISION_RECONCILE_PHRASES:
        assert phrase in section, (
            f"[0.2.0] - Unreleased must mention {phrase!r} "
            "(PR #43 provision-reconcile recovery is missing from the changelog)"
        )


def test_unreleased_section_documents_session_audit_reliability() -> None:
    section = _unreleased_section()
    for phrase in SESSION_RELIABILITY_PHRASES:
        assert phrase in section, (
            f"[0.2.0] - Unreleased must mention {phrase!r} "
            "(PR #44 verifier/session reliability is missing from the changelog)"
        )


def test_unreleased_section_does_not_claim_the_queue_run_crash_fixed() -> None:
    section = _unreleased_section()
    for phrase in FORBIDDEN_FIXED_CLAIMS:
        assert phrase not in section, (
            f"{phrase!r} must not appear: the queue-run crash on provisioning "
            "refusal is still an open follow-up, not a fixed behavior"
        )
