// @fixture year
/**
 * Festival albums, in a browser: no Create menu entry until the console
 * switches festival_albums on; then it opens on every festival as a card,
 * the ones found first with their years and next day; a festival opens on
 * its latest photograph and its years side by side, the newest in view, a
 * row of years to jump along, a year marked when found by date alone,
 * photographs that open in the viewer and back, and a book at the end. The
 * same in Tamil, and on a phone.
 *
 * The server's answer is stood in for (page.route): finding Deepavali needs
 * photographs taken on it and the picture search's model, which a drawn
 * library does not have. What the server does is tests/test_festivals.py.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
const shots = process.env.NINAIVU_SHOTS;
const errors = [];
const KEYS = ['pongal', 'puthandu', 'krishna', 'vinayagar', 'navaratri', 'deepavali', 'karthigai', 'christmas'];
const NAMES = ['Pongal', 'Tamil New Year', 'Krishna Jayanthi', 'Vinayagar Chathurthi', 'Navaratri',
               'Deepavali', 'Karthigai Deepam', 'Christmas'];
const DAYS = { 1994: '1994-11-02', 2001: '2001-11-14', 2009: '2009-10-17', 2015: '2015-11-10',
               2019: '2019-10-27', 2023: '2023-11-12', 2024: '2024-10-31', 2025: '2025-10-20' };

async function switchOn(context, value) {
  const page = await context.newPage();
  await page.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const status = await page.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { festival_albums: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

function shown(item, date) {
  return { id: item.id, kind: 'picture', date, width: item.width, height: item.height, thumb_v: item.thumb_v || '' };
}

function albums(items, { full = false } = {}) {
  const at = (i) => items[i % items.length];
  const years = Object.entries(DAYS).map(([year, day], n) => {
    const photos = Array.from({ length: 1 + (n % 6) }, (_, k) => shown(at(n * 3 + k), day));
    const entry = { year: Number(year), day, from: day, to: day, items: photos.length + 4,
                    pictures: photos.length + 4, videos: n === 2 ? 1 : 0, checked: n !== 0 };
    return full ? { ...entry, photos, ids: photos.map((p) => p.id) } : entry;
  });
  const festivals = KEYS.map((key, i) => ({
    key, index: i + 1, name: NAMES[i],
    years: key === 'deepavali' ? years : (key === 'pongal' ? years.slice(-2) : []),
    first: key === 'deepavali' ? 1994 : (key === 'pongal' ? 2024 : null),
    last: ['deepavali', 'pongal'].includes(key) ? 2025 : null,
    items: key === 'deepavali' ? 60 : (key === 'pongal' ? 12 : 0),
    cover: ['deepavali', 'pongal'].includes(key) ? shown(at(i + 2), '2025-10-20') : null,
    next: key === 'deepavali' ? '2026-11-08' : '2027-01-15',
  }));
  return { festivals, checked: true, today: '2026-10-22' };
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/festivals-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const items = await page.evaluate(async () => {
    const r = await fetch('/api/assets?limit=40');
    return ((await r.json()).items || []).filter((i) => i.kind === 'picture');
  });
  const booked = [];
  await page.route(/\/api\/festivals$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(albums(items)),
  }));
  await page.route(/\/api\/festivals\/deepavali$/, (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ ...albums(items, { full: true }).festivals[5], checked: true, today: '2026-10-22' }),
  }));
  await page.route(/\/api\/books\/options$/, (route) => {
    booked.push(true);
    return route.fulfill({ contentType: 'application/json', body: '{"fonts":{"tamil":true},"templates":[],"sizes":[]}' });
  });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  return { context, page, shot, items, booked };
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has photographs to show', desk.items.length >= 18, String(desk.items.length));
// On by default since 3.1: turned off first, to see what off looks like.
ok('the console switches it off', (await switchOn(desk.context, false)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('off: no entry in the Create menu', await desk.page.locator('#festivals-btn').evaluate((b) => b.hidden));
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('on: in the Create menu', !(await desk.page.locator('#festivals-btn').evaluate((b) => b.hidden)));

async function tour({ page, shot }, label, words) {
  await page.evaluate(() => document.getElementById('festivals-btn').click());
  await page.waitForSelector('#festivals:not([hidden]) .fv-card', { timeout: 15000 });
  ok(`${label}: a card for every festival`, (await page.locator('#festivals .fv-card').count()) === 8);
  const first = await page.locator('#festivals .fv-card .fv-card-name').first().textContent();
  ok(`${label}: the ones found first`, first.trim() === words.pongal, first);
  ok(`${label}: the rest waiting`, (await page.locator('#festivals .fv-card.fv-none').count()) === 6);
  await page.waitForTimeout(500);
  await shot('1-festivals');

  await page.locator('#festivals .fv-deepavali').click();
  await page.waitForSelector('#festivals .fv-year', { timeout: 15000 });
  const name = await page.locator('#festivals .fv-name').textContent();
  ok(`${label}: every Deepavali`, name.includes(words.deepavali), name);
  ok(`${label}: a column for each year`, (await page.locator('#festivals .fv-year').count()) === 8);
  const order = await page.locator('#festivals .fv-year-num').allTextContents();
  ok(`${label}: the oldest on the left`, order[0] === '1994' && order.at(-1) === '2025', order.join());
  await page.waitForTimeout(400);
  const scrolled = await page.locator('#festivals .fv-years').evaluate((r) => r.scrollLeft > 0);
  ok(`${label}: the newest in view`, scrolled);
  ok(`${label}: a year found by date alone is marked`, (await page.locator('#festivals .fv-by-date').count()) === 1);
  ok(`${label}: a row of years`, (await page.locator('#festivals .fv-rail-step').count()) === 8);
  await page.waitForTimeout(300);
  await shot('2-years');

  await page.locator('#festivals .fv-rail-step').first().click();
  await page.waitForTimeout(900);
  const back = await page.locator('#festivals .fv-years').evaluate((r) => r.scrollLeft);
  ok(`${label}: the years row jumps back`, back < 40, String(back));
  await shot('3-first-years');

  await page.locator('#festivals .fv-year .fv-lead').nth(1).click();
  await page.waitForFunction(() => document.querySelector('#viewer')?.classList.contains('open')
    || !document.querySelector('#viewer')?.hidden, { timeout: 10000 });
  ok(`${label}: a photograph opens in the viewer`, true);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  ok(`${label}: and the album is still there`, await page.locator('#festivals').isVisible());

  ok(`${label}: a book at the end`, (await page.locator('#festivals .fv-end .btn.primary').count()) === 1);
  await page.locator('#festivals .fv-back').click();
  await page.waitForSelector('#festivals .fv-card', { timeout: 10000 });
  ok(`${label}: back to every festival`, (await page.locator('#festivals .fv-card').count()) === 8);
  await page.locator('#festivals .fv-close').click();
  ok(`${label}: closes`, await page.locator('#festivals').isHidden());
}

await tour(desk, 'English', { pongal: 'Pongal', deepavali: 'Deepavali' });

// A book of every year opens the book sheet.
await desk.page.evaluate(() => document.getElementById('festivals-btn').click());
await desk.page.locator('#festivals .fv-deepavali').click();
await desk.page.waitForSelector('#festivals .fv-end .btn.primary', { timeout: 10000 });
await desk.page.locator('#festivals .fv-end .btn.primary').click();
await desk.page.waitForTimeout(800);
const title = await desk.page.evaluate(() => [...document.querySelectorAll('input.input')]
  .map((i) => i.value).find((v) => v.includes('Deepavali')) || '');
ok('the book is of every Deepavali', title.includes('Deepavali'), title);
await desk.page.keyboard.press('Escape');

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', { pongal: 'பொங்கல்', deepavali: 'தீபாவளி' });

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await phone.page.evaluate(() => document.getElementById('festivals-btn').click());
await phone.page.waitForSelector('#festivals .fv-card', { timeout: 15000 });
await phone.page.waitForTimeout(500);
await phone.shot('1-festivals');
await phone.page.locator('#festivals .fv-deepavali').click();
await phone.page.waitForSelector('#festivals .fv-year', { timeout: 15000 });
await phone.page.waitForTimeout(600);
await phone.shot('2-years');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1
  && document.querySelector('#festivals .fv-body').scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen but the years row', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
