/**
 * Letters read aloud, in a browser: hidden until the console switches it on;
 * Read aloud opens the photograph beside its words; the words read off the
 * photograph are offered to correct, kept, read aloud with the sentence
 * being read lit, and the viewer's keys left alone while the letter is open.
 *
 * The runner has neither the text reader's answer for a fixture photograph
 * nor the voices, so those two answers are made here; keeping the words is
 * the real server's.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
// The page's own service worker would answer GETs before page.route sees them.
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

const LETTER = 'அன்புள்ள கண்ணன்,\nநாங்கள் மதுரை வந்து சேர்ந்தோம்.\n\nஆசீர்வாதங்களுடன், அப்பா';

/** Three seconds of quiet, as a WAV, for the reading. */
function silence(seconds = 3, rate = 8000) {
  const samples = seconds * rate;
  const buffer = Buffer.alloc(44 + samples * 2);
  buffer.write('RIFF', 0); buffer.writeUInt32LE(36 + samples * 2, 4); buffer.write('WAVE', 8);
  buffer.write('fmt ', 12); buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20);
  buffer.writeUInt16LE(1, 22); buffer.writeUInt32LE(rate, 24); buffer.writeUInt32LE(rate * 2, 28);
  buffer.writeUInt16LE(2, 32); buffer.writeUInt16LE(16, 34); buffer.write('data', 36);
  buffer.writeUInt32LE(samples * 2, 40);
  return buffer;
}

async function moreMenu() {
  if (await page.locator('#v-more').isVisible()) await page.click('#v-more');
}

async function switchOn(value) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { letters_aloud: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await consolePage.close();
  return answer;
}

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
const first = await page.evaluate(async () => {
  const { items } = await (await fetch('/api/assets?limit=100')).json();
  const shown = new Set([...document.querySelectorAll('#grid .cell[data-id]')].map((c) => Number(c.dataset.id)));
  return items.find((i) => i.kind === 'picture' && shown.has(i.id))?.id;
});
ok('a photograph to read', Number.isFinite(first), String(first));

await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
ok('off: no Read aloud', await page.locator('#v-letter').isHidden());
await page.keyboard.press('Escape');

ok('the console switches it on', (await switchOn(true)) === 200);
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });

// Both voices said to be ready; the rest of the answer is the server's.
await page.route(new RegExp(`/api/asset/${first}/letter$`), async (route) => {
  const real = await route.fetch();
  const body = await real.json();
  body.voices = { ta: { ready: true, why: '' }, en: { ready: true, why: '' } };
  body.reader = true;
  if (body.words && readingMade) body.reading = READING;
  await route.fulfill({ response: real, json: body });
});
await page.route(new RegExp(`/api/asset/${first}/letter/read$`), (route) => route.fulfill({
  json: { words: LETTER.replace('மதுரை', 'மதுறை'), language: 'ta', lines: 3 } }));
let readingMade = false;
const READING = {
  audio: `/api/asset/${first}/letter/voice.wav?k=0123456789abcdef0123`, duration: 3,
  segments: [{ text: 'அன்புள்ள கண்ணன்,', start: 0, end: 0.9 },
    { text: 'நாங்கள் மதுரை வந்து சேர்ந்தோம்.', start: 1.3, end: 2.2, paragraph: true },
    { text: 'ஆசீர்வாதங்களுடன், அப்பா', start: 2.4, end: 3 }],
};
await page.route(new RegExp(`/api/asset/${first}/letter/voice$`), async (route) => {
  readingMade = true;
  const real = await (await route.fetch({ method: 'GET', url: route.request().url().replace(/\/voice$/, '') })).json();
  await route.fulfill({ json: { ...real, voices: { ta: { ready: true, why: '' }, en: { ready: true, why: '' } }, reading: READING } });
});
await page.route(/\/letter\/voice\.wav/, (route) => route.fulfill({ status: 200, contentType: 'audio/wav', body: silence() }));

await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await moreMenu();
await page.waitForSelector('#v-letter:not([hidden])', { timeout: 8000 });
ok('on: Read aloud is offered', (await page.locator('#v-letter').textContent()).includes('Read aloud'));
await page.click('#v-letter');
await page.waitForSelector('.letter-read:not(.busy) .lr-edit:not([hidden])', { timeout: 8000 });
ok('with no words yet, the box to type them is open', true);
ok('and the photograph is beside it',
  (await page.locator('.letter-read .lr-photo img').getAttribute('src') || '').length > 0);
await page.click('.letter-read .lr-scan');
await page.waitForFunction(() => document.querySelector('.letter-read .lr-edit').value.includes('மதுறை'), null, { timeout: 8000 });
ok('the words read off the photo are offered to correct', true);
ok('Tamil is chosen for Tamil words',
  await page.locator('.letter-read .lr-lang.on').getAttribute('data-lang') === 'ta');
await shot('letter-read-to-correct');
await page.fill('.letter-read .lr-edit', LETTER);
await page.keyboard.press('ArrowRight');
ok('typing in the box does not page to the next photograph',
  await page.locator('.letter-read').count() === 1);
const kept = page.waitForResponse((r) => r.url().endsWith(`/api/asset/${first}/letter`) && r.request().method() === 'PUT');
await page.click('.letter-read .lr-save');
ok('the corrected words are kept', (await kept).status() === 200);
await page.waitForSelector('.letter-read .lr-play:not([hidden])', { timeout: 8000 });
ok('kept, they are shown as written', (await page.locator('.letter-read .lr-words').textContent()).includes('மதுரை'));
await page.click('.letter-read .lr-play');
await page.waitForSelector('.letter-read .lr-sentence', { timeout: 8000 });
ok('read aloud, the words become sentences', await page.locator('.letter-read .lr-sentence').count() === 3);
await page.locator('.letter-read .lr-sentence').nth(1).click();
await page.waitForFunction(() => document.querySelector('.letter-read .lr-sentence.now'), null, { timeout: 8000 });
ok('the sentence being read lights up',
  (await page.locator('.letter-read .lr-sentence.now').textContent()).includes('மதுரை'));
await shot('letter-reading');
await page.keyboard.press('ArrowRight');
ok('the arrows do not page past an open letter', await page.locator('.letter-read').count() === 1);
await page.keyboard.press('Escape');
ok('Escape closes the letter and leaves the viewer open',
  await page.locator('.letter-read').count() === 0 && await page.locator('#viewer').isVisible());

// A phone: the photograph a strip above the words.
await page.setViewportSize({ width: 390, height: 844 });
await moreMenu();
await page.click('#v-letter');
await page.waitForSelector('.letter-read:not(.busy) .lr-words', { timeout: 8000 });
const box = await page.locator('.letter-read .lr-paper').boundingBox();
ok('on a phone the words fit the width', box.width <= 390, String(box.width));
await shot('letter-phone');
await page.keyboard.press('Escape');

await page.evaluate(async (id) => {
  await fetch(`/api/asset/${id}/letter`, { method: 'DELETE', headers: { 'X-Requested-With': 'fetch' } });
}, first);
ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(false);
await done(browser);
