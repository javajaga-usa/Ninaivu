/* Skin and hair in Sudar (the AI studio), in a real browser.
 *
 * The same engine and the same panel as the Photo Studio's, in the AI studio's
 * dialog. Standalone, like portrait_ui.mjs: a face drawn on a canvas, and a
 * stand-in for the one request that asks where the faces are.
 *
 * What the dialog replaced painted "skin" over anything the colour of skin and
 * "hair" over anything else that was not too bright — the wall behind a head as
 * much as the head. So the first thing checked is that the wall is left alone.
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { launch } from './harness.mjs';
import { SYNTHETIC } from './synthetic_portrait.mjs';

createRequire(import.meta.url)('playwright');
const root = path.resolve('ninaivu/static');

const PAGE = `<link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css"><link rel="stylesheet" href="/css/ai-playground.css"><script type="module">
  import { openPortraitStudio } from '/js/ai-playground/components/portrait.js';
  import * as i18n from '/js/i18n.js';
  ${SYNTHETIC}
  window.applied = null;
  if (new URLSearchParams(location.search).get('nohair')) window.analysis.faces[0].hair = { found: false, unsure: false };
  await i18n.use(new URLSearchParams(location.search).get('lang') || 'en', { remember: false });
  const image = new Image(); image.src = photoUrl; await image.decode();
  window.source = await createImageBitmap(image);
  openPortraitStudio(window.source, (bitmap) => { window.applied = bitmap; });
  window.ready = true;
</script>`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); res.end(PAGE); return; }
  const target = path.resolve(root, '.' + url.pathname.replace(/^\/static/, ''));
  if (!target.startsWith(root + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : target.endsWith('.json') ? 'application/json' : 'text/javascript');
    res.end(await fs.readFile(target));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;

let browser;
const failures = [];
let current = null;
const step = async (name, run) => {
  try { await run(); console.log(`  ok   ${name}`); } catch (error) {
    failures.push(name);
    console.log(`  FAIL ${name}\n       ${error.message.split('\n').slice(0, process.env.UI_DEBUG ? 12 : 1).join('\n       ')}`);
    if (process.env.UI_DEBUG && current) await current.screenshot({ path: `${process.env.UI_DEBUG}-ai-${failures.length}.png` });
  }
};

try {
  browser = await launch();
  const open = async ({ analysis = 'faces', query = '', viewport = { width: 1440, height: 900 } } = {}) => {
    const page = await browser.newPage({ viewport });
    page.errors = [];
    page.on('pageerror', (e) => page.errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errors.push(m.text()); });
    await page.route('**/static/i18n/*.json', async (route) => {
      const name = new URL(route.request().url()).pathname.split('/').pop();
      await route.fulfill({ contentType: 'application/json', body: await fs.readFile(path.join(root, 'i18n', name)) });
    });
    await page.route('**/api/portrait/analyse', async (route) => {
      if (analysis === 'none') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ size: [600, 800], faces: [], maps: null, reason: 'No face was found in this photograph.' }) });
        return;
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: await page.evaluate(() => JSON.stringify(window.analysis)) });
    });
    await page.goto(`${base}/${query}`);
    await page.waitForFunction(() => window.ready);
    current = page;
    return page;
  };
  const settle = (page) => page.waitForFunction(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 150)))));
  const canvasPixel = (page, x, y) => page.evaluate(([x, y]) => Array.from(document.querySelector('#ap-portrait-canvas').getContext('2d').getImageData(x, y, 1, 1).data.slice(0, 3)), [x, y]);

  console.log('Skin and hair in Sudar');
  const page = await open();

  await step('the dialog opens, looks for the faces and shows them', async () => {
    await page.waitForSelector('.ap-portrait-dialog[open] .pp-face:not(.pp-everyone)');
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="skin"] .pp-face').count(), 2);
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="skin"] [data-pp="improve"]').isVisible(), true);
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="skin"] [data-pp="refine"]').count(), 0, 'no brush here: that is the Photo Studio');
  });

  await step('nothing presented as a preset that lightens or whitens anybody', async () => {
    const text = (await page.locator('.ap-portrait-dialog').innerText()).toLowerCase();
    for (const word of ['porcelain', 'whiten', 'fair skin', 'glamour', 'hollywood', 'platinum']) assert.ok(!text.includes(word), `the dialog says "${word}"`);
  });

  await step('improving the faces retouches the face and leaves the wall alone', async () => {
    // The split slider starts in the middle: the right half is the retouched picture.
    await page.locator('.ap-portrait-dialog [data-compare]').fill('0');
    const before = await canvasPixel(page, 250, 390), wallBefore = await canvasPixel(page, 560, 20);
    await page.locator('.ap-portrait-dialog [data-panel-host="skin"] [data-pp="improve"]').click();
    await settle(page);
    const after = await canvasPixel(page, 250, 390), wallAfter = await canvasPixel(page, 560, 20);
    assert.ok(after[0] > before[0] + 10, `${before} → ${after}`);
    assert.deepEqual(wallAfter, wallBefore, 'the wall was changed');
    if (process.env.UI_SHOT) await page.screenshot({ path: `${process.env.UI_SHOT}-ai.png` });
  });

  await step('the before-and-after slider shows the original on its left', async () => {
    await page.locator('.ap-portrait-dialog [data-compare]').fill('100');
    const plain = await page.evaluate(() => {
      const c = Object.assign(document.createElement('canvas'), { width: 600, height: 800 });
      c.getContext('2d').drawImage(window.source, 0, 0);
      return Array.from(c.getContext('2d').getImageData(250, 390, 1, 1).data.slice(0, 3));
    });
    assert.deepEqual(await canvasPixel(page, 250, 390), plain);
    await page.locator('.ap-portrait-dialog [data-compare]').fill('0');
  });

  await step('hair has its own tab and its own tools', async () => {
    await page.locator('.ap-portrait-dialog [data-tab="hair"]').click();
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="hair"]').isVisible(), true);
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="skin"]').isVisible(), false);
    assert.equal(await page.locator('.ap-portrait-dialog [data-panel-host="hair"] .pp-slider[data-key]').count(), 3);
    const before = await canvasPixel(page, 300, 127);
    await page.locator('.ap-portrait-dialog [data-panel-host="hair"] [data-pp="colour"][data-hex="#7a3b22"]').click();
    await settle(page);
    const after = await canvasPixel(page, 300, 127);
    assert.ok(after[0] > before[0] + 3, `the hair took the colour: ${before} → ${after}`);
    await page.locator('.ap-portrait-dialog [data-tab="skin"]').click();
  });

  await step('applying retouches the photograph at full size and hands it back', async () => {
    // The dialog closes the bitmap it was given when it closes, so keep the plain picture.
    await page.evaluate(() => {
      window.plain = Object.assign(document.createElement('canvas'), { width: 600, height: 800 });
      window.plain.getContext('2d').drawImage(window.source, 0, 0);
    });
    await page.locator('.ap-portrait-dialog [data-apply-portrait]').click();
    await page.waitForFunction(() => window.applied);
    const result = await page.evaluate(() => {
      const bitmap = window.applied;
      const c = Object.assign(document.createElement('canvas'), { width: bitmap.width, height: bitmap.height });
      const x = c.getContext('2d'); x.drawImage(bitmap, 0, 0);
      const o = window.plain;
      return { size: [bitmap.width, bitmap.height], cheek: Array.from(x.getImageData(250, 390, 1, 1).data), plain: Array.from(o.getContext('2d').getImageData(250, 390, 1, 1).data),
               wall: Array.from(x.getImageData(560, 20, 1, 1).data), plainWall: Array.from(o.getContext('2d').getImageData(560, 20, 1, 1).data) };
    });
    assert.deepEqual(result.size, [600, 800]);
    assert.ok(result.cheek[0] > result.plain[0] + 10);
    assert.deepEqual(result.wall, result.plainWall);
  });

  assert.deepEqual(page.errors, [], 'no errors in the page');
  await page.close();

  await step('with no face in the photograph it says so, and offers nothing it cannot do', async () => {
    const bare = await open({ analysis: 'none' });
    await bare.waitForSelector('.ap-portrait-dialog .pp-state[data-state="none"]');
    assert.match(await bare.locator('.ap-portrait-dialog [data-panel-host="skin"] .pp-state').textContent(), /No face was found/);
    assert.equal(await bare.locator('.ap-portrait-dialog [data-panel-host="skin"] [data-pp="improve"]').isVisible(), false);
    assert.deepEqual(bare.errors, []);
    await bare.close();
  });

  await step('hair that was not found says so, and points to where it can be painted in', async () => {
    const bald = await open({ query: '?nohair=1' });
    await bald.waitForSelector('.ap-portrait-dialog[open] .pp-face:not(.pp-everyone)');
    await bald.locator('.ap-portrait-dialog [data-tab="hair"]').click();
    assert.match(await bald.locator('.ap-portrait-dialog [data-panel-host="hair"] .pp-needs').textContent(), /Photo Studio can paint it in/);
    await bald.close();
  });

  await step('it speaks Tamil when the language is Tamil', async () => {
    const ta = await open({ query: '?lang=ta' });
    await ta.waitForSelector('.ap-portrait-dialog[open] .pp-face:not(.pp-everyone)');
    const text = await ta.locator('.ap-portrait-dialog').innerText();
    for (const word of ['சருமமும் முடியும்', 'முகங்களை மேம்படுத்து', 'முகத்தின் வெளிச்சம்']) assert.ok(text.includes(word), `${word} is missing`);
    await ta.close();
  });

  await step('it fits a phone', async () => {
    const phone = await open({ viewport: { width: 390, height: 844 } });
    await phone.waitForSelector('.ap-portrait-dialog[open] .pp-face:not(.pp-everyone)');
    const box = await phone.locator('.ap-portrait-dialog').boundingBox();
    assert.ok(box.width <= 391, `the dialog is ${box.width} wide`);
    if (process.env.UI_SHOT) await phone.screenshot({ path: `${process.env.UI_SHOT}-ai-phone.png` });
    assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, 'no sideways scroll');
    await phone.close();
  });
} finally {
  await browser?.close();
  server.close();
}
if (failures.length) { console.error(`\n${failures.length} failed: ${failures.join('; ')}`); process.exit(1); }
console.log('\nPASS: skin and hair in the AI studio');
