from pathlib import Path
import urllib.request,hashlib,json,shutil
r=Path(__file__).resolve().parents[1]; (r/'assets/fonts').mkdir(parents=True,exist_ok=True);(r/'assets/audio').mkdir(exist_ok=True);(r/'assets/images').mkdir(exist_ok=True)
base='https://raw.githubusercontent.com/google/fonts/main/'
files=[('ofl/spacegrotesk/SpaceGrotesk[wght].ttf','Grotesk.ttf'),('ofl/spacegrotesk/OFL.txt','spacegrotesk-OFL.txt'),('ofl/ibmplexmono/IBMPlexMono-Medium.ttf','Mono.ttf'),('ofl/ibmplexmono/OFL.txt','ibmplexmono-OFL.txt')]
items=[]
for src,dst in files:
 url=base+src.replace('[','%5B').replace(']','%5D');p=r/'assets/fonts'/dst
 with urllib.request.urlopen(url,timeout=30) as q:p.write_bytes(q.read())
 items.append({'path':str(p.relative_to(r)),'url':url,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'license':'OFL-1.1'})
shutil.copy2(r.parent/'LICENSE',r/'assets/repository-MIT.txt')
(r/'assets/provenance.json').write_text(json.dumps({'sourceCommit':'bf5f1a7b4ef7ed661a892ac2450186081e238e4c','repository':'https://github.com/mojomast/bundlebees','assets':items},indent=2)+'\n')
print('Fonts and original licenses imported.')
