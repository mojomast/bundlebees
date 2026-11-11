from pathlib import Path
import json,hashlib,re
r=Path(__file__).resolve().parents[1]
s=json.loads((r/'settings.json').read_text())
copy=json.loads((r/'copy.json').read_text())
facts=(r/'FACTS.md').read_text()
assert s['frames']==2160 and s['fps']==30 and s['duration']==72
last=0
for scene in s['scenes']:
    assert scene['start']==last and scene['end']>last
    last=scene['end']
    assert scene['id'] in copy
assert last==s['frames']
for a in json.loads((r/'assets/provenance.json').read_text())['assets']:
    assert hashlib.sha256((r/a['path']).read_bytes()).hexdigest()==a['sha256'],a['path']
secret=re.compile(r'(?:\d{1,3}\.){3}\d{1,3}|[a-z0-9-]+\.ts\.net|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,}|hf_[A-Za-z0-9]{20,}')
for p in list((r/'scenes').glob('*.js'))+[r/'copy.json',r/'FACTS.md',r/'SCRIPT.md',r/'STORYBOARD.md']:
    text=p.read_text()
    assert not secret.search(text),p
assert copy['proof']['tools']==['bundlebees_status','bundlebees_dispatch','bundlebees_attempt','bundlebees_host_prompt']
assert copy['proof']['budget']=='571 o200k tokens · measured'
assert copy['proof']['tests']=='150 tests passed'
assert '571' in facts and '150' in facts and '57a6f12' in facts and '153888' in facts
assert '571' in (r/'scenes'/'09-proof.js').read_text()
assert '150' in (r/'scenes'/'09-proof.js').read_text()
assert 'pane-health' in (r/'scenes'/'10-pane.js').read_text() or 'paneHealth' in (r/'shared'/'runtime.js').read_text()
assert copy['end']['url']=='github.com/mojomast/bundlebees'
assert '57a6f12' in copy['end']['version'] and '153888' in copy['end']['version']
assert copy['pane']['evidence']=='ACTUAL PANE · HEALTH'
print('PASS: exact timeline, measured 4/571/150 facts, original asset hashes, no private addresses/credential signatures in screen copy.')
