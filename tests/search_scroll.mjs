/**
 * Bug: searching the AI search box left the gallery scrolled to wherever it
 * happened to be before the search — the new results render starting at the
 * top of the grid, but the scroller itself never moves, so they are hidden
 * above the fold until you scroll all the way up by hand.
 *
 * Root cause: Grid.setData() always calls relayout(true) — "preserve the
 * scroll anchor" — which is right for an in-place refresh (an edit, a
 * background rescan) but wrong for a genuinely new result set, where the
 * anchored item from the old layout usually is not even in the new one.
 *
 *   node tests/search_scroll.mjs
 */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 900, height: 650 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
await page.waitForSelector('#scroller', { state: 'attached', timeout: 15000 });
// Let the initial load settle before poking at it.
await page.waitForFunction(() => window.__ninaivuGridCellCount?.() > 0
  || document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });

// Scroll well down into the (unfiltered) gallery.
await page.evaluate(() => {
  document.querySelector('#scroller').scrollTop = 1200;
});
await page.waitForTimeout(150);
const before = await page.evaluate(() => document.querySelector('#scroller').scrollTop);
ok('the grid actually scrolled before searching', before > 200, `scrollTop=${before}`);

// Search for something the sample library's filenames actually contain —
// tools/make_sample.py names files after the scene ("beach_0000.jpg" etc.) —
// so this is a real, much smaller result set, not a no-op query.
await page.fill('#search', 'beach');
// wireSearch() debounces non-empty queries by 260ms before reloading.
await page.waitForTimeout(900);

const after = await page.evaluate(() => document.querySelector('#scroller').scrollTop);
ok('the grid scrolls back to the top when a search changes the results',
   after === 0, `scrollTop=${after} (still scrolled from before the search)`);

const resultCount = await page.evaluate(() => document.querySelectorAll('#grid .cell').length);
ok('the search actually narrowed the results', resultCount > 0 && resultCount < 90,
   `${resultCount} cells rendered`);

ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
