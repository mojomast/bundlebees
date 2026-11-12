"""Approved acceptance tests for HR1 in the CLI and the report (written first, RED).

Contract: docs/tasks/HR1_HUMAN_REVIEW_TOKEN.md, AC-007 .. AC-010.

``tdd review --role human --token-file F`` records a human review only when
the token verifies against the configured ``review_public_key`` and is bound
to this contract, its approved test revision and the current evidence chain
head. Anything else records nothing and fails with ``human_review_unverified``.
``tdd report`` re-verifies the stored token and never reports a human review
it cannot verify. Keys are throwaway.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.fakes import FakeCtx, load_plugin
from tests.test_cli_acdd import FakeClient, _invoke, _records
from tests.test_cli_acdd_review import TEST_AC, _verified
from tests.test_review_token import _keypair



def _ctx(public_key=None):
    plugin = load_plugin()
    settings = {"verify_allow_unsafe_local": True}
    if public_key is not None:
        settings["review_public_key"] = public_key
    ctx = FakeCtx(settings=settings)
    ctx.bundlebees_client = FakeClient({})
    plugin.register(ctx)
    return ctx


def _report(ctx, state, path, capsys):
    capsys.readouterr()
    _invoke(ctx, ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    return json.loads(capsys.readouterr().out)


def _mint(tmp_path, priv, path, chain_head, **extra):
    from bundlebees_plugin import review_token as rt

    frozen = json.loads(path.read_text())
    token = rt.mint(rt.load_private_key(priv), reviewer=extra.pop("reviewer", "kyle"),
                    contract_id=frozen["id"], contract_sha256=frozen["review"]["contract_sha256"],
                    test_rev=extra.pop("test_rev", frozen["source_rev"]), chain_head=chain_head,
                    ttl_s=extra.pop("ttl_s", 900))
    out = tmp_path / "token.txt"
    out.write_text(token + "\n")
    os.chmod(out, 0o600)
    return out


def _human_review(ctx, state, path, token_file, reviewer="kyle"):
    return _invoke(ctx, ["tdd", "review", str(path), "--state-dir", str(state), "--role", "human",
                         "--reviewer", reviewer, "--token-file", str(token_file), "--approve",
                         "--note", "read the diff and the evidence"])


def _setup(tmp_path, capsys):
    priv, _, inline = _keypair(tmp_path)
    repo, state, path = _verified(tmp_path, capsys, ["red", "green", "review"],
                                  acceptance_criteria=[TEST_AC])
    ctx = _ctx(inline)
    head = _report(ctx, state, path, capsys)["chain_head"]
    return priv, inline, state, path, ctx, head


# AC-007: a valid token records a verified human review; the report says so.
def test_valid_token_records_a_verified_human_review(tmp_path, capsys):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    assert len(head) == 64
    before = len(_records(state))
    assert _human_review(ctx, state, path, _mint(tmp_path, priv, path, head)) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "human review: verified" in out
    records = _records(state)
    assert len(records) == before + 1
    review = records[-1]
    assert review["phase"] == "review" and review["produced_by"]["role"] == "human"
    att = review["attestation"]
    assert att["kind"] == "human_authorization" and att["identity_verified"] is True
    assert att["reviewer_label"] == "kyle" and att["chain_head"] == head == review["prev_sha256"]
    assert att["token"].startswith("bbrt1.") and len(att["key_id"]) == 16
    report = _report(ctx, state, path, capsys)
    assert report["review_identity"] == "human_verified"
    assert report["human_authorization"]["status"] == "verified"
    assert report["human_authorization"]["reviewer"] == "kyle"
    assert report["phases"]["review"] == "valid" and report["accepted"] is True


# AC-008: a bad token, a wrong binding or no public key records nothing.
@pytest.mark.parametrize("case", ["wrong_head", "wrong_key", "wrong_test_rev", "reviewer_mismatch", "no_key", "garbage"])
def test_unverifiable_token_fails_closed_and_records_nothing(tmp_path, capsys, case):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    reviewer = "kyle"
    if case == "wrong_head":
        token = _mint(tmp_path, priv, path, "f" * 64)
    elif case == "wrong_key":
        other, _, _ = _keypair(tmp_path, "mallory")
        token = _mint(tmp_path, other, path, head)
    elif case == "wrong_test_rev":
        token = _mint(tmp_path, priv, path, head, test_rev="0" * 40)
    elif case == "reviewer_mismatch":
        token = _mint(tmp_path, priv, path, head)
        reviewer = "someone-else"
    elif case == "no_key":
        token = _mint(tmp_path, priv, path, head)
        ctx = _ctx(None)
    else:
        token = tmp_path / "token.txt"
        token.write_text("bbrt1.not.atoken\n")
    before = len(_records(state))
    assert _human_review(ctx, state, path, token, reviewer=reviewer) == 1
    assert "human_review_unverified" in capsys.readouterr().out
    assert len(_records(state)) == before
    report = _report(_ctx(inline), state, path, capsys)
    assert report["human_authorization"]["status"] == "not_recorded"
    assert report["accepted"] is False


def test_same_token_cannot_be_used_twice(tmp_path, capsys):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    token = _mint(tmp_path, priv, path, head)
    assert _human_review(ctx, state, path, token) == 0
    capsys.readouterr()
    assert _human_review(ctx, state, path, token) == 1
    assert "human_review_unverified" in capsys.readouterr().out


# AC-009: the report re-verifies; a stored human review it cannot verify is not accepted.
@pytest.mark.parametrize("report_key", ["rotated", "missing"])
def test_report_reverifies_and_fails_closed(tmp_path, capsys, report_key):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    assert _human_review(ctx, state, path, _mint(tmp_path, priv, path, head)) == 0
    other = _keypair(tmp_path, "rotated")[2] if report_key == "rotated" else None
    report = _report(_ctx(other), state, path, capsys)
    assert report["review_identity"] == "unverified_human_authorization"
    assert report["human_authorization"]["status"] == "unverified"
    assert report["human_authorization"]["reason"]
    assert report["phases"]["review"] == "human_review_unverified"
    assert report["accepted"] is False


# AC-010: without a token, --role human stays a coordinator-recorded review; mint is a CLI command.
def test_role_human_without_token_stays_coordinator_recorded(tmp_path, capsys):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    rc = _invoke(ctx, ["tdd", "review", str(path), "--state-dir", str(state), "--role", "human",
                       "--reviewer", "kyle", "--approve", "--note", "looked at it"])
    assert rc == 0
    report = _report(ctx, state, path, capsys)
    assert report["review_identity"] == "coordinator_recorded"
    assert report["human_authorization"]["status"] == "not_recorded"


def test_plugin_cli_has_review_token_mint(tmp_path, capsys):
    priv, inline, state, path, ctx, head = _setup(tmp_path, capsys)
    rc = _invoke(ctx, ["review-token", "mint", "--private-key", str(priv), "--contract", str(path),
                       "--chain-head", head, "--reviewer", "kyle", "--ttl", "300"])
    assert rc == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("bbrt1.") and "PRIVATE" not in token
