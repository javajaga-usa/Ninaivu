/**
 * No tile is ever empty, however fast the scroll (grid.js, placeholder.js).
 *
 * Thumbnails are held back here as a phone far from home gets them — each
 * one a second late — and the grid is flung through stretches it has never
 * shown. Every tile on screen must show its photograph or a blurred preview
 * of it, never its bare colour, on every frame once the first previews have
 * come; the previews must come a few hundred to a request, not one request a
 * tile; and the photograph must still replace the preview when it lands.
 *
 *   node tests/scroll_previews_ui.mjs   (wants the family app up with a library
 *                                        of a few thousand photographs)
 * @fixture busy
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
// No service worker: its fetches would go round the slow thumbnails set below.
const context = await b.newContext({
  viewport: { width: 1200, height: 800 }, deviceScaleFactor: 2, serviceWorkers: 'block',
});
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));
// Keep hold of the grid: the app's own scroll listener is the first thing to touch it.
await page.addInitScript(() => {
  const add = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function addEventListener(...rest) {
    if (!window.__grid && this && this.layout && this.mounted instanceof Map) window.__grid = this;
    return add.apply(this, rest);
  };
});

await signInAtHome(page);
const tallEnough = async () => {
  for (let i = 0; i < 90; i++) {
    const height = await page.evaluate(() => (window.__grid?.layout.cells.length > 1500
      ? document.querySelector('#grid').offsetHeight : 0));
    if (height > 30000) return height;
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: 'networkidle' });
  }
  return 0;
};
const height = await tallEnough();
ok('the gallery holds enough photographs to fling through', height > 30000, `height=${height}`);

// From here, every thumbnail is a second late.
let slow = true;
const previewRequests = [];
await page.route('**/api/thumb/**', async (route) => {
  if (slow) await new Promise((resolve) => setTimeout(resolve, 1000));
  route.continue().catch(() => {});
});
page.on('request', (request) => {
  const url = request.url();
  if (url.includes('/api/placeholders')) {
    previewRequests.push(new URL(url).searchParams.get('ids').split(',').length);
  }
});

/** What the tiles on screen show, by area. */
const sampler = () => {
  window.__sample = () => {
    const grid = window.__grid;
    const top = grid.scroller.scrollTop;
    const bottom = top + grid.scroller.clientHeight;
    const counts = { photo: 0, preview: 0, empty: 0 };
    for (const cell of grid.layout.cells) {
      if (cell.y > bottom) break;
      if (cell.y + cell.h <= top || (cell.flags & 2) === 0) continue;
      const area = cell.w * (Math.min(bottom, cell.y + cell.h) - Math.max(top, cell.y));
      const node = grid.mounted.get(cell.n);
      const img = node && node.firstElementChild;
      if (img && img.classList.contains('ready') && img.complete && img.naturalWidth > 0) counts.photo += area;
      else if (node && /url\(/.test(node.style.backgroundImage || '')) counts.preview += area;
      else counts.empty += area;
    }
    return counts;
  };
};
await page.evaluate(sampler);

/* -- a jump: previews before photographs ------------------------------ */

await page.evaluate(() => { window.__grid.scroller.scrollTop = 9000; });
await page.waitForFunction(() => {
  const c = window.__sample();
  return c.empty === 0 && c.preview > 0;
}, null, { timeout: 4000, polling: 30 }).catch(() => {});
const afterJump = await page.evaluate(() => window.__sample());
ok('a jump far away shows every tile blurred before its photograph arrives',
  afterJump.empty === 0 && afterJump.preview > 0, JSON.stringify(afterJump));
const previewLook = await page.evaluate(() => {
  const node = [...window.__grid.mounted.values()].find((n) => /url\(/.test(n.style.backgroundImage));
  const style = node ? getComputedStyle(node) : null;
  return style ? { size: style.backgroundSize, url: node.style.backgroundImage.slice(0, 30) } : null;
});
ok('a preview is a small picture stretched over the tile',
  previewLook && previewLook.size === 'cover' && previewLook.url.includes('data:image/png'),
  JSON.stringify(previewLook));

/* -- a fling with the photographs held back ---------------------------- */

const fling = await page.evaluate(() => new Promise((resolve) => {
  const scroller = window.__grid.scroller;
  const totals = { photo: 0, preview: 0, empty: 0 };
  let emptyFrames = 0;
  let frames = 0;
  let start = 0;
  let last = 0;
  const step = (at) => {
    if (!start) { start = at; last = at; }
    // 8 px a millisecond: a hard flick, through tiles never shown before.
    scroller.scrollTop += 8 * Math.min(50, at - last);
    last = at;
    requestAnimationFrame(() => {
      const c = window.__sample();
      totals.photo += c.photo; totals.preview += c.preview; totals.empty += c.empty;
      frames += 1;
      if (c.empty > 0) emptyFrames += 1;
      if (at - start < 2500) step(performance.now());
      else resolve({ ...totals, frames, emptyFrames });
    });
  };
  requestAnimationFrame(step);
}));
const area = fling.photo + fling.preview + fling.empty;
ok('a fling through new stretches never shows an empty tile',
  fling.empty / area < 0.01, JSON.stringify(fling));
ok('…on almost every frame', fling.emptyFrames <= Math.max(2, fling.frames * 0.03),
  `${fling.emptyFrames} of ${fling.frames} frames`);

ok('previews come a few hundred to a request',
  previewRequests.length > 0 && previewRequests.every((n) => n <= 256)
    && previewRequests.reduce((a, n) => a + n, 0) / previewRequests.length > 20,
  JSON.stringify(previewRequests));

/* -- and the photographs still land ------------------------------------- */

slow = false;
await page.waitForTimeout(400);       // the scroll settles
const landed = await page.waitForFunction(() => {
  const c = window.__sample();
  return c.empty === 0 && c.preview === 0 && c.photo > 0;
}, null, { timeout: 15000, polling: 100 }).then(() => true).catch(() => false);
ok('the photographs replace the previews once they arrive', landed,
  JSON.stringify(await page.evaluate(() => window.__sample())));
const stale = await page.evaluate(() => {
  // A tile handed on to another photograph keeps no preview of the last one.
  let wrong = 0;
  for (const [n, node] of window.__grid.mounted) {
    const cell = window.__grid.layout.cells[n];
    if (node.previewOf != null && node.previewOf !== cell.id) wrong += 1;
  }
  return wrong;
});
ok('no tile shows another photograph\'s preview', stale === 0, `${stale}`);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
