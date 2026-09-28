/**
 * Companion to search_scroll.mjs: makes sure the fix for the search-scroll
 * bug didn't overcorrect. An in-place refresh of the *same* query (a bulk
 * favourite/unfavourite here) must NOT reset the scroll position — only a
 * genuinely new query should.
 *
 *   node tests/search_scroll_regression.mjs
 */
import { launch, ok, done, HOME, signInAtHome } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 900, height: 650 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
await page.waitForSelector('#scroller', { state: 'attached', timeout: 15000 });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0,
  { timeout: 15000 });

await page.evaluate(() => { document.querySelector('#scroller').scrollTop = 900; });
await page.waitForTimeout(150);
const before = await page.evaluate(() => document.querySelector('#scroller').scrollTop);
ok('scrolled down before the bulk edit', before > 200, `scrollTop=${before}`);

// Select a couple of currently-mounted cells (Grid.onClick treats a plain
// click as "open the viewer" — selecting needs a modifier key, same as a
// real ctrl/cmd-click) and toggle favourite — same call path
// (reload() -> grid.setData()) the search fix touched, but with no
// resetScroll flag, exactly like bulk()/deleteSelected()/rotate do.
const selected = await page.evaluate(() => {
  const cells = [...document.querySelectorAll('#grid .cell')].slice(0, 2);
  for (const cell of cells) {
    cell.dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true }));
  }
  return cells.length;
});
await page.waitForTimeout(150);
ok('two cells were selected', selected === 2, `selected ${selected}`);
const favBtn = await page.$('#sel-fav');
ok('the selection bar\'s favourite button appeared', !!favBtn && await favBtn.isVisible());
if (favBtn) {
  await favBtn.click();
  await page.waitForTimeout(600);
}

const after = await page.evaluate(() => document.querySelector('#scroller').scrollTop);
ok('an in-place bulk edit does not reset the scroll position', after > 200,
   `scrollTop=${after} (expected it to stay near ${before})`);

ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
