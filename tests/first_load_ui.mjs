/**
 * What the family app shows while it is opening.
 *
 * It used to show nothing. On a library of 196,174 photographs with a cold
 * cache the opening calls took most of half a minute, and an empty page is
 * indistinguishable from a broken one — so people reloaded, which made it
 * worse.
 *
 * Two things fixed that, and this checks the visible half: a cover that is
 * on screen immediately, says how far along it is, and — the part that
 * matters most — always gets out of the way. An overlay that outlives its
 * page is a worse bug than the empty page it replaced, because nothing
 * behind it can be read or clicked.
 *
 * The calls are answered from here, deliberately slowly, because the whole
 * point is what is on screen *during* a slow load. The real server is too
 * fast to see it now, which is the improvement.
 *
 *   node tests/first_load_ui.mjs   (wants the family app up; no sign-in,
 *                                   since every call is intercepted)
 */
import { launch, ok, done, HOME } from './harness.mjs';

const USER = {
  id: 1, username: 'dad', display_name: 'Dad', role: 'admin', anonymous: false,
  can: { manage_library: true, set_visibility: true, favorite: true,
         download: true, delete_media: true, rotate: true },
};

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** A page with every call answered, and the slow ones held back. */
async function opened(page, { slow = {}, auth = null } = {}) {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    const send = (body) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (slow[path]) await wait(slow[path]);
    if (path === '/api/auth/state') {
      return send(auth || { ready: true, signed_in: true, setup_required: false,
                            user: USER, profiles: [] });
    }
    if (path === '/api/status') {
      return send({ has_library: true, root: '/library', root_label: 'Library',
                    roots: ['/library'], user: USER, ai: { engine: 'off' },
                    scan: { status: 'idle', running: false, percent: 100 },
                    capabilities: {}, thumb_sizes: [256, 640], hostnames: {},
                    home_port: 443, admin_port: 3000, scheme: 'https' });
    }
    if (path === '/api/status/stats') {
      return send({ stats: { count: 196174, bytes: 3013475385344,
                             pictures: 161377, videos: 21936, audio: 12861,
                             live: 0, duplicate_groups: 4102, hidden: 0,
                             public: 0, favorites: 4 } });
    }
    return send({ items: [], segments: [], total: 0, entries: [], tags: [],
                  years: [], people: [], albums: [], occasions: [] });
  });
  page.goto(HOME, { waitUntil: 'commit' });
}

/** The overlay's state right now. */
const look = () => ({
  up: (() => {
    const boot = document.querySelector('#boot');
    return Boolean(boot) && !boot.hidden && !boot.classList.contains('done');
  })(),
  percent: document.querySelector('#boot-percent')?.textContent || '',
  step: document.querySelector('#boot-step')?.textContent || '',
  counts: document.querySelector('#count-all')?.textContent || '',
  gate: !document.querySelector('#gate')?.hidden,
});

/** Watch until the cover goes, or give up. Returns everything it saw. */
async function watch(page, { limit = 120 } = {}) {
  const seen = [];
  for (let i = 0; i < limit; i += 1) {
    const now = await page.evaluate(look).catch(() => null);
    if (now) {
      const last = seen[seen.length - 1];
      if (!last || JSON.stringify(last) !== JSON.stringify(now)) seen.push(now);
      if (!now.up) break;
    }
    await page.waitForTimeout(120);
  }
  return seen;
}

const b = await launch();
const ctx = await b.newContext({ ignoreHTTPSErrors: true,
                                 viewport: { width: 1400, height: 900 } });

/* ---------- a slow load, which is the case it exists for ---------- */
{
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await opened(page, { slow: { '/api/status': 900, '/api/segments': 1200,
                               '/api/status/stats': 2500 } });
  const seen = await watch(page);

  ok('the cover is up before anything has loaded',
    seen.length > 0 && seen[0].up, JSON.stringify(seen[0]));
  ok('it says a percentage', seen.some((s) => /^\d+%$/.test(s.percent)),
    seen.map((s) => s.percent).join(' '));
  ok('the percentage only goes forward',
    (() => {
      const numbers = seen.map((s) => parseInt(s.percent, 10)).filter(Number.isFinite);
      return numbers.every((n, i) => i === 0 || n >= numbers[i - 1]);
    })(), seen.map((s) => s.percent).join(' '));
  ok('it names what it is waiting for', seen.every((s) => s.step.length > 0));
  ok('the cover comes off', seen.length > 0 && !seen[seen.length - 1].up);

  // The point of the whole change: the sidebar's numbers are the slowest
  // call here, and the gallery must not have waited for them.
  ok('it did not wait for the counts',
    seen[seen.length - 1].counts === '',
    `counts were already ${seen[seen.length - 1].counts}`);
  await page.waitForTimeout(3000);
  ok('and the counts arrive afterwards',
    (await page.evaluate(look)).counts !== '');
  ok('no page errors', errors.length === 0, errors.join(' | '));
  await page.close();
}

/* ---------- a page that never reaches the gallery ---------- */
{
  const page = await ctx.newPage();
  await opened(page, {
    auth: { ready: true, signed_in: false, setup_required: false, profiles: [] },
  });
  const seen = await watch(page);
  ok('the sign-in screen is not left underneath the cover',
    seen.length > 0 && !seen[seen.length - 1].up,
    JSON.stringify(seen[seen.length - 1]));
  await page.close();
}

/* ---------- a server that never answers ---------- */
{
  const page = await ctx.newPage();
  // Every call hangs. Nothing will ever load; the cover must still go.
  await page.route('**/api/**', () => { /* swallowed on purpose */ });
  page.goto(HOME, { waitUntil: 'commit' });
  await page.waitForTimeout(2000);
  const stuck = await page.evaluate(look);
  ok('a hung server leaves the cover up while it is still hoping', stuck.up);

  // And then gives up. Thirty seconds of waiting is the point — a cover that
  // never lifts is the failure this guards against, so the test waits it out
  // rather than asserting that the source contains a number.
  await page.waitForTimeout(31000);
  ok('...and lifts it anyway, rather than trapping the page for ever',
    !(await page.evaluate(look)).up);
  await page.close();
}

await done(b);
