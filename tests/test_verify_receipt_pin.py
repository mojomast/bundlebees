"""Only an executed, scoped coordinator write can issue a private receipt pin."""

import hashlib

import pytest

from bundlebees_plugin import leases, verify_local
from tests.test_return_report import _frozen
from tests.test_verify_local import git, make_patch, make_repo


@pytest.fixture
def campaign(tmp_path):
    repo, base = make_repo(tmp_path)
    state = tmp_path / 'state'
    test = 'tests/test_existing.py'
    frozen, digest = _frozen(base, contract_id='TOOLS', assigned='pkg/existing.py',
                             statement='Update the value', test_file=test)
    patch = make_patch(repo, base, {'pkg/existing.py': 'VALUE = 2\n'})
    locks = []

    def dispatch(**overrides):
        brief = dict(job_id='job-pin', task_id='T1', source_rev=base, assigned_files=['pkg/existing.py'],
                     contract_sha256=digest, contract=frozen, acceptance={'tests': [test]})
        return leases.record_lease(state, 'att_1', dict(brief, **overrides))

    def run(preflight):
        # Local synthetic runner, no child executes worker code.
        def check(cwd, argv):
            return dict(exit_code=int((cwd/'pkg/existing.py').read_text() == 'VALUE = 1\n'), timed_out=False)

        return verify_local.verify_and_commit(state, repo, 'att_1', patch=patch, base_rev=base,
            declared_files=['pkg/existing.py'], tests=[test], suite_command=['test'], require_gitleaks=False,
            acdd_preflight=lambda files: preflight, runner=check, on_receipt_written=locks.append)

    return state, repo, base, dispatch, run, dict(contract=frozen, sha=digest), locks


def test_pin_names_written_file_and_scope_once(campaign):
    state, repo, base, dispatch, run, preflight, pins = campaign
    dispatch()
    rec = run(preflight)
    content = verify_local.record_path(state, 'att_1').read_bytes()
    assert pins == [verify_local.ReceiptPin('job-pin', 'T1', preflight['sha'], 'att_1', base,
                                           rec['sha'], hashlib.sha256(content).hexdigest())]
    assert git(repo, 'rev-parse', 'bb-verify/att_1').stdout.strip() == pins[0].commit
    assert run(preflight)['sha'] == rec['sha']
    assert len(pins) == 1  # no pin minted by rediscovering the file


@pytest.mark.parametrize('fault', ['legacy_lease', 'foreign_contract', 'unfrozen_contract', 'missing_job'])
def test_unscoped_or_foreign_receipt_does_not_issue_a_fingerprint(campaign, fault):
    state, _, _, dispatch, run, preflight, pins = campaign
    fields = {'legacy_lease': {'contract_sha256': None}, 'foreign_contract': {'contract_sha256': 'a'*64},
              'missing_job': {'job_id': None}}.get(fault, {})
    dispatch(**fields)
    if fault == 'unfrozen_contract':
        preflight['contract']['title'] = 'changed after freeze'
    with pytest.raises(verify_local.VerifyError) as refused:
        run(preflight)
    assert refused.value.code == 'receipt_scope_mismatch'
    assert pins == [] and not verify_local.record_path(state, 'att_1').exists()
