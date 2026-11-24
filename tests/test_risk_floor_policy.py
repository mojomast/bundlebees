"""RED for wave A slice A7: the risk floor is a data file, and test
infrastructure is high tier.

Today ``tests/conftest.py`` is low tier because anything under ``tests/`` is.
A conftest, a fixtures tree or a test-runner config can change what every
approved test means, so it gets the same tier as the verifier itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bundlebees_plugin import risk

POLICY = Path(risk.__file__).resolve().parent / "policy" / "risk-floor.json"


@pytest.mark.parametrize(
    "path",
    [
        "tests/conftest.py",
        "conftest.py",
        "src/pkg/conftest.py",
        "tests/fixtures/data.json",
        "tests/fixtures/sub/case.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "noxfile.py",
        "sitecustomize.py",
        ".github/workflows/ci.yml",
    ],
)
def test_test_infrastructure_is_high(path):
    assert risk.derive_floor([path])["tier"] == "high"


@pytest.mark.parametrize("path", ["tests/test_a.py", "docs/x.md", "README.md"])
def test_plain_tests_and_docs_stay_low(path):
    assert risk.derive_floor([path])["tier"] == "low"


def test_policy_file_exists_and_carries_the_rules():
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    assert data["schema"] == "bundlebees.risk-floor/1"
    high = {rule["pattern"] for rule in data["high"]}
    assert "**/conftest.py" in high
    assert "sandbox.py" in {rule.get("basename") for rule in data["high"] if "basename" in rule} or any(
        "sandbox.py" in pattern for pattern in high
    )
    assert all(rule.get("reason") for rule in data["high"])


def test_sensitive_basenames_are_no_longer_hard_coded():
    assert not hasattr(risk, "_SENSITIVE_MODULES")


def test_unreadable_policy_fails_closed(tmp_path):
    bad = tmp_path / "risk-floor.json"
    bad.write_text("{not json", encoding="utf-8")
    floor = risk.derive_floor(["docs/x.md"], policy_path=bad)
    assert floor["tier"] == "high"
    assert any("policy" in reason for reason in floor["reasons"])


def test_missing_policy_fails_closed(tmp_path):
    floor = risk.derive_floor(["docs/x.md"], policy_path=tmp_path / "absent.json")
    assert floor["tier"] == "high"
