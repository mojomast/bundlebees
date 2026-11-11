from pathlib import Path
import subprocess,json,shutil
import numpy as np
from PIL import Image,ImageDraw
r=Path(__file__).resolve().parents[1];src=r/'out/bundlebees-1080p.mp4';settings=json.loads((r/'settings.json').read_text());folder=r/'previews/encoded';folder.mkdir(parents=True,exist_ok=True);(r/'delivery').mkdir(exist_ok=True)
times=[1.5,4,9,14.5,18.5,23,29,33,36.5,38.5,43,49,54,61,69]
cuts=[s['start'] for s in settings['scenes'][1:]]+[90,480,840,1020,1080,1140,2010]
frames=set(round(t*30) for t in times)
for f in cuts:frames.update([f-1,f,f+1])
for s in settings['scenes'][:-1]:frames.update([s['start']+6,s['start']+24])
frames.update([2040,2130]);frames=sorted(frames)
selection='+'.join(f'eq(n\\,{n})' for n in frames)
cmd=['ffmpeg','-y','-hide_banner','-loglevel','error','-nostdin','-threads','2','-filter_threads','2','-filter_complex_threads','2','-i',str(src),'-vf',f'select={selection}','-fps_mode','vfr','-threads','2',str(folder/'capture-%03d.png')]
subprocess.run(cmd,check=True)
for i,f in enumerate(frames,1):(folder/f'capture-{i:03d}.png').replace(folder/f'{f}.png')
def read(f):return np.array(Image.open(folder/f'{f}.png').convert('RGB'))
motion=[]
for s in settings['scenes'][:-1]:
 a=read(s['start']+6);b=read(s['start']+24);changed=int(np.count_nonzero(a!=b));assert changed>500,s;motion.append({'scene':s['id'],'changedChannels':changed})
def sheet(name,grid,labels,cols,w):
 h=round(w*1080/1920);gap=24;rows=(len(grid)+cols-1)//cols;im=Image.new('RGB',(cols*w,rows*(h+gap)),(9,15,19));d=ImageDraw.Draw(im)
 for i,f in enumerate(grid):
  x=(i%cols)*w;y=(i//cols)*(h+gap);im.paste(Image.open(folder/f'{f}.png').convert('RGB').resize((w,h),Image.Resampling.LANCZOS),(x,y));d.text((x+8,y+h+4),labels[i],fill=(255,190,79))
 im.save(r/'previews'/name,quality=92)
sheet('encoded-contact-sheet.jpg',[round(t*30) for t in times],[f'{t}s' for t in times],3,480)
cutframes=[f+d for f in sorted(cuts) for d in [-1,0,1]]
sheet('cut-boundaries.jpg',cutframes,[f'{f}/2160' for f in cutframes],3,384)
poster=Image.open(folder/'2070.png').convert('RGB').resize((1280,720),Image.Resampling.LANCZOS);poster.save(r/'delivery/poster.png');assert poster.size==(1280,720)
# Compression can shift flat RGB values slightly; spatial range catches intrusion.
safe=[]
for f in frames:
 lower=read(f)[730:,:,:];span=int(np.max(np.ptp(lower.reshape(-1,3).astype('int16'),axis=0)));assert span<=5,(f,span);safe.append(span)
end=float(np.mean(np.abs(read(2040).astype('float32')-read(2130).astype('float32'))))
report={'source':src.name,'stills':times,'cutFrames':sorted(cuts),'motion':motion,'lowerThirdMaxSpatialRange':max(safe),'endHoldMeanDifference':end,'poster':{'file':'delivery/poster.png','width':1280,'height':720,'frame':2070},'passed':True}
(r/'out/encoded-review.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
