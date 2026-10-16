/**
 * The sidebar after signing in: People, Albums and Folders are there even
 * when the first answer for one of them fails.
 *
 * Reported 16 October 2026: in the family app, the first sign-in showed no
 * People, no Albums and no Folders; signing out and in again showed them all.
 * People, trips and albums were asked for only after the tag/year/folder
 * answer came back, and not at all when it failed, so one failed answer at
 * sign-in took four sections with it, and nothing asked again until the next
 * sign-in. Each is now asked for on its own and asked again after a failure.
 *
 * Here the first answer of each of the four fails (a 503, as a busy or
 * restarting server gives), and the sidebar must still fill in.
 *
 *   node tests/sidebar_first_login_ui.mjs   (wants the family app up with a library)
 * @fixture busy
 */
import { launch, ok, done, HOME, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const b = await launch();
// No service worker: requests it makes are not seen by page.route.
const context = await b.newContext({ viewport: { width: 1300, height: 900 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

// An album to list, made before the test's own page signs in.
const setup = await b.newContext();
const sp = await setup.newPage();
await sp.goto(HOME, { waitUntil: 'networkidle' });
await sp.evaluate(async ([username, password]) => {
  const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
  await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
  await fetch('/api/albums', { method: 'POST', headers, body: JSON.stringify({ name: 'Holiday' }) });
}, [ADMIN_USER, ADMIN_PASS]);
await setup.close();

// The first answer of each sidebar question fails; later ones go through.
const failed = new Set();
const asked = {};
let armed = false;                 // from the moment the sign-in is sent
await page.route(/\/api\/(facets|albums|faces\/people|occasions)(\?|$)/, async (route) => {
  const kind = new URL(route.request().url()).pathname;
  if (!armed) { await route.continue(); return; }
  asked[kind] = (asked[kind] || 0) + 1;
  if (route.request().method() === 'GET' && !failed.has(kind)) {
    failed.add(kind);
    await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"busy"}' });
    return;
  }
  await route.continue();
});

// Signing in the way a person does: the picker, then the username form.
await page.goto(HOME, { waitUntil: 'networkidle' });
await page.waitForSelector('#gate .picker-foot .gate-skip', { timeout: 30000 });
await page.click('#gate .picker-foot .gate-skip');
await page.fill('#gate input[type="text"], #gate input[name="username"]', ADMIN_USER);
await page.fill('#gate input[type="password"]', ADMIN_PASS);
armed = true;
await page.click('#gate .btn.primary');

const sidebar = () => page.evaluate(() => {
  const shown = (id) => {
    const el = document.getElementById(id);
    return Boolean(el && !el.hidden && el.offsetParent !== null);
  };
  return {
    people: shown('people-block'),
    albums: shown('albums-block') && document.querySelectorAll('#album-list .album-card').length,
    folders: shown('folders-block') && document.querySelectorAll('#folder-list button').length,
  };
});
let seen = await sidebar();
for (let i = 0; i < 40 && !(seen.albums && seen.folders); i++) {
  await page.waitForTimeout(250);
  seen = await sidebar();
}
ok('each sidebar question failed once', failed.size === 4, `${[...failed].join(', ')} ${JSON.stringify(asked)}`);
ok('albums are listed after a failed first answer', seen.albums >= 1, JSON.stringify(seen));
ok('folders are listed after a failed first answer', seen.folders >= 2, JSON.stringify(seen));
ok('the people block is there for a signed-in member', seen.people, JSON.stringify(seen));
ok('each was asked again, not more than three times',
  Object.values(asked).every((n) => n >= 2 && n <= 3), JSON.stringify(asked));
ok('no page errors', errs.length === 0, errs.join('; '));

await done(b);
