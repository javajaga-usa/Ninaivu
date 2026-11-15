// Sudar's face-safe hairstyles: the card, the marked hair, the mask that is sent,
// and a result whose face is the photograph's own even though the server
// changed every pixel it was given.
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

// A 600 × 400 picture: grey wall, a face, dark hair on top. The eyes are at
// (300, 170), 40 px apart; the maps say the same, as the server's analysis would.
const W=600,H=400,EX=300,EY=170,D=40;
const {fixture,maps}=await page.evaluate(({W,H,EX,EY,D})=>{
  const c=document.createElement('canvas');c.width=W;c.height=H;const x=c.getContext('2d');
  x.fillStyle='#707070';x.fillRect(0,0,W,H);
  x.fillStyle='#b98a66';x.beginPath();x.ellipse(EX,EY+0.45*D,1.05*D,1.45*D,0,0,7);x.fill();
  x.fillStyle='#1a1410';x.beginPath();x.ellipse(EX,EY-1.1*D,1.5*D,0.9*D,0,Math.PI,0);x.fill();
  const plane=fill=>{const p=document.createElement('canvas');p.width=W;p.height=H;const q=p.getContext('2d');q.fillStyle='#000';q.fillRect(0,0,W,H);fill(q);return p.toDataURL().split(',')[1];};
  const skin=(q,colour)=>{q.fillStyle=colour;q.beginPath();q.ellipse(EX,EY+0.45*D,1.05*D,1.45*D,0,0,7);q.fill();};
  const hair=(q,colour)=>{q.fillStyle=colour;q.beginPath();q.ellipse(EX,EY-1.1*D,1.5*D,0.9*D,0,Math.PI,0);q.fill();};
  return {fixture:c.toDataURL().split(',')[1],maps:{
    a:plane(q=>skin(q,'rgb(230,200,0)')),
    b:plane(q=>hair(q,'rgb(220,0,0)')),
    c:plane(()=>{}),
    ids:plane(q=>{skin(q,'rgb(1,0,0)');q.globalCompositeOperation='lighter';hair(q,'rgb(0,1,0)');}),
  }};
},{W,H,EX,EY,D});
await page.route('**/current.png',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
await page.route('**/api/ai-playground/capabilities',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({server_jobs:['inpaint'],local_jobs:[],enhance_tools:{}})}));
await page.route('**/api/portrait/analyse',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({maps,faces:[{id:1,usable:true,frame:{x:EX/W,y:EY/H,d:D/W,cos:1,sin:0}}]})}));
// The AI server: whatever it is sent, it paints the whole picture bright green.
let sent=null;
await page.route('**/api/ai-playground/server-jobs',async route=>{sent=JSON.parse(route.request().postData());await route.fulfill({contentType:'application/json',body:'{"id":"j1","status":{"state":"running"}}'});});
await page.route('**/api/ai-playground/server-jobs/j1',route=>route.fulfill({contentType:'application/json',body:'{"state":"done"}'}));
const green=await page.evaluate(({W,H})=>{const c=document.createElement('canvas');c.width=W;c.height=H;const x=c.getContext('2d');x.fillStyle='#00ff00';x.fillRect(0,0,W,H);return c.toDataURL().split(',')[1];},{W,H});
await page.route('**/api/ai-playground/server-jobs/j1/result',route=>route.fulfill({contentType:'image/png',body:Buffer.from(green,'base64')}));

await page.evaluate(async()=>{const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');openPlayground({item:{view:'/current.png',name:'p.png'}});});
await page.waitForFunction(()=>!document.querySelector('[data-export]').disabled);
await page.locator('.ap-mode-btn[data-tab="magic"]').click();
const card=page.locator('[data-hairstyle-card]');
assert.ok(await card.isVisible(),'the Hairstyles card is always on the Magic tools');
assert.ok(await page.locator('[data-hairstyle-needs]').isHidden(),'set up, it does not say what it needs');
assert.ok(await card.getByRole('button',{name:'Jasmine plait'}).isVisible(),'family styles are offered');
await card.getByRole('button',{name:'Bob'}).click();
const dialog=page.locator('.ap-remove-dialog');
await dialog.waitFor({state:'visible'});
assert.match(await dialog.locator('h2').textContent(),/Bob/);
assert.match(await dialog.locator('[data-ask]').inputValue(),/bob/i);
assert.ok(await dialog.locator('[data-apply]').isEnabled(),'the hair is marked already, so it can be drawn at once');

await dialog.locator('[data-apply]').click();
await page.waitForFunction(()=>!document.querySelector('.ap-remove-dialog [data-apply-photo]').disabled,null,{timeout:15000});
assert.equal(sent.kind,'inpaint');
// The mask that went to the server covers the hair and not the face.
const maskCheck=await page.evaluate(async({mask,EX,EY,D})=>{
  const img=new Image();img.src='data:image/png;base64,'+mask;await img.decode();
  const c=document.createElement('canvas');c.width=img.width;c.height=img.height;const x=c.getContext('2d');x.drawImage(img,0,0);
  const alpha=(px,py)=>{const s=img.width/600;return x.getImageData(Math.round(px*s),Math.round(py*s),1,1).data[3];};
  return {hair:alpha(EX,EY-1.5*D),eye:alpha(EX-0.5*D,EY),nose:alpha(EX,EY+0.6*D)};
},{mask:sent.mask,EX,EY,D});
assert.ok(maskCheck.hair>200,'the hair is sent to be redrawn');
assert.equal(maskCheck.eye,0,'the eyes are not sent');
assert.equal(maskCheck.nose,0,'the nose is not sent');

// Apply: the face is the photograph's own, the hair is what was drawn.
await dialog.locator('[data-apply-photo]').click();
await page.waitForFunction(()=>document.querySelector('.ap-status').textContent.includes('hairstyle'));
const result=await page.evaluate(async({EX,EY,D})=>{
  const img=document.querySelector('.ap-edited');await img.decode().catch(()=>{});
  const c=document.createElement('canvas');c.width=img.naturalWidth;c.height=img.naturalHeight;const x=c.getContext('2d');x.drawImage(img,0,0);
  const s=img.naturalWidth/600,at=(px,py)=>[...x.getImageData(Math.round(px*s),Math.round(py*s),1,1).data.slice(0,3)];
  return {eye:at(EX-0.5*D,EY),cheek:at(EX+0.5*D,EY+0.8*D),hair:at(EX,EY-1.5*D),far:at(20,380)};
},{EX,EY,D});
assert.deepEqual(result.eye,[0xb9,0x8a,0x66],'the face is exactly as it was');
assert.deepEqual(result.cheek,[0xb9,0x8a,0x66],'the face is exactly as it was');
assert.ok(result.hair[1]>200&&result.hair[0]<60,'the hair is the drawing');
assert.deepEqual(result.far,[0x70,0x70,0x70],'away from the hair the photograph is untouched');
assert.deepEqual(errors,[]); console.log('hairstyles: card, marked hair, mask sent without the face, face kept on apply');
await browser.close(); server.close();
