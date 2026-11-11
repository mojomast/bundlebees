from pathlib import Path
import hashlib,json,shutil,zipfile,re
r=Path(__file__).resolve().parents[1];d=r/'delivery';d.mkdir(exist_ok=True)
report={'sourceCommit':'57a6f12cdcfa789c5aba5f22f3f46efd2b8b3d5c','filmBase':'bf5f1a7b4ef7ed661a892ac2450186081e238e4c','facts':{'tools':4,'o200kSchemaTokens':571,'testsPassed':150,'release':'v0.1.0','catalogPR':'NousResearch/hermes-agent#153888'},'score':json.loads((r/'assets/audio/mastering.json').read_text()),'files':[],'subjectiveListening':False}
for src,name,ver in [('out/bundlebees-1080p.mp4','bundlebees-1080p.mp4','master-verification.json'),('previews/bundlebees-720p.mp4','bundlebees-720p.mp4','preview-verification.json')]:
 j=json.loads((r/'out'/ver).read_text());p=r/src;assert p.stat().st_size==j['bytes'] and hashlib.sha256(p.read_bytes()).hexdigest()==j['sha256'];j['file']=name;report['files'].append(j);tmp=d/(name+'.tmp');shutil.copy2(p,tmp);tmp.replace(d/name)
for name in ['STORYBOARD.md','SCRIPT.md','REVIEW.md']:
 shutil.copy2(r/name,d/name)
for name in ['render-build','encoded-review','clean-build','playback']:
 report[name]=json.loads((r/'out'/f'{name}.json').read_text())
for name in ['poster.png','STORYBOARD.md','SCRIPT.md','REVIEW.md']:
 p=d/name;report['files'].append({'file':name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
report['preflight']={k:v for k,v in json.loads((r/'out/seek.json').read_text()).items() if k!='typography'}
report['textBoundsChecks']=json.loads((r/'out/seek.json').read_text())['typography']['checks']
(d/'verification.json').write_text(json.dumps(report,indent=2)+'\n')
files=['bundlebees-1080p.mp4','bundlebees-720p.mp4','poster.png','STORYBOARD.md','SCRIPT.md','REVIEW.md','verification.json','index.html']
for name in files:
 p=d/name
 if p.suffix in ['.md','.json','.html']:
  text=p.read_text();assert not re.search(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|[a-z0-9-]+\.ts\.net|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,}',text),name
with zipfile.ZipFile(d/'bundlebees-delivery.zip','w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
 for name in files:z.write(d/name,'bundlebees/'+name)
with zipfile.ZipFile(d/'bundlebees-delivery.zip') as z:assert z.testzip() is None
print(json.dumps({'archiveBytes':(d/'bundlebees-delivery.zip').stat().st_size,'files':len(files),'mediaHashesVerified':True},indent=2))
