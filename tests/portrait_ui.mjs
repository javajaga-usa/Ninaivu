/* Skin and Hair in the Photo Studio, in a real browser.
 *
 * Standalone: a tiny server hands out the app's scripts, the photograph is a face
 * drawn on a canvas, and the one request the tools make — "where are the faces?"
 * — is answered by a stand-in that draws the maps of that same face (what the
 * household's face detector and server/portrait.py would send). What is checked
 * is everything the browser is responsible for:
 *
 *   - the faces strip, and what each person is offered;
 *   - a setting for everyone, and a setting for one person alone;
 *   - a bindi, which no tool may touch;
 *   - that everything painted and everything known about the faces goes with the
 *     picture when it is turned or cropped, and with an undo;
 *   - painting by hand where the tools missed, and taking away where they erred;
 *   - the same retouch at full size when the picture is saved;
 *   - Tamil.
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

/** The page: a face, its maps, and an editor open on it. `lang` is the language the editor is built in. */
const PAGE = `<link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css"><script type="module">
  import { PhotoEditor } from '/js/editor.js';
  import * as i18n from '/js/i18n.js';
  ${SYNTHETIC}
  window.asked = 0;
  // ?cast=1: the same face, measured to have a yellow-green cast.
  if (new URLSearchParams(location.search).get('cast')) window.analysis.faces[0].skin = { lightness: 60, hue: 76, chroma: 26 };
  // ?nohair=1: the hair could not be told from what is behind it.
  if (new URLSearchParams(location.search).get('nohair')) window.analysis.faces[0].hair = { found: false, unsure: false };

  await i18n.use(new URLSearchParams(location.search).get('lang') || 'en', { remember: false });
  window.editor = new PhotoEditor({ id: 1, src: photoUrl, rotation: 0 }, (copy) => { window.saved = copy; });
  editor.open();
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
const shot = async (page, name) => { if (process.env.UI_SHOT) await page.screenshot({ path: `${process.env.UI_SHOT}-${name}.png` }); };
const step = async (name, run) => {
  try { await run(); console.log(`  ok   ${name}`); } catch (error) {
    failures.push(name);
    console.log(`  FAIL ${name}\n       ${error.message.split('\n').slice(0, process.env.UI_DEBUG ? 12 : 1).join('\n       ')}`);
    if (process.env.UI_DEBUG && current) await current.screenshot({ path: `${process.env.UI_DEBUG}-${failures.length}.png` });
  }
};

try {
  browser = await launch();
  const open = async (viewport = { width: 1440, height: 900 }, query = '', { analysis = true } = {}) => {
    const page = await browser.newPage({ viewport });
    page.errors = [];
    page.on('pageerror', (e) => page.errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) page.errors.push(m.text()); });
    await page.route('**/static/i18n/*.json', async (route) => {
      const name = new URL(route.request().url()).pathname.split('/').pop();
      await route.fulfill({ contentType: 'application/json', body: await fs.readFile(path.join(root, 'i18n', name)) });
    });
    await page.route('**/api/portrait/analyse', async (route) => {
      if (!analysis) { await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'needs the model', needs: 'faces' }) }); return; }
      assert.equal(route.request().headers()['content-type'], 'image/jpeg');
      assert.ok(route.request().postDataBuffer().length > 500, 'a picture was sent');
      await page.evaluate(() => { window.asked++; });
      const body = await page.evaluate(() => JSON.stringify(window.analysis));
      await route.fulfill({ status: 200, contentType: 'application/json', body });
    });
    await page.goto(`${base}/${query}`);
    await page.waitForFunction(() => window.editor?.ready);
    current = page;
    return page;
  };
  const settle = (page) => page.waitForFunction(() => !editor.rendering && !editor.pending);
  const pixel = (page, x, y) => page.evaluate(([x, y]) => {
    const i = (y * editor.preview.width + x) * 4;
    return Array.from(editor.result ? editor.result.data.slice(i, i + 3) : editor.original.data.slice(i, i + 3));
  }, [x, y]);
  const original = (page, x, y) => page.evaluate(([x, y]) => {
    const i = (y * editor.preview.width + x) * 4;
    return Array.from(editor.original.data.slice(i, i + 3));
  }, [x, y]);
  const same = (a, b) => a.every((v, i) => v === b[i]);
  const slide = async (page, panel, key, value) => {
    const input = page.locator(`[data-portrait="${panel}"] .pp-slider[data-key="${key}"] [data-pp="slider"]`);
    await input.fill(String(value));
    await input.dispatchEvent('change');
    await settle(page);
  };
  const FACE_CHEEK = [300 - 50, 330 + 60], BINDI = [300, 330 - 68], WALL = [20, 20], HAIR = [300, 127];

  // -------------------------------------------------------------------------
  console.log('Skin and Hair, desktop');
  const page = await open();

  await step('opening Skin looks for the faces, once, and shows them', async () => {
    await page.locator('[data-tool="skin"]').click();
    await page.waitForSelector('[data-portrait="skin"] .pp-face:not(.pp-everyone)');
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face').count(), 2, 'Everyone and one person');
    assert.equal(await page.evaluate(() => window.asked), 1);
    await page.locator('[data-tool="hair"]').click();
    await page.locator('[data-tool="skin"]').click();
    assert.equal(await page.evaluate(() => window.asked), 1, 'it does not ask again for a photograph it has already looked at');
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face.pp-on').getAttribute('data-face'), 'all');
    assert.equal(await page.locator('[data-portrait="skin"] [data-pp="improve"]').isVisible(), true);
  });

  await step('the faces are walked with the arrow keys', async () => {
    await page.locator('[data-portrait="skin"] .pp-face[data-face="all"]').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face.pp-on').getAttribute('data-face'), '1');
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face.pp-on').getAttribute('data-face'), 'all', 'and round again');
  });

  await step('nothing is changed until something is asked', async () => {
    await settle(page);
    assert.ok(same(await pixel(page, ...FACE_CHEEK), await original(page, ...FACE_CHEEK)));
  });

  await step('a slider moved for everyone changes the face and nothing else', async () => {
    await slide(page, 'skin', 'faceLight', 70);
    const lifted = await pixel(page, ...FACE_CHEEK), before = await original(page, ...FACE_CHEEK);
    assert.ok(lifted[0] > before[0] + 15, `${before} → ${lifted}`);
    assert.ok(same(await pixel(page, ...WALL), await original(page, ...WALL)), 'the wall was touched');
    assert.ok(same(await pixel(page, ...HAIR), await original(page, ...HAIR)), 'the hair was touched by a skin tool');
  });

  await step('face light keeps the colour of the skin', async () => {
    const [r, g, b] = await pixel(page, ...FACE_CHEEK), [r0, g0, b0] = await original(page, ...FACE_CHEEK);
    assert.ok(Math.abs(r / g - r0 / g0) < 0.12 && Math.abs(g / b - g0 / b0) < 0.25, 'the hue turned');
  });

  await step('a bindi is left exactly as it is, however the skin round it is asked to change', async () => {
    await slide(page, 'skin', 'smooth', 100);
    await slide(page, 'skin', 'even', 100);
    await slide(page, 'skin', 'shine', 100);
    await slide(page, 'skin', 'tone', -100);
    await slide(page, 'skin', 'faceLight', 0);
    assert.ok(same(await pixel(page, ...BINDI), await original(page, ...BINDI)), 'the bindi was changed');
    for (const key of ['smooth', 'even', 'shine', 'tone']) await slide(page, 'skin', key, 0);
  });

  await step("one person's setting is theirs alone", async () => {
    await page.locator('[data-portrait="skin"] .pp-face[data-face="1"]').click();
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face.pp-on').getAttribute('data-face'), '1');
    await slide(page, 'skin', 'smooth', 55);
    assert.equal(await page.evaluate(() => editor.session.portrait.faces[1].smooth), 55);
    assert.equal(await page.evaluate(() => editor.session.portrait.all.smooth ?? 0), 0, 'Everyone did not move');
    assert.equal(await page.locator('[data-portrait="skin"] .pp-slider[data-key="smooth"] .pp-own').isVisible(), true, 'a dot says it is theirs');
    await page.locator('[data-portrait="skin"] .pp-slider[data-key="smooth"] [data-pp="clear"]').click();
    assert.equal(await page.evaluate(() => editor.session.portrait.faces[1]), undefined);
    await page.locator('[data-portrait="skin"] .pp-face[data-face="all"]').click();
  });

  await step('Improve faces gives each person what stood out about them, in one undoable step', async () => {
    await slide(page, 'skin', 'faceLight', 0);
    await page.evaluate(() => {          // history is capped, so count the steps taken rather than its length
      const remember = editor.remember.bind(editor);
      window.steps = 0;
      editor.remember = () => { window.steps++; remember(); };
    });
    await page.locator('[data-portrait="skin"] [data-pp="improve"]').click();
    await settle(page);
    assert.deepEqual(await page.evaluate(() => editor.session.portrait.faces[1]), { faceLight: 60, balance: 40, smooth: 20, hairDetail: 30 });
    assert.equal(await page.evaluate(() => window.steps), 1, 'one step');
    assert.match(await page.locator('.pe-status').textContent(), /Improved one person/);
    await page.locator('[data-portrait="skin"] .pp-face[data-face="1"]').click();
    assert.equal(await page.inputValue('[data-portrait="skin"] .pp-slider[data-key="faceLight"] input'), '60');
    assert.match(await page.locator('[data-portrait="skin"] .pp-needs').textContent(), /Nothing stands out|Stands out/);
    await page.locator('[data-action="undo"]').click();
    await settle(page);
    assert.equal(await page.evaluate(() => editor.session.portrait.faces[1]), undefined, 'undo took it back');
    await page.locator('[data-action="redo"]').click();
    await settle(page);
    assert.equal(await page.evaluate(() => editor.session.portrait.faces[1].faceLight), 60);
    await shot(page, 'skin-improved');
    await page.locator('[data-portrait="skin"] [data-pp="reset-face"]').click();
    await page.locator('[data-portrait="skin"] .pp-face[data-face="all"]').click();
    await settle(page);
  });

  await step('the faces are taken with the picture when it is turned', async () => {
    await slide(page, 'skin', 'faceLight', 90);
    await page.locator('[data-panel="crop"]').click();
    await page.locator('[data-turn="90"]').click();
    await settle(page);
    // A quarter turn clockwise takes (x, y) to (height - y, x).
    const moved = [800 - FACE_CHEEK[1], FACE_CHEEK[0]], where = await page.evaluate(() => [editor.preview.width, editor.preview.height]);
    assert.deepEqual(where, [800, 600]);
    const now = await pixel(page, ...moved), before = await original(page, ...moved);
    assert.ok(now[0] > before[0] + 15, `the cheek, turned: ${before} → ${now}`);
    const stale = await pixel(page, ...FACE_CHEEK), staleBefore = await original(page, ...FACE_CHEEK);
    assert.ok(same(stale, staleBefore), 'where the face used to be was retouched');
    await page.locator('[data-action="undo"]').click();
    await settle(page);
    const back = await pixel(page, ...FACE_CHEEK), backBefore = await original(page, ...FACE_CHEEK);
    assert.ok(back[0] > backBefore[0] + 15, 'undoing the turn puts the face back under its retouch');
  });

  await step('a face cropped out of the picture leaves the strip', async () => {
    await page.evaluate(() => { editor.remember(); editor.geometry.crop = { x: 0, y: 0.6, w: 1, h: 0.4 }; editor.buildStage(); editor.render(); });
    await page.locator('[data-tool="skin"]').click();
    await settle(page);
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face:not(.pp-everyone)').count(), 0);
    await page.locator('[data-action="undo"]').click();
    await settle(page);
    assert.equal(await page.locator('[data-portrait="skin"] .pp-face:not(.pp-everyone)').count(), 1);
  });

  await step('hands can add what was missed and take away what was wrong', async () => {
    await slide(page, 'skin', 'faceLight', 0);
    await slide(page, 'skin', 'smooth', 0);
    await slide(page, 'skin', 'tone', 100);
    // Take the left half of the face out of it.
    await page.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="erase"]').click();
    await page.locator('[data-portrait="skin"] [data-pp="size"]').fill('220');
    const box = await page.locator('#pe-mask').boundingBox();
    const at = (x, y) => [box.x + box.width * x / 600, box.y + box.height * y / 800];
    await page.mouse.move(...at(235, 330)); await page.mouse.down(); await page.mouse.move(...at(235, 400), { steps: 4 }); await page.mouse.up();
    await settle(page);
    const left = await pixel(page, 235, 360), leftBefore = await original(page, 235, 360);
    assert.ok(same(left, leftBefore), `the erased side was still retouched: ${leftBefore} → ${left}`);
    const right = await pixel(page, 365, 360), rightBefore = await original(page, 365, 360);
    assert.ok(!same(right, rightBefore), 'the rest of the face still is');
    // Put something where there is no face: a patch of the wall becomes skin, and takes the same settings.
    await page.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="add"]').click();
    await page.locator('[data-portrait="skin"] [data-pp="edge"]').uncheck();
    await page.mouse.move(...at(90, 650)); await page.mouse.down(); await page.mouse.move(...at(120, 650), { steps: 3 }); await page.mouse.up();
    await settle(page);
    assert.ok(!same(await pixel(page, 100, 650), await original(page, 100, 650)), 'painted skin was retouched with the shared settings');
    await shot(page, 'overlay');
    assert.ok(same(await pixel(page, ...WALL), await original(page, ...WALL)), 'the rest of the wall was not');
    await page.locator('[data-portrait="skin"] [data-pp="clear-paint"]').click();
    await settle(page);
    assert.ok(!same(await pixel(page, 235, 360), await original(page, 235, 360)), 'clearing the painting gives the face back');
    await page.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="add"]').click();     // and stops painting
  });

  await step('painting is undone with undo', async () => {
    await page.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="erase"]').click();
    const box = await page.locator('#pe-mask').boundingBox();
    await page.mouse.move(box.x + box.width * 0.6, box.y + box.height * 0.45); await page.mouse.down(); await page.mouse.up();
    assert.equal(await page.evaluate(() => editor.session.paint.skinErase.any()), true);
    await page.locator('[data-action="undo"]').click();
    assert.equal(await page.evaluate(() => editor.session.paint.skinErase.any()), false);
    await page.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="erase"]').click();
  });

  await step('hair has its own tools, and the face is left alone by them', async () => {
    await slide(page, 'skin', 'tone', 0);
    await page.locator('[data-tool="hair"]').click();
    assert.equal(await page.locator('[data-portrait="hair"] .pp-slider[data-key]').count(), 3);
    await page.locator('[data-portrait="hair"] [data-pp="colour"][data-hex="#7a3b22"]').click();
    await settle(page);
    assert.equal(await page.inputValue('[data-portrait="hair"] .pp-slider[data-key="hairAmount"] input'), '50', 'choosing a colour moves the strength off nothing');
    const hair = await pixel(page, ...HAIR), hairBefore = await original(page, ...HAIR);
    assert.ok(hair[0] > hairBefore[0] + 3, `the hair took the colour: ${hairBefore} → ${hair}`);
    assert.ok(same(await pixel(page, ...FACE_CHEEK), await original(page, ...FACE_CHEEK)), 'a hair tool reached the face');
    await shot(page, 'hair');
  });

  await step('the picture that is saved is retouched the same way, at full size', async () => {
    await page.locator('[data-tool="skin"]').click();
    await slide(page, 'skin', 'faceLight', 80);
    const result = await page.evaluate(async () => {
      const { blob } = await editor.renderExport();
      const bitmap = await createImageBitmap(blob);
      const c = Object.assign(document.createElement('canvas'), { width: bitmap.width, height: bitmap.height });
      const x = c.getContext('2d'); x.drawImage(bitmap, 0, 0);
      const cheek = Array.from(x.getImageData(350, 390, 1, 1).data), wall = Array.from(x.getImageData(20, 20, 1, 1).data);
      const s = editor.stage.getContext('2d');
      return { size: [bitmap.width, bitmap.height], cheek, wall, plainCheek: Array.from(s.getImageData(350, 390, 1, 1).data), plainWall: Array.from(s.getImageData(20, 20, 1, 1).data) };
    });
    assert.deepEqual(result.size, [600, 800]);
    assert.ok(result.cheek[0] > result.plainCheek[0] + 15, `${result.plainCheek} → ${result.cheek}`);
    assert.ok(same(result.wall.slice(0, 3), result.plainWall.slice(0, 3)), 'the wall was touched in the export');
  });

  await step('it fits and works in the window', async () => {
    const fits = await page.evaluate(() => {
      const strip = document.querySelector('[data-portrait="skin"] .pp-faces');
      const aside = document.querySelector('.photo-editor aside').getBoundingClientRect();
      return strip.getBoundingClientRect().right <= aside.right + 1;
    });
    assert.ok(fits, 'the strip stays inside the panel');
  });
  assert.deepEqual(page.errors, [], 'no errors in the page');
  await page.close();

  // -------------------------------------------------------------------------
  console.log('Skin and Hair, small screen');
  await step('the same, on a phone', async () => {
    const phone = await open({ width: 390, height: 844 });
    await phone.locator('[data-tool="skin"]').click();
    await phone.waitForSelector('[data-portrait="skin"] .pp-face:not(.pp-everyone)');
    await slide(phone, 'skin', 'faceLight', 70);
    await shot(phone, 'phone');
    const lifted = await pixel(phone, ...FACE_CHEEK), before = await original(phone, ...FACE_CHEEK);
    assert.ok(lifted[0] > before[0] + 15);
    const overflow = await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
    assert.equal(overflow, false, 'no sideways scroll');
    assert.deepEqual(phone.errors, []);
    await phone.close();
  });

  // -------------------------------------------------------------------------
  console.log('When the faces cannot be looked for');
  await step('without the face model it says so and the brush still works', async () => {
    const bare = await open({ width: 1440, height: 900 }, '', { analysis: false });
    await bare.locator('[data-tool="skin"]').click();
    await bare.waitForSelector('.pp-state[data-state="unavailable"]');
    assert.match(await bare.locator('[data-portrait="skin"] .pp-state').textContent(), /AI models/);
    assert.equal(await bare.locator('[data-portrait="skin"] [data-pp="improve"]').isVisible(), false);
    await bare.locator('[data-portrait="skin"] [data-pp="refine"][data-mode="add"]').click();
    await bare.locator('[data-portrait="skin"] [data-pp="edge"]').uncheck();
    const box = await bare.locator('#pe-mask').boundingBox();
    await bare.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.45); await bare.mouse.down(); await bare.mouse.move(box.x + box.width * 0.56, box.y + box.height * 0.45, { steps: 3 }); await bare.mouse.up();
    await slide(bare, 'skin', 'smooth', 100);
    assert.equal(await bare.evaluate(() => editor.session.paint.skinAdd.any()), true);
    assert.deepEqual(bare.errors, []);
    await bare.close();
  });

  await step('hair that was not found says so, and says what to do', async () => {
    const bald = await open({ width: 1440, height: 900 }, '?nohair=1');
    await bald.locator('[data-tool="hair"]').click();
    await bald.waitForSelector('[data-portrait="hair"] .pp-face:not(.pp-everyone)');
    assert.match(await bald.locator('[data-portrait="hair"] .pp-needs').textContent(), /Paint it in/);
    await bald.locator('[data-tool="skin"]').click();
    assert.ok(!/Hair could not/.test(await bald.locator('[data-portrait="skin"]').innerText()), 'skin does not say it');
    await bald.close();
  });

  // -------------------------------------------------------------------------
  console.log('Natural skin tone');
  await step('skin that already looks natural is left alone', async () => {
    const plain = await open();
    await plain.locator('[data-panel="colour"]').click();
    await plain.locator('[data-action="natural-tone"]').click();
    await plain.waitForFunction(() => /already looks natural/.test(document.querySelector('.pe-status').textContent));
    assert.deepEqual(await plain.evaluate(() => [editor.settings.warmth, editor.settings.tint]), [0, 0]);
    await plain.close();
  });
  await step('a cast on the faces is turned back, by two sliders that can be moved back', async () => {
    const cast = await open({ width: 1440, height: 900 }, '?cast=1');
    await cast.locator('[data-panel="colour"]').click();
    await cast.locator('[data-action="natural-tone"]').click();
    await cast.waitForFunction(() => /Turned the colour cast/.test(document.querySelector('.pe-status').textContent));
    const [warmth, tint] = await cast.evaluate(() => [editor.settings.warmth, editor.settings.tint]);
    assert.ok(warmth !== 0 || tint !== 0, 'something moved');
    assert.equal(await cast.evaluate(() => editor.settings.exposure + editor.settings.contrast + editor.settings.saturation), 0, 'nothing else did');
    assert.equal(Number(await cast.inputValue('[data-setting="warmth"]')), warmth, 'and the slider shows it');
    await settle(cast);
    await cast.locator('[data-action="undo"]').click();
    assert.deepEqual(await cast.evaluate(() => [editor.settings.warmth, editor.settings.tint]), [0, 0]);
    await cast.close();
  });
  await step('without the face model it says what to install', async () => {
    const bare = await open({ width: 1440, height: 900 }, '', { analysis: false });
    await bare.locator('[data-panel="colour"]').click();
    await bare.locator('[data-action="natural-tone"]').click();
    await bare.waitForFunction(() => /AI models/.test(document.querySelector('.pe-status').textContent));
    await bare.close();
  });

  // -------------------------------------------------------------------------
  console.log('Tamil');
  await step('the panel speaks Tamil when the language is Tamil', async () => {
    const ta = await open({ width: 1440, height: 900 }, '?lang=ta');
    await ta.locator('[data-tool="skin"]').click();
    await ta.waitForSelector('[data-portrait="skin"] .pp-face:not(.pp-everyone)');
    const text = await ta.locator('[data-portrait="skin"]').innerText();
    for (const word of ['முகங்களை மேம்படுத்து', 'முகத்தின் வெளிச்சம்', 'மென்மையாக்கு', 'அனைவரும்']) assert.ok(text.includes(word), `${word} is missing`);
    assert.ok(!/Face light|Improve faces|Everyone/.test(text), 'English is still showing');
    await ta.locator('[data-portrait="skin"] [data-pp="improve"]').click();
    await shot(ta, 'tamil');
    await ta.close();
  });
} finally {
  await browser?.close();
  server.close();
}
if (failures.length) { console.error(`\n${failures.length} failed: ${failures.join('; ')}`); process.exit(1); }
console.log('\nPASS: skin and hair in the Photo Studio');
