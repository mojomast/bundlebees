import fs from 'node:fs';
import http from 'node:http';
import {createHash} from 'node:crypto';
import {build} from 'esbuild';
import puppeteer from 'puppeteer-core';
fs.mkdirSync('.cache/pane',{recursive:true});
fs.mkdirSync('assets/images',{recursive:true});
await build({entryPoints:['scripts/pane-fixture.jsx'],bundle:true,format:'iife',outfile:'.cache/pane/app.js',plugins:[{name:'local-sdk',setup(b){
 b.onResolve({filter:/^@hermes\/plugin-sdk$/},()=>({path:'sdk',namespace:'fixture'}));
 b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'import React from "react"; export {React}; export default {React};',resolveDir:process.cwd()}));
}}]});
const server=http.createServer((req,res)=>{
 res.setHeader('Content-Type',req.url==='/app.js'?'text/javascript':'text/html');
 res.end(req.url==='/app.js'?fs.readFileSync('.cache/pane/app.js'):'<!doctype html><html><body style="margin:0;background:#0b0f14"><div id="root"></div><script src="app.js"></script></body></html>');
});
let browser;
try{
 await new Promise(r=>server.listen(0,'localhost',r));
 browser=await puppeteer.launch({executablePath:process.env.CHROME_BIN,headless:true,args:['--disable-dev-shm-usage',...(process.getuid?.()===0?['--no-sandbox']:[])]});
 const page=await browser.newPage();
 await page.setViewport({width:720,height:520,deviceScaleFactor:3});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`http://localhost:${server.address().port}`);
 await page.waitForSelector('.bb-pane');
 await page.evaluate(()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='Models').click());
 await page.waitForFunction(()=>document.body.innerText.includes('deepseek-v4.1-flash'));
 await page.screenshot({path:'.cache/pane/full-pane.png'});
 const card=await page.$('.bb-main .bb-card');
 await card.screenshot({path:'assets/images/pane-models.png'});
 const box=await card.boundingBox();
 if(errors.length)throw Error(errors.join('\n'));
 const m=JSON.parse(fs.readFileSync('assets/provenance.json'));
 m.assets=m.assets.filter(x=>x.path!=='assets/images/pane-models.png');
 m.assets.push({path:'assets/images/pane-models.png',url:'https://github.com/mojomast/bundlebees/blob/bf5f1a7b4ef7ed661a892ac2450186081e238e4c/desktop/plugin.js',sha256:createHash('sha256').update(fs.readFileSync('assets/images/pane-models.png')).digest('hex'),cssBox:box,deviceScaleFactor:3,license:'MIT',capture:'Actual unmodified pane component and CSS via local React SDK fixture; Models card cropped at device scale 3 for legibility. Model ID from docs/RESEARCH.md; no launcher connection or result metrics.',browser:await browser.version()});
 fs.writeFileSync('assets/provenance.json',JSON.stringify(m,null,2)+'\n');
 console.log('Captured actual Models card at 3×; no live worker data.');
}finally{
 if(browser)await browser.close();
 await new Promise(r=>server.close(r));
}
