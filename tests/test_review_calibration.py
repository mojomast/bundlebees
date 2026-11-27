"""Shadow calibration before any automatic approval (program: automated outside review, Phase 1b).

Every PR the outside reviewer or the owner reviews also gets a review bee at the same head.
Both verdicts are recorded and the bee's is not posted. The promotion bar is mechanical:

* at least 10 PRs, including at least 3 gate_security or deploy_path reviews;
* zero blocking findings the bee missed that the human/outside reviewer raised;
* false-block rate (bee blocking findings the human did not raise) at or below 20%.

Run: python -m pytest tests/test_review_calibration.py -q
"""

from __future__ import annotations

from bundlebees_plugin.review_calibration import (
    calibration_table,
    promotion_status,
    record_pair,
)

HEAD = "c" * 40


def _pair(pr: int, bee: str, human: str, *, types=("general",), bee_block=(), human_block=()):
    return record_pair(
        {
            "repo": "mojomast/rizzstack",
            "pr": pr,
            "head_sha": HEAD,
            "verdict": bee,
            "review_types": list(types),
            "blocking": [{"title": title} for title in bee_block],
        },
        {
            "repo": "mojomast/rizzstack",
            "pr": pr,
            "head_sha": HEAD,
            "verdict": human,
            "review_types": list(types),
            "blocking": [{"title": title} for title in human_block],
        },
    )


def test_table_pairs_both_verdicts_and_names_the_deltas():
    record = _pair(41, "approve", "changes_requested", human_block=("gate bypass",))
    table = calibration_table([record])
    row = table[0]
    assert row["pr"] == 41
    assert row["bee"] == "approve"
    assert row["human"] == "changes_requested"
    assert row["missed"] == ["gate bypass"]
    assert row["false_blocks"] == []


def test_false_blocks_are_bee_findings_the_human_did_not_raise():
    record = _pair(42, "changes_requested", "approve", bee_block=("style nit",))
    row = calibration_table([record])[0]
    assert row["false_blocks"] == ["style nit"]
    assert row["missed"] == []


def test_nine_reviews_are_not_promoted():
    records = [_pair(n, "approve", "approve") for n in range(1, 10)]
    status = promotion_status(records)
    assert status["prs"] == 9
    assert status["promoted"] is False


def test_ten_reviews_with_three_sensitive_ones_can_promote():
    records = [_pair(n, "approve", "approve") for n in range(1, 8)]
    records += [
        _pair(8, "approve", "approve", types=("gate_security",)),
        _pair(9, "approve", "approve", types=("deploy_path",)),
        _pair(10, "approve", "approve", types=("gate_security", "general")),
    ]
    status = promotion_status(records)
    assert (status["prs"], status["sensitive"]) == (10, 3)
    assert status["missed"] == 0
    assert status["promoted"] is True


def test_one_missed_blocking_finding_blocks_promotion():
    records = [_pair(n, "approve", "approve") for n in range(1, 8)]
    records += [_pair(8, "approve", "approve", types=("gate_security",)),
                _pair(9, "approve", "approve", types=("deploy_path",)),
                _pair(10, "approve", "changes_requested", types=("gate_security",),
                      human_block=("vendored copy is stale",))]
    status = promotion_status(records)
    assert status["missed"] == 1
    assert status["promoted"] is False


def test_false_block_rate_boundary_is_twenty_percent():
    records = []
    for n in range(1, 9):
        records.append(_pair(n, "approve", "approve", types=("gate_security",)))
    records.append(_pair(9, "changes_requested", "approve", bee_block=("nit",)))
    records.append(_pair(10, "changes_requested", "approve", bee_block=("nit",)))
    status = promotion_status(records)
    assert status["false_block_rate"] == 0.2
    assert status["promoted"] is True

    records.append(_pair(11, "changes_requested", "approve", bee_block=("nit",)))
    status = promotion_status(records)
    assert status["false_block_rate"] > 0.2
    assert status["promoted"] is False
