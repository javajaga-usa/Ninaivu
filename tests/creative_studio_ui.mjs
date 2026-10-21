// Exercise every creative mode and its exported artifact with synthetic photos.
import os from 'node:os';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {launch} from './harness.mjs';
// The creations are cards, the creation's photographs a row of prints.
const pickMode=(page,mode)=>page.locator(`#creative-studio [data-mode="${mode}"]`).click();
const pickPhoto=(page,i)=>page.locator('#creative-studio .cs-thumb').nth(i).click();
const photoCount=page=>page.locator('#creative-studio .cs-thumb').count();
// Waits on something the test's own routes saw.
async function until(check,what){for(let i=0;i<100;i++){if(check())return;await new Promise(r=>setTimeout(r,50));}throw new Error(`never happened: ${what}`);}
const root=path.resolve('ninaivu/static');
const server=http.createServer(async(req,res)=>{
  if(req.url==='/'){res.setHeader('Content-Type','text/html');res.setHeader('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'");res.end('<html><link rel="stylesheet" href="/static/css/style.css"><button id="open">Open</button></html>');return;}
  const file=path.resolve(root,'.'+req.url.replace(/^\/static/,''));
  if(!file.startsWith(root+path.sep)){res.writeHead(404).end();return;}
  try{res.setHeader('Content-Type',file.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(file));}catch{res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try{
  // Was `channel:'msedge'`: a browser this machine need not have.
  browser=await launch();
  for(const mobile of [false,true]){
    const page=await browser.newPage({viewport:mobile?{width:390,height:844}:{width:1440,height:1000}}),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.addInitScript(()=>{window.cspErrors=[];document.addEventListener('securitypolicyviolation',e=>window.cspErrors.push(e.violatedDirective));});
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.evaluate(async()=>{
      const c=document.createElement('canvas');c.width=600;c.height=400;const x=c.getContext('2d');x.fillStyle='#df2929';x.fillRect(0,0,300,400);x.fillStyle='#176de0';x.fillRect(300,0,300,400);
      const blob=await new Promise(r=>c.toBlob(r));window.fixture=c.toDataURL();
      const {openCreativeStudio}=await import('/static/js/creative-studio.js');
      await openCreativeStudio({original:blob,edited:blob,returnFocus:document.querySelector('button')});
    });
    const fixture=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=600;c.height=400;const x=c.getContext('2d');x.fillStyle='green';x.fillRect(0,0,600,400);return c.toDataURL().split(',')[1];});
    await pickMode(page,'compare');assert.ok(await page.locator('[data-export]').isDisabled());
    await page.locator('[data-files]').setInputFiles({name:'Second.png',mimeType:'image/png',buffer:Buffer.from(fixture,'base64')});
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===2);
    await page.locator('[data-caption]').fill('A family memory');
    await page.locator('[data-title]').fill('Family <script>alert(1)</script>');
    await page.locator('[data-story]').fill('A story with dates, food and wonderful people.');
    await pickMode(page,'album');
    const wav=Buffer.alloc(1644);wav.write('RIFF');wav.writeUInt32LE(1636,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(1600,40);
    await page.locator('[data-audio]').setInputFiles({name:'memory.wav',mimeType:'audio/wav',buffer:wav});
    await page.waitForFunction(()=>document.querySelector('audio').readyState>=1);
    assert.match(await page.locator('audio').getAttribute('src'),/^blob:/);
    await page.locator('[data-clear-audio]').click();assert.ok(await page.locator('audio').isHidden());
    await page.evaluate(()=>{window.RealReader=window.FileReader;window.FileReader=class extends window.RealReader{readAsDataURL(blob){setTimeout(()=>super.readAsDataURL(blob),100);}};});
    await page.locator('[data-audio]').setInputFiles({name:'memory.wav',mimeType:'audio/wav',buffer:wav});await page.locator('[data-clear-audio]').click();
    await page.waitForTimeout(180);assert.ok(await page.locator('audio').isHidden());await page.evaluate(()=>window.FileReader=window.RealReader);
    for(const mode of ['restoration','album','postcard','collage','compare','slideshow','selective','versions','recipe','calendar']){
      await pickMode(page,mode);
      if(mode==='recipe'){await page.locator('[data-ingredients]').fill('2 cups flour\n1 cup milk');await page.locator('[data-directions]').fill('Mix. Bake. Share.');}
      if(mode==='calendar'){await page.locator('[data-month]').fill('2026-09');await page.locator('[data-events]').fill('12 — Birthday');}
      if(mode==='selective'){
        const pixel=()=>page.locator('canvas').evaluate(c=>[...c.getContext('2d').getImageData(720,540,1,1).data]);
        const before=await pixel();assert.equal(before[0],before[1]);await page.locator('canvas').focus();await page.keyboard.press('Space');const after=await pixel();assert.notEqual(after[0],after[2]);
        await pickPhoto(page,1);await pickPhoto(page,0);assert.deepEqual(await pixel(),after);
        await page.locator('[data-undo-paint]').click();assert.deepEqual(await pixel(),before);
      }
      if(mode==='versions'){await page.locator('[data-look]').selectOption('grayscale(1) contrast(1.15)');await page.locator('[data-capture]').click();await page.waitForFunction(()=>document.querySelector('[data-status]').textContent.startsWith('Version kept'));}
      if(mode==='compare'||mode==='restoration')await page.locator('[data-compare]').fill('35');
      const promise=page.waitForEvent('download');await page.locator('[data-export]').click();const artifact=await promise,bytes=await fs.readFile(await artifact.path());
      assert.ok(bytes.length>1000,mode);
      if(artifact.suggestedFilename().endsWith('.html')){
        const html=bytes.toString();assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>alert(1)</script>'));
        const exported=await browser.newPage();const exportErrors=[];exported.on('pageerror',e=>exportErrors.push(e.message));await exported.setContent(html);
        if(mode==='compare'||mode==='restoration'){assert.equal(await exported.locator('#comparison').inputValue(),'35');await exported.locator('#comparison').fill('25');assert.match(await exported.locator('#before').getAttribute('style'),/75%/);}
        if(mode==='slideshow'){await exported.locator('#next').click();assert.equal(await exported.locator('.slide:not([hidden])').count(),1);await exported.locator('#play').click();await exported.locator('#play').click();}
        assert.deepEqual(exportErrors,[]);await exported.close();
        if(mode==='slideshow'){
          const popup=page.waitForEvent('popup');await page.locator('[data-print]').click();const printable=await popup;
          await printable.emulateMedia({media:'print'});
          assert.equal(await printable.locator('script').count(),0);
          assert.equal(await printable.locator('.slide:visible').count(),2);
          await printable.close();
        }
      }else{assert.equal(bytes.readUInt32BE(16),1440);assert.equal(bytes.readUInt32BE(20),['recipe','calendar'].includes(mode)?1800:1080);}
    }
    assert.ok(await page.evaluate(()=>{const d=document.querySelector('dialog');return d.scrollWidth<=d.clientWidth;}));
    await pickMode(page,'restoration');const restoration=await page.locator('canvas').evaluate(c=>c.toDataURL());
    await pickPhoto(page,1);await page.locator('[data-earlier]').click();assert.equal(await page.locator('canvas').evaluate(c=>c.toDataURL()),restoration);
    assert.equal(await page.locator('#creative-studio .cs-thumb[aria-pressed="true"]').getAttribute('aria-label'),'1. Second.png','the moved print stays the chosen one');
    await page.locator('[data-later]').click();assert.equal(await page.locator('#creative-studio .cs-thumb').nth(1).getAttribute('aria-label'),'2. Second.png');
    await page.locator('[data-remove]').click();assert.equal(await photoCount(page),1);assert.equal(await page.locator('canvas').evaluate(c=>c.toDataURL()),restoration);
    assert.ok(await page.locator('[data-remove]').isDisabled(),'the last photograph cannot be removed');
    // The creations are a radio group: the arrows move along it and choose.
    await page.locator('#creative-studio [data-mode="restoration"]').focus();await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#creative-studio [aria-checked="true"]').getAttribute('data-mode'),'album');
    await pickMode(page,'collage');
    await page.screenshot({path:path.join(process.env.NINAIVU_SHOTS||os.tmpdir(),`ninaivu-creative-${mobile?'mobile':'desktop'}.png`)});
    page.once('dialog',d=>d.accept());await page.locator('[data-close]').click();await page.locator('#creative-studio').waitFor({state:'detached'});
    // Photographs come from the library as well as from files; a picture
    // creation can be saved into the library the way an edit is; the sheet can
    // be square or a story; a collage has layouts; a postcard is a postcard.
    // The library is asked the way the grid asks it: photographs only, and
    // narrowed to favourites, an album, a person or a search when one is chosen.
    const asked=[];
    await page.route('**/api/assets?*',route=>{asked.push(new URL(route.request().url()).searchParams);route.fulfill({contentType:'application/json',body:JSON.stringify({items:[{id:7,kind:'picture',name:'Amma.jpg',view:'/api/file/7',thumb_v:1,date:'2019-01-15',caption:'Pongal morning'},{id:8,kind:'video',name:'clip.mp4',view:'/api/file/8'},{id:9,kind:'picture',name:'Appa.jpg',view:'/api/file/9',date:'',caption:''}],total:3})});});
    await page.route('**/api/albums',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({albums:[{id:3,name:'Pongal 2019',n:24,cover_id:7},{id:4,name:'Empty',n:0}]})}));
    await page.route('**/api/faces/people',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({people:[{id:5,name:'Paati',photo_count:310,cover_face_id:11}]})}));
    await page.route('**/api/faces/thumb/*',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
    // The photograph being edited brings its day and caption, as the viewer shows them.
    await page.route('**/api/asset/42',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({id:42,name:'IMG_0042.jpg',kind:'picture',date:'2019-01-14',caption:'The kolam outside'})}));
    await page.route('**/api/thumb/*',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
    await page.route('**/api/file/*',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
    const saves=[];
    await page.route('**/api/asset/42/edited-copy',async route=>{saves.push(route.request().headers()['content-type']);await route.fulfill({status:201,contentType:'application/json',body:'{"id":43}'});});
    await page.evaluate(async()=>{
      const c=document.createElement('canvas');c.width=600;c.height=400;const x=c.getContext('2d');x.fillStyle='#df2929';x.fillRect(0,0,600,400);
      const blob=await new Promise(r=>c.toBlob(r));
      const {openCreativeStudio}=await import('/static/js/creative-studio.js');
      await openCreativeStudio({original:blob,edited:blob,returnFocus:document.querySelector('button'),sourceId:42,canSave:true,onSaved:copy=>{window.savedCopy=copy;}});
    });
    await page.waitForFunction(()=>document.querySelector('#creative-studio [data-status]').textContent.startsWith('Ready.'));
    await page.waitForFunction(()=>document.querySelector('[data-caption]').value==='The kolam outside');
    assert.equal(await page.locator('[data-date]').inputValue(),'2019-01-14');
    assert.equal(await page.locator('#creative-studio .cs-thumb').first().getAttribute('aria-label'),'1. IMG_0042.jpg');
    await page.locator('[data-library]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-picker button').length===2,null,{timeout:10000});   // the video is not offered
    assert.deepEqual(asked.at(-1).getAll('kind'),['picture']);
    await page.locator('#creative-studio .cs-picker button').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===2);
    assert.match(await page.locator('#creative-studio .cs-thumb').nth(1).getAttribute('aria-label'),/Amma/);
    assert.equal(await page.locator('[data-caption]').inputValue(),'Pongal morning','a library photograph brings its caption');
    assert.equal(await page.locator('[data-date]').inputValue(),'2019-01-15','and its day');
    assert.equal(await page.locator('#creative-studio .cs-picker button').first().getAttribute('aria-pressed'),'true','what is in the creation is ticked');
    // Pressed again, it comes out; pressed a third time, it goes back in.
    await page.locator('#creative-studio .cs-picker button').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===1);
    await page.locator('#creative-studio .cs-picker button').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===2);
    await page.locator('#creative-studio [data-source="favorites"]').click();
    await until(()=>asked.at(-1).get('favorites')==='1','favourites asked for');
    await page.locator('#creative-studio [data-source="albums"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-picker .cs-group').length===1);   // the empty album is not offered
    await page.locator('#creative-studio .cs-group').click();
    await page.waitForFunction(()=>!document.querySelector('[data-picker-head]').hidden&&document.querySelectorAll('#creative-studio .cs-picker [data-asset]').length===2);
    assert.equal(asked.at(-1).get('album'),'3');assert.equal(await page.locator('[data-picker-title]').textContent(),'Pongal 2019');
    await page.locator('#creative-studio [data-source="people"]').click();
    await page.locator('#creative-studio .cs-group').click();
    await page.waitForFunction(()=>document.querySelector('[data-picker-title]').textContent==='Paati'&&document.querySelectorAll('#creative-studio .cs-picker [data-asset]').length===2);
    assert.equal(asked.at(-1).get('person'),'5');
    await page.locator('[data-picker-search]').fill('beach');
    await until(()=>asked.at(-1).get('q')==='beach','the search asked for');
    assert.equal(asked.at(-1).get('person'),'5','a search inside a person stays inside them');
    await pickMode(page,'album');assert.ok(await page.locator('[data-save-library]').isHidden(),'an album is a web page, not a picture');
    await pickMode(page,'postcard');assert.ok(await page.locator('[data-save-library]').isVisible());
    await page.locator('[data-format]').selectOption('story');
    assert.deepEqual(await page.locator('canvas').evaluate(c=>[c.width,c.height]),[1080,1920]);
    await pickMode(page,'collage');
    for(const layout of ['feature','polaroid']){await page.locator('[data-layout]').selectOption(layout);const promise=page.waitForEvent('download');await page.locator('[data-export]').click();const artifact=await promise;assert.ok((await fs.readFile(await artifact.path())).length>1000,layout);}
    await page.locator('[data-format]').selectOption('square');
    assert.deepEqual(await page.locator('canvas').evaluate(c=>[c.width,c.height]),[1080,1080]);
    await page.locator('[data-save-library]').click();
    await page.waitForFunction(()=>window.savedCopy?.id===43);
    assert.deepEqual(saves,['image/png']);
    await page.locator('#creative-studio [data-close]').click();
    await page.locator('#creative-studio').waitFor({state:'detached'});
    // From the gallery: a selection of library photographs, no edit and so no
    // restoration story; the video in the selection is left out.
    await page.route('**/api/asset/7',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({id:7,kind:'picture',name:'Amma.jpg',view:'/api/file/7',date:'2019-01-15',caption:'Pongal morning'})}));
    await page.route('**/api/asset/8',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({id:8,kind:'video',name:'clip.mp4',view:'/api/file/8'})}));
    await page.route('**/api/asset/9',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({id:9,kind:'picture',name:'Appa.jpg',view:'/api/file/9'})}));
    const fromGallery=async options=>page.evaluate(async options=>{const {openCreativeStudio}=await import('/static/js/creative-studio.js');await openCreativeStudio(options);},options);
    const closeStudio=async()=>{await page.locator('#creative-studio [data-close]').click();await page.locator('#creative-studio').waitFor({state:'detached'});};
    await fromGallery({ids:[7,8,9]});
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===2&&/2 photos ready/.test(document.querySelector('#creative-studio [data-status]').textContent));
    assert.equal(await page.locator('#creative-studio [data-mode="restoration"]').count(),0,'no restoration story without an edit');
    assert.equal(await page.locator('#creative-studio [data-mode="collage"]').getAttribute('aria-checked'),'true','several photographs start as a collage');
    assert.ok(await page.locator('[data-export]').isEnabled());assert.ok(await page.locator('[data-save-library]').isHidden());
    await closeStudio();
    // One photograph from the viewer is a card, and can be saved beside it.
    await fromGallery({ids:[7],sourceId:7,canSave:true});
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===1&&/1 photo ready/.test(document.querySelector('#creative-studio [data-status]').textContent));
    assert.equal(await page.locator('#creative-studio [data-mode="postcard"]').getAttribute('aria-checked'),'true');
    assert.ok(await page.locator('[data-save-library]').isVisible());
    await closeStudio();
    // An album's "Make a collage": its first photographs, asked of the library like the grid asks.
    await fromGallery({ids:[],filters:{album:3}});
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===2);
    assert.equal(asked.at(-1).get('album'),'3');assert.equal(asked.at(-1).get('limit'),'12');
    await closeStudio();
    // From the Create menu with nothing chosen: it opens at the library picker, with nothing to download yet.
    await fromGallery({});
    await page.waitForFunction(()=>!document.querySelector('#creative-studio [data-picker]').hidden&&document.querySelectorAll('#creative-studio .cs-picker [data-asset]').length===2);
    assert.ok(await page.locator('[data-export]').isDisabled());assert.ok(await page.locator('[data-print]').isDisabled());
    await page.locator('#creative-studio .cs-picker [data-asset]').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#creative-studio .cs-thumb').length===1);
    assert.ok(await page.locator('[data-export]').isEnabled());
    page.once('dialog',d=>d.accept());
    await closeStudio();
    assert.deepEqual(errors,[]);assert.deepEqual(await page.evaluate(()=>window.cspErrors),[]);await page.close();
  }
  console.log('Creative Studio: all ten modes, desktop/mobile exports, HTML interaction, escaped captions, selective-color undo, the library picker, library dates and captions, and opening from the gallery passed.');
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
