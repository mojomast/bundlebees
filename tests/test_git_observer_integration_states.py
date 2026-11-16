"""The seven PR integration-state observations (orbitdesktop #34 class).

The real failure: orbitdesktop PR #34 (head ``94123f2a…``) was integrated — the
exact head commit is an ancestor of main (``16b86209…``) — while GitHub kept
reporting the PR open with no merge commit, and it was later closed with **no**
merged event. A coordinator that trusts only GitHub's PR metadata concludes the
work is not integrated and either waits forever or integrates again.

The observer must classify every PR state the world can present, from git
evidence and the PR API together, and never act on any of it:

1. a normal open PR whose head is *not* on main — clean;
2. a properly merged PR — ``pr_merged``;
3. a closed-unmerged PR whose head is not on main — ``pr_closed``;
4. a PR whose exact head is already an ancestor of main while GitHub still
   reports it open/unmerged — ``integrated_not_pr_merged`` (the #34 case);
5. equivalent content on main (a cherry-pick-like patch) with the PR's exact
   head *not* an ancestor — ``equivalent_content_unverified``, explicitly NOT
   treated as a proved merge;
6. branch movement after the PR's recorded head — ``pr_head_moved``;
7. stale or incomplete API observations (a head oid absent locally, a view
   without a head oid, a missing main ref) — ``unverifiable``, never classified.

Every test here is read-only by construction: the fake world refuses any
subcommand outside the observer's allowlist, so a mutating "fix" would fail the
test instead of the repository.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import campaign, git_observer, job as jobs, reconcile, security

REPO_SLUG = "mojomast/orbitdesktop"
BRANCH = "feat/acdd-evidence"
JOB = "observer-job-34"
BASE = "a" * 40
# The real orbitdesktop #34 revisions: the PR head is an ancestor of main.
PR34_HEAD = "94123f2a7f05e89675111c578afb31624bb2af7e"
MAIN = "16b8620997fd47717005ba8d5b31b8086aef1175"
MOVED = "e" * 40
OLD_HEAD = "7" * 40
T0 = "2026-11-16T00:00:00Z"


# ---------------------------------------------------------------------------
# Injected worlds
# ---------------------------------------------------------------------------


class GitWorld:
    """A fake read-only git that answers only the observer's exact commands.

    ``refs`` maps ref names without the ``refs/`` prefix (``heads/feat/x``,
    ``remotes/origin/main``) to revisions; ``known`` is the set of revisions
    ``rev-parse --verify <rev>^{commit}`` resolves; ``ancestors`` holds the
    ``(ancestor, descendant)`` pairs ``merge-base --is-ancestor`` accepts; the
    two ``rev-list`` counters feed the cherry-pick equivalence probe.
    """

    def __init__(
        self,
        *,
        refs=None,
        known=(),
        ancestors=(),
        ahead=1,
        base_resolves=True,
        pr_side=1,
        cherry_missing=1,
    ):
        self.refs = dict(refs or {})
        self.known = set(known)
        self.base_resolves = base_resolves
        self.ancestors = set(ancestors)
        self.ahead = ahead
        self.pr_side = pr_side
        self.cherry_missing = cherry_missing
        self.calls = []

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        assert argv[0] in git_observer._READ_ONLY_GIT, argv
        if argv[0] == "rev-parse":
            target = argv[-1]
            if target.startswith("refs/"):
                sha = self.refs.get(target[len("refs/"):])
                return (0, f"{sha}\n", "") if sha else (1, "", "")
            rev = target[: -len("^{commit}")]
            return (0, f"{rev}\n", "") if rev in self.known else (1, "", "")
        if argv[0] == "merge-base":
            return (0, "", "") if (argv[-2], argv[-1]) in self.ancestors else (1, "", "")
        if argv[0] == "rev-list":
            if "--right-only" in argv:
                count = self.cherry_missing if "--cherry-pick" in argv else self.pr_side
            else:
                count = self.ahead
            return (0, f"{count}\n", "")
        raise AssertionError(f"unexpected git subcommand: {argv[0]}")

    def mutations(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] not in git_observer._READ_ONLY_GIT]


class PRWorld:
    """A fake PR source recording the queries it was asked."""

    def __init__(self, *, view=None, open_heads=()):
        self.view = view
        self.open_heads = list(open_heads)
        self.calls = []

    def __call__(self, slug, branch, number):
        self.calls.append((slug, branch, number))
        return {"view": self.view, "open": list(self.open_heads)}


def _view(oid=PR34_HEAD, state="open", head_branch=BRANCH, number=34):
    view = {"number": number, "state": state, "headRefName": head_branch, "headRefOid": oid}
    return {key: value for key, value in view.items() if value is not None}


def _open(number, oid=PR34_HEAD):
    return {"number": number, "headRefOid": oid, "headRefName": BRANCH}


def _record(base=BASE, branch=BRANCH, number=34):
    auth = {"auth_id": "auth-1", "repo": REPO_SLUG, "base_rev": base}
    return {
        "schema": "rizzstack.campaign-state/1",
        "campaign_id": "campaign-34",
        "job_id": JOB,
        "authorization": auth,
        "branch": branch,
        "pull_request": number,
    }


def _34_world(*, state="open", head=PR34_HEAD, main=MAIN, main_ref=True):
    """The real #34 world: the PR head is an ancestor of authoritative main."""
    refs = {
        f"heads/{BRANCH}": head,
        f"remotes/origin/{BRANCH}": head,
    }
    if main_ref:
        refs["remotes/origin/main"] = main
    return GitWorld(
        refs=refs,
        known={BASE, PR34_HEAD, MAIN, head, main} - {None},
        ancestors={(BASE, head), (head, main)} - {(None, None)},
        ahead=1,
    )


def _kinds(report) -> list[str]:
    return [item["kind"] for item in report["drift"]]


# ---------------------------------------------------------------------------
# Case 4 — the #34 regression: integrated, not PR-merged
# ---------------------------------------------------------------------------


def test_orbitdesktop_34_open_pr_head_already_ancestor_of_main():
    """PR #34: head 94123f2 is an ancestor of main 16b8620, GitHub says open."""
    world = _34_world()
    # The real API response reported the PR open with no merge commit; the
    # observer must classify from what it can prove, not from the absent field.
    view = _view(state="open", head_branch=BRANCH, number=34)
    view["mergeCommit"] = None
    prs = PRWorld(view=view)

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=prs, now=T0, main_ref="main"
    )

    assert "integrated_not_pr_merged" in _kinds(report)
    entry = next(item for item in report["drift"] if item["kind"] == "integrated_not_pr_merged")
    assert entry["expected"] == {"pull_request": 34, "integrated": False}
    assert entry["actual"] == {
        "pull_request": 34,
        "integrated": True,
        "pr_head": PR34_HEAD,
        "main_head": MAIN,
        "main_ref": "main",
        "github_state": "open",
        "github_reports_merged": False,
    }
    assert PR34_HEAD in json.dumps(entry["actual"]) and MAIN in json.dumps(entry["actual"])
    assert "not merged" in entry["detail"] and "already an ancestor" in entry["detail"]
    assert report["clean"] is False
    integration = report["actual"]["integration"]
    assert integration["verdict"] == "integrated_not_pr_merged"
    assert integration["head_ancestor_of_main"] is True
    assert integration["main_head"] == MAIN and integration["pr_head"] == PR34_HEAD
    assert "integration" in report["scope"]


def test_the_34_epilogue_closed_without_merge_is_still_integrated_not_pr_merged():
    """Closed with NO merged event, the head on main still proves integration."""
    report = git_observer.observe(
        _record(),
        repo="/work",
        git=_34_world(),
        pull_requests=PRWorld(view=_view(state="closed")),
        now=T0,
        main_ref="main",
    )

    assert "integrated_not_pr_merged" in _kinds(report)
    entry = next(item for item in report["drift"] if item["kind"] == "integrated_not_pr_merged")
    assert entry["actual"]["github_state"] == "closed"
    assert report["actual"]["integration"]["verdict"] == "integrated_not_pr_merged"


# ---------------------------------------------------------------------------
# Cases 1-3 — the states that were already observable
# ---------------------------------------------------------------------------


def test_a_normal_open_pr_head_off_main_is_clean():
    """Case 1: an open PR whose head is not on main is not drift."""
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": PR34_HEAD,
            f"remotes/origin/{BRANCH}": PR34_HEAD,
            "remotes/origin/main": MAIN,
        },
        known={BASE, PR34_HEAD, MAIN},
        ancestors={(BASE, PR34_HEAD)},  # descends from base, NOT from main
        ahead=1,
        pr_side=1,
        cherry_missing=1,
    )

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view()), now=T0, main_ref="main"
    )

    assert report["drift"] == [] and report["clean"] is True
    assert report["actual"]["integration"]["verdict"] == "not_integrated"
    assert report["actual"]["integration"]["head_ancestor_of_main"] is False
    assert report["actual"]["integration"]["equivalent_on_main"] is False


def test_a_properly_merged_pr_reports_pr_merged():
    """Case 2: GitHub reporting merged stays the pr_merged drift, no anomaly."""
    report = git_observer.observe(
        _record(),
        repo="/work",
        git=_34_world(),
        pull_requests=PRWorld(view=_view(state="merged")),
        now=T0,
        main_ref="main",
    )

    assert _kinds(report) == ["pr_merged"]
    assert report["actual"]["integration"]["verdict"] == "merged"


def test_a_closed_unmerged_pr_head_off_main_reports_pr_closed():
    """Case 3: closed without a merge and nothing on main is only pr_closed."""
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": PR34_HEAD,
            f"remotes/origin/{BRANCH}": PR34_HEAD,
            "remotes/origin/main": MAIN,
        },
        known={BASE, PR34_HEAD, MAIN},
        ancestors={(BASE, PR34_HEAD)},
        pr_side=1,
        cherry_missing=1,
    )

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(state="closed")), now=T0, main_ref="main"
    )

    assert _kinds(report) == ["pr_closed"]
    assert report["actual"]["integration"]["verdict"] == "not_integrated"


# ---------------------------------------------------------------------------
# Case 5 — equivalent content is never a proved merge
# ---------------------------------------------------------------------------


def test_a_cherry_picked_equivalent_patch_is_not_a_proved_merge():
    """Case 5: a patch-equivalent commit on main is NOT integrated_not_merged."""
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": PR34_HEAD,
            f"remotes/origin/{BRANCH}": PR34_HEAD,
            "remotes/origin/main": MAIN,
        },
        known={BASE, PR34_HEAD, MAIN},
        ancestors={(BASE, PR34_HEAD)},  # head NOT an ancestor of main
        pr_side=1,
        cherry_missing=0,  # every PR commit has a patch-equivalent on main
    )

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view()), now=T0, main_ref="main"
    )

    assert "integrated_not_pr_merged" not in _kinds(report)
    assert "equivalent_content_unverified" in _kinds(report)
    entry = next(item for item in report["drift"] if item["kind"] == "equivalent_content_unverified")
    assert entry["actual"]["integrated"] == "unverified"
    assert entry["actual"]["pr_head"] == PR34_HEAD and entry["actual"]["main_head"] == MAIN
    assert entry["actual"]["pr_commits_without_patch_equivalent_on_main"] == 0
    integration = report["actual"]["integration"]
    assert integration["verdict"] == "equivalent_content_unverified"
    assert integration["head_ancestor_of_main"] is False
    assert integration["equivalent_on_main"] is True


# ---------------------------------------------------------------------------
# Case 6 — the branch moved after the recorded head
# ---------------------------------------------------------------------------


def test_branch_moved_after_the_recorded_head_is_reported():
    """Case 6: the branch advanced past the head the PR still reports."""
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": MOVED,
            f"remotes/origin/{BRANCH}": MOVED,
            "remotes/origin/main": MAIN,
        },
        known={BASE, OLD_HEAD, MOVED, MAIN},
        ancestors={(BASE, MOVED), (OLD_HEAD, MOVED)},
        ahead=2,
        pr_side=1,
        cherry_missing=1,
    )

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(oid=OLD_HEAD)), now=T0, main_ref="main"
    )

    entry = next(item for item in report["drift"] if item["kind"] == "pr_head_moved")
    assert entry["expected"] == {"pull_request": 34, "head_oid": OLD_HEAD}
    assert entry["actual"] == {"pull_request": 34, "head_oid": MOVED, "branch": BRANCH}
    assert report["actual"]["integration"]["pr_head"] == OLD_HEAD


# ---------------------------------------------------------------------------
# Case 7 — stale or incomplete API observations are never classified
# ---------------------------------------------------------------------------


def test_a_stale_head_oid_absent_locally_is_unverifiable():
    """Case 7a: an API head the worktree cannot resolve proves nothing."""
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": PR34_HEAD,
            f"remotes/origin/{BRANCH}": PR34_HEAD,
            "remotes/origin/main": MAIN,
        },
        known={BASE, MAIN},  # the reported head oid is unknown locally
        ancestors={(PR34_HEAD, MAIN)},
    )

    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view()), now=T0, main_ref="main"
    )

    assert "integrated_not_pr_merged" not in _kinds(report)
    assert "equivalent_content_unverified" not in _kinds(report)
    integration = report["actual"]["integration"]
    assert integration["verdict"] == "unverifiable"
    assert integration["head_ancestor_of_main"] is None


def test_an_incomplete_view_without_head_oid_is_unverifiable():
    """Case 7b: a view without headRefOid must never be classified integrated."""
    report = git_observer.observe(
        _record(),
        repo="/work",
        git=_34_world(),
        pull_requests=PRWorld(view=_view(oid=None)),
        now=T0,
        main_ref="main",
    )

    assert "integrated_not_pr_merged" not in _kinds(report)
    integration = report["actual"]["integration"]
    assert integration["verdict"] == "unverifiable"
    assert integration["pr_head"] is None


def test_a_missing_main_ref_is_unverifiable_not_drift():
    """Case 7c: an unresolvable main ref is unobservable, never integrated."""
    report = git_observer.observe(
        _record(),
        repo="/work",
        git=_34_world(main_ref=False),
        pull_requests=PRWorld(view=_view()),
        now=T0,
        main_ref="main",
    )

    assert "integrated_not_pr_merged" not in _kinds(report)
    integration = report["actual"]["integration"]
    assert integration["observed"] is True
    assert integration["main_head"] is None
    assert integration["verdict"] == "unverifiable"


# ---------------------------------------------------------------------------
# Backward compatibility and the read-only guarantee
# ---------------------------------------------------------------------------


def test_integration_evidence_is_absent_without_a_main_ref():
    """Without a named main ref the observer behaves exactly as before."""

    report = git_observer.observe(
        _record(),
        repo="/work",
        git=_34_world(),
        pull_requests=PRWorld(view=_view()),
        now=T0,
    )

    assert report["drift"] == [] and report["clean"] is True
    assert report["actual"]["integration"] == {
        "observed": False,
        "main_ref": "",
        "main_head": None,
        "pr_head": None,
        "head_ancestor_of_main": None,
        "equivalent_on_main": None,
        "verdict": "unobserved",
    }
    assert "integration" not in report["scope"]


def test_the_observer_stays_read_only_while_classifying():
    """The #34 classification reads the world; it never writes to any of it."""
    world = _34_world()
    prs = PRWorld(view=_view())

    first = git_observer.observe(_record(), repo="/work", git=world, pull_requests=prs, now=T0, main_ref="main")
    world.calls.clear()
    prs.calls.clear()
    second = git_observer.observe(_record(), repo="/work", git=world, pull_requests=prs, now=T0, main_ref="main")

    assert first == second  # observation is pure
    assert world.mutations() == []  # every git call stayed on the allowlist
    assert all(argv[0] in git_observer._READ_ONLY_GIT for argv in world.calls)
    assert prs.calls == [(REPO_SLUG, BRANCH, 34)]  # only view/list-shaped queries


def test_main_ref_must_be_a_usable_ref_name():
    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe(_record(), repo="/work", git=_34_world(), main_ref="../etc")
    assert caught.value.code == "main_ref_invalid"
    with pytest.raises(git_observer.GitObserverError) as caught:
        git_observer.observe(_record(), repo="/work", git=_34_world(), main_ref="")
    assert caught.value.code == "main_ref_invalid"


# ---------------------------------------------------------------------------
# The reconcile bridge and the tick consume the evidence, read-only
# ---------------------------------------------------------------------------


def _authorize(tmp_path: Path) -> None:
    jobs.record_authorization(
        tmp_path,
        job_id=JOB,
        statement="Observe the integrated-not-merged world.",
        repo=REPO_SLUG,
        base_rev=BASE,
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


def test_the_reconcile_bridge_passes_the_main_ref_through(tmp_path: Path):
    _authorize(tmp_path)
    campaign.create_campaign(
        tmp_path,
        campaign_id="campaign-34",
        job_id=JOB,
        objective="Observe the integrated-not-merged world.",
        branch=BRANCH,
        pull_request=34,
    )
    path = campaign._path(tmp_path, "campaign-34")
    before = path.read_bytes()

    report = reconcile.campaign_git_drift(
        tmp_path,
        "campaign-34",
        repo="/work",
        git=_34_world(),
        pull_requests=PRWorld(view=_view()),
        main_ref="main",
    )

    assert "integrated_not_pr_merged" in _kinds(report)
    assert report["actual"]["integration"]["verdict"] == "integrated_not_pr_merged"
    assert path.read_bytes() == before  # the bridge wrote nothing


class _UnusedLauncher:
    """A launcher that fails the test if the tick tries to use it."""

    idempotent = True

    def list_attempts(self):
        return []

    def get_attempt(self, attempt_id):
        return None

    def post_attempt(self, brief):
        raise AssertionError("nothing may be dispatched while the world disagrees")


def _verified_job(tmp_path: Path) -> None:
    _authorize(tmp_path)
    job = jobs.load_job(tmp_path, JOB)
    for state in ("dispatching", "dispatched", "running", "verifying", "verified"):
        jobs.transition(job, "T1", state, actor="reconciler", reason="fixture")
    jobs.save_job(tmp_path, job)


def test_a_tick_with_integrated_evidence_never_integrates_again(tmp_path: Path):
    """The #34 class: the work is on main; the tick must not integrate twice."""
    _verified_job(tmp_path)
    report = git_observer.observe(
        _record(), repo="/work", git=_34_world(), pull_requests=PRWorld(view=_view()), now=T0, main_ref="main"
    )
    calls = []

    def integrator(job):
        calls.append(job)
        return {"ok": True, "evidence": "fixture"}

    result = reconcile.tick(
        tmp_path,
        JOB,
        launcher=_UnusedLauncher(),
        integrator=integrator,
        now=1_800_000_000.0,
        git_evidence=report,
    )

    assert calls == []  # no duplicate integration attempt
    assert result.action == "record_disposition"
    assert result.status == "ok"
    assert result.evidence == "integrated_not_pr_merged"
    assert "stop waiting" in result.detail
    job = jobs.load_job(tmp_path, JOB)
    assert job["tasks"][0]["state"] == "verified"  # untouched; disposition is not a fake acceptance


def test_a_tick_with_a_disagreeing_world_blocks_integration(tmp_path: Path):
    """A closed PR and a moved branch are human decisions, never auto-integration."""
    _verified_job(tmp_path)
    world = GitWorld(
        refs={
            f"heads/{BRANCH}": PR34_HEAD,
            f"remotes/origin/{BRANCH}": PR34_HEAD,
            "remotes/origin/main": MAIN,
        },
        known={BASE, PR34_HEAD, MAIN},
        ancestors={(BASE, PR34_HEAD)},
        pr_side=1,
        cherry_missing=1,
    )
    report = git_observer.observe(
        _record(), repo="/work", git=world, pull_requests=PRWorld(view=_view(state="closed")), now=T0, main_ref="main"
    )

    def integrator(job):
        raise AssertionError("the integrator must not run")

    result = reconcile.tick(
        tmp_path,
        JOB,
        launcher=_UnusedLauncher(),
        integrator=integrator,
        now=1_800_000_000.0,
        git_evidence=report,
    )

    assert result.status == "blocked"
    assert result.action == "needs_human"
    assert "pr_closed" in result.detail


def test_a_tick_without_evidence_integrates_as_before(tmp_path: Path):
    """The default path is unchanged: no git evidence, normal integration."""
    _verified_job(tmp_path)

    result = reconcile.tick(
        tmp_path,
        JOB,
        launcher=_UnusedLauncher(),
        integrator=lambda job: {"ok": True, "evidence": "fixture wave", "commit": "f" * 40},
        now=1_800_000_000.0,
    )

    assert result.action == "integrate"
    job = jobs.load_job(tmp_path, JOB)
    assert job["tasks"][0]["state"] == "accepted"


def test_a_tick_with_clean_evidence_integrates_as_before(tmp_path: Path):
    """A report that matches the world changes nothing about integration."""
    _verified_job(tmp_path)
    clean = git_observer.observe(
        _record(),
        repo="/work",
        git=GitWorld(
            refs={
                f"heads/{BRANCH}": PR34_HEAD,
                f"remotes/origin/{BRANCH}": PR34_HEAD,
                "remotes/origin/main": MAIN,
            },
            known={BASE, PR34_HEAD, MAIN},
            ancestors={(BASE, PR34_HEAD)},
            pr_side=1,
            cherry_missing=1,
        ),
        pull_requests=PRWorld(view=_view()),
        now=T0,
        main_ref="main",
    )
    assert clean["clean"] is True

    result = reconcile.tick(
        tmp_path,
        JOB,
        launcher=_UnusedLauncher(),
        integrator=lambda job: {"ok": True, "evidence": "fixture wave", "commit": "f" * 40},
        now=1_800_000_000.0,
        git_evidence=clean,
    )

    assert result.action == "integrate"
    job = jobs.load_job(tmp_path, JOB)
    assert job["tasks"][0]["state"] == "accepted"


def test_a_tick_refuses_a_malformed_evidence_report(tmp_path: Path):
    _verified_job(tmp_path)

    with pytest.raises(reconcile.ReconcileError) as caught:
        reconcile.tick(
            tmp_path,
            JOB,
            launcher=_UnusedLauncher(),
            integrator=lambda job: {"ok": True, "evidence": "fixture"},
            now=1_800_000_000.0,
            git_evidence={"drift": "not-a-list"},
        )
    assert caught.value.code == "git_evidence_invalid"


# ---------------------------------------------------------------------------
# Real worktrees: the default git adapter classifies real histories
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


def test_a_real_merged_history_with_an_open_pr_is_integrated_not_pr_merged(tmp_path: Path):
    """The #34 shape in a real worktree: merge locally, GitHub never merged."""
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-qb", BRANCH)
    (repo / "src" / "b.py").write_text("value = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "the work")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-qm", "integrate without GitHub", head)
    main_head = _git(repo, "rev-parse", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", main_head)

    report = git_observer.observe(
        _record(base),
        repo=repo,
        pull_requests=PRWorld(view=_view(oid=head, state="open")),
        now=T0,
        main_ref="main",
    )

    assert "integrated_not_pr_merged" in _kinds(report)
    entry = next(item for item in report["drift"] if item["kind"] == "integrated_not_pr_merged")
    assert entry["actual"]["pr_head"] == head and entry["actual"]["main_head"] == main_head
    assert report["actual"]["integration"]["head_ancestor_of_main"] is True


def test_a_real_cherry_picked_patch_is_unverified_never_a_proved_merge(tmp_path: Path):
    """Same patch on main via cherry-pick: a different commit is not the PR head."""
    repo, base = _repo(tmp_path)
    _git(repo, "checkout", "-qb", BRANCH)
    (repo / "src" / "b.py").write_text("value = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "the work")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "cherry-pick", "-x", head)
    main_head = _git(repo, "rev-parse", "main")
    assert main_head != head
    _git(repo, "update-ref", "refs/remotes/origin/main", main_head)

    report = git_observer.observe(
        _record(base),
        repo=repo,
        pull_requests=PRWorld(view=_view(oid=head, state="open")),
        now=T0,
        main_ref="main",
    )

    assert "integrated_not_pr_merged" not in _kinds(report)
    assert "equivalent_content_unverified" in _kinds(report)
    integration = report["actual"]["integration"]
    assert integration["head_ancestor_of_main"] is False
    assert integration["equivalent_on_main"] is True
    assert integration["pr_side_commits"] == 1
    assert integration["pr_commits_without_patch_equivalent_on_main"] == 0
