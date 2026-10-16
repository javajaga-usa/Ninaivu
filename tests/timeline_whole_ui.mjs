/**
 * The date rail covers the whole timeline without scrolling to the end first.
 *
 * Reported 16 October 2026: on a large library the rail "folded once". The
 * gallery arrives in pieces (25,000 photographs each) so the first screen is
 * quick, and the rail is drawn from what has arrived: it spanned only the
 * newest piece, and drew itself again, longer, when a scroll near the end
 * fetched the next. The rest is now fetched as soon as the first piece is on
 * screen, so the rail covers every year straight away.
 *
 * Here the first piece is made small (300 photographs, all from the newest
 * year) so the library needs several, and nothing is scrolled.
 *
 *   node tests/timeline_whole_ui.mjs   (wants the family app up with a library
 *                                       spread over a few years)
 * @fixture busy
 */
import { launch, ok, done, signInAtHome } from './harness.mjs';

const b = await launch();
const context = await b.newContext({ viewport: { width: 1200, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

await signInAtHome(page);
// Wait for a library big enough to need more than one small piece.
let total = 0;
for (let i = 0; i < 90 && total < 1200; i++) {
  total = await page.evaluate(async () => {
    const r = await fetch('/api/segments?limit=1', { headers: { 'X-Requested-With': 'fetch' } });
    return r.ok ? (await r.json()).total : 0;
  });
  if (total < 1200) await page.waitForTimeout(2000);
}
ok('the library needs several pieces', total >= 1200, `total=${total}`);

const pieces = [];
let received = 0;
let oldestYear = '';
await page.route(/\/api\/segments\?/, async (route) => {
  const url = new URL(route.request().url());
  const offset = Number(url.searchParams.get('offset') || 0);
  if (!offset) url.searchParams.set('limit', '300');
  pieces.push(offset);
  const response = await route.fetch({ url: url.toString() });
  const body = await response.json();
  for (const seg of body.segments || []) {
    received += seg.items.length;
    if (/^\d{4}-/.test(seg.key)) oldestYear = seg.key.slice(0, 4);
  }
  await route.fulfill({ response, json: body });
});
await page.reload({ waitUntil: 'networkidle' });

let yearsOnRail = [];
for (let i = 0; i < 80; i++) {
  yearsOnRail = await page.evaluate(() =>
    [...document.querySelectorAll('#scrubber-track span.year')].map((s) => s.textContent));
  if (received >= total && oldestYear && yearsOnRail.includes(oldestYear)) break;
  await page.waitForTimeout(250);
}
const scrolled = await page.evaluate(() => document.querySelector('#scroller').scrollTop);

ok('the rest was asked for without scrolling', pieces.some((o) => o > 0) && scrolled < 50,
  `pieces=${JSON.stringify(pieces)} scrollTop=${scrolled}`);
ok('every photograph arrived', received === total, `${received} of ${total}`);
ok('the rail names the oldest year', Boolean(oldestYear) && yearsOnRail.includes(oldestYear),
  `oldest=${oldestYear} rail=${yearsOnRail.join(',')}`);
ok('the rest came in few pieces', pieces.length <= 4, JSON.stringify(pieces));
ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
