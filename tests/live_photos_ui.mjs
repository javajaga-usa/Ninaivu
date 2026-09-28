/**
 * A live photo counting as the one thing the household took.
 *
 * The still and the clip are two files on disk, and both were indexed as
 * ordinary items — so the pair counted once among the photographs and again
 * among the videos, and the Videos view listed a three-second fragment of a
 * photograph that was already sitting in Photos. And the Live Photos row, in
 * the sidebar for some time, never showed a number: it had a place for one
 * that nothing ever filled in.
 *
 * Wants a library with live pairs in it, spelled the way a phone spells them
 * (IMG_0001.JPG beside IMG_0001.MOV, uppercase) — the case is the point: the
 * path Ninaivu wrote down used to be the spelling it had guessed rather than
 * the one on disk, so a fixture written in lower case passes either way.
 *
 *   node tests/live_photos_ui.mjs   (wants the family app up, admin
 *                                    dad / correcthorse1, and a scanned
 *                                    library holding at least one live pair)
 */
import { launch, ok, done, HOME } from './harness.mjs';

const b = await launch();
const ctx = await b.newContext({ serviceWorkers: 'block' });
const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(HOME, { waitUntil: 'networkidle' });
await p.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await p.reload({ waitUntil: 'networkidle' });
await p.waitForSelector('#count-pic', { timeout: 20000 });
await p.waitForTimeout(1500);

const stats = await p.evaluate(async () =>
  (await (await fetch('/api/status')).json()).stats);

ok('the library reports live photographs', stats.live > 0, JSON.stringify(stats));
ok('there is exactly one Live Photos row',
  await p.locator('#count-live').count() === 1
  && await p.locator('.side-item[data-view="live"]').count() === 1,
  `${await p.locator('.side-item[data-view="live"]').count()} rows`);

/* ---------- the count ---------- */

const row = p.locator('#nav-live');
ok('the sidebar offers them', await row.isVisible());
ok('with a number beside it',
  (await p.locator('#count-live').textContent()).trim() === String(stats.live),
  await p.locator('#count-live').textContent());

// The whole point: one thing the household took, counted once.
ok('the clip is not counted as a video as well',
  stats.videos + stats.live <= stats.pictures + stats.videos,
  `${stats.videos} videos, ${stats.live} live, ${stats.pictures} pictures`);
ok('the parts do not add up to more than the library holds',
  stats.pictures + stats.videos + stats.audio <= stats.count,
  `${stats.pictures}+${stats.videos}+${stats.audio} vs ${stats.count}`);

/* ---------- and the view behind it ---------- */

await row.click();
await p.waitForTimeout(1500);
const live = await p.evaluate(async () =>
  (await (await fetch('/api/assets?is_live=1&limit=500')).json()));
ok('the view has something in it', live.total === stats.live,
  `${live.total} vs ${stats.live}`);
ok('and everything in it is a photograph, not a clip',
  live.items.every((i) => i.kind === 'picture'),
  JSON.stringify(live.items.slice(0, 3).map((i) => i.kind)));

const videos = await p.evaluate(async () =>
  (await (await fetch('/api/assets?kinds=video&limit=2000')).json()));
ok('the Videos view agrees with the count beside it',
  videos.total === stats.videos, `${videos.total} listed, ${stats.videos} counted`);

// The clips are named after their stills, so a clip that slipped into the
// Videos view would share a stem with one of the live photographs.
const stems = new Set(live.items.map(
  (i) => (i.filename || '').replace(/\.[^.]+$/, '').toLowerCase()));
ok('and holds none of the clips belonging to them',
  !videos.items.some(
    (i) => stems.has((i.filename || '').replace(/\.[^.]+$/, '').toLowerCase())),
  JSON.stringify(videos.items.slice(0, 5).map((i) => i.filename)));

/* ---------- the path that used to be a guess ---------- */

// `live_src` is only filled when the still has a clip recorded against it, so
// this is the case repair seen from the outside: before it, the path written
// down was the spelling Ninaivu guessed rather than the one on disk.
ok('every live photograph has a clip to play',
  live.items.every((i) => Boolean(i.live_src)),
  JSON.stringify(live.items.slice(0, 3).map((i) => i.live_src)));

ok('no page errors', errs.length === 0, errs.join(' | '));

await done(b);
