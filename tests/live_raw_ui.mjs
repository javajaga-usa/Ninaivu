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

/* ---------- the Live button, by mouse ---------- */

// The stage captures every pointer for swipes and pans, and a click on the
// button used to be sent to the stage instead: a resting mouse played it
// quietly, and a click never asked for the sound.
await p.setViewportSize({ width: 1280, height: 860 });
await p.locator('#grid .cell.is-live').first().click();
await p.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
if (canPlay) {
  await p.waitForFunction(() => !document.querySelector('.viewer-stage .live-badge.on'),
    null, { timeout: 8000 }).catch(() => {});
  await p.locator('.viewer-stage .live-badge').click();
  await p.waitForTimeout(200);
  const clicked = await p.evaluate(() => ({
    on: document.querySelector('.viewer-stage .live-badge').classList.contains('on'),
    muted: document.querySelector('.viewer-stage .live-clip').muted,
  }));
  ok('a click on Live plays it with its sound', clicked.on && !clicked.muted, JSON.stringify(clicked));
  await p.locator('.viewer-stage .live-badge').click();
  await p.waitForTimeout(200);
  ok('and a second click stops it',
    !(await p.locator('.viewer-stage .live-badge').evaluate((el) => el.classList.contains('on'))));
}

// A right click on a live photograph is still the browser's: Save image as.
// (Dispatched rather than clicked: where a real right click's menu event
// lands depends on the platform and on the stage's pointer capture.)
const menuRefused = () => p.evaluate(() => {
  const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2 });
  document.querySelector('.viewer-stage > img.live-still').dispatchEvent(event);
  return event.defaultPrevented;
});
ok('a right click on a live photograph keeps the browser menu', !(await menuRefused()));
if (!(await p.locator('.viewer-stage .live-badge').count())) {
  await p.locator('#grid .cell.is-live').first().click();
  await p.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
}

/* ---------- paging with the keys leaves nothing behind ---------- */

// A live photo's listeners sit on the stage, which every photograph shares.
// They were dropped only by the next pointer event, and paging with the keys
// (or a slideshow, or the ambient frame) has none, so each live photo shown
// kept its clip alive for as long as the viewer stayed open.
const cdp = await ctx.newCDPSession(p);
const stageListeners = async () => {
  const { result } = await cdp.send('Runtime.evaluate', { expression: 'document.querySelector(".viewer-stage")' });
  const { listeners } = await cdp.send('DOMDebugger.getEventListeners', { objectId: result.objectId });
  return listeners.filter((l) => l.type === 'pointermove').length;
};
const detachedVideos = async () => {
  await cdp.send('HeapProfiler.collectGarbage');
  await cdp.send('HeapProfiler.collectGarbage');
  const { result } = await cdp.send('Runtime.evaluate', { expression: 'HTMLVideoElement.prototype' });
  const { objects } = await cdp.send('Runtime.queryObjects', { prototypeObjectId: result.objectId });
  const { result: n } = await cdp.send('Runtime.callFunctionOn', {
    objectId: objects.objectId, returnByValue: true,
    functionDeclaration: 'function () { return this.filter((v) => !v.isConnected).length; }',
  });
  await cdp.send('Runtime.releaseObject', { objectId: objects.objectId });
  return n.value;
};
const listenersBefore = await stageListeners();
const clipsBefore = await detachedVideos();
for (let i = 0; i < 16; i += 1) {
  await p.keyboard.press(i % 2 ? 'ArrowLeft' : 'ArrowRight');
  await p.waitForTimeout(150);
}
// Back on the first live photo, so the stage holds exactly one again.
const listenersAfter = await stageListeners();
ok('paging with the keys leaves no listeners on the stage',
  listenersAfter <= listenersBefore, `${listenersBefore} before, ${listenersAfter} after`);
await p.waitForTimeout(500);
const leftOver = await detachedVideos();
ok('nor the clips of the photographs paged past', leftOver <= clipsBefore + 1,
  `${clipsBefore} before, ${leftOver} after`);
await p.keyboard.press('Escape');
await p.waitForTimeout(300);

/* ---------- less motion ---------- */

{
  const still = await b.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 860 },
    reducedMotion: 'reduce' });
  const page = await still.newPage();
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell.is-live').length > 0,
    { timeout: 20000 });
  await page.locator('#grid .cell.is-live').first().hover();
  await page.waitForTimeout(900);
  ok('asked for less motion, a resting mouse does not set a tile moving',
    await page.locator('#grid .live-peek').count() === 0);
  await still.close();
}

/* ---------- the Live button, on a phone ---------- */

{
  const phone = await b.newContext({ serviceWorkers: 'block', viewport: { width: 390, height: 844 },
    hasTouch: true, isMobile: true });
  const page = await phone.newPage();
  const phoneErrs = [];
  page.on('pageerror', (e) => phoneErrs.push(e.message));
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell.is-live').length > 0,
    { timeout: 20000 });
  await page.locator('#grid .cell.is-live').first().tap();
  await page.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
  await page.waitForTimeout(500);
  // The title bar wraps onto more lines on a phone; the button sat under it.
  const reachable = await page.evaluate(() => {
    const badge = document.querySelector('.viewer-stage .live-badge');
    const r = badge.getBoundingClientRect();
    return badge.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  });
  ok('on a phone the Live button is clear of the title bar', reachable);
  if (canPlay) {
    await page.waitForFunction(() => !document.querySelector('.viewer-stage .live-badge.on'),
      null, { timeout: 8000 }).catch(() => {});
    await page.locator('.viewer-stage .live-badge').tap();
    await page.waitForTimeout(300);
    ok('a tap on Live plays it',
      await page.locator('.viewer-stage .live-badge.on').count() === 1);
  }
  // A finger held on the picture is the Live Photo, not the save-image menu.
  const still = await page.locator('.viewer-stage > img.live-still').boundingBox();
  await page.touchscreen.tap(still.x + still.width / 2, still.y + still.height / 2);
  const refused = await page.evaluate(() => {
    const event = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    document.querySelector('.viewer-stage > img.live-still').dispatchEvent(event);
    return event.defaultPrevented;
  });
  ok('but a long press on a phone does not offer to save the picture', refused);
  ok('no page errors on the phone', phoneErrs.length === 0, phoneErrs.join(' | '));
  await phone.close();
}

/* ---------- a browser that cannot open the original ---------- */

{
  // As Chrome without HEVC is: the original fails, the converted copy plays.
  const other = await b.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 860 } });
  const page = await other.newPage();
  const asked = [];
  let copyFails = false;
  await other.route(/\/api\/live-video\/\d+(\?.*)?$/, async (route) => {
    const request = route.request();
    const isCopy = request.url().includes('convert=1');
    asked.push(`${request.method()} ${isCopy ? 'copy' : 'original'}`);
    if (!isCopy) {
      await route.fulfill({ status: 200, contentType: 'video/quicktime', body: Buffer.alloc(6000, 7) });
    } else if (copyFails) {
      await route.fulfill({ status: 200, contentType: 'video/mp4', body: Buffer.alloc(6000, 9) });
    } else {
      await route.continue();
    }
  });
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell.is-live').length > 1,
    { timeout: 20000 });
  await page.locator('#grid .cell.is-live').first().click();
  await page.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
  await page.waitForTimeout(4000);
  ok('the converted copy is asked after with HEAD, not fetched twice',
    asked.filter((a) => a === 'GET copy').length <= 1 && asked.includes('HEAD copy'),
    asked.join(', '));
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);

  asked.length = 0;
  copyFails = true;
  await page.locator('#grid .cell.is-live').nth(1).click();
  await page.waitForSelector('.viewer-stage .live-badge', { timeout: 8000 });
  await page.waitForTimeout(3000);
  ok('the next one goes straight to the copy', !asked.some((a) => a.endsWith('original')),
    asked.join(', '));
  const stuck = await page.evaluate(() => {
    const badge = document.querySelector('.viewer-stage .live-badge');
    return { on: badge.classList.contains('on'), disabled: badge.disabled };
  });
  ok('a copy that will not play either leaves the button off, not spinning',
    !stuck.on && stuck.disabled, JSON.stringify(stuck));
  await other.close();
}

/* ---------- and the plain photograph has neither ---------- */

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
