"""Offline tests for lease-time memory recall.

Every query program here is a local stub written to ``tmp_path``.  The tests
use no network, no vault, and no ambient credentials.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from bundlebees_plugin import leases, memory_recall
from bundlebees_plugin.leases import _canonical, _sign, load_lease, record_lease

SHA40 = "a" * 40
NOW = "2026-01-02T03:04:05Z"


def _brief(**overrides):
    brief = {
        "objective": "change one file",
        "source_rev": SHA40,
        "branch": "feat/memory",
        "assigned_files": ["src/a.py"],
        "acceptance": ["tests pass"],
    }
    brief.update(overrides)
    return brief


def _item(item_id: str = "m1", excerpt: str = "short note", **overrides):
    item = {
        "id": item_id,
        "type": "note",
        "sha": "c" * 40,
        "path": "memory/note.md",
        "title": "Title",
        "excerpt": excerpt,
    }
    item.update(overrides)
    return item


def _stub(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return f"{sys.executable} {path}"


def _result_stub(tmp_path: Path, result: object, name: str = "query_stub.py") -> str:
    payload = json.dumps(result)
    body = (
        "import json, sys\n"
        "request = sys.stdin.read()\n"
        "print(" + repr(payload) + ")\n"
    )
    return _stub(tmp_path, name, body)


def _enable(monkeypatch, command: str) -> None:
    monkeypatch.setenv("MEMORY_ENABLED", "1")
    monkeypatch.setenv("BUNDLEBEES_MEMORY_QUERY", command)


def _misses(state: Path) -> list[dict]:
    path = memory_recall.miss_path(state)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_flag_off_touches_nothing_and_same_mac_as_unpatched_shape(
    tmp_path: Path, monkeypatch
):
    marker = tmp_path / "subprocess-ran"
    stub = _stub(
        tmp_path,
        "marker_stub.py",
        "from pathlib import Path\n"
        "Path(" + repr(str(marker)) + ").write_text('ran', encoding='utf-8')\n",
    )
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    monkeypatch.setenv("BUNDLEBEES_MEMORY_QUERY", stub)

    assert memory_recall.recall(tmp_path, _brief()) is None
    assert not marker.exists()
    assert not (tmp_path / "memory").exists()

    first = record_lease(tmp_path, "att-off", _brief(), now=NOW)
    assert "memory_context" not in first
    assert first["brief"]["objective"] == "change one file"
    assert first["brief_sha256"] == hashlib.sha256(
        _canonical(first["brief"])
    ).hexdigest()

    body = {name: value for name, value in first.items() if name != "mac"}
    key = (tmp_path / "lease.key").read_bytes()
    assert first["mac"] == _sign(body, key)

    # Build the same record a second time against the same key and clock.
    path = tmp_path / "leases" / "att-off.json"
    path.chmod(0o600)
    path.unlink()
    second = record_lease(tmp_path, "att-off", _brief(), now=NOW)
    assert second == first
    assert "memory_context" not in second
    assert not (tmp_path / "memory").exists()


def test_caps_are_enforced_exactly(tmp_path: Path, monkeypatch):
    items = [_item(f"m{i}", excerpt=("x" * 600) if i == 0 else "short") for i in range(9)]
    command = _result_stub(
        tmp_path,
        {"contract": memory_recall.MEMORY_CONTRACT, "vault_rev": "rev-1", "items": items},
    )
    _enable(monkeypatch, command)

    result = memory_recall.recall(tmp_path, _brief())

    assert result is not None
    assert result["contract"] == memory_recall.MEMORY_CONTRACT
    assert result["caps"] == {
        "max_items": 8,
        "max_excerpt_chars": 480,
        "max_total_chars": 4000,
    }
    assert result["vault_rev"] == "rev-1"
    assert len(result["items"]) == 8
    assert [item["id"] for item in result["items"]] == [f"m{i}" for i in range(8)]
    assert len(result["items"][0]["excerpt"]) == 480
    assert result["items"][0]["excerpt"] == "x" * 480
    assert _misses(tmp_path) == []


def test_total_cap_drops_whole_items(tmp_path: Path, monkeypatch):
    # Each cleaned item costs 480 excerpt + 90 other chars = 570.  Seven fit
    # under 4000; the eighth is dropped rather than truncated to fit.
    items = [
        _item(
            f"m{i}",
            excerpt="y" * 600,
            title="t" * 20,
            path="p" * 20,
            id="i" * 5,
            type="n" * 5,
            sha="s" * 40,
        )
        for i in range(8)
    ]
    command = _result_stub(
        tmp_path,
        {"contract": memory_recall.MEMORY_CONTRACT, "vault_rev": "rev-2", "items": items},
    )
    _enable(monkeypatch, command)

    result = memory_recall.recall(tmp_path, _brief())

    assert result is not None
    assert len(result["items"]) == 7
    assert all(len(item["excerpt"]) == 480 for item in result["items"])
    assert sum(
        len(value)
        for item in result["items"]
        for value in item.values()
    ) == 7 * 570


def test_honest_empty_list_stays_empty(tmp_path: Path, monkeypatch):
    command = _result_stub(
        tmp_path,
        {"contract": memory_recall.MEMORY_CONTRACT, "vault_rev": "rev-0", "items": []},
    )
    _enable(monkeypatch, command)

    result = memory_recall.recall(tmp_path, _brief())

    assert result is not None
    assert result["items"] == []
    assert _misses(tmp_path) == []


def test_items_are_reduced_to_exactly_the_six_fields(tmp_path: Path, monkeypatch):
    item = _item("m2")
    item["score"] = 0.9
    item["body"] = "not part of the contract"
    command = _result_stub(
        tmp_path,
        {
            "contract": memory_recall.MEMORY_CONTRACT,
            "vault_rev": "rev-3",
            "items": [item],
        },
    )
    _enable(monkeypatch, command)

    result = memory_recall.recall(tmp_path, _brief())

    assert result is not None
    assert len(result["items"]) == 1
    assert set(result["items"][0]) == {
        "id",
        "type",
        "sha",
        "path",
        "title",
        "excerpt",
    }
    assert "score" not in result["items"][0]
    assert "body" not in result["items"][0]


def test_item_missing_sha_is_dropped(tmp_path: Path, monkeypatch):
    missing_sha = _item("bad")
    del missing_sha["sha"]
    good = _item("good")
    command = _result_stub(
        tmp_path,
        {
            "contract": memory_recall.MEMORY_CONTRACT,
            "vault_rev": "rev-4",
            "items": [missing_sha, good],
        },
    )
    _enable(monkeypatch, command)

    result = memory_recall.recall(tmp_path, _brief())

    assert result is not None
    assert [item["id"] for item in result["items"]] == ["good"]


def test_lease_attaches_context_without_touching_brief(tmp_path: Path, monkeypatch):
    command = _result_stub(
        tmp_path,
        {
            "contract": memory_recall.MEMORY_CONTRACT,
            "vault_rev": "rev-5",
            "items": [_item("m9", excerpt="remember this")],
        },
    )
    _enable(monkeypatch, command)
    brief = _brief()

    record = record_lease(tmp_path, "att-on", brief, now=NOW)

    assert "memory_context" in record
    assert record["memory_context"]["items"][0]["id"] == "m9"
    assert "memory_context" not in record["brief"]
    assert brief == _brief()
    assert record["brief_sha256"] == hashlib.sha256(
        _canonical(record["brief"])
    ).hexdigest()
    assert load_lease(tmp_path, "att-on") == record


def test_unconfigured_query_is_a_logged_miss_and_lease_still_written(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("MEMORY_ENABLED", "1")
    monkeypatch.delenv("BUNDLEBEES_MEMORY_QUERY", raising=False)

    assert memory_recall.recall(tmp_path, _brief()) is None
    assert [miss["code"] for miss in _misses(tmp_path)] == [
        "memory_query_unconfigured"
    ]

    record = record_lease(tmp_path, "att-no-query", _brief(), now=NOW)
    assert "memory_context" not in record
    assert load_lease(tmp_path, "att-no-query") == record


def test_refusal_is_a_scrubbed_miss_and_lease_still_written(
    tmp_path: Path, monkeypatch
):
    marker_text = "excerpt-needle-not-for-log"
    host_marker = "stderr-host-canary"
    url_marker = "stderr-url-canary"
    stub = _stub(
        tmp_path,
        "refuse_stub.py",
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stderr.write('" + host_marker + " " + url_marker + " " + marker_text + "\\n')\n"
        "sys.exit(3)\n",
    )
    _enable(monkeypatch, stub)

    assert memory_recall.recall(tmp_path, _brief(objective=marker_text)) is None
    misses = _misses(tmp_path)
    assert len(misses) == 1
    assert misses[0]["code"] == "memory_query_failed"

    log_text = memory_recall.miss_path(tmp_path).read_text(encoding="utf-8")
    assert marker_text not in log_text
    assert host_marker not in log_text
    assert url_marker not in log_text
    assert str(tmp_path) not in log_text

    record = record_lease(tmp_path, "att-refused", _brief(), now=NOW)
    assert "memory_context" not in record
    assert load_lease(tmp_path, "att-refused") == record


def test_timeout_is_a_miss(tmp_path: Path, monkeypatch):
    stub = _stub(
        tmp_path,
        "slow_stub.py",
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(2)\n"
        "print('{}')\n",
    )
    _enable(monkeypatch, stub)
    monkeypatch.setattr(memory_recall, "QUERY_TIMEOUT_S", 0.2)

    assert memory_recall.recall(tmp_path, _brief()) is None
    assert [miss["code"] for miss in _misses(tmp_path)] == ["memory_query_timeout"]


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        ("not json at all", "memory_query_invalid_json"),
        ("[]", "memory_query_invalid_json"),
        (
            '{"contract":"other/1","items":[],"vault_rev":"r"}',
            "memory_query_wrong_contract",
        ),
        (
            '{"contract":"rizzstack.memory/1","items":{},"vault_rev":"r"}',
            "memory_query_invalid_result",
        ),
    ],
)
def test_garbage_or_wrong_contract_is_a_miss(
    tmp_path: Path, monkeypatch, stdout: str, code: str
):
    stub = _stub(
        tmp_path,
        "garbage_stub.py",
        "import sys\n"
        "sys.stdin.read()\n"
        "sys.stdout.write(" + repr(stdout) + ")\n",
    )
    _enable(monkeypatch, stub)

    assert memory_recall.recall(tmp_path, _brief()) is None
    assert [miss["code"] for miss in _misses(tmp_path)] == [code]


def test_miss_log_is_append_only(tmp_path: Path, monkeypatch):
    stub = _stub(
        tmp_path,
        "refuse_twice.py",
        "import sys\nsys.stdin.read()\nsys.exit(1)\n",
    )
    _enable(monkeypatch, stub)

    assert memory_recall.recall(tmp_path, _brief()) is None
    path = memory_recall.miss_path(tmp_path)
    first = path.read_bytes()
    assert memory_recall.recall(tmp_path, _brief()) is None

    after = path.read_bytes()
    assert after.startswith(first)
    assert len(after.splitlines()) == 2
    assert [miss["code"] for miss in _misses(tmp_path)] == [
        "memory_query_failed",
        "memory_query_failed",
    ]


def test_query_gets_text_argv_and_json_request_on_stdin(tmp_path: Path, monkeypatch):
    argv_out = tmp_path / "argv.txt"
    stdin_out = tmp_path / "stdin.json"
    stub = _stub(
        tmp_path,
        "capture_stub.py",
        "import sys\n"
        "from pathlib import Path\n"
        "Path(" + repr(str(stdin_out)) + ").write_text(sys.stdin.read(), encoding='utf-8')\n"
        "Path(" + repr(str(argv_out)) + ").write_text(sys.argv[1] if len(sys.argv) > 1 else '', encoding='utf-8')\n"
        "print('{\"contract\":\"rizzstack.memory/1\",\"items\":[],\"vault_rev\":\"r\"}')\n",
    )
    _enable(monkeypatch, stub)

    assert memory_recall.recall(tmp_path, _brief(objective="needle task")) is not None
    request = json.loads(stdin_out.read_text(encoding="utf-8"))
    assert argv_out.read_text(encoding="utf-8") == "needle task"
    assert request["contract"] == memory_recall.MEMORY_CONTRACT
    assert request["query"] == "needle task"
    assert request["caps"] == memory_recall.CAPS
