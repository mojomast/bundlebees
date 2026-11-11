import fs from 'node:fs';import http from 'node:http';import path from 'node:path';import assert from 'node:assert/strict';import puppeteer from 'puppeteer-core';
const file=path.resolve('previews/bundlebees-720p.mp4'),size=fs.statSync(file).size;
const html='<!doctype html><html><body style="margin:0;background:#090f13"><video controls width="1280" height="720" src="/film.mp4"></video><button>Play</button><script>document.querySelector("button").onclick=()=>document.querySelector("video").play()</script></body></html>';
const server=http.createServer((req,res)=>{if(req.url==='/'){res.setHeader('Content-Type','text/html');return res.end(html)}if(req.url!=='/film.mp4'){res.statusCode=404;return res.end()}const match=/bytes=(\d+)-(\d*)/.exec(req.headers.range||''),lo=match?Number(match[1]):0,hi=match?Math.min(size-1,match[2]?Number(match[2]):lo+4*1024*1024-1):size-1;res.writeHead(match?206:200,{'Content-Type':'video/mp4','Content-Length':hi-lo+1,'Accept-Ranges':'bytes',...(match?{'Content-Range':`bytes ${lo}-${hi}/${size}`}:{})});fs.createReadStream(file,{start:lo,end:hi}).pipe(res)});let browser;
try{
 await new Promise(r=>server.listen(0,'localhost',r));
 browser=await puppeteer.launch({executablePath:process.env.CHROME_BIN||process.env.PRODUCER_HEADLESS_SHELL_PATH,headless:true,args:['--disable-dev-shm-usage',...(process.getuid?.()===0?['--no-sandbox']:[])]});
 const page=await browser.newPage();await page.setViewport({width:1360,height:850});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://localhost:${server.address().port}/`);await page.evaluate(()=>{window.waits=0;document.querySelector('video').addEventListener('waiting',()=>window.waits++)});await page.click('button');await page.waitForFunction(()=>document.querySelector('video').currentTime>0);await page.waitForFunction(()=>document.querySelector('video').ended,{timeout:100000});
 const result=await page.$eval('video',v=>({duration:v.duration,ended:v.ended,muted:v.muted,error:v.error?.message||null,frames:v.getVideoPlaybackQuality().totalVideoFrames,dropped:v.getVideoPlaybackQuality().droppedVideoFrames,waitingEvents:window.waits}));
 assert.equal(result.frames,2160);assert.ok(Math.abs(result.duration-72)<.1);assert.ok(result.ended&&!result.muted);assert.equal(result.error,null);assert.deepEqual(errors,[]);
 await page.$eval('video',v=>{v.currentTime=46});await page.waitForFunction(()=>!document.querySelector('video').seeking&&Math.abs(document.querySelector('video').currentTime-46)<.1);
 fs.writeFileSync('out/playback.json',JSON.stringify({passed:true,...result,seek46Passed:true,pageErrors:errors,subjectiveListening:false},null,2));console.log(result);
}finally{if(browser)await browser.close();await new Promise(r=>server.close(r))}
