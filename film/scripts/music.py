"""HIVE MIND — original 120 BPM electronic composition; CC0; no samples/voice."""
from pathlib import Path
import numpy as np,wave,json,subprocess
r=Path(__file__).resolve().parents[1];out=r/'assets/audio';out.mkdir(exist_ok=True);SR=48000;D=72;N=D*SR;rng=np.random.default_rng(3612026);mix=np.zeros((N,2),np.float32)
def clock(d):return np.arange(round(d*SR),dtype=np.float32)/SR
def add(a,start,gain,pan=0):
 s=round(start*SR);e=min(N,s+len(a));p=(pan+1)*np.pi/4
 if s>=0 and e>s:mix[s:e,0]+=a[:e-s]*gain*np.cos(p);mix[s:e,1]+=a[:e-s]*gain*np.sin(p)
def hz(n):return 440*2**((n-69)/12)
def bell(n,d=.8):
 x=clock(d);f=hz(n);body=np.sin(2*np.pi*f*x+1.7*np.sin(2*np.pi*f*2*x)*np.exp(-x*8));return body*np.minimum(1,x/.008)*np.exp(-x*5)*np.minimum(1,(d-x)/.06)
def bass(n,d=.42):
 x=clock(d);f=hz(n);return (np.sin(2*np.pi*f*x)+.23*np.sin(4*np.pi*f*x))*np.minimum(1,x/.015)*np.exp(-x*2.5)*np.minimum(1,(d-x)/.06)
def pad(n,d=3.9):
 x=clock(d);f=hz(n);return (np.sin(2*np.pi*f*x)+.24*np.sin(2*np.pi*f*1.003*x)+.12*np.sin(2*np.pi*f*.998*x))*np.minimum(1,x/.3)*np.minimum(1,(d-x)/.7)
def kick():
 x=clock(.4);return np.sin(2*np.pi*(43*x+3.2*(1-np.exp(-x*32))))*np.exp(-x*12)*np.minimum(1,x/.003)
def snare():
 x=clock(.18);noise=rng.normal(0,.15,len(x));return (noise+.14*np.sin(2*np.pi*175*x))*np.exp(-x*23)*np.minimum(1,x/.003)
def hat(d=.065):
 x=clock(d);noise=rng.normal(0,.1,len(x));return np.diff(noise,prepend=0)*np.exp(-x*70)*np.minimum(1,x/.002)
chords=[[40,47,54,59],[36,43,50,55],[43,50,57,62],[38,45,52,57]]
for phrase in range(17):
 start=phrase*4;ch=chords[phrase%4];level=.7 if start<12 else 1
 for i,n in enumerate(ch[1:]):add(pad(n),start,.029*level,(i-1)*.6)
for bar in range(33):
 s=bar*2;ch=chords[(bar//2)%4];energy=.45 if s<6 else (.65 if s<12 else (.82 if s<20 else 1))
 if 26<=s<32 or 40<=s<52:energy=.67
 melody=[ch[2]+12,ch[1]+12,ch[3]+12,ch[2]+12]
 for j,n in enumerate(melody):
  start=s+j*.5;v=.062*energy;add(bell(n,.8),start,v,(-1)**j*.45);add(bell(n,.8),start+.375,v*.18,(-1)**(j+1)*.5)
 if s>=6:
  for b in [0,.75,1.5]:add(bass(ch[0]-12),s+b,.21*energy,-.08)
 if s>=12:
  for beat in range(4):
   if not (40<=s<52 and beat%2):add(kick(),s+beat*.5,.38*energy)
  for beat in [1,3]:add(snare(),s+beat*.5,.22*energy,.05)
  for j in range(8):add(hat(),s+j*.25,.08*energy,(-1)**j*.7)
 if 20<=s<26 or 32<=s<40 or 52<=s<64:
  for j,n in enumerate([ch[3]+12,ch[2]+12,ch[1]+24,ch[2]+12]):add(bell(n,.5),s+.25+j*.5,.035,(-1)**j*.3)
hits=[0,6,12,20,26,32,40,46,52,58,65]
for s in hits:
 x=clock(.12);add(rng.normal(0,.07,len(x))*np.exp(-x*38)*np.minimum(1,x/.002),s,.3);add(bell(88,.25),s,.022,.4)
for s in [11.5,19,31.5,51.5,64.5]:
 d=1 if s==19 else .5;x=clock(d);noise=rng.normal(0,.045,len(x));add(noise*np.linspace(0,1,len(x))**2*np.minimum(1,(d-x)/.02),s,.7,.2)
# A descending dissonance marks the illustrated rejection, then returns to key.
for j,n in enumerate([74,72,71]):add(bell(n,.6),28+j*.25,.035,-.25)
add(kick(),65,.3)
for i,n in enumerate([40,47,54,59,64]):add(pad(n,6.6),65,.055,(i-2)*.3)
for j,n in enumerate([83,78,76,71]):add(bell(n,1.5),65+j*.5,.06,(-1)**j*.35)
fade=np.minimum(1,np.arange(N)/SR/.01)*np.minimum(1,(D-np.arange(N)/SR)/1.4);mix*=fade[:,None]
with wave.open(str(out/'raw.wav'),'wb') as w:w.setnchannels(2);w.setsampwidth(2);w.setframerate(SR);w.writeframes((np.clip(mix,-1,1)*32767).astype('<i2').tobytes())
base=['ffmpeg','-hide_banner','-nostdin','-threads','2','-filter_threads','2']
def measure(p):
 q=subprocess.run(base+['-i',str(p),'-af','loudnorm=I=-15:TP=-3:LRA=9:print_format=json','-threads','2','-f','null','-'],capture_output=True,text=True,check=True).stderr;return json.JSONDecoder().raw_decode(q[q.rfind('{'):])[0]
j=measure(out/'raw.wav');f=f"loudnorm=I=-15:TP=-3:LRA=9:measured_I={j['input_i']}:measured_TP={j['input_tp']}:measured_LRA={j['input_lra']}:measured_thresh={j['input_thresh']}:offset={j['target_offset']}:linear=false"
subprocess.run(base+['-y','-loglevel','error','-i',str(out/'raw.wav'),'-af',f,'-ar','48000','-c:a','pcm_s16le',str(out/'hive-mind.wav')],check=True);(out/'raw.wav').unlink();j=measure(out/'hive-mind.wav');gain=-15-float(j['input_i']);assert float(j['input_tp'])+gain<=-1.8
subprocess.run(base+['-y','-loglevel','error','-i',str(out/'hive-mind.wav'),'-af',f'volume={gain}dB','-c:a','pcm_s16le',str(out/'trim.wav')],check=True);(out/'trim.wav').replace(out/'hive-mind.wav')
report={'title':'HIVE MIND','seed':3612026,'bpm':120,'downbeatSeconds':0,'liftSeconds':20,'hitsSeconds':hits,'license':'CC0-1.0','pcm':measure(out/'hive-mind.wav')};(out/'mastering.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
