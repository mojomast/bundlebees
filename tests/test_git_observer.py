"""Read-only Git/PR observer tests (workflow-F3).

The observer compares a campaign checkpoint's recorded revisions with real git
refs and open PR heads and reports drift. These tests pin the three hard edges:
the report carries exact expected-vs-actual values and never resolves anything;
the subprocess surface is read-only and runs on the minimal child environment;
no token-shaped or URL-shaped value from the world can survive into a report.
Every secret-shaped string here is assembled at runtime so repository scanners
see no literal value in the source.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import campaign, git_observer, job as jobs, reconcile, security

REPO_SLUG = "mojomast/campaign-fixture"
BRANCH = "feat/campaign"
JOB = "observer-job"
BASE = "a" * 40
HEAD = "c" * 40
MOVED = "d" * 40
T0 = "2026-11-16T00:00:00Z"


# ---------------------------------------------------------------------------
# Injected worlds
# ---------------------------------------------------------------------------


class GitWorld:
    """A fake read-only git that answers only the observer's exact commands."""

    def __init__(self, *, local=None, remote=None, base_resolves=True, ancestor=True, ahead=1):
        self.local = local
        self.remote = remote
        self.base_resolves = base_resolves
        self.ancestor = ancestor
        self.ahead = ahead
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), cwd))
        # Every world call must stay on the read-only allowlist, whatever the
        # observation is doing; a mutating subcommand would fail the test here.
        assert argv[0] in git_observer._READ_ONLY_GIT, argv
        if argv[0] == "rev-parse":
            target = argv[-1]
            if target.startswith("refs/"):
                sha = self.local if target.startswith("refs/heads/") else self.remote
                if not sha:
                    return (1, "", "")  # --verify --quiet: exit 1 means no such ref
                return (0, f"{sha}\n", "")
            return (0 if self.base_resolves else 1, "", "")
        if argv[0] == "merge-base":
            return (0 if self.ancestor else 1, "", "")
        if argv[0] == "rev-list":
            return (0, f"{self.ahead}\n", "")
        raise AssertionError(f"unexpected git subcommand: {argv[0]}")


class PRWorld:
    """A fake PR source recording the queries it was asked."""

    def __init__(self, *, view=None, open_heads=()):
        self.view = view
        self.open_heads = list(open_heads)
        self.calls = []

    def __call__(self, slug, branch, number):
        self.calls.append((slug, branch, number))
        return {"view": self.view, "open": list(self.open_heads)}


def _view(oid=HEAD, state="open", head_branch=BRANCH, number=7):
    return {"number": number, "state": state, "headRefName": head_branch, "headRefOid": oid}


def _open(number, oid=HEAD):
    return {"number": number, "headRefOid": oid, "headRefName": BRANCH}


def _record(base=BASE, branch=BRANCH, number=7):
    auth = {"auth_id": "auth-1", "repo": REPO_SLUG, "base_rev": base}
    return {
        "schema": "rizzstack.campaign-state/1",
        "campaign_id": "campaign-a",
        "job_id": JOB,
        "authorization": auth,
        "branch": branch,
        "pull_request": number,
    }


# ---------------------------------------------------------------------------
# The pure comparison: exact expected-vs-actual, no resolution
# ---------------------------------------------------------------------------


def test_a_descendant_head_and_an_open_pr_are_not_drift():
    world = GitWorld(local=HEAD, remote=HEAD, ancestor=True, ahead=1)
    prs = PRWorld(view=_view(), open_heads=[_open(7)])

    report = git_observer.observe(_record(), repo="/work", git=world, pull_requests=prs, now=T0)

    assert report["schema"] == git_observer.SCHEMA
    assert report["clean"] is True and report["drift"] == []
    assert report["campaign_id"] == "campaign-a" and report["job_id"] == JOB
    assert report["repo"] == REPO_SLUG and report["observed_at"] == T0
    assert report["scope"] == ["git", "pr"]
    assert report["expected"] == {"base_rev": BASE, "branch": BRANCH, "pull_request": 7}
    assert report["actual"]["local_head"] == HEAD and report["actual"]["remote_head"] == HEAD
    assert report["actual"]["base_rev_resolves"] is True
    assert report["actual"]["descends_from_base"] is True
    assert report["actual"]["ahead_of_base"] == 1
    assert report["actual"]["pull_request"] == {
        "number": 7, "state": "open", "head_branch": BRANCH, "head_oid": HEAD,
    }
    assert world.calls and prs.calls == [(REPO_SLUG, BRANCH, 7)]


def test_a_rewritten_branch_is_reported_moved_with_exact_values():
    world = GitWorld(local=MOVED, remote=None, ancestor=False, ahead=2)

    report = git_observer.observe(_record(), repo="/work", git=world, pull_requests=PRWorld(view=_view()))

    entry = next(item for item in report["drift"] if item["kind"] == "branch_moved")
    assert entry["expected"] == {"base_rev": BASE, "head_descends_from_base": True}
    assert entry["actual"] == {"head": MOVED, "head_descends_from_base": False, "ahead_of_base": 2}
    assert report["clean"] is False


def test_a_vanished_branch_is_reported_missing():
    world = GitWorld(local=None, remote=None)

    report = git_observer.observe(_record(), repo="/work", git=world, pull_requests=PRWorld(view=_view()))

    entry = next(item for item in report["drift"] if item["kind"] == "branch_missing")
    assert entry["expected"] == {"branch": BRANCH, "head": "present"}
    assert entry["actual"] == {"branch": BRANCH, "local_head": None, "remote_head": None}


def test_a_merged_pr_is_reported_not_open():
    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD),
        pull_requests=PRWorld(view=_view(state="merged")),
    )

    entry = next(item for item in report["drift"] if item["kind"] == "pr_merged")
    assert entry["expected"] == {"pull_request": 7, "state": "open"}
    assert entry["actual"] == {"pull_request": 7, "state": "merged"}


def test_a_closed_pr_is_reported_not_open():
    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD),
        pull_requests=PRWorld(view=_view(state="closed")),
    )

    assert [item["kind"] for item in report["drift"]] == ["pr_closed"]


def test_an_unresolvable_recorded_pr_is_reported_missing():
    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD),
        pull_requests=PRWorld(view=None),
    )

    entry = next(item for item in report["drift"] if item["kind"] == "pr_missing")
    assert entry["expected"] == {"pull_request": 7, "state": "open"}
    assert entry["actual"] == {"pull_request": 7, "state": "missing"}


def test_a_pr_head_on_another_branch_is_reported():
    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD),
        pull_requests=PRWorld(view=_view(head_branch="feat/elsewhere")),
    )

    entry = next(item for item in report["drift"] if item["kind"] == "pr_head_branch_changed")
    assert entry["expected"] == {"pull_request": 7, "head_branch": BRANCH}
    assert entry["actual"] == {"pull_request": 7, "head_branch": "feat/elsewhere"}


def test_extra_open_heads_for_the_recorded_branch_are_unexpected():
    prs = PRWorld(view=_view(), open_heads=[_open(7), _open(9, oid=MOVED)])

    report = git_observer.observe(_record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD), pull_requests=prs)

    entry = next(item for item in report["drift"] if item["kind"] == "unexpected_pr_head")
    assert entry["expected"] == {"head_branch": BRANCH, "open_heads": [7]}
    assert entry["actual"] == {"head_branch": BRANCH, "open_heads": [9]}


def test_an_open_head_with_no_recorded_pr_is_unexpected():
    record = _record(number=None)
    prs = PRWorld(open_heads=[_open(9, oid=MOVED)])

    report = git_observer.observe(record, repo="/work", git=GitWorld(local=HEAD, remote=HEAD), pull_requests=prs)

    entry = next(item for item in report["drift"] if item["kind"] == "unexpected_pr_head")
    assert entry["expected"] == {"head_branch": BRANCH, "open_heads": []}
    assert entry["actual"] == {"head_branch": BRANCH, "open_heads": [9]}


def test_drift_is_only_reported_never_resolved():
    """A moved branch and a merged PR produce a report and nothing else."""

    world = GitWorld(local=MOVED, remote=None, ancestor=False, ahead=3)
    prs = PRWorld(view=_view(state="merged"))

    report = git_observer.observe(_record(), repo="/work", git=world, pull_requests=prs)

    assert [item["kind"] for item in report["drift"]] == ["branch_moved", "pr_merged"]
    assert report["clean"] is False
    assert world.calls  # the world was only read; no caller can resolve drift here


def test_an_unobservable_world_is_an_error_never_drift():
    def broken(argv, cwd):
        raise git_observer.GitObserverError("git_failed", "the worktree is gone")

    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe(_record(), repo="/work", git=broken, pull_requests=PRWorld(view=_view()))
    assert caught.value.code == "git_failed"


def test_a_malformed_checkpoint_is_refused_not_guessed():
    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe({})
    assert caught.value.code == "checkpoint_invalid"
    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe(_record(base="not-a-digest"))
    assert caught.value.code == "checkpoint_invalid"
    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe(_record(number=0))
    assert caught.value.code == "checkpoint_invalid"


# ---------------------------------------------------------------------------
# Read-only subprocess surface and the minimal environment
# ---------------------------------------------------------------------------


def test_a_mutating_git_command_is_refused_before_dispatch():
    calls = []

    def runner(argv, cwd):
        calls.append(list(argv))
        return (0, "", "")

    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer._guarded_git(runner, Path("/work"), ["push", "--force", "origin", BRANCH])
    assert caught.value.code == "git_refused"
    assert calls == []


def test_the_default_runners_use_the_minimal_child_environment(monkeypatch, tmp_path):
    ambient = "ghp_" + "B" * 36  # assembled at runtime so the scanners see no value
    monkeypatch.setenv("GH_TOKEN", ambient)
    monkeypatch.setenv("BUNDLEBEES_LAUNCHER_TOKEN", "launcher-" + "C" * 12)
    seen = []

    class _Done:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs.get("env"), kwargs.get("timeout")))
        return _Done()

    monkeypatch.setattr(git_observer.subprocess, "run", fake_run)
    git_observer._default_git(["rev-parse", "HEAD"], str(tmp_path))
    git_observer._default_gh(["pr", "list", "--repo", REPO_SLUG, "--state", "open", "--json", "number"])

    assert [argv[0] for argv, _, _ in seen] == ["git", "gh"]
    for argv, env, timeout in seen:
        assert env == security.child_env()
        assert "GH_TOKEN" not in env and "BUNDLEBEES_LAUNCHER_TOKEN" not in env
        assert ambient not in json.dumps(env)
        assert timeout in (git_observer._GIT_TIMEOUT_S, git_observer._GH_TIMEOUT_S)


def test_the_default_pr_source_issues_exact_read_only_queries():
    seen = []

    def gh(argv):
        seen.append(list(argv))
        if argv[1] == "view":
            return (0, json.dumps(_view()), "")
        return (0, json.dumps([_open(7)]), "")

    world = git_observer.default_pull_requests(REPO_SLUG, BRANCH, 7, gh=gh)

    assert seen[0] == ["pr", "view", "7", "--repo", REPO_SLUG, "--json", "number,state,headRefName,headRefOid"]
    assert seen[1] == [
        "pr", "list", "--repo", REPO_SLUG, "--head", BRANCH, "--state", "open",
        "--json", "number,headRefOid,headRefName",
    ]
    assert world["view"]["state"] == "open" and world["open"][0]["number"] == 7


def test_a_gh_failure_is_scrubbed_and_never_names_its_raw_text():
    ambient = "ghp_" + "D" * 36  # assembled at runtime so the scanners see no value

    def gh(argv):
        return (4, "", "fatal: " + ambient + " at " + "https://" + "gh.corp.invalid/api")

    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.default_pull_requests(REPO_SLUG, BRANCH, 7, gh=gh)

    text = json.dumps(caught.value.to_dict())
    assert caught.value.code == "gh_failed"
    assert ambient not in text and "gh.corp.invalid" not in text
    assert "[redacted]" in text and "[redacted-url]" in text


def test_an_unresolvable_pr_is_a_missing_view_not_a_failure():
    def gh(argv):
        if argv[1] == "view":
            return (1, "", "GraphQL: Could not resolve to a PullRequest with the number of 7.")
        return (0, "[]", "")

    world = git_observer.default_pull_requests(REPO_SLUG, BRANCH, 7, gh=gh)
    assert world == {"view": None, "open": []}


# ---------------------------------------------------------------------------
# The report never carries a token-shaped or URL-shaped value
# ---------------------------------------------------------------------------


def test_free_text_is_scrubbed_while_digests_survive_exactly():
    marker = "ghp_" + "A" * 36  # assembled at runtime so the scanners see no value
    leaky = "feat/" + marker + "/" + "https://" + "git.corp.invalid/" + REPO_SLUG
    prs = PRWorld(view=_view(head_branch=leaky), open_heads=[_open(9, oid=leaky)])

    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD), pull_requests=prs
    )

    blob = json.dumps(report)
    assert marker not in blob and "git.corp.invalid" not in blob
    assert "[redacted]" in blob and "[redacted-url]" in blob
    assert report["actual"]["local_head"] == HEAD and report["actual"]["remote_head"] == HEAD
    assert report["actual"]["pull_request"]["head_oid"] == HEAD
    assert report["actual"]["open_heads"][0]["head_oid"] is None
    assert "[redacted]" in report["actual"]["pull_request"]["head_branch"]
    assert report["expected"]["base_rev"] == BASE and report["expected"]["branch"] == BRANCH


# ---------------------------------------------------------------------------
# Real worktrees: the default git adapter, read-only end to end
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=security.child_env()
    )
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "work"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bundlebees Test")
    _git(repo, "config", "user.email", "bundlebees@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _world(repo: Path) -> str:
    refs = _git(repo, "show-ref") or "(no refs)"
    return refs + "\n" + _git(repo, "status", "--porcelain")


def test_a_real_descendant_branch_head_is_clean(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-qb", BRANCH)
    (repo / "src" / "b.py").write_text("value = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "campaign work")
    head = _git(repo, "rev-parse", "HEAD")

    report = git_observer.observe(_record(base), repo=repo, pull_requests=PRWorld(view=_view(head)))

    assert report["clean"] is True and report["drift"] == []
    assert report["actual"]["local_head"] == head and report["actual"]["remote_head"] is None
    assert report["actual"]["descends_from_base"] is True
    assert report["scope"] == ["git", "pr"]


def test_a_real_rewritten_branch_is_drift_with_exact_values(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-qb", BRANCH)
    _git(repo, "checkout", "-q", "--orphan", "moved")
    (repo / "other.txt").write_text("unrelated\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated root")
    _git(repo, "branch", "-f", BRANCH, "HEAD")
    moved = _git(repo, "rev-parse", BRANCH)

    report = git_observer.observe(_record(base), repo=repo, pull_requests=PRWorld(view=_view()))

    entry = next(item for item in report["drift"] if item["kind"] == "branch_moved")
    assert entry["expected"] == {"base_rev": base, "head_descends_from_base": True}
    assert entry["actual"] == {"head": moved, "head_descends_from_base": False, "ahead_of_base": 1}


def test_a_deleted_branch_is_reported_missing(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    _git(repo, "branch", "-D", BRANCH)

    report = git_observer.observe(_record(base), repo=repo, pull_requests=PRWorld(view=_view()))

    assert [item["kind"] for item in report["drift"]] == ["branch_missing"]
    assert report["actual"]["local_head"] is None and report["actual"]["remote_head"] is None


def test_a_remote_only_head_is_still_observed(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    _git(repo, "update-ref", f"refs/remotes/origin/{BRANCH}", base)
    _git(repo, "branch", "-D", BRANCH)

    report = git_observer.observe(_record(base), repo=repo, pull_requests=PRWorld(view=_view(base)))

    assert report["actual"]["remote_head"] == base and report["actual"]["local_head"] is None
    assert report["clean"] is True


def test_the_observer_never_touches_the_worktree(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    (repo / "src" / "dirty.txt").write_text("dirty\n")
    before = _world(repo)

    report = git_observer.observe(_record(base), repo=repo, pull_requests=PRWorld(view=_view()))

    assert _world(repo) == before
    assert report["clean"] is True


# ---------------------------------------------------------------------------
# The reconcile bridge: report-only against a real campaign checkpoint
# ---------------------------------------------------------------------------


def _campaign(tmp_path: Path, base: str) -> dict:
    jobs.record_authorization(
        tmp_path,
        job_id=JOB,
        statement="Observe real git drift.",
        repo=REPO_SLUG,
        base_rev=base,
        scope=["src/**"],
        bees=1,
        wall_clock_s=60,
        require_tty=False,
        interactive=False,
        by="tester",
    )
    jobs.create_job(
        tmp_path,
        job_id=JOB,
        summary="observer fixture",
        tasks=[{"task_id": "T1", "objective": "only", "lease": ["src/a.py"], "depends_on": []}],
    )
    return campaign.create_campaign(
        tmp_path,
        campaign_id="campaign-a",
        job_id=JOB,
        objective="Observe real git drift.",
        branch=BRANCH,
        pull_request=7,
    )


def test_the_reconcile_bridge_reports_a_clean_world_without_touching_anything(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    record = _campaign(tmp_path, base)
    path = campaign._path(tmp_path, record["campaign_id"])
    before = path.read_bytes()

    report = reconcile.campaign_git_drift(tmp_path, record["campaign_id"], repo=repo, pull_requests=PRWorld(view=_view(base)))

    assert report["campaign_id"] == "campaign-a" and report["job_id"] == JOB
    assert report["expected"] == {"base_rev": base, "branch": BRANCH, "pull_request": 7}
    assert report["clean"] is True
    assert path.read_bytes() == before


def test_drift_is_reported_not_resolved_and_the_checkpoint_is_untouched(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    record = _campaign(tmp_path, base)
    _git(repo, "branch", "-D", BRANCH)
    path = campaign._path(tmp_path, record["campaign_id"])
    before = path.read_bytes()

    report = reconcile.campaign_git_drift(tmp_path, record["campaign_id"], repo=repo, pull_requests=PRWorld(view=_view()))

    assert "branch_missing" in [item["kind"] for item in report["drift"]]
    assert path.read_bytes() == before
    assert campaign.load_campaign(tmp_path, record["campaign_id"])["phase"] == "authorized"


def test_campaign_reconcile_is_unchanged_when_there_is_no_drift(tmp_path: Path):
    repo, base = _repo(tmp_path)
    _git(repo, "branch", BRANCH, base)
    record = _campaign(tmp_path, base)
    first = campaign.reconcile(tmp_path, record["campaign_id"])
    assert first["revision"] == 2
    path = campaign._path(tmp_path, record["campaign_id"])
    before = path.read_bytes()

    report = reconcile.campaign_git_drift(tmp_path, record["campaign_id"], repo=repo, pull_requests=PRWorld(view=_view(base)))
    second = campaign.reconcile(tmp_path, record["campaign_id"])

    assert report["clean"] is True
    assert second["revision"] == first["revision"]
    assert path.read_bytes() == before


def test_an_unknown_campaign_is_a_named_refusal(tmp_path: Path):
    with pytest.raises(campaign.CampaignError) as caught:
        git_observer.observe_campaign(tmp_path, "missing")
    assert caught.value.code == "campaign_missing"


# ---------------------------------------------------------------------------
# Real gh shapes and scrub coverage (independent-review follow-ups on #45)
# ---------------------------------------------------------------------------


def test_real_gh_uppercase_states_are_normalized_not_false_drift():
    world = GitWorld(local=HEAD, remote=HEAD)

    open_report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(state="OPEN")), now=T0
    )
    assert open_report["clean"] is True and open_report["drift"] == []
    assert open_report["actual"]["pull_request"]["state"] == "open"

    merged_report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(state="MERGED")), now=T0
    )
    entry = next(item for item in merged_report["drift"] if item["kind"] == "pr_merged")
    assert entry["actual"] == {"pull_request": 7, "state": "merged"}

    closed_report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(state="CLOSED")), now=T0
    )
    assert [item["kind"] for item in closed_report["drift"]] == ["pr_closed"]
    assert closed_report["actual"]["pull_request"]["state"] == "closed"


def test_drift_facts_and_observed_at_never_carry_world_secrets():
    marker = "ghp_" + "A" * 36  # assembled at runtime so the scanners see no value
    hostile_state = "MERGED " + marker + " https://" + "git.corp.invalid/x"
    hostile_now = marker + " https://" + "scan.invalid/" + "now"

    report = git_observer.observe(
        _record(), repo="/work", git=GitWorld(local=HEAD, remote=HEAD),
        pull_requests=PRWorld(view=_view(state=hostile_state)), now=hostile_now,
    )

    blob = json.dumps(report)
    assert marker not in blob and "git.corp.invalid" not in blob and "scan.invalid" not in blob
    entry = report["drift"][0]
    assert marker not in json.dumps(entry["actual"]) and "git.corp.invalid" not in json.dumps(entry["actual"])
    assert "[redacted]" in json.dumps(entry["actual"])
    assert marker not in report["observed_at"] and "scan.invalid" not in report["observed_at"]
    assert report["observed_at"] != hostile_now
