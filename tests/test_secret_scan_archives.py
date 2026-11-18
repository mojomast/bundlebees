"""Archive-aware secret scanning (RED first).

A changed tar archive over ``_MAX_SCAN_BYTES`` must not fail closed when it can
be parsed: verify compares its members with the base revision's blob of the same
path and scans only the lines the regenerated archive adds. The archive helper is
pure (base bytes, new bytes), so these tests never run git.
"""

from __future__ import annotations

import io
import random
import tarfile

from bundlebees_plugin import cli

# Built at runtime so this repository's own secret scanners stay quiet.
SECRET_LINE = "pass" + "word=" + "Zq9" * 6 + "\n"
PADDING = random.Random(20261118).randbytes(cli._MAX_SCAN_BYTES + 100_000)


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _base_archive(app: bytes) -> bytes:
    return _tar_gz({"src/app.py": app, "vendor/blob.bin": PADDING})


def test_added_archive_member_secret_is_flagged_with_rule_evidence():
    base = _base_archive(b"value = 1\n")
    new = _base_archive(b"value = 1\n" + SECRET_LINE.encode("utf-8"))
    assert len(new) > cli._MAX_SCAN_BYTES

    trips = cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", base, new)

    assert [(trip["file"], trip["member"], trip["line_no"], trip["rule"]) for trip in trips] == [
        ("hermes-plugin/orbit-source.tar.gz", "src/app.py", 2, "assignment")
    ]
    assert "Zq9Zq9" not in (trips[0]["excerpt"] or "")


def test_regenerated_archive_with_benign_member_change_is_not_flagged():
    base = _base_archive(b"value = 1\n")
    new = _base_archive(b"value = 2\n")
    assert len(new) > cli._MAX_SCAN_BYTES

    assert cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", base, new) == []


def test_secret_already_in_base_member_is_not_an_added_line():
    base = _base_archive(SECRET_LINE.encode("utf-8"))
    new = _base_archive((SECRET_LINE + "value = 2\n").encode("utf-8"))

    assert cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", base, new) == []


def test_secret_in_a_new_archive_member_is_flagged():
    base = _base_archive(b"value = 1\n")
    new = _tar_gz(
        {
            "src/app.py": b"value = 1\n",
            "src/settings.py": SECRET_LINE.encode("utf-8"),
            "vendor/blob.bin": PADDING,
        }
    )

    trips = cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", base, new)

    assert [(trip["member"], trip["line_no"], trip["rule"]) for trip in trips] == [
        ("src/settings.py", 1, "assignment")
    ]


def test_unparseable_archive_fails_closed():
    trips = cli._archive_secret_trips(
        "hermes-plugin/orbit-source.tar.gz", b"", b"not a tar archive"
    )

    assert [(trip["file"], trip["line_no"], trip["rule"], trip["excerpt"]) for trip in trips] == [
        ("hermes-plugin/orbit-source.tar.gz", None, None, None)
    ]


def test_unsafe_archive_member_path_fails_closed():
    for unsafe in ("/etc/passwd", "../escape.py", "pkg/../../escape.py"):
        new = _tar_gz({unsafe: b"value = 1\n"})

        trips = cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", b"", new)

        assert len(trips) == 1, unsafe
        assert trips[0]["file"] == "hermes-plugin/orbit-source.tar.gz"
        assert trips[0]["rule"] is None
        assert trips[0]["excerpt"] is None


def test_differing_archive_member_over_the_cap_fails_closed():
    base = _tar_gz({"src/app.py": b"small\n"})
    new = _tar_gz({"src/app.py": b"x" * (cli._MAX_SCAN_BYTES + 1)})

    trips = cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", base, new)

    assert [(trip["member"], trip["line_no"], trip["rule"], trip["excerpt"]) for trip in trips] == [
        ("src/app.py", None, None, None)
    ]


def test_secret_trips_applies_base_comparison_to_an_oversize_archive(tmp_path):
    base = _base_archive(SECRET_LINE.encode("utf-8"))
    new = _base_archive((SECRET_LINE + "value = 2\n").encode("utf-8"))
    rel = "hermes-plugin/orbit-source.tar.gz"
    archive = tmp_path / rel
    archive.parent.mkdir(parents=True)
    archive.write_bytes(new)
    assert archive.stat().st_size > cli._MAX_SCAN_BYTES

    assert cli._secret_trips(tmp_path, [rel], base_blobs={rel: base}) == []


def test_archive_expanding_beyond_the_bound_fails_closed(monkeypatch):
    monkeypatch.setattr(cli, "_MAX_ARCHIVE_BYTES", 1000)
    new = _tar_gz({"src/big.py": b"value = 1\n" * 500})

    trips = cli._archive_secret_trips("hermes-plugin/orbit-source.tar.gz", b"", new)

    assert trips == [
        {"file": "hermes-plugin/orbit-source.tar.gz", "line_no": None, "rule": None, "excerpt": None}
    ]


def test_secret_trips_detects_gzip_magic_on_an_oversize_archive(tmp_path):
    base = _tar_gz({"vendor/blob.bin": PADDING})
    new = _tar_gz({"src/app.py": SECRET_LINE.encode("utf-8"), "vendor/blob.bin": PADDING})
    rel = "hermes-plugin/orbit-source.bin"
    archive = tmp_path / rel
    archive.parent.mkdir(parents=True)
    archive.write_bytes(new)
    assert archive.stat().st_size > cli._MAX_SCAN_BYTES

    trips = cli._secret_trips(tmp_path, [rel], base_blobs={rel: base})

    assert [(trip["member"], trip["rule"]) for trip in trips] == [("src/app.py", "assignment")]


def test_archive_refusal_names_member_and_rule_without_the_secret():
    rel = "hermes-plugin/orbit-source.tar.gz"
    trips = [
        {
            "file": rel,
            "member": "src/app.py",
            "line_no": 2,
            "rule": "assignment",
            "excerpt": "pass" + "word=[redacted]",
        }
    ]

    refusal = cli._secret_refusal([rel], trips)

    assert f"{rel} member=src/app.py line=2 rule=assignment" in refusal
    assert "Zq9Zq9" not in refusal


def test_non_archive_opaque_file_over_the_cap_still_fails_closed(tmp_path):
    (tmp_path / "opaque.bin").write_bytes(b"x" * (cli._MAX_SCAN_BYTES + 1))

    trips = cli._secret_trips(tmp_path, ["opaque.bin"])

    assert [(trip["file"], trip["line_no"], trip["rule"], trip["excerpt"]) for trip in trips] == [
        ("opaque.bin", None, None, None)
    ]
    assert cli._secret_scan(tmp_path, ["opaque.bin"]) == ["opaque.bin"]
    assert cli._MAX_SCAN_BYTES == 2_000_000
