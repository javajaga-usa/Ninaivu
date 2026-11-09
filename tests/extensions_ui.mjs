/**
 * Home & extensions: the list of extensions, drawn by its page module
 * (pages/console/extensions.js) when the page is opened, and the pages an
 * extension brings shown only while it is on, which the console asks at
 * sign-in.
 *
 *   node tests/extensions_ui.mjs   (wants the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);

const listing = await page.evaluate(async () => (await fetch('/api/admin/extensions')).json());
const studioOn = (listing.active || []).includes('creative-studio');
await page.waitForFunction(() => document.querySelector('#tabs button[data-tab="overview"]'), null,
  { timeout: 20000 });
const aiServerShown = await page.evaluate(() => {
  const tab = document.querySelector('#tabs button[data-tab="ai-server"]');
  return tab ? !tab.hidden : false;
});
ok('the AI server page shows only while Creative Studio is on', aiServerShown === studioOn,
  JSON.stringify({ studioOn, aiServerShown }));

await page.evaluate(() => document.querySelector('#tabs button[data-tab="settings"]').click());
const items = (listing.extensions || []).length;
await page.waitForFunction((count) => (count
  ? document.querySelectorAll('#extensions-list .ext-row').length === count
  : !document.querySelector('#extensions-none').hidden), items, { timeout: 15000 });
ok('every extension is listed when the page opens', true);
const said = (await page.textContent('#extensions-state')).trim();
ok('the list says how many are on', said.length > 0, said);
if (items) {
  const leaves = await page.$$eval('#extensions-list .ext-row', (rows) => rows.map((r) => r.textContent));
  ok('each says whether anything leaves this computer',
    leaves.every((t) => t.includes('leaves this computer')), leaves.join(' | ').slice(0, 300));
}

ok('no page errors', errs.length === 0, errs.join('\n'));
await done(b);
