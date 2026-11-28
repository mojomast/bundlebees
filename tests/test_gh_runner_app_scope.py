"""The sweep's gh calls are app-scoped, never owner-scoped.

``gh`` on the coordinator host is a wrapper: a call carrying ``--repo`` is
served by the coordinator app's installation token scoped to that repository;
a call without it falls back to the operator's personal token. The sweep must
therefore always name its repository, and must fail closed when it cannot.

These tests pin the argv contract (RED before ``_github_slug`` existed) without
network, gh, or a real git remote.
"""

from __future__ import annotations

import pytest

from bundlebees_plugin import beat_reconciliation as br


def _git(result):
    def run(argv):
        assert list(argv) == ["config", "--get", "remote.origin.url"]
        return result

    return run


@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://github.com/mojomast/rizzstack.git", "mojomast/rizzstack"),
        ("https://github.com/mojomast/rizzstack", "mojomast/rizzstack"),
        ("https://x-access-token@github.com/mojomast/workflow.git", "mojomast/workflow"),
        ("git@github.com:mojomast/bundlebees.git", "mojomast/bundlebees"),
        ("ssh://git@github.com/mojomast/hyvv.git", "mojomast/hyvv"),
        ("https://github.com/mojomast/uirizz/", "mojomast/uirizz"),
    ],
)
def test_github_slug_parses_every_remote_form(tmp_path, url, slug):
    assert br._github_slug(tmp_path, git=_git((0, url + "\n", ""))) == slug


@pytest.mark.parametrize(
    "result",
    [
        (1, "", "fatal: No such remote 'origin'"),
        (0, "", ""),
        (0, "https://gitlab.com/mojomast/rizzstack.git\n", ""),
        (0, "not-a-url\n", ""),
    ],
)
def test_github_slug_fails_closed(tmp_path, result):
    with pytest.raises(br.SweepError) as exc:
        br._github_slug(tmp_path, git=_git(result))
    assert exc.value.code == "repo_unresolved"


def test_gh_runner_appends_the_origin_scope(tmp_path, monkeypatch):
    calls = []

    class Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return Proc()

    monkeypatch.setattr(br.shutil, "which", lambda _name: "/usr/local/bin/gh")
    monkeypatch.setattr(br.subprocess, "run", fake_run)

    run = br._gh_runner(tmp_path, git=_git((0, "https://github.com/mojomast/rizzstack.git\n", "")))
    run(["pr", "list", "--state", "open", "--json", "number"])

    assert calls == [
        ["gh", "pr", "list", "--state", "open", "--json", "number", "--repo", "mojomast/rizzstack"]
    ]


@pytest.mark.parametrize(
    "argv,expected_count",
    [
        (["pr", "view", "46", "--repo", "mojomast/rizzstack", "--json", "state"], 1),
        (["pr", "view", "46", "--repo=mojomast/rizzstack", "--json", "state"], 1),
        (["pr", "view", "46", "-R", "mojomast/rizzstack", "--json", "state"], 1),
        (["pr", "view", "46", "-Rmojomast/rizzstack", "--json", "state"], 1),
    ],
)
def test_gh_runner_never_duplicates_an_explicit_scope(tmp_path, monkeypatch, argv, expected_count):
    calls = []

    class Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(br.shutil, "which", lambda _name: "/usr/local/bin/gh")
    monkeypatch.setattr(br.subprocess, "run", lambda cmd, **_kw: (calls.append(list(cmd)), Proc())[1])

    run = br._gh_runner(tmp_path, git=_git((0, "git@github.com:mojomast/rizzstack.git\n", "")))
    run(argv)

    joined = " ".join(calls[0])
    assert joined.count("--repo") + joined.count(" -R") == expected_count
