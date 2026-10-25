// @fixture thennow
/**
 * Then and now, in a browser: no button and no strip while the console
 * has then_and_now off (it is on by default since 3.1); then the top bar's button opens the pairs, a
 * pair opens large with the newer photograph over the older up to a handle
 * that follows the pointer and the arrow keys, or the two side by side; a
 * person's pair is held by the eyes; the years people were born are asked
 * for and sent; chosen pairs become a film; and the strip above the gallery
 * shows this week's pairs. The same in Tamil, and on a phone.
 *
 * The server's answer is stood in for (page.route): real pairs need the
 * picture-search model and the face model, which this machine does not have.
 * What the server does with them is tests/test_then_and_now.py.
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
      method: 'POST', headers, body: JSON.stringify({ settings: { then_and_now: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

function side(item, date, eyes) {
  return { id: item.id, thumb_v: item.thumb_v || '', date, width: item.width, height: item.height,
           ...(eyes ? { eyes } : {}) };
}

function answer(byName, { born = false } = {}) {
  const n = (name) => byName[name];
  const tank = { kind: 'place', then: side(n('TANK_1994.JPG'), '1994-06-18'),
                 now: side(n('TANK_2021.JPG'), '2021-06-20'), years: 27, score: 0.91,
                 place: 'Madurai, India' };
  const door = { kind: 'view', then: side(n('DOOR_1988.JPG'), '1988-06-17'),
                 now: side(n('DOOR_2019.JPG'), '2019-06-19'), years: 31, score: 0.93,
                 place: 'Coimbatore, India' };
  const amma = { id: 1, name: 'Amma' };
  const arun = { id: 2, name: 'Arun' };
  const sameAge = { kind: 'same_age', age: 6, parent: amma, child: arun, years: 30, score: 0.8,
                    then: side(n('AMMA_1966.JPG'), '1966-06-15', [[0.352, 0.35], [0.448, 0.35]]),
                    now: side(n('ARUN_1996.JPG'), '1996-06-16', [[0.504, 0.45], [0.696, 0.45]]) };
  const person = { ...sameAge, kind: 'person', person: amma, now: { ...sameAge.now } };
  return {
    places: [door, tank],
    people: [{ person: { ...amma, face: null }, pairs: [person] }],
    same_age: born ? [{ parent: { ...amma, born: 1960 }, child: { ...arun, born: 1990 }, pairs: [sameAge] }] : [],
    ask_born: born ? [] : [{ ...amma, face: null }, { ...arun, face: null }],
    films: true, min_years: 5,
  };
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/then-now-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const byName = await page.evaluate(async () => {
    const r = await fetch('/api/assets?limit=50');
    const out = {};
    for (const item of (await r.json()).items || []) out[item.name] = item;
    return out;
  });
  const sent = { born: [], film: null };
  let born = false;
  await page.route(/\/api\/then-and-now$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(answer(byName, { born })),
  }));
  await page.route(/\/api\/then-and-now\/today$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ pairs: answer(byName).places }),
  }));
  await page.route(/\/api\/then-and-now\/born\/\d+$/, (route) => {
    sent.born.push(JSON.parse(route.request().postData() || '{}'));
    born = true;
    return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  const film = { id: 9, title: 'Then and now', state: 'building', progress: { done: 3, total: 40 } };
  await page.route(/\/api\/then-and-now\/film$/, (route) => {
    sent.film = JSON.parse(route.request().postData() || '{}');
    return route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(film) });
  });
  await page.route(/\/api\/films\/9$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(film),
  }));
  // Again, now that the answers are stood in for.
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  return { context, page, shot, byName, sent };
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has the drawn pairs', Object.keys(desk.byName).length >= 6, Object.keys(desk.byName).join(','));
// On by default since 3.1: turned off first, to see that off hides it.
ok('the console switches it off', (await switchOn(desk.context, false)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('off: no Then and now button', await desk.page.locator('#then-btn').isHidden());
ok('off: no strip', await desk.page.locator('#then-now-banner').isHidden());
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });

async function tour({ page, shot, sent }, label, words) {
  await page.waitForSelector('#then-now-banner:not([hidden]) .tn-strip-card', { timeout: 10000 });
  ok(`${label}: this week's pairs above the gallery`, (await page.locator('#then-now-carousel .tn-strip-card').count()) === 2);
  await page.waitForTimeout(600);
  await shot('1-strip');

  await page.evaluate(() => document.getElementById('then-btn').click());
  await page.waitForSelector('#then-now:not([hidden]) .tn-card', { timeout: 15000 });
  ok(`${label}: a card for each place`, (await page.locator('#then-now .tn-card').count()) === 2);
  const heading = await page.locator('#then-now .tn-head h1').textContent();
  ok(`${label}: named`, heading.trim() === words.title, heading);
  ok(`${label}: three headings`, (await page.locator('#then-now .tn-tab:not([hidden])').count()) === 3);
  await page.waitForTimeout(600);
  await shot('2-places');

  // A pair, large: the handle follows the pointer and the keys.
  await page.locator('#then-now .tn-card-open').nth(1).click();
  await page.waitForSelector('.tn-compare .tn-stage img.tn-now', { timeout: 10000 });
  const box = await page.locator('.tn-compare .tn-stage').boundingBox();
  await page.mouse.move(box.x + box.width * 0.5, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.3, box.y + box.height / 2, { steps: 4 });
  await page.mouse.up();
  const split = await page.locator('.tn-compare .tn-handle').inputValue();
  ok(`${label}: the handle follows the pointer`, Math.abs(Number(split) - 30) < 3, split);
  await page.keyboard.press('ArrowRight');
  ok(`${label}: and the arrow keys`, Math.abs(Number(await page.locator('.tn-compare .tn-handle').inputValue()) - 35) < 3);
  const sub = await page.locator('.tn-compare-sub').textContent();
  ok(`${label}: the years apart are said`, sub.includes('1994') && sub.includes('2021') && sub.includes('27'), sub);
  await page.waitForTimeout(700);
  await shot('3-slider');
  await page.locator('.tn-compare-tools .btn').nth(1).click();
  ok(`${label}: side by side`, await page.locator('.tn-compare .tn-stage.side').count() === 1);
  await page.waitForTimeout(700);
  await shot('4-side-by-side');
  await page.keyboard.press('Escape');
  ok(`${label}: Escape goes back to the pairs`, await page.locator('.tn-compare').count() === 0
    && await page.locator('#then-now').isVisible());

  // A person's pair, held by the eyes: both faces' eyes in the same place.
  await page.locator('#then-now .tn-tab[data-tab="people"]').click();
  await page.locator('#then-now .tn-card-open').first().click();
  await page.waitForFunction(() => document.querySelectorAll('.tn-compare img.held').length === 2
    && [...document.querySelectorAll('.tn-compare img.held')].every((i) => i.complete && i.style.width), { timeout: 10000 });
  const eyes = await page.evaluate(() => {
    const out = [];
    for (const [img, e] of [[document.querySelector('.tn-compare img.tn-then'), [0.4, 0.35]],
      [document.querySelector('.tn-compare img.tn-now'), [0.6, 0.45]]]) {
      const r = img.getBoundingClientRect();
      out.push([r.left + e[0] * r.width, r.top + e[1] * r.height]);
    }
    return out;
  });
  ok(`${label}: the two faces' eyes meet`, Math.hypot(eyes[0][0] - eyes[1][0], eyes[0][1] - eyes[1][1]) < 6,
    JSON.stringify(eyes));
  await page.waitForTimeout(600);
  await shot('5-faces');
  await page.keyboard.press('Escape');

  // The years they were born, then parent and child at the same age.
  await page.locator('#then-now .tn-tab[data-tab="same_age"]').click();
  await page.waitForSelector('#then-now .tn-born');
  await page.waitForTimeout(400);
  await shot('6-born');
  const inputs = page.locator('#then-now .tn-born input');
  await inputs.nth(0).fill('1960');
  await inputs.nth(1).fill('1990');
  await page.locator('#then-now .tn-born button[type="submit"]').click();
  await page.waitForSelector('#then-now .tn-group .tn-card', { timeout: 10000 });
  ok(`${label}: the years are sent`, JSON.stringify(sent.born.slice(-2)) === '[{"year":1960},{"year":1990}]',
    JSON.stringify(sent.born));
  const said = await page.locator('#then-now .tn-card-text strong').first().textContent();
  ok(`${label}: parent and child at the same age`, said.includes('Amma') && said.includes('Arun') && said.includes('6'), said);
  await page.waitForTimeout(500);
  await shot('7-same-age');

  // Two pairs, chosen, become a film.
  await page.locator('#then-now .tn-card .tn-pick input').first().check();
  await page.locator('#then-now .tn-tab[data-tab="places"]').click();
  await page.locator('#then-now .tn-card .tn-pick input').first().check();
  const make = page.locator('#then-now .tn-footer button');
  ok(`${label}: the film button counts the pairs`, (await make.textContent()).includes('2'), await make.textContent());
  await page.waitForTimeout(300);
  await shot('8-chosen');
  await make.click();
  await page.waitForSelector('#film-modal:not([hidden])', { timeout: 10000 });
  ok(`${label}: the film is asked for with each face`, sent.film?.pairs?.length === 2
    && sent.film.pairs[0].then.person === 1 && sent.film.pairs[0].now.person === 2
    && sent.film.pairs[1].then.person === null, JSON.stringify(sent.film));
  await page.waitForTimeout(400);
  await shot('9-film');
  await page.evaluate(() => document.querySelector('#film-modal .icon-btn')?.click());
}

await tour(desk, 'English', { title: 'Then and now' });

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', { title: 'அன்றும் இன்றும்' });

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await phone.page.waitForSelector('#then-now-banner:not([hidden]) .tn-strip-card', { timeout: 10000 });
await phone.page.waitForTimeout(500);
await phone.shot('1-strip');
await phone.page.evaluate(() => document.getElementById('then-btn').click());
await phone.page.waitForSelector('#then-now:not([hidden]) .tn-card', { timeout: 15000 });
await phone.page.waitForTimeout(500);
await phone.shot('2-places');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);
await phone.page.locator('#then-now .tn-card-open').first().click();
await phone.page.waitForSelector('.tn-compare .tn-stage img.tn-now', { timeout: 10000 });
await phone.page.waitForTimeout(700);
await phone.shot('3-slider');

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
