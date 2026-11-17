"""Replay persistent intake/result facts. A claim or timeout isn't an outcome."""
import hashlib
import json
import re
from pathlib import Path

from . import coordinator_actions as queue
from .rails_actions import KIND, Refused, action_id, public_id


class QueueSource:
    def __init__(self, state_dir):
        self.state = Path(state_dir)

    def describe(self):
        return dict(schema='rizzstack.rails-describe/1', adapter_id='rails-queue', version='1',
                    source_instance='coordinator-1', capabilities=['committed-intake-result'], supported_formats=[])

    def health(self):
        return dict(schema='rizzstack.rails-source-health/1', state='live' if self.state.is_dir() else 'unavailable',
                    last_success=None, error_code=None if self.state.is_dir() else 'source_unavailable')

    def _candidate(self, r, done):
        aid, p = r['action_id'], r['payload']
        if (aid != action_id(p['request_id']) or p['queue_action_id'] != aid or r['action_key'] != p['revision']
                or r['target'] != p['target_event_id'] or p['operation'] != 'retry'):
            raise Refused('source_invalid')
        part = 'result' if done else 'intake'
        payload = {key: public_id(p[key]) for key in ('authority_ref','operation','request_id','queue_action_id','target_event_id')}
        if done and r['result'] != {'decision':'approved', 'reason':'recovery_decision_recorded'}:
            raise Refused('source_invalid')
        payload.update(r['result'] if done else {'decision':'pending','reason':'owner_requested'})
        scope = {key:public_id(p[key]) for key in ('attempt_id','job_id','task_id','code_rev','contract_sha256')}
        scope['action_id'] = aid
        return dict(kind='intervention.result' if done else 'intervention.requested',
                    dedupe_key=f'{aid}:{part}', source=dict(adapter='rails-queue',instance='coordinator-1',
                                                           record_id=aid, revision=part),
                    payload=payload, scope=scope, lane={'lane_id':aid}, authority='coordinator_record',
                    occurred_at=r['updated_at' if done else 'created_at'].replace('Z','.000000Z'),
                    duration_ms=None, timing_quality='unknown', redaction_policy={'mode':'structured_safe'})

    def read_batch(self, cursor=None, limit=100, byte_limit=1048576):
        if type(limit) is not int or not 1 <= limit <= 500 or type(byte_limit) is not int or not 1 <= byte_limit <= 1048576:
            raise Refused('invalid_request')
        if cursor is not None and (not isinstance(cursor,str) or not re.fullmatch(r'[0-9a-f]{64}:[0-9]{1,5}',cursor)):
            raise Refused('invalid_cursor')
        root = queue.actions_dir(self.state)
        if not self.state.is_dir() or root.is_symlink():
            raise Refused('source_unavailable')
        facts = []
        for i, path in enumerate(root.glob('rails-*.json')):
            if i >= 2048:
                raise Refused('source_capacity')
            if not re.fullmatch(r'rails-[0-9a-f]{64}',path.stem):
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise Refused('source_invalid')
            r = queue.load(self.state,path.stem)
            if r is None or r['kind'] != KIND or r['status'] not in ('queued','claimed','done'):
                raise Refused('source_invalid')
            facts.append(self._candidate(r, False))
            if r['status'] == 'done':
                facts.append(self._candidate(r, True))
        facts.sort(key=lambda fact:fact['dedupe_key'])
        head = hashlib.sha256(json.dumps(facts,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        pos = int(cursor.split(':')[1]) if cursor and cursor.split(':')[0] == head else 0
        if pos > len(facts):
            raise Refused('invalid_cursor')
        out, used = [], 0
        for fact in facts[pos:pos+limit]:
            size = len(json.dumps(fact, separators=(',',':')).encode())
            if used + size > byte_limit:
                if not out:
                    raise Refused('too_large')
                break
            used += size
            out.append(fact)
        end = pos + len(out)
        return dict(schema='rizzstack.rails-read-batch-result/1',records=out,next_cursor=f'{head}:{end}',
                    has_more=end<len(facts),source_head=head,diagnostics=[])
