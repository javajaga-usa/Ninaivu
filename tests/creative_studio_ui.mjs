// Exercise every creative mode and its exported artifact with synthetic photos.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {launch} from './harness.mjs';
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
  browser=await launch({channel:'msedge'});
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
    await page.locator('[data-mode]').selectOption('compare');assert.ok(await page.locator('[data-export]').isDisabled());
    await page.locator('[data-files]').setInputFiles({name:'Second.png',mimeType:'image/png',buffer:Buffer.from(fixture,'base64')});
    await page.waitForFunction(()=>document.querySelector('[data-photo]').options.length===2);
    await page.locator('[data-caption]').fill('A family memory');
    await page.locator('[data-title]').fill('Family <script>alert(1)</script>');
    await page.locator('[data-story]').fill('A story with dates, food and wonderful people.');
    await page.locator('[data-mode]').selectOption('album');
    const wav=Buffer.alloc(1644);wav.write('RIFF');wav.writeUInt32LE(1636,4);wav.write('WAVEfmt ',8);wav.writeUInt32LE(16,16);wav.writeUInt16LE(1,20);wav.writeUInt16LE(1,22);wav.writeUInt32LE(8000,24);wav.writeUInt32LE(16000,28);wav.writeUInt16LE(2,32);wav.writeUInt16LE(16,34);wav.write('data',36);wav.writeUInt32LE(1600,40);
    await page.locator('[data-audio]').setInputFiles({name:'memory.wav',mimeType:'audio/wav',buffer:wav});
    await page.waitForFunction(()=>document.querySelector('audio').readyState>=1);
    assert.match(await page.locator('audio').getAttribute('src'),/^blob:/);
    await page.locator('[data-clear-audio]').click();assert.ok(await page.locator('audio').isHidden());
    await page.evaluate(()=>{window.RealReader=window.FileReader;window.FileReader=class extends window.RealReader{readAsDataURL(blob){setTimeout(()=>super.readAsDataURL(blob),100);}};});
    await page.locator('[data-audio]').setInputFiles({name:'memory.wav',mimeType:'audio/wav',buffer:wav});await page.locator('[data-clear-audio]').click();
    await page.waitForTimeout(180);assert.ok(await page.locator('audio').isHidden());await page.evaluate(()=>window.FileReader=window.RealReader);
    for(const mode of ['restoration','album','postcard','collage','compare','slideshow','selective','versions','recipe','calendar']){
      await page.locator('[data-mode]').selectOption(mode);
      if(mode==='recipe'){await page.locator('[data-ingredients]').fill('2 cups flour\n1 cup milk');await page.locator('[data-directions]').fill('Mix. Bake. Share.');}
      if(mode==='calendar'){await page.locator('[data-month]').fill('2026-09');await page.locator('[data-events]').fill('12 — Birthday');}
      if(mode==='selective'){
        const pixel=()=>page.locator('canvas').evaluate(c=>[...c.getContext('2d').getImageData(720,540,1,1).data]);
        const before=await pixel();assert.equal(before[0],before[1]);await page.locator('canvas').focus();await page.keyboard.press('Space');const after=await pixel();assert.notEqual(after[0],after[2]);
        await page.locator('[data-photo]').selectOption('1');await page.locator('[data-photo]').selectOption('0');assert.deepEqual(await pixel(),after);
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
    await page.locator('[data-mode]').selectOption('restoration');const restoration=await page.locator('canvas').evaluate(c=>c.toDataURL());
    await page.locator('[data-photo]').selectOption('1');await page.locator('[data-earlier]').click();assert.equal(await page.locator('canvas').evaluate(c=>c.toDataURL()),restoration);
    await page.locator('[data-photo]').selectOption('1');await page.locator('[data-remove]').click();assert.equal(await page.locator('canvas').evaluate(c=>c.toDataURL()),restoration);
    await page.locator('[data-mode]').selectOption('collage');
    await page.screenshot({path:path.join(process.env.TEMP||'.',`ninaivu-creative-${mobile?'mobile':'desktop'}.png`)});
    page.once('dialog',d=>d.accept());await page.locator('[data-close]').click();await page.locator('#creative-studio').waitFor({state:'detached'});
    assert.deepEqual(errors,[]);assert.deepEqual(await page.evaluate(()=>window.cspErrors),[]);await page.close();
  }
  console.log('Creative Studio: all ten modes, desktop/mobile exports, HTML interaction, escaped captions and selective-color undo passed.');
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
