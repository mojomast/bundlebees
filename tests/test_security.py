"""Security helper tests: scrub, child env, path confinement, URL guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from bundlebees_plugin import security


def test_scrub_redacts_tokens_urls_ips_and_ssh_hosts():
    value = {
        "message": "deploy token=supersecretvalue to http://10.0.0.5:8080/x",
        "authorization": "Bearer abcdefghijklmnop",
        "address": "127.0.0.1",
        "ssh": "deploy@worker.internal",
        "safe": "normal progress message",
    }
    text = str(security.scrub(value))
    assert "supersecretvalue" not in text
    assert "abcdefghijklmnop" not in text
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
    value = "AKIAABCDEFGHIJKLMNOP and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
    text = security.scrub(value)
    assert "AKIAABCDEFGHIJKLMNOP" not in text
    assert "eyJhbGciOiJIUzI1NiJ9" not in text


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
