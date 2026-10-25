// @fixture year
/**
 * A life in photographs, in a browser: no Create menu entry until the
 * console switches life_story on; then it opens on everybody whose life can
 * be told, a person opens with their best photograph, name and years, a
 * chapter for each decade with the page's own words while the writing model
 * writes, its words swapped in when they come, a rail of the decades that
 * follows the reading, photographs that open in the viewer, a story to play,
 * the year somebody was born asked for and sent, and a book at the end. The
 * same in Tamil, and on a phone.
 *
 * The server's answer is stood in for (page.route): a real life needs named
 * faces across decades, which a drawn library does not have. What the server
 * does with them is tests/test_life_story.py.
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
      method: 'POST', headers, body: JSON.stringify({ settings: { life_story: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

function shown(item, date) {
  return { id: item.id, kind: 'picture', date, width: item.width, height: item.height, thumb_v: item.thumb_v || '' };
}

function life(items, { born = null } = {}) {
  const at = (i) => items[i % items.length];
  const chapter = (start, first, last, from, count, extra) => ({
    start, end: start + 9, first_year: first, last_year: last,
    ages: born ? [first - born, last - born] : null,
    items: count * 7, pictures: count * 7, videos: 0, years: last - first + 1,
    photos: Array.from({ length: count }, (_, k) => shown(at(from + k), `${first + (k % (last - first + 1))}-0${1 + (k % 9)}-1${k % 9}`)),
    story_photos: [], places: [], with: [], new: [], born: [], trips: [], stories: [], stories_count: 0,
    ...extra,
  });
  const thatha = { id: 2, name: 'Thatha', face: null, relation: 'spouse', photos: 40 };
  const amma = { id: 3, name: 'Amma', face: null, relation: 'child', photos: 61 };
  return {
    person: { id: 1, name: 'Paati', face: null, born, photos: 214, undated: 3,
              first_year: 1968, last_year: 2023, capped: false },
    first: shown(at(0), '1968-04-02'),
    cover: shown(at(5), '1975-01-14'),
    this_year: 2026,
    writer: true,
    written: null,
    chapters: [
      chapter(1960, 1968, 1969, 0, 3, {
        places: [{ place: 'Madurai, India', items: 9 }],
        new: [{ ...thatha, first: '1968-04-02', born: null }],
        with: [thatha],
        stories: [{ id: 7, asset: at(0).id, speaker: 'Paati', text: 'எங்கள் கல்யாண நாள், மதுரையில்.',
                    english: 'Our wedding day, in Madurai.', duration: 12 }],
        stories_count: 1,
      }),
      chapter(1970, 1971, 1979, 3, 9, {
        places: [{ place: 'Madurai, India', items: 40 }, { place: 'Chennai, India', items: 12 }],
        born: [{ id: 3, name: 'Amma', year: 1972, relation: 'child' }],
        new: [{ ...amma, first: '1972-08-10', born: 1972 }],
        with: [amma, thatha],
        trips: [{ id: 4, title: 'Ooty', place: 'Ooty, India', days: 5, items: 30, cover: null, started_at: 0 }],
      }),
      chapter(2010, 2015, 2023, 12, 6, {
        places: [{ place: 'Chennai, India', items: 60 }],
        with: [amma],
      }),
    ],
  };
}

const WRITTEN = {
  opening: 'Paati, from a young bride in Madurai to the heart of a large family.',
  chapters: [
    { title: 'Madurai, and Thatha', text: 'The first photographs are of the wedding in Madurai in 1968.' },
    { title: 'Amma arrives', text: 'Amma was born in 1972, and the family went to Ooty for five days.' },
    { title: 'Chennai years', text: 'Later years in Chennai, most often with Amma.' },
  ],
  model: 'gemma-3-12b',
};

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/life-story-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const items = await page.evaluate(async () => {
    const r = await fetch('/api/assets?limit=40');
    return ((await r.json()).items || []).filter((i) => i.kind === 'picture');
  });
  const sent = { born: [], words: [] };
  let born = null;
  await page.route(/\/api\/life$/, (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ people: [{ id: 1, name: 'Paati', photos: 214, first: 1968, last: 2023, face: null },
                                    { id: 3, name: 'Amma', photos: 61, first: 1972, last: 2023, face: null }],
                           writer: true, why: '' }),
  }));
  await page.route(/\/api\/life\/1(\?.*)?$/, (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify(life(items, { born })),
  }));
  await page.route(/\/api\/life\/1\/words$/, async (route) => {
    sent.words.push(JSON.parse(route.request().postData() || '{}'));
    await new Promise((resolve) => setTimeout(resolve, 1200));
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(WRITTEN) });
  });
  await page.route(/\/api\/life\/1\/born$/, (route) => {
    sent.born.push(JSON.parse(route.request().postData() || '{}'));
    born = sent.born.at(-1).year;
    return route.fulfill({ contentType: 'application/json', body: '{"ok":true}' });
  });
  await page.route(/\/api\/stories\/7\/audio$/, (route) => route.fulfill({ status: 404, body: '' }));
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  return { context, page, shot, items, sent };
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has photographs to tell with', desk.items.length >= 18, String(desk.items.length));
// On by default since 3.1: turned off first, to see what off looks like.
ok('the console switches it off', (await switchOn(desk.context, false)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('off: no entry in the Create menu', await desk.page.locator('#life-btn').evaluate((b) => b.hidden));
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
ok('on: in the Create menu', !(await desk.page.locator('#life-btn').evaluate((b) => b.hidden)));

async function tour({ page, shot, sent }, label, words) {
  sent.words.length = 0;
  await page.evaluate(() => document.getElementById('life-btn').click());
  await page.waitForSelector('#life-story:not([hidden]) .ls-pick-card', { timeout: 15000 });
  ok(`${label}: everybody whose life can be told`, (await page.locator('#life-story .ls-pick-card').count()) === 2);
  await page.waitForTimeout(400);
  await shot('1-people');

  await page.locator('#life-story .ls-pick-card').first().click();
  await page.waitForSelector('#life-story .ls-chapter', { timeout: 15000 });
  const name = await page.locator('#life-story .ls-name').textContent();
  ok(`${label}: the person's name across the top`, name.trim() === 'Paati', name);
  ok(`${label}: a chapter for each decade`, (await page.locator('#life-story .ls-chapter').count()) === 3);
  ok(`${label}: plain words while the model writes`,
    (await page.locator('#life-story .ls-writing').count()) === 1
    && (await page.locator('#life-story .ls-text.ls-written').count()) === 0);
  const decade = await page.locator('#life-story .ls-decade').first().textContent();
  ok(`${label}: the decade named`, decade.trim() === words.sixties, decade);
  ok(`${label}: asked for in the page's language`, sent.words[0]?.language === words.lang, JSON.stringify(sent.words));
  await page.waitForTimeout(500);
  await shot('2-opening');

  await page.waitForSelector('#life-story .ls-text.ls-written', { timeout: 10000 });
  const title = await page.locator('#life-story .ls-title').first().textContent();
  ok(`${label}: the model's titles swapped in`, title.trim() === 'Madurai, and Thatha', title);
  ok(`${label}: and the opening`, (await page.locator('#life-story .ls-opening').textContent()).startsWith('Paati, from'));
  ok(`${label}: the writing note goes`, (await page.locator('#life-story .ls-writing').count()) === 0);

  // The rail follows the reading down the page.
  await page.locator('#life-story .ls-rail-step').nth(1).click();
  await page.waitForTimeout(1200);
  const on = await page.locator('#life-story .ls-rail-step.on').textContent();
  ok(`${label}: the rail follows the reading`, on.trim() === '1970', on);
  ok(`${label}: who joined, with how they are related`,
    (await page.locator('#ls-ch-1 .ls-block').first().textContent()).includes('Amma'));
  ok(`${label}: the trip`, (await page.locator('#ls-ch-1 .ls-trip').textContent()).includes('Ooty'));
  await shot('3-chapter');

  await page.locator('#ls-ch-0').scrollIntoViewIfNeeded();
  ok(`${label}: the story told, in its words`,
    (await page.locator('#ls-ch-0 .ls-story blockquote').textContent()).includes('மதுரை'));
  await page.waitForTimeout(300);
  await shot('4-story');

  // A photograph opens in the viewer, over the story, and back.
  await page.locator('#ls-ch-1 .ls-tile').first().click();
  await page.waitForFunction(() => document.querySelector('#viewer')?.classList.contains('open')
    || !document.querySelector('#viewer')?.hidden, { timeout: 10000 });
  ok(`${label}: a photograph opens in the viewer`, true);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(500);
  ok(`${label}: and the story is still there`, await page.locator('#life-story').isVisible());

  ok(`${label}: a book at the end`, (await page.locator('#life-story .ls-end .btn.primary').count()) === 1);
  await page.locator('#life-story .ls-close').click();
  ok(`${label}: closes`, await page.locator('#life-story').isHidden());
}

await tour(desk, 'English', { sixties: '1960s', lang: 'en' });

// The year she was born, asked for once and sent.
await desk.page.evaluate(() => document.getElementById('life-btn').click());
await desk.page.locator('#life-story .ls-pick-card').first().click();
await desk.page.waitForSelector('#life-story .ls-born input', { timeout: 10000 });
await desk.page.locator('#life-story .ls-born input').fill('1950');
await desk.page.locator('#life-story .ls-born .btn').click();
await desk.page.waitForFunction(() => document.querySelector('#life-story .ls-meta')?.textContent.includes('18'),
  { timeout: 10000 });
ok('the year born is sent', desk.sent.born[0]?.year === 1950, JSON.stringify(desk.sent.born));
ok('and each chapter says how old', (await desk.page.locator('#life-story .ls-meta').first().textContent()).includes('18'));
ok('and it is not asked again', (await desk.page.locator('#life-story .ls-born').count()) === 0);
await desk.page.locator('#life-story .ls-close').click();

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', { sixties: '1960கள்', lang: 'ta' });

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await phone.page.evaluate(() => document.getElementById('life-btn').click());
await phone.page.locator('#life-story .ls-pick-card').first().click();
await phone.page.waitForSelector('#life-story .ls-text.ls-written', { timeout: 15000 });
await phone.page.waitForTimeout(600);
await phone.shot('2-opening');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1
  && document.querySelector('#life-story .ls-body').scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);
await phone.page.locator('#ls-ch-1').scrollIntoViewIfNeeded();
await phone.page.waitForTimeout(500);
await phone.shot('3-chapter');

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
