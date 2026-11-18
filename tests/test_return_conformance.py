"""Run the unmodified, pinned Workflow validator on native RETURN bytes."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from bundlebees_plugin import return_report, verify_local
from tests.test_return_report import _build_exact, _copy_state, HAS_WORKFLOW, REQUIRE_WORKFLOW

pytestmark = pytest.mark.skipif(not (HAS_WORKFLOW or REQUIRE_WORKFLOW), reason="requires WORKFLOW_SRC")


def validator():
    return Path(os.environ['WORKFLOW_SRC']) / 'tools/return_manifest.py'


def check(path):
    return subprocess.run([sys.executable, str(validator()), str(path)], text=True, capture_output=True)


def test_retained_malformed_native_bytes():
    path = Path(__file__).parent / 'fixtures/return/r18-malformed.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '7eeae1946c2e3261443720faab4f357a02db34a64f1775f8e46ddf95783336e6'
    doc = json.loads(path.read_bytes())
    assert doc['evidence_digest'] == return_report.evidence_digest(doc) == '000940e81034bf2b02dbce23e03e203b48f71d8311b6443ff91383dec80e38b2'
    schema = json.loads((validator().parents[1] / 'schemas/return-manifest.schema.json').read_bytes())
    assert {e.json_path for e in jsonschema.Draft202012Validator(schema).iter_errors(doc)} == {
        '$.authorization.authority', '$.red_green.red', '$.red_green.green', '$.verification.suite',
        '$.verification.gitleaks', '$.integration.wave', '$.attempts[0].project',
    }
    assert check(path).returncode == 1


@pytest.fixture(scope='module')
def campaign(tmp_path_factory):
    # Only this test-authored repository and pytest command; real tests, real
    # local commits, real gitleaks. Does not exercise a sandbox/remote worker.
    def run(workdir, argv):
        assert argv[:4] == [sys.executable, '-m', 'pytest', '-q']
        proc = subprocess.run(argv, cwd=workdir, capture_output=True, timeout=60)
        return dict(exit_code=proc.returncode, timed_out=False)

    return _build_exact(tmp_path_factory.mktemp('conformance'), runner=run)


def test_exact_native_bytes_validate_and_replay(campaign, tmp_path):
    state = _copy_state(campaign, tmp_path)
    doc = return_report.build(state, 'job_a', repo=campaign['repo'])
    result = check(return_report.return_path(state, 'job_a'))
    assert result.returncode == 0, result.stderr
    assert doc['verification']['suite'] == dict(result='PASS', command=[sys.executable, '-m', 'pytest', '-q'])
    assert doc['verification']['gitleaks']['result'] == 'PASS'
    assert doc['attempts'][0]['project'] == 'smoke-real'
    assert return_report.verify(state, 'job_a', repo=campaign['repo'])['ok']


def test_wave_inputs_bind_the_verified_commit_and_tested_tree(campaign, tmp_path):
    from bundlebees_plugin import job as jobs

    state = _copy_state(campaign, tmp_path)
    job = jobs.load_job(state, 'job_a', recover=False)
    wave, = return_report._waves(state, job, prove_inputs_at=campaign['repo'])
    assert wave['verified_inputs'] == {'att_1': campaign['verified']}
    # Still a real commit and the right attempt, but not the candidate that
    # earned this particular sealed integration handoff.
    path = state / 'wave-identities/job_a.w1.json'
    receipt = json.loads(path.read_bytes())
    receipt['identity']['attempts'][0]['sha'] = campaign['root']
    path.write_text(json.dumps(receipt))
    with pytest.raises(return_report.ReturnError) as caught:
        return_report._waves(state, job, prove_inputs_at=campaign['repo'])
    assert caught.value.code == 'wave_mismatch'


def test_unpinned_cases_appended_to_receipt_cannot_replace_signed_green(campaign, tmp_path):
    state = _copy_state(campaign, tmp_path)
    doc = return_report.build(state, 'job_a', repo=campaign['repo'])
    green = doc['red_green']['green']
    if green:
        (state / green['record_path']).unlink()
    path = verify_local.record_path(state, 'att_1')
    receipt = json.loads(path.read_bytes())
    receipt['suite']['cases'] = [dict(id=case, outcome='passed') for case in doc['acceptance'][0]['tests']]
    path.write_text(json.dumps(receipt))
    for operation in (return_report.verify, return_report.build):
        with pytest.raises(return_report.ReturnError, match='AC-001: UNVERIFIED'):
            operation(state, 'job_a', repo=campaign['repo'])


@pytest.mark.parametrize('change', ['unrun_scan', 'timeout', 'blocking', 'bool_exit', 'wrong_base', 'resumed', 'wrong_candidate'])
def test_native_incomplete_or_contradictory_evidence_cannot_publish(campaign, tmp_path, change):
    state = _copy_state(campaign, tmp_path)
    path = verify_local.record_path(state, 'att_1')
    record = json.loads(path.read_bytes())
    if change == 'unrun_scan':
        record['gitleaks'] = None
    elif change == 'wrong_base':
        record['base_rev'] = campaign['verified']
    elif change == 'resumed':
        record['resumed'] = True
    elif change == 'wrong_candidate':
        record['sha'] = campaign['accepted']
    else:
        record['suite'].update({'timeout': {'timed_out': True}, 'blocking': {'blocking': ['contract']},
                                'bool_exit': {'exit_code': False}}[change])
    path.write_text(json.dumps(record))
    with pytest.raises(return_report.ReturnError):
        return_report.build(state, 'job_a', repo=campaign['repo'])
    assert not return_report.return_path(state, 'job_a').exists()


@pytest.mark.parametrize('field', ['authorization', 'receiver_decision', 'tests', 'wave', 'candidate'])
def test_independent_replay_refuses_resealed_substitution(campaign, tmp_path, field):
    state = _copy_state(campaign, tmp_path)
    doc = return_report.build(state, 'job_a', repo=campaign['repo'])
    if field == 'authorization':
        doc['authorization']['reference'] = 'authorizations/job_b.json'
    elif field == 'receiver_decision':
        doc['receiver_decision']['decided_by'] = 'fixture'
    elif field == 'tests':
        doc['acceptance'][0]['tests'] = ['test_other.py']
    elif field == 'wave':
        doc['integration']['accepted_wave_digest'] = 'b' * 64
    else:
        doc['verification']['commit'] = campaign['root']
    doc['evidence_digest'] = return_report.evidence_digest(doc)
    return_report.return_path(state, 'job_a').write_bytes(return_report.document_bytes(doc))
    return_report.render_path(state, 'job_a').write_text(return_report.render(doc))
    with pytest.raises(return_report.ReturnError, match='.') as caught:
        return_report.verify(state, 'job_a', repo=campaign['repo'])
    assert caught.value.code == 'report_tampered'
