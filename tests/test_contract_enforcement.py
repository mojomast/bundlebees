"""RED-first acceptance tests for the F2 ``require_contract`` enforcement machinery.

Reliability campaign F2 (corrected-lease r2). Contract:

- ``require_contract`` is durable configuration resolved at the
  campaign/repository/execution-profile boundary (``settings.py``), never
  session state, and never readable from the brief itself.
- When it is enabled for a scope, dispatch refuses work that lacks a valid
  frozen ``acdd/1`` contract with the machine-readable refusal
  ``contract_required``, before any launcher call.
- Attached contracts are validated before dispatch: the frozen hash is
  verified, provenance is checked against the pinned workflow revision, and
  acceptance ids must stay consistent across contract, feature packet, brief
  and verification evidence. Tampered, stale and widened contracts are
  refused with reasons.
- ``python3 -m bundlebees_plugin.contracts scaffold`` generates a valid
  ``acdd/1`` contract plus a feature packet skeleton.
- Legacy compatibility: enforcement is disabled by default and every legacy
  dispatch is labelled, never silent. The only profile that ships with the
  flag on is the documented M2 canary profile, which opts in explicitly.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import types

import pytest

from bundlebees_plugin import contract as acdd
from tests import _packet_fixtures as packet_fixtures
from tests.fakes import FakeCtx

SHA = "a" * 40
OTHER_SHA = "b" * 40
ASSIGNED = "shop/pricing.py"
TEST_FILE = "tests/test_pricing.py"
TEST_ID = f"{TEST_FILE}::test_apply_tax"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _contract(**over):
    value = {
        "contract_version": "acdd/1",
        "id": "W1-f2",
        "title": "Add apply_tax",
        "outcome": {"summary": "tax", "observable_behavior": ["apply_tax(1000, 10) == 1100"]},
        "acceptance_criteria": [
            {
                "id": "AC-001",
                "given": "1000",
                "when": "apply_tax",
                "then": "1100",
                "verification": "test",
                "tests": [TEST_ID],
            }
        ],
        "scope": {"assigned_files": [ASSIGNED], "test_files": [TEST_FILE]},
        "source_rev": SHA,
        "tdd": {"applicable": True},
        "verification": {"independent": True},
        "evidence_required": ["red", "green"],
        "authorization": {"human_gates": ["merge", "release", "deploy", "publish"]},
    }
    value.update(over)
    return value


def _frozen(**over):
    return acdd.freeze_contract(_contract(**over), approved_by="kyle")


def _widened_after_freeze():
    tampered = dict(_frozen())
    tampered["scope"] = {"assigned_files": [ASSIGNED, "shop/extra.py"],
                         "test_files": [TEST_FILE]}
    return tampered


def _brief(contract=None, **over):
    brief = {
        "objective": "implement",
        "project": "demo",
        "source_rev": SHA,
        "assigned_files": [ASSIGNED],
        "acceptance": ["AC-001 passes"],
    }
    if contract is not None:
        brief["contract"] = contract
    brief.update(over)
    return brief


_ENABLED = {"require_contract": True}


def _packet(frozen, workflow_ref, **over):
    packet = packet_fixtures.exact_packet(
        contract=frozen,
        contract_sha256=str(frozen["review"]["contract_sha256"]),
        workflow_ref=workflow_ref,
    )
    packet.update(over)
    return packet


@pytest.fixture()
def pinned_checkout(tmp_path, monkeypatch):
    """A minimal pinned workflow checkout; returns ``(path, head_sha)``."""

    repo = tmp_path / "workflow"
    (repo / "tools").mkdir(parents=True)
    (repo / "tools" / "feature_packet.py").write_text("# pinned validator\n", encoding="utf-8")

    def run(*argv: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(repo), *argv], capture_output=True, text=True
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    run("init", "-q")
    run("-c", "user.email=acdd@example.invalid", "-c", "user.name=acdd",
        "commit", "--allow-empty", "--message", "pin")
    head = run("rev-parse", "HEAD")
    monkeypatch.setenv("WORKFLOW_SRC", str(repo))
    return repo, head


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, brief, **kwargs):
        self.calls.append(dict(brief))
        return {"attempt_id": "att_f2f2f2f2f2f2f2f2", "state": "queued"}


# ---------------------------------------------------------------------------
# The machine-readable refusal
# ---------------------------------------------------------------------------


def test_enabled_scope_refuses_work_without_a_contract():
    from bundlebees_plugin import contracts

    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.pre_dispatch(_brief(), config=dict(_ENABLED))
    refusal = caught.value
    assert refusal.code == "contract_required"
    body = refusal.to_dict()
    assert body["ok"] is False
    assert body["code"] == "contract_required"
    assert body["error"].strip() and body["fix"].strip()


def test_enabled_dispatch_refuses_before_the_launcher_is_called():
    from bundlebees_plugin import contract_gate as dispatch

    send = _Recorder()
    with pytest.raises(dispatch.ContractRefusal) as caught:
        dispatch.dispatch(_brief(), config=dict(_ENABLED), send=send)
    assert caught.value.code == "contract_required"
    assert send.calls == []


# ---------------------------------------------------------------------------
# Valid contract-bound work succeeds
# ---------------------------------------------------------------------------


def test_valid_contract_bound_work_dispatches_and_labels_the_envelope():
    from bundlebees_plugin import contract_gate as dispatch

    frozen = _frozen()
    send = _Recorder()
    block, result = dispatch.dispatch(
        _brief(frozen), config=dict(_ENABLED), send=send
    )
    assert block["mode"] == "contract"
    assert block["contract_id"] == "W1-f2"
    assert block["contract_sha256"] == acdd.contract_sha256(frozen)
    assert block["tdd_applicable"] is True
    assert block["acceptance_ids"] == ["AC-001"]
    assert len(block["contract_sha256"]) == 64
    assert result["attempt_id"] == "att_f2f2f2f2f2f2f2f2"
    assert len(send.calls) == 1


def test_gate_never_mutates_the_brief():
    from bundlebees_plugin import contract_gate as dispatch

    frozen = _frozen()
    brief = _brief(frozen)
    before = copy.deepcopy(brief)
    dispatch.gate(brief, config=dict(_ENABLED))
    assert brief == before


# ---------------------------------------------------------------------------
# Disabled by default; legacy is labelled, never silent
# ---------------------------------------------------------------------------


def test_enforcement_is_off_by_default_and_legacy_is_labelled():
    from bundlebees_plugin import contracts, contract_gate as dispatch, settings

    assert settings.require_contract(settings.DEFAULTS) is False
    block = dispatch.gate(_brief(), config=dict(settings.DEFAULTS))
    assert block["mode"] == "legacy"
    assert "no ACDD contract" in block["note"]

    send = _Recorder()
    _, _ = dispatch.dispatch(_brief(), config={}, send=send)
    assert len(send.calls) == 1


def test_only_an_explicit_opt_in_enables_enforcement():
    from bundlebees_plugin import settings

    for value in (False, "false", "0", "off", "", None):
        assert settings.require_contract({"require_contract": value}) is False
    for value in (True, "true", "1", "yes", "on"):
        assert settings.require_contract({"require_contract": value}) is True
    assert settings.require_contract({}) is False


# ---------------------------------------------------------------------------
# Tampered / stale / widened / unfrozen / invalid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make, code",
    [
        (lambda: _brief(dict(_frozen(), title="changed after freeze")), "contract_changed"),
        (lambda: _brief(_widened_after_freeze()), "contract_changed"),
        (lambda: _brief(_frozen(), source_rev=OTHER_SHA), "stale_revision"),
        (lambda: _brief(_frozen(), assigned_files=[ASSIGNED, "shop/extra.py"]), "scope_violation"),
        (lambda: _brief(_frozen(), assigned_files=[ASSIGNED, TEST_FILE]), "tests_tampered"),
        (lambda: _brief(_contract()), "contract_not_frozen"),
        (lambda: _brief(dict(_contract(), source_rev="zz")), "contract_invalid"),
        (lambda: _brief(dict(_contract(), contract_version="acdd/2")), "contract_invalid"),
    ],
)
def test_bad_contracts_are_refused_with_a_reason(make, code):
    from bundlebees_plugin import contract_gate as dispatch

    with pytest.raises(dispatch.ContractRefusal) as caught:
        dispatch.gate(make(), config=dict(_ENABLED))
    assert caught.value.code == code
    assert caught.value.error.strip() and caught.value.fix.strip()


# ---------------------------------------------------------------------------
# Provenance: the pinned workflow revision
# ---------------------------------------------------------------------------


def test_packet_provenance_must_be_the_pinned_workflow_revision(pinned_checkout):
    from bundlebees_plugin import contracts, contract_gate as dispatch

    _, head = pinned_checkout
    frozen = _frozen()
    good = dispatch.gate(
        _brief(frozen), config=dict(_ENABLED), packet=_packet(frozen, head)
    )
    assert good["provenance"]["workflow_ref"] == head

    with pytest.raises(contracts.ContractRefusal) as caught:
        dispatch.gate(
            _brief(frozen), config=dict(_ENABLED), packet=_packet(frozen, OTHER_SHA)
        )
    assert caught.value.code == "provenance_mismatch"


def test_provenance_fails_closed_without_a_pinned_checkout(pinned_checkout, monkeypatch):
    from bundlebees_plugin import contracts

    _, head = pinned_checkout
    monkeypatch.delenv("WORKFLOW_SRC", raising=False)
    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.pre_dispatch(
            _brief(_frozen()), config=dict(_ENABLED),
            packet=_packet(_frozen(), head),
        )
    assert caught.value.code == "provenance_unavailable"


def test_a_packet_without_a_workflow_ref_is_refused(pinned_checkout):
    from bundlebees_plugin import contracts

    packet = _packet(_frozen(), "f" * 0)  # placeholder; workflow_ref removed below
    packet = {key: value for key, value in packet.items() if key != "workflow_ref"}
    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.pre_dispatch(
            _brief(_frozen()), config=dict(_ENABLED), packet=packet
        )
    assert caught.value.code == "provenance_invalid"


def test_a_packet_sealed_to_another_contract_is_refused(pinned_checkout):
    from bundlebees_plugin import contracts

    _, head = pinned_checkout
    other = _frozen(id="W1-other")  # a different contract body hashes differently
    packet = _packet(_frozen(), head)
    packet["contract_sha256"] = str(other["review"]["contract_sha256"])
    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.pre_dispatch(
            _brief(_frozen()), config=dict(_ENABLED), packet=packet
        )
    assert caught.value.code == "contract_changed"


# ---------------------------------------------------------------------------
# Acceptance ids consistent across contract / packet / brief / evidence
# ---------------------------------------------------------------------------


def test_acceptance_ids_stay_consistent_across_all_artifacts(pinned_checkout):
    from bundlebees_plugin import contract_gate as dispatch

    _, head = pinned_checkout
    frozen = _frozen()
    evidence = [{"criteria": {"AC-001": "passed"}, "phase": "green"}]
    block = dispatch.gate(
        _brief(frozen), config=dict(_ENABLED),
        packet=_packet(frozen, head), evidence=evidence,
    )
    assert block["acceptance_ids"] == ["AC-001"]


@pytest.mark.parametrize(
    "packet_ids, evidence, brief_over",
    [
        (["AC-009"], None, {}),
        (["AC-001", "AC-002"], None, {}),
        (None, None, {}),
        (None, [{"criteria": {"AC-002": "passed"}}], {}),
        (None, [{"criteria": {"AC-001": "passed"}}, {"acceptance_ids": ["AC-002"]}], {}),
        (None, None, {"acceptance": ["AC-002 passes"]}),
    ],
)
def test_inconsistent_acceptance_ids_are_refused(pinned_checkout, packet_ids, evidence, brief_over):
    from bundlebees_plugin import contracts

    _, head = pinned_checkout
    frozen = _frozen()
    packet = _packet(frozen, head)
    if packet_ids is None:
        del packet["acceptance_ids"]
    else:
        packet["acceptance_ids"] = packet_ids
    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.pre_dispatch(
            _brief(frozen, **brief_over), config=dict(_ENABLED),
            packet=packet, evidence=evidence,
        )
    assert caught.value.code == "acceptance_mismatch"


# ---------------------------------------------------------------------------
# Durable configuration, not session state
# ---------------------------------------------------------------------------


def test_the_flag_is_durable_config_and_survives_a_restart():
    from bundlebees_plugin import contract_gate as dispatch, settings

    first = FakeCtx(settings={"require_contract": "true"})
    config_first = dispatch.enforcement_config(first)
    assert settings.require_contract(config_first) is True
    with pytest.raises(dispatch.ContractRefusal):
        dispatch.gate(_brief(), config=config_first)

    # A brand-new "process" resolves the flag again from configuration only.
    second = FakeCtx(settings={"require_contract": "true"})
    assert dispatch.enforcement_config(second) == config_first
    with pytest.raises(dispatch.ContractRefusal):
        dispatch.gate(_brief(), config=dispatch.enforcement_config(second))

    # Nothing was cached: flipping the durable config flips the decision.
    assert settings.require_contract(dispatch.enforcement_config(FakeCtx())) is False


def test_a_brief_cannot_switch_enforcement_off():
    from bundlebees_plugin import contract_gate as dispatch

    brief = _brief()
    brief["require_contract"] = False
    brief["acdd"] = {"mode": "legacy"}
    with pytest.raises(dispatch.ContractRefusal) as caught:
        dispatch.gate(brief, config=dict(_ENABLED))
    assert caught.value.code == "contract_required"


def test_enforcement_scopes_to_campaign_and_repository():
    from bundlebees_plugin import settings

    scoped = {
        "require_contract": {
            "default": False,
            "campaigns": {"reliability": True},
            "repositories": {"mojomast/bundlebees": True},
        }
    }
    assert settings.require_contract(scoped) is False
    assert settings.require_contract(scoped, campaign="reliability") is True
    assert settings.require_contract(scoped, repository="mojomast/bundlebees") is True
    assert settings.require_contract(scoped, campaign="other") is False


# ---------------------------------------------------------------------------
# Settings boundary: execution profile and Hermes profile file
# ---------------------------------------------------------------------------


def test_settings_boundary_reads_the_execution_profile_context():
    from bundlebees_plugin import settings

    config = settings.settings_from_ctx(FakeCtx(settings={"require_contract": "true"}))
    assert settings.require_contract(config) is True
    assert settings.require_contract(settings.settings_from_ctx(FakeCtx())) is False


def test_settings_boundary_reads_the_hermes_profile_file(monkeypatch):
    from bundlebees_plugin import settings

    stored = {
        "plugins": {"entries": {"bundlebees": {"settings": {"require_contract": True}}}}
    }
    stub = types.ModuleType("hermes_cli.config")
    stub.load_config_readonly = lambda: stored
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", stub)
    config = settings.settings_from_hermes_config()
    assert settings.require_contract(config) is True


def test_settings_boundary_falls_back_to_defaults_without_hermes(monkeypatch):
    from bundlebees_plugin import settings

    monkeypatch.setitem(sys.modules, "hermes_cli", None)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)
    config = settings.settings_from_hermes_config()
    assert settings.require_contract(config) is False


# ---------------------------------------------------------------------------
# Scaffold: a valid contract plus a feature packet skeleton
# ---------------------------------------------------------------------------


def test_scaffold_generates_a_valid_contract_and_a_packet_skeleton():
    from bundlebees_plugin import contracts

    built = contracts.scaffold(
        contract_id="W1-f2",
        title="Add apply_tax",
        summary="apply_tax adds ten percent",
        behaviors=["apply_tax(1000, 10) == 1100"],
        tests=[TEST_ID],
        assigned_files=[ASSIGNED],
        test_files=[TEST_FILE],
        source_rev=SHA,
        feature_id="feature-f2",
        job_id="job_f2",
    )
    validated = acdd.validate_contract(built["contract"])
    assert validated["id"] == "W1-f2"
    assert [c["id"] for c in validated["acceptance_criteria"]] == ["AC-001"]
    frozen = acdd.freeze_contract(built["contract"], approved_by="kyle")
    assert len(frozen["review"]["contract_sha256"]) == 64

    packet = built["packet"]
    assert packet["packet_version"] == "rizzstack.feature-packet/1"
    assert packet["feature_id"] == "feature-f2"
    assert packet["job_id"] == "job_f2"
    assert packet["acceptance_ids"] == ["AC-001"]
    assert packet["tests"] == [TEST_ID]
    assert packet["scope"]["lease"] == [ASSIGNED]
    assert packet["contract"] == validated
    assert packet["workflow_ref"] == contracts.PIN_WORKFLOW_REVISION


def test_scaffold_output_passes_the_enabled_gate(pinned_checkout):
    from bundlebees_plugin import contracts, contract_gate as dispatch

    _, head = pinned_checkout
    built = contracts.scaffold(
        contract_id="W1-f2",
        title="Add apply_tax",
        summary="apply_tax adds ten percent",
        behaviors=["apply_tax(1000, 10) == 1100"],
        tests=[TEST_ID],
        assigned_files=[ASSIGNED],
        test_files=[TEST_FILE],
        source_rev=SHA,
    )
    frozen = acdd.freeze_contract(built["contract"], approved_by="kyle")
    packet = _packet(frozen, head)
    block = dispatch.gate(
        _brief(frozen), config=dict(_ENABLED), packet=packet,
        evidence=[{"criteria": {"AC-001": "passed"}}],
    )
    assert block["mode"] == "contract"


def test_scaffold_refuses_an_lease_that_touches_the_approved_tests():
    from bundlebees_plugin import contracts

    with pytest.raises(contracts.ContractRefusal) as caught:
        contracts.scaffold(
            contract_id="W1-f2",
            title="Self-judging lease",
            summary="the worker edits its own judge",
            behaviors=["x"],
            tests=[TEST_ID],
            assigned_files=[ASSIGNED, TEST_FILE],
            test_files=[TEST_FILE],
            source_rev=SHA,
        )
    assert caught.value.code == "contract_invalid"


def test_scaffold_cli_smoke(tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = tmp_path / "scaffold"
    done = subprocess.run(
        [
            sys.executable, "-m", "bundlebees_plugin.contracts", "scaffold",
            "--id", "W1-f2",
            "--title", "Add apply_tax",
            "--summary", "apply_tax adds ten percent",
            "--behavior", "apply_tax(1000, 10) == 1100",
            "--test", TEST_ID,
            "--assigned", ASSIGNED,
            "--test-file", TEST_FILE,
            "--source-rev", SHA,
            "--feature-id", "feature-f2",
            "--job-id", "job_f2",
            "--out", str(out),
        ],
        capture_output=True, text=True, cwd=repo_root,
        env={**os.environ, "PYTHONPATH": repo_root},
    )
    assert done.returncode == 0, done.stderr
    contract_body = json.loads((out / "contract.json").read_text(encoding="utf-8"))
    packet = json.loads((out / "packet.json").read_text(encoding="utf-8"))
    assert acdd.validate_contract(contract_body)["id"] == "W1-f2"
    assert packet["packet_version"] == "rizzstack.feature-packet/1"
    assert packet["acceptance_ids"] == ["AC-001"]


# ---------------------------------------------------------------------------
# The dispatch check surface stays machine-readable
# ---------------------------------------------------------------------------


def test_check_cli_prints_the_refusal_and_exits_nonzero(tmp_path, capsys):
    from bundlebees_plugin import contract_gate as dispatch

    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(_brief()), encoding="utf-8")
    code = dispatch.main(
        ["check", str(brief_path)],
        config={"require_contract": True},
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["code"] == "contract_required"
