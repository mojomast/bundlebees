"""The unit-test job provides the pinned cryptography the ticket tests need.

C3's ``tickets`` extra is optional at install time, and the test job installs
the core dependencies plus ``dev`` only. Without a CI overlay the Ed25519
paths fail with ``ModuleNotFoundError`` instead of being tested. The overlay
must stay a test-environment pin: cryptography never becomes a core
dependency, and the pin must sit inside the range the extra declares.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PIN = "cryptography==44.0.0"


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def job_block(name: str) -> str:
    """The lines of one top-level job in the workflow."""
    lines = workflow_text().splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == f"  {name}:"),
        None,
    )
    assert start is not None, f"workflow has no {name!r} job"
    block = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
            break
        block.append(line)
    return "\n".join(block)


def extra_specs() -> list[str]:
    extras = pyproject().get("project", {}).get("optional-dependencies", {})
    return list(extras.get("tickets", []))


def declared_range() -> tuple[int, int]:
    spec = next((item for item in extra_specs() if item.startswith("cryptography")), "")
    match = re.fullmatch(r"cryptography>=(\d+),<(\d+)", spec.strip())
    assert match, f"the tickets extra must declare cryptography>=N,<M, got {spec!r}"
    return int(match.group(1)), int(match.group(2))


def pinned_major() -> int:
    match = re.fullmatch(r"cryptography==(\d+)\.(\d+)\.(\d+)", PIN)
    assert match, PIN
    return int(match.group(1))


def test_the_tickets_extra_declares_the_optional_cryptography_range() -> None:
    assert extra_specs(), "pyproject must declare a 'tickets' optional extra"
    low, high = declared_range()
    assert low < high


def test_the_ci_pin_sits_inside_the_declared_range() -> None:
    low, high = declared_range()
    assert low <= pinned_major() < high, (
        f"{PIN} is outside the declared tickets range {low}..{high}"
    )


def test_the_tests_job_installs_the_pinned_overlay() -> None:
    block = job_block("tests")
    assert PIN in block, f"the tests job must install {PIN}"
    assert re.search(r"pip install[^\n]*" + re.escape(PIN), block), (
        "the pin must be installed in the tests job before pytest runs"
    )


def test_cryptography_stays_out_of_the_core_dependencies() -> None:
    core = pyproject().get("project", {}).get("dependencies", [])
    assert not any(item.startswith("cryptography") for item in core), (
        "cryptography must stay an optional extra, never a core dependency"
    )
