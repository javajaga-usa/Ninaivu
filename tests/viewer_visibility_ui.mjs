/**
 * Hiding a photograph from the viewer shows on its tile, as hiding it from a
 * selection does.
 *
 * Reported 16 October 2026: choosing "Hidden — admins only" for the photograph
 * open in the viewer left its tile in the gallery without the hidden badge,
 * and the Hidden view without it, until a reload. The same choice made for a
 * selection of tiles reloaded the gallery and was shown at once.
 *
 *   node tests/viewer_visibility_ui.mjs   (wants the family app up with a library)
 * @fixture several
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
const context = await b.newContext({ viewport: { width: 1300, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
await page.waitForSelector('#grid .cell', { timeout: 60000 });

const id = await page.evaluate(() => document.querySelector('#grid .cell').dataset.id);
const badge = (vis) => page.evaluate(([cellId, kind]) =>
  Boolean(document.querySelector(`#grid .cell[data-id="${cellId}"] .badge.vis.is-${kind}`)), [id, vis]);

ok('the tile starts without a hidden badge', !(await badge('hidden')));

await page.click(`#grid .cell[data-id="${id}"]`);
await page.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await page.waitForFunction(() => !document.querySelector('#v-visibility')?.hidden, null, { timeout: 15000 });

// Pressed through the page: the picker sits in the viewer's More menu.
await page.evaluate(() => document.querySelector('#v-visibility button[data-vis="hidden"]').click());
let shown = false;
for (let i = 0; i < 40 && !shown; i++) {
  shown = await badge('hidden');
  if (!shown) await page.waitForTimeout(100);
}
ok('the tile shows it hidden while the viewer is still open', shown);

await page.keyboard.press('Escape');
await page.waitForFunction(() => document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await page.waitForTimeout(800);
ok('the tile stays hidden after the gallery is brought up to date', await badge('hidden'));

// The Hidden view lists it, without reloading the page.
await page.click('[data-view="hidden"]');
let listed = false;
for (let i = 0; i < 40 && !listed; i++) {
  listed = await page.evaluate((cellId) => Boolean(document.querySelector(`#grid .cell[data-id="${cellId}"]`)), id);
  if (!listed) await page.waitForTimeout(150);
}
ok('the Hidden view lists it', listed);

// Back to family, from the viewer opened in the Hidden view: it leaves that
// view when the viewer closes.
await page.click(`#grid .cell[data-id="${id}"]`);
await page.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await page.evaluate(() => document.querySelector('#v-visibility button[data-vis="family"]').click());
await page.waitForTimeout(500);
await page.keyboard.press('Escape');
let gone = false;
for (let i = 0; i < 40 && !gone; i++) {
  gone = await page.evaluate((cellId) => !document.querySelector(`#grid .cell[data-id="${cellId}"]`), id);
  if (!gone) await page.waitForTimeout(150);
}
ok('shown to the family again, it leaves the Hidden view', gone);
ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
