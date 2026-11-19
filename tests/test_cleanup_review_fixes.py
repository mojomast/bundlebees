"""Rizzstack cleanup: fixes for the Nov 18 2026 review of the cleanup branches.

B1  the driver's sweep reads the launcher through LauncherAdapter (the real client has no
    list_attempts(); the raw client made every production sweep launcher_incompatible).
B2  the driver's sweep is classify-only end to end: no sweep record, no claim files, no
    board-stock duty (no kanban comment, no subprocess); only factory/sweep-last.json is new.
S1  the origin view's git ls-remote runs with child_env() and GIT_TERMINAL_PROMPT=0.
S2  without a PR view the sweep says so ("pr_view": "unavailable") and does not flag
    pushed-without-PR gaps it cannot see.
S3  the gate recorder can never abort a dispatch (any exception, not only OSError).
S4  the pinned workflow HEAD is cached per process for a short TTL; git timeout is <= 5 s.
N1  a future-dated sweep marker (clock skew) does not suppress the sweep.
N2  contract-gate/report-only.jsonl rotates by size.
"""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bundlebees_plugin import board_stock, contracts, factory_driver, tools
from bundlebees_plugin.client import LauncherClient

T0 = 1_800_000_000.0
TOKEN = "test-only-launcher-token-0000"
GAP = {
    "attempt_id": "att-gap", "task_id": "att-gap", "state": "succeeded", "branch": "bee/gap",
    "created_at": "2027-01-15T07:00:00Z", "finished_at": "2027-01-15T07:27:30Z",
}


class _Launcher(BaseHTTPRequestHandler):
    attempts = [GAP]

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {TOKEN}" or self.path != "/v1/attempts":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"attempts": self.attempts}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def launcher_url():
    server = HTTPServer(("127.0.0.1", 0), _Launcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})


def _repo_with_origin(tmp_path: Path, branches=()) -> Path:
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    _git("init", "-q", str(work), cwd=tmp_path)
    _git("commit", "-q", "--allow-empty", "-m", "seed", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    for name in ("main", *branches):
        _git("push", "-q", "origin", f"HEAD:refs/heads/{name}", cwd=work)
    return work


def _queue(tmp_path: Path) -> Path:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"items": {}}), encoding="utf-8")
    return path


def _driver(state: Path, tmp_path: Path, clock, **kw):
    return factory_driver.FactoryDriver(state, _queue(tmp_path), startup=False,
                                        now=lambda: clock["t"], **kw)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def _sweep(report):
    return report["duties"]["reconcile_completed_attempts"]


class TestSweepThroughTheRealClient:
    def test_b1_production_views_read_the_launcher_through_the_adapter(self, tmp_path, launcher_url):
        client = LauncherClient(launcher_url, token=TOKEN)
        repo = _repo_with_origin(tmp_path)
        views = factory_driver.sweep_views_for(client, repo)
        result = _driver(tmp_path / "state", tmp_path, {"t": T0}, sweep_views=views).run_pass()
        sweep = _sweep(result["sweep"])
        assert sweep["status"] == "ok", sweep
        assert sweep["attempts_seen"] == 1
        assert [d["attempt_id"] for d in sweep["detected"]] == ["att-gap"]


class TestSweepIsClassifyOnly:
    def test_b2_only_the_marker_is_written_and_nothing_is_posted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BUNDLEBEES_BOARD_STOCK", "1")
        called = []
        monkeypatch.setattr(board_stock, "flag", lambda *a, **k: called.append("flag") or {})
        real_run = subprocess.run

        def spy(argv, *a, **k):
            called.append(list(argv))
            return real_run(argv, *a, **k)

        monkeypatch.setattr(subprocess, "run", spy)
        views = {"launcher": [GAP], "origin_branches": {}, "open_prs": {}}
        clock = {"t": T0}
        plain = tmp_path / "plain"
        swept = tmp_path / "swept"
        _driver(plain, tmp_path, clock).run_pass()
        called.clear()
        result = _driver(swept, tmp_path, clock, sweep_views=views).run_pass()
        assert [d["attempt_id"] for d in _sweep(result["sweep"])["detected"]] == ["att-gap"]
        assert result["sweep"].get("mode") == "classify_only"
        assert "stock_ready_column" not in result["sweep"]["duties"]
        assert called == [], called
        assert _files(swept) - _files(plain) == {"factory/sweep-last.json"}


class TestOriginViewEnvironment:
    def test_s1_ls_remote_gets_child_env_and_no_prompt(self, tmp_path, monkeypatch):
        repo = _repo_with_origin(tmp_path, branches=["bee/x"])
        monkeypatch.setenv("BUNDLEBEES_LAUNCHER_TOKEN", "secret-never-reaches-git")
        seen = {}
        real_run = subprocess.run

        def spy(argv, *a, **k):
            seen["env"] = k.get("env")
            return real_run(argv, *a, **k)

        monkeypatch.setattr(factory_driver.subprocess, "run", spy)
        assert "bee/x" in factory_driver._origin_branch_view(repo, 30.0)
        env = seen["env"]
        assert env is not None, "git must not inherit the full environment"
        assert "BUNDLEBEES_LAUNCHER_TOKEN" not in env
        assert env.get("GIT_TERMINAL_PROMPT") == "0"


class TestPrViewUnavailable:
    def test_s2_without_a_pr_view_pushed_gaps_are_not_flagged(self, tmp_path, launcher_url):
        client = LauncherClient(launcher_url, token=TOKEN)
        repo = _repo_with_origin(tmp_path, branches=["bee/gap"])
        views = factory_driver.sweep_views_for(client, repo)
        assert "open_prs" not in views or views["open_prs"] is None
        result = _driver(tmp_path / "state", tmp_path, {"t": T0}, sweep_views=views).run_pass()
        sweep = _sweep(result["sweep"])
        assert sweep["pr_view"] == "unavailable"
        assert sweep["detected"] == []
        assert [d["attempt_id"] for d in sweep["unverified"]] == ["att-gap"]


class TestGateRecorder:
    def test_s3_any_recording_failure_is_swallowed(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise TypeError("not serialisable")

        monkeypatch.setattr(tools.json, "dumps", boom)
        acdd = {"contract_id": "c", "contract_sha256": "0" * 64,
                "gate": {"would_refuse": [{"code": "x", "error": "e", "field": None}]}}
        tools._record_gate_findings(tmp_path, {"project": "p"}, acdd)  # must not raise

    def test_n2_report_only_log_rotates_by_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "REPORT_ONLY_MAX_BYTES", 400)
        acdd = {"contract_id": "c", "contract_sha256": "0" * 64,
                "gate": {"would_refuse": [{"code": "acceptance_mismatch", "error": "e" * 60, "field": "f"}]}}
        for _ in range(12):
            tools._record_gate_findings(tmp_path, {"project": "p"}, acdd)
        log = tmp_path / "contract-gate" / "report-only.jsonl"
        rotated = tmp_path / "contract-gate" / "report-only.jsonl.1"
        assert rotated.exists()
        assert log.stat().st_size <= 400 + 400


class TestPinnedHeadCache:
    def test_s4_head_is_cached_briefly_and_git_timeout_is_short(self, tmp_path, monkeypatch):
        repo = _repo_with_origin(tmp_path)
        monkeypatch.setenv("WORKFLOW_SRC", str(repo))
        contracts._reset_workflow_ref_cache()
        calls = []
        real_run = subprocess.run

        def spy(argv, *a, **k):
            calls.append(k.get("timeout"))
            return real_run(argv, *a, **k)

        monkeypatch.setattr(contracts.subprocess, "run", spy)
        first = contracts.pinned_workflow_ref()
        second = contracts.pinned_workflow_ref()
        assert first == second and len(first) == 40
        assert len(calls) == 1
        assert calls[0] is not None and calls[0] <= 5
        contracts._reset_workflow_ref_cache()


class TestMarkerClockSkew:
    def test_n1_future_marker_does_not_suppress_the_sweep(self, tmp_path):
        state = tmp_path / "state"
        marker = state / "factory" / "sweep-last.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"at": T0 + 86_400}), encoding="utf-8")
        views = {"launcher": [GAP], "origin_branches": {}, "open_prs": {}}
        result = _driver(state, tmp_path, {"t": T0}, sweep_views=views).run_pass()
        assert result["sweep"].get("status") != "skipped", result["sweep"]
