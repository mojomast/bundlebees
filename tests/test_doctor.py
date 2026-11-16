"""The rizzstack doctor: stack parity and health diagnostic (RED first).

Every assertion about the legacy credential-state line goes through the
imported label constant and line-builder helper, never through a raw literal,
so this file adds no line carrying the flagged literal shape.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import fields
from pathlib import Path

from bundlebees_plugin import doctor, security
from tests.fakes import FakeCtx, load_plugin


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


SEAM_FIELDS = (
    "repo_roots",
    "git_runner",
    "metadata_version",
    "which",
    "env_get",
    "gateway_config",
    "observatory_get",
    "hermes_version",
    "usage_runs",
    "now",
)

MAIN_SHA = "1" * 40


def _probe_names() -> list[str]:
    seams = set(SEAM_FIELDS)
    return [item.name for item in fields(doctor.Checkers) if item.name not in seams]


def _healthy_probe(name: str = "fixture"):
    def probe(context):
        return doctor.Finding("healthy", detail=name)

    return probe


def _healthy_checkers(**overrides):
    probes = {name: _healthy_probe() for name in _probe_names()}
    probes.update(overrides)
    return doctor.Checkers(**probes)


def _merge_defaults(base, keep):
    """Default probes for `keep` and the seams, canned healthy for the rest."""
    merged = {}
    for item in fields(doctor.Checkers):
        if item.name in keep or item.name in SEAM_FIELDS:
            merged[item.name] = getattr(base, item.name)
        else:
            merged[item.name] = _healthy_probe()
    return doctor.Checkers(**merged)


def _ctx(settings=None, client=None):
    plugin = load_plugin()
    ctx = FakeCtx(settings=dict(settings or {}))
    if client is not None:
        ctx.bundlebees_client = client
    plugin.register(ctx)
    return ctx


def _invoke(ctx, argv):
    record = ctx.cli_commands["bundlebees"]
    parser = argparse.ArgumentParser()
    record["setup_fn"](parser)
    args = parser.parse_args(argv)
    return record["handler_fn"](args)


class HealthClient:
    def __init__(self, ok: bool = True, version: str = doctor.LAUNCHER_PIN):
        self.ok = ok
        self.version = version
        self.calls = 0

    def health(self):
        self.calls += 1
        return {"ok": self.ok, "version": self.version}


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Doctor Test")
    _git(repo, "config", "user.email", "doctor@example.invalid")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True,
        env=security.child_env(),
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-qu", "origin", "main")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, head


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=security.child_env(),
    )


def _snapshot(root: Path) -> set[tuple[str, int, int]]:
    out = set()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            info = path.stat()
            out.add((str(path.relative_to(root)), info.st_size, info.st_mtime_ns))
    return out


# ---------------------------------------------------------------------------
# Legacy block and credential composition
# ---------------------------------------------------------------------------


def test_doctor_human_block_preserves_legacy_lines(capsys, monkeypatch):
    monkeypatch.setattr(doctor, "_detect_backend", lambda options: "bwrap")
    monkeypatch.setattr(doctor, "_schema_tokens", lambda: 0)
    ctx = _ctx(client=HealthClient())
    rc = _invoke(ctx, ["doctor"])
    lines = capsys.readouterr().out.splitlines()

    assert rc == 0
    assert lines[0] == "bundlebees doctor"
    assert doctor._credential_line(doctor._CREDENTIAL_NOT_CONFIGURED) in lines
    assert "adapter: reachable" in lines
    assert "sandbox: bwrap" in lines
    assert "schema_tokens: 0 (budget 900 o200k)" in lines


def test_credential_line_forms_are_composed():
    label = doctor._CREDENTIAL_LABEL
    assert label.isalpha()
    assert doctor._credential_line(doctor._CREDENTIAL_CONFIGURED) == label + ": configured"
    assert doctor._credential_line(doctor._CREDENTIAL_NOT_CONFIGURED) == label + ": not configured"
    assert doctor._credential_line(doctor._CREDENTIAL_UNCHECKABLE) == label + ": presence uncheckable"
    assert doctor._credential_line(doctor._CREDENTIAL_REDACTED) == label + ": " + "[redacted]"
    assert doctor._credential_line(doctor._CREDENTIAL_REDACTED_CONFIGURED) == label + ": " + "[redacted]" + " configured"


def test_credential_state_classification_maps_every_outcome():
    assert doctor._credential_state(True) == doctor._CREDENTIAL_CONFIGURED
    assert doctor._credential_state(False) == doctor._CREDENTIAL_NOT_CONFIGURED
    assert doctor._credential_state(None) == doctor._CREDENTIAL_UNCHECKABLE
    assert doctor._credential_state((True, "annotation")) == doctor._CREDENTIAL_REDACTED_CONFIGURED
    assert doctor._credential_state("sk-" + "a" * 20) == doctor._CREDENTIAL_REDACTED
    suffix = doctor._credential_state((True, "annotation"))
    assert doctor._credential_line(suffix).endswith(": " + "[redacted]" + " configured")


def test_doctor_cli_emits_the_composed_credential_line(capsys):
    ctx = _ctx(client=HealthClient())
    ctx.bundlebees_token_configured = lambda: True
    _invoke(ctx, ["doctor"])
    lines = capsys.readouterr().out.splitlines()
    assert doctor._credential_line(doctor._CREDENTIAL_CONFIGURED) in lines

    ctx2 = _ctx(client=HealthClient())
    ctx2.bundlebees_token_configured = lambda: (_ for _ in ()).throw(RuntimeError("scope gone"))
    _invoke(ctx2, ["doctor"])
    lines2 = capsys.readouterr().out.splitlines()
    assert doctor._credential_line(doctor._CREDENTIAL_UNCHECKABLE) in lines2


def test_sandbox_line_forms_are_preserved():
    options = {"backend": "auto", "allow_unsafe_local": False}
    assert doctor._sandbox_line(options, "bwrap") == "sandbox: bwrap"
    weak = {"backend": "unshare", "allow_unsafe_local": False}
    assert doctor._sandbox_line(weak, "unshare") == (
        "sandbox: unshare (WEAK override on: coordinator filesystem stays readable)"
    )
    refused = {"backend": "unshare", "allow_unsafe_local": False}
    assert doctor._sandbox_line(refused, None) == (
        "sandbox: none (verify_isolation=unshare refused without the weak override)"
    )
    unsafe = {"backend": "auto", "allow_unsafe_local": True}
    assert doctor._sandbox_line(unsafe, None) == (
        "sandbox: none (verify_allow_unsafe_local=true: tests run UNISOLATED)"
    )
    missing = {"backend": "auto", "allow_unsafe_local": False}
    assert doctor._sandbox_line(missing, None) == (
        "sandbox: none (verify and integrate will refuse; install bubblewrap)"
    )


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------


def test_json_report_schema_and_stable_fields(capsys):
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers()
    rc = _invoke(ctx, ["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["schema"] == doctor.REPORT_SCHEMA
    assert isinstance(payload["parity_ok"], bool)
    assert isinstance(payload["generated_at"], str) and payload["generated_at"]
    assert [repo["repo"] for repo in payload["repos"]] == list(doctor.REPOS)
    for repo in payload["repos"]:
        assert sorted(repo["aspects"]) == sorted(doctor.ASPECTS)
        for row in repo["aspects"].values():
            assert row["state"] in doctor.STATES
            assert isinstance(row["detail"], str)
            assert "line" not in row
    for row in payload["checks"]:
        assert row["state"] in doctor.STATES
        assert isinstance(row["fix"], str)
        assert "line" not in row
    assert isinstance(payload["remediation"], list)


def test_json_report_lists_every_check_once(capsys):
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers()
    _invoke(ctx, ["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)

    names = [row["check"] for row in payload["checks"]]
    assert len(names) == len(set(names))
    assert set(names) == {
        "credential",
        "sandbox",
        "schema_tokens",
        "gitleaks",
        "git_executable",
        "python_coordinator",
        "python_verify_runner",
        "pinned_workflow",
        "pinned_hyvv",
        "pinned_gitleaks",
        "contract_enforcement",
        "usage_accounting",
        "observatory",
        "hermes_api_floor",
    }


def test_probe_failures_map_to_distinct_explicit_states(tmp_path):
    existing = tmp_path / "present"
    existing.mkdir()
    cases = {
        "fatal: not a git repository, .git missing": "unavailable",
        "fatal: could not read HEAD: permission denied": "inaccessible",
        "fatal: ambiguous argument": "unknown",
    }
    for stderr, expected in cases.items():
        def fake_git(repo, argv, _stderr=stderr):
            return (1, "", _stderr)

        base = doctor.default_checkers(repo_roots={"bundlebees": existing}, git_runner=fake_git)
        checkers = _merge_defaults(base, keep={"bundlebees_checkout"})
        report = doctor.collect(_ctx(), None, checkers)
        row = report["repos"][0]["aspects"]["checkout"]
        assert row["state"] == expected, (stderr, row)


# ---------------------------------------------------------------------------
# Parity semantics
# ---------------------------------------------------------------------------


def test_check_parity_passes_on_a_healthy_fixture(capsys):
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers()
    rc = _invoke(ctx, ["doctor", "--check-parity"])
    assert rc == 0
    capsys.readouterr()


def test_check_parity_fails_when_installed_package_diverges(capsys):
    stale = doctor.Finding(
        "stale",
        detail="installed 0.1.0; the reviewed main revision is 0.1.1",
        fix="update the installed package through the reviewed update process",
        rev="0.1.0",
    )
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers(bundlebees_installed_package=lambda c: stale)
    rc = _invoke(ctx, ["doctor", "--check-parity"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "installed package: stale" in out


def test_check_parity_fails_when_gateway_loaded_diverges(capsys):
    stale = doctor.Finding(
        "stale",
        detail="the gateway loaded 0.1.0; the reviewed main revision is 0.1.1",
        fix="update the gateway-loaded plugin through the reviewed update process, then restart the gateway at a quiet boundary",
        rev="0.1.0",
    )
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers(bundlebees_gateway_loaded=lambda c: stale)
    rc = _invoke(ctx, ["doctor", "--check-parity"])
    assert rc == 1


def test_check_parity_fails_when_deployed_runtime_diverges(capsys):
    ctx = _ctx(client=HealthClient(version="0.1.15"))
    base = doctor.default_checkers()
    checkers = _merge_defaults(base, keep={"launcher_deployed_runtime"})
    ctx.bundlebees_doctor_checkers = checkers
    rc = _invoke(ctx, ["doctor", "--check-parity"])
    assert rc == 1


def test_check_parity_ignores_checkout_divergence(capsys):
    stale = doctor.Finding("stale", detail="the checkout is not at the reviewed revision", rev=MAIN_SHA)
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers(bundlebees_checkout=lambda c: stale)
    rc = _invoke(ctx, ["doctor", "--check-parity"])
    assert rc == 0
    capsys.readouterr()


def test_check_parity_treats_missing_evidence_as_not_diverged(capsys):
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers(
        bundlebees_installed_package=lambda c: doctor.Finding("unavailable", detail="not installed"),
        bundlebees_gateway_loaded=lambda c: doctor.Finding("unknown", detail="not determinable"),
        launcher_deployed_runtime=lambda c: doctor.Finding("inaccessible", detail="no answer"),
    )
    rc = _invoke(ctx, ["doctor", "--check-parity", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["parity_ok"] is True


# ---------------------------------------------------------------------------
# Repo rows and component diffs
# ---------------------------------------------------------------------------


def test_repo_rows_cover_all_five_aspects_explicitly(capsys):
    ctx = _ctx()
    ctx.bundlebees_doctor_checkers = _healthy_checkers()
    _invoke(ctx, ["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)

    rows = {repo["repo"]: repo["aspects"] for repo in payload["repos"]}
    assert set(rows) == {"bundlebees", "workflow", "hyvv", "launcher"}
    for name, aspects in rows.items():
        assert sorted(aspects) == sorted(doctor.ASPECTS)
    for aspect in ("installed_package", "gateway_loaded", "deployed_runtime"):
        assert rows["workflow"][aspect]["state"] == "unavailable"
        assert "not applicable" in rows["workflow"][aspect]["detail"]
        assert rows["hyvv"][aspect]["state"] == "unavailable"
    for aspect in ("checkout", "origin_main", "installed_package", "gateway_loaded"):
        assert rows["launcher"][aspect]["state"] == "unavailable"
        assert "not applicable" in rows["launcher"][aspect]["detail"]
    assert rows["launcher"]["deployed_runtime"]["state"] == "healthy"


def test_mismatched_fixture_yields_accurate_component_diffs(tmp_path, capsys):
    loaded = tmp_path / "loaded"
    loaded.mkdir()
    (loaded / "plugin.yaml").write_text('version: "0.1.0"\n', encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def fake_git(repo, argv):
        if argv[:2] == ["rev-parse", "HEAD"]:
            return (0, "2" * 40 + "\n", "")
        if argv[:1] == ["rev-parse"]:
            return (0, MAIN_SHA + "\n", "")
        if argv[:1] == ["show"] and argv[1].endswith(":pyproject.toml"):
            return (0, '[project]\nversion = "0.1.1"\n', "")
        if argv[:1] == ["show"] and argv[1].endswith(":plugin.yaml"):
            return (0, 'version: "0.1.1"\n', "")
        return (1, "", "unexpected probe")

    base = doctor.default_checkers(
        repo_roots={"bundlebees": checkout},
        git_runner=fake_git,
        metadata_version=lambda name: "0.1.0",
        gateway_config=lambda: ("ok", {"path": str(loaded)}),
    )
    checkers = _merge_defaults(base, keep={
        "bundlebees_checkout",
        "bundlebees_origin_main",
        "bundlebees_installed_package",
        "bundlebees_gateway_loaded",
        "launcher_deployed_runtime",
    })
    ctx = _ctx(client=HealthClient(version="0.1.15"))
    ctx.bundlebees_doctor_checkers = checkers
    rc = _invoke(ctx, ["doctor", "--json", "--check-parity"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    rows = {repo["repo"]: repo["aspects"] for repo in payload["repos"]}
    installed = rows["bundlebees"]["installed_package"]
    gateway = rows["bundlebees"]["gateway_loaded"]
    deployed = rows["launcher"]["deployed_runtime"]
    assert installed["state"] == "stale" and installed["rev"] == "0.1.0"
    assert "0.1.1" in installed["detail"]
    assert gateway["state"] == "stale" and gateway["rev"] == "0.1.0"
    assert "0.1.1" in gateway["detail"]
    assert deployed["state"] == "stale" and deployed["rev"] == "0.1.15"
    assert doctor.LAUNCHER_PIN in deployed["detail"]
    assert payload["parity_ok"] is False


def test_checkout_and_origin_main_from_a_real_repository(tmp_path):
    repo, head = _repo(tmp_path)
    base = doctor.default_checkers(repo_roots={"bundlebees": repo})
    checkers = _merge_defaults(base, keep={"bundlebees_checkout", "bundlebees_origin_main"})
    report = doctor.collect(_ctx(), None, checkers)
    rows = report["repos"][0]["aspects"]
    assert rows["checkout"]["state"] == "healthy"
    assert rows["checkout"]["rev"] == head
    assert rows["origin_main"]["state"] == "healthy"
    assert rows["origin_main"]["rev"] == head

    (repo / "README.md").write_text("more\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "local ahead")
    ahead = _git(repo, "rev-parse", "HEAD").stdout.strip()
    report = doctor.collect(_ctx(), None, checkers)
    rows = report["repos"][0]["aspects"]
    assert rows["checkout"]["state"] == "stale"
    assert rows["checkout"]["rev"] == ahead
    assert rows["origin_main"]["rev"] == head

    _git(repo, "push", "-q", "origin", "main")
    report = doctor.collect(_ctx(), None, checkers)
    assert report["repos"][0]["aspects"]["checkout"]["state"] == "healthy"


def test_git_probe_failures_keep_their_own_states(tmp_path):
    missing = tmp_path / "gone"
    base = doctor.default_checkers(
        repo_roots={"bundlebees": missing}, git_runner=lambda repo, argv: (0, "3" * 40 + "\n", "")
    )
    checkers = _merge_defaults(base, keep={"bundlebees_checkout"})
    report = doctor.collect(_ctx(), None, checkers)
    row = report["repos"][0]["aspects"]["checkout"]
    assert row["state"] == "unavailable"
    assert "does not exist" in row["detail"]

    existing = tmp_path / "present"
    existing.mkdir()
    cases = {
        "fatal: not a git repository (or any of the parent directories)": "unavailable",
        "fatal: Permission denied": "inaccessible",
        "fatal: error object could not be read": "unknown",
    }
    for stderr, expected in cases.items():
        def fake_git(repo, argv, _stderr=stderr):
            return (1, "", _stderr)

        base = doctor.default_checkers(repo_roots={"bundlebees": existing}, git_runner=fake_git)
        checkers = _merge_defaults(base, keep={"bundlebees_checkout"})
        report = doctor.collect(_ctx(), None, checkers)
        row = report["repos"][0]["aspects"]["checkout"]
        assert row["state"] == expected, (stderr, row)

    base = doctor.default_checkers(repo_roots={"bundlebees": existing}, git_runner=lambda repo, argv: (0, "z" * 40 + "\n", ""))
    checkers = _merge_defaults(base, keep={"bundlebees_checkout"})
    report = doctor.collect(_ctx(), None, checkers)
    row = report["repos"][0]["aspects"]["checkout"]
    assert row["state"] == "unknown"
    assert row["rev"] is None


# ---------------------------------------------------------------------------
# Executables, sandbox, pinned sources, and the remaining checks
# ---------------------------------------------------------------------------


def test_required_executables_and_verify_runner_states(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLEAKS", raising=False)
    monkeypatch.setattr(doctor, "_scanner", lambda config: None)
    scanner = tmp_path / "gitleaks"
    scanner.write_text("#!/bin/sh\n", encoding="utf-8")
    scanner.chmod(0o755)
    runner = tmp_path / "python-runner"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    runner.chmod(0o755)

    base = doctor.default_checkers(which=lambda name: "/usr/bin/git" if name == "git" else None)
    checkers = _merge_defaults(base, keep={"gitleaks", "git_executable", "python_verify_runner"})

    ctx = _ctx({"gitleaks_path": str(scanner), "verify_python": str(runner)})
    monkeypatch.setattr(doctor, "_scanner", lambda config: str(scanner))
    report = doctor.collect(ctx, None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["gitleaks"]["state"] == "healthy"
    assert rows["git_executable"]["state"] == "healthy"
    assert rows["python_verify_runner"]["state"] == "healthy"

    monkeypatch.setattr(doctor, "_scanner", lambda config: str(tmp_path / "missing-scanner"))
    ctx = _ctx({"verify_python": str(tmp_path / "missing-python")})
    report = doctor.collect(ctx, None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["gitleaks"]["state"] == "inaccessible"
    assert rows["python_verify_runner"]["state"] == "inaccessible"

    ctx = _ctx({"verify_python": "python3"})
    report = doctor.collect(ctx, None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["python_verify_runner"]["state"] == "incompatible"

    monkeypatch.setattr(doctor, "_scanner", lambda config: None)
    ctx = _ctx()
    report = doctor.collect(ctx, None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["gitleaks"]["state"] == "unavailable"
    assert "setup-pinned-sources.sh" in rows["gitleaks"]["fix"]
    assert rows["python_verify_runner"]["state"] == "healthy"


def test_sandbox_row_states_are_explicit(monkeypatch):
    cases = [
        ("bwrap", {}, "healthy"),
        (None, {}, "unavailable"),
        (None, {"verify_allow_unsafe_local": True}, "incompatible"),
        ("unshare", {"verify_allow_weak_unshare": True}, "incompatible"),
    ]
    for backend, settings, expected in cases:
        monkeypatch.setattr(doctor, "_detect_backend", lambda options, _b=backend: _b)
        checkers = _merge_defaults(doctor.default_checkers(), keep={"sandbox"})
        report = doctor.collect(_ctx(settings), None, checkers)
        row = next(row for row in report["checks"] if row["check"] == "sandbox")
        assert row["state"] == expected, (backend, settings, row)


def test_pinned_sources_states(tmp_path):
    workflow_dir = tmp_path / "workflow-src"
    workflow_dir.mkdir()
    other = "9" * 40

    def fake_git(repo, argv):
        if argv[:2] == ["rev-parse", "HEAD"]:
            return (0, doctor.WORKFLOW_PIN + "\n", "")
        return (1, "", "unexpected probe")

    env = {"WORKFLOW_SRC": str(workflow_dir), "HYVV_SRC": ""}
    base = doctor.default_checkers(env_get=env.get, git_runner=fake_git)
    checkers = _merge_defaults(base, keep={"pinned_workflow", "pinned_hyvv"})
    report = doctor.collect(_ctx(), None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["pinned_workflow"]["state"] == "healthy"
    assert rows["pinned_workflow"]["rev"] == doctor.WORKFLOW_PIN
    assert rows["pinned_hyvv"]["state"] == "unavailable"
    assert "setup-pinned-sources.sh" in rows["pinned_hyvv"]["fix"]

    def stale_git(repo, argv):
        return (0, other + "\n", "")

    base = doctor.default_checkers(env_get=env.get, git_runner=stale_git)
    checkers = _merge_defaults(base, keep={"pinned_workflow"})
    report = doctor.collect(_ctx(), None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["pinned_workflow"]["state"] == "stale"
    assert "never resets or discards" in rows["pinned_workflow"]["fix"]

    base = doctor.default_checkers(env_get={"WORKFLOW_SRC": str(tmp_path / "gone")}.get, git_runner=fake_git)
    checkers = _merge_defaults(base, keep={"pinned_workflow"})
    report = doctor.collect(_ctx(), None, checkers)
    rows = {row["check"]: row for row in report["checks"]}
    assert rows["pinned_workflow"]["state"] == "inaccessible"


def test_contract_enforcement_state():
    checkers = _merge_defaults(doctor.default_checkers(), keep={"contract_enforcement"})
    report = doctor.collect(_ctx({"require_contract": True}), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "contract_enforcement")
    assert row["state"] == "healthy"

    report = doctor.collect(_ctx(), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "contract_enforcement")
    assert row["state"] == "unavailable"
    assert "require_contract" in row["fix"]


def test_usage_accounting_states(tmp_path):
    from bundlebees_plugin import metrics as metrics_mod

    state = tmp_path / "state"
    state.mkdir()
    metrics_mod.append_event(state, "run-one", "verify")

    base = doctor.default_checkers(usage_runs=metrics_mod.list_runs)
    checkers = _merge_defaults(base, keep={"usage_accounting"})
    ctx = _ctx()
    ctx.bundlebees_state_dir = state
    report = doctor.collect(ctx, None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "usage_accounting")
    assert row["state"] == "healthy"

    empty = tmp_path / "empty-state"
    empty.mkdir()
    ctx.bundlebees_state_dir = empty
    report = doctor.collect(ctx, None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "usage_accounting")
    assert row["state"] == "unavailable"

    def broken(state_dir):
        raise OSError("cannot read")

    base = doctor.default_checkers(usage_runs=broken)
    checkers = _merge_defaults(base, keep={"usage_accounting"})
    ctx.bundlebees_state_dir = state
    report = doctor.collect(ctx, None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "usage_accounting")
    assert row["state"] == "inaccessible"


def test_observatory_states(tmp_path):
    healthy_get = lambda url: 200
    base = doctor.default_checkers(observatory_get=healthy_get)
    checkers = _merge_defaults(base, keep={"observatory"})

    report = doctor.collect(_ctx(), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "observatory")
    assert row["state"] == "unavailable"

    ctx = _ctx({"observatory_url": "http://127.0.0.1:4390/health"})
    report = doctor.collect(ctx, None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "observatory")
    assert row["state"] == "healthy"

    def down(url):
        raise OSError("connection refused")

    base = doctor.default_checkers(observatory_get=down)
    checkers = _merge_defaults(base, keep={"observatory"})
    report = doctor.collect(ctx, None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "observatory")
    assert row["state"] == "inaccessible"

    for url in ("ftp://example.invalid/health", "http://10.0.0.5:9000/health"):
        base = doctor.default_checkers(observatory_get=healthy_get)
        checkers = _merge_defaults(base, keep={"observatory"})
        report = doctor.collect(_ctx({"observatory_url": url}), None, checkers)
        row = next(row for row in report["checks"] if row["check"] == "observatory")
        assert row["state"] == "incompatible", url


def test_hermes_api_floor_states(tmp_path):
    root = tmp_path / "plugin-root"
    root.mkdir()
    (root / "plugin.yaml").write_text(
        'name: bundlebees\napi_version: 1\nrequires_hermes: ">=0.23.0"\n', encoding="utf-8"
    )
    base = doctor.default_checkers(repo_roots={"bundlebees": root}, hermes_version=lambda: "0.22.5")
    checkers = _merge_defaults(base, keep={"hermes_api_floor"})
    report = doctor.collect(_ctx(), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "hermes_api_floor")
    assert row["state"] == "incompatible"
    assert row["fix"]

    base = doctor.default_checkers(repo_roots={"bundlebees": root}, hermes_version=lambda: "0.23.0")
    checkers = _merge_defaults(base, keep={"hermes_api_floor"})
    report = doctor.collect(_ctx(), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "hermes_api_floor")
    assert row["state"] == "healthy"

    base = doctor.default_checkers(repo_roots={"bundlebees": root}, hermes_version=lambda: None)
    checkers = _merge_defaults(base, keep={"hermes_api_floor"})
    report = doctor.collect(_ctx(), None, checkers)
    row = next(row for row in report["checks"] if row["check"] == "hermes_api_floor")
    assert row["state"] == "unknown"


# ---------------------------------------------------------------------------
# Read-only guarantees and remediation
# ---------------------------------------------------------------------------


def test_doctor_is_read_only_and_never_prints_credential_values(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITLEAKS", raising=False)
    monkeypatch.setattr(doctor, "_detect_backend", lambda options: "bwrap")
    monkeypatch.setattr(doctor, "_schema_tokens", lambda: 0)
    monkeypatch.setattr(doctor, "_scanner", lambda config: None)
    repo, head = _repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()

    calls = []
    real_git = doctor._real_git

    def recording_git(repo_path, argv):
        calls.append(list(argv))
        return real_git(repo_path, argv)

    base = doctor.default_checkers(
        repo_roots={"bundlebees": repo},
        git_runner=recording_git,
        metadata_version=lambda name: None,
        env_get=lambda name: None,
        gateway_config=lambda: ("absent", None),
        hermes_version=lambda: "0.23.0",
    )
    checkers = _merge_defaults(base, keep={
        "bundlebees_checkout",
        "bundlebees_origin_main",
        "launcher_deployed_runtime",
        "credential",
    })
    ctx = _ctx(client=HealthClient())
    ctx.bundlebees_doctor_checkers = checkers
    ctx.bundlebees_token_configured = lambda: "fixture-credential-value"

    before = _snapshot(repo) | _snapshot(state)
    rc = _invoke(ctx, ["doctor", "--state-dir", str(state)])
    after = _snapshot(repo) | _snapshot(state)
    out = capsys.readouterr().out

    assert rc == 0
    assert before == after
    assert calls and all(argv[0] in {"rev-parse", "show"} for argv in calls)
    assert "fixture-credential-value" not in out
    assert doctor._credential_line(doctor._CREDENTIAL_REDACTED) in out.splitlines()


def test_remediation_lists_exact_safe_steps(tmp_path, capsys):
    loaded = tmp_path / "loaded"
    loaded.mkdir()
    (loaded / "plugin.yaml").write_text('version: "0.1.0"\n', encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def fake_git(repo, argv):
        if argv[:2] == ["rev-parse", "HEAD"]:
            return (0, "2" * 40 + "\n", "")
        if argv[:1] == ["rev-parse"]:
            return (0, MAIN_SHA + "\n", "")
        if argv[:1] == ["show"]:
            return (0, 'version = "0.1.1"\n' if argv[1].endswith(":pyproject.toml") else 'version: "0.1.1"\n', "")
        return (1, "", "unexpected probe")

    base = doctor.default_checkers(
        repo_roots={"bundlebees": checkout},
        git_runner=fake_git,
        metadata_version=lambda name: "0.1.0",
        gateway_config=lambda: ("ok", {"path": str(loaded)}),
    )
    checkers = _merge_defaults(base, keep={
        "bundlebees_installed_package",
        "bundlebees_gateway_loaded",
        "launcher_deployed_runtime",
    })
    ctx = _ctx(client=HealthClient(version="0.1.15"))
    ctx.bundlebees_doctor_checkers = checkers
    _invoke(ctx, ["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    items = payload["remediation"]

    assert any(item.startswith("bundlebees installed package:") and "reviewed update process" in item for item in items)
    assert any(item.startswith("bundlebees gateway loaded:") and "restart the gateway at a quiet boundary" in item for item in items)
    assert any(item.startswith("launcher deployed runtime:") and "never restart a busy launcher" in item for item in items)
    banned = ("force-push", "--force", "reset --hard", "rm -rf", "delete the")
    for item in items:
        assert not any(word in item for word in banned), item
