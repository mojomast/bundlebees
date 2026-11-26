"""``ATTACH_FINDINGS`` is dark by default and its attached findings are capped.

The research-before-retry gate still refuses a retry without an accepted
findings artifact; the flag controls only whether that artifact travels to the
launcher as ``brief.findings``, and the cap bounds how much of it can. The flag
is resolved through the shared source in :mod:`bundlebees_plugin.flags`, the
same file and precedence both the driver and the gateway read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import flags
from bundlebees_plugin import research_gate as rg
from bundlebees_plugin import tools

WF = "c" * 40
REV = "a" * 40
BASE_ENTRIES = 10


def failure(**over):
    base = {
        "kind": "verify",
        "code": "tests_failed",
        "message": "/home/one/repo/tests/test_alpha.py::test_one failed\nassert 1 == 2",
        "failing_tests": ["tests/test_alpha.py::test_one"],
        "attempt_id": "att-first",
        "versions": {"tool": "bundlebees-0.2", "runtime": "python-3.11"},
        "repo": "org/repo",
    }
    base.update(over)
    return base


def findings(fid: str, verdict: str = "patch-bug") -> dict:
    return {
        "schema": "bundlebees.research-findings/1",
        "failure_id": fid,
        "sources": [{"kind": "failing-test", "ref": "tests/test_alpha.py::test_one"}],
        "rejected_alternatives": ["retry without a fix (duplicate failure)"],
        "verdict": verdict,
        "summary": "the parser drops the trailing comma",
    }


def findings_with_entries(fid: str, entries: int) -> dict:
    """A valid artifact carrying exactly *entries* contract-list entries."""

    artifact = findings(fid)
    artifact["sources"] = [
        {"kind": "failing-test", "ref": f"tests/test_alpha.py::test_{index}"}
        for index in range(entries - BASE_ENTRIES)
    ]
    artifact["rejected_alternatives"] = [
        f"rejected alternative {index}" for index in range(BASE_ENTRIES)
    ]
    return artifact


def findings_sized(fid: str, size: int) -> dict:
    """A valid artifact whose canonical JSON encoding is exactly *size* bytes."""

    artifact = findings(fid)
    artifact["summary"] = ""
    base = len(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    artifact["summary"] = "x" * (size - base)
    assert len(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")) == size
    return artifact


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def projects(self):
        return []

    def attempts(self):
        return []

    def create(self, brief, **kwargs):
        self.calls.append(dict(brief))
        return {"attempt_id": "att-retry", "state": "queued"}


def retry_brief(**over) -> dict:
    brief = {
        "objective": "retry the failed work with the researched fix",
        "source_rev": REV,
        "assigned_files": ["pkg/x.py"],
        "acceptance": {"criteria": ["the failing test passes"]},
        "retry_of": "att-first",
    }
    brief.update(over)
    return brief


def _all_off(monkeypatch, tmp_path: Path) -> None:
    for name in flags.FLAGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(flags.FLAGS_FILE_ENV, str(tmp_path / "absent.env"))


def _attached(monkeypatch, tmp_path: Path, value: str = "1") -> None:
    _all_off(monkeypatch, tmp_path)
    monkeypatch.setenv(flags.FLAG_ATTACH_FINDINGS, value)


def _gated_state(tmp_path: Path, artifact: dict) -> None:
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    stored = dict(artifact)
    stored["failure_id"] = queued["failure_id"]
    rg.record_findings(tmp_path, stored)


def test_the_default_off_flag_attaches_nothing_despite_a_gated_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _all_off(monkeypatch, tmp_path)
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    # The gate still saw the accepted artifact (otherwise the dispatch would be
    # refused); only the attach stayed dark.
    assert result["ok"] is True
    assert len(client.calls) == 1
    assert "findings" not in client.calls[0]


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "2"])
def test_only_exactly_one_attaches(tmp_path: Path, monkeypatch, value: str) -> None:
    _attached(monkeypatch, tmp_path, value)
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is True
    assert "findings" not in client.calls[0]


def test_the_shared_flags_file_turns_the_attach_on(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "flags.env"
    path.write_text("ATTACH_FINDINGS=1\n", encoding="utf-8")
    _all_off(monkeypatch, tmp_path)
    monkeypatch.setenv(flags.FLAGS_FILE_ENV, str(path))
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == artifact


def test_the_flag_on_attaches_the_accepted_findings(tmp_path: Path, monkeypatch) -> None:
    _attached(monkeypatch, tmp_path)
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    artifact = rg.record_findings(tmp_path, findings(queued["failure_id"]))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == artifact


def test_the_entry_cap_accepts_exactly_the_limit(tmp_path: Path, monkeypatch) -> None:
    _attached(monkeypatch, tmp_path)
    _gated_state(tmp_path, findings_with_entries("ignored", rg.ATTACH_MAX_ENTRIES))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is True
    assert len(client.calls) == 1
    assert "findings" in client.calls[0]


def test_one_entry_over_the_cap_refuses_with_the_recorded_reason(
    tmp_path: Path, monkeypatch
) -> None:
    _attached(monkeypatch, tmp_path)
    _gated_state(tmp_path, findings_with_entries("ignored", rg.ATTACH_MAX_ENTRIES + 1))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is False
    assert result["code"] == "findings_too_large"
    assert "reason=entries" in result["error"]
    assert f"limits {rg.ATTACH_MAX_ENTRIES} entries" in result["error"]
    assert client.calls == []


def test_the_byte_cap_accepts_exactly_the_limit() -> None:
    assert rg.attachment_cap(findings_sized("fid", rg.ATTACH_MAX_BYTES)) is None


def test_one_byte_over_the_cap_reports_the_byte_reason(tmp_path: Path, monkeypatch) -> None:
    _attached(monkeypatch, tmp_path)
    _gated_state(tmp_path, findings_sized("ignored", rg.ATTACH_MAX_BYTES + 1))
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": retry_brief()}, tmp_path))

    assert result["ok"] is False
    assert result["code"] == "findings_too_large"
    assert "reason=bytes" in result["error"]
    assert client.calls == []


def test_the_entry_limit_is_reported_before_the_byte_limit() -> None:
    artifact = findings_with_entries("fid", rg.ATTACH_MAX_ENTRIES + 1)
    artifact["summary"] = "x" * rg.ATTACH_MAX_BYTES

    cap = rg.attachment_cap(artifact)

    assert cap is not None
    assert cap["reason"] == "entries"


def test_caller_findings_win_while_the_flag_is_off(tmp_path: Path, monkeypatch) -> None:
    _all_off(monkeypatch, tmp_path)
    queued = rg.enqueue_research(tmp_path, failure(), repo="org/repo", workflow_ref=WF)
    rg.record_findings(tmp_path, findings(queued["failure_id"]))
    direct = findings(queued["failure_id"])
    direct["summary"] = "the caller supplied this accepted block"
    brief = retry_brief(failure_id=queued["failure_id"], findings=direct)
    client = FakeClient()

    result = json.loads(tools._dispatch(client, {"brief": brief}, tmp_path))

    assert result["ok"] is True
    assert client.calls[0]["findings"] == direct


def test_the_flags_line_shows_attach_findings_off(tmp_path: Path) -> None:
    line = flags.startup_line(environ={flags.FLAGS_FILE_ENV: str(tmp_path / "absent.env")})

    assert "ATTACH_FINDINGS=0" in line
