// @fixture burst
/**
 * Photo stacks, in a browser: every frame its own tile until the console
 * switches photo_stacks on; then the burst and its edited copy are one tile,
 * the sharpest frame, with the count on it. Opened, the bar above the
 * filmstrip shows every frame with the top one starred; pressing a frame
 * opens it and the arrows walk the stack; Put on top and Show separately
 * change the timeline once the viewer closes, and Stack again puts it back.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
// The page's own service worker would answer GETs before page.route sees them.
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

async function switchOn(value) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { photo_stacks: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await consolePage.close();
  return answer;
}

const cells = () => page.locator('#grid .cell[data-id]').count();

async function tiles(count) {
  return page.waitForFunction((n) => document.querySelectorAll('#grid .cell[data-id]').length === n,
    count, { timeout: 15000 }).then(() => true, () => false);
}

async function closeViewer() {
  await page.keyboard.press('Escape');
  await page.waitForSelector('#viewer', { state: 'hidden', timeout: 8000 });
}

await signInAtHome(page);
ok('every photograph is a tile while it is off', await tiles(9), String(await cells()));
const byName = await page.evaluate(async () => {
  const { items } = await (await fetch('/api/assets?limit=100')).json();
  return Object.fromEntries(items.map((i) => [i.name, i.id]));
});
const sharp = byName['IMG_0502.JPG'];
const first = byName['IMG_0500.JPG'];
ok('the fixture is there', sharp && first, JSON.stringify(byName));
ok('off: no stack on any tile', await page.locator('#grid .badge.stack-count').count() === 0);

ok('the console switches it on', (await switchOn(true)) === 200);
await page.reload({ waitUntil: 'networkidle' });
ok('on: the burst and its copy are one tile', await tiles(4), String(await cells()));
const badge = page.locator(`#grid .cell[data-id="${sharp}"] .badge.stack-count`);
ok('the sharpest frame is the tile, with how many there are', (await badge.textContent()).trim() === '6',
  await badge.textContent().catch(() => 'no badge'));
await shot('stacks-timeline');

await page.click(`#grid .cell[data-id="${sharp}"]`);
await page.waitForSelector('#v-stack:not([hidden]) .stack-frame', { timeout: 8000 });
ok('the viewer shows every frame', await page.locator('#v-stack .stack-frame').count() === 6);
ok('the open one is the top one, starred',
  await page.locator('#v-stack .stack-frame.current.top .stack-top-mark').count() === 1);
ok('says how many', (await page.locator('#v-stack .stack-label').textContent()).includes('6'));
ok('the top one is not offered to put on top', await page.locator('#v-stack .btn', { hasText: 'Put on top' }).count() === 0);
await page.waitForTimeout(700);
await shot('stacks-viewer');

await page.locator('#v-stack .stack-frame').first().click();
await page.waitForFunction(() => document.querySelector('#v-stack .stack-frame.current')
  === document.querySelector('#v-stack .stack-frame'), null, { timeout: 8000 });
ok('pressing a frame opens it', true);
await page.keyboard.press('ArrowRight');
await page.waitForFunction(() => document.querySelectorAll('#v-stack .stack-frame')[1]?.classList.contains('current'),
  null, { timeout: 8000 }).then(() => ok('the arrows walk through the stack', true),
  () => ok('the arrows walk through the stack', false));
await page.keyboard.press('ArrowLeft');
await page.waitForFunction(() => document.querySelector('#v-stack .stack-frame.current')
  === document.querySelector('#v-stack .stack-frame'), null, { timeout: 8000 });
const putOnTop = page.waitForResponse((r) => r.url().endsWith(`/api/stacks/${first}/top`));
await page.locator('#v-stack .btn', { hasText: 'Put on top' }).click();
ok('Put on top is kept', (await putOnTop).status() === 200);
await page.waitForSelector('#v-stack .stack-frame.current.top', { timeout: 8000 });
ok('and the star moves to it', true);
await closeViewer();
await page.waitForSelector(`#grid .cell[data-id="${first}"] .badge.stack-count`, { timeout: 15000 })
  .then(() => ok('closed, the timeline shows the new top one', true),
    () => ok('closed, the timeline shows the new top one', false));
ok('and not the old', await page.locator(`#grid .cell[data-id="${sharp}"]`).count() === 0);

await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#v-stack:not([hidden]) .stack-frame', { timeout: 8000 });
await page.locator('#v-stack .btn', { hasText: 'Show separately' }).click();
await page.waitForSelector('#v-stack .btn:has-text("Stack again")', { timeout: 8000 });
ok('apart, the bar says so and offers to stack them again',
  (await page.locator('#v-stack .stack-label').textContent()).includes('separately'));
await closeViewer();
ok('closed, every frame is a tile again', await tiles(9), String(await cells()));
await page.click(`#grid .cell[data-id="${sharp}"]`);
await page.waitForSelector('#v-stack .btn:has-text("Stack again")', { timeout: 8000 });
await page.locator('#v-stack .btn', { hasText: 'Stack again' }).click();
await page.waitForSelector('#v-stack .btn:has-text("Show separately")', { timeout: 8000 });
await closeViewer();
ok('Stack again: one tile, the chosen top one kept', await tiles(4)
  && await page.locator(`#grid .cell[data-id="${first}"] .badge.stack-count`).count() === 1);

// A phone: no filmstrip, the bar where it was.
await page.setViewportSize({ width: 390, height: 844 });
await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#v-stack:not([hidden]) .stack-frame', { timeout: 8000 });
const box = await page.locator('#v-stack').boundingBox();
ok('on a phone the bar fits the width', box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box));
await page.waitForTimeout(700);
await shot('stacks-phone');
await closeViewer();

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(false);
await done(browser);
