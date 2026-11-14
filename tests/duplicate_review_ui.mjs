// @fixture burst
/**
 * The duplicates review, in a browser: the Duplicates view's banner opens a
 * screen with the burst's frames side by side, the best one first and kept;
 * another frame is chosen with its number key, "Hide the others" hides the
 * rest (nothing deleted), Set aside lists the decision, and Put back shows
 * every frame again.
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const browser = await launch();
const context = await browser.newContext({ viewport: { width: 1360, height: 860 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, null, { timeout: 30000 });
const before = await page.evaluate(async () => (await (await fetch('/api/duplicates/review')).json()));
ok('the burst is a group to review', before.groups.length >= 1, JSON.stringify(before.counts));
const group = before.groups[0];

await page.evaluate(() => document.querySelector('.side-item[data-view="duplicates"]').click());
await page.waitForSelector('#duplicates-banner:not([hidden])', { timeout: 10000 });
ok('the banner says how many groups', /review/.test(await page.textContent('#duplicates-headline')));
await page.click('#duplicates-review-btn');
await page.waitForSelector('#duplicates-modal:not([hidden]) .dup-col', { timeout: 10000 });
const columns = await page.locator('.dup-col').count();
ok('every copy is a column side by side', columns === Math.min(group.items.length, 24), String(columns));
ok('the best is first, and kept', await page.getAttribute('.dup-col >> nth=0', 'data-id') === String(group.best)
  && await page.locator('.dup-col.kept').count() === 1);
ok('what differs is marked', await page.locator('.dup-detail.differs').count() > 0);
ok('an administrator is offered the decisions', await page.isVisible('#dup-actions'));
await page.waitForTimeout(400);
await shot('duplicates-review');

await page.keyboard.press('2');
const chosen = await page.getAttribute('.dup-col.kept', 'data-id');
ok('the number key chooses which to keep', chosen === String(group.items[1].id), chosen);

await page.click('#dup-hide');
await page.waitForFunction((id) => !document.querySelector(`.dup-col[data-id="${id}"]`)
  || document.querySelectorAll('.dup-col').length !== 0, chosen, { timeout: 10000 });
const after = await page.evaluate(async () => (await (await fetch('/api/duplicates/review')).json()));
ok('the group is done', after.groups.every((g) => g.group !== group.group || g.items.length < 2),
  JSON.stringify(after.groups.map((g) => g.group)));
const kept = await page.evaluate(async (id) => (await fetch(`/api/asset/${id}`)).status, Number(chosen));
ok('the kept one is still there', kept === 200, String(kept));

await page.click('[data-dup-tab="aside"]');
await page.waitForSelector('.dup-aside-row', { timeout: 10000 });
ok('Set aside lists the decision', await page.locator('.dup-aside-row').count() === 1);
await shot('duplicates-set-aside');
await page.click('.dup-aside-row .btn');
await page.waitForSelector('#dup-aside-list .empty-note', { timeout: 10000 });
const again = await page.evaluate(async () => (await (await fetch('/api/duplicates/review')).json()));
ok('Put back brings the group back', again.groups.some((g) => g.group === group.group && g.count === group.count));

await page.keyboard.press('Escape');
ok('Escape closes the review', await page.isHidden('#duplicates-modal'));
ok('no page errors', errors.length === 0, errors.join(' | '));
await done(browser);
