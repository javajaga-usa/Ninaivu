// @fixture year
/**
 * Young faces, older names, in a browser: no Create menu entry until the
 * console switches young_faces on; then it opens on the people somebody may
 * be, each unnamed young face beside the person's earliest named one with why
 * (the year, how old they were, a mother in the same photograph), "or her
 * sister?" when two fit; yes names the face, no is kept, a relative is asked
 * with a "Who is this?" link, the year somebody was born is asked for and
 * sent, and a photograph opens in the viewer. The same in Tamil, and on a
 * phone.
 *
 * The server's answer is stood in for (page.route): a real suggestion needs
 * faces with likenesses across decades, which a drawn library does not have.
 * What the server does with them is tests/test_young_faces.py.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

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
      method: 'POST', headers, body: JSON.stringify({ settings: { young_faces: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

function photo(item) {
  return { id: item.id, thumb_v: item.thumb_v || '', date: '', width: item.width, height: item.height };
}

function suggestions(items, born) {
  const at = (i) => items[i % items.length];
  const card = (faceId, i, extra) => ({
    face_id: faceId, asset_id: at(i).id, person_id: 1, score: 0.47, clear: 0.05,
    year: 1962, age: born ? 1962 - born : null, gap: 13, family: [], also: null,
    photo: photo(at(i)), ...extra,
  });
  return {
    faces: 5120,
    min_gap: 2,
    people: [
      { person: { id: 1, name: 'Paati', born, face: 901, earliest: 1975, earliest_face: 902 },
        faces: [
          card(101, 0, { family: [{ id: 3, name: 'Lakshmi', relation: 'parent' }] }),
          card(102, 1, { year: 0, age: null, gap: 0, also: { id: 2, name: 'Kamala', face: 903 } }),
          card(103, 2, { year: 1958 }),
        ] },
      { person: { id: 4, name: 'Thatha', born: 1940, face: 904, earliest: 1968, earliest_face: 905 },
        faces: [card(104, 3, { person_id: 4, year: 1950, age: 10, gap: 18 })] },
    ],
    ask_born: born ? [] : [{ id: 1, name: 'Paati', face: 901 }],
  };
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/young-faces-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const items = await page.evaluate(async () => {
    const r = await fetch('/api/assets?limit=40');
    return ((await r.json()).items || []).filter((i) => i.kind === 'picture');
  });
  const sent = { yes: [], no: [], ask: [], born: [] };
  let born = null;
  await page.route(/\/api\/young-faces$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(suggestions(items, born)),
  }));
  await page.route(/\/api\/young-faces\/(\d+)\/(yes|no)$/, (route) => {
    const [, face, what] = route.request().url().match(/\/api\/young-faces\/(\d+)\/(yes|no)$/);
    sent[what].push({ face: Number(face), ...JSON.parse(route.request().postData() || '{}') });
    return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  await page.route(/\/api\/young-faces\/born\/1$/, (route) => {
    sent.born.push(JSON.parse(route.request().postData() || '{}'));
    born = sent.born.at(-1).year;
    return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  await page.route(/\/api\/ask-family\/questions$/, (route) => {
    sent.ask.push(JSON.parse(route.request().postData() || '{}'));
    return route.fulfill({ contentType: 'application/json',
                           body: JSON.stringify({ token: 'abc', url: '/ask/abc', question: {} }) });
  });
  // A face crop is a corner of a real photograph here.
  await page.route(/\/api\/faces\/thumb\/(\d+)$/, async (route) => {
    const n = Number(route.request().url().match(/(\d+)$/)[1]);
    const response = await route.fetch({ url: new URL(`/api/thumb/${items[n % items.length].id}?s=256`, page.url()).href });
    return route.fulfill({ response });
  });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  return { context, page, shot, items, sent };
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has photographs', desk.items.length >= 4, String(desk.items.length));
ok('off: no entry in the Create menu', await desk.page.locator('#young-btn').evaluate((b) => b.hidden));
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('on: in the Create menu', !(await desk.page.locator('#young-btn').evaluate((b) => b.hidden)));

async function tour({ page, shot, sent }, label, words) {
  sent.yes.length = 0;
  sent.no.length = 0;
  sent.ask.length = 0;
  await page.evaluate(() => document.getElementById('young-btn').click());
  await page.waitForSelector('#young-faces:not([hidden]) .yf-card', { timeout: 15000 });
  ok(`${label}: a section for each person`, (await page.locator('#young-faces .yf-person').count()) === 2);
  ok(`${label}: a card for each face`, (await page.locator('#young-faces .yf-card').count()) === 4);
  const first = page.locator('#young-faces .yf-card').first();
  const why = await first.locator('.yf-why').textContent();
  ok(`${label}: the year and the mother beside her`, why.includes('1962') && why.includes('Lakshmi'), why);
  ok(`${label}: her earliest named face beside it`,
    (await first.locator('.yf-known figcaption').textContent()).includes('1975'));
  const second = page.locator('#young-faces .yf-card').nth(1);
  ok(`${label}: or her sister`, (await second.locator('.yf-question').textContent()).includes('Kamala'));
  ok(`${label}: an undated print says so`, (await second.locator('.yf-why').textContent()).includes(words.unknown));
  ok(`${label}: the year she was born is asked`, (await page.locator('#young-faces .yf-born').count()) === 1);
  await page.waitForTimeout(500);
  await shot('1-people');

  await first.locator('.yf-actions .btn.primary').click();
  await page.waitForSelector('#young-faces .yf-card.yf-done', { timeout: 10000 });
  ok(`${label}: yes names her`, sent.yes[0]?.face === 101 && sent.yes[0]?.person_id === 1, JSON.stringify(sent.yes));
  await second.locator('.yf-actions .btn').nth(1).click();
  await page.waitForFunction(() => document.querySelectorAll('#young-faces .yf-card.yf-done').length === 2);
  ok(`${label}: or her sister`, sent.yes[1]?.face === 102 && sent.yes[1]?.person_id === 2, JSON.stringify(sent.yes));
  const third = page.locator('#young-faces .yf-card').nth(2);
  await third.locator('.yf-actions .btn.ghost').first().click();
  await page.waitForFunction(() => document.querySelectorAll('#young-faces .yf-card.yf-done').length === 3);
  ok(`${label}: no is sent`, sent.no[0]?.face === 103 && sent.no[0]?.person_id === 1, JSON.stringify(sent.no));
  const fourth = page.locator('#young-faces .yf-card').nth(3);
  await fourth.locator('.yf-actions .btn.ghost').nth(1).click();
  await page.waitForSelector('#young-faces .yf-card .yf-link', { timeout: 10000 });
  ok(`${label}: a relative is asked about that face alone`, JSON.stringify(sent.ask[0]?.face_ids) === '[104]',
    JSON.stringify(sent.ask));
  ok(`${label}: the link to send`, (await page.locator('#young-faces .yf-link').inputValue()).endsWith('/ask/abc'));
  await page.waitForTimeout(300);
  await shot('2-answered');

  await page.locator('#young-faces .yf-close').click();
  ok(`${label}: closes`, await page.locator('#young-faces').isHidden());
}

await tour(desk, 'English', { unknown: 'Year not known' });

// A photograph opens in the viewer, and the page is still there after.
await desk.page.evaluate(() => document.getElementById('young-btn').click());
await desk.page.waitForSelector('#young-faces .yf-card', { timeout: 15000 });
await desk.page.locator('#young-faces .yf-photo').first().click();
await desk.page.waitForFunction(() => document.querySelector('#viewer')?.classList.contains('open')
  || !document.querySelector('#viewer')?.hidden, { timeout: 10000 });
ok('a photograph opens in the viewer', true);
await desk.page.keyboard.press('Escape');
await desk.page.waitForTimeout(500);
ok('and the page is still there', await desk.page.locator('#young-faces').isVisible());

// The year she was born, asked once and sent.
await desk.page.locator('#young-faces .yf-born input').fill('1954');
await desk.page.locator('#young-faces .yf-born .btn').click();
await desk.page.waitForFunction(() => !document.querySelector('#young-faces .yf-born'), { timeout: 10000 });
ok('the year born is sent', desk.sent.born[0]?.year === 1954, JSON.stringify(desk.sent.born));
ok('and each card says how old', (await desk.page.locator('#young-faces .yf-card').first().textContent()).includes('8'));
await desk.page.locator('#young-faces .yf-close').click();

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', { unknown: 'ஆண்டு தெரியவில்லை' });

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await phone.page.evaluate(() => document.getElementById('young-btn').click());
await phone.page.waitForSelector('#young-faces .yf-card', { timeout: 15000 });
await phone.page.waitForTimeout(600);
await phone.shot('1-people');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1
  && document.querySelector('#young-faces .yf-body').scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
