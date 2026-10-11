/**
 * Scrolling the gallery: pictures are there before they are looked at, and
 * nothing moves inside a tile while it goes past.
 *
 * Two things made a scroll look artificial. A pointer resting on the grid
 * hovered every tile that slid under it, and the hover zooms the picture in its
 * frame, so each one swelled and shrank again as it passed. And pictures were
 * fetched ahead only in the direction last scrolled, so turning round met a
 * screen of tiles fading in one by one. What is checked for that is that the
 * pictures were asked for before the scroll arrived at them, which does not
 * depend on how fast this machine serves them.
 *
 *   node tests/smooth_scroll_ui.mjs   (wants the family app up with a library
 *                                      of a few thousand photographs)
 * @fixture busy
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
// No service worker: its fetches would go round the slow network set below.
const context = await b.newContext({ viewport: { width: 1200, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);

// The library is scanned and its thumbnails made after the server starts;
// wait until the grid is tall enough to scroll through.
const tallEnough = async () => {
  for (let i = 0; i < 90; i++) {
    const height = await page.evaluate(() => {
      const grid = document.querySelector('#grid');
      const thumbed = document.querySelectorAll('#grid .cell img[src]').length;
      return thumbed ? grid.offsetHeight : 0;
    });
    if (height > 12000) return height;
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: 'networkidle' });
  }
  return 0;
};
const height = await tallEnough();
ok('the gallery holds enough photographs to scroll through', height > 12000, `height=${height}`);

/* -- no hover while scrolling ---------------------------------------- */

await page.mouse.move(600, 420);
await page.waitForTimeout(500);
const hoveredBefore = await page.evaluate(() => document.querySelectorAll('#grid .cell:hover').length);
ok('a resting pointer hovers the tile under it', hoveredBefore === 1, `hovered=${hoveredBefore}`);

await page.mouse.wheel(0, 240);
await page.waitForTimeout(40);
// The tile under the pointer mid-scroll: hovered, as far as the browser is
// concerned, but showing none of it.
const hoverLook = () => page.evaluate(() => {
  const grid = document.querySelector('#grid');
  const cell = document.elementFromPoint(600, 420)?.closest('.cell');
  const img = cell?.querySelector('img');
  return {
    scrolling: grid.classList.contains('scrolling'),
    hovered: !!cell?.matches(':hover'),
    zoom: img ? getComputedStyle(img).transform : '',
    pointer: cell ? getComputedStyle(cell).pointerEvents : '',
  };
});
const during = await hoverLook();
ok('the grid says it is scrolling', during.scrolling);
ok('tiles show no hover while the grid scrolls', during.zoom === 'none', JSON.stringify(during));
ok('but still take the pointer', during.pointer !== 'none', JSON.stringify(during));

await page.waitForTimeout(500);
await page.mouse.move(602, 422);
await page.waitForTimeout(450);
const after = await hoverLook();
ok('the hover comes back once the scroll stops',
  !after.scrolling && after.hovered && after.zoom !== 'none', JSON.stringify(after));

/* -- a click straight after a scroll --------------------------------- */

// A trackpad goes on scrolling for a moment after the fingers lift, and the
// click that comes in that moment is meant for the tile under it.
let opened = 0;
for (let i = 0; i < 3; i++) {
  await page.mouse.wheel(0, 300);
  await page.waitForTimeout(50);
  const mid = await page.evaluate(() => document.querySelector('#grid').classList.contains('scrolling'));
  await page.mouse.click(600, 420);
  await page.waitForTimeout(600);
  const open = await page.evaluate(() => !document.querySelector('#viewer').hidden);
  if (mid && open) opened++;
  if (open) {
    await page.keyboard.press('Escape');
    await page.waitForTimeout(400);
  }
}
ok('a click mid-scroll opens the photograph under it', opened === 3, `${opened} of 3`);

/* -- pictures fetched ahead in both directions ------------------------ */

// Every thumbnail the page asks for, and when it first asked.
const asked = new Map();
page.on('request', (request) => {
  const url = new URL(request.url());
  if (!url.pathname.startsWith('/api/thumb/')) return;
  const key = url.pathname + url.search;
  if (!asked.has(key)) asked.set(key, Date.now());
});

const view = await page.evaluate(() => document.querySelector('#scroller').clientHeight);
const scrollTo = (top) => page.evaluate((y) => { document.querySelector('#scroller').scrollTop = y; }, top);

/** Of the tiles on screen with a picture, how many had it asked for before
 *  `since` — that is, before the scroll brought them here. */
const askedBefore = async (since) => {
  const srcs = await page.evaluate(() => {
    const box = document.querySelector('#scroller').getBoundingClientRect();
    const out = [];
    for (const cell of document.querySelectorAll('#grid .cell')) {
      const r = cell.getBoundingClientRect();
      if (r.bottom <= box.top || r.top >= box.bottom) continue;
      const img = cell.querySelector('img');
      if (img && !img.hidden && img.getAttribute('src')) out.push(img.getAttribute('src'));
    }
    return out;
  });
  const early = srcs.filter((src) => asked.has(src) && asked.get(src) < since).length;
  return { total: srcs.length, early };
};

// Straight to a third of the way down, and stop there. Nothing near it has
// been looked at yet, so whatever is ready around it the look-ahead fetched.
const middle = Math.round(height / 3);
await scrollTo(middle);
await page.waitForTimeout(3000);

// Back up two screens: past the tiles kept made above the screen, into what
// only a look-ahead behind the scroll would have fetched.
let jumped = Date.now();
await scrollTo(middle - view * 2);
await page.waitForTimeout(150);
const back = await askedBefore(jumped);
ok('going back up finds the pictures already asked for',
  back.total > 0 && back.early / back.total >= 0.8, JSON.stringify(back));

// Somewhere new further down, stop, then carry on down two screens.
const further = middle + view * 12;
await scrollTo(further);
await page.waitForTimeout(3000);
jumped = Date.now();
await scrollTo(further + view * 2);
await page.waitForTimeout(150);
const ahead = await askedBefore(jumped);
ok('carrying on down finds the pictures already asked for',
  ahead.total > 0 && ahead.early / ahead.total >= 0.8, JSON.stringify(ahead));

ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
