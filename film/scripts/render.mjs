import fs from 'node:fs';import path from 'node:path';import os from 'node:os';import {execFileSync} from 'node:child_process';
const root=process.cwd(),S=JSON.parse(fs.readFileSync('settings.json')),run=(p,a)=>execFileSync(p,a,{stdio:'inherit'});
fs.mkdirSync('out',{recursive:true});fs.mkdirSync('previews',{recursive:true});
const locks=['out/render.lock',path.join(fs.existsSync('/tmp/opencode')?'/tmp/opencode':os.tmpdir(),'hyperframes-production-render.lock')],owned=[];
try{
 for(const lock of locks){const fd=fs.openSync(lock,'wx');owned.push({lock,fd});fs.writeFileSync(fd,JSON.stringify({pid:process.pid,project:'bundlebees-showcase'}))}
 run(process.execPath,['scripts/build.mjs']);run('node_modules/.bin/hyperframes',['lint','.build']);
 process.env.HYPERFRAMES_FFMPEG_PATH=path.join(root,'scripts/ffmpeg-bounded.py');fs.chmodSync(process.env.HYPERFRAMES_FFMPEG_PATH,0o755);
 const fingerprint=fs.readFileSync('.build/build.json','utf8');
 const args=['node_modules/.bin/hyperframes','render','.build','--fps','30','--quality','delivery','--workers','2','--strict','--no-best-effort','--output','out/picture-candidate.mp4'];
 if(process.platform==='linux'){
  const allowed=fs.readFileSync('/proc/self/status','utf8').match(/^Cpus_allowed_list:\s*(.+)$/m)[1];
  const cpus=allowed.split(',').flatMap(p=>{const[a,b=a]=p.split('-').map(Number);return Array.from({length:b-a+1},(_,i)=>a+i)}).slice(0,2).join(',');
  run('nice',['-n','19','taskset','-c',cpus,...args]);
 }else run(args[0],args.slice(1));
 const ff=['-y','-hide_banner','-nostdin','-threads','2','-filter_threads','2','-filter_complex_threads','2'];
 run('nice',['-n','19','ffmpeg',...ff,'-i','out/picture-candidate.mp4','-i',S.audio,'-map','0:v','-map','1:a','-c:v','libx264','-preset','medium','-crf','18','-maxrate','6000k','-bufsize','12000k','-pix_fmt','yuv420p','-threads','2','-c:a','aac','-b:a','192k','-t',String(S.duration),'-movflags','+faststart','out/master-candidate.mp4']);
 run('nice',['-n','19','ffmpeg',...ff,'-i','out/master-candidate.mp4','-vf','scale=1280:720:flags=lanczos','-c:v','libx264','-crf','22','-preset','medium','-threads','2','-c:a','aac','-b:a','160k','-movflags','+faststart','out/preview-candidate.mp4']);
 for(const [file,w,h,report] of [['out/master-candidate.mp4',1920,1080,'out/master-verification.json'],['out/preview-candidate.mp4',1280,720,'out/preview-verification.json']])
  run('nice',['-n','19','python3','scripts/verify-media.py',file,'--width',String(w),'--height',String(h),'--fps','30','--duration',String(S.duration),'--audio','required','--target-lufs','-15','--max-true-peak','-1','--max-bytes','49000000','--report',report]);
 if(fs.readFileSync('.build/build.json','utf8')!==fingerprint)throw Error('Build mutated during render');
 fs.renameSync('out/master-candidate.mp4','out/bundlebees-1080p.mp4');fs.renameSync('out/preview-candidate.mp4','previews/bundlebees-720p.mp4');fs.writeFileSync('out/render-build.json',fingerprint);
 console.log('Verified and published bundlebees master + preview.');
}finally{for(const {lock,fd} of owned.reverse()){fs.closeSync(fd);fs.unlinkSync(lock)}}
