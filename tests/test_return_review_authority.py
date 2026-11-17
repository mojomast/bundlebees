"""A signed coordinator observation cannot authenticate an independent reviewer."""

import pytest

from bundlebees_plugin import contract, leases, return_report, tdd
from tests.test_return_report import _frozen


@pytest.mark.parametrize('role,attested', [('reviewer', True), ('human', True), ('human', False)])
def test_native_return_separates_test_coverage_from_recorded_review(tmp_path, role, attested):
    base, candidate = 'a' * 40, 'b' * 40
    test_file = 'tests/test_tool.py'
    draft, _ = _frozen(base, contract_id='TOOLS', statement='Tool returns 42', assigned='tools/tool.py',
                       test_file=test_file)
    del draft['review']
    draft['acceptance_criteria'].append(dict(id='AC-002', statement='Review the tool change', verification='review'))
    draft['evidence_required'].append('review')
    frozen = contract.freeze_contract(draft, approved_by='fixture', approved_at='2026-11-13T00:00:00Z')
    digest = contract.check_frozen(frozen)
    tasks = return_report._tasks(tmp_path, {'tasks': [dict(task_id='T1', contract=frozen, contract_sha256=digest,
                                                           attempts=['att_fixture'], state='verified')]})
    record = dict(attempt_id='att_fixture', sha=candidate, ok=True, tests=[test_file],
                  suite={'exit_code': 0}, gitleaks={'exit_code': 0})
    review = tdd.make_record(frozen, digest, phase='review', revision=candidate, test_fingerprints={},
                             command=['review'], result=None, produced_by={'role': role, 'name': 'fixture label'},
                             test_first=False, notes='Coordinator recorded the review outcome.', review_outcome='passed',
                             attestation=tdd.coordinator_review_attestation('fixture label', role) if attested else None)
    key = leases.evidence_key(tmp_path)
    path = tdd.write_record(tmp_path, review, key)
    assert tdd.verify_signature(tdd.load_records(tmp_path, 'TOOLS', key)[0], key)
    assert review['criteria']['AC-002'] == 'passed'
    test, reviewed = return_report._acceptance(tmp_path, tasks, {'att_fixture': record}, waves=[])
    assert (test['id'], test['status'], test['evidence']) == ('AC-001', 'PASS', ['verify-results/att_fixture.json'])
    assert (reviewed['id'], reviewed['status'], reviewed['evidence']) == (
        'AC-002', 'UNVERIFIED', [path.relative_to(tmp_path).as_posix()])
    assert 'independent reviewer' in reviewed['_limitation']
