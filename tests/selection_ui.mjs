/**
 * Browser check for the media-type selection and the estimate that reports it.
 *
 * The fault this pins down: the three checkboxes above the folder list read
 * "scan for photos / video / audio", but had no change handler at all. They set
 * the default for folders added *later* only, so unticking Video left every
 * folder already on the list still scanning for video. The estimate then
 * faithfully reported a job nobody had knowingly configured — and, worse, so
 * would Start.
 *
 *   node tests/selection_ui.mjs [adminPort] [source] [destination]
 */
import { launch, ok, done, failed, HOME, ADMIN } from './harness.mjs';

const PORT = process.argv[2] || '';
const SRC = process.env.NINAIVU_SRC || process.argv[3];
const DEST = process.env.NINAIVU_DEST || process.argv[4];
// The runner picks a free port, so the harness's ADMIN is the truth;
// an explicit port still wins for a hand-driven run.
const BASE = PORT ? `http://localhost:${PORT}` : ADMIN;

const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1340, height: 1000 } });

const errors = [];
page.on('pageerror', (e) => errors.push(`PAGEERROR ${e.message}`));
page.on('console', (m) => { if (m.type() === 'error') errors.push(`CONSOLE ${m.text()}`); });

// Record what the browser actually asks for, which is the real contract.
const sent = [];
page.on('request', (r) => {
  if (r.url().endsWith('/api/archive/capacity') && r.method() === 'POST') {
    try { sent.push(JSON.parse(r.postData() || '{}')); } catch { /* ignore */ }
  }
});


const estimateText = () => page.textContent('#ar-capacity');
const chipsOn = () => page.evaluate(() =>
  [...document.querySelectorAll('.ar-source .ar-kind.on')].map((c) => c.dataset.kind));

async function settle() { await page.waitForTimeout(900); }

/* -- sign in and open the tab -------------------------------------------- */

// The runner shares one Ninaivu across the whole run, and the Archive tool
// remembers the last job on purpose — so anything a previous test left listed
// is still here. Start from nothing, which is what this test means by "the
// folder".
async function clearSources(target) {
  for (let i = 0; i < 20; i += 1) {
    const removed = await target.evaluate(() => {
      const row = document.querySelector('.ar-source');
      const button = row && [...row.querySelectorAll('button')]
        .find((b) => b.textContent.trim() === 'Remove');
      if (!button) return false;
      button.click();
      return true;
    });
    if (!removed) break;
    await target.waitForTimeout(120);
  }
}

await page.goto(BASE, { waitUntil: 'networkidle' });
await page.fill('#gate input[type="text"], #gate input[name="username"]', 'dad');
await page.fill('#gate input[type="password"]', 'correcthorse1');
await page.click('#gate button[type="submit"], #gate .btn.primary');
await page.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });
await page.click('#tabs button[data-tab="archive"]');
await page.waitForSelector('[data-panel="archive"].active', { timeout: 5000 });
await page.waitForTimeout(600);

/* -- a job with everything selected -------------------------------------- */

await page.fill('#ar-dest-input', DEST);
await clearSources(page);
await page.fill('#ar-source-input', SRC);
await page.click('#ar-add-source');
await settle();

ok('the folder starts scanning for all three kinds',
   (await chipsOn()).length === 3, JSON.stringify(await chipsOn()));

const everything = await estimateText();
console.log(`      estimate: ${everything.trim()}`);

ok('the estimate breaks the total down by kind',
   /photos/.test(everything) && /video/.test(everything) && /audio/.test(everything),
   everything);
ok('the estimate counts all eight files',
   /\b8\b/.test(everything), everything);

/* -- the actual bug: untick Video ---------------------------------------- */

await page.uncheck('#ar-type-video');
await settle();

ok('unticking Video turns it off on the folder already listed',
   !(await chipsOn()).includes('video'), JSON.stringify(await chipsOn()));

const last = sent.at(-1);
ok('the browser asks for a photos-and-audio job',
   JSON.stringify(last?.source_dirs?.[0]?.types?.slice().sort()) === '["audio","image"]',
   JSON.stringify(last?.source_dirs?.[0]?.types));

const narrowed = await estimateText();
console.log(`      estimate: ${narrowed.trim()}`);
ok('the estimate no longer mentions video', !/video/.test(narrowed), narrowed);
ok('the estimate drops to five files', /\b5\b/.test(narrowed), narrowed);

/* -- and audio, leaving photos only -------------------------------------- */

await page.uncheck('#ar-type-audio');
await settle();

const photos = await estimateText();
console.log(`      estimate: ${photos.trim()}`);
ok('photos only leaves four files',
   /\b4\b/.test(photos) && !/audio/.test(photos), photos);
ok('with one kind left there is no breakdown to show',
   !/·/.test(photos), photos);

/* -- ticking it back on ---------------------------------------------------*/

await page.check('#ar-type-video');
await settle();
ok('ticking Video back on restores it on the folder',
   (await chipsOn()).includes('video'), JSON.stringify(await chipsOn()));

/* -- a per-folder change makes the master box indeterminate --------------- */

await page.evaluate(() =>
  document.querySelector('.ar-source [data-kind="video"]').click());
await settle();
ok('the master checkbox follows a per-folder change',
   await page.evaluate(() => !document.querySelector('#ar-type-video').checked));

/* -- nothing selected at all --------------------------------------------- */

for (const kind of ['image', 'video', 'audio']) {
  await page.evaluate((k) => {
    const chip = document.querySelector(`.ar-source [data-kind="${k}"]`);
    if (chip?.classList.contains('on')) chip.click();
  }, kind);
}
await settle();
const notes = await page.textContent('#ar-notes, .ar-note, [data-panel="archive"]');
ok('a folder set to scan for nothing says so',
   /no kinds of file selected/.test(notes),
   notes.slice(0, 200));

console.log(`\n  ${errors.length} page errors`);
for (const e of errors.slice(0, 5)) console.log(`    ${e}`);
if (errors.length) { failed() += 1; process.exitCode = 1; }
console.log(failed() ? `\n  ${failed()} FAILED\n` : '\n  all checks passed\n');

await browser.close();
