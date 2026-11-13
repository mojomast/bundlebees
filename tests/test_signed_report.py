"""Approved acceptance tests for contract BR1-signed-report (cross-repo gap with Orbit #26, RED first).

``hermes bundlebees tdd report`` writes a signed ``acceptance_report/1`` artifact at
``<state>/reports/<contract_id>.json`` (0600, directory 0700, atomic). It carries the report fields Orbit's
ACDD evidence view reads (``contract_id``, ``contract_sha256``, ``accepted``, ``phases``, ``criteria``) and a
lowercase hex HMAC-SHA256 ``signature`` over Python canonical JSON (sort_keys, compact separators, ASCII) of
the report without ``signature`` and ``_``-prefixed keys, keyed with the bundlebees evidence key (derived
from ``lease.key``). That is the scheme Orbit #26 (d08b8f6, ``verifyRecordSignature`` /
``bundlebeesEvidenceKey``) checks. A not-accepted report is written too, with ``accepted: false``.

The cross-repo test runs Orbit's real verifier from an orbitdesktop checkout named by ``ORBIT_ACDD_SRC``; it
skips without one unless ``BUNDLEBEES_REQUIRE_ROUNDTRIP=1``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bundlebees_plugin import leases, tdd
from tests.test_cli_acdd import GOOD_PATCH, FakeClient, _ctx, _frozen, _invoke, _lease, _repo, _result, _verify, _write

ORBIT_ACDD_SRC = os.environ.get("ORBIT_ACDD_SRC", "")
NODE = shutil.which("node")


def _accepted_run(tmp_path: Path, capsys, *, green: bool = True) -> tuple[Path, dict, Path]:
    repo, base = _repo(tmp_path)
    state = tmp_path / "state"
    frozen = _frozen(base, evidence_required=["red", "green"])
    _lease(state, base, frozen)
    path = _write(tmp_path / "frozen.json", frozen)
    assert _invoke(_ctx(), ["tdd", "red", str(path), "--workdir", str(repo), "--state-dir", str(state)]) == 0
    if green:
        assert _verify(_ctx(FakeClient(_result(GOOD_PATCH, base))), repo, state) == 0
    capsys.readouterr()
    return state, frozen, path


def _report(state: Path, frozen: dict) -> Path:
    return state / "reports" / f"{frozen['id']}.json"


def test_report_writes_a_signed_private_artifact(tmp_path, capsys):
    state, frozen, path = _accepted_run(tmp_path, capsys)
    assert _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    file = _report(state, frozen)
    assert file.is_file() and not file.is_symlink()
    assert file.stat().st_mode & 0o777 == 0o600 and file.parent.stat().st_mode & 0o777 == 0o700
    assert f"report: reports/{frozen['id']}.json" in out and str(state) not in out
    data = json.loads(file.read_text())
    assert data["report_version"] == "acceptance_report/1"
    assert data["contract_id"] == frozen["id"] and data["contract_sha256"] == frozen["review"]["contract_sha256"]
    assert data["accepted"] is True and data["phases"]["green"] == "valid"
    assert all(item["status"] == "passed" for item in data["criteria"].values())
    assert tdd.verify_signature(data, leases.evidence_key(state))
    assert "lease.key" not in file.read_text()


def test_a_tampered_report_no_longer_verifies(tmp_path, capsys):
    state, frozen, path = _accepted_run(tmp_path, capsys)
    _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state)])
    data = json.loads(_report(state, frozen).read_text())
    data["accepted"] = False
    assert not tdd.verify_signature(data, leases.evidence_key(state))


def test_a_not_accepted_report_is_written_and_says_so(tmp_path, capsys):
    state, frozen, path = _accepted_run(tmp_path, capsys, green=False)
    assert _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state)]) == 1
    data = json.loads(_report(state, frozen).read_text())
    assert data["accepted"] is False and data["phases"]["green"] == "unrun"
    assert tdd.verify_signature(data, leases.evidence_key(state))


def test_the_json_flag_prints_the_signed_report(tmp_path, capsys):
    state, frozen, path = _accepted_run(tmp_path, capsys)
    _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state), "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(_report(state, frozen).read_text())


_NODE_CHECK = r"""
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [src, state, contractFile, reportFile] = process.argv.slice(2);
const m = await import(pathToFileURL(path.join(src, 'server', 'acdd-evidence.mjs')).href);
const leaseKey = readFileSync(path.join(state, 'lease.key'));
const keys = [leaseKey, m.bundlebeesEvidenceKey(leaseKey)].filter(Boolean);
const report = JSON.parse(readFileSync(reportFile, 'utf8'));
const contract = JSON.parse(readFileSync(contractFile, 'utf8'));
const verify = (r) => keys.some((k) => m.verifyRecordSignature(r, k));
const view = m.buildEvidenceView(contract, [], report, { verify, verificationAvailable: true });
const tampered = { ...report, accepted: !report.accepted };
process.stdout.write(JSON.stringify({ verified: verify(report), accepted: view.accepted, tampered: verify(tampered) }) + '\n');
"""


@pytest.mark.skipif(
    not (ORBIT_ACDD_SRC and NODE and Path(ORBIT_ACDD_SRC, "server", "acdd-evidence.mjs").is_file())
    and os.environ.get("BUNDLEBEES_REQUIRE_ROUNDTRIP") != "1",
    reason="ORBIT_ACDD_SRC (an orbitdesktop checkout with the ACDD evidence verifier) and node are needed",
)
def test_orbit_acdd_verifier_accepts_the_real_bundlebees_report(tmp_path, capsys):
    assert ORBIT_ACDD_SRC and NODE and Path(ORBIT_ACDD_SRC, "server", "acdd-evidence.mjs").is_file()
    state, frozen, path = _accepted_run(tmp_path, capsys)
    assert _invoke(_ctx(), ["tdd", "report", str(path), "--state-dir", str(state)]) == 0
    script = tmp_path / "check.mjs"
    script.write_text(_NODE_CHECK)
    proc = subprocess.run([NODE, str(script), ORBIT_ACDD_SRC, str(state), str(path), str(_report(state, frozen))],
                          capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")})
    assert proc.returncode == 0, proc.stderr[-800:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result == {"verified": True, "accepted": True, "tampered": False}, result
