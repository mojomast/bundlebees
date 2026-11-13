"""Approved acceptance tests for contract BL1-leftovers (review leftovers on bundlebees #11, RED first).

1. ``tdd.write_report`` failures never leak absolute host paths: the error text names only
   ``reports/<contract_id>.json``.
2. ``security.looks_like_secret`` (used by the added-lines secret scan) catches quoted secret values such as
   ``password = 'hunter2hunter2'``, and ``scrub`` redacts them; empty quoted values are not secrets.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import security, tdd


def _signed() -> dict:
    return tdd.sign_record({"contract_id": "BL-demo", "contract_sha256": "a" * 64, "accepted": False,
                            "phases": {}, "criteria": {}, "report_version": "acceptance_report/1"}, b"k" * 32)


def _text(exc) -> str:
    return " ".join(str(part) for part in (exc, getattr(exc, "error", ""), getattr(exc, "fix", "")))


def test_a_report_write_failure_does_not_leak_the_state_path(tmp_path):
    state = tmp_path / "private-state-dir"
    state.mkdir()
    (state / "reports").write_text("not a directory")
    with pytest.raises(tdd.TddError) as exc:
        tdd.write_report(state, _signed())
    text = _text(exc.value)
    assert str(tmp_path) not in text and "private-state-dir" not in text
    assert "reports/BL-demo.json" in text


def test_a_failed_replace_does_not_leak_the_state_path(tmp_path):
    state = tmp_path / "private-state-dir"
    (state / "reports" / "BL-demo.json").mkdir(parents=True)
    (state / "reports" / "BL-demo.json" / "keep").write_text("x")
    with pytest.raises(tdd.TddError) as exc:
        tdd.write_report(state, _signed())
    text = _text(exc.value)
    assert str(tmp_path) not in text and "private-state-dir" not in text
    assert "reports/BL-demo.json" in text
    assert not [p for p in (state / "reports").iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("line", [
    "password = 'hunter2hunter2'",
    # assembled at runtime so repository secret scanners do not flag the fixtures
    "api" + "_key: " + '"' + "fixture" + "-value-" + "1" * 4 + '"',
    "tok" + 'en="' + "fixture" + "-value-" + "2" * 4 + '"',
    "SECRET = 's3cr3t-v@lue!'",
    "    db_password = \"correct horse battery\"",
])
def test_quoted_secret_values_are_detected_and_redacted(line):
    assert security.looks_like_secret(line), line
    value = line.split("=", 1)[-1] if "=" in line else line.split(":", 1)[-1]
    secret = value.strip().strip("'\"")
    assert secret not in security.scrub(line)


@pytest.mark.parametrize("line", ["password = ''", 'token = ""'])
def test_empty_quoted_values_are_not_secrets(line):
    assert not security.looks_like_secret(line)
