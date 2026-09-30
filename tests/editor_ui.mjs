/* Standalone browser exercise; no household photographs or running app needed.
 * NODE_PATH may point to a bundled installation of Playwright. */
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { launch } from './harness.mjs';
const { chromium } = createRequire(import.meta.url)('playwright');
const root = path.resolve('ninaivu/static');
const server = http.createServer(async (req,res) => {
  if(req.url==='/') {res.setHeader('Content-Type','text/html');res.end(`<link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css"><script type="module">
    import { PhotoEditor } from '/js/editor.js';
    const c=document.createElement('canvas');c.width=600;c.height=800;const x=c.getContext('2d');
    const g=x.createLinearGradient(0,0,600,800);g.addColorStop(0,'#a66f50');g.addColorStop(1,'#333c40');x.fillStyle=g;x.fillRect(0,0,600,800);
    window.editor=new PhotoEditor({id:1,src:c.toDataURL(),rotation:0},copy=>window.saved=copy);editor.open();
  </script>`);return;}
  const target=path.resolve(root,'.'+req.url);
  if(!target.startsWith(root+path.sep)){res.writeHead(404);res.end();return;}
  try {res.setHeader('Content-Type',target.endsWith('.css')?'text/css':'text/javascript');res.end(await fs.readFile(target));}
  catch {res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  // Was `channel:'msedge'`: a browser this machine need not have,
  // asked for by a `launch` that was never imported.
  browser=await launch();
  for(const viewport of [{width:1440,height:900},{width:390,height:844}]){
    const page=await browser.newPage({viewport});const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/api/asset/1/edited-copy',async route=>{
      assert.equal(route.request().headers()['content-type'],'image/png');
      assert.ok(route.request().postDataBuffer().length>100);
      await route.fulfill({status:201,contentType:'application/json',body:'{"id":2}'});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(()=>window.editor?.ready);
    const fits=await page.evaluate(()=>{
      const p=document.querySelector('#pe-photo').getBoundingClientRect(),v=document.querySelector('.pe-viewport').getBoundingClientRect();
      return p.width<=v.width&&p.height<=v.height&&document.querySelector('dialog').getBoundingClientRect().width<=innerWidth;
    });assert.ok(fits,'Photo and dialog must fit');
    // The editor opens on Light, where the mask canvas takes no pointer events.
    // Pick a painting tool first, or the drag below paints nothing and the brush
    // has no area to work on.
    await page.locator('[data-panel="brush"]').click();
    const box=await page.locator('#pe-mask').boundingBox();
    await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+20,box.y+box.height/2+20);await page.mouse.up();
    await page.locator('[data-setting="dodgeAmount"]').fill('45');
    await page.locator('[data-setting="dodgeAmount"]').dispatchEvent('change');
    await page.waitForFunction(()=>!editor.rendering);
    assert.ok(await page.evaluate(()=>editor.result.data.some((v,i)=>v!==editor.original.data[i])),'Adjustment changes selected pixels');
    await page.locator('[data-action="compare"]').click();assert.equal(await page.locator('[data-action="compare"]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-action="compare"]').click();
    await page.locator('[data-action="undo"]').click();await page.waitForFunction(()=>!editor.rendering);
    assert.equal(await page.inputValue('[data-setting="dodgeAmount"]'),'0');
    await page.locator('[data-action="redo"]').click();await page.waitForFunction(()=>!editor.rendering);
    assert.equal(await page.inputValue('[data-setting="dodgeAmount"]'),'45');
    // Check what is actually painted on screen, not just worker output.
    const displayed = () => page.evaluate(()=>editor.photo.toDataURL());
    const edited = await displayed();
    await page.locator('[data-action="compare"]').click();
    assert.deepEqual(await displayed(),await page.evaluate(()=>editor.preview.toDataURL()));
    await page.locator('[data-action="compare"]').click();
    assert.deepEqual(await displayed(),edited);
    // Editing while comparing must immediately return to the edited view.
    await page.locator('[data-action="compare"]').click();
    await page.locator('[data-setting="dodgeAmount"]').fill('65');
    await page.locator('[data-setting="dodgeAmount"]').dispatchEvent('change');
    await page.waitForFunction(()=>!editor.rendering);
    assert.equal(await page.locator('[data-action="compare"]').getAttribute('aria-pressed'),'false');
    assert.notDeepEqual(await displayed(),await page.evaluate(()=>editor.preview.toDataURL()));
    assert.equal(await page.locator('#pe-overlay').isChecked(),false);
    await page.locator('[data-action="split"]').click();
    await page.locator('#pe-split-position').fill('50');
    assert.ok(await page.evaluate(()=>{
      const data=editor.photo.getContext('2d').getImageData(0,0,editor.photo.width,editor.photo.height).data;
      return data.every((v,i)=>v===(Math.floor(i/4)%editor.photo.width<editor.photo.width/2?editor.original.data[i]:editor.result.data[i]));
    }),'Split preview paints the original on the left and the edited pixels on the right');
    await page.locator('#pe-split-position').fill('100');
    assert.deepEqual(await displayed(),await page.evaluate(()=>editor.preview.toDataURL()));
    await page.locator('#pe-split-position').fill('0');
    assert.ok(await page.evaluate(()=>editor.photo.getContext('2d').getImageData(0,0,editor.photo.width,editor.photo.height).data.every((v,i)=>v===editor.result.data[i])));
    await page.locator('[data-action="split"]').click();
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    const fitWidth=(await page.locator('#pe-photo').boundingBox()).width;
    await page.selectOption('#pe-zoom','2');
    assert.ok(Math.abs((await page.locator('#pe-photo').boundingBox()).width-fitWidth*2)<2);
    await page.selectOption('#pe-zoom','1');
    // A second brush has its own area: the darkening brush, painted high on the picture.
    await page.locator('[data-tool="burn"]').click();
    const burnBox=await page.locator('#pe-mask').boundingBox();
    await page.mouse.move(burnBox.x+burnBox.width*.3,burnBox.y+burnBox.height*.25);
    await page.mouse.down();await page.mouse.up();
    const beforeBurn=await page.evaluate(()=>Array.from(editor.photo.getContext('2d').getImageData(180,200,1,1).data));
    await page.locator('[data-setting="burnAmount"]').fill('80');
    await page.locator('[data-setting="burnAmount"]').dispatchEvent('change');
    await page.waitForFunction(()=>!editor.rendering);
    assert.notDeepEqual(await page.evaluate(()=>Array.from(editor.photo.getContext('2d').getImageData(180,200,1,1).data)),beforeBurn);
    assert.ok(await page.evaluate(()=>{
      const i=(10*editor.photo.width+10)*4;
      return editor.result.data.slice(i,i+4).every((v,c)=>v===editor.original.data[i+c]);
    }),'Unselected pixels remain unchanged');
    await page.locator('[data-panel="light"]').click();
    assert.ok(await page.locator('[data-setting="exposure"]').isVisible());
    await page.locator('[data-setting="exposure"]').fill('20');
    await page.locator('[data-setting="exposure"]').dispatchEvent('change');
    await page.waitForFunction(()=>!editor.rendering);
    assert.ok(await page.evaluate(()=>editor.result.data[0]!==editor.original.data[0]),'Light controls affect the whole photo');
    await page.locator('[data-action="undo"]').click();await page.waitForFunction(()=>!editor.rendering);
    assert.equal(await page.inputValue('[data-setting="exposure"]'),'0');
    await page.locator('[data-tool="skin"]').click();
    if(process.env.EDITOR_SCREENSHOT){
      await page.evaluate(()=>{document.querySelector('.photo-editor aside').scrollTop=0;document.querySelector('.pe-layout').scrollTop=0;});
      await page.screenshot({path:process.env.EDITOR_SCREENSHOT.replace('.png',`-${viewport.width}.png`)});
    }
    // Download: the same picture to this device, in the format chosen, with the
    // library left alone (no request to the server is made for it).
    await page.selectOption('#pe-format','image/jpeg');
    assert.equal(await page.locator('#pe-quality-field').isVisible(),true,'JPEG has a quality');
    const [download]=await Promise.all([page.waitForEvent('download'),page.locator('[data-action="download"]').click()]);
    assert.match(download.suggestedFilename(),/-edited\.jpg$/);
    assert.equal(await page.evaluate(()=>window.saved),undefined,'a download must not save to the library');
    await page.selectOption('#pe-format','image/png');
    assert.equal(await page.locator('#pe-quality-field').isVisible(),false,'PNG has no quality');
    await page.locator('[data-action="save"]').click();await page.waitForFunction(()=>window.saved?.id===2);
    assert.deepEqual(errors,[]);console.log(`PASS ${viewport.width}×${viewport.height}: original/edited/split pixels, zoom, brushes, undo/redo, JPEG download, PNG export`);await page.close();
  }
} finally {await browser?.close();server.close();}
