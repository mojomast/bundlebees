from pathlib import Path
import json,hashlib,re
r=Path(__file__).resolve().parents[1];s=json.loads((r/'settings.json').read_text());copy=json.loads((r/'copy.json').read_text());assert s['frames']==2160 and s['fps']==30 and s['duration']==72
last=0
for scene in s['scenes']:
 assert scene['start']==last and scene['end']>last;last=scene['end'];assert scene['id'] in copy
assert last==s['frames']
for a in json.loads((r/'assets/provenance.json').read_text())['assets']:assert hashlib.sha256((r/a['path']).read_bytes()).hexdigest()==a['sha256'],a['path']
for p in list((r/'scenes').glob('*.js'))+[r/'copy.json']:
 text=p.read_text()
 assert not re.search(r'''["'](?:~)?(?:571|142)(?:["' ]|$)''',text),p
 assert not re.search(r'(?:\d{1,3}\.){3}\d{1,3}|[a-z0-9-]+\.ts\.net|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,}',text),p
assert copy['proof']['tools']==['bundlebees_status','bundlebees_dispatch','bundlebees_attempt','bundlebees_host_prompt']
readme=(r.parent/'README.md').read_text();assert '**900**' in readme
for name in copy['proof']['tools']:assert name in readme
assert copy['end']['url']=='github.com/mojomast/bundlebees'
print('PASS: exact timeline, 4 documented tools, budget qualification, original asset hashes, no private addresses/credential signatures in screen copy.')
