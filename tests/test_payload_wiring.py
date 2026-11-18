"""Wiring tests for the lease payload: dispatch records it, every runner serves it.

The payload half (``dispatch.py`` / ``sandbox.py``) is proven against the real
firejail jail in ``tests/test_sandbox_pinned_sources.py``; this module proves the
WIRING around it:

* ``record_lease`` -- the chokepoint all three dispatch paths share -- records
  digests of what the coordinator actually serves, through
  ``dispatch.payload_digest(dispatch.collect_payload())``. Pins unset means the
  manifest key is absent (never an empty manifest); a pin that is set but
  unusable fails dispatch closed with the named ``dispatch.PayloadError``.
* ``execution.sandbox_options`` / ``execution.SandboxRunner`` serve the payload
  on every runner-driven sandbox run, and the child env names the staged copy
  relatively only; the host checkout path never reaches the child.
* ``cli._cmd_verify`` binds the lease manifest via ``dispatch.lease_payload``
  BEFORE any test runs; a stale pin is a ``payload_lease_mismatch`` refusal, a
  manifest-less lease binds nothing, and ``--commit-local`` serves from the
  environment without manifest binding (disclosed in docs/pinned-sources.md).
* Receipt identity gains the payload digests only when something is served and
  stays byte-identical to the base when nothing is served.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bundlebees_plugin import (
    dispatch,
    execution,
    factory_driver,
    job_cli,
    leases,
    sandbox,
    verify_local,
)
from tests.test_cli import (
    ATTEMPT_ID,
    BRANCH,
    IN_LEASE_PATCH,
    FakeClient,
    _ctx,
    _invoke,
    _lease,
    _repo,
    _result,
    _verify_argv,
)

MARKER_TEXT = "# pinned recovery validator (payload-wiring probe)\n"
PAYLOAD_NAME = "WORKFLOW_SRC"
HYVV_NAME = "HYVV_SRC"


def _make_checkout(checkout: Path) -> Path:
    """A fake pinned checkout with the workflow marker file."""
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / "tools").mkdir(parents=True, exist_ok=True)
    (checkout / "tools" / "recovery.py").write_text(MARKER_TEXT, encoding="utf-8")
    return checkout


def _payload_env(monkeypatch: pytest.MonkeyPatch, checkout: Path) -> None:
    monkeypatch.setenv(PAYLOAD_NAME, str(checkout))
    monkeypatch.delenv(HYVV_NAME, raising=False)


def _no_payload_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PAYLOAD_NAME, raising=False)
    monkeypatch.delenv(HYVV_NAME, raising=False)


def _brief(**overrides):
    from bundlebees_plugin.brief import validate_brief

    brief = {
        "objective": "apply the change",
        "source_rev": "a" * 40,
        "branch": BRANCH,
        "assigned_files": ["src/a.py"],
        "acceptance": {"criteria": ["sample test passes"], "tests": ["tests/test_sample.py"]},
    }
    brief.update(overrides)
    return validate_brief(brief)


# ---------------------------------------------------------------------------
# DISPATCH half: record_lease is the chokepoint all dispatch paths share
# ---------------------------------------------------------------------------


def test_record_lease_records_the_served_payload_manifest(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)

    record = leases.record_lease(tmp_path, "att-payload-1", _brief())

    expected = dispatch.payload_digest({PAYLOAD_NAME: str(checkout)})[PAYLOAD_NAME]
    manifest = record["brief"]["payload"]
    assert manifest == {PAYLOAD_NAME: expected}
    assert set(manifest) == {PAYLOAD_NAME}
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in manifest.values())
    # The manifest is inside the MAC-covered brief and survives the round trip.
    loaded = leases.load_lease(tmp_path, "att-payload-1")
    assert loaded["brief"]["payload"] == manifest


def test_record_lease_leaves_the_payload_key_absent_when_pins_are_unset(
    tmp_path, monkeypatch
):
    _no_payload_env(monkeypatch)
    calls: list[str] = []
    real_collect = dispatch.collect_payload

    def counted(*args, **kwargs):
        calls.append("collect")
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(dispatch, "collect_payload", counted)

    record = leases.record_lease(tmp_path, "att-payload-2", _brief())

    # The chokepoint always consults the coordinator env, even with no pins.
    assert calls == ["collect"]
    assert "payload" not in record["brief"]
    assert "payload" not in record
    assert leases.load_lease(tmp_path, "att-payload-2") == record


def test_record_lease_never_records_an_empty_manifest(tmp_path, monkeypatch):
    _no_payload_env(monkeypatch)

    record = leases.record_lease(
        tmp_path, "att-payload-3", _brief(payload={})
    )

    assert "payload" not in record["brief"]


def test_record_lease_still_binds_an_operator_authored_manifest(tmp_path, monkeypatch):
    """A manifest the brief already carries keeps binding verify (base behaviour)."""
    _no_payload_env(monkeypatch)
    authored = {PAYLOAD_NAME: "c" * 64}

    record = leases.record_lease(
        tmp_path, "att-payload-4", _brief(payload=dict(authored))
    )

    assert record["brief"]["payload"] == authored
    assert leases.load_lease(tmp_path, "att-payload-4")["brief"]["payload"] == authored


def test_record_lease_fails_closed_on_an_unusable_pin_even_with_an_authored_manifest(
    tmp_path, monkeypatch
):
    """Collection is unconditional: an unusable pin refuses any dispatch."""
    authored = {PAYLOAD_NAME: "c" * 64}
    _payload_env(monkeypatch, Path("/nonexistent/pinned-checkout"))

    with pytest.raises(dispatch.PayloadError) as excinfo:
        leases.record_lease(
            tmp_path, "att-payload-4b", _brief(payload=dict(authored))
        )
    assert excinfo.value.code == "payload_source_missing"
    assert not (tmp_path / "leases" / "att-payload-4b.json").exists()


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (str(Path("/nonexistent/pinned-checkout")), "payload_source_missing"),
        ("relative/checkouts", "payload_source_invalid"),
    ],
)
def test_record_lease_fails_closed_on_a_set_but_unusable_pin(
    tmp_path, monkeypatch, value, code
):
    _no_payload_env(monkeypatch)
    monkeypatch.setenv(PAYLOAD_NAME, value)

    with pytest.raises(dispatch.PayloadError) as excinfo:
        leases.record_lease(tmp_path, "att-payload-5", _brief())
    assert excinfo.value.code == code
    assert excinfo.value.fix
    # Fail closed: no lease record is written for a dispatch that cannot serve.
    assert not (tmp_path / "leases" / "att-payload-5.json").exists()


# ---------------------------------------------------------------------------
# A sandboxed run records the lease from the payload it was actually served
# ---------------------------------------------------------------------------


def _stage_served_payload(root: Path) -> Path:
    """The sandbox copy's staged payload for PAYLOAD_NAME, with a marker file."""
    staged = root / sandbox._SANDBOX_PAYLOAD_DIR / PAYLOAD_NAME
    (staged / "tools").mkdir(parents=True)
    (staged / "tools" / "recovery.py").write_text(MARKER_TEXT, encoding="utf-8")
    return staged


def test_record_lease_accepts_the_served_relative_payload(tmp_path, monkeypatch):
    """The served relative form resolves to the staged payload, not a refusal.

    A test that runs inside the verify sandbox calls the dispatch chokepoint
    with the child env it was given: ``WORKFLOW_SRC`` names the staged copy
    relatively because the jail's cwd is the copy. Recording a lease there must
    digest that staged tree (byte-identical to the source tree) instead of
    failing the run with ``payload_source_invalid``.
    """
    _no_payload_env(monkeypatch)
    staged = _stage_served_payload(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        PAYLOAD_NAME, f"./{sandbox._SANDBOX_PAYLOAD_DIR}/{PAYLOAD_NAME}"
    )

    record = leases.record_lease(tmp_path / "state", "att-payload-served", _brief())

    assert record["brief"]["payload"] == dispatch.payload_digest(
        {PAYLOAD_NAME: str(staged)}
    )


@pytest.mark.parametrize(
    "value",
    [
        # Anything that is not the staged entry for THIS payload name stays a
        # refused relative path: the absolute requirement itself is unchanged.
        "./.bundlebees-payload/HYVV_SRC",
        "./.bundlebees-payload/../escape",
        "./elsewhere/WORKFLOW_SRC",
        ".bundlebees-payload",
        "relative/checkouts",
    ],
)
def test_served_relative_payload_outside_the_staged_entry_fails_closed(
    tmp_path, monkeypatch, value
):
    _no_payload_env(monkeypatch)
    _stage_served_payload(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(PAYLOAD_NAME, value)

    with pytest.raises(dispatch.PayloadError) as excinfo:
        leases.record_lease(tmp_path / "state", "att-payload-served-2", _brief())

    assert excinfo.value.code == "payload_source_invalid"
    assert not (tmp_path / "state" / "leases" / "att-payload-served-2.json").exists()


def test_served_relative_payload_that_escapes_the_copy_fails_closed(
    tmp_path, monkeypatch
):
    """A staged-name symlink pointing outside the copy is not a served payload."""
    _no_payload_env(monkeypatch)
    outside = tmp_path / "outside" / PAYLOAD_NAME
    (outside / "tools").mkdir(parents=True)
    (outside / "tools" / "recovery.py").write_text(MARKER_TEXT, encoding="utf-8")
    staged_root = tmp_path / "work" / sandbox._SANDBOX_PAYLOAD_DIR
    staged_root.mkdir(parents=True)
    (staged_root / PAYLOAD_NAME).symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(tmp_path / "work")
    monkeypatch.setenv(
        PAYLOAD_NAME, f"./{sandbox._SANDBOX_PAYLOAD_DIR}/{PAYLOAD_NAME}"
    )

    with pytest.raises(dispatch.PayloadError) as excinfo:
        leases.record_lease(tmp_path / "state", "att-payload-served-3", _brief())

    assert excinfo.value.code == "payload_source_invalid"
    assert not (tmp_path / "state" / "leases" / "att-payload-served-3.json").exists()


# ---------------------------------------------------------------------------
# SERVING half: every runner-driven sandbox run serves the payload
# ---------------------------------------------------------------------------


def test_sandbox_options_serve_the_env_payload(tmp_path, monkeypatch):
    _no_payload_env(monkeypatch)
    assert "payload" not in execution.sandbox_options({})

    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    options = execution.sandbox_options({})

    assert options["payload"] == {PAYLOAD_NAME: str(checkout)}


def test_sandbox_options_fail_closed_on_an_unusable_pin(tmp_path, monkeypatch):
    _payload_env(monkeypatch, tmp_path / "not-a-checkout")

    with pytest.raises(dispatch.PayloadError) as excinfo:
        execution.sandbox_options({})
    assert excinfo.value.code == "payload_source_missing"


def test_sandbox_runner_serves_the_payload_on_every_run(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    captured = []

    def run(workdir, argv, **options):
        captured.append(options)
        return {"exit_code": 0}

    monkeypatch.setattr(sandbox, "run_in_sandbox", run)
    runner = execution.SandboxRunner({"verify_python": sys.executable})

    runner(tmp_path / "one", [sys.executable, "-m", "pytest", "-q"])
    runner(tmp_path / "two", [sys.executable, "-m", "pytest", "-q"])

    assert captured[0]["payload"] == {PAYLOAD_NAME: str(checkout)}
    assert captured[1]["payload"] == {PAYLOAD_NAME: str(checkout)}


def test_runner_driven_run_serves_relative_paths_only(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    work = tmp_path / "work"
    work.mkdir()

    runner = execution.SandboxRunner({"verify_allow_unsafe_local": True})
    result = runner(
        work,
        [
            sys.executable,
            "-c",
            "import os; print('SERVED=' + str(os.environ.get('WORKFLOW_SRC')))",
        ],
    )

    assert result["exit_code"] == 0
    assert "SERVED=./.bundlebees-payload/WORKFLOW_SRC" in result["output_tail"]
    # The host checkout path never appears in the child env.
    assert str(checkout) not in result["output_tail"]


def test_receipt_identity_admits_the_served_payload(tmp_path, monkeypatch):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    runner = execution.SandboxRunner({"verify_python": sys.executable})

    identity = runner.receipt_identity()

    assert identity["payload"] == dispatch.payload_digest(
        {PAYLOAD_NAME: str(checkout)}
    )


def test_receipt_identity_is_byte_identical_when_nothing_is_served(
    tmp_path, monkeypatch
):
    _no_payload_env(monkeypatch)
    plain = execution.SandboxRunner({"verify_python": sys.executable})

    identity = plain.receipt_identity()

    assert "payload" not in identity
    assert "payload" not in identity["options"]
    assert set(identity) == {"options", "executable", "dependencies"}
    assert "payload" not in json.dumps(identity, sort_keys=True)


# ---------------------------------------------------------------------------
# cli._cmd_verify binds the lease manifest BEFORE tests run
# ---------------------------------------------------------------------------


def test_cmd_verify_refuses_a_stale_pin_before_tests_run(tmp_path, monkeypatch, capsys):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    calls: list[str] = []

    def no_sandbox(*args, **kwargs):
        calls.append("run")
        raise AssertionError("the sandbox ran despite the stale-pin refusal")

    monkeypatch.setattr(sandbox, "run_in_sandbox", no_sandbox)
    (checkout / "tools" / "recovery.py").write_text(
        MARKER_TEXT + "drifted\n", encoding="utf-8"
    )
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))
    out = capsys.readouterr().out

    assert rc != 0
    assert "payload_lease_mismatch" in out
    assert "recorded in the lease" in out
    assert calls == []
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_cmd_verify_binds_nothing_for_a_manifest_less_lease_but_still_serves(
    tmp_path, monkeypatch
):
    _no_payload_env(monkeypatch)
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    _lease(state, base_sha)
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    captured: dict = {}
    real = sandbox.run_in_sandbox

    def spy(workdir, argv, **kwargs):
        captured.update(kwargs)
        return real(workdir, argv, **kwargs)

    monkeypatch.setattr(sandbox, "run_in_sandbox", spy)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(_ctx(client), _verify_argv(repo, state, "--dry-run"))

    assert rc == 0
    assert captured["payload"] == {PAYLOAD_NAME: str(checkout)}


def test_commit_local_serves_from_env_without_binding_the_manifest(
    tmp_path, monkeypatch
):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    repo, _remote, base_sha = _repo(tmp_path)
    state = tmp_path / "state"
    captured: dict = {}

    def fake_verify_and_commit(state_dir, workdir, attempt_id, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "sha": "b" * 40, "branch": BRANCH, "files": ["src/a.py"]}

    def no_binding(*args, **kwargs):
        raise AssertionError("commit-local must not bind a lease manifest")

    monkeypatch.setattr(verify_local, "verify_and_commit", fake_verify_and_commit)
    monkeypatch.setattr(dispatch, "lease_payload", no_binding)
    client = FakeClient(_result(IN_LEASE_PATCH, base_rev=base_sha))

    rc = _invoke(
        _ctx(client),
        [
            "verify",
            ATTEMPT_ID,
            "--workdir",
            str(repo),
            "--state-dir",
            str(state),
            "--commit-local",
        ],
    )

    assert rc == 0
    runner = captured["runner"]
    assert runner.options["payload"] == {PAYLOAD_NAME: str(checkout)}


def test_job_cli_inherits_serving_through_the_runner(monkeypatch, tmp_path):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    captured: dict = {}
    monkeypatch.setattr(
        job_cli, "tick", lambda *args, **kwargs: captured.update(kwargs) or {}
    )

    class Ctx:
        def get_config(self, key, default=None):
            return {
                "verify_python": sys.executable,
                "gitleaks_path": "configured-scanner",
            }.get(key, default)

    args = SimpleNamespace(job_handler="tick", job_id="j", repo=str(tmp_path), gitleaks=None)

    assert job_cli.cmd_job(Ctx(), args, state_dir=tmp_path, client_factory=object) == 0

    assert captured["runner"].options["payload"] == {PAYLOAD_NAME: str(checkout)}


def test_factory_driver_inherits_serving_through_the_runner(monkeypatch, tmp_path):
    checkout = _make_checkout(tmp_path / "pinned")
    _payload_env(monkeypatch, checkout)
    captured: dict = {}
    monkeypatch.setattr(
        job_cli, "tick", lambda *args, **kwargs: captured.update(kwargs) or {}
    )
    tick = factory_driver.make_job_tick(
        tmp_path,
        client=object(),
        repo=tmp_path,
        gitleaks="configured-scanner",
        runner=execution.SandboxRunner({}),
    )

    tick("j")

    assert captured["runner"].options["payload"] == {PAYLOAD_NAME: str(checkout)}
