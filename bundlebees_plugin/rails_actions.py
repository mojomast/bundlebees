"""Owner-only handoff to the existing create-only recovery decision writer.

The queue is the outbox. Its immutable intake and its terminal result survive a
crash before audit publication. No work is dispatched here. A retry decision
is consumed later by the normal reconciler, under its existing gates.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from . import coordinator_actions as queue, job, reconcile, recovery

KIND = 'rails_retry'


class Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def public_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,190}', value):
        raise Refused('invalid_request')
    return value


def action_id(request_id):
    # One logical handoff per recovery request, independent of click/event ID.
    return 'rails-' + digest(public_id(request_id))


class RetryHandoff:
    def __init__(self, state_dir, reader, *, authorize=None):
        self.state, self.reader, self.authorize = Path(state_dir), reader, authorize

    def _owner(self, target, request_id):
        if self.authorize is None:
            raise Refused('capability_unavailable')
        if self.authorize(public_id(target), public_id(request_id), 'retry') is not True:
            raise Refused('forbidden')

    def _target(self, target):
        rows = self.reader.query('SELECT envelope_json AS doc FROM event_rows WHERE event_id=? '
                                 'UNION ALL SELECT origin_bytes FROM imported_origin_rows WHERE event_id=? LIMIT 2',
                                 (public_id(target), target))
        if len(rows) != 1:
            raise Refused('not_found')
        return json.loads(rows[0]['doc'])['scope']

    def _describe(self, target, request_id):
        scope = self._target(target)
        try:
            # load_request/outstanding may label legacy records on disk. A
            # preview must not migrate anything, even when the request is bad.
            located = recovery._locate_request(self.state, public_id(request_id))
            if located is None:
                raise Refused('not_found')
            path, request = located
            if recovery.legacy_reasons(request, 'request'):
                raise Refused('unsupported_source_version')
            n = recovery._ordinal_from_path(path)
            jid, tid = public_id(request['job_id']), public_id(request['task_id'])
            if (request_id != recovery.request_id_for(jid, tid, n) or
                    path != recovery.request_path(self.state, jid, tid, n)):
                raise Refused('target_mismatch')
            if (recovery.decision_path(self.state, jid, tid, n).exists() or
                    any(recovery._ordinal_from_path(other) > n for other in path.parent.glob(f'{tid}.*.request.json'))):
                raise Refused('stale_preview')
            recovery._admit_request(self.state, request)
            j, task = recovery._load_task(self.state, request)
            auth = job.load_authorization(self.state, j['job_id'])
            job._enforce_narrowing(j, auth)
            if (auth.get('kind') != 'authorization' or auth.get('authority', 'human') != 'human'
                    or auth['auth_id'] != j['authorization']['auth_id']):
                raise Refused('forbidden')
            if (scope.get('attempt_id') != request['attempt_id'] or
                    any(scope.get(key) not in (None, request[key]) for key in ('job_id','task_id')) or
                    scope.get('contract_sha256') not in (None, request['contract']['sha256']) or
                    scope.get('code_rev') not in (None, j['goal']['base_rev'])):
                raise Refused('target_mismatch')
            if (task['state'] != 'rejected' or task['attempts'][-1:] != [request['attempt_id']] or
                    task.get('contract_sha256') != request['contract']['sha256']):
                raise Refused('stale_preview')
            body = recovery._decision_body(request, {'decision':'retry', 'rationale':action_id(request_id)}, 'rails-owner', 0)
            recovery._check_decision(self.state, request, body, job=j, task=task)
            recovery._admit_decision(self.state, request, body, j, task)
        except (recovery.RecoveryError, job.JobError) as exc:
            raise Refused(exc.code) from None
        view = dict(target_event_id=target, request_id=request_id, operation='retry',
                    queue_action_id=action_id(request_id), authority_ref=public_id(auth['auth_id']),
                    expected_fence=0, job_id=public_id(j['job_id']), task_id=public_id(task['task_id']),
                    attempt_id=public_id(request['attempt_id']), contract_sha256=request['contract']['sha256'],
                    code_rev=j['goal']['base_rev'], scope_count=len(task['lease']), scope_digest=digest(sorted(task['lease'])),
                    remaining_attempts=recovery.remaining_attempts(request, task),
                    workflow_ref=recovery._PROTOCOL['commit'])
        view['revision'] = digest([view, request['request_digest'], auth['recovery_digest'], job.job_digest(j)])
        return view

    def preview(self, target_event_id, request_id, operation='retry'):
        self._owner(target_event_id, request_id)
        if operation != 'retry':
            raise Refused('capability_unavailable')
        if queue.load(self.state, action_id(request_id)) is not None:
            raise Refused('already_requested')
        return self._describe(target_event_id, request_id)

    def submit(self, target_event_id, request_id, revision, operation='retry'):
        self._owner(target_event_id, request_id)
        if operation != 'retry':
            raise Refused('capability_unavailable')
        aid = action_id(request_id)
        prior = queue.load(self.state, aid)
        if prior:
            if (prior['kind'] != KIND or prior['action_key'] != revision or prior['target'] != target_event_id):
                raise Refused('idempotency_conflict')
        else:
            view = self._describe(target_event_id, request_id)
            if revision != view['revision']:
                raise Refused('stale_preview')
            queue.enqueue(self.state, aid, action_key=revision, kind=KIND, target=target_event_id,
                          workflow_ref=view['workflow_ref'], payload=view)
        return {'queue_action_id': aid, 'request_id': request_id}

    def _load(self, aid):
        if not re.fullmatch(r'rails-[0-9a-f]{64}', public_id(aid)):
            raise Refused('invalid_request')
        r = queue.load(self.state, aid)
        if not r or r['kind'] != KIND or action_id(r['payload']['request_id']) != aid:
            raise Refused('not_found')
        return r

    def _revalidate(self, record):
        view = self._describe(record['target'], record['payload']['request_id'])
        if view != record['payload'] or record['action_key'] != view['revision']:
            raise Refused('stale_preview')

    def claim(self, aid, *, now=None):
        r = self._load(aid)
        self._owner(r['target'], r['payload']['request_id'])
        if r['status'] != 'queued':
            raise Refused('reconcile_required')
        self._revalidate(r)
        return queue.claim(self.state, aid, 'rails-owner', now=now)['fence_seq']

    def commit(self, aid, fence):
        return self._finish(aid, fence, reconcile_only=False)

    def reconcile(self, aid, fence):
        return self._finish(aid, fence, reconcile_only=True)

    def _finish(self, aid, fence, *, reconcile_only):
        check = self._load(aid)
        self._owner(check['target'], check['payload']['request_id'])
        # Serialize the actual decision write with queue adoption; checking
        # a fence before releasing the lock would leave a stale-owner window.
        with queue._locked(self.state):
            r = self._load(aid)
            self._owner(r['target'], r['payload']['request_id'])
            if type(fence) is not int or fence != r['fence_seq']:
                raise Refused('stale_fence')
            if r['status'] == 'done':
                return r['result']
            if r['status'] != 'claimed':
                raise Refused('not_claimed')
            lock = reconcile._acquire_lock(self.state, r['payload']['job_id'], time.time())
            if not lock:
                raise Refused('job_busy')
            try:
                result = self._decision(r, reconcile_only or bool(r.get('adopted_from')))
            finally:
                lock.release()
        # Adoption in the gap can only move the fence: the create-only receipt
        # is already durable. The next holder reconciles that exact receipt.
        queue.complete(self.state, aid, fence, result=result)
        return result

    def _decision(self, r, reconcile_only):
        p = r['payload']
        try:
            req = recovery.load_request(self.state, p['request_id'])
            old = recovery.read_decision(self.state, req)
            expected = recovery._decision_body(req, {'decision':'retry', 'rationale':r['action_id'],
                                                    'decided_at':r['created_at']}, 'rails-owner', 0)
            if old is None:
                if reconcile_only:
                    raise Refused('reconcile_required')
                self._revalidate(r)
                self._owner(r['target'], p['request_id'])
                recovery.record_decision(self.state, req['request_id'], expected, adapter='rails-owner')
            elif old != expected:
                raise Refused('decision_conflict')
        except (recovery.RecoveryError, job.JobError) as exc:
            raise Refused(exc.code) from None
        return {'decision':'approved', 'reason':'recovery_decision_recorded'}
