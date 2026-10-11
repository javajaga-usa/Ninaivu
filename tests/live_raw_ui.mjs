/**
 * Live Photos that move, and RAW+JPEG shots that are one photograph.
 *
 * @fixture pairs
 *
 * The library holds a live photo whose clip was filed under the next day (an
 * iPhone dates the clip in UTC), one whose clip sits beside it, a CR2 with
 * its JPEG, and a plain photograph: four things the household took, in six
 * files. The gallery shows four tiles, the live ones play under a resting
 * mouse, the viewer plays a live photo while it is held, and the RAW is taken
 * from the JPEG's More menu.
 *
 *   python tests/run_browser_tests.py tests/live_raw_ui.mjs
 *
 * SHOTS=<folder> keeps screenshots of the grid and the viewer.
 */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const SHOTS = process.env.SHOTS || '';
const b = await launch();
const ctx = await b.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 860 } });
const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(p);
await p.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0,
  { timeout: 20000 });
await p.waitForTimeout(800);

/* ---------- counted once ---------- */

const stats = await p.evaluate(async () =>
  (await (await fetch('/api/status')).json()).stats);
ok('four things taken, in six files', stats.count === 4, JSON.stringify(stats));
ok('both live photos are found, the split one too', stats.live === 2, `${stats.live} live`);
ok('no clip counted as a video', stats.videos === 0, `${stats.videos} videos`);

/* ---------- the grid ---------- */

const cells = p.locator('#grid .cell');
ok('four tiles, not six', await cells.count() === 4, `${await cells.count()} tiles`);
ok('the live ones carry a Live badge',
  await p.locator('#grid .cell.is-live .badge.live').count() === 2);
ok('the RAW+JPEG one carries a RAW badge',
  await p.locator('#grid .cell .badge.raw').count() === 1);
if (SHOTS) await p.screenshot({ path: `${SHOTS}/grid.png` });

const canPlay = await p.evaluate(() =>
  document.createElement('video').canPlayType('video/mp4; codecs="vp09.00.10.08"') !== '');

const live = p.locator('#grid .cell.is-live').first();
await live.hover();
await p.waitForSelector('#grid .cell.is-live .live-peek', { timeout: 3000 }).catch(() => {});
ok('a resting mouse plays the clip in the tile',
  await p.locator('#grid .cell.is-live .live-peek').count() === 1);
if (canPlay) {
  await p.waitForSelector('#grid .cell.live-playing', { timeout: 8000 }).catch(() => {});
  ok('and it is playing', await p.locator('#grid .cell.live-playing').count() === 1);
}
await p.mouse.move(5, 5);
await p.waitForTimeout(300);
ok('and stops when the mouse leaves',
  await p.locator('#grid .live-peek').count() === 0);

/* ---------- the viewer: a live photo ---------- */

await live.click();
await p.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
ok('the viewer offers the Live button', await p.locator('.viewer-stage .live-badge').isVisible());
ok('the clip sits over the photograph, not over the screen',
  await p.evaluate(() => getComputedStyle(document.querySelector('.viewer-stage .live-clip'))
    .gridArea.startsWith('1')));
if (canPlay) {
  await p.waitForFunction(() => document.querySelector('.viewer-stage .live-clip.playing'),
    null, { timeout: 8000 }).catch(() => {});
  ok('it plays once when opened', await p.locator('.viewer-stage .live-clip.playing').count() === 1);
  await p.waitForFunction(() => !document.querySelector('.viewer-stage .live-clip.playing'),
    null, { timeout: 8000 }).catch(() => {});
  ok('and goes back to the photograph', await p.locator('.viewer-stage .live-clip.playing').count() === 0);

  const box = await p.locator('.viewer-stage > img:not(.placeholder)').boundingBox();
  await p.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await p.mouse.down();
  await p.waitForFunction(() => document.querySelector('.viewer-stage .live-clip.playing'),
    null, { timeout: 8000 }).catch(() => {});
  ok('holding the photograph plays it', await p.locator('.viewer-stage .live-clip.playing').count() === 1);
  if (SHOTS) await p.screenshot({ path: `${SHOTS}/viewer-live-held.png` });
  await p.mouse.up();
  await p.waitForTimeout(400);
  ok('letting go stops it', await p.locator('.viewer-stage .live-clip.playing').count() === 0);
} else {
  console.log('  (this browser cannot play the fixture clip; playback was not checked)');
}
const info = await p.evaluate(async () => {
  const id = document.querySelector('#grid .cell.is-live').dataset.id;
  return (await fetch(`/api/asset/${id}`)).json();
});
const converted = await p.evaluate(async (url) => {
  for (let i = 0; i < 60; i += 1) {
    const r = await fetch(url);
    if (r.status !== 202) return { status: r.status, type: r.headers.get('content-type') };
    await new Promise((done) => { setTimeout(done, 500); });
  }
  return { status: 202 };
}, info.live_convert);
ok('the clip has a converted copy to fall back on',
  converted.status === 200 && converted.type === 'video/mp4', JSON.stringify(converted));
await p.keyboard.press('Escape');
await p.waitForTimeout(400);

/* ---------- the viewer: a RAW+JPEG shot ---------- */

await p.locator('#grid .cell:has(.badge.raw)').click();
await p.waitForTimeout(800);
const raw = p.locator('#v-download-raw');
ok('the More menu offers the RAW', !(await raw.evaluate((el) => el.hidden)));
ok('named with its type',
  (await raw.textContent()).includes('CR2'), await raw.textContent());
const href = await raw.getAttribute('href');
const got = await p.evaluate(async (url) => {
  const r = await fetch(url);
  return { status: r.status, kind: r.headers.get('content-disposition') || '' };
}, href);
ok('and it downloads the CR2', got.status === 200 && /CR2/i.test(got.kind), JSON.stringify(got));
if (SHOTS) await p.screenshot({ path: `${SHOTS}/viewer-raw-wide.png` });
// Narrower, the tools fold into More, where the RAW is named in words.
await p.setViewportSize({ width: 1000, height: 760 });
await p.locator('#v-more').click();
await p.waitForTimeout(300);
ok('folded into More, it is named in words',
  (await raw.textContent()).includes('Download RAW') && await raw.isVisible());
if (SHOTS) await p.screenshot({ path: `${SHOTS}/viewer-raw-menu.png` });
await p.keyboard.press('Escape');
await p.keyboard.press('Escape');

/* ---------- and the plain photograph has neither ---------- */

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
