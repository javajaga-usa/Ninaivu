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

// The test before this one on the same library puts files back from the bin,
// and the scan that follows can still be running here. On a slow runner it
// held the visibility change and kept redrawing the grid, so the fixed waits
// below ran out (17 October 2026). Start once the library is quiet.
for (let i = 0; i < 120; i++) {
  const running = await page.evaluate(async () =>
    Boolean((await (await fetch('/api/status')).json()).scan?.running));
  if (!running) break;
  await page.waitForTimeout(500);
}

// A tile shown to the family. screening_ui.mjs runs before this one on the
// same library and leaves the photograph it hid hidden; when that was the
// first tile, this test began on a hidden one (26 October 2026).
const id = await page.evaluate(() => [...document.querySelectorAll('#grid .cell')]
  .find((cell) => !cell.querySelector('.badge.vis'))?.dataset.id);
const badge = (vis) => page.evaluate(([cellId, kind]) =>
  Boolean(document.querySelector(`#grid .cell[data-id="${cellId}"] .badge.vis.is-${kind}`)), [id, vis]);

ok('the tile starts without a hidden badge', !(await badge('hidden')));

await page.click(`#grid .cell[data-id="${id}"]`);
await page.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await page.waitForFunction(() => !document.querySelector('#v-visibility')?.hidden, null, { timeout: 15000 });
// The picker shows as soon as the viewer opens, for an admin, but a press
// does nothing until the photograph's details have arrived. On a slow runner
// the press came first and no change was ever sent (19 October 2026). The
// picker says where the setting came from once they have.
await page.waitForFunction(() => document.querySelector('#v-visibility')?.dataset.source, null, { timeout: 15000 });

// Pressed through the page: the picker sits in the viewer's More menu. The
// badge is checked once the server has the change, however long that takes.
const saved = page.waitForResponse((r) => r.url().endsWith('/api/visibility')
  && r.request().method() === 'POST', { timeout: 30000 });
await page.evaluate(() => document.querySelector('#v-visibility button[data-vis="hidden"]').click());
await saved;
let shown = false;
for (let i = 0; i < 40 && !shown; i++) {
  shown = await badge('hidden');
  if (!shown) await page.waitForTimeout(100);
}
ok('the tile shows it hidden while the viewer is still open', shown);

await page.keyboard.press('Escape');
await page.waitForFunction(() => document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
let kept = false;
for (let i = 0; i < 40 && !kept; i++) {
  await page.waitForTimeout(200);
  kept = await badge('hidden');
}
ok('the tile stays hidden after the gallery is brought up to date', kept);

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
const back = page.waitForResponse((r) => r.url().endsWith('/api/visibility')
  && r.request().method() === 'POST', { timeout: 30000 });
await page.evaluate(() => document.querySelector('#v-visibility button[data-vis="family"]').click());
await back;
await page.keyboard.press('Escape');
let gone = false;
for (let i = 0; i < 40 && !gone; i++) {
  gone = await page.evaluate((cellId) => !document.querySelector(`#grid .cell[data-id="${cellId}"]`), id);
  if (!gone) await page.waitForTimeout(150);
}
ok('shown to the family again, it leaves the Hidden view', gone);
ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
