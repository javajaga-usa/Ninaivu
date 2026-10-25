// @fixture moments
/**
 * Words spoken in videos, in a browser: no strip until the console switches
 * video_words on; then a search shows "Said in videos" above its results,
 * each line said with its time and the words searched for marked, and a tap
 * opens the video in the viewer at that second with the words shown under
 * it as subtitles. The same in Tamil, and on a phone.
 *
 * The server's answers are stood in for (page.route): writing down what is
 * said needs the speech model, which this machine does not have. What the
 * server does with a model is tests/test_video_words.py.
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
      method: 'POST', headers, body: JSON.stringify({ settings: { video_words: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

const LINES = [
  { at: 4, end: 9, language: 'ta', text: 'பிறந்தநாள் வாழ்த்துக்கள் கண்ணா', english: 'Happy birthday, kanna' },
  { at: 12, end: 16, language: 'ta', text: 'கேக் வெட்டு, கேக் வெட்டு', english: 'Cut the cake, cut the cake' },
  { at: 21, end: 26, language: 'en', text: 'Blow the candles now', english: '' },
];

function vtt(which) {
  const stamp = (s) => `00:00:${String(s).padStart(2, '0')}.000`;
  const cues = LINES.map((line, n) => {
    const words = which === 'english' ? (line.english || line.text) : line.text;
    return `${n + 1}\n${stamp(line.at)} --> ${stamp(line.end)}\n${words}\n`;
  });
  return `WEBVTT\n\n${cues.join('\n')}`;
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/words-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const clip = await page.evaluate(async () => {
    const r = await fetch('/api/assets?kinds=video&limit=5');
    const items = (await r.json()).items || [];
    return items.find((i) => i.name === 'birthday.mp4') || items[0] || null;
  });
  // The server's answers: a search, the video's lines and its subtitles,
  // and the lines' pictures (the clip's own thumbnail stands in for each).
  await page.route(/\/api\/video-words\?/, (route) => {
    const q = new URL(route.request().url()).searchParams.get('q') || '';
    const hit = (line) => q.toLowerCase().split(/\s+/).every(
      (w) => `${line.text} ${line.english}`.toLowerCase().includes(w));
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        ready: true, looked_clips: 1, looked_lines: 3,
        clips: clip && LINES.some(hit) ? [{
          id: clip.id, name: clip.name, date: '2023-08-15', duration: 30, thumb_v: '', hits: 1,
          lines: LINES.filter(hit),
        }] : [],
      }),
    });
  });
  await page.route(/\/api\/video-words\/\d+$/, (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ id: clip?.id, lines: LINES, languages: ['en', 'ta'], english: true }),
  }));
  await page.route(/\/api\/video-words\/\d+\.vtt/, (route) => route.fulfill({
    contentType: 'text/vtt',
    body: vtt(new URL(route.request().url()).searchParams.get('lang')),
  }));
  await page.route(/\/api\/video-words\/\d+\/frame/, (route) => route.continue({
    url: route.request().url().replace(/\/api\/video-words\/(\d+)\/frame.*/, '/api/thumb/$1?s=320'),
  }));
  return { context, page, shot, clip };
}

async function search(page, words) {
  await page.fill('#search', words);
  await page.press('#search', 'Enter');
  await page.waitForTimeout(1200);
}

const desk = await visit('desktop', { viewport: { width: 1280, height: 800 } });
ok('the library has the clip', Boolean(desk.clip), JSON.stringify(desk.clip));
// On by default since 3.1: turned off first, to see what off looks like.
ok('the console switches it off', (await switchOn(desk.context, false)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
await search(desk.page, 'cake');
ok('off: no strip of words', await desk.page.locator('#words-banner').isHidden());
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });

async function tour({ page, shot, clip }, label, heading, query, quoted) {
  await search(page, query);
  await page.waitForSelector('#words-banner:not([hidden]) .words-card', { timeout: 10000 });
  const cards = page.locator('#words-carousel .words-card');
  ok(`${label}: a card for the line`, (await cards.count()) === 1);
  const time = await cards.first().locator('.memory-badge').textContent();
  ok(`${label}: with its time`, time === '0:12', time);
  const quote = await cards.first().locator('.words-quote').textContent();
  ok(`${label}: with what was said`, quote === quoted, quote);
  const marks = await cards.first().locator('.words-quote mark').count();
  ok(`${label}: the words searched for are marked`, marks >= 1, String(marks));
  const title = await page.locator('#words-banner .memories-title span').textContent();
  ok(`${label}: the strip is named`, title.trim() === heading, title);
  await page.waitForTimeout(500);
  await shot('1-strip');
  await cards.first().click();
  await page.waitForSelector('#viewer:not([hidden]) video', { timeout: 10000 });
  await page.waitForFunction(() => {
    const v = document.querySelector('#viewer video');
    return v && v.readyState >= 1 && v.currentTime >= 11.5;
  }, { timeout: 15000 }).catch(() => {});
  const at = await page.evaluate(() => document.querySelector('#viewer video')?.currentTime || 0);
  ok(`${label}: the video opens at the second`, at >= 11.5 && at < 20, String(at));
  await page.waitForFunction(() => document.querySelectorAll('#viewer video track').length === 2,
    { timeout: 10000 }).catch(() => {});
  const tracks = await page.evaluate(() => [...document.querySelectorAll('#viewer video track')].map(
    (t) => ({ lang: t.srclang, label: t.label, mode: t.track.mode })));
  ok(`${label}: subtitles as said and in English`, tracks.map((t) => t.lang).join(',') === 'ta,en',
    JSON.stringify(tracks));
  const shown = tracks.filter((t) => t.mode === 'showing').map((t) => t.lang);
  ok(`${label}: the page's language is shown`, shown.join(',') === (label === 'Tamil' ? 'ta' : 'en'),
    JSON.stringify(tracks));
  await page.evaluate(() => document.querySelector('#viewer video')?.pause());
  await page.waitForTimeout(600);
  await shot('2-viewer');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  await search(page, 'nothing said like this');
  ok(`${label}: words nobody said, no strip`, await page.locator('#words-banner').isHidden());
  await search(page, '');
  ok(`${label}: no words, no strip`, await page.locator('#words-banner').isHidden());
}

await tour(desk, 'English', 'Said in videos', 'cake', 'Cut the cake, cut the cake');

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', 'வீடியோக்களில் சொன்னவை', 'கேக்', 'கேக் வெட்டு, கேக் வெட்டு');

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await search(phone.page, 'birthday');
await phone.page.waitForSelector('#words-banner:not([hidden]) .words-card', { timeout: 10000 });
await phone.page.waitForTimeout(500);
await phone.shot('1-strip');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
