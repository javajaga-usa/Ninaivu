/**
 * The Health page's Copies: how many photographs are in at least two places,
 * and each copy's own line. Read from what the copies confirmed they keep
 * (ninaivu/copies); opening the page sends and checks nothing.
 *
 *   node tests/copies_ui.mjs   (wants the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];
const context = await b.newContext({ viewport: { width: 1280, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);

const asked = await page.evaluate(async () => (await fetch('/api/admin/copies')).json());
ok('the console answers how many are in two places',
  Number.isInteger(asked.headline?.total) && Array.isArray(asked.destinations), JSON.stringify(asked).slice(0, 200));

await page.click('#tab-groups button[data-group="backup"]').catch(() => {});
await page.click('#tabs button[data-tab="health"]');
await page.waitForFunction(() => document.querySelector('#copies-headline')?.textContent.trim(),
  null, { timeout: 15000 });
const headline = (await page.textContent('#copies-headline')).trim();
const total = asked.headline.total;
ok('the headline says it',
  total ? headline.includes('photographs are in at least two places') : headline === 'No photographs yet', headline);
const cards = await page.$$eval('#copies-destinations .card-label', (nodes) => nodes.map((n) => n.textContent.trim()));
ok('one line for each copy', cards.length === asked.destinations.length, JSON.stringify(cards));
ok('each named as the household knows it',
  cards.includes('Second disk') && cards.includes('Off-site copy'), JSON.stringify(cards));
const offs = await page.$$eval('#copies-destinations .card-sub', (nodes) => nodes.map((n) => n.textContent.trim()));
ok('a copy not set up says Off', offs.includes('Off'), JSON.stringify(offs));

/* ---------- in Tamil ---------- */
await page.evaluate(() => { localStorage.setItem('ninaivu.lang', 'ta'); });
await page.reload();
await page.click('#tab-groups button[data-group="backup"]').catch(() => {});
await page.click('#tabs button[data-tab="health"]');
await page.waitForFunction(() => document.querySelector('#copies-headline')?.textContent.trim(),
  null, { timeout: 15000 });
const tamil = await page.$$eval('#copies-destinations .card-label', (nodes) => nodes.map((n) => n.textContent.trim()));
ok('in Tamil, the copies are named in Tamil', tamil.includes('இரண்டாவது வட்டு'), JSON.stringify(tamil));

ok('no script errors', errs.length === 0, errs.join('\n'));
await done(b);
