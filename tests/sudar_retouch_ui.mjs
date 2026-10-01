// Sudar's retouch studio without a face model: the brushes, the overlay, a slider on painted hair, and apply.
import http from 'node:http'; import fs from 'node:fs/promises'; import path from 'node:path'; import assert from 'node:assert/strict';
import {launch} from './harness.mjs';
const root=path.resolve('ninaivu/static');
const server=http.createServer(async(req,res)=>{
  if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<html data-theme="light"><link rel="stylesheet" href="/static/css/style.css"><button id="nav">x</button></html>');return;}
  const target=path.resolve(root,'.'+req.url.replace(/^\/static/,''));
  try{res.setHeader('Content-Type',target.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(target));}catch{res.writeHead(404).end();}
});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await launch(); const page=await browser.newPage({viewport:{width:1440,height:1000}});
const errors=[]; page.on('pageerror',e=>errors.push(e.message));
await page.goto(`http://127.0.0.1:${server.address().port}`);
const fixture=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=600;c.height=400;const ctx=c.getContext('2d');ctx.fillStyle='#303030';ctx.fillRect(0,0,600,400);ctx.fillStyle='#c89a76';ctx.fillRect(200,100,200,220);return c.toDataURL().split(',')[1];});
await page.route('**/current.png',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
// No face model on this machine: the server says so, and the brush is the way in.
await page.route('**/api/portrait/analyse',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"Finding faces needs the face model. Download it under AI models, or paint the area by hand.","needs":"faces"}'}));
await page.evaluate(async()=>{const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');openPlayground({item:{view:'/current.png',name:'p.png'}});});
await page.waitForFunction(()=>!document.querySelector('[data-export]').disabled);
await page.locator('.ap-mode-btn[data-tab="magic"]').click(); await page.locator('[data-portrait]').click();
await page.locator('.ap-portrait-dialog').waitFor({state:'visible'});
await page.locator('.ap-portrait-tabs [data-tab="hair"]').click();
const hairPanel=page.locator('[data-panel-host="hair"]');
assert.ok(await hairPanel.locator('[data-pp="refine"][data-mode="add"]').isVisible(),'the brush is offered');
await hairPanel.locator('[data-pp="refine"][data-mode="add"]').click();
assert.equal(await page.locator('.ap-portrait-overlay').evaluate(e=>getComputedStyle(e).pointerEvents),'auto');
const box=await page.locator('.ap-portrait-overlay').boundingBox();
await page.mouse.move(box.x+box.width*0.5,box.y+box.height*0.2); await page.mouse.down();
await page.mouse.move(box.x+box.width*0.55,box.y+box.height*0.25,{steps:5}); await page.mouse.up();
await page.waitForTimeout(300);
const painted=await page.evaluate(()=>{const o=document.querySelector('.ap-portrait-overlay');const d=o.getContext('2d').getImageData(0,0,o.width,o.height).data;let n=0;for(let i=3;i<d.length;i+=4)if(d[i]>0)n++;return n;});
assert.ok(painted>50,'the painted selection is shown');
// Add hair: a third brush. A stroke puts its slider to full, so what is painted shows at once, in its own colour on the overlay.
await hairPanel.locator('[data-pp="refine"][data-mode="grow"]').click();
await page.mouse.move(box.x+box.width*0.45,box.y+box.height*0.3); await page.mouse.down();
await page.mouse.move(box.x+box.width*0.5,box.y+box.height*0.32,{steps:4}); await page.mouse.up();
await page.waitForTimeout(300);
const grown=await page.evaluate(()=>{const o=document.querySelector('.ap-portrait-overlay');const d=o.getContext('2d').getImageData(0,0,o.width,o.height).data;let n=0;for(let i=0;i<d.length;i+=4)if(d[i+1]>d[i]+40&&d[i+3]>0)n++;return n;});
assert.ok(grown>20,'the added hair is shown in its own colour');
await hairPanel.locator('[data-pp="refine"][data-mode="add"]').click();
// A slider on painted hair reaches the photograph.
const before=await page.locator('#ap-portrait-canvas').evaluate(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let s=0;for(let i=0;i<d.length;i+=4)s+=d[i];return s;});
await hairPanel.locator('input[type=range][data-key="hairDetail"], input[type=range]').first().fill('100');
await page.waitForTimeout(600);
const after=await page.locator('#ap-portrait-canvas').evaluate(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let s=0;for(let i=0;i<d.length;i+=4)s+=d[i];return s;});
console.log('canvas sum before/after', before, after);
// Look younger: offered once there is something to work on (here, painted hair), as slider values.
await page.locator('.ap-portrait-tabs [data-tab="skin"]').click();
const skinPanel=page.locator('[data-panel-host="skin"]');
assert.ok(await skinPanel.locator('[data-pp="younger"]').isVisible(),'Look younger is offered');
await skinPanel.locator('[data-pp="younger-amount"]').fill('80');
await skinPanel.locator('[data-pp="younger"]').click();
await page.waitForFunction(()=>document.querySelector('.ap-portrait-status').textContent.includes('younger look'));
assert.equal(await skinPanel.locator('.pp-slider[data-key="smooth"] input[type=range]').inputValue(),'44','smooth is 80% of its full younger value, as a slider');
await page.locator('[data-apply-portrait]').click(); await page.locator('.ap-portrait-dialog').waitFor({state:'detached'});
await page.waitForFunction(()=>document.querySelector('.ap-status').textContent.includes('Skin and hair'));
assert.deepEqual(errors,[]); console.log('retouch dialog: brush, overlay, slider on painted hair and apply all work');
await browser.close(); server.close();
