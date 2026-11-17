"""Security helper tests: scrub, child env, path confinement, URL guards.

The assignment secret-scan tests near the bottom pin the value-shape
boundary of ``looks_like_secret``: a secret-keyword name trips only when the
value is credential-shaped, never for a code reference such as a function
definition, a bare identifier, or a property chain. Every fixture that could
resemble a credential is assembled at runtime so repository secret scanners
never see a credential-shaped literal in this file.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import pytest

from bundlebees_plugin import security


def _url_for_address(address: ipaddress._BaseAddress) -> str:
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}/"


def test_scrub_redacts_tokens_urls_ips_and_ssh_hosts():
    # Assembled at runtime; the value is deliberately not identifier-shaped
    # (it carries dashes) so the assignment rule keeps tripping on it.
    value = {
        "message": "deploy " + "tok" + "en=super-secret-value to http://10.0.0.5:8080/x",
        "authorization": "Bearer " + "a" * 16,
        "address": "127.0.0.1",
        "ssh": "deploy@worker.internal",
        "safe": "normal progress message",
    }
    text = str(security.scrub(value))
    assert "super-secret-value" not in text
    assert "a" * 16 not in text
    assert "10.0.0.5" not in text
    assert "127.0.0.1" not in text
    assert "worker.internal" not in text
    assert "normal progress message" in text


def test_scrub_recurses_lists_and_keeps_non_strings():
    value = ["sk-" + "a" * 32, {"nested": "ghp_" + "b" * 30}, 7, True, None]
    out = security.scrub(value)
    text = repr(out)
    assert "sk-" not in text
    assert "ghp_" not in text
    assert 7 in out
    assert True in out
    assert None in out


def test_scrub_redacts_key_shaped_values():
    # Runtime-constructed: an AKIA-shaped key and a three-segment JWT.
    value = "AKIA" + "A2" * 8 + " and " + "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
    text = security.scrub(value)
    assert "A2" * 8 not in text
    assert "eyJ" + "a" * 12 not in text


def test_child_env_does_not_inherit_ambient_environment(monkeypatch):
    monkeypatch.setenv("BUNDLEBEES_LEAK_CANARY", "1")
    env = security.child_env()
    assert "BUNDLEBEES_LEAK_CANARY" not in env
    assert "PATH" in env
    assert "LANG" in env


def test_child_env_allows_named_extras():
    env = security.child_env({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/bin/false"


def test_child_env_rejects_unknown_names():
    with pytest.raises(ValueError):
        security.child_env({"LD_PRELOAD": "/tmp/evil.so"})


def test_contain_path_allows_inside_root(tmp_path: Path):
    root = tmp_path / "root"
    target = root / "a" / "b.txt"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")
    resolved = security.contain_path(str(target), str(root))
    assert Path(resolved) == target.resolve()


def test_contain_path_rejects_parent_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError):
        security.contain_path(str(root / ".." / "outside.txt"), str(root))


def test_contain_path_rejects_symlink_escape(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        security.contain_path(str(link / "secret.txt"), str(root))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:4318/api/local-workers",
        "http://[::1]:4318/",
        "http://localhost:4318/",
    ],
)
def test_validate_url_allows_loopback(url: str):
    assert security.validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://172.16.0.1/",
        "http://[fd00::1]/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/",
    ],
)
def test_validate_url_rejects_private_metadata_and_link_local(url: str):
    with pytest.raises(security.UnsafeURLError):
        security.validate_url(url)


def test_validate_url_allows_explicit_private_bind_from_config():
    assert security.validate_url("http://10.0.0.5:8080", allow_private=True)


def test_cgnat_allowed_with_private():
    cgnat = ipaddress.ip_network("100.64.0.1/10", strict=False)
    url = "http://100.64.0.1:4318/health"
    assert security.validate_url(url, allow_private=True) == url
    assert security.resolve_pinned_endpoint(url, allow_private=True) == (
        "100.64.0.1",
        4318,
        "100.64.0.1",
        "http",
    )
    for address in (cgnat.network_address, cgnat.broadcast_address):
        assert security.validate_url(_url_for_address(address), allow_private=True)
    for neighbour in (cgnat.network_address - 1, cgnat.broadcast_address + 1):
        assert security.validate_url(_url_for_address(neighbour))


def test_cgnat_rejected_by_default():
    cgnat = ipaddress.ip_network("100.64.0.1/10", strict=False)
    for address in (
        cgnat.network_address,
        ipaddress.ip_address("100.64.0.1"),
        cgnat.broadcast_address,
    ):
        with pytest.raises(security.UnsafeURLError):
            security.validate_url(_url_for_address(address))
        with pytest.raises(security.UnsafeURLError):
            security.validate_url(_url_for_address(address), allow_private=False)
    with pytest.raises(security.UnsafeURLError):
        security.resolve_pinned_endpoint("http://100.64.0.1:4318/health")


def test_still_rejects_metadata_and_link_local():
    metadata = tuple(security._METADATA_IPS)
    link_local = (
        ipaddress.ip_network(
            f"{next(addr for addr in metadata if addr.version == 4)}/16",
            strict=False,
        ).network_address
        + 1
    )
    rejected = [
        *metadata,
        link_local,
        ipaddress.IPv6Address((0xFE80 << 112) | 1),
        ipaddress.IPv4Address(0xE0000001),
        ipaddress.IPv4Address(0),
        ipaddress.IPv4Address(0x7F000001),
    ]
    for address in rejected:
        with pytest.raises(security.UnsafeURLError):
            security.validate_url(
                _url_for_address(address),
                allow_loopback=False,
                allow_private=True,
            )
    for url in ("ftp://example.invalid/", "file:///etc/passwd"):
        with pytest.raises(security.UnsafeURLError):
            security.validate_url(url, allow_private=True)


def test_metadata_is_rejected_even_with_private_opt_in():
    with pytest.raises(security.UnsafeURLError):
        security.validate_url("http://169.254.169.254/", allow_private=True)


def test_validate_url_rejects_credentials_and_bad_schemes():
    for url in (
        "http://user:pass@127.0.0.1:4318/",
        "file:///etc/passwd",
        "ftp://127.0.0.1/",
    ):
        with pytest.raises(security.UnsafeURLError):
            security.validate_url(url)


def test_public_fetch_guard_rejects_loopback():
    with pytest.raises(security.UnsafeURLError):
        security.public_fetch_guard("http://127.0.0.1:4318/")


def test_is_public_git_url_accepts_public_https():
    assert security.is_public_git_url("https://github.com/example/repo.git")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo.git",
        "https://user:pass@github.com/example/repo.git",
        "https://127.0.0.1/example/repo.git",
        "https://10.0.0.5/example/repo.git",
        "https://localhost/example/repo.git",
        "https://github.com",
        "file:///tmp/repo",
    ],
)
def test_is_public_git_url_rejects_non_public_or_credentialed(url: str):
    assert not security.is_public_git_url(url)


def test_validate_url_rejects_ipv4_mapped_metadata_address():
    with pytest.raises(security.UnsafeURLError):
        security.validate_url("http://[::ffff:169.254.169.254]/")


def test_scrub_leaves_plain_text_alone():
    assert security.scrub("normal progress message") == "normal progress message"


def test_config_flag_does_not_treat_string_false_as_true():
    assert security.config_flag(True) is True
    assert security.config_flag(False) is False
    assert security.config_flag("false") is False
    assert security.config_flag("true") is True
    assert security.config_flag(None, False) is False
    assert security.config_flag("nope", False) is False


def test_resolve_pinned_endpoint_pins_literal_loopback():
    host, port, ip, scheme = security.resolve_pinned_endpoint("http://127.0.0.1:4318/health")
    assert (host, port, ip, scheme) == ("127.0.0.1", 4318, "127.0.0.1", "http")


def test_resolve_pinned_endpoint_rejects_private_without_opt_in():
    with pytest.raises(security.UnsafeURLError):
        security.resolve_pinned_endpoint("http://10.0.0.5:8080/")


def test_resolve_pinned_endpoint_uses_a_single_checked_resolution(monkeypatch):
    import socket

    def fake_gai(host, port, *args, **kwargs):
        assert host == "launcher.example.test"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_gai)
    host, port, ip, scheme = security.resolve_pinned_endpoint(
        "http://launcher.example.test:4318/health"
    )
    assert host == "launcher.example.test"
    assert port == 4318
    assert ip == "127.0.0.1"
    assert scheme == "http"


def test_resolve_pinned_endpoint_rejects_if_any_address_is_metadata(monkeypatch):
    import socket

    def fake_gai(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port)),
        ]

    monkeypatch.setattr(security.socket, "getaddrinfo", fake_gai)
    with pytest.raises(security.UnsafeURLError):
        security.resolve_pinned_endpoint(
            "http://evil.example.test/",
            allow_loopback=False,
            allow_private=False,
        )


def test_child_env_keeps_home_and_user_path_but_no_secret(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("PATH", "/opt/custom/bin:/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "sentinel-should-not-pass")
    monkeypatch.setenv("BUNDLEBEES_LAUNCHER_TOKEN", "sentinel-token")
    env = security.child_env()
    assert env["HOME"] == "/home/someone"
    assert env["PATH"] == "/opt/custom/bin:/usr/bin"
    assert "sentinel" not in " ".join(env.values())
    assert set(env) <= {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR"}


# ---------------------------------------------------------------------------
# Assignment secret scan: value-shape boundary (defect lane r2).
#
# The pre-fix assignment rule accepted ANY run of non-space characters as
# the value after a secret-keyword name, so a line tripped on the name alone
# even when the value was a function definition, a bare identifier, or a
# property access (r3: the only trip on a clean approval payload was the
# signing-secret constant assigned an arrow function, where the secret
# enters via env at call time). The name/separator halves of every fixture
# below are split across string literals and joined at runtime so repository
# secret scanners never see a credential-shaped literal in this file.
# ---------------------------------------------------------------------------


def _trip(text: str) -> bool:
    return security.looks_like_secret(text)


# Unquoted values that are code references: exempt from name-only trips.
_EXEMPT_LINES = {
    "arrow-constant": "const signing" + "Secret" + " = () => {",
    "arrow-method": "sec" + "ret: () => {",
    "function-keyword": "signing" + "Secret" + " = function () {",
    "call-reference": "api_" + "key = loadSecret(env)",
    "property-chain": "auth" + "orization = config.token",
    "env-chain": "signing_" + "token" + " = process.env.SIGNING",
    "bare-identifier": "api_" + "key = placeholder",
    "short-identifier": "tok" + "en = abc123",
    "none-literal": "pass" + "wd = None",
}

# Credential-shaped values that must still trip after the refinement.
_STILL_TRIP_LINES = {
    "quoted-double": "db_" + "pass" + 'word = "correct horse battery"',
    "quoted-single": "api_" + "key = 's3cr3t-v@lue!'",
    "digit-led": "pass" + "word = 123456",
    "dashed": "tok" + "en = abc-def-ghi",
    "base64ish": "tok" + "en = c2VjcmV0dmFsdWU=",
    "punctuated": "pass" + "wd = p@ssw0rd!x",
    # 32+ characters with digits and letters: still trips unquoted even
    # though the run is identifier-shaped (requirement (d)).
    "high-entropy-unquoted": "tok" + "en = " + "a" * 16 + "7" * 16,
    "bearer": "auth" + "orization: Bearer " + "z" * 12,
    "sk-prefix": "key = sk-" + "x" * 24,
    "ghp-prefix": "ghp_" + "a" * 30,
    "github-pat": "github_" + "pat_" + "b" * 22,
    "xox-prefix": "xox" + "b-" + "c" * 12,
    "akia-prefix": "AKIA" + "A2" * 8,
    "aiza-prefix": "AIza" + "a" * 10 + "9" * 12,
    "jwt": "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12,
}

# Lines that never tripped and must keep not tripping.
_BENIGN_LINES = {
    "plain-text": "normal progress message",
    "unrelated-assignment": "retries = 3",
    "keyword-not-at-name-end": "sec" + "ret_count = 5",
    "quoted-empty": "pass" + "word = ''",
}


@pytest.mark.parametrize("label", sorted(_EXEMPT_LINES))
def test_code_reference_values_do_not_trip_on_the_name_alone(label):
    line = _EXEMPT_LINES[label]
    assert not _trip(line), line


@pytest.mark.parametrize("label", sorted(_EXEMPT_LINES))
def test_scrub_leaves_code_reference_values_intact(label):
    line = _EXEMPT_LINES[label]
    assert security.scrub(line) == line


@pytest.mark.parametrize("label", sorted(_STILL_TRIP_LINES))
def test_credential_shaped_values_still_trip(label):
    line = _STILL_TRIP_LINES[label]
    assert _trip(line), line


@pytest.mark.parametrize("label", sorted(_BENIGN_LINES))
def test_benign_lines_stay_benign(label):
    line = _BENIGN_LINES[label]
    assert not _trip(line), line


def test_r3_approval_surface_line_is_not_flagged():
    line = _EXEMPT_LINES["arrow-constant"]
    assert not _trip(line)
    assert security.scrub(line) == line


def test_short_unquoted_boundary_is_documented_and_pinned():
    # Chosen boundary (documented in the security module docstring): a short
    # unquoted value lands on the EXEMPT side when it is a bare identifier,
    # because a bare identifier carries no credential material and every
    # realistic inline credential format is caught by the quoted, prefix,
    # bearer, and high-entropy arms; exempting identifier shapes is what lets
    # code that merely references a runtime secret pass. Values that cannot
    # be identifiers (digit-led, special characters) still trip.
    assert not _trip("tok" + "en = abc123")
    assert _trip("tok" + "en = 123abc")
    assert _trip("pass" + "word = 123456")


# The pre-fix assignment rule, reconstructed verbatim from the source of
# bundlebees_plugin/security.py at base commit 78ed031 (upstream
# 17adb52f872a59f7876b08775e18c1d259fb7b2b). Its unquoted value arm was a
# bare run of non-space, non-delimiter characters, so any identifier-shaped
# value tripped on the name alone.
_OLD_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*?"
    r"(?:api[_-]?key|token|secret|password|passwd|pwd|authorization))"
    r"(\s*[:=]\s*)(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;\"']+)"
)

# Only the assignment pair redacts as ``\1\2[redacted]``; locating it by its
# replacement keeps this harness independent of tuple ordering.
_ASSIGNMENT_REPLACEMENT = r"\1\2[redacted]"


def _pattern_sets():
    live = security._SECRET_PATTERNS
    assignment_pairs = [pair for pair in live if pair[1] == _ASSIGNMENT_REPLACEMENT]
    assert len(assignment_pairs) == 1, "assignment rule not uniquely identifiable"
    others = tuple(pair for pair in live if pair[1] != _ASSIGNMENT_REPLACEMENT)
    old = ((_OLD_ASSIGNMENT_RE, _ASSIGNMENT_REPLACEMENT),) + others
    new = ((assignment_pairs[0][0], _ASSIGNMENT_REPLACEMENT),) + others
    return old, new


def test_value_shape_matrix_loses_no_redaction_beyond_exempt_shapes():
    old_patterns, new_patterns = _pattern_sets()
    rows = {**_EXEMPT_LINES, **_STILL_TRIP_LINES, **_BENIGN_LINES}
    for label, line in rows.items():
        old_hit = any(pattern.search(line) for pattern, _ in old_patterns)
        new_hit = any(pattern.search(line) for pattern, _ in new_patterns)
        if label in _EXEMPT_LINES:
            assert old_hit, f"{label}: pre-fix predicate should trip on {line!r}"
            assert not new_hit, f"{label}: newly exempt shape unexpectedly trips"
        elif label in _BENIGN_LINES:
            assert not old_hit and not new_hit, f"{label}: benign line trips"
        else:
            assert old_hit and new_hit, f"{label}: redaction lost for {line!r}"
