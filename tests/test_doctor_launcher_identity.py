"""The doctor's launcher build-identity and provision-capabilities check (RED first).

The launcher-identity check reports the running launcher's build identity in
three evidence tiers — receipt-backed, version-string-only, unavailable — and
classifies one read-only GET of ``/v1/provision/capabilities`` through the
already-authenticated launcher client. A version string without a deployment
receipt is always flagged (owner directive, 2026-11-14): a version string is
not deployment evidence. The capabilities classification never upgrades the
identity tier.

Two recorded gaps (documented, deliberately not fixed in this task's scope):
``client.public_health`` whitelists health-payload keys, so a real launcher's
deployment receipt is stripped before the doctor sees it (pinned below so the
future client change flips this file consciously); and RULES.md section 4 does
not exist in this repo — the no-unpause rule is enforced structurally (the
doctor has no write path) and pinned by the exact-request-list test.

No secret-shaped literal appears in this file: every example value that could
look like a credential is assembled from repeated characters at runtime.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from dataclasses import fields, MISSING

from bundlebees_plugin import doctor, provisioning
from bundlebees_plugin.client import ClientError, public_health
from tests.fakes import FakeCtx, load_plugin

CAP_PATH = provisioning.CAPABILITY_PATH
PIN = doctor.LAUNCHER_PIN
OTHER = "0.1.15"
RECEIPT_REV = "b" * 40

CAPS_OK = {
    "schema": provisioning.CAPABILITY_SCHEMA,
    "backend": "hyvv",
    "profiles": ["worker"],
    "enabled": True,
}
CAPS_DISABLED = dict(CAPS_OK, enabled=False)
CAPS_FOREIGN = dict(CAPS_OK, schema="rizzstack.provisioning/9")
CAPS_MALFORMED = dict(CAPS_OK, profiles="not-a-list")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _probe_names() -> list[str]:
    return [
        item.name
        for item in fields(doctor.Checkers)
        if item.default is MISSING and item.default_factory is MISSING
    ]


def _healthy_probe(name: str = "fixture"):
    def probe(context):
        return doctor.Finding("healthy", detail=name)

    return probe


def _all_healthy(**overrides):
    probes = {name: _healthy_probe() for name in _probe_names()}
    probes.update(overrides)
    return doctor.Checkers(**probes)


def _merge_defaults(base, keep):
    merged = {}
    for item in fields(doctor.Checkers):
        if item.name in keep or item.default is not MISSING or item.default_factory is not MISSING:
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


def _identity_row(client=None, *, settings=None):
    """Run one collect with only the launcher-identity probe left real."""
    ctx = _ctx(settings, client=client)
    checkers = _merge_defaults(doctor.default_checkers(), keep={"launcher_identity"})
    report = doctor.collect(ctx, None, checkers)
    return next(row for row in report["checks"] if row["check"] == "launcher_identity")


def _receipt(version: str = PIN) -> dict:
    return {"deployed_at": "2026-11-14T09:00:00Z", "version": version}


class FixtureLauncher:
    """A fixture launcher: records every authorized read the doctor asks of it."""

    def __init__(
        self,
        health: dict | None = None,
        *,
        health_error: Exception | None = None,
        capabilities_body=None,
        capabilities_error: Exception | None = None,
    ) -> None:
        self.health_payload = health
        self.health_error = health_error
        self.capabilities_body = capabilities_body
        self.capabilities_error = capabilities_error
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[object] = []

    def health(self) -> dict:
        self.requests.append(("GET", "/v1/health"))
        if self.health_error is not None:
            raise self.health_error
        return dict(self.health_payload or {})

    def request_json(self, method, path, payload=None, **_kwargs):
        self.requests.append((str(method), str(path)))
        self.bodies.append(payload)
        if self.capabilities_error is not None:
            raise self.capabilities_error
        return self.capabilities_body


# ---------------------------------------------------------------------------
# Identity tiers
# ---------------------------------------------------------------------------


def test_identity_tiers_distinguish_receipt_version_string_and_unavailable():
    client = FixtureLauncher(
        health={"ok": True, "version": PIN, "deployment_receipt": _receipt()},
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)
    assert row["state"] == "healthy"
    assert "deployment receipt" in row["detail"]
    assert doctor._IDENTITY_FLAG not in row["detail"]
    assert row["rev"] == PIN

    client = FixtureLauncher(health={"ok": True, "version": PIN}, capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert doctor._IDENTITY_FLAG in row["detail"]
    assert row["rev"] == PIN

    row = _identity_row(settings={"adapter": "fixture-unconfigured"})
    assert row["state"] == "unavailable"
    assert "no launcher adapter is configured" in row["detail"]


def test_version_string_only_is_never_healthy_and_carries_a_safe_fix():
    client = FixtureLauncher(health={"ok": True, "version": PIN}, capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert row["fix"]
    assert "restart" not in row["fix"].lower() or "never" in row["fix"].lower()


def test_receipt_backed_mismatch_is_stale_with_the_quiet_boundary_fix():
    client = FixtureLauncher(
        health={"ok": True, "version": OTHER, "deployment_receipt": _receipt(OTHER)},
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)
    assert row["state"] == "stale"
    assert doctor.LAUNCHER_PIN in row["detail"]
    assert "deployment receipt" in row["detail"]
    assert "never restart a busy launcher" in row["fix"]


def test_runtime_and_receipt_disagreement_is_unknown_not_healthy():
    client = FixtureLauncher(
        health={"ok": True, "version": PIN, "deployment_receipt": _receipt(OTHER)},
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert "disagree" in row["detail"]
    assert row["rev"] == PIN


def test_receipt_naming_a_revision_is_not_comparable_to_the_version_pin():
    client = FixtureLauncher(
        health={
            "ok": True,
            "version": PIN,
            "deployment_receipt": {"deployed_at": "2026-11-14T09:00:00Z", "rev": RECEIPT_REV},
        },
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert "not comparable" in row["detail"]
    assert row["rev"] == RECEIPT_REV


def test_unusable_receipt_falls_back_to_version_string_only_with_a_note():
    for bad in ({"version": PIN}, "yes", {"deployed_at": "2026-11-14T09:00:00Z"}):
        client = FixtureLauncher(
            health={"ok": True, "version": PIN, "deployment_receipt": bad},
            capabilities_body=CAPS_OK,
        )
        row = _identity_row(client)
        assert row["state"] == "unknown", bad
        assert doctor._IDENTITY_FLAG in row["detail"], bad
        assert "could not be validated" in row["detail"], bad


def test_unreachable_launcher_is_inaccessible_and_never_healthy():
    client = FixtureLauncher(health_error=OSError("connection refused"), capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "inaccessible"
    assert "did not answer its health endpoint" in row["detail"]
    assert doctor._IDENTITY_FLAG not in row["detail"]


def test_health_answer_without_identity_evidence_is_unknown():
    client = FixtureLauncher(health={"ok": True}, capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert "no usable build identity" in row["detail"]


# ---------------------------------------------------------------------------
# Capabilities probe classification
# ---------------------------------------------------------------------------


def test_capabilities_classifications_cover_the_full_vocabulary():
    cases = [
        ({"capabilities_body": CAPS_OK}, "ok"),
        ({"capabilities_body": CAPS_DISABLED}, "disabled"),
        ({"capabilities_body": CAPS_FOREIGN}, "incompatible"),
        ({"capabilities_body": CAPS_MALFORMED}, "incompatible"),
        ({"capabilities_error": ClientError("no such route", status=404)}, "not advertised"),
        ({"capabilities_error": ClientError("unauthorized", "unauthorized", status=401)}, "refused"),
        ({"capabilities_error": ClientError("forbidden", "forbidden", status=403)}, "refused"),
        ({"capabilities_error": ClientError("unavailable", status=503)}, "answered 503"),
        ({"capabilities_body": ["not-an-object"]}, "unreadable"),
        (
            {"capabilities_error": ClientError("bad json", "launcher_error")},
            "unreadable",
        ),
    ]
    for kwargs, expected in cases:
        client = FixtureLauncher(health={"ok": True, "version": PIN}, **kwargs)
        ctx = _ctx(client=client)
        checkers = _merge_defaults(doctor.default_checkers(), keep={"launcher_identity"})
        context = doctor.Context(ctx, None, checkers)
        classification, fragment = doctor._capabilities_finding(context)
        assert classification == expected, (expected, classification, fragment)
        assert fragment.startswith("capabilities: "), (expected, fragment)


def test_unconfigured_credential_is_unreadable_not_ok():
    client = FixtureLauncher(
        health_error=ClientError("the launcher is not configured", "not_configured"),
        capabilities_error=ClientError("the launcher is not configured", "not_configured"),
    )
    row = _identity_row(client)
    assert "capabilities: unreadable" in row["detail"]


def test_capabilities_outcomes_never_change_the_identity_tier():
    client = FixtureLauncher(health={"ok": True, "version": PIN}, capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert doctor._IDENTITY_FLAG in row["detail"]
    assert "capabilities: ok" in row["detail"]

    client = FixtureLauncher(health={"ok": True}, capabilities_body=CAPS_OK)
    row = _identity_row(client)
    assert row["state"] == "unknown"
    assert "capabilities: ok" in row["detail"]

    client = FixtureLauncher(
        health={"ok": True, "version": PIN, "deployment_receipt": _receipt()},
        capabilities_body=CAPS_FOREIGN,
    )
    row = _identity_row(client)
    assert row["state"] == "healthy"
    assert "capabilities: incompatible" in row["detail"]

    client = FixtureLauncher(
        health_error=OSError("connection refused"),
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)
    assert row["state"] == "inaccessible"
    assert "capabilities: ok" in row["detail"]


# ---------------------------------------------------------------------------
# Authorized read paths: exactly two GETs, no subprocess, no secrets
# ---------------------------------------------------------------------------


def test_the_probe_reads_exactly_two_authorized_gets_and_never_spawns(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the doctor identity check must not spawn a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(doctor, "_detect_backend", lambda options: "bwrap")

    client = FixtureLauncher(
        health={"ok": True, "version": PIN, "deployment_receipt": _receipt()},
        capabilities_body=CAPS_OK,
    )
    row = _identity_row(client)

    assert row["state"] == "healthy"
    assert client.requests == [("GET", "/v1/health"), ("GET", CAP_PATH)]
    assert all(method == "GET" for method, _path in client.requests)
    assert all(body is None for body in client.bodies)
    lowered = [path.lower() for _method, path in client.requests]
    assert all("token" not in path and "authorization" not in path for path in lowered)


def test_the_no_unpause_rule_holds_because_only_reads_exist(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the doctor never issues a mutating request")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(doctor, "_detect_backend", lambda options: "bwrap")

    client = FixtureLauncher(health={"ok": True, "version": PIN}, capabilities_body=CAPS_OK)
    _identity_row(client)
    assert client.requests == [("GET", "/v1/health"), ("GET", CAP_PATH)]


# ---------------------------------------------------------------------------
# Roster, output parity, and the recorded client gap
# ---------------------------------------------------------------------------


def test_the_check_is_in_the_roster_once_and_parity_semantics_unchanged(capsys):
    client = FixtureLauncher(
        health={"ok": True, "version": OTHER, "deployment_receipt": _receipt(OTHER)},
        capabilities_body=CAPS_OK,
    )
    ctx = _ctx(client=client)
    ctx.bundlebees_doctor_checkers = _merge_defaults(doctor.default_checkers(), keep={"launcher_identity"})

    _invoke(ctx, ["doctor", "--json", "--check-parity"])
    payload = json.loads(capsys.readouterr().out)
    names = [row["check"] for row in payload["checks"]]
    assert names.count("launcher_identity") == 1
    assert names[-1] == "launcher_identity"
    row = next(row for row in payload["checks"] if row["check"] == "launcher_identity")
    assert row["state"] == "stale"
    assert payload["parity_ok"] is True

    rc = _invoke(ctx, ["doctor", "--check-parity"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "launcher_identity: stale" in out
    assert any(item.startswith("launcher_identity:") for item in payload["remediation"])


def test_recorded_gap_the_real_client_strips_receipts_before_the_doctor():
    """Recorded gap, deliberately not fixed here (client.py is out of scope).

    ``client.public_health`` whitelists health-payload keys, so a real
    launcher's deployment receipt is stripped before the doctor sees it; the
    identity check then reports version-string-only and flags it. When the
    client mapping learns the receipt key, this test changes with it.
    """
    payload = {"ok": True, "version": PIN, "deployment_receipt": _receipt()}
    assert public_health(payload) == {"ok": True, "version": PIN}
