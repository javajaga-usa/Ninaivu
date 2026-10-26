/**
 * Picking several search results at once, as in the gallery.
 *
 * A mouse brings up each tile's tick on hover; a phone has no hover, and its
 * only way in was a section's "Select all" — which, over a search's one
 * "Best matches" section, picked every result. A finger held on a tile now
 * picks it, and taps after that add and take away, without opening the
 * viewer. The search is answered as a ranking by meaning (one "match"
 * section), the way it comes back when a picture search model is installed.
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const browser = await launch();
const errors = [];

async function open(options) {
  // No service worker, so that the route below sees the grid's requests.
  const context = await browser.newContext({ ...options, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(e.message));
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, null, { timeout: 15000 });
  await page.route('**/api/segments**', async (route) => {
    if (!new URL(route.request().url()).searchParams.get('q')) return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    // Whatever the gallery holds, ranked as one list, so that there are
    // tiles to pick whatever the words matched.
    const all = await (await route.fetch({ url: route.request().url().replace(/q=[^&]*/, 'q=') })).json();
    const items = all.segments.flatMap((s) => s.items);
    body.segments = [{ key: 'match', items }];
    body.semantic = true;
    body.total = items.length;
    body.next_offset = null;
    return route.fulfill({ response, json: body });
  });
  await page.fill('#search', 'birthday cake');
  await page.waitForFunction(() => document.querySelector('#grid .section-head .title')?.textContent === 'Best matches',
    null, { timeout: 10000 });
  return { context, page };
}

const count = (page) => page.evaluate(() =>
  document.querySelector('#selection-bar').hidden ? 0 : parseInt(document.querySelector('#sel-count').textContent, 10));
const viewerOpen = (page) => page.evaluate(() => !document.querySelector('#viewer').hidden);

/** A finger on tile *n*: held for *ms*, then lifted, as a phone sends it. */
async function touch(page, n, ms) {
  await page.evaluate(async ([n, ms]) => {
    const node = document.querySelectorAll('#grid .cell')[n];
    const box = node.getBoundingClientRect();
    const at = { clientX: box.left + box.width / 2, clientY: box.top + box.height / 2,
      bubbles: true, cancelable: true, pointerType: 'touch', pointerId: 7, isPrimary: true };
    const target = node.querySelector('img') || node;
    target.dispatchEvent(new PointerEvent('pointerdown', at));
    await new Promise((r) => setTimeout(r, ms));
    target.dispatchEvent(new PointerEvent('pointerup', at));
    target.dispatchEvent(new MouseEvent('click', at));
  }, [n, ms]);
  await page.waitForTimeout(150);
}

{
  const { context, page } = await open({ viewport: { width: 390, height: 800 }, hasTouch: true, isMobile: true });
  ok('phone: there are results to pick', (await page.locator('#grid .cell').count()) >= 2);
  await touch(page, 0, 700);
  ok('phone: holding a result picks it', (await count(page)) === 1, String(await count(page)));
  ok('phone: and does not open it', !(await viewerOpen(page)));
  await touch(page, 1, 40);
  ok('phone: a tap then adds another', (await count(page)) === 2);
  await touch(page, 0, 40);
  ok('phone: and a tap takes one away', (await count(page)) === 1);
  ok('phone: still no viewer', !(await viewerOpen(page)));
  ok('phone: the bar offers to add them to an album', await page.locator('#sel-album').isVisible());
  await page.click('#sel-cancel');
  ok('phone: the cross ends it', (await count(page)) === 0);
  await touch(page, 1, 40);
  ok('phone: then a tap opens the photograph again', await viewerOpen(page));
  await context.close();
}

{
  const { context, page } = await open({ viewport: { width: 1280, height: 800 } });
  const cells = page.locator('#grid .cell');
  await cells.nth(0).hover();
  await cells.nth(0).locator('.pick').click();
  await cells.nth(1).click({ modifiers: ['Shift'] });
  ok('desktop: the tick and Shift pick a run of results', (await count(page)) === 2, String(await count(page)));
  ok('desktop: without opening the viewer', !(await viewerOpen(page)));
  await context.close();
}

ok('no page errors', errors.length === 0, errors.join(' | '));
await done(browser);
