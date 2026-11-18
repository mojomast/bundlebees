"""Publish a single read-only coordinator observation. See docs/hive-docket.md."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import coverage, factory_driver, job, leases, work_scheduler


LIMIT = 1024 * 1024
ROWS = 256
PUBLIC_ID = r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}'
INTERNAL_ID = r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}'
SHA = r'[0-9a-f]{40}'
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
READ = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
FEATURE_STATES = ('claimed', 'running', 'ready_for_review', 'needs_approval', 'failed', 'blocked')


class DocketError(ValueError):
    """A refusal safe to show without disclosing state."""


def require(ok):
    if not ok:
        raise DocketError('docket_refused')


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def utc(value):
    if matches(value, r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z'):
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            return value
        except ValueError:
            pass
    return None


def decode(raw):
    # Narrowed from the bounded, duplicate-rejecting 07e02ba offline queue reader.
    def unique(pairs):
        result = dict(pairs)
        require(len(pairs) == len(result))
        return result

    doc = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=lambda _: require(False))
    require(isinstance(doc, dict))
    return doc


@contextmanager
def directory(path, root=None):
    path = Path(path)
    if root is None:
        path = path.absolute()
        fd = os.open('/', DIR)
        parts = path.parts[1:]
    else:
        require(not path.is_absolute())
        parts, fd = path.parts, os.dup(root)
    try:
        for part in parts:
            require(part not in ('.', '..'))
            child = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_ctime_ns, info.st_mtime_ns


class Snapshot:
    """Fixed/derived names only. Retain identities of both missing and present sources."""

    def __init__(self, root):
        self.root = root
        self.seen = {}
        self.observed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def recorded_at(self, value):
        if value is not None:
            # Both sides are canonical UTC seconds; equality is observable.
            require(utc(value) is not None and value <= self.observed_at)
        return value

    def raw(self, name, *, key=False):
        path = Path(name)
        limit = 32 if key else LIMIT
        try:
            with directory(path.parent, self.root) as parent:
                fd = os.open(path.name, READ, dir_fd=parent)
            with os.fdopen(fd, 'rb') as f:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit)
                if key:
                    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077 and info.st_size == 32)
                raw = f.read(limit + 1)
                require(len(raw) == info.st_size and stamp(os.fstat(fd)) == stamp(info))
                self.seen[name] = stamp(info)
                return raw
        except FileNotFoundError:
            self.seen[name] = None
            return None

    def record(self, name):
        raw = self.raw(name)
        return (None, None) if raw is None else (decode(raw), digest(raw))

    def recheck(self):
        for name, initial in self.seen.items():
            path = Path(name)
            try:
                with directory(path.parent, self.root) as parent:
                    current = stamp(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                current = None
            require(current == initial)


def rows(values):
    require(isinstance(values, list) and len(values) <= ROWS and all(isinstance(v, dict) for v in values))
    return values


def strings(values):
    require(isinstance(values, list) and len(values) <= ROWS and
            all(isinstance(v, str) and 0 < len(v) <= 1024 for v in values) and len(set(values)) == len(values))
    return values


def project_scheduler(source, doc, attempt):
    wid = factory_driver.work_id_for_event({'data': {'attempt_id': attempt}})
    mapping, _ = source.record(f'factory/work-jobs/{wid}.json')
    if mapping is None:
        return
    require(mapping.get('work_id') == wid and mapping.get('job_id') == doc['subject']['jobId'] and
            mapping.get('attempt_id') == attempt)
    group, sha = source.record('coordinator/work.json')
    if group is None:
        return
    require(group.get('schema') == work_scheduler.SCHEMA)
    items = rows(group.get('items'))
    strings([v.get('work_id') for v in items])
    selected = next((v for v in items if v['work_id'] == wid), None)
    if selected is None:
        return
    # Same stored file-key intersection as 07e02ba/project_work. Emit only its existence.
    require(all(v.get('status') in work_scheduler.STATUSES for v in items))
    files = set(strings(selected.get('lease_files')))
    others = [v for v in items if v is not selected and v['status'] == 'claimed']
    for item in [selected, *others]:
        source.recorded_at(item.get('created_at'))
        source.recorded_at(item.get('updated_at'))
    other_files = [strings(v.get('lease_files')) for v in others]
    doc['scheduler'] = dict(state=selected['status'], overlap='recorded' if any(files.intersection(v) for v in other_files)
                            else 'none', sha256=sha)


def checked_lease(source, key, attempt, task, base):
    record, sha = source.record(f'leases/{attempt}.json')
    if record is None:
        return None
    # load_lease reopens and reads an unbounded path. Use its canonical signing
    # primitives on the SAME bounded bytes that supply our provenance digest.
    mac = record.pop('mac', None)
    brief = record.get('brief')
    require(record.get('version') == 1 and record.get('attempt_id') == attempt and isinstance(brief, dict) and
            matches(mac, r'[0-9a-f]{64}') and hmac.compare_digest(mac, leases._sign(record, key)) and
            digest(leases._canonical(brief)) == record.get('brief_sha256') and
            record.get('source_rev') == brief.get('source_rev') == base and
            record.get('assigned_files') == brief.get('assigned_files') and
            set(strings(leases.lease_files(record))) == set(strings(task.get('lease'))))
    source.recorded_at(record.get('created_at'))
    return sha


def project_coverage(source, doc, goal, tasks, task, attempt, head, candidate_state, pr):
    report, report_sha = source.record('coverage.json')
    verifier, verify_sha = source.record(f'verify-results/{attempt}.json')
    capture, _ = source.record('coverage-pr.json')
    if report is None or verifier is None or capture is None:
        return
    require(report.get('schema') == coverage.SCHEMA)
    feature_id = doc['subject']['featureId']
    hits = [r for r in rows(report.get('records')) if r.get('task_id') == feature_id]
    if not hits:
        return
    require(len(hits) == 1)
    row = hits[0]
    kind = row.get('disposition')
    if kind not in (coverage.COVERED_BY_DRAFT, coverage.PARTIAL, coverage.LANDED):
        return  # MISSING is not proof of eligibility.
    repo, base = goal.get('repo'), goal.get('base_rev')
    require(matches(repo, r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+') and row.get('repo') == repo and
            row.get('contract') == base and task.get('state') in ('verified', 'integrating', 'accepted'))
    # #83's unmerged Candidate alone does not distinguish an open draft from a
    # closed PR. Require an independently obtained, retained PR observation.
    require(type(capture.get('number')) is int and capture['number'] == pr and
            capture.get('url') == f'https://github.com/{repo}/pull/{pr}' and capture.get('headRefOid') == head and
            capture.get('baseRefName') == 'main')
    if candidate_state == 'draft':
        require(capture.get('state') == 'OPEN' and capture.get('isDraft') is True and
                'mergedAt' in capture and capture['mergedAt'] is None)
    else:
        require(capture.get('state') == 'MERGED' and capture.get('isDraft') is False and
                source.recorded_at(capture.get('mergedAt')) is not None)
    require(verifier.get('ok') is True and verifier.get('mode') == 'commit-local' and verifier.get('sha') == head and
            verifier.get('attempt_id') == attempt and verifier.get('base_rev') == base)
    source.recorded_at(verifier.get('at'))
    files = strings(verifier.get('files'))
    require(set(files) == set(task['lease']))
    assigned = strings(row.get('assigned_files'))
    require(set(assigned) == {f for t in tasks for f in strings(t.get('lease'))})
    # Report labels and candidate posture alone cannot prove coverage. Reuse #83
    # on sealed job intent + verifier files, after the *full* repo/head/PR joins.
    classified = coverage.classify(coverage.Task.build(task_id=feature_id, repo=repo, assigned_files=assigned), [
        coverage.Candidate.build(repo=repo, number=pr, head_sha=head, merged=candidate_state == 'merged', files=files)])
    require(kind == classified['disposition'])
    if kind == coverage.LANDED:
        fields = ('landed_by', 'commit')
    elif kind == coverage.PARTIAL:
        require(candidate_state == 'draft')  # #83 does not encode partial-merged posture.
        fields = ('covered_by', 'draft_commit', 'covered_files', 'missing_files')
    else:
        require(row.get('requires_validation') is True)
        fields = ('covered_by', 'draft_commit')
    require(all(row.get(k) == classified[k] for k in fields))
    covered = len(set(assigned).intersection(files))
    require(0 < covered <= len(assigned) <= ROWS)
    doc['coverage'] = dict(state=kind, head=head, covered=covered, total=len(assigned), sha256=report_sha,
                           verificationSha256=verify_sha)


def collect(source, feature_id, job_id, head, candidate_state, pr):
    record, sha = source.record(f'queue/{feature_id}.json')
    require(record is not None and record.get('feature_id') == feature_id and record.get('job_id') == job_id)
    state = record.get('state')
    claimed_at = source.recorded_at(record.get('claimed_at'))
    updated_at = source.recorded_at(record.get('updated_at'))
    doc = dict(version='hive.queue/1', observedAt=source.observed_at, subject=dict(featureId=feature_id, jobId=job_id),
               queue=dict(state=state if state in FEATURE_STATES else 'unknown', sha256=sha,
                          updatedAt=updated_at or claimed_at),
               scheduler=dict(state='unknown', overlap='unknown', sha256=None), lease=dict(state='unknown', sha256=None),
               coverage=dict(state='UNKNOWN', sha256=None, head=None, covered=None, total=None, verificationSha256=None))
    envelope, _ = source.record(f'jobs/{job_id}.json')
    if envelope is None:
        return doc
    key = source.raw('lease.key', key=True)
    if key is None:
        return doc
    j = job.open_sealed(envelope, key, 'job')
    require(j.get('kind') == 'job' and j.get('job_id') == job_id and j.get('schema_version') == job.JOB_SCHEMA_VERSION)
    tasks = rows(j.get('tasks'))
    attempted = [t for t in tasks if strings(t.get('attempts'))]
    if len(attempted) != 1:
        return doc  # No unique current attempt for this feature. Never hunt for a convenient match.
    task = attempted[0]
    for event in rows(j.get('events', [])):
        if event.get('task_id') == task.get('task_id'):
            source.recorded_at(event.get('at'))
    attempt = task['attempts'][-1]
    require(matches(attempt, INTERNAL_ID) and '..' not in attempt)
    project_scheduler(source, doc, attempt)
    goal = j.get('goal')
    require(isinstance(goal, dict) and matches(goal.get('base_rev'), SHA))
    lease_sha = checked_lease(source, key, attempt, task, goal['base_rev'])
    if lease_sha is not None:
        doc['lease'] = dict(state='recorded', sha256=lease_sha)
        if head:
            project_coverage(source, doc, goal, tasks, task, attempt, head, candidate_state, pr)
    return doc


def publish(state_dir, publication_dir, feature_id, job_id, *, head=None, candidate_state=None, pr=None):
    require(matches(feature_id, PUBLIC_ID) and matches(job_id, PUBLIC_ID))
    require((head is None and pr is None and candidate_state is None) or
            (matches(head, SHA) and candidate_state in ('draft', 'merged') and type(pr) is int and 0 < pr < 10**9))
    state, pub = Path(state_dir).absolute(), Path(publication_dir).absolute()
    require(not state.is_relative_to(pub) and not pub.is_relative_to(state))
    with directory(state) as src, directory(pub) as dst:
        owner = os.geteuid()
        info = os.fstat(dst)
        private = os.fstat(src)
        require(info.st_uid == private.st_uid == owner and stat.S_IMODE(info.st_mode) == 0o700 and
                stat.S_IMODE(private.st_mode) == 0o700 and (private.st_dev, private.st_ino) != (info.st_dev, info.st_ino))
        snap = Snapshot(src)
        doc = collect(snap, feature_id, job_id, head, candidate_state, pr)
        payload = json.dumps(doc, sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'
        require(len(payload) <= 65536)
        tmp = f'.queue.{secrets.token_hex(16)}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dst)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            snap.recheck()
            os.replace(tmp, 'queue.json', src_dir_fd=dst, dst_dir_fd=dst)
            os.fsync(dst)
        finally:
            try:
                os.unlink(tmp, dir_fd=dst)
            except FileNotFoundError:
                pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('state-dir', 'publication-dir', 'feature-id', 'job-id'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--head')
    p.add_argument('--candidate-state', choices=('draft', 'merged'))
    p.add_argument('--pr', type=int)
    args = p.parse_args()
    try:
        publish(**vars(args))
    except (ValueError, TypeError, RecursionError, OSError, job.JobError, leases.LeaseError):
        p.exit(2, 'docket_refused\n')


if __name__ == '__main__':
    main()
