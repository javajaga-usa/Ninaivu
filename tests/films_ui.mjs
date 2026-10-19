/**
 * Memory films from the family app: "Make a film" beside "Make a book",
 * the moments it picked, the making, and the film playing at the end.
 *
 * The switch is off by default, so the test turns `memory_films` on through
 * the console's Advanced settings, and off again at the end. The library
 * needs a few photographs and ffmpeg; without ffmpeg the making is refused
 * with a sentence, which is checked instead.
 *
 *   node tests/films_ui.mjs   (wants the family app and the console up,
 *                              admin dad / correcthorse1, a scanned library)
 */
import { launch, ok, done, HOME, ADMIN, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const b = await launch();
const ctx = await b.newContext({ serviceWorkers: 'block' });
const errs = [];

const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
const sign = async (page, url) => {
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.evaluate(async ([username, password, h]) => {
    await fetch('/api/auth/login', { method: 'POST', headers: h, body: JSON.stringify({ username, password }) });
  }, [ADMIN_USER, ADMIN_PASS, headers]);
};
const setSwitch = async (page, on) => page.evaluate(async ([value, h]) =>
  (await fetch('/api/admin/settings/all', { method: 'POST', headers: h,
    body: JSON.stringify({ settings: { memory_films: value } }) })).status, [on, headers]);

const consolePage = await ctx.newPage();
await sign(consolePage, ADMIN);
ok('the console switches films on', await setSwitch(consolePage, true) === 200);

const p = await ctx.newPage();
p.on('pageerror', (e) => errs.push(e.message));
await sign(p, HOME);
const album = await p.evaluate(async (h) => {
  const assets = await (await fetch('/api/assets?limit=40')).json();
  const items = assets.items || [];
  const ids = items.filter((a) => a.kind !== 'audio').map((a) => a.id).slice(0, 8);
  const made = await (await fetch('/api/albums', { method: 'POST', headers: h,
    body: JSON.stringify({ name: 'Film test', ids }) })).json();
  return { id: made.id || made.album?.id, n: ids.length };
}, headers);
ok('an album of photographs to film', album.id && album.n >= 1, JSON.stringify(album));

await p.reload({ waitUntil: 'networkidle' });
await p.waitForSelector(`.album-card[data-album="${album.id}"]`, { timeout: 20000 });
await p.click(`.album-card[data-album="${album.id}"]`);
const filmButton = p.locator('#filter-bar button', { hasText: 'Make a film' });
await filmButton.waitFor({ timeout: 10000 });
ok('"Make a film" sits beside "Make a book"',
  await p.locator('#filter-bar button', { hasText: 'Make a book' }).count() === 1);

await filmButton.click();
await p.waitForSelector('#film-modal:not([hidden]) #film-form', { timeout: 10000 });
ok('the settings open with the album as the title',
  await p.inputValue('#film-form input[type="text"]') === 'Film test');
ok('three paces and four looks are offered',
  await p.locator('#film-form select').nth(1).locator('option').count() === 3
  && await p.locator('#film-form select').nth(2).locator('option').count() === 4);

await p.click('#film-form button[type="submit"]');
await p.waitForSelector('#film-form .book-pick', { timeout: 15000 });
const picks = await p.locator('#film-form .book-pick input:checked').count();
ok('the moments it picked are shown, every one ticked', picks >= 1, `${picks} picked`);

// Ninaivu drafts the chapters (from dates and places here: no writing model).
await p.click('#film-form .film-storyboard button');
await p.waitForFunction(() => document.querySelector('#film-form .film-storyboard .hint')?.textContent
  && !document.querySelector('#film-form .film-storyboard button').disabled, null, { timeout: 30000 });
ok('the chapters are drafted, or it says why there are none',
  (await p.textContent('#film-form .film-storyboard .hint')).length > 10,
  await p.textContent('#film-form .film-storyboard .hint'));

await p.click('#film-form button[type="submit"]');
// Either the film is made and plays, or the refusal says why.
const outcome = await Promise.race([
  p.waitForSelector('#film-modal video.film-player', { timeout: 180000 }).then(() => 'made'),
  p.waitForSelector('.toast:has-text("Could not make the film")', { timeout: 180000 })
    .then(async (t) => `toast: ${await t.textContent()}`),
]).catch((e) => `nothing: ${e.message}`);
ok('the film is made, or the reason it cannot be is said',
  (outcome === 'made' || outcome.startsWith('toast')), outcome);

if (outcome === 'made') {
  // The Chromium Playwright ships has no H.264 decoder, so the page cannot
  // play the film here; Safari and Chrome can. What is checked is that the
  // player is handed an MP4 it can seek in.
  const served = await p.evaluate(async () => {
    const src = document.querySelector('#film-modal video.film-player').src;
    const r = await fetch(src, { headers: { Range: 'bytes=0-15' } });
    const head = new Uint8Array(await r.arrayBuffer());
    return { status: r.status, type: r.headers.get('content-type'),
             box: String.fromCharCode(...head.slice(4, 8)) };
  });
  ok('the player is given an MP4 it can seek in',
    served.status === 206 && served.type === 'video/mp4' && served.box === 'ftyp', JSON.stringify(served));
  ok('and can be downloaded',
    await p.locator('#film-modal a[download]').count() === 1);

  await p.keyboard.press('Escape');
  await p.click('#books-btn');
  await p.waitForSelector('#book-modal:not([hidden])', { timeout: 10000 });
  await p.waitForSelector('#book-modal h3:text("Your films")', { timeout: 10000 });
  ok('the film is listed in Books', await p.locator('#book-modal .book-row b', { hasText: 'Film test' }).count() >= 1);

  // The photo frame plays the films its profile made.
  await consolePage.evaluate(async (h) => fetch('/api/admin/settings/all', { method: 'POST', headers: h,
    body: JSON.stringify({ settings: { photo_frame: true } }) }), headers);
  await p.reload({ waitUntil: 'networkidle' });
  await p.waitForSelector('#frame-btn:not([hidden])', { timeout: 15000 });
  await p.click('#frame-btn');
  await p.waitForSelector('#photo-frame .frame-film-btn:not([hidden])', { state: 'attached', timeout: 15000 });
  await p.mouse.move(200, 200);
  await p.click('#photo-frame .frame-film-btn');
  const playing = await p.evaluate(() => document.querySelector('#photo-frame video.frame-film')?.src || '');
  ok('the frame plays our films', /\/api\/films\/\d+\/video$/.test(playing), playing);
  await p.keyboard.press('Escape');
  ok('and goes back to the photographs', await p.locator('#photo-frame video.frame-film').count() === 0
    && await p.locator('#photo-frame:not([hidden])').count() === 1);
  await p.keyboard.press('Escape');
  await consolePage.evaluate(async (h) => fetch('/api/admin/settings/all', { method: 'POST', headers: h,
    body: JSON.stringify({ settings: { photo_frame: false } }) }), headers);
}

ok('no script errors', errs.length === 0, errs.join(' | '));

// Leave things as they were.
await p.evaluate(async ([id, h]) => {
  const list = await (await fetch('/api/films')).json();
  for (const film of list.films || []) {
    if (film.title === 'Film test') await fetch(`/api/films/${film.id}`, { method: 'DELETE', headers: h });
  }
  await fetch(`/api/albums/${id}`, { method: 'DELETE', headers: h });
}, [album.id, headers]);
await setSwitch(consolePage, false);
await done(b);
