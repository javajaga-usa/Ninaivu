/**
 * Browser check for the Archive tab inside the admin console.
 *
 * Drives a real consolidation end to end: add two source folders, toggle a
 * media type, watch validation and the capacity estimate, run a dry run, then
 * a real run, then accept the offer to add the finished archive to the
 * library. Asserts against the page and against the files actually on disk.
 *
 *   node tests/archive_ui.mjs [adminPort] [sourceA] [sourceB] [destination]
 *
 * Wants a *fresh* instance: the tool deliberately remembers the last job and
 * the library remembers an adopted archive, so a second run against the same
 * state directory starts from a different place than the one asserted here.
 * See tests/archive_ui.sh, which builds the fixture and the instance first.
 */
import { launch, ok, done, failed, HOME, ADMIN } from './harness.mjs';

const PORT = process.argv[2] || '';
const SRC_A = process.env.NINAIVU_SRC_A || process.argv[3];
const SRC_B = process.env.NINAIVU_SRC_B || process.argv[4];
const DEST = process.env.NINAIVU_DEST || process.argv[5];
// The runner picks a free port, so the harness's ADMIN is the truth;
// an explicit port still wins for a hand-driven run.
const BASE = PORT ? `http://localhost:${PORT}` : ADMIN;

const SHOTS = process.env.NINAIVU_SHOTS || '.';
const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1340, height: 1000 } });

const errors = [];
page.on('pageerror', (e) => errors.push(`PAGEERROR ${e.message}`));
page.on('console', (m) => { if (m.type() === 'error') errors.push(`CONSOLE ${m.text()}`); });


const status = () => page.evaluate(async () =>
  (await (await fetch('/api/archive/status')).json()));

async function waitIdle(timeout = 90000) {
  const until = Date.now() + timeout;
  // Let the job actually start before deciding it has finished.
  await page.waitForTimeout(1200);
  while (Date.now() < until) {
    const s = await status();
    if (!s.is_scanning) return s;
    await page.waitForTimeout(400);
  }
  throw new Error('the run never finished');
}

/* -- sign in and open the tab -------------------------------------------- */

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.fill('#gate input[type="text"], #gate input[name="username"]', 'dad');
await page.fill('#gate input[type="password"]', 'correcthorse1');
await page.click('#gate button[type="submit"], #gate .btn.primary');
await page.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });

ok('the console has an Archive tab',
   await page.$('#tabs button[data-tab="archive"]') !== null);

await page.click('#tabs button[data-tab="archive"]');
await page.waitForSelector('[data-panel="archive"].active', { timeout: 5000 });
await page.waitForTimeout(800);

const stampTitle = await page.evaluate(() =>
  document.querySelector('#archive-stamp')?.closest('.block')?.querySelector('h2')?.title || '');
ok('the engine version is in the heading tooltip, not on the page',
   /Archive engine 2\.9/.test(stampTitle) && !(await page.textContent('#archive-stamp')),
   stampTitle);

/* -- build the job ------------------------------------------------------- */

await page.fill('#ar-source-input', SRC_A);
await page.click('#ar-add-source');
await page.fill('#ar-source-input', SRC_B);
await page.click('#ar-add-source');
await page.waitForTimeout(300);

ok('both sources are listed',
   (await page.$$('.ar-source')).length === 2);
ok('the empty note is gone',
   await page.evaluate(() => document.querySelector('#ar-sources-empty').hidden));

// Adding the same folder twice is refused.
await page.fill('#ar-source-input', SRC_A);
await page.click('#ar-add-source');
await page.waitForTimeout(250);
ok('a duplicate source is refused',
   (await page.$$('.ar-source')).length === 2);

// The tool remembers the last job, so normalise with Apply to all first —
// which is itself the thing being checked here.
await page.click('#ar-apply-all');
await page.waitForTimeout(300);
ok('Apply to all sets every folder to the default kinds',
   await page.evaluate(() =>
     [...document.querySelectorAll('.ar-source')].every(
       (r) => r.querySelectorAll('.ar-kind.on').length === 3)));

// Turn video off on the first folder only.
await page.evaluate(() => {
  document.querySelector('.ar-source [data-kind="video"]').click();
});
await page.waitForTimeout(250);
ok('a kind can be switched off per folder',
   await page.evaluate(() => {
     const rows = [...document.querySelectorAll('.ar-source')];
     return rows[0].querySelectorAll('.ar-kind.on').length === 2
         && rows[1].querySelectorAll('.ar-kind.on').length === 3;
   }));

await page.fill('#ar-dest-input', DEST);
await page.waitForTimeout(1400);   // debounced validate + capacity

const capacity = await page.evaluate(() => ({
  hidden: document.querySelector('#ar-capacity').hidden,
  text: document.querySelector('#ar-capacity').innerText,
}));
ok('the capacity estimate appears before committing',
   !capacity.hidden && /files/.test(capacity.text), capacity.text);
ok('no refusal for a legitimate job',
   await page.evaluate(() => document.querySelector('#ar-problems').hidden));

await page.screenshot({ path: `${SHOTS}/ar-1-form.png` });

/* -- dry run ------------------------------------------------------------- */

await page.click('#ar-dry');
await page.waitForTimeout(600);
ok('the dry-run banner says nothing is written',
   await page.evaluate(() => !document.querySelector('#ar-dry-banner').hidden));

let s = await waitIdle();
ok('the dry run planned files without writing any',
   s.planned > 0, `planned=${s.planned}`);

const wroteDuringDryRun = await page.evaluate(async (dest) =>
  (await (await fetch(`/api/library/browse?path=${encodeURIComponent(dest)}`)).json()),
DEST).catch(() => null);
ok('a dry run creates nothing to browse',
   !wroteDuringDryRun || (wroteDuringDryRun.dirs || []).length === 0,
   JSON.stringify(wroteDuringDryRun?.dirs || []));

await page.screenshot({ path: `${SHOTS}/ar-2-dryrun.png` });

/* -- the real run -------------------------------------------------------- */

await page.click('#ar-start');

// The library indexer must stand down while the disk is busy. Poll, because
// the status stream ticks about once a second and a small job is quick.
let yielded = false;
for (let i = 0; i < 40 && !yielded; i += 1) {
  yielded = await page.evaluate(() =>
    (document.querySelector('#ar-indexer').hidden === false
     && document.querySelector('#ar-indexer').innerText) || false);
  if (!yielded) await page.waitForTimeout(150);
}
ok('the library indexer is shown as paused', !!yielded, String(yielded));

s = await waitIdle();
ok('files were verified', s.verified > 0, `verified=${s.verified}`);
ok('the byte-identical copy was caught as a duplicate',
   s.duplicates >= 1, `duplicates=${s.duplicates}`);
ok('no errors', (s.errors || 0) === 0, `errors=${s.errors}`);

await page.waitForTimeout(1200);
const cards = await page.evaluate(() => ({
  verified: document.querySelector('#ar-verified').innerText,
  duplicates: document.querySelector('#ar-duplicates').innerText,
  errors: document.querySelector('#ar-errors').innerText,
  label: document.querySelector('#ar-label-verified').textContent,
}));
ok('the cards show the run', Number(cards.verified.replace(/,/g, '')) === s.verified,
   JSON.stringify(cards));
ok('the label went back to Verified after the dry run',
   cards.label === 'Verified', JSON.stringify(cards.label));

const rows = await page.evaluate(() =>
  document.querySelectorAll('#ar-tbody tr').length);
ok('the files table filled in', rows > 1, `rows=${rows}`);

const yearsShown = await page.evaluate(() => ({
  hidden: document.querySelector('#ar-years').hidden,
  columns: document.querySelectorAll('.ar-year').length,
}));
ok('the capture-year chart is drawn',
   !yearsShown.hidden && yearsShown.columns >= 2, JSON.stringify(yearsShown));

// Filter to duplicates only.
await page.click('#ar-filters button[data-status="duplicate"]');
await page.waitForTimeout(600);
ok('the duplicate filter works',
   await page.evaluate(() =>
     [...document.querySelectorAll('#ar-tbody .ar-state')]
       .every((s) => s.textContent === 'duplicate')));
await page.click('#ar-filters button[data-status="all"]');
await page.waitForTimeout(400);

// The indexer must be back up.
ok('the library indexer resumed once the run ended',
   (await status()).indexer.deferred === null);

await page.screenshot({ path: `${SHOTS}/ar-3-done.png` });

/* -- the hand-off to the library ----------------------------------------- */

const offer = await page.evaluate(() => ({
  hidden: document.querySelector('#ar-handoff').hidden,
  title: document.querySelector('#ar-handoff-title').innerText,
  sub: document.querySelector('#ar-handoff-sub').innerText,
}));
ok('the finished archive is offered to the library',
   !offer.hidden && /Add this archive/.test(offer.title), JSON.stringify(offer));
ok('the offer names the folder and the count',
   offer.sub.includes(DEST) && /verified files/.test(offer.sub), offer.sub);

const before = await page.evaluate(async () =>
  (await (await fetch('/api/admin/overview')).json()).library.roots);
ok('nothing was added to the library without being asked',
   !before.includes(DEST), JSON.stringify(before));

await page.click('#ar-adopt');
await page.waitForTimeout(2500);

const after = await page.evaluate(async () =>
  (await (await fetch('/api/admin/overview')).json()).library.roots);
ok('one click adds it to the library', after.includes(DEST), JSON.stringify(after));

const settled = await page.evaluate(() => ({
  title: document.querySelector('#ar-handoff-title').innerText,
  buttonHidden: document.querySelector('#ar-adopt').hidden,
}));
ok('the offer becomes a confirmation',
   /is in your library/.test(settled.title) && settled.buttonHidden === true,
   JSON.stringify(settled));

await page.screenshot({ path: `${SHOTS}/ar-4-adopted.png` });

/* -- it shows up in the rest of the console ------------------------------ */

await page.click('#tabs button[data-tab="activity"]');
await page.waitForTimeout(900);
const log = await page.evaluate(() => document.querySelector('#activity')?.innerText || '');
ok('the activity log records the run and the adoption',
   /started a consolidation/.test(log) && /added an archive to the library/.test(log),
   log.slice(0, 300));

ok('no console errors', errors.length === 0);
if (errors.length) console.log(errors.join('\n'));

await browser.close();
console.log(failed() ? `\n${failed()} check(s) failed()` : '\nall checks passed');
console.log('screenshots: /tmp/ar-1-form.png /tmp/ar-2-dryrun.png /tmp/ar-3-done.png /tmp/ar-4-adopted.png');
