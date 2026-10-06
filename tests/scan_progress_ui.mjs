/**
 * The library scan saying where it is, in both apps.
 *
 * Two things are checked here, and the second was a bug found while adding the
 * first. The scan strip carries the folder the walk is in, so a count that has
 * not moved for a minute can be told apart from a drive that has stopped
 * answering. And the strip works *on the family port at all*: progress is
 * delivered over an admin-only event stream, which on port 5000 genuinely does
 * not exist, so the page used to reconnect to a 404 every four seconds for
 * ever and the bar never once appeared.
 *
 * The strip is now a list — indexing is one job in it, alongside anything else
 * holding the disk — so what is read here is the indexing row rather than the
 * single bar it used to be.
 *
 *   node tests/scan_progress_ui.mjs   (wants both apps up, admin
 *                                      dad / correcthorse1, and a library with
 *                                      enough in it to scan for a few seconds)
 * @fixture busy
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

const startScan = (page) => page.evaluate(async () => {
  await fetch('/api/scan', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full: true }),
  });
});

// The rows sit in the sidebar in the family app and in the page heading in
// the console, so both are found through the list itself rather than through
// whichever box happens to hold it.
const READ_INDEXING_ROW = () => {
  const rows = document.querySelector('#job-rows');
  const strip = rows?.parentElement;
  if (!rows || !strip || strip.hidden) return '';
  const row = rows.querySelector('.job-row[data-id="indexing"]');
  if (!row) return '';
  // Title and detail together: the phase it is in is in the title, and the
  // folder the walk has reached is in the detail.
  return [row.querySelector('.job-title').textContent,
          row.querySelector('.job-text').textContent].join(' — ');
};

const STRIP_IS_HIDDEN = () => {
  const strip = document.querySelector('#job-rows')?.parentElement;
  return !strip || strip.hidden;
};

/** Keep every distinct line the strip showed, until it goes away. */
async function watch(page, { start = true } = {}) {
  const lines = [];
  if (start) await startScan(page);
  for (let i = 0; i < 220; i += 1) {
    await page.waitForTimeout(120);
    const line = await page.evaluate(READ_INDEXING_ROW);
    if (line && line !== lines[lines.length - 1]) lines.push(line);
    if (lines.length && !line && lines.length > 2) break;   // finished
  }
  return lines;
}

/* ---------- the admin console ---------- */

const con = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const conErrs = [];
con.on('pageerror', (e) => conErrs.push(e.message));
await con.goto(ADMIN, { waitUntil: 'networkidle' });
await con.fill('#gate input[type="text"]', 'dad');
await con.fill('#gate input[type="password"]', 'correcthorse1');
await con.click('#gate .btn.primary');
await con.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await con.waitForTimeout(1500);

const conLines = await watch(con);
ok('the console shows a scan strip', conLines.length > 0, JSON.stringify(conLines));
ok('the console names the folder it is in',
  conLines.some((l) => l.includes('·')), JSON.stringify(conLines));
ok('and the console strip is the one in the page heading, so it is on every page',
  await con.evaluate(() => Boolean(document.querySelector('#scan-live #job-rows'))));
// `watch` stops collecting once it has what it needs, which is not the same
// moment the scan ends. Asserting the strip is gone right then made this test
// a bet on the library being small enough to have finished — it failed the
// moment the library was made big enough for the checks above to see anything.
await con.waitForFunction(STRIP_IS_HIDDEN, null, { timeout: 120000 })
  .catch(() => { /* assert on what it shows, not on the wait */ });
ok('the strip goes away when the scan finishes',
  await con.evaluate(STRIP_IS_HIDDEN));

// A real scan may finish between activity polls on a fast runner. Check
// changing folders with controlled endpoint responses, while the real scans
// above and below still check that both apps notice the running scanner.
let folder = '2019';
await con.route('**/api/status/activity', (route) => route.fulfill({
  json: {
    running: true, uses: ['disk'], scan: { running: true },
    jobs: [{ id: 'indexing', title: 'Indexing', detail: `1 of 3 · ${folder}`,
             page: 'library', uses: ['disk'], percent: 33, paused: false }],
  },
}));
await con.waitForFunction(() =>
  document.querySelector('#job-rows .job-text')?.textContent.includes('2019'));
folder = '2020';
await con.waitForFunction(() =>
  document.querySelector('#job-rows .job-text')?.textContent.includes('2020'));
ok('the folder changes as the scan moves',
  (await con.evaluate(READ_INDEXING_ROW)).includes('2020'));
await con.unroute('**/api/status/activity');
await con.waitForFunction(STRIP_IS_HIDDEN);

/* ---------- the family app, where the stream does not exist ---------- */

const fam = await b.newPage({ viewport: { width: 1440, height: 900 } });
const famErrs = [];
fam.on('pageerror', (e) => famErrs.push(e.message));
await fam.goto(HOME, { waitUntil: 'networkidle' });
await fam.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await fam.reload({ waitUntil: 'networkidle' });
await fam.waitForTimeout(2000);

// Started from the console, which is the only place a scan can be started
// from — so this is also the real question: does the family app notice a scan
// it did not begin?
await startScan(con);
const famLines = await watch(fam, { start: false });
ok('the family app notices a scan started from the console',
  famLines.length > 0, JSON.stringify(famLines));
ok('the family app names the folder too',
  famLines.some((l) => l.includes('·')), JSON.stringify(famLines));
await fam.waitForFunction(STRIP_IS_HIDDEN, null, { timeout: 120000 })
  .catch(() => { /* as above */ });
ok('and its strip goes away when the scan finishes',
  await fam.evaluate(STRIP_IS_HIDDEN));

ok('no page errors in the console', conErrs.length === 0, conErrs.join(' | '));
ok('no page errors in the family app', famErrs.length === 0, famErrs.join(' | '));
await done(b);
