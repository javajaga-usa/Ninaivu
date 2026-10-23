/**
 * Jump to offers every year the library has, in a list that scrolls.
 *
 * Reported 23 October 2026: the panel showed five year numbers and the rest
 * could not be reached. Here the library spans 1995 to 2026, far more years
 * than the panel is tall; each one must be reachable, the list must open on
 * the year being looked at, and a wheel over it must move the years, not the
 * gallery behind. Light, dark and a phone's width each.
 *
 *   node tests/jump_years_ui.mjs   (wants the family app up on the decades library)
 * @fixture decades
 */
import path from 'node:path';
import { launch, ok, done, signInAtHome } from './harness.mjs';

const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();

async function look(label, { width, height, scheme }) {
  const context = await b.newContext({
    viewport: { width, height }, colorScheme: scheme, serviceWorkers: 'block',
    deviceScaleFactor: 2, hasTouch: width < 600, isMobile: width < 600,
  });
  const page = await context.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push(e.message));
  await signInAtHome(page);
  await page.waitForSelector('#jump:not([hidden])', { timeout: 30000 });

  // Somewhere in the middle of the timeline, so the list should open there.
  await page.evaluate(() => {
    const s = document.querySelector('.scroller');
    s.scrollTop = s.scrollHeight * 0.5;
  });
  await page.waitForTimeout(400);
  await page.click('#jump-btn');
  await page.waitForSelector('#jump-pop:not([hidden])');
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(SHOTS, `jump-years-${label}.png`) });

  const facts = await page.evaluate(() => {
    const list = document.querySelector('#jump-years');
    const box = list.getBoundingClientRect();
    const buttons = [...list.querySelectorAll('button')];
    const seen = buttons.filter((bt) => {
      const r = bt.getBoundingClientRect();
      return r.top >= box.top - 1 && r.bottom <= box.bottom + 1;
    });
    const active = list.querySelector('.active');
    const a = active?.getBoundingClientRect();
    return {
      years: buttons.map((bt) => bt.textContent),
      seen: seen.length,
      scrolls: list.scrollHeight > list.clientHeight + 4,
      activeShown: Boolean(a && a.top >= box.top - 1 && a.bottom <= box.bottom + 1),
      active: active?.textContent,
      height: Math.round(box.height),
    };
  });
  ok(`${label}: every year from 1995 to 2026 is offered`,
     facts.years.length === 32 && facts.years.includes('1995') && facts.years.includes('2026'),
     JSON.stringify(facts.years));
  ok(`${label}: more than five years are seen at once`, facts.seen > 5, `seen=${facts.seen} height=${facts.height}`);
  ok(`${label}: the list scrolls`, facts.scrolls);
  ok(`${label}: it opens on the year being looked at`, facts.activeShown, `active=${facts.active}`);

  // A wheel over the years moves them, and not the gallery behind.
  const before = await page.evaluate(() => [
    document.querySelector('#jump-years').scrollTop, document.querySelector('.scroller').scrollTop]);
  const list = await page.$('#jump-years');
  const box = await list.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.wheel(0, 4000);
  await page.waitForTimeout(500);
  const after = await page.evaluate(() => [
    document.querySelector('#jump-years').scrollTop, document.querySelector('.scroller').scrollTop]);
  if (width >= 600) {
    ok(`${label}: the wheel scrolls the years`, after[0] > before[0], `${before} -> ${after}`);
    ok(`${label}: the gallery behind stays put`, Math.abs(after[1] - before[1]) < 2, `${before} -> ${after}`);
  }
  // The oldest year can be reached and chosen.
  await page.evaluate(() => {
    const l = document.querySelector('#jump-years');
    l.scrollTop = l.scrollHeight;
  });
  const last = page.locator('#jump-years button', { hasText: '1995' });
  await last.click();
  await page.waitForTimeout(200);
  const months = await page.$$eval('#jump-months button:not(:disabled)', (bs) => bs.length);
  ok(`${label}: 1995 can be chosen and offers its months`, months === 3, `months=${months}`);
  ok(`${label}: no page errors`, errs.length === 0, errs.join('; '));
  await context.close();
}

await look('desktop-light', { width: 1280, height: 800, scheme: 'light' });
await look('desktop-dark', { width: 1280, height: 800, scheme: 'dark' });
await look('phone-light', { width: 390, height: 844, scheme: 'light' });
await look('phone-dark', { width: 390, height: 844, scheme: 'dark' });
await done(b);
