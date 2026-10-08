// Standalone synthetic-image browser checks: no household data or running app.
import os from 'node:os';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {launch} from './harness.mjs';
const root=path.resolve('ninaivu/static');
const server=http.createServer(async(req,res)=>{
  if(req.url==='/'){
    res.setHeader('Content-Type','text/html');
    res.end('<html data-theme="light"><link rel="stylesheet" href="/static/css/style.css"><button id="nav-ai-playground">AI Playground</button><script type="module" src="/boot.js"></script></html>');return;
  }
  if(req.url==='/boot.js'){res.setHeader('Content-Type','text/javascript');res.end("import {openPlayground} from '/static/js/ai-playground/components/playground.js'; document.querySelector('button').onclick=openPlayground;");return;}
  const target=path.resolve(root,'.'+req.url.replace(/^\/static/,''));
  if(!target.startsWith(root+path.sep)){res.writeHead(404).end();return;}
  try{res.setHeader('Content-Type',target.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(target));}catch{res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try{
  // Was `channel:'msedge'`: a browser this machine need not have. The
  // harness knows which one is actually installed — same fix as editor_ui.
  browser=await launch();
  for(const mobile of [false,true]){
    const page=await browser.newPage({viewport:mobile?{width:390,height:844}:{width:1440,height:1000}});
    const errors=[],uploads=[];page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.method()==='POST')uploads.push(r.url());});
    if(mobile)await page.addInitScript(()=>{window.Worker=undefined;}); // CPU fallback
    await page.goto(`http://127.0.0.1:${server.address().port}`);await page.locator('#nav-ai-playground').click();
    assert.ok(await page.locator('[data-export]').isDisabled());
    const fixture=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=600;c.height=400;const ctx=c.getContext('2d');ctx.fillStyle='#303030';ctx.fillRect(0,0,600,400);return c.toDataURL().split(',')[1];});
    await page.locator('[data-close]').click();
    await page.route('**/current.png',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{view:'/current.png',name:'dark.png'},returnFocus:document.querySelector('button')});
    });
    // Waits for the preview to be ready, by the state rather than the words:
    // this waited for a status of "Preview ready", which the playground has
    // never said — it says "Preview updated." — so it timed out on wording
    // that had been changed out from under it. The export button going from
    // disabled to enabled *is* the preview being ready, and it is the same
    // thing asserted disabled a few lines above.
    const ready=()=>page.waitForFunction(
      ()=>!document.querySelector('[data-export]').disabled);
    await ready();assert.ok(await page.getByRole('button',{name:/Improve lighting/}).count());
    await page.locator('[data-view=edited]').click();assert.ok(await page.locator('.ap-original').isHidden());
    await page.locator('[data-view=original]').click();assert.equal(await page.locator('[data-view=original]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-view=compare]').click();assert.ok(await page.locator('.ap-compare').isVisible());
    // The prompt ideas belong to the AI Assist mode, which is not the one it opens in.
    await page.locator('.ap-mode-btn[data-tab="ai"]').click();
    await page.getByRole('button',{name:'Natural light',exact:true}).click();assert.match(await page.locator('#ap-prompt').inputValue(),/shadows/);
    await page.locator('.ap-mode-btn[data-tab="adjustments"]').click();
    const pixel=()=>page.evaluate(async()=>{const img=document.querySelector('.ap-edited');await img.decode();const c=document.createElement('canvas');c.width=img.naturalWidth;c.height=img.naturalHeight;const ctx=c.getContext('2d');ctx.drawImage(img,0,0);return ctx.getImageData(20,20,1,1).data[0];});
    const original=await pixel();await page.getByRole('button',{name:/Improve lighting/}).click();await ready();assert.ok(await pixel()>original);
    await page.locator('[data-undo]').click();await ready();assert.equal(await pixel(),original);
    await page.locator('[data-redo]').click();await ready();assert.ok(await pixel()>original);
    await page.locator('[data-adjust="exposure"]').focus();await page.keyboard.press('Control+z');await ready();assert.equal(await pixel(),original);
    await page.keyboard.press('Control+Shift+z');await ready();assert.ok(await pixel()>original);
    // A signed readout: +25 is brighter, -25 darker.
    assert.equal(await page.locator('[data-value="exposure"]').textContent(),'+25');
    await page.locator('[data-crop]').selectOption('square');await ready();
    const downloadPromise=page.waitForEvent('download');await page.locator('[data-export]').click();const download=await downloadPromise;assert.equal(download.suggestedFilename(),'ninaivu-edited.png');
    const bytes=await fs.readFile(await download.path());assert.equal(bytes.readUInt32BE(16),400);assert.equal(bytes.readUInt32BE(20),400);assert.ok(!bytes.includes(Buffer.from('eXIf')));
    await page.locator('[data-reset]').click();await ready();assert.equal(await pixel(),original);
    await page.locator('[data-compare]').fill('25');assert.match(await page.locator('.ap-original').getAttribute('style'),/75%/);
    // A mouse drag along a slider must not sweep a text selection across the
    // panel (Safari did, ignoring the unprefixed user-select); text elsewhere
    // stays selectable.
    const selectionAfterPress=selector=>page.evaluate(selector=>{const target=document.querySelector(selector);target.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));const ev=new Event('selectstart',{bubbles:true,cancelable:true});target.dispatchEvent(ev);return !ev.defaultPrevented;},selector);
    assert.equal(await selectionAfterPress('[data-adjust="exposure"]'),false);
    assert.equal(await selectionAfterPress('[data-compare]'),false);
    const stageBox=await page.locator('.ap-stage').boundingBox();await page.mouse.move(stageBox.x+stageBox.width/2,stageBox.y+stageBox.height/2);await page.mouse.down();
    assert.equal(await page.evaluate(()=>{const ev=new Event('selectstart',{bubbles:true,cancelable:true});document.querySelector('.ap-stage').dispatchEvent(ev);return !ev.defaultPrevented;}),false);
    await page.mouse.up();await page.locator('[data-compare]').fill('25');
    assert.equal(await selectionAfterPress('.ap-group-title'),true);
    // A highlight already over the picture goes when a slider is pressed.
    assert.equal(await page.evaluate(()=>{getSelection().selectAllChildren(document.querySelector('.ap-stage'));document.querySelector('[data-compare]').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));return getSelection().rangeCount;}),0);
    assert.ok(await page.evaluate(()=>{const d=document.querySelector('dialog');return d.scrollWidth<=d.clientWidth&&d.getBoundingClientRect().width<=innerWidth;}));
    await page.screenshot({path:path.join(process.env.NINAIVU_SHOTS||os.tmpdir(),`ninaivu-ai-${mobile?'mobile':'desktop'}.png`),fullPage:true});
    page.once('dialog',d=>d.dismiss());await page.locator('[data-close]').click();assert.ok(await page.locator('#ai-playground').isVisible());
    page.once('dialog',d=>d.accept());await page.locator('[data-close]').click();await page.locator('dialog').waitFor({state:'detached'});
    assert.equal(await page.evaluate(()=>document.activeElement.id),'nav-ai-playground');
    await page.route('**/current.png',route=>route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')}));
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{view:'/current.png',name:'Current library photo',rotation:90},returnFocus:document.querySelector('button')});
    });
    await ready();assert.match(await page.locator('.ap-dimensions').textContent(),/Current library photo · 400 × 600/);
    assert.equal(await pixel(),original);
    await page.locator('[data-close]').click();await page.locator('dialog').waitFor({state:'detached'});
    assert.equal(await page.evaluate(()=>document.activeElement.id),'nav-ai-playground');
    assert.deepEqual(uploads,[]);
    await page.route('**/api/asset/42/edited-copy',route=>route.fulfill({status:403,contentType:'application/json',body:'{"error":"Saving is not permitted."}'}));
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{id:42,view:'/current.png',name:'Library source'},canSave:true,onSaved:copy=>window.savedCopy=copy});
    });
    await ready();await page.locator('[data-save]').click();
    await page.waitForFunction(()=>document.querySelector('.ap-status').textContent==='Saving is not permitted.');
    assert.ok(await page.locator('#ai-playground').isVisible());assert.equal(await page.evaluate(()=>window.savedCopy),undefined);
    await page.route('**/api/asset/42/edited-copy',async route=>{
      assert.equal(route.request().headers()['content-type'],'image/png');
      const buffer=route.request().postDataBuffer();assert.equal(buffer.readUInt32BE(16),600);
      await route.fulfill({status:201,contentType:'application/json',body:'{"id":43}'});
    });
    await page.locator('[data-save]').click();await page.waitForFunction(()=>window.savedCopy?.id===43);
    await page.locator('#ai-playground').waitFor({state:'detached'});assert.equal(uploads.length,2);
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{id:42,view:'/current.png',name:'Guest source'},canSave:false});
    });
    await ready();assert.ok(await page.locator('[data-save]').isHidden());
    await page.locator('[data-close]').click();await page.locator('#ai-playground').waitFor({state:'detached'});
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{id:42,view:'/current.png',name:'Library source'},canSave:true});
    });
    await ready();assert.ok(await page.locator('[data-save]').isVisible());
    // The Enhance tools are on the page whether or not anything is set up: with
    // no capabilities at all, every tool is there, greyed out, and the lines
    // under them say what each needs. The library's tools are hidden until they
    // are known to work; the clothing colour studio needs no model.
    await page.locator('.ap-mode-btn[data-tab="magic"]').click();
    assert.ok(await page.locator('[data-server-tools]').isVisible());
    for(const kind of ['upscale','restore','colorize']){assert.ok(await page.locator(`[data-server-job="${kind}"]`).isVisible());assert.ok(await page.locator(`[data-server-job="${kind}"]`).isDisabled());}
    assert.equal(await page.locator('[data-enhance-needs] li').count(),3);
    assert.ok(await page.locator('[data-revive]').isHidden());assert.ok(await page.locator('[data-pop-bg]').isHidden());
    assert.ok(await page.locator('[data-recolor]').isEnabled());
    await page.locator('.ap-mode-btn[data-tab="adjustments"]').click();
    await page.locator('.ap-mode-btn[data-tab="ai"]').click();      // the prompt is in AI Assist, not the opening mode
    await page.locator('#ap-prompt').fill('Lift the shadows, reduce highlights, and make it slightly warmer');
    const beforePlan=await pixel();await page.locator('.ap-ask button[type=submit]').click();
    await page.locator('.ap-plan').waitFor({state:'visible'});assert.equal(await pixel(),beforePlan);
    assert.match(await page.locator('[data-plan-changes]').textContent(),/Shadows: 0 → 30/);
    await page.locator('[data-apply-plan]').click();await ready();assert.ok(await pixel()>beforePlan);
    await page.locator('[data-undo]').click();await ready();assert.equal(await pixel(),beforePlan);
    await page.route('**/api/ai-playground/plan',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({patch:{shadows:20},summary:'Lift dark tones',provider:'Test local AI'})}));
    await page.locator('[data-provider]').selectOption('local');await page.locator('#ap-prompt').fill('Help me see detail in the dim parts without changing the color');
    await page.locator('.ap-ask button[type=submit]').click();await page.locator('.ap-plan').waitFor({state:'visible'});
    assert.match(await page.locator('[data-plan-summary]').textContent(),/Test local AI/);
    await page.route('**/api/ai-playground/generate',async route=>{
      const payload=route.request().postDataJSON();assert.ok(payload.image.length>100);assert.equal(payload.prompt,'Turn this scene into a watercolor painting');
      assert.deepEqual(payload.options,{quality:'detailed',fidelity:'balanced',negative_prompt:'added text',seed:123});
      await route.fulfill({contentType:'image/png',body:Buffer.from(fixture,'base64')});
    });
    await page.locator('[data-provider]').selectOption('generate');await page.locator('#ap-prompt').fill('Turn this scene into a watercolor painting');
    assert.ok(await page.locator('[data-generation-options]').isVisible());
    await page.locator('[data-new-seed]').click();
    assert.ok(Number(await page.locator('[data-generation-seed]').inputValue())>=0);
    await page.locator('[data-generation-quality]').selectOption('detailed');
    await page.locator('[data-generation-fidelity]').selectOption('balanced');
    await page.locator('[data-generation-negative]').fill('added text');
    await page.locator('[data-generation-seed]').fill('123');
    await page.locator('.ap-ask button[type=submit]').click();await page.locator('.ap-generated').waitFor({state:'visible'});
    assert.match(await page.locator('[data-generated-note]').textContent(),/seed 123/);
    assert.equal(await pixel(),beforePlan);
    page.once('dialog',d=>d.dismiss());await page.locator('[data-close]').click();assert.ok(await page.locator('#ai-playground').isVisible());
    await page.locator('[data-save]').click();
    await page.waitForFunction(()=>document.querySelector('.ap-status').textContent.startsWith('Copy saved.'));
    assert.ok(await page.locator('.ap-generated').isVisible());
    const generatedDownload=page.waitForEvent('download');await page.locator('[data-download-generated]').click();assert.equal((await generatedDownload).suggestedFilename(),'ninaivu-ai-generated.png');
    await page.locator('[data-discard-generated]').click();assert.ok(await page.locator('.ap-generated').isHidden());
    // (The dress-colour studio that used to be tested here moved out of the core into the
    // Creative Studio extension, and is covered by creative_studio_ui.mjs.)
    await page.locator('.ap-mode-btn[data-tab="magic"]').click();    // Magic tools: where Creative Studio is opened from
    await page.locator('[data-creative]').click();
    await page.locator('#creative-studio').waitFor({state:'visible'});
    await page.waitForFunction(()=>document.querySelector('#creative-studio [data-status]').textContent.startsWith('Ready.'));
    await page.locator('#creative-studio [data-close]').click();
    assert.ok(await page.locator('#ai-playground').isVisible());
    await page.evaluate(async()=>{
      const {openRemoveObject}=await import('/static/js/ai-playground/components/removeobject.js');
      window.lifecycleBlob=await (await fetch('/current.png')).blob();
      openRemoveObject(await createImageBitmap(window.lifecycleBlob),{removeObject:()=>new Promise(resolve=>window.resolveRemoval=resolve)});
    });
    await page.locator('.ap-recolor canvas').focus();await page.keyboard.press('Space');await page.locator('.ap-recolor [data-apply]').click();
    await page.waitForFunction(()=>typeof window.resolveRemoval==='function');
    await page.locator('[data-close-remove]').click();await page.locator('.ap-recolor').waitFor({state:'detached'});
    await page.evaluate(()=>{window.originalBitmap=createImageBitmap;window.lateBitmaps=0;window.createImageBitmap=(...args)=>{window.lateBitmaps++;return window.originalBitmap(...args);};window.resolveRemoval(window.lifecycleBlob);});
    await page.waitForTimeout(50);assert.equal(await page.evaluate(()=>window.lateBitmaps),0);
    await page.evaluate(()=>window.createImageBitmap=window.originalBitmap);
    await page.locator('[data-close]').click();await page.locator('#ai-playground').waitFor({state:'detached'});
    // A tool's result goes into the library the same way the sliders' edit
    // does: here a colour pop, from a mask the server is stood in for. An
    // administrator's save comes back with an id; a family member's save is
    // told it is waiting for approval, and the result stays on the page.
    await page.route('**/api/ai-playground/capabilities',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({segmentation_model:true,server_jobs:[],local_jobs:['restore','colorize'],enhance_tools:{upscale:{ready:false,model:'Upscale ×4 (Real-ESRGAN)'},restore:{ready:true},colorize:{ready:true}}})}));
    const maskPng=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=600;c.height=400;const ctx=c.getContext('2d');ctx.fillStyle='#000';ctx.fillRect(0,0,600,400);ctx.fillStyle='#fff';ctx.fillRect(150,100,300,200);return c.toDataURL().split(',')[1];});
    await page.route('**/api/ai-playground/segment',route=>route.fulfill({contentType:'image/png',body:Buffer.from(maskPng,'base64')}));
    const saves=[];
    await page.route('**/api/asset/42/edited-copy',async route=>{
      saves.push(route.request().headers()['content-type']);
      await route.fulfill(saves.length===1
        ?{status:201,contentType:'application/json',body:'{"id":44}'}
        :{status:202,contentType:'application/json',body:'{"status":"pending","message":"Saved for an administrator to approve."}'});
    });
    await page.evaluate(async()=>{
      const {openPlayground}=await import('/static/js/ai-playground/components/playground.js');
      openPlayground({item:{id:42,view:'/current.png',name:'Library source'},canSave:true,onSaved:copy=>window.savedCopy=copy});
    });
    await ready();await page.locator('.ap-mode-btn[data-tab="magic"]').click();
    await page.waitForFunction(()=>!document.querySelector('[data-pop-bg]').hidden);
    assert.ok(await page.locator('[data-revive]').isVisible(),'Restore and Colourise together offer Revive old photo');
    assert.ok(await page.locator('[data-server-job="upscale"]').isDisabled());assert.equal(await page.locator('[data-enhance-needs] li').count(),1);
    assert.match(await page.locator('[data-enhance-needs]').textContent(),/Upscale needs the Upscale ×4 \(Real-ESRGAN\) model/);
    await page.locator('[data-pop-bg]').click();
    await page.locator('.ap-background').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-background-title]').textContent(),'Colour pop');
    await page.locator('[data-save-background]').click();
    await page.waitForFunction(()=>window.savedCopy?.id===44);
    assert.equal(saves[0],'image/png');assert.ok(await page.locator('.ap-background').isVisible(),'the result stays after it is saved');
    await page.locator('[data-save-background]').click();
    await page.waitForFunction(()=>document.querySelector('.ap-status').textContent==='Saved for an administrator to approve.');
    assert.equal(saves.length,2);
    // The clothing colour studio opens, and its result can come back onto the photo.
    await page.locator('[data-recolor]').click();await page.locator('.ap-recolor-dialog').waitFor({state:'visible'});
    assert.ok(await page.locator('[data-apply-recolor]').isVisible());assert.ok(await page.locator('[data-apply-recolor]').isDisabled());
    await page.locator('[data-close-recolor]').click();await page.locator('.ap-recolor-dialog').waitFor({state:'detached'});
    page.once('dialog',d=>d.accept());await page.locator('[data-close]').click();await page.locator('#ai-playground').waitFor({state:'detached'});
    assert.deepEqual(errors,[]);await page.close();
  }
  console.log('Desktop worker and mobile CPU: previews, keyboard/history, crop/export, discard protection, explicit library save, guest restrictions, comparison modes and prompt shortcuts passed.');
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
