"""Coordinator-local, one-request forced-command adapter. No listener or issuer.

The relay's login is not owner authority. A detached SSH signature over the
exact review must match an independently enrolled workspace *and* the approver
of a genuine coordinator-sealed job. Only a quality annotation is admitted.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from uuid import UUID

from . import job
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'contrib'))
from rizzstack_rails import RailsLedger, contracts, feedback

NAMESPACE='rizzstack-rails-feedback-v1'
ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,191}')
OWNER=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
FIELDS={'workspace_id','target_event_id','idempotency_key','category','rating','note'}

class Refusal(Exception): pass

def check(ok,code='forbidden'):
    if not ok:raise Refusal(code)

def private(path,cap):
    check(isinstance(path,(str,Path)) and Path(path).is_absolute(),'capability_unavailable')
    try:
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as f:
            s=os.fstat(f.fileno());check(stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and not s.st_mode&0o077,'capability_unavailable')
            data=f.read(cap+1);check(len(data)<=cap,'capability_unavailable');return data
    except OSError:raise Refusal('capability_unavailable') from None

def canonical(o):return json.dumps(o,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)

def uuid(value):
    try:return isinstance(value,str) and str(UUID(value))==value
    except ValueError:return False

def permitted(config):
    p=json.loads(private(config,4096));check(set(p)=={'workspace_id','owner','ledger_uuid','ledger','state_dir','signers'},'capability_unavailable')
    check(uuid(p['workspace_id']) and uuid(p['ledger_uuid']) and OWNER.fullmatch(p['owner']),'capability_unavailable')
    check(all(Path(p[k]).is_absolute() for k in ('ledger','state_dir','signers')),'capability_unavailable')
    # No broadening a generic host login, wildcard principal, unrestricted key,
    # or another workspace's enrollment into permission to record feedback.
    identity=p['workspace_id']+':'+p['owner']
    pattern=re.escape(identity)+r' namespaces="'+NAMESPACE+r'" ssh-ed25519 [A-Za-z0-9+/]{60,128}={0,2}'
    allowed=private(p['signers'],16384).decode('ascii').splitlines()
    matches=[line for line in allowed if re.fullmatch(pattern,line)]
    check(len(matches)==1,'forbidden');return p,identity,matches[0]

def subject(p,db,draft):
    check(isinstance(draft,dict) and set(draft)==FIELDS,'invalid_request');check(draft['workspace_id']==p['workspace_id'])
    check(uuid(draft['idempotency_key']) and draft['category']=='quality' and type(draft['rating']) is int and
          draft['rating'] in (-1,0,1) and draft['note'] in ('','[redacted]'),'invalid_request')
    target=draft['target_event_id'];check(isinstance(target,str) and ID.fullmatch(target),'invalid_request')
    check(db.ledger_uuid==p['ledger_uuid']);rows=db.query('SELECT envelope_json AS doc FROM event_rows WHERE event_id=? '
        'UNION ALL SELECT origin_bytes AS doc FROM imported_origin_rows WHERE event_id=? LIMIT 2',(target,target))
    check(len(rows)==1,'not_found');e=json.loads(rows[0]['doc']);digest=e.pop('hash')
    check(e['event_id']==target and digest==contracts.rails_hash(e['prev_hash'],e),'source_fork')
    jid=e['scope'].get('job_id');check(isinstance(jid,str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,95}',jid))
    scope={'job_id':jid}
    for k in ('task_id','attempt_id','session_id','campaign_id','contract_sha256','code_rev'):
        v=e['scope'].get(k)
        if isinstance(v,str) and ID.fullmatch(v):scope[k]=v
    return digest,scope

def review(p,db,draft,expires):
    digest,scope=subject(p,db,draft);jid=scope['job_id']
    try:auth=job.load_authorization(Path(p['state_dir']),jid)
    except job.JobError:raise Refusal('forbidden') from None
    check(auth['kind']=='authorization' and auth.get('authority','human')=='human' and auth['by']==p['owner'])
    fields=dict(schema=NAMESPACE,owner=p['owner'],ledger_uuid=db.ledger_uuid,job_id=jid,authorization_id=auth['auth_id'],
                authorization_digest=hashlib.sha256(canonical(auth).encode()).hexdigest(),target_hash=digest,expires_at=expires,**draft)
    # The final newline is required, making this directly signable as a text file.
    return canonical(fields)+'\n',scope

def recover(db,draft,scope,provenance):
    """Match the entire original owner record on a read-only connection."""
    target = draft['target_event_id']
    key = draft['idempotency_key']
    source = feedback._source_for(feedback._feedback_id_for(target, key), 1, 'record', provenance)
    payload = dict(target_event_id=target, category='quality', rating=draft['rating'], note=draft['note'])
    body = contracts.event_body(feedback.FEEDBACK_KIND, source, scope, payload, 'owner_record',
                                None, None, None, 'unknown', None)
    body.update(chain_id=db.chain_id, dedupe_key='feedback:' + key)
    rows = db.query('SELECT e.event_id, e.envelope_json FROM dedupe_index d JOIN event_rows e USING(event_id) '
                    'WHERE d.chain_id=? AND d.dedupe_key=?', (db.chain_id, body['dedupe_key']))
    check(len(rows) == 1)
    event = json.loads(rows[0]['envelope_json'])
    digest = event.pop('hash')
    check(event['event_id'] == rows[0]['event_id'] and digest == contracts.rails_hash(event['prev_hash'], event)
          and contracts.semantic_digest(event) == contracts.semantic_digest(body))
    return {'event_id': event['event_id'], 'duplicated': True}

def verify(signers,identity,reviewed,signature):
    check(isinstance(signature,str) and len(signature)<=2048 and signature.startswith('-----BEGIN SSH SIGNATURE-----\n'))
    with tempfile.TemporaryDirectory() as tmp:
        pub=Path(tmp)/'allowed';pub.write_text(signers+'\n');sig=Path(tmp)/'signed';sig.write_text(signature)
        result=subprocess.run(['/usr/bin/ssh-keygen','-Y','verify','-f',str(pub),'-I',identity,'-n',NAMESPACE,'-s',str(sig)],
              input=reviewed.encode('ascii'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=4,
              env={'PATH':'/usr/bin:/bin','HOME':tmp,'LC_ALL':'C'});check(result.returncode==0)

def handle(config,req):
    action=req.get('action');check(action in ('preview','confirm'),'capability_unavailable')
    p,identity,signers=permitted(config);now=int(time.time())
    if action=='preview':
        check(set(req)=={'action','draft'},'invalid_request')
        with contextlib.closing(RailsLedger(p['ledger'],read_only=True)) as db:
            return {'review':review(p,db,req['draft'],now+300)[0]}
    check(set(req)=={'action','review','signature'} and isinstance(req['review'],str) and len(req['review'])<=3072,'invalid_request')
    signed=json.loads(req['review']);expires=signed['expires_at'];check(type(expires) is int and expires<=now+300)
    draft={k:signed[k] for k in FIELDS}
    def admit(db):
        text,scope=review(p,db,draft,expires);check(req['review']==text,'conflict');return scope
    with contextlib.closing(RailsLedger(p['ledger'],read_only=True)) as db:
        text=req['review'];verify(signers,identity,text,req['signature'])
        provenance=dict(adapter='rails-owner-feedback',instance=p['owner'],record_id=hashlib.sha256(text.encode('ascii')).hexdigest(),revision='v1')
        if expires<=int(time.time()):
            digest,scope=subject(p,db,draft)
            check(signed['schema']==NAMESPACE and signed['target_hash']==digest and signed['ledger_uuid']==db.ledger_uuid and
                  signed['owner']==p['owner'] and signed['job_id']==scope['job_id'])
            return recover(db,draft,scope,provenance)
        scope=admit(db)
    with contextlib.closing(RailsLedger(p['ledger'],allowed_authorities={'owner_record'})) as writer:
        check(writer.ledger_uuid==p['ledger_uuid'])
        def authorize_new_record():
            # Bind the already verified signature to the still-enrolled key and
            # exact sealed authorization under the writer lock. Sample time last.
            check(permitted(config) == (p, identity, signers))
            admit(writer)
            now = int(time.time())
            check(now < expires <= now + 300)
        receipt=feedback.record(writer,draft['target_event_id'],scope=scope,idempotency_key=draft['idempotency_key'],
            rating=draft['rating'],category='quality',note=draft['note'],source=provenance,authorize_new_record=authorize_new_record)
        return {'event_id':receipt['event_id'],'duplicated':receipt['duplicated']}

def main():
    try:
        check(len(sys.argv)==2,'capability_unavailable');raw=sys.stdin.buffer.read(8193);check(len(raw)<=8192,'too_large')
        out={'ok':True,**handle(sys.argv[1],json.loads(raw.decode('utf8')))}
    except Refusal as exc:out={'ok':False,'code':str(exc)}
    except contracts.RailsRefusal as exc:out={'ok':False,'code':'conflict' if exc.code in ('feedback_conflict','idempotency_conflict') else 'source_unavailable'}
    except Exception:out={'ok':False,'code':'source_unavailable'}
    sys.stdout.write(canonical(out)+'\n')

if __name__=='__main__':main()
