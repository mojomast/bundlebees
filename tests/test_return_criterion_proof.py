"""Native RETURN: requested selectors, failed siblings and obsolete attempts aren't proof."""
import pytest
import hashlib
import json

from bundlebees_plugin import return_report as report
from bundlebees_plugin import rails_return
from bundlebees_plugin.rails_bundlebees_source import _alias


@pytest.mark.parametrize('cases,expected', [
    (None, 'UNVERIFIED'), ([], 'UNVERIFIED'), ([('test_a', 'passed')], 'UNVERIFIED'),
    ([('test_a', 'passed'), ('test_b', 'skipped')], 'UNVERIFIED'),
    ([('test_a', 'passed'), ('test_b', 'passed')], 'PASS'),
])
def test_regression_summary_only_proves_observed_cases(tmp_path, cases, expected):
    ids = ['test_impl.py::test_a', 'test_impl.py::test_b']
    suite = {'exit_code': 0}
    if cases is not None:
        suite['cases'] = [dict(id='test_impl.py::' + name, outcome=outcome) for name, outcome in cases]
    record = dict(sha='b'*40, attempt_id='att_new', tests=['test_impl.py'], suite=suite,
                  gitleaks={'exit_code': 0}, ok=True)
    row = dict(id='AC-001', tests=ids, verification='test')
    task = dict(task_id='T1', attempts=['att_new'], contract_ok=True, state='verified', task={})
    status, *_ = report._classify(row, task=task, record=record, record_problem='', signed={},
                                  waves=[], state_dir=tmp_path)
    assert status == expected


def test_overlapping_regression_selector_is_not_frozen_red_intent(tmp_path):
    # Native record has already been MAC-checked by the producer; these are
    # synthetic inputs to the link reducer, not an independent signed run.
    criteria = [dict(id='AC-001', verification='test', tests=['test_x.py::test_x']),
                dict(id='AC-002', verification='test', tests=['test_x.py::test_x'], regression=True)]
    task = dict(contract=dict(id='C', acceptance_criteria=criteria), attempts=[])
    path = '0001-red.json'
    raw = json.dumps(dict(criteria={ac['id']: 'failed' for ac in criteria}, test_fingerprints={})).encode()
    (tmp_path / path).write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    rid = _alias('record', path)
    chain = 'm-111111111111-00000000-0000-4000-8000-000000000001'
    h = dict(chain_id=chain, seq=1, hash='d'*64)
    eid = chain + ':1'
    scope = dict(job_id='job_a', task_id='C', attempt_id=None, repo='fixture/r', code_rev='a'*40, contract_sha256='c'*64)
    cfg = dict(scope, evidence={'AC-002':[eid]}, coordinator=h)
    e = dict(chain_id=chain, event_id=eid, scope=scope, kind='verification.result', authority='verifier_record',
             source=dict(adapter='bundlebees', record_id=rid, revision=sha),
             payload=dict(phase='red', verdict='EXPECTED_RED', ac_id='AC-002', code_rev='a'*40, contract_sha256='c'*64,
                          run_id=rid, evidence_refs=[rid], expected_intent_ref=_alias('intent', {})))
    manifest = dict(repository=dict(identity='fixture/r'), acceptance=[dict(id='AC-002', status='FAIL', evidence=[])],
                    red_green=dict(red=dict(phase='red', record_path=path, revision='a'*40, sha256=sha)))
    with pytest.raises(report.ReturnError, match='RED'):
        rails_return._document(cfg, task, [h], [e], [], '2026-11-17T00:00:00.000000Z', manifest, tmp_path)


@pytest.mark.parametrize('winning,verified,expected', [
    ('att_old', 'b'*40, 'NOT RUN'), ('att_new', 'd'*40, 'UNVERIFIED'), ('att_new', 'b'*40, 'PASS')])
def test_integrated_ac_requires_winning_attempt_and_candidate(tmp_path, winning, verified, expected):
    row = dict(id='AC-003', verification='integration')
    task = dict(task_id='T1', attempts=['att_old', 'att_new'], contract_ok=True)
    wave = dict(head='c'*40, attempts=[winning], wave_id='job.w1', verified_inputs={winning: verified})
    status, *_ = report._classify(row, task=task, record={'attempt_id': 'att_new', 'sha': 'b'*40},
                                  record_problem='', signed={}, waves=[wave], state_dir=tmp_path)
    assert status == expected
