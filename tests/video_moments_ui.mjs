// @fixture moments
/**
 * Moments inside videos, in a browser: no strip until the console switches
 * video_moments on; then a search shows "Moments in videos" above its
 * results, each scene with its time, and a tap opens the clip in the viewer
 * at that second. The same in Tamil, and on a phone.
 *
 * The server's answer is stood in for (page.route): finding real scenes needs
 * the picture-search model, which this machine does not have. What the
 * server does with a model is tests/test_video_moments.py.
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
      method: 'POST', headers, body: JSON.stringify({ settings: { video_moments: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await page.close();
  return status;
}

async function visit(name, options) {
  const context = await browser.newContext({ serviceWorkers: 'block', ...options });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
  const shot = (what) => (shots ? page.screenshot({ path: `${shots}/moments-${name}-${what}.png` }) : null);
  await signInAtHome(page);
  await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });
  const clip = await page.evaluate(async () => {
    const r = await fetch('/api/assets?kinds=video&limit=5');
    const items = (await r.json()).items || [];
    return items.find((i) => i.name === 'birthday.mp4') || items[0] || null;
  });
  // The server's answer to a search, and the scenes' pictures (the clip's
  // own thumbnail stands in for each).
  await page.route(/\/api\/moments\?/, (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      ready: true, looked_clips: 1, looked_moments: 3,
      clips: clip ? [{
        id: clip.id, name: clip.name, date: '2023-08-15', duration: 30, thumb_v: '', score: 0.31,
        moments: [{ at: 12, score: 0.31 }, { at: 24, score: 0.27 }],
      }] : [],
    }),
  }));
  await page.route(/\/api\/moments\/\d+\/frame/, (route) => route.continue({
    url: route.request().url().replace(/\/api\/moments\/(\d+)\/frame.*/, '/api/thumb/$1?s=320'),
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
await search(desk.page, 'candles');
ok('off: no strip of moments', await desk.page.locator('#moments-banner').isHidden());
ok('the console switches it on', (await switchOn(desk.context, true)) === 200);
await desk.page.reload({ waitUntil: 'networkidle' });
await desk.page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 30000 });

async function tour({ page, shot, clip }, label, heading) {
  await search(page, 'candles');
  await page.waitForSelector('#moments-banner:not([hidden]) .moment-card', { timeout: 10000 });
  const cards = page.locator('#moments-carousel .moment-card');
  ok(`${label}: a card for each scene`, (await cards.count()) === 2);
  const times = await cards.locator('.memory-badge').allTextContents();
  ok(`${label}: each with its time`, times.join(',') === '0:12,0:24', times.join(','));
  const title = await page.locator('#moments-banner .memories-title span').textContent();
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
  ok(`${label}: the clip opens at the second`, at >= 11.5 && at < 20, String(at));
  ok(`${label}: it is the clip`, await page.evaluate((id) => document.querySelector('#viewer video')?.src.includes(`/${id}`), clip.id));
  await page.waitForTimeout(400);
  await shot('2-viewer');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(400);
  await search(page, '');
  ok(`${label}: no words, no strip`, await page.locator('#moments-banner').isHidden());
}

await tour(desk, 'English', 'Moments in videos');

const tamil = await visit('ta', { viewport: { width: 1280, height: 800 }, locale: 'ta-IN' });
await tour(tamil, 'Tamil', 'வீடியோக்களில் தருணங்கள்');

const phone = await visit('phone', { viewport: { width: 390, height: 844 }, deviceScaleFactor: 2,
                                     isMobile: true, hasTouch: true });
await search(phone.page, 'candles');
await phone.page.waitForSelector('#moments-banner:not([hidden]) .moment-card', { timeout: 10000 });
await phone.page.waitForTimeout(500);
await phone.shot('1-strip');
const wide = await phone.page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
ok('Phone: nothing wider than the screen', wide);

ok('no script errors', errors.length === 0, errors.join(' | '));
await switchOn(desk.context, false);
await done(browser);
