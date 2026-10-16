/**
 * Dragging the date rail: the grid follows the handle, and pictures are
 * fetched for where it is let go — not for every month it passes.
 *
 * A mouse wheel moves the grid a row or two a frame, so the tiles kept ready
 * ahead of it are tiles it is about to reach. A drag of the rail's handle
 * moves it years a frame. Each of those frames made four or five screens of
 * tiles and started all of their pictures, threw them away the next frame,
 * and left a queue of downloads for places the handle had already left: the
 * drag stuttered, and the screen it stopped on waited behind that queue.
 *
 * What is checked: while the handle moves, the grid draws only the screen
 * under it and asks for almost nothing; let go (or held still), the pictures
 * for the screen it stopped on are the first asked for, and they arrive.
 *
 *   node tests/scrubber_drag_ui.mjs   (wants the family app up with a library
 *                                      of a few thousand photographs)
 * @fixture busy
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
const context = await b.newContext({ viewport: { width: 1200, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

// Every thumbnail asked of the server, in order, with when.
const asked = [];
page.on('request', (request) => {
  const match = request.url().match(/\/api\/thumb\/(\d+)/);
  if (match) asked.push({ id: match[1], at: Date.now() });
});

await signInAtHome(page);

const tallEnough = async () => {
  for (let i = 0; i < 90; i++) {
    const height = await page.evaluate(() => {
      const grid = document.querySelector('#grid');
      const thumbed = document.querySelectorAll('#grid .cell img[src]').length;
      const scrubber = document.querySelector('#scrubber');
      return thumbed && !scrubber.hidden ? grid.offsetHeight : 0;
    });
    if (height > 12000) return height;
    await page.waitForTimeout(2000);
    await page.reload({ waitUntil: 'networkidle' });
  }
  return 0;
};
const height = await tallEnough();
ok('the gallery holds enough photographs to drag through', height > 12000, `height=${height}`);
await page.waitForTimeout(1500);              // what the first screen wanted has landed

/** The tiles on screen, and whether each has its picture. */
const screen = () => page.evaluate(() => {
  const scroller = document.querySelector('#scroller');
  const box = scroller.getBoundingClientRect();
  const cells = [...document.querySelectorAll('#grid .cell')];
  const on = cells.filter((cell) => {
    const r = cell.getBoundingClientRect();
    return r.bottom > box.top && r.top < box.bottom;
  });
  return {
    top: scroller.scrollTop,
    made: cells.length,
    ids: on.map((cell) => cell.dataset.id),
    shown: on.filter((cell) => cell.querySelector('img.ready')).length,
    count: on.length,
  };
});

const handle = async () => {
  await page.mouse.move(600, 400);            // the rail shows over a hovered grid
  await page.waitForTimeout(250);
  return page.locator('#scrubber-thumb').boundingBox();
};
const railBox = () => page.locator('#scrubber').boundingBox();

/* -- a drag down the rail -------------------------------------------- */

let grip = await handle();
const rail = await railBox();
ok('the rail has a handle to take hold of', !!grip && !!rail);
const x = grip.x + grip.width - 10;
let y = grip.y + grip.height / 2;
const start = await screen();

await page.mouse.move(x, y);
await page.mouse.down();
const askedBefore = asked.length;
const most = { made: 0 };
// Down to three-quarters of the rail, a step a frame or so: years of
// photographs go past, as they do under a hand.
const target = rail.y + rail.height * 0.75;
const steps = 40;
for (let i = 1; i <= steps; i++) {
  y = grip.y + grip.height / 2 + ((target - (grip.y + grip.height / 2)) * i) / steps;
  await page.mouse.move(x, y);
  await page.waitForTimeout(20);
  if (i % 8 === 0) {
    const now = await screen();
    if (now.made > most.made) Object.assign(most, now);
  }
}
const duringDrag = asked.length - askedBefore;
const moved = await screen();
ok('the grid followed the handle', moved.top > start.top + height * 0.4,
  `from ${start.top} to ${moved.top} of ${height}`);
ok('while the handle moves, almost nothing is fetched',
  duringDrag <= 12, `${duringDrag} thumbnails asked for during the drag`);
const view = await page.evaluate(() => document.querySelector('#scroller').clientHeight);
ok('while the handle moves, only the screen under it is drawn',
  most.made <= Math.max(60, most.count * 2), `${most.made} tiles made for ${most.count} on screen (${view}px)`);
const label = await page.locator('#scrubber-thumb span').textContent();
ok('the handle names the month it is on', /\d{4}/.test(label || ''), label);

const releasedAt = Date.now();
await page.mouse.up();

// Let go: the screen it stopped on fills, and its pictures are asked for
// before anything around it.
let after = await screen();
for (let i = 0; i < 40 && after.shown < after.count; i++) {
  await page.waitForTimeout(150);
  after = await screen();
}
ok('let go, the screen it stopped on fills', after.count > 0 && after.shown === after.count,
  `${after.shown} of ${after.count}`);
const onScreen = new Set(after.ids);
const first = asked.filter((one) => one.at >= releasedAt - 5).slice(0, Math.min(10, onScreen.size));
const offScreenFirst = first.filter((one) => !onScreen.has(one.id));
ok('the first pictures asked for are the ones on the screen it stopped on',
  first.length > 0 && offScreenFirst.length === 0,
  `${offScreenFirst.length} of ${first.length} were elsewhere`);

/* -- held still, without letting go ---------------------------------- */

await page.waitForTimeout(800);
grip = await handle();
await page.mouse.move(grip.x + grip.width - 10, grip.y + grip.height / 2);
await page.mouse.down();
for (let i = 1; i <= 10; i++) {
  await page.mouse.move(grip.x + grip.width - 10, grip.y + grip.height / 2 - i * 25);
  await page.waitForTimeout(20);
}
let held = await screen();
for (let i = 0; i < 40 && held.shown < held.count; i++) {
  await page.waitForTimeout(150);
  held = await screen();
}
ok('held still mid-drag, the screen under the handle fills', held.count > 0 && held.shown === held.count,
  `${held.shown} of ${held.count}`);
await page.mouse.up();

/* -- the handle moves without laying the page out -------------------- */

const placed = await page.evaluate(() => {
  const thumb = document.querySelector('#scrubber-thumb');
  return { top: thumb.style.top, transform: thumb.style.transform };
});
ok('the handle is placed with a transform, not top',
  !placed.top && placed.transform.startsWith('translateY('), JSON.stringify(placed));

ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
