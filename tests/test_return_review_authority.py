"""A signed coordinator observation cannot authenticate an independent reviewer."""

import pytest

from bundlebees_plugin import contract, leases, return_report, tdd
from tests.test_return_report import _fixture_green, _frozen, _git, _make_repo


@pytest.mark.parametrize('role,attested', [('reviewer', True), ('human', True), ('human', False)])
def test_native_return_separates_test_coverage_from_recorded_review(tmp_path, role, attested):
    repo, base = _make_repo(tmp_path)
    (repo / 'smoke/answer.py').write_text('def answer():\n    return 42\n')
    _git(['add', 'smoke/answer.py'], repo)
    _git(['commit', '-qm', 'implement answer'], repo)
    candidate = _git(['rev-parse', 'HEAD'], repo)
    test_file = 'tests/test_smoke.py'
    draft, _ = _frozen(base, contract_id='TOOLS', statement='Tool returns 42', assigned='smoke/answer.py',
                       test_file=test_file, test_id=test_file + '::test_answer_is_42')
    del draft['review']
    draft['acceptance_criteria'].append(dict(id='AC-002', statement='Review the tool change', verification='review'))
    draft['evidence_required'].append('review')
    frozen = contract.freeze_contract(draft, approved_by='fixture', approved_at='2026-11-13T00:00:00Z')
    digest = contract.check_frozen(frozen)
    tasks = return_report._tasks(tmp_path, {'tasks': [dict(task_id='T1', contract=frozen, contract_sha256=digest,
                                                           attempts=['att_fixture'], state='verified')]})
    record = dict(attempt_id='att_fixture', sha=candidate, ok=True, tests=[test_file],
                  suite={'exit_code': 0},
                  gitleaks={'exit_code': 0})
    _fixture_green(tmp_path, repo, candidate, frozen)
    review = tdd.make_record(frozen, digest, phase='review', revision=candidate, test_fingerprints={},
                             command=['review'], result=None, produced_by={'role': role, 'name': 'fixture label'},
                             test_first=False, notes='Coordinator recorded the review outcome.', review_outcome='passed',
                             attestation=tdd.coordinator_review_attestation('fixture label', role) if attested else None)
    key = leases.evidence_key(tmp_path)
    path = tdd.write_record(tmp_path, review, key)
    assert tdd.verify_signature(tdd.load_records(tmp_path, 'TOOLS', key)[-1], key)
    assert review['criteria']['AC-002'] == 'passed'
    test, reviewed = return_report._acceptance(tmp_path, tasks, {'att_fixture': record}, waves=[])
    assert (test['id'], test['status'], test['evidence']) == (
        'AC-001', 'PASS', ['evidence/TOOLS/0001-green.json', 'verify-results/att_fixture.json'])
    assert (reviewed['id'], reviewed['status'], reviewed['evidence']) == (
        'AC-002', 'UNVERIFIED', [path.relative_to(tmp_path).as_posix()])
    assert 'independent reviewer' in reviewed['_limitation']
