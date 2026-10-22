// @fixture year
/**
 * Find this print, in a browser: no entry in the Add photos menu until the
 * console switches find_print on; then a photograph of a print is sent, the
 * originals come back with the sure one marked and open in the viewer, a
 * hidden one is marked for the administrator, and when the library has no
 * copy "Take it in" opens Scan old prints with the print already found. The
 * same in Tamil, and on a phone.
 *
 * The server's answer is stood in for (page.route): what it compares and
 * who may be shown what is tests/test_find_print.py.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
const shots = process.env.NINAIVU_SHOTS;
const errors = [];

// A small grey JPEG, as the phone would send (the server is stood in for).
const JPEG = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==',
  'base64');

async function switchOn(context, value) {
  const page = await context.newPage();
  await page.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const status = await page.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { find_print: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

function answer(items, verdict) {
  const thumb = 'data:image/svg+xml;utf8,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120"><rect width="160" height="120" fill="#7a5c3a"/><rect x="18" y="14" width="124" height="92" fill="#3c6e96"/></svg>');
  const match = (item, sure, extra = {}) => ({
    id: item.id, thumb_v: item.thumb_v || '', date: '1986-04-14', width: item.width, height: item.height,
    sure, how: sure ? ['fingerprint', 'look'] : ['look'], bits: sure ? 4 : null, look: sure ? 0.95 : 0.86,
    prints: [0], ...extra,
  });
  const matches = verdict === 'none' ? [] : verdict === 'sure'
    ? [match(items[0], true), match(items[1], true, { hidden: true, explicit: false }), match(items[2], false)]
    : [match(items[0], false), match(items[1], false)];
  return {
    verdict, matches, looked: 1, search: true, session: 'a'.repeat(32), guess: null,
    photos: [{ name: 'IMG_1.jpg', prints: [{ index: 0, whole: false, width: 160, height: 120, thumb }] }],
    capabilities: { restore: false, upscale: false, year_guess: false, needs_review: false,
                    max_photos: 10, max_photo_mb: 60, folder: 'Scanned prints' },
  };
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/find-print-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const items = await page.evaluate(async () => {
    const r = await fetch('/api/assets?limit=10');
    return ((await r.json()).items || []).filter((i) => i.kind === 'picture');
  });
  const state = { verdict: 'sure', sent: 0 };
  await page.route(/\/api\/prints\/find$/, (route) => {
    state.sent += 1;
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(answer(items, state.verdict)) });
  });
  return { context, page, shot, items, state };
}

async function look(page) {
  await page.evaluate(() => document.getElementById('find-print-btn').click());
  await page.waitForSelector('#find-print-modal:not([hidden]) #find-print-pick:not([hidden])');
  await page.setInputFiles('#find-print-input', { name: 'IMG_1.jpg', mimeType: 'image/jpeg', buffer: JPEG });
  await page.waitForSelector('#find-print-result:not([hidden])', { timeout: 10000 });
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has photographs', desk.items.length >= 3, String(desk.items.length));
ok('off: not in the Add photos menu', await desk.page.locator('#find-print-btn').evaluate((b) => b.hidden));
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('on: in the Add photos menu', !(await desk.page.locator('#find-print-btn').evaluate((b) => b.hidden)));

async function tour({ page, shot, state }, label, words) {
  state.verdict = 'sure';
  await page.evaluate(() => document.getElementById('find-print-btn').click());
  await page.waitForSelector('#find-print-modal:not([hidden])');
  await shot('1-pick');
  await page.locator('#find-print-close').click();
  await look(page);
  ok(`${label}: the photograph was sent`, state.sent > 0);
  const verdict = await page.locator('#find-print-verdict').textContent();
  ok(`${label}: found it`, verdict.includes(words.found), verdict);
  ok(`${label}: every match shown`, (await page.locator('#find-print-grid .fp-tile').count()) === 3);
  ok(`${label}: the sure ones marked`, (await page.locator('#find-print-grid .fp-tile.sure').count()) === 2);
  ok(`${label}: a hidden one is marked for the administrator`,
    (await page.locator('#find-print-grid .fp-badge').textContent()).trim() === words.hidden);
  ok(`${label}: nothing to take in when it is there`, await page.locator('#find-print-take').isHidden());
  await page.waitForTimeout(400);
  await shot('2-found');

  await page.locator('#find-print-grid .fp-tile').first().click();
  await page.waitForFunction(() => document.querySelector('#viewer')?.classList.contains('open')
    || !document.querySelector('#viewer')?.hidden, { timeout: 10000 });
  ok(`${label}: a match opens in the viewer`, true);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  ok(`${label}: and the answer is still there`, await page.locator('#find-print-result').isVisible());

  state.verdict = 'maybe';
  await page.locator('#find-print-again').click();
  await page.setInputFiles('#find-print-input', { name: 'IMG_1.jpg', mimeType: 'image/jpeg', buffer: JPEG });
  await page.waitForSelector('#find-print-take:not([hidden])', { timeout: 10000 });
  ok(`${label}: likely ones, and taking it in offered`,
    (await page.locator('#find-print-grid .fp-tile.sure').count()) === 0
    && (await page.locator('#find-print-grid .fp-tile').count()) === 2);

  state.verdict = 'none';
  await page.locator('#find-print-again').click();
  await page.setInputFiles('#find-print-input', { name: 'IMG_1.jpg', mimeType: 'image/jpeg', buffer: JPEG });
  await page.waitForFunction(() => document.querySelector('#find-print-verdict').textContent
    && !document.querySelector('#find-print-take').hidden
    && document.querySelectorAll('#find-print-grid .fp-tile').length === 0, { timeout: 10000 });
  ok(`${label}: no copy yet`, (await page.locator('#find-print-verdict').textContent()).includes(words.none));
  await shot('3-none');
  await page.locator('#find-print-take').click();
  await page.waitForSelector('#prints-modal:not([hidden]) #prints-review:not([hidden])', { timeout: 10000 });
  ok(`${label}: Scan old prints opens with the print found`,
    (await page.locator('#prints-grid .prints-tile').count()) === 1
    && await page.locator('#find-print-modal').isHidden());
  await page.waitForTimeout(300);
  await shot('4-take-in');
  await page.locator('#prints-close').click();
}

await tour(desk, 'English', { found: 'Found it', hidden: 'Hidden', none: 'no copy' });

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', { found: 'கிடைத்தது', hidden: 'மறைக்கப்பட்டவை', none: 'நூலகத்தில் இல்லை' });

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await look(phone.page);
await phone.page.waitForTimeout(500);
await phone.shot('2-found');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
