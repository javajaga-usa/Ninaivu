/**
 * Speaking to search and colour for black-and-white photographs, in a
 * browser: both hidden until the console switches them on; the microphone
 * records only when pressed and puts what was heard in the search box; Add
 * colour shows the colour over the original with a line to drag, keeps it as
 * a copy, and the copy offers Before and after.
 *
 * The models are not on the runner, so the two answers that need them (what
 * was heard, and the coloured picture) are answered here; the rest, the copy
 * saved and remembered, is the real server's.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, ENGINE, signInAtHome } from './harness.mjs';

// A fake microphone, and no question asked about it (Chromium only).
const browser = await launch(ENGINE === 'chromium'
  ? { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] } : {});
// The page's own service worker would answer GETs before page.route sees them.
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
if (ENGINE === 'chromium') await context.grantPermissions(['microphone']);
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);
// A black-and-white photograph and what DDColor made of it on a real run.
const data = (name) => readFileSync(fileURLToPath(new URL(`./data/${name}`, import.meta.url)));
const BEFORE = data('colour-before.jpg');
const AFTER = data('colour-after.jpg');

/** The viewer's More menu, where the screen is narrow enough to have one. */
async function moreMenu() {
  if (await page.locator('#v-more').isVisible()) await page.click('#v-more');
}

async function switches(on) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, value]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { voice_search: value, colour_photos: value } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, on]);
  await consolePage.close();
  return answer;
}

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
ok('off: no microphone in the search box', await page.locator('#voice-search').isHidden());

ok('the console switches both on', (await switches(true)) === 200);
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
ok('on: the microphone shows', await page.locator('#voice-search').isVisible());

/* -- speaking to search ------------------------------------------------------ */

let sent = null;
await page.route('**/api/voice-search', async (route) => {
  if (route.request().method() !== 'POST') return route.continue();
  sent = route.request().postDataBuffer();
  await route.fulfill({ json: { text: 'கடற்கரை', english: 'beach', language: 'ta', query: 'beach', took: 1.1 } });
});
if (ENGINE === 'chromium') {
  await page.click('#voice-search');
  await page.waitForSelector('#voice-search.listening', { timeout: 5000 });
  ok('pressed, it listens', await page.locator('#voice-search').getAttribute('aria-pressed') === 'true');
  await page.waitForTimeout(700);
  await shot('voice-listening');
  await page.click('#voice-search');
  await page.waitForFunction(() => document.querySelector('#search').value === 'beach', { timeout: 8000 });
  ok('what was heard is searched as if typed', true);
  ok('the recording was sent', Boolean(sent?.length), String(sent?.length));
  ok('and the microphone is let go of',
    !(await page.locator('#voice-search').getAttribute('class')).includes('listening'));
  await shot('voice-heard');
  await page.click('#clear-search');
}

/* -- colour for a black-and-white photograph --------------------------------- */

const first = await page.evaluate(async () => {
  const { items } = await (await fetch('/api/assets?limit=100')).json();
  const shown = new Set([...document.querySelectorAll('#grid .cell[data-id]')].map((c) => Number(c.dataset.id)));
  return items.find((i) => i.kind === 'picture' && shown.has(i.id))?.id;
});
ok('a photograph to colour', Number.isFinite(first), String(first));
// The runner has no model: the first photograph is said to be black and
// white and ready, and is "coloured" with a fixed picture.
await page.route(new RegExp(`/api/asset/${first}/colour/upright$`),
  (route) => route.fulfill({ status: 200, contentType: 'image/jpeg', body: BEFORE }));
await page.route(new RegExp(`/api/asset/${first}/colour$`), async (route) => {
  if (route.request().method() === 'POST') {
    await route.fulfill({ status: 200, contentType: 'image/jpeg', body: AFTER });
  } else {
    await route.fulfill({ json: { on: true, ready: true, why: '', mono: true, source: null, copies: [], before: null } });
  }
});
await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await moreMenu();
await page.waitForSelector('#v-colour:not([hidden])', { timeout: 8000 });
ok('Add colour is offered', (await page.locator('#v-colour').textContent()).includes('Add colour'));
await page.click('#v-colour');
await page.waitForSelector('.colour-compare:not(.busy) .cc-after[src^="blob:"]', { timeout: 8000 });
ok('the colour is laid over the original',
  await page.locator('.colour-compare .cc-before').getAttribute('src') === `/api/asset/${first}/colour/upright`);
await page.keyboard.press('ArrowLeft');
ok('the arrows move the line, not the photograph',
  await page.locator('.colour-compare .cc-slider').inputValue() === '45');
const frame = await page.locator('.colour-compare .cc-frame').boundingBox();
await page.mouse.click(frame.x + frame.width * 0.8, frame.y + frame.height / 2);
ok('dragging moves the line', Number(await page.locator('.colour-compare .cc-slider').inputValue()) > 70);
await page.mouse.click(frame.x + frame.width * 0.5, frame.y + frame.height / 2);
await page.waitForTimeout(300);
await shot('colour-compare');
const saved = page.waitForResponse((r) => r.url().includes(`/api/asset/${first}/edited-copy?made=colour`));
await page.click('.colour-compare .cc-keep');
await page.waitForTimeout(500);
ok('kept as a copy beside the original', (await saved).status() === 201);
await page.waitForSelector('.colour-compare', { state: 'detached', timeout: 5000 });
// The viewer opens the copy, and the copy remembers where it came from.
await page.waitForFunction(() => !document.querySelector('#v-colour').hidden
  && document.querySelector('#v-colour .viewer-tools-more-label').textContent === 'Before and after',
null, { timeout: 8000 });
ok('the copy offers Before and after', true);
await moreMenu();
await page.click('#v-colour');
await page.waitForSelector('.colour-compare .cc-before', { timeout: 5000 });
ok('its before is the original',
  await page.locator('.colour-compare .cc-before').getAttribute('src') === `/api/asset/${first}/colour/upright`);
ok('nothing to keep twice', await page.locator('.colour-compare .cc-keep').isHidden());
await page.keyboard.press('Escape');
ok('Escape closes the comparison and leaves the viewer open',
  await page.locator('.colour-compare').count() === 0 && await page.locator('#viewer').isVisible());

ok('no script errors', errors.length === 0, errors.join(' | '));
await switches(false);
await done(browser);
