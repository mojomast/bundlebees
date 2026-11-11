import fs from 'node:fs';import path from 'node:path';import http from 'node:http';import {createHash} from 'node:crypto';import assert from 'node:assert/strict';import puppeteer from 'puppeteer-core';
const S=JSON.parse(fs.readFileSync('settings.json')),dir=fs.realpathSync('.build');
const html=fs.readFileSync('.build/index.html','utf8').replace('<script src="runtime.js"></script>','<script>window.__hyperframes={assetUrl:p=>p,registerFrameSource:()=>()=>{}};</script><script src="runtime.js"></script>');
const types={'.js':'text/javascript','.css':'text/css','.png':'image/png','.ttf':'font/ttf','.wav':'audio/wav'};
const server=http.createServer((req,res)=>{try{if(req.url==='/'){res.setHeader('Content-Type','text/html');return res.end(html)}const p=fs.realpathSync(path.resolve(dir,'.'+decodeURIComponent(new URL(req.url,'http://localhost').pathname)));if(!p.startsWith(dir+path.sep))throw Error();res.setHeader('Content-Type',types[path.extname(p)]||'application/octet-stream');fs.createReadStream(p).pipe(res)}catch{res.statusCode=404;res.end()}});
let browser;
try{
 await new Promise(r=>server.listen(0,'localhost',r));
 browser=await puppeteer.launch({executablePath:process.env.CHROME_BIN||process.env.PRODUCER_HEADLESS_SHELL_PATH,headless:true,args:['--disable-dev-shm-usage',...(process.getuid?.()===0?['--no-sandbox']:[])]});
 const page=await browser.newPage();await page.setViewport({width:S.width,height:S.height,deviceScaleFactor:1});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.evaluateOnNewDocument(()=>{window.bounds={checks:0,errors:[]};const original=CanvasRenderingContext2D.prototype.fillText;CanvasRenderingContext2D.prototype.fillText=function(s,x,y,...rest){if(this.globalAlpha>.001){const m=this.measureText(s),a=this.getTransform(),pts=[[x-m.actualBoundingBoxLeft,y-m.actualBoundingBoxAscent],[x+m.actualBoundingBoxRight,y+m.actualBoundingBoxDescent]].map(([x,y])=>({x:a.a*x+a.c*y+a.e,y:a.b*x+a.d*y+a.f}));window.bounds.checks++;if(pts.some(p=>p.x<48||p.x>1872||p.y<32||p.y>710))window.bounds.errors.push({s,font:this.font,pts})}return original.call(this,s,x,y,...rest)}});
 await page.goto(`http://localhost:${server.address().port}/`,{waitUntil:'load'});
 fs.mkdirSync('previews/stills',{recursive:true});fs.mkdirSync('out',{recursive:true});
 const seek=f=>page.evaluate(async f=>Film.test.seek(f),f);
 const capture=async f=>{await seek(f);return createHash('sha256').update(await page.screenshot()).digest('hex')};
 const lowerThird=async()=>page.evaluate(()=>{const c=[...document.querySelectorAll('section')].find(s=>s.style.visibility==='visible').querySelector('canvas'),g=c.getContext('2d'),p=g.getImageData(0,720,1920,360).data;for(let i=0;i<p.length;i+=4)if(p[i]!==9||p[i+1]!==15||p[i+2]!==19||p[i+3]!==255)return false;return true});
 if(process.argv.includes('--stills')){
  const times=process.argv.includes('--hero')?[4,54,69]:[1.5,4,9,14.5,18.5,23,29,33,36.5,38.5,43,49,54,61,69];
  for(const t of times){await seek(Math.round(t*30));assert.ok(await lowerThird(),`Caption area at ${t}s`);await page.screenshot({path:`previews/stills/${t}.png`})}
  const b=await page.evaluate(()=>window.bounds);fs.writeFileSync('out/still-bounds.json',JSON.stringify(b,null,2));assert.deepEqual(errors,[]);assert.deepEqual(b.errors,[]);console.log(`${times.length} stills; ${b.checks} typography checks, lower third clear.`);
 }else{
  const points=new Set([0,S.frames-1]);for(const s of S.scenes)for(const f of [s.start-1,s.start,s.start+1,s.start+15,Math.floor((s.start+s.end)/2)])if(f>=0&&f<S.frames)points.add(f);for(const t of [3,16,28,34,36,38,67])for(const d of [-1,0,1])points.add(Math.round(t*30)+d);
  const list=[...points].sort((a,b)=>a-b),hashes=new Map();
  for(const f of list){hashes.set(f,await capture(f));assert.ok(await lowerThird(),`Caption area at frame ${f}`)}
  for(const f of [...list].reverse())assert.equal(await capture(f),hashes.get(f));
  for(const f of [...list.filter((_,i)=>i%2),...list.filter((_,i)=>!(i%2))])assert.equal(await capture(f),hashes.get(f));
  for(const s of S.scenes.slice(0,-1))assert.notEqual(await capture(s.start+6),await capture(s.start+24));
  const original=await capture(60);await page.evaluate(()=>Film.test.setIntensity(0));assert.notEqual(await capture(60),original);await page.evaluate(()=>Film.test.setIntensity(1));
  const b=await page.evaluate(()=>window.bounds);assert.deepEqual(b.errors,[]);assert.deepEqual(errors,[]);
  fs.writeFileSync('out/seek.json',JSON.stringify({frames:list,reverseMatches:list.length,shuffledMatches:list.length,motionIntervals:10,effectSensitive:true,captionAreaClear:true,typography:b,pageErrors:errors,build:JSON.parse(fs.readFileSync('.build/build.json')),browser:await browser.version()},null,2));
  console.log(`${list.length} reverse + shuffled exact matches; ${b.checks} text bounds checks; caption area clear.`);
 }
}finally{if(browser)await browser.close();await new Promise(r=>server.close(r))}
