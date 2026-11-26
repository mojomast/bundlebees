"""The unit-test job provides the pinned cryptography the ticket tests need.

C3's ``tickets`` extra is optional at install time, and the test job installs
the core dependencies plus ``dev`` only. Without a CI overlay the Ed25519
paths fail with ``ModuleNotFoundError`` instead of being tested. The overlay
must stay a test-environment pin: cryptography never becomes a core
dependency, and the pin must sit inside the range the extra declares.

The overlay mirrors the verify interpreter exactly: ``cryptography==50.0.2``
and its ``cffi==2.1.1`` / ``pycparser==3.12`` dependencies come from a
hash-pinned requirements file installed with ``--require-hashes``, so a
runner cannot resolve a different build than the one verify tested with.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS = ROOT / ".github" / "requirements" / "cryptography.txt"
PIN = "cryptography==50.0.2"
PINNED = {"cryptography": "50.0.2", "cffi": "2.1.1", "pycparser": "3.12"}
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}\b")


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def requirements_text() -> str:
    return REQUIREMENTS.read_text(encoding="utf-8")


def requirement_blocks(text: str) -> list[str]:
    """Logical requirement lines, with backslash continuations joined."""
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        current.append(stripped)
        if not stripped.endswith("\\"):
            blocks.append(" ".join(part.rstrip("\\").strip() for part in current))
            current = []
    if current:
        blocks.append(" ".join(part.rstrip("\\").strip() for part in current))
    return blocks


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


def test_the_requirements_file_pins_every_version_with_hashes() -> None:
    blocks = requirement_blocks(requirements_text())
    assert blocks, "the overlay requirements file must declare its pins"
    for name, version in PINNED.items():
        matching = [block for block in blocks if block.split()[0] == f"{name}=={version}"]
        assert len(matching) == 1, f"{name}=={version} must be pinned exactly once"
        assert HASH_RE.search(matching[0]), f"{name}=={version} must carry sha256 hashes"
    for block in blocks:
        first = block.split()[0]
        assert "==" in first, f"every overlay requirement must be exact: {block!r}"
        assert HASH_RE.search(block), f"every overlay requirement needs a hash: {block!r}"


def test_the_tests_job_installs_the_hash_pinned_overlay() -> None:
    block = job_block("tests")
    assert "--require-hashes" in block, "the tests job must install with --require-hashes"
    assert re.search(
        r"pip install[^\n]*--require-hashes[^\n]*-r[^\n]*\.github/requirements/cryptography\.txt",
        block,
    ), "the tests job must install the hash-pinned requirements file before pytest runs"
    assert PIN in requirements_text(), f"the requirements file must pin {PIN}"


def test_cryptography_stays_out_of_the_core_dependencies() -> None:
    core = pyproject().get("project", {}).get("dependencies", [])
    assert not any(item.startswith("cryptography") for item in core), (
        "cryptography must stay an optional extra, never a core dependency"
    )
