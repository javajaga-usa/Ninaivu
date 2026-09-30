import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {launch} from './harness.mjs';
const root=path.resolve('ninaivu/static');
const template=await fs.readFile('ninaivu/templates/index.html','utf8');
const markup=template.match(/<div class="viewer"[\s\S]*?<div class="filmstrip" id="filmstrip"><\/div>\s*<\/div>/)[0];
const server=http.createServer(async(req,res)=>{
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');res.end(`<html data-theme="light"><link rel="stylesheet" href="/css/style.css">${markup}<script type="module" src="/boot.js"></script></html>`);return;
  }
  if(req.url==='/boot.js'){
    res.setHeader('Content-Type','text/javascript');res.end(`import {Viewer} from '/js/viewer.js';
      const c=document.createElement('canvas');c.width=600;c.height=400;const ctx=c.getContext('2d');ctx.fillStyle='#536c48';ctx.fillRect(0,0,600,400);
      window.viewer=new Viewer(document.querySelector('#viewer'));viewer.canDownload=true;viewer.canRotate=true;
      viewer.fetchItem=async id=>({id,kind:'picture',ext:'.png',name:'Photo '+id,src:c.toDataURL(),download:c.toDataURL(),width:600,height:400,playable:true,tags:[]});
      await viewer.open([1,2,3],0,new Set());window.ready=true;`);return;
  }
  const target=path.resolve(root,'.'+req.url);
  if(!target.startsWith(root+path.sep)){res.writeHead(404).end();return;}
  try{res.setHeader('Content-Type',target.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(target));}catch{res.writeHead(404).end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));let browser;
try{
  browser=await launch({channel:'msedge'});
  for(const width of [1440,390]){
    const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);await page.waitForFunction(()=>window.ready);
    // The settings are not in the details panel: they are asked for when
    // Slideshow is pressed, and nothing plays until Start.
    await page.locator('#v-info').click();
    assert.equal(await page.locator('#viewer-info select').count(),0);
    await page.locator('#v-info-close').click();
    await page.clock.install();
    if(await page.locator('#v-more').isVisible()) await page.locator('#v-more').click();
    await page.locator('#v-slideshow').click();
    assert.equal(await page.locator('#slide-pop').isVisible(),true);
    assert.notEqual(await page.locator('#v-slideshow').getAttribute('aria-pressed'),'true','it must not play before it has been asked');
    await page.locator('#v-slide-delay').selectOption('8000');await page.locator('#v-slide-loop').uncheck();
    assert.deepEqual(await page.evaluate(()=>JSON.parse(localStorage.getItem('mv.slideshow'))),{delay:8000,loop:false});
    await page.locator('#v-slide-start').click();
    assert.equal(await page.locator('#slide-pop').isVisible(),false);
    assert.equal(await page.locator('#v-slideshow').getAttribute('aria-pressed'),'true');
    await page.clock.runFor(7900);assert.equal(await page.evaluate(()=>viewer.index),0);
    await page.clock.runFor(100);await page.waitForFunction(()=>viewer.index===1);
    await page.clock.runFor(16000);await page.waitForFunction(()=>viewer.slideshow===null);assert.equal(await page.evaluate(()=>viewer.index),2);
    assert.equal(await page.locator('#v-slideshow').getAttribute('aria-pressed'),'false');
    await page.reload();await page.waitForFunction(()=>window.ready);
    if(await page.locator('#v-more').isVisible()) await page.locator('#v-more').click();
    await page.locator('#v-slideshow').click();
    assert.equal(await page.locator('#v-slide-delay').inputValue(),'8000');assert.equal(await page.locator('#v-slide-loop').isChecked(),false);
    assert.deepEqual(errors,[]);await page.close();
  }
  console.log('Desktop/mobile slideshow settings, timing, end behavior, accessible state and persistence passed.');
}finally{await browser?.close();await new Promise(r=>server.close(r));}
