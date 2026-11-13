/** Shared lifecycle regressions, independent of a household or model stack. */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { launch } from './harness.mjs';

const root = path.resolve('ninaivu/static');
const server = http.createServer(async (request, response) => {
  if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html');
    response.end(`<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
      <link rel="stylesheet" href="/css/style.css">
      <style>html,body{height:auto}body{min-height:2600px;overflow:auto}.launch{margin-top:600px}.custom-limit{max-height:730px}</style>
      <button class="launch" id="launch">Open dialog</button><button id="outside">Background</button>
      <div id="first" class="modal" hidden role="dialog" aria-modal="true" aria-labelledby="title">
        <div class="modal-card"><h2 id="title">First dialog</h2><input id="first-input" aria-label="Name">
          <button id="nested">Open child</button><button disabled>Disabled</button>
          <button hidden>Hidden</button><button id="last">Last</button></div></div>
      <div id="second" class="sheet" hidden role="dialog" aria-modal="true" aria-label="Second dialog">
        <div class="sheet-card"><input id="second-input" aria-label="Child name"><button id="legacy-close">Close child</button></div></div>
      <dialog id="native"><input aria-label="Editor"><button id="native-last">Last editor control</button></dialog>
      <div class="gate" hidden><input id="auth" aria-label="Sign in"></div>
      <script type="module">
        import {registerDialog,activeDialog} from '/js/dialogs.js';
        window.dialogCloseCount=0;window.pageEscapes=0;window.dismissals=0;window.allowClose=false;
        const first=document.querySelector('#first'),second=document.querySelector('#second'),native=document.querySelector('#native');
        window.first=registerDialog(first,{escape:true,backdrop:true,initialFocus:'#first-input',onClose:()=>window.dialogCloseCount++});
        window.second=registerDialog(second,{escape:true,backdrop:true,initialFocus:'#second-input'});
        window.nativeDialog=registerDialog(native,{escape:true,initialFocus:false,onDismiss:()=>{
          window.dismissals++;if(window.allowClose)window.nativeDialog.close();
        }});
        native.addEventListener('keydown',event=>event.stopPropagation());
        document.querySelector('#launch').onclick=()=>window.first.open();
        document.querySelector('#nested').onclick=()=>window.second.open();
        document.querySelector('#legacy-close').onkeydown=event=>{if(event.key==='Escape')window.second.close();};
        document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!event.defaultPrevented&&!activeDialog())window.pageEscapes++;});
        window.activeDialog=activeDialog;window.registerDialog=registerDialog;window.ready=true;
      </script>`);
    return;
  }
  const target = path.resolve(root, '.' + request.url.split('?')[0]);
  if (!target.startsWith(root + path.sep)) { response.writeHead(404); response.end(); return; }
  try {
    response.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : 'text/javascript');
    response.end(await fs.readFile(target));
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const browser = await launch();
try {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
    const context = await browser.newContext({ viewport, hasTouch: true, isMobile: viewport.width < 500 });
    context.setDefaultTimeout(10000);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => window.ready);
    await page.evaluate(() => scrollTo(0, 400));
    const scroll = await page.evaluate(() => scrollY);
    await page.locator('#launch').click();
    assert.equal(await page.locator('#first-input').evaluate(node => node === document.activeElement), true);
    assert.equal(await page.locator('#first .modal-card').evaluate(node => {
      node.classList.add('custom-limit');const height=getComputedStyle(node).maxHeight;node.classList.remove('custom-limit');return height;
    }), '730px', 'the layer preserves component sizing outside keyboard-constrained viewports');
    assert.equal(await page.evaluate(() => document.documentElement.classList.contains('dialog-open')), true);
    await page.locator('#last').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#first-input').evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.locator('#last').evaluate(node => node === document.activeElement), true);
    await page.locator('#outside').evaluate(node => node.focus());
    assert.equal(await page.locator('#first-input').evaluate(node => node === document.activeElement), true);

    await page.locator('#nested').click();
    assert.equal(await page.locator('#first').evaluate(node => node.inert), true);
    await page.locator('#legacy-close').focus();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#second').isHidden(), true);
    assert.equal(await page.locator('#first').isVisible(), true);
    assert.equal(await page.locator('#first').evaluate(node => node.inert), false);
    assert.equal(await page.locator('#nested').evaluate(node => node === document.activeElement), true);

    await page.evaluate(() => nativeDialog.open());
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#native').evaluate(node => node.open), true, 'a busy/unsaved guard can refuse dismissal');
    assert.equal(await page.evaluate(() => dismissals), 1, 'native cancel must not call the guard twice');
    await page.evaluate(() => { window.allowClose = true; });
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#native').evaluate(node => node.open), false);
    assert.equal(await page.locator('#first').isVisible(), true);
    await page.evaluate(() => {
      nativeDialog = registerDialog(document.querySelector('#native'), { onDismiss: () => {
        window.dismissals++;
        return new Promise(resolve => { window.finishDismiss = () => { nativeDialog.close(); resolve(); }; });
      } });
      nativeDialog.open();
    });
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => dismissals), 3, 'an asynchronous close guard runs once while pending');
    await page.evaluate(() => finishDismiss());
    assert.equal(await page.locator('#native').evaluate(node => node.open), false);
    await page.evaluate(() => {
      nativeDialog.open();
      const lock=document.createElement('dialog');lock.className='lock-screen';lock.setAttribute('aria-label','Locked');
      lock.innerHTML='<input id="lock-secret" aria-label="Password">';
      lock.addEventListener('cancel',event=>event.preventDefault());document.body.append(lock);lock.showModal();
      lock.querySelector('input').focus();window.testLock=lock;
    });
    await page.keyboard.press('Escape');await page.keyboard.press('Escape');
    assert.equal(await page.locator('.lock-screen').evaluate(node => node.open), true, 'repeated Escape cannot bypass a screen lock');
    await page.evaluate(() => { testLock.close();testLock.remove(); });
    assert.equal(await page.locator('#native').evaluate(node => node.open), true, 'unlocking preserves the editor underneath');
    await page.evaluate(() => nativeDialog.close());

    // Simulate keyboard bounds: real visualViewport events use the same update.
    await page.evaluate(() => {
      window.originalViewport = window.visualViewport;
      Object.defineProperty(window, 'visualViewport', { configurable: true,
        value: { width: innerWidth, height: 330, offsetTop: 60, offsetLeft: 0 } });
      window.dispatchEvent(new Event('resize'));
    });
    await page.waitForFunction(() => document.querySelector('#first').getBoundingClientRect().height === 330).catch(async error => {
      console.error(await page.evaluate(() => ({ active: activeDialog()?.id,
        hidden: document.querySelector('#first').hidden, height: document.querySelector('#first').getBoundingClientRect().height,
        style: document.querySelector('#first').getAttribute('style') })));
      throw error;
    });
    assert.equal(await page.locator('#first').evaluate(node => node.getBoundingClientRect().top), 60);
    assert.ok(await page.locator('#first .modal-card').evaluate(node => node.getBoundingClientRect().height <= 290));
    await page.evaluate(() => {
      Object.defineProperty(window, 'visualViewport', { configurable: true, value: window.originalViewport });
      window.dispatchEvent(new Event('resize'));
      document.querySelector('.gate').hidden = false;
      document.querySelector('#auth').focus();
    });
    assert.equal(await page.locator('#auth').evaluate(node => node === document.activeElement), true, 'the layer must yield to sign-in/lock screens');
    await page.evaluate(() => { document.querySelector('.gate').hidden = true; });
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#first').isHidden(), true);
    assert.equal(await page.evaluate(() => dialogCloseCount), 1);
    assert.equal(await page.evaluate(() => pageEscapes), 0, 'one Escape cannot also trigger a page shortcut');
    assert.equal(await page.locator('#launch').evaluate(node => node === document.activeElement), true);
    assert.equal(await page.evaluate(() => scrollY), scroll);

    // Legacy hidden toggles and removed dynamic dialogs get the same cleanup.
    await page.evaluate(() => { document.querySelector('#first').hidden = false; });
    await page.waitForFunction(() => activeDialog()?.id === 'first');
    const field = await page.locator('#first-input').boundingBox();
    await page.mouse.move(field.x + 5, field.y + 5);
    await page.mouse.down();await page.mouse.move(5, 5);await page.mouse.up();
    assert.equal(await page.locator('#first').isVisible(), true, 'a drag out of the card is not a backdrop dismissal');
    await page.mouse.click(5, 5);
    assert.equal(await page.locator('#first').isHidden(), true);
    assert.equal(await page.evaluate(() => dialogCloseCount), 2);
    await page.evaluate(() => { first.open();document.querySelector('#first').hidden=true; });
    await page.waitForFunction(() => !activeDialog());
    assert.equal(await page.evaluate(() => dialogCloseCount), 3, 'legacy hidden changes run onClose exactly once');
    await page.evaluate(() => {
      const node = document.createElement('div');node.className='modal';node.innerHTML='<div class="modal-card"><button>Dynamic</button></div>';
      document.body.append(node);window.dynamic=node;registerDialog(node,{escape:true}).open();
    });
    await page.evaluate(() => dynamic.remove());
    await page.waitForFunction(() => !activeDialog());
    assert.equal(await page.evaluate(() => document.documentElement.classList.contains('dialog-open')), false);
    await page.evaluate(() => {
      const node=document.createElement('div');node.className='sheet';node.hidden=true;
      node.innerHTML='<div class="sheet-card"><h2>Profile</h2><input aria-label="Profile name"></div>';
      document.body.append(node);window.labelled=node;window.labelledDialog=registerDialog(node);labelledDialog.open();
    });
    assert.equal(await page.locator('.sheet').last().getAttribute('role'), 'dialog');
    assert.equal(await page.locator('.sheet').last().getAttribute('aria-modal'), 'true');
    await page.evaluate(() => { labelled.querySelector('.sheet-card').innerHTML='<h2>Updated profile</h2><input aria-label="New name">'; });
    await page.waitForFunction(() => document.getElementById(labelled.getAttribute('aria-labelledby'))?.textContent === 'Updated profile');
    await page.evaluate(() => labelledDialog.dispose());
    assert.equal(await page.evaluate(() => labelled.hasAttribute('role') || labelled.hasAttribute('aria-labelledby')), false);
    await page.evaluate(() => labelled.remove());
    await page.evaluate(() => { first.open();first.dispose();document.querySelector('#first').hidden=false; });
    await page.waitForTimeout(30);
    assert.equal(await page.evaluate(() => activeDialog()), null, 'disposed controllers must not be silently re-adopted');
    assert.deepEqual(errors, []);
    console.log(`PASS ${viewport.width}×${viewport.height}: focus, nested Escape, native guards, keyboard bounds, scroll restoration and lifecycle cleanup`);
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
