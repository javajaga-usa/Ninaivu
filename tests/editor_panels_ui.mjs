/* The Photo Studio's editing panels, in a browser: Light with its histogram,
 * Colour with the mixer and black and white, the Curve, Detail, Looks, the flips
 * and the export size. Standalone: a picture is painted in the page, so no
 * household photographs or running app are needed.
 *
 * Run with `node tests/editor_panels_ui.mjs`, or through run_browser_tests.py.
 * NODE_PATH may point to a bundled installation of Playwright. */
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { launch } from './harness.mjs';

const root = path.resolve('ninaivu/static');
// Sky above, grass below, a brown face, and a red silk sari in front: the colours the panels are about.
const page = `<link rel="stylesheet" href="/css/style.css"><link rel="stylesheet" href="/css/editor.css"><script type="module">
  import { PhotoEditor } from '/js/editor.js';
  const c=document.createElement('canvas');c.width=900;c.height=600;const x=c.getContext('2d');
  const g=x.createLinearGradient(0,0,0,420);g.addColorStop(0,'#cfe0f2');g.addColorStop(1,'#8aa0b8');x.fillStyle=g;x.fillRect(0,0,900,420);
  x.fillStyle='#4a6b2f';x.fillRect(0,420,900,180);
  x.fillStyle='rgb(141,85,60)';x.beginPath();x.ellipse(450,300,110,150,0,0,7);x.fill();
  x.fillStyle='#b01e2c';x.fillRect(330,430,240,170);
  x.fillStyle='#f0d020';x.fillRect(20,20,60,60);
  window.editor=new PhotoEditor({id:1,src:c.toDataURL(),rotation:0,filename:'family.jpg'},()=>{});editor.open();
</script>`;
const server = http.createServer(async (req, res) => {
  if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); res.end(page); return; }
  const target = path.resolve(root, '.' + req.url.split('?')[0]);
  if (!target.startsWith(root + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader('Content-Type', target.endsWith('.css') ? 'text/css' : target.endsWith('.json') ? 'application/json' : 'text/javascript');
    res.end(await fs.readFile(target));
  } catch { res.writeHead(404); res.end(); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));

/** Mean colour of a rectangle of the edited preview, in the preview's own pixels, as fractions of it. */
const region = (p, fx0, fy0, fx1, fy1, which = 'result') => p.evaluate(([fx0, fy0, fx1, fy1, which]) => {
  const img = editor[which] || editor.original, W = img.width, H = img.height, s = [0, 0, 0];
  let n = 0;
  for (let y = Math.floor(fy0 * H); y < Math.floor(fy1 * H); y++) {
    for (let x = Math.floor(fx0 * W); x < Math.floor(fx1 * W); x++) {
      const i = (y * W + x) * 4; s[0] += img.data[i]; s[1] += img.data[i + 1]; s[2] += img.data[i + 2]; n++;
    }
  }
  return s.map((v) => v / n);
}, [fx0, fy0, fx1, fy1, which]);
const settle = (p) => p.waitForFunction(() => !editor.rendering && !editor.pending);
const set = async (p, key, value) => {
  const input = p.locator(`[data-setting="${key}"]`);
  await input.fill(String(value)); await input.dispatchEvent('change'); await settle(p);
};
const FACE = [0.46, 0.45, 0.54, 0.55], GRASS = [0.05, 0.8, 0.25, 0.95], CORNER = [0, 0, 0.04, 0.04];

let browser;
try {
  browser = await launch();
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const p = await browser.newPage({ viewport });
    const errors = [];
    p.on('pageerror', (e) => errors.push(e.message));
    await p.goto(`http://127.0.0.1:${server.address().port}/`);
    await p.waitForFunction(() => window.editor?.ready);
    const label = `${viewport.width}×${viewport.height}`;

    // -- Light: the histogram is drawn, and shadows lift the face ------------
    await set(p, 'exposure', 10);
    const inked = await p.evaluate(() => {
      const c = document.querySelector('.pe-histogram'), d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      return d.some((v, i) => i % 4 === 3 && v > 0);
    });
    assert.ok(inked, `${label}: the histogram is drawn`);
    await set(p, 'exposure', 0);
    const before = await region(p, ...FACE, 'original');
    await set(p, 'shadows', 80);
    const lifted = await region(p, ...FACE);
    assert.ok(lifted[1] > before[1] + 3, `${label}: shadows lift the face ${before[1]} → ${lifted[1]}`);
    await p.locator('[data-setting="shadows"]').dblclick();
    await settle(p);
    assert.equal(await p.evaluate(() => editor.settings.shadows), 0, 'a double click rests the slider');

    // -- Curve: a point added lightens the midtones; undo takes it away -------
    await p.locator('[data-panel="curve"]').click();
    const box = await p.locator('#pe-curve').boundingBox();
    await p.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await p.mouse.down(); await p.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.32, { steps: 4 }); await p.mouse.up();
    await settle(p);
    const curve = await p.evaluate(() => editor.settings.curve.rgb);
    assert.equal(curve.length, 3, `${label}: one point added: ${JSON.stringify(curve)}`);
    assert.ok(curve[1][1] > 0.6, 'and lifted');
    assert.ok((await region(p, ...FACE))[1] > before[1] + 8, 'the midtones are lighter');
    await p.locator('[data-channel="r"]').click();
    assert.equal(await p.locator('[data-channel="r"]').getAttribute('aria-pressed'), 'true');
    await p.locator('[data-channel="rgb"]').click();
    await p.locator('[data-action="undo"]').click(); await settle(p);
    assert.equal(await p.evaluate(() => editor.settings.curve.rgb.length), 2, 'undo puts the straight line back');

    // -- Colour: the mixer drains the grass and leaves the face ---------------
    await p.locator('[data-panel="colour"]').click();
    await p.locator('[data-mixer="sat"]').click();
    assert.ok(await p.locator('[data-setting="satGreen"]').isVisible(), 'the saturation sliders are shown');
    assert.ok(!(await p.locator('[data-setting="hueGreen"]').isVisible()), 'and the hue sliders are not');
    await set(p, 'satGreen', -100);
    const grass = await region(p, ...GRASS);
    assert.ok(Math.max(...grass) - Math.min(...grass) < 12, `${label}: the grass is grey: ${grass}`);
    const face = await region(p, ...FACE), faceBefore = await region(p, ...FACE, 'original');
    assert.ok(face.every((v, i) => Math.abs(v - faceBefore[i]) < 3), 'and the face is as it was');
    await p.locator('[data-toggle="mono"]').check(); await settle(p);
    const grey = await region(p, ...FACE);
    assert.ok(Math.max(...grey) - Math.min(...grey) < 3, 'black and white');
    assert.ok(await p.locator('#pe-mono-hint').isVisible());
    await p.locator('[data-action="reset"]').click(); await settle(p);

    // -- Looks: pictures to choose between, laid over the sliders -------------
    await p.locator('[data-panel="looks"]').click();
    const drawn = await p.evaluate(() => [...document.querySelectorAll('[data-look] canvas')].every((c) => {
      const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
      return d.some((v, i) => i % 4 === 3 && v > 0);
    }));
    assert.ok(drawn, `${label}: every look has its small picture`);
    await p.locator('[data-look="festival"]').click(); await settle(p);
    assert.equal(await p.locator('[data-look="festival"]').getAttribute('aria-pressed'), 'true');
    assert.ok((await p.locator('#pe-look-about').textContent()).length > 10, 'the look is described');
    const silkBefore = await region(p, 0.4, 0.8, 0.6, 0.95, 'original'), silk = await region(p, 0.4, 0.8, 0.6, 0.95);
    assert.notDeepEqual(silk, silkBefore, 'the look changes the photograph');
    await set(p, 'lookAmount', 0);
    assert.deepEqual(await region(p, 0.4, 0.8, 0.6, 0.95), silkBefore, 'at no strength the look is nothing');
    await p.locator('[data-look=""]').click(); await settle(p);
    assert.equal(await p.evaluate(() => editor.settings.look), '');

    // -- Detail: a vignette darkens the corners ------------------------------
    await p.locator('[data-panel="detail"]').click();
    const corner = await region(p, ...CORNER, 'original');
    await set(p, 'vignette', 80);
    assert.ok((await region(p, ...CORNER))[2] < corner[2] - 20, `${label}: the corners darken`);
    await set(p, 'vignette', 0);
    await set(p, 'sharpenRadius', 20);
    assert.equal(await p.locator('[data-value="sharpenRadius"]').textContent(), '2.0', 'a radius is shown in pixels');

    // -- Crop: flipping across mirrors the photograph -------------------------
    await p.locator('[data-panel="crop"]').click();
    await p.locator('[data-flip="h"]').click(); await settle(p);
    assert.equal(await p.locator('[data-flip="h"]').getAttribute('aria-pressed'), 'true');
    // The yellow square painted in the top left is now in the top right.
    const right = await region(p, 0.93, 0.05, 0.96, 0.1, 'original'), left = await region(p, 0.04, 0.05, 0.07, 0.1, 'original');
    assert.ok(right[0] > 200 && right[2] < 100 && left[2] > 150, `the picture is mirrored: ${right} / ${left}`);
    await p.locator('[data-action="undo"]').click(); await settle(p);
    assert.equal(await p.evaluate(() => editor.geometry.flipH), false, 'undo unflips');

    // -- Export: a reduced size is honoured -----------------------------------
    await p.selectOption('#pe-export-size', '1080');
    await p.selectOption('#pe-format', 'image/jpeg');
    const size = await p.evaluate(async () => {
      const { blob } = await editor.renderExport();
      const bitmap = await createImageBitmap(blob);
      return [bitmap.width, bitmap.height, blob.type];
    });
    assert.deepEqual(size, [900, 600, 'image/jpeg'], 'a photograph smaller than the limit is not enlarged');
    await p.evaluate(() => { const o = document.querySelector('#pe-export-size'); o.append(new Option('600', '600')); o.value = '600'; });
    const reduced = await p.evaluate(async () => { const { blob } = await editor.renderExport(); const b = await createImageBitmap(blob); return [b.width, b.height]; });
    assert.deepEqual(reduced, [600, 400], 'the long edge is brought to the chosen size');

    assert.deepEqual(errors, [], `${label}: no page errors`);
    await p.evaluate(() => { editor.dirty = false; });
    await p.close();
    console.log(`PASS ${label}: histogram, shadows, curve, mixer, black and white, looks, vignette, flip, export size`);
  }
} finally {
  await browser?.close();
  server.close();
}
