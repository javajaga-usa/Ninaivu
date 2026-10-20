// @fixture year
/**
 * Our Year, in a browser: hidden until the console switches it on; then the
 * top bar's button opens the year full screen, the cards move on by
 * themselves and with the arrow keys, a tap on a month's photograph opens it
 * in the viewer, and the last card offers a book of the year. The same in
 * Tamil, and on a phone.
 */
import { launch, ok, done, HOME, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
const shots = process.env.NINAIVU_SHOTS;
const errors = [];

async function switchOn(context, value) {
  const page = await context.newPage();
  await page.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const status = await page.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { our_year: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

async function visit(name, options) {
  const context = await browser.newContext(options);
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/our-year-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  return { context, page, shot };
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('off: no Our year button', await desk.page.locator('#year-btn').isHidden());
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('on: the button is in the top bar', await desk.page.locator('#year-btn').count() === 1
  && !(await desk.page.locator('#year-btn').evaluate((b) => b.hidden)));

async function tour({ page, shot }, label) {
  await page.evaluate(() => document.getElementById('year-btn').click());
  await page.waitForSelector('#our-year:not([hidden]) .year-card.shown', { timeout: 15000 });
  const title = await page.locator('#our-year .year-card.shown .year-title').textContent();
  ok(`${label}: it opens on 2023`, title?.trim() === '2023', title);
  const picked = await page.locator('#our-year .year-picker').inputValue();
  ok(`${label}: the year can be changed`, picked === '2023'
    && (await page.locator('#our-year .year-picker option').count()) === 1);
  await page.waitForTimeout(900);
  await shot('1-intro');
  const total = await page.locator('#our-year .year-card').count();
  ok(`${label}: a card for each thing to tell`, total >= 5, String(total));
  ok(`${label}: one segment a card`, (await page.locator('#our-year .year-seg').count()) === total);
  const seen = [];
  for (let i = 1; i < total; i += 1) {
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(700);
    const kind = await page.locator('#our-year .year-card.shown').getAttribute('class');
    seen.push((kind.match(/year-is-([\w-]+)/) || [])[1]);
    await shot(`${i + 1}-${seen.at(-1)}`);
  }
  ok(`${label}: numbers, months and a photo a month`, ['numbers', 'months', 'every-month'].every((k) => seen.includes(k)), seen.join(','));
  ok(`${label}: it ends with a book of the year`, seen.at(-1) === 'end'
    && (await page.locator('#our-year .year-card.shown .year-actions .btn.primary').count()) === 1);
  return seen;
}

const seen = await tour(desk, 'English');
// A month's photograph opens in the viewer, and the cards wait behind it.
await desk.page.keyboard.press('ArrowLeft');
await desk.page.waitForTimeout(700);
if (seen.at(-2) === 'every-month') {
  await desk.page.locator('#our-year .year-card.shown .year-thumb').first().click();
  await desk.page.waitForSelector('#viewer:not([hidden]), .viewer:not([hidden])', { timeout: 5000 }).catch(() => {});
  const opened = await desk.page.evaluate(() => [...document.querySelectorAll('#viewer, .viewer')].some((v) => !v.hidden));
  ok('a month photograph opens in the viewer', opened);
  await desk.page.keyboard.press('Escape');
  await desk.page.waitForTimeout(500);
}
await desk.page.keyboard.press('Escape');
await desk.page.waitForTimeout(300);
ok('Escape closes it', await desk.page.locator('#our-year').isHidden());
// Left alone, the first card moves on by itself.
await desk.page.evaluate(() => document.getElementById('year-btn').click());
await desk.page.waitForSelector('#our-year:not([hidden]) .year-card.shown', { timeout: 15000 });
await desk.page.waitForTimeout(7800);
const moved = await desk.page.locator('#our-year .year-card.shown').getAttribute('class');
ok('the next card comes by itself', !moved.includes('year-is-intro'), moved);
await desk.page.keyboard.press('Escape');

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil');
const kicker = await tamil.page.locator('#our-year .year-card').first().locator('.year-kicker').textContent();
ok('Tamil: the cards are in Tamil', /நம் ஆண்டு/.test(kicker || ''), kicker);

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await tour(phone, 'Phone');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
