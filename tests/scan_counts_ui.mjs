/**
 * The activity strip drawing what the server told it, in both apps.
 *
 * This used to check that every scan phase counted — each pass after indexing
 * measures itself in items, and both strips only built a moving line for
 * tagging, so "Describing 17,685 videos" sat unchanged above a bar that moved
 * once every 177 clips, which is indistinguishable from a scan that has hung.
 *
 * The words are the server's now: one list, composed once, for both faces
 * (ninaivu/server/activity.py, and tests/test_activity.py checks the phrasing).
 * What is left here is what the browser still decides, and each of these has
 * been wrong at some point:
 *
 *   - how long is left, in words rather than in seconds;
 *   - the bar drawn from the percentage the server gave;
 *   - a job that has no percentage pacing rather than sitting at 0%, which
 *     reads as work that has not started;
 *   - a paused job showing no number at all, rather than one that never moves.
 *
 * The rows come from an intercepted /api/status/activity rather than real
 * work: reaching the video pass wants a library of thousands of clips, and
 * what is checked is what the strip does with an answer, not that the scanner
 * can produce one.
 *
 *   node tests/scan_counts_ui.mjs   (wants both apps up and admin
 *                                    dad / correcthorse1; the library can be
 *                                    anything, since nothing is started)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

// The family app installs a service worker, and a request that goes through
// one is invisible to page.route. Without this the console passes every check
// below and the family app sees nothing at all.
const ctx = await b.newContext({ serviceWorkers: 'block' });

const indexing = {
  id: 'indexing', title: 'Describing videos', detail: '184 of 17,685',
  percent: 1, eta: null, paused: false, page: 'library', uses: ['cpu', 'disk'],
};

let payload = { jobs: [indexing], running: true, uses: ['cpu', 'disk'] };

async function prepare(page) {
  // Server-sent events are taken away so the console falls back to the same
  // polling path the family app uses. The strip has always polled, but the
  // scan stream underneath it would otherwise keep redrawing the bar on the
  // Settings page from real counters while this serves invented ones.
  await page.addInitScript(() => { delete window.EventSource; });
  await page.route('**/api/status/activity*', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    headers: { 'Cache-Control': 'no-store' },
    body: JSON.stringify({ ...payload, scan: { status: 'idle', running: false, percent: 100 } }),
  }));
}

/** Serve `jobs`, wait for the strip to catch up, and read the first row back.
 *
 * The wait is for the strip to *change*, not for it to say any particular
 * thing. Waiting for the new payload's detail looks tighter and is useless:
 * these cases differ only in how much time is left, so the detail is identical
 * across them and the condition was already true of the row still on screen —
 * the wait returned at once and the assertions read the previous payload. With
 * no jobs at all it was worse, since every string starts with "".
 */
async function strip(page, jobs) {
  const before = await page.evaluate(
    () => document.querySelector('#job-rows')?.textContent ?? '');
  payload = { jobs, running: jobs.length > 0, uses: [] };
  await page.waitForFunction(
    (was) => (document.querySelector('#job-rows')?.textContent ?? '') !== was,
    before, { timeout: 15000 },
  ).catch(() => { /* read it anyway, so the failure says what it showed */ });
  return page.evaluate(() => {
    const row = document.querySelector('#job-rows .job-row');
    if (!row) return null;
    return {
      title: row.querySelector('.job-title').textContent,
      text: row.querySelector('.job-text').textContent,
      percent: row.querySelector('.job-percent').textContent,
      width: row.querySelector('.job-bar i').style.width,
      unknown: row.classList.contains('unknown'),
      paused: row.classList.contains('paused'),
      rows: document.querySelectorAll('#job-rows .job-row').length,
    };
  });
}

async function check(page, label) {
  let r = await strip(page, [indexing]);
  ok(`${label}: the strip shows what the server called it`,
    r.title === 'Describing videos' && r.text === '184 of 17,685',
    `${r.title} / ${r.text}`);
  ok(`${label}: and the bar is drawn from the percentage it was given`,
    r.width === '1%' && r.percent === '1%', `${r.width} @ ${r.percent}`);

  r = await strip(page, [{ ...indexing, detail: '9,000 of 17,685', percent: 51 }]);
  ok(`${label}: the count moves with the pass`,
    r.text === '9,000 of 17,685' && r.width === '51%',
    `${r.text} @ ${r.width}`);

  // How long is left — the question a count still does not answer. Rounded
  // hard: a pass measured over thousands of items is not good to the minute,
  // and what is being decided is whether to leave the machine on tonight.
  r = await strip(page, [{ ...indexing, eta: 50000 }]);
  ok(`${label}: a long pass says how many hours are left`,
    r.text === '184 of 17,685 · about 14 hours left', r.text);

  r = await strip(page, [{ ...indexing, eta: 1500 }]);
  ok(`${label}: a short pass says minutes instead`,
    r.text === '184 of 17,685 · about 25 minutes left', r.text);

  r = await strip(page, [{ ...indexing, eta: 40 }]);
  ok(`${label}: and the end of a pass is not a number at all`,
    r.text === '184 of 17,685 · nearly done', r.text);

  // Too early to tell is said by saying nothing, not by guessing.
  r = await strip(page, [{ ...indexing, eta: null }]);
  ok(`${label}: a pass with nothing to say about the time says nothing`,
    r.text === '184 of 17,685', r.text);

  // A walk has no total yet — the count it is heading for is the thing it is
  // still finding — and an empty bar would read as work that has not begun.
  r = await strip(page, [{
    ...indexing, title: 'Looking for files', detail: '900', percent: null,
  }]);
  ok(`${label}: a job with no percentage paces instead of sitting at zero`,
    r.unknown && r.percent === '', `${r.unknown} @ ${r.percent}`);

  // Paused is not stopped and not broken: another job has the disk, and the
  // row says which rather than showing a number that never moves.
  r = await strip(page, [{
    ...indexing, title: 'Indexing', percent: null, paused: true,
    detail: 'Waiting for checking the library’s storage to finish',
  }]);
  ok(`${label}: a paused job says what it is waiting for, with no number`,
    r.paused && r.percent === '' && r.text.startsWith('Waiting for'),
    `${r.paused} @ ${r.percent} / ${r.text}`);

  // The whole point of the change: more than one thing at a time.
  r = await strip(page, [
    { ...indexing, id: 'archive', title: 'Archive — copy', detail: '10 of 90',
      percent: 11, uses: ['disk'] },
    { ...indexing, id: 'cloud', title: 'Backing up to the cloud',
      detail: 'IMG_4410.HEIC · 2.1 MB/s', percent: 40, uses: ['network'] },
    indexing,
  ]);
  ok(`${label}: every running job gets its own row`, r.rows === 3, String(r.rows));
  ok(`${label}: in the order the server put them in`,
    r.title === 'Archive — copy', r.title);

  r = await strip(page, []);
  ok(`${label}: and an idle machine shows nothing at all`, r === null,
    JSON.stringify(r));
}

/* ---------- the family app ---------- */

const fam = await ctx.newPage();
const famErrs = [];
fam.on('pageerror', (e) => famErrs.push(e.message));
await prepare(fam);
await fam.goto(HOME, { waitUntil: 'networkidle' });
await fam.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await fam.reload({ waitUntil: 'networkidle' });
await check(fam, 'family app');
ok('no page errors in the family app', famErrs.length === 0, famErrs.join(' | '));

/* ---------- the console ---------- */

const con = await ctx.newPage({ viewport: { width: 1500, height: 1000 } });
const conErrs = [];
con.on('pageerror', (e) => conErrs.push(e.message));
await prepare(con);
await con.goto(ADMIN, { waitUntil: 'networkidle' });
await con.fill('#gate input[type="text"]', 'dad');
await con.fill('#gate input[type="password"]', 'correcthorse1');
await con.click('#gate .btn.primary');
await con.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await check(con, 'console');
ok('no page errors in the console', conErrs.length === 0, conErrs.join(' | '));

await done(b);
