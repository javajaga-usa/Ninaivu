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
 * Then flicker (1.10.7). The hover look came back the moment a scroll stopped,
 * so between the notches of a mouse wheel the tile under the pointer lit up and
 * dropped it again; it now waits for the mouse to move. And the look-ahead held
 * connections the tiles coming on screen were waiting for, so they arrived
 * late and faded in; it now starts only once every tile made has its picture.
 *
 *   node tests/smooth_scroll_ui.mjs   (wants the family app up with a library
 *                                      of a few thousand photographs)
 * @fixture busy
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
// No service worker: its fetches would go round the slow network set below.
// A Retina screen, so a tile drawn at 200 px wants the 640 px picture and
// takes the 256 px one flying past.
const context = await b.newContext({
  viewport: { width: 1200, height: 800 }, deviceScaleFactor: 2, serviceWorkers: 'block',
});
const page = await context.newPage();
// Note every look-ahead fetch (grid.js makes them with `new Image()` at low
// priority; the sharp picture fetched on the side for a tile is the same
// element at a higher one) and whether a tile was still waiting for its
// picture when it started.
await page.addInitScript(() => {
  const Native = window.Image;
  window.__aheadWhileLoading = 0;
  window.__ahead = 0;
  window.__sideLoaded = new Set();            // what those fetches have brought in
  window.Image = function Image(...args) {
    const image = new Native(...args);
    image.addEventListener('load', () => window.__sideLoaded.add(image.getAttribute('src')));
    const set = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src').set;
    Object.defineProperty(image, 'src', {
      configurable: true,
      get() { return image.getAttribute('src') || ''; },
      set(value) {
        if (String(value).includes('/api/thumb/') && image.fetchPriority === 'low') {
          window.__ahead++;
          const waiting = [...document.querySelectorAll('#grid .cell > img')]
            .some((img) => !img.hidden && img.getAttribute('src') && !img.complete);
          if (waiting) window.__aheadWhileLoading++;
        }
        set.call(image, value);
      },
    });
    return image;
  };
  window.Image.prototype = Native.prototype;
});
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
    noHover: grid.classList.contains('no-hover'),
    hovered: !!cell?.matches(':hover'),
    zoom: img ? getComputedStyle(img).transform : '',
    pointer: cell ? getComputedStyle(cell).pointerEvents : '',
  };
});
const during = await hoverLook();
ok('the grid says it is scrolling', during.scrolling);
ok('tiles show no hover while the grid scrolls', during.zoom === 'none', JSON.stringify(during));
ok('but still take the pointer', during.pointer !== 'none', JSON.stringify(during));

// Stopped, with the mouse where it was: still no hover look. Brought back at
// the stop, it lit up between every notch of a wheel and dropped again.
await page.waitForTimeout(500);
const stopped = await hoverLook();
ok('the scroll stopping alone does not bring the hover back',
  !stopped.scrolling && stopped.zoom === 'none', JSON.stringify(stopped));
let flashes = 0;
for (let i = 0; i < 4; i++) {
  await page.mouse.wheel(0, 120);
  await page.waitForTimeout(260);           // longer than a scroll takes to settle
  const look = await hoverLook();            // a header may be under the point: no zoom to read
  if (look.zoom && look.zoom !== 'none') flashes++;
}
ok('no flash of hover between the notches of a wheel', flashes === 0, `${flashes} of 4`);

await page.mouse.move(602, 422);
await page.waitForTimeout(450);
const after = await hoverLook();
ok('the hover comes back once the scroll stops',
  !after.scrolling && after.hovered && after.zoom !== 'none',
  JSON.stringify(after));

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

/* -- small pictures while scrolling, sharpened at the stop ---------------- */

// Scroll on at a reading pace: every tile made meanwhile takes the small
// picture (a quarter of the bytes, the way Photos shows its small ones while
// the finger moves). Stop, and watch the tiles sharpen: each must change its
// picture only to one the browser already holds, or Safari blanks the tile
// until the sharp one arrives.
await page.evaluate(() => {
  window.__swaps = [];
  new MutationObserver((muts) => {
    for (const m of muts) {
      const img = m.target;
      if (m.attributeName !== 'src' || !m.oldValue || !img.classList.contains('ready')) continue;
      const to = img.getAttribute('src');
      const asset = (url) => url.split('?')[0];
      // The same photograph at another size (a recycled tile given a different
      // photograph is the ordinary mount, not a swap). Brought in on the side
      // already, in the browser's memory (complete at once), or being fetched
      // by this very change of address?
      if (asset(to) !== asset(m.oldValue)) continue;
      const had = window.__sideLoaded.has(to) || (img.complete && img.naturalWidth > 0);
      window.__swaps.push({ to, from: m.oldValue, had });
    }
  }).observe(document.querySelector('#grid'), {
    subtree: true, attributes: true, attributeOldValue: true, attributeFilter: ['src'],
  });
});
const sizesOnScreen = () => page.evaluate(() => {
  const box = document.querySelector('#scroller').getBoundingClientRect();
  const sizes = { small: 0, sharp: 0 };
  for (const cell of document.querySelectorAll('#grid .cell')) {
    const r = cell.getBoundingClientRect();
    if (r.bottom <= box.top || r.top >= box.bottom) continue;
    const src = cell.querySelector('img')?.getAttribute('src') || '';
    if (src.includes('s=256')) sizes.small++;
    else if (src.includes('s=640')) sizes.sharp++;
  }
  return sizes;
});
await page.mouse.move(640, 400);
for (let i = 0; i < 40; i++) {                    // 1,500 px a second, for two seconds
  await page.mouse.wheel(0, 75);
  await page.waitForTimeout(50);
}
const moving = await sizesOnScreen();
ok('tiles made while scrolling take the small picture',
  moving.small > 0 && moving.sharp === 0, JSON.stringify(moving));
await page.waitForTimeout(3500);
const atRest = await sizesOnScreen();
ok('and are sharpened once the scroll stops',
  atRest.sharp > 0 && atRest.small === 0, JSON.stringify(atRest));
const swaps = await page.evaluate(() => window.__swaps);
ok('the sharpening changed the tiles\' pictures in place', swaps.length > 0, `${swaps.length} swaps`);
const early = swaps.filter((swap) => !swap.had);
ok('a tile only ever changes to a picture the browser already holds',
  early.length === 0, `${early.length} of ${swaps.length} early: ${JSON.stringify(early.slice(0, 3))}`);

/* -- no layer per tile ----------------------------------------------- */

const layering = await page.evaluate(() => {
  const cell = document.querySelector('#grid .cell');
  const style = getComputedStyle(cell);
  return { willChange: style.willChange, transform: style.transform.slice(0, 7) };
});
ok('a tile is not promoted to a layer of its own',
  layering.willChange === 'auto' && layering.transform === 'matrix(', JSON.stringify(layering));

const counts = await page.evaluate(() => ({ ahead: window.__ahead, early: window.__aheadWhileLoading }));
ok('the look-ahead fetched something', counts.ahead > 0, JSON.stringify(counts));
ok('the look-ahead never started while a tile was waiting for its picture',
  counts.early === 0, JSON.stringify(counts));

ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
