"""Offline rehearsal: native durable records and one private, disposable Git repo."""

import hashlib
import json
import sys

import pytest

from bundlebees_plugin import coverage, factory_driver, hive_docket, job, leases, queue, verify_local, work_scheduler
from tests.test_verify_local import make_patch, make_repo, sandboxed


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def manifest(root):
    return {p.relative_to(root): (p.stat().st_mode, p.stat().st_mtime_ns, sha(p.read_bytes()) if p.is_file() else '')
            for p in root.rglob('*')}


@pytest.fixture
def roots(tmp_path):
    state, pub = tmp_path / 'private-state', tmp_path / 'published'
    state.mkdir(mode=0o700)
    pub.mkdir(mode=0o700)
    queue.QueueStore(state).claim('F1', job_id='job_a')
    return state, pub


def stage(state, pub, **options):
    hive_docket.publish(state, pub, 'F1', 'job_a', **options)
    return json.loads((pub / 'queue.json').read_bytes())


def test_claim_only_does_not_create_optional_sources(roots):
    state, pub = roots
    before = manifest(state)
    doc = stage(state, pub)
    assert manifest(state) == before
    assert doc['subject'] == {'featureId': 'F1', 'jobId': 'job_a'}
    assert doc['queue']['state'] == 'claimed'
    assert doc['queue']['sha256'] == sha((state / 'queue/F1.json').read_bytes())
    assert doc['coverage']['state'] == 'UNKNOWN'
    assert doc['lease']['state'] == doc['scheduler']['state'] == 'unknown'
    old = (pub / 'queue.json').stat().st_ino
    stage(state, pub)
    assert (pub / 'queue.json').stat().st_ino != old
    assert (pub / 'queue.json').stat().st_mode & 0o777 == 0o600


def native_records(root, state, *, partial=True, merged=False):
    """Use actual coordinator writers; no secrets or records copied from a live job."""
    changes = {'pkg/newmod.py': 'def double(x): return x * 2\n', 'pkg/test_newmod.py':
               'import unittest\nfrom .newmod import double\nclass Named(unittest.TestCase):\n'
               '    def test_result(self): self.assertEqual(double(2), 4)\n'}
    files, attempt = sorted(changes), 'privateAttempt'
    repo, base = make_repo(root)
    patch = make_patch(repo, base, changes)
    later = ['future.py'] if partial else []
    identity = 'owner/repository'
    tasks = [{'task_id': 'first', 'lease': files}]
    if later:
        tasks.append({'task_id': 'second', 'lease': later})
    job.record_authorization(state, job_id='job_a', statement='private statement', repo=identity, base_rev=base,
                             scope=files + later, bees=2, wall_clock_s=600, interactive=False, require_tty=False)
    j = job.create_job(state, job_id='job_a', tasks=tasks)
    j['tasks'][0]['attempts'].append(attempt)
    for step in ('dispatching', 'dispatched', 'running', 'verifying'):
        job.transition(j, 'first', step, actor='coordinator')
    leases.record_lease(state, attempt, dict(source_rev=base, assigned_files=files, acceptance=['private criterion']))
    record = verify_local.verify_and_commit(state, repo, attempt, patch=patch, declared_files=files, base_rev=base,
                                            tests=['pkg.test_newmod'], suite_command=[sys.executable, '-m', 'unittest', '-q'],
                                            runner=sandboxed, require_gitleaks=False)
    head = record['sha']
    job.transition(j, 'first', 'verified', actor='coordinator', evidence=head)
    job.save_job(state, j)
    # Another claimed item intersects this one. Neither ID, lease path nor owner is public.
    work_scheduler.add_item(state, work_id='privateOther', lease_files=files, title='F1')
    work_scheduler.claim(state, 'privateOther', 'privateOwner')
    factory_driver.record_eligible_work(state, {'id': 'e1', 'data': {'attempt_id': attempt, 'job_id': 'job_a',
                                       'lease': {'assigned_files': files}}})
    task = coverage.Task.build(task_id='F1', repo=identity, assigned_files=files + later, contract=base)
    candidate = coverage.Candidate.build(repo=identity, number=3, head_sha=head, merged=merged, files=files)
    report = coverage.reconcile([task], [candidate])
    (state / 'coverage.json').write_text(json.dumps(report))
    # Simulated external PR metadata; no web request and no real PR claimed.
    (state / 'coverage-pr.json').write_text(json.dumps(dict(number=3, url=f'https://github.com/{identity}/pull/3',
        headRefOid=head, baseRefName='main', state='MERGED' if merged else 'OPEN', isDraft=not merged,
        mergedAt='2026-11-01T00:00:00Z' if merged else None)))
    return dict(head=head, candidate_state='merged' if merged else 'draft', pr=3)


def test_partial_native_and_cross_head_refusal(tmp_path, roots):
    state, pub = roots
    pins = native_records(tmp_path, state)
    before = manifest(state)
    doc = stage(state, pub, **pins)
    assert doc['coverage']['state'] == 'PARTIAL'
    assert (doc['coverage']['covered'], doc['coverage']['total']) == (2, 3)
    assert doc['lease']['state'] == 'recorded'
    assert doc['scheduler']['state'] == 'pending' and doc['scheduler']['overlap'] == 'recorded'
    raw = (pub / 'queue.json').read_bytes()
    assert len(raw) < 65536 and all(s not in raw for s in (b'private', b'owner/', b'future.py', b'test_newmod', b'http', b'pytest'))
    with pytest.raises(hive_docket.DocketError):
        stage(state, pub, **dict(pins, head='f'*40))
    assert (pub / 'queue.json').read_bytes() == raw and manifest(state) == before
    observed = state / 'coverage-pr.json'
    pr_record = json.loads(observed.read_bytes())
    for changes in ({'headRefOid': '2'*40}, {'state': 'CLOSED'}, {'isDraft': False}):
        observed.write_text(json.dumps(dict(pr_record, **changes)))
        with pytest.raises(hive_docket.DocketError):
            stage(state, pub, **pins)
        assert (pub / 'queue.json').read_bytes() == raw
    observed.write_text(json.dumps(pr_record))
    # Same-title/source or same repo basename is not a join, nor is an old verifier head.
    path = state / 'coverage.json'
    report = json.loads(path.read_bytes())
    for changes in ({'task_id': 'other', 'title': 'F1'}, {'repo': 'imposter/repository'},
                    {'draft_commit': '1'*40}, {'missing_files': []}, {'covered_by': 'owner/repository#4'}):
        changed = dict(report, records=[dict(report['records'][0], **changes)])
        path.write_text(json.dumps(changed))
        try:
            result = stage(state, pub, **pins)
        except hive_docket.DocketError:
            continue
        assert result['coverage']['state'] == 'UNKNOWN'


@pytest.mark.parametrize(('merged','expected'), [(False, 'COVERED_BY_DRAFT'), (True, 'LANDED')])
def test_full_classification_requires_posture_and_verifier(tmp_path, roots, merged, expected):
    state, pub = roots
    pins = native_records(tmp_path, state, partial=False, merged=merged)
    doc = stage(state, pub, **pins)
    assert doc['coverage']['state'] == expected
    assert doc['coverage']['covered'] == doc['coverage']['total'] == 2
    (state / 'verify-results/privateAttempt.json').unlink()
    assert stage(state, pub, **pins)['coverage']['state'] == 'UNKNOWN'


def test_bounds_selection_symlinks_and_private_dir(roots, tmp_path):
    state, pub = roots
    for feature, job_id in [('F1', 'other'), ('F2', 'job_a'), ('../F1', 'job_a'), ('F1', 'https://a.b')]:
        with pytest.raises((ValueError, OSError)):
            hive_docket.publish(state, pub, feature, job_id)
    for wrong in (state, state/'queue', tmp_path):
        with pytest.raises((ValueError, OSError)):
            stage(state, wrong)
    pub.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        stage(state, pub)
    pub.chmod(0o700)
    queue_path = state/'queue/F1.json'
    for raw in (b'{"feature_id":"F2","feature_id":"F1","job_id":"job_a"}', b' '*1048577):
        queue_path.write_bytes(raw)
        with pytest.raises((ValueError, OSError)):
            stage(state, pub)
    queue_path.unlink()
    queue_path.symlink_to(tmp_path/'elsewhere')
    with pytest.raises((ValueError, OSError)):
        stage(state, pub)
    assert not list(pub.iterdir())


def test_source_generation_change_refuses_old_projection(roots, monkeypatch):
    state, pub = roots
    stage(state, pub)
    old = (pub / 'queue.json').read_bytes()
    original = hive_docket.Snapshot.recheck

    def concurrent_write(snapshot):
        queue.QueueStore(state).write('F1', dict(job_id='job_a', state='running'))
        original(snapshot)

    monkeypatch.setattr(hive_docket.Snapshot, 'recheck', concurrent_write)
    with pytest.raises(hive_docket.DocketError):
        stage(state, pub)
    assert (pub / 'queue.json').read_bytes() == old and len(list(pub.iterdir())) == 1
