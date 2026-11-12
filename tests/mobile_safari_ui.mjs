/** iPhone touch journeys and editor exports, in WebKit or Chromium.
 * The native OS share sheet is stubbed; activation expiry and retry are real UI paths. */
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { launch, playwright, signInAtHome } from './harness.mjs';

const root = path.resolve('ninaivu/static');
const server = http.createServer(async (request, response) => {
  if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html');
    response.end(`<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
      <link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css">
      <script type="module">
        import {PhotoEditor} from '/js/editor.js';
        const c=document.createElement('canvas');c.width=120;c.height=160;
        c.getContext('2d').fillRect(0,0,120,160);
        window.editor=new PhotoEditor({id:1,filename:'Amma.jpg',src:c.toDataURL()},()=>{});
        editor.open();
      </script>`);
    return;
  }
  const target = path.resolve(root, '.' + request.url.split('?')[0]);
  if (!target.startsWith(root + path.sep)) { response.writeHead(404); response.end(); return; }
  try {
    response.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : target.endsWith('.json') ? 'application/json' : 'text/javascript');
    response.end(await fs.readFile(target));
  } catch { response.writeHead(404); response.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const { defaultBrowserType: _engine, ...iPhone } = playwright().devices['iPhone 13'];
const browser = await launch();
const errors = [];
try {
  const context = await browser.newContext({ ...iPhone, serviceWorkers: 'block' });
  context.setDefaultTimeout(15000);
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await signInAtHome(page);
  console.log('PASS: the iPhone gallery opens');
  await page.waitForSelector('#grid .cell');
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.locator('#more-btn').tap();
  await page.locator('#upload-btn').tap();
  await page.waitForSelector('#upload-modal:not([hidden])');
  const chooserPromise = page.waitForEvent('filechooser');
  await page.locator('#upload-dropzone').tap();
  const chooser = await chooserPromise;
  console.log('PASS: a touch on Upload opens the phone file chooser');
  await page.route('**/api/upload', route => route.fulfill({ json: { total: 1, uploaded: [{ filename: 'phone.png', rel_path: 'phone.png' }] } }));
  await chooser.setFiles({ name: 'phone.png', mimeType: 'image/png', buffer: Buffer.from('test') });
  await page.waitForFunction(() => document.querySelector('#upload-status').textContent.includes('Uploaded'));
  await page.locator('#upload-close').tap();
  await page.locator('#grid .cell').first().tap();
  await page.waitForFunction(() => document.querySelector('#v-name').textContent.trim());
  assert.ok(await page.locator('#v-share').evaluate(node => {
    const box = node.getBoundingClientRect();
    return box.width >= 44 && box.height >= 44;
  }), 'the viewer offers a full touch target for sharing');
  await page.locator('#v-share').tap();
  await page.waitForSelector('#share-modal:not([hidden])');
  assert.equal(await page.locator('#ios-install-banner').isVisible(), false, 'the install banner must step aside for sharing');
  await page.setViewportSize({ width: 390, height: 420 });
  await page.locator('#share-password').fill('123456');
  assert.ok(await page.locator('#share-modal .modal-card').evaluate(node => {
    const box = node.getBoundingClientRect();
    return box.height <= innerHeight - 20;
  }), 'the share dialog fits a reduced-height viewport');
  assert.ok(await page.locator('#share-password').evaluate(node => parseFloat(getComputedStyle(node).fontSize) >= 16));
  await page.locator('#share-create-btn').tap();
  await page.waitForSelector('#share-link-result:not([hidden])');
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', {
    configurable: true, value: { writeText: async () => { throw new Error('denied'); } },
  }));
  await page.locator('#share-copy-btn').tap();
  assert.ok(await page.locator('#share-url-text').evaluate(node => node.selectionEnd - node.selectionStart === node.value.length));
  await page.locator('#share-close').tap();
  console.log('PASS: iPhone touch upload, reduced-height sharing, legible fields and manual copy fallback');

  for (const viewport of [{ width: 390, height: 844 }, { width: 844, height: 390 }, { width: 375, height: 667 }, { width: 667, height: 375 }]) {
    const editorPage = await context.newPage();
    editorPage.on('pageerror', error => errors.push(error.message));
    await editorPage.setViewportSize(viewport);
    await editorPage.goto(`http://127.0.0.1:${server.address().port}/`);
    await editorPage.waitForFunction(() => window.editor?.ready);
    await editorPage.evaluate(() => {
      window.shared = [];
      Object.defineProperty(navigator, 'canShare', { configurable: true, value: () => true });
      Object.defineProperty(navigator, 'share', { configurable: true, value: async data => {
        shared.push(data.files[0].name);
        if (shared.length === 1) throw Object.assign(new Error('activation expired'), { name: 'NotAllowedError' });
      } });
    });
    const fits = await editorPage.locator('.photo-editor').evaluate(node => {
      const box = node.getBoundingClientRect();
      return box.width <= innerWidth + 1 && box.height <= innerHeight + 1;
    });
    assert.ok(fits, `${viewport.width}×${viewport.height}: editor fits the phone`);
    await editorPage.locator('[data-panel="colour"]').tap();
    await editorPage.locator('[data-setting="saturation"]').fill('10');
    await editorPage.locator('[data-setting="saturation"]').dispatchEvent('change');
    await editorPage.waitForFunction(() => !editor.rendering);
    if (process.env.MOBILE_REVIEW_SHOTS) {
      await editorPage.screenshot({ path: `${process.env.MOBILE_REVIEW_SHOTS}/iphone-editor-${viewport.width}.png` });
    }
    const url = editorPage.url();
    await editorPage.locator('[data-action="download"]').tap();
    const retry = editorPage.locator('.photo-editor footer .save-ready .btn.primary');
    await retry.waitFor();
    await retry.tap();
    await editorPage.waitForFunction(() => shared.length === 2);
    assert.deepEqual(await editorPage.evaluate(() => shared), ['Amma-edited.png', 'Amma-edited.png']);
    assert.equal(editorPage.url(), url);
    assert.equal(await editorPage.locator('.photo-editor').isVisible(), true);
    assert.equal(await editorPage.locator('.save-ready').count(), 0);
    console.log(`PASS: ${viewport.width}×${viewport.height} iPhone editor export retries inside the dialog and keeps the app open`);
    await editorPage.close();
  }
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
