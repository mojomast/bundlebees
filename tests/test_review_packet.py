"""The review packet builder (program: automated outside review, Phase 1.2).

``review-packet <repo>#<pr>`` stages the offline ``review/`` tree the review bee reads,
exactly as ``skills/rizzstack-outside-review/SKILL.md`` §1 lists it. It is built only from
trusted sources: the GitHub API for the PR/checks/reviews, the trusted gate run, the verify
store, the base branch for leases/contracts, and canonical repos for ``related/``. The
author's transcript or reasoning is never a source.

Run: python -m pytest tests/test_review_packet.py -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest




def _load():
    import importlib

    return importlib.import_module("bundlebees_plugin.review_packet")
HEAD = "1a1d032f98691fb4121d5b9673b36562c77a5627"


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin",
        },
    )
    return out.stdout.strip()


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _sources(checkout: Path) -> dict:
    return {
        "packet": {
            "repo": "mojomast/rizzstack",
            "pr": 41,
            "title": "t",
            "body": "b",
            "base_ref": "main",
            "base_sha": "316087f7da8ea01a206b4c6417c2a1599e84b35c",
            "head_sha": _git(checkout, "rev-parse", "HEAD"),
            "author": "mojomast",
            "author_model_family": "unknown",
            "files": [{"path": "README.md", "status": "modified"}],
        },
        "checks": [{"name": "lease-tamper", "event": "pull_request", "conclusion": "failure"}],
        "reviews": [{"state": "COMMENTED", "commit_id": _git(checkout, "rev-parse", "HEAD")}],
        "verify": {"att_example.json": {"ok": True}},
        "lease": {"slice.json": {"contract_sha256": "0" * 64}},
        "related": {"lease_tamper.py": "# canonical\n"},
        "claims": ["the gate accepts the owner line", "the head is unchanged"],
        "repo_dir": str(checkout),
    }


def test_packet_layout_matches_skill(tmp_path: Path, checkout: Path):
    m = _load()
    out = tmp_path / "review-root"
    m.build_packet(_sources(checkout), out)
    for rel in m.PACKET_PATHS:
        assert (out / rel).exists(), rel
    packet = json.loads((out / "review/packet.json").read_text())
    assert packet["head_sha"] == _git(checkout, "rev-parse", "HEAD")
    assert (out / "review/repo/.git").exists()


def test_claims_are_claims_not_transcript():
    m = _load()
    text = m.claims_markdown(["the gate accepts the owner line"])
    assert text.startswith("# Claims")
    assert "- [ ] the gate accepts the owner line" in text


def test_author_transcript_is_not_a_trusted_source(tmp_path: Path, checkout: Path):
    m = _load()
    sources = _sources(checkout)
    sources["transcript"] = "the author's reasoning"
    with pytest.raises(m.PacketError) as caught:
        m.build_packet(sources, tmp_path / "out")
    assert caught.value.code == "packet_untrusted_source"

    sources = _sources(checkout)
    sources["author_reasoning"] = "why the change is right"
    with pytest.raises(m.PacketError) as caught:
        m.build_packet(sources, tmp_path / "out")
    assert caught.value.code == "packet_untrusted_source"


def test_head_mismatch_refused(tmp_path: Path, checkout: Path):
    m = _load()
    sources = _sources(checkout)
    sources["packet"]["head_sha"] = "f" * 40
    with pytest.raises(m.PacketError) as caught:
        m.build_packet(sources, tmp_path / "out")
    assert caught.value.code == "packet_head_mismatch"
    assert not m.head_matches(checkout, "f" * 40)


def test_incomplete_packet_refused(tmp_path: Path, checkout: Path):
    m = _load()
    sources = _sources(checkout)
    del sources["packet"]["head_sha"]
    with pytest.raises(m.PacketError) as caught:
        m.build_packet(sources, tmp_path / "out")
    assert caught.value.code == "packet_incomplete"
