"""Test-receipt ledger: exact-key reuse, invalidation and refusal paths."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from bundlebees_plugin import test_receipts as receipts

A = "a" * 64
B = "b" * 64
C = "c" * 64


def key_inputs(**overrides):
    base = {
        "source_tree_sha256": A,
        "patch_sha256": B,
        "test_sources_sha256": C,
        "command": ["python3", "-m", "pytest", "tests/test_example.py", "-q"],
        "dependencies": ["pytest==8.3.0"],
        "stack": {"python": "3.11", "pytest": "8.3.0"},
        "environment": {"CI": "1", "PYTHONHASHSEED": "0"},
    }
    base.update(overrides)
    return base


def test_exact_key_hit_returns_receipt_without_overwriting(tmp_path: Path) -> None:
    inputs = key_inputs()
    first = receipts.record_receipt(tmp_path, inputs, {"passed": True}, 0.25)

    assert first["reused"] is False
    assert first["key"] == receipts.canonical_key(inputs)
    assert first["result"] == {"passed": True}
    assert first["duration_s"] == 0.25

    path = receipts.receipt_path(tmp_path, inputs)
    assert path.is_file()
    original_bytes = path.read_bytes()

    hit = receipts.lookup_receipt(tmp_path, inputs)
    assert hit is not None
    assert hit["reused"] is True
    assert hit["key"] == first["key"]
    assert hit["result"] == {"passed": True}
    assert hit["duration_s"] == 0.25

    # A second record call is a reuse: the supplied result is never used and
    # the create-only file is left byte-for-byte unchanged.
    replay = receipts.record_receipt(tmp_path, inputs, {"passed": False}, 99.0)
    assert replay["reused"] is True
    assert replay["result"] == {"passed": True}
    assert replay["duration_s"] == 0.25
    assert path.read_bytes() == original_bytes


def test_missing_key_is_a_miss(tmp_path: Path) -> None:
    assert receipts.lookup_receipt(tmp_path, key_inputs()) is None


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("source_tree_sha256", "d" * 64),
        ("test_sources_sha256", "e" * 64),
        ("command", ["python3", "-m", "pytest", "tests/test_other.py", "-q"]),
        ("environment", {"CI": "1", "PYTHONHASHSEED": "1"}),
    ],
)
def test_changed_source_test_command_or_environment_invalidates(
    tmp_path: Path, field: str, changed
) -> None:
    original = key_inputs()
    first = receipts.record_receipt(tmp_path, original, {"passed": True}, 0.5)

    modified = key_inputs(**{field: changed})
    assert receipts.canonical_key(modified) != first["key"]
    assert receipts.lookup_receipt(tmp_path, modified) is None

    second = receipts.record_receipt(tmp_path, modified, {"passed": False}, 0.75)
    assert second["reused"] is False
    assert second["key"] != first["key"]
    assert second["result"] == {"passed": False}

    # The old exact key is unaffected and still a hit.
    old = receipts.lookup_receipt(tmp_path, original)
    assert old is not None
    assert old["result"] == {"passed": True}


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("patch_sha256", "f" * 64),
        ("dependencies", ["pytest==8.4.0"]),
        ("stack", {"python": "3.12", "pytest": "8.3.0"}),
    ],
)
def test_every_other_key_input_invalidates(tmp_path: Path, field: str, changed) -> None:
    original = key_inputs()
    receipts.record_receipt(tmp_path, original, {"passed": True}, 0.5)
    assert receipts.lookup_receipt(tmp_path, key_inputs(**{field: changed})) is None


def test_key_is_canonical_across_mapping_order(tmp_path: Path) -> None:
    first = key_inputs(environment={"B": "2", "A": "1"})
    second = key_inputs(environment={"A": "1", "B": "2"})
    assert receipts.canonical_key(first) == receipts.canonical_key(second)

    reordered = {name: key_inputs()[name] for name in reversed(list(key_inputs()))}
    reordered["environment"] = {"A": "1", "B": "2"}
    assert receipts.canonical_key(reordered) == receipts.canonical_key(first)

    one = receipts.record_receipt(tmp_path, first, {"passed": True}, 1)
    two = receipts.record_receipt(tmp_path, second, {"passed": True}, 1)
    assert two["reused"] is True
    assert two["key"] == one["key"]


def test_receipt_stores_no_raw_inputs_paths_or_secrets(tmp_path: Path) -> None:
    inputs = key_inputs()
    recorded = receipts.record_receipt(
        tmp_path,
        inputs,
        {"passed": True, "note": "local run"},
        1.5,
    )
    text = receipts.receipt_path(tmp_path, inputs).read_text(encoding="utf-8")

    assert "key_inputs" not in recorded
    assert " ".join(inputs["command"]) not in text
    assert inputs["source_tree_sha256"] not in text
    assert inputs["test_sources_sha256"] not in text
    assert "pytest==8.3.0" not in text
    assert str(tmp_path) not in text
    assert "receipt_sha256" in text


def test_storage_is_private_and_create_only(tmp_path: Path) -> None:
    inputs = key_inputs()
    receipts.record_receipt(tmp_path, inputs, {"passed": True}, 1)

    directory = tmp_path / "receipts"
    path = receipts.receipt_path(tmp_path, inputs)
    assert directory.is_dir()
    assert path.is_file()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # No temporary files are left behind.
    assert sorted(item.name for item in directory.iterdir()) == [path.name]


def test_corrupt_receipt_is_refused_and_not_repaired(tmp_path: Path) -> None:
    inputs = key_inputs()
    receipts.record_receipt(tmp_path, inputs, {"passed": True}, 1)
    path = receipts.receipt_path(tmp_path, inputs)
    path.write_text("{not json", encoding="utf-8")
    corrupt_bytes = path.read_bytes()

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.lookup_receipt(tmp_path, inputs)
    assert caught.value.code == "receipt_corrupt"

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.record_receipt(tmp_path, inputs, {"passed": True}, 1)
    assert caught.value.code == "receipt_corrupt"
    assert path.read_bytes() == corrupt_bytes


def test_tampered_receipt_body_is_refused(tmp_path: Path) -> None:
    inputs = key_inputs()
    receipts.record_receipt(tmp_path, inputs, {"passed": True}, 1)
    path = receipts.receipt_path(tmp_path, inputs)
    body = json.loads(path.read_text(encoding="utf-8"))
    body["result"] = {"passed": False}
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.lookup_receipt(tmp_path, inputs)
    assert caught.value.code == "receipt_corrupt"


def test_receipt_for_another_key_is_refused(tmp_path: Path) -> None:
    first = key_inputs()
    other = key_inputs(command=["python3", "-m", "pytest", "tests/test_other.py", "-q"])
    receipts.record_receipt(tmp_path, first, {"passed": True}, 1)
    source = receipts.receipt_path(tmp_path, first)
    target = receipts.receipt_path(tmp_path, other)
    os.link(source, target)

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.lookup_receipt(tmp_path, other)
    assert caught.value.code == "receipt_corrupt"


def test_live_checks_are_never_cached(tmp_path: Path) -> None:
    inputs = key_inputs()

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.record_receipt(
            tmp_path, inputs, {"passed": True}, 0.1, cacheable=False
        )
    assert caught.value.code == "receipt_not_cacheable"
    assert not (tmp_path / "receipts").exists()
    assert receipts.lookup_receipt(tmp_path, inputs, cacheable=False) is None
    assert receipts.lookup_receipt(tmp_path, inputs) is None


def test_invalid_inputs_are_refused(tmp_path: Path) -> None:
    missing = key_inputs()
    del missing["environment"]
    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.record_receipt(tmp_path, missing, {"passed": True}, 1)
    assert caught.value.code == "receipt_invalid"

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.record_receipt(tmp_path, key_inputs(), {"passed": True}, -1)
    assert caught.value.code == "receipt_invalid"

    with pytest.raises(receipts.ReceiptError) as caught:
        receipts.record_receipt(
            tmp_path, key_inputs(command=object()), {"passed": True}, 1
        )
    assert caught.value.code == "receipt_invalid"
