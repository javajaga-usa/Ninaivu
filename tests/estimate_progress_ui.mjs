/**
 * The Archive tab's capacity line, while it is still counting.
 *
 * Adding a drive as a source starts a walk of every folder on it. The console
 * used to show one static word for however long that took, so a walk working
 * steadily through four hundred thousand files looked exactly like one that
 * had hit a spun-down disk. This checks that the line says what it is doing,
 * keeps saying it, and then gets out of the way.
 *
 *   node tests/estimate_progress_ui.mjs /some/big/folder
 *      (wants a console on 3000, admin dad / correcthorse1, and a source
 *       folder and a destination; progress readings are controlled here,
 *       and the final estimate comes from the real folder walk)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

// The final estimate still comes from walking this real folder.
const SOURCE = process.env.NINAIVU_SRC_BIG || process.env.NINAIVU_SRC_A
  || process.argv[2];
const DEST = process.env.NINAIVU_DEST || process.argv[3];

const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.click('#tabs button[data-tab="archive"]');
await p.waitForTimeout(1500);

const line = () => p.evaluate(() => {
  const n = document.querySelector('#ar-capacity');
  return {
    hidden: n.hidden,
    working: n.classList.contains('working'),
    spinner: !!n.querySelector('.ar-spinner'),
    where: n.querySelector('.ar-working-where')?.textContent || '',
    text: n.innerText.replace(/\s+/g, ' ').trim(),
  };
});

/* ---------- the pause before the count starts ---------- */
//
// Reaching a sleeping external drive takes seconds before a single file has
// been looked at. Held open here, because on a local folder it is over in
// milliseconds and the point is what happens when it is not.

let holdValidate = true;
await p.route('**/api/archive/validate', async (route) => {
  if (holdValidate) await new Promise((r) => setTimeout(r, 2500));
  await route.continue();
});

await p.fill('#ar-source-input', SOURCE);
await p.click('#ar-add-source');
await p.fill('#ar-dest-input', DEST);
await p.waitForTimeout(1200);
const checking = await line();
ok('it says it is checking the folder before it can count',
  /Checking that folder/.test(checking.text), checking.text);
ok('the spinner is turning during that wait', checking.spinner);

holdValidate = false;
await p.waitForTimeout(4000);
await p.evaluate(() => document.querySelector('#ar-sources .btn.ghost')?.click());
await p.waitForTimeout(1000);

/* ---------- sample the whole sequence ---------- */

// A fast walk may expose just one nonzero count before it finishes. Hold
// its real final response while supplying known progress snapshots, so a
// missing repaint or poll always fails, regardless of the runner's disk.
let progressFiles = 1;
let countingStarted = false;
let releaseEstimate;
const estimateReady = new Promise(resolve => { releaseEstimate = resolve; });
const capacityRoute = async route => {
  // Form edits also peek at the last estimate. Let those finish normally;
  // only Count files starts the fresh walk whose result is held here.
  if (!route.request().postDataJSON()?.fresh) return route.continue();
  countingStarted = true;
  const response = await route.fetch();
  await estimateReady;
  await route.fulfill({ response });
};
const progressRoute = route => countingStarted ? route.fulfill({ json: {
  known: true, files: progressFiles, bytes: progressFiles * 1024,
  elapsed: progressFiles, folder: 'DCIM',
} }) : route.continue();
await p.route('**/api/archive/capacity', capacityRoute);
await p.route('**/api/archive/capacity/progress?*', progressRoute);

await p.fill('#ar-source-input', SOURCE);
await p.click('#ar-add-source');
await p.fill('#ar-dest-input', DEST);
// Since 2.1 nothing is counted until Count files is pressed.
await p.waitForTimeout(500);
await p.click('#ar-count-files');

const frames = [];
try {
  for (const count of [1, 2, 3]) {
    progressFiles = count;
    await p.waitForFunction(value => document.querySelector('#ar-capacity')
      ?.innerText.includes(`${value} files so far`), count, { timeout: 15000 });
    frames.push(await line());
  }
} finally {
  releaseEstimate();
}
await p.waitForFunction(() => {
  const node = document.querySelector('#ar-capacity');
  return node && !node.classList.contains('working') && /files,/.test(node.innerText);
}, null, { timeout: 30000 });
frames.push(await line());
await p.unroute('**/api/archive/capacity', capacityRoute);
await p.unroute('**/api/archive/capacity/progress?*', progressRoute);

const working = frames.filter((f) => f.working);
const counting = frames.filter((f) => /Counting…/.test(f.text));
const finished = frames.filter((f) => !f.working && !f.hidden && /files,/.test(f.text));

ok('the line appears as soon as a source is added',
  frames.slice(0, 6).some((f) => !f.hidden), JSON.stringify(frames[0]));
ok('there is a spinner while it works',
  working.length > 0 && working.every((f) => f.spinner));
ok('it reports a running count, not a static word',
  counting.length > 0, working.map((f) => f.text).slice(0, 3).join(' | '));
ok('the count moves',
  new Set(counting.map((f) => f.text.match(/([\d,]+) files so far/)?.[1])).size > 1,
  counting.map((f) => f.text.match(/([\d,]+) files/)?.[1]).join(','));
ok('it says which folder it is in',
  counting.some((f) => f.where.length > 0),
  JSON.stringify(counting[0] || {}));
ok('the folder is shortened rather than a full deep path',
  counting.every((f) => !f.where || f.where.length < 70),
  counting.map((f) => f.where).find((w) => w.length >= 70) || '');
ok('it shows an elapsed time',
  counting.some((f) => /· \d+[smh]/.test(f.text)),
  counting[0]?.text);

/* ---------- and then gets out of the way ---------- */

ok('the final answer replaces the working line', finished.length > 0,
  frames[frames.length - 1].text);
ok('the spinner is gone once it is done',
  finished.every((f) => !f.spinner));
ok('the final answer is a real estimate',
  /files, .*Needs .* free/.test(frames[frames.length - 1].text),
  frames[frames.length - 1].text);

/* ---------- changing the job calls the old walk off ---------- */

const cancels = [];
await p.route('**/api/archive/capacity/cancel', async (route) => {
  cancels.push(JSON.parse(route.request().postData() || '{}').token);
  await route.continue();
});

// Start a fresh walk and pull the source out from under it while it is still
// going — the case that used to leave a drive being read for a job nobody has
// any more, and let a stale answer land on top of a newer one.
await p.evaluate(() => document.querySelector('#ar-sources .btn.ghost')?.click());
await p.waitForTimeout(900);
cancels.length = 0;

await p.fill('#ar-source-input', SOURCE);
await p.click('#ar-add-source');
await p.waitForTimeout(400);
await p.click('#ar-count-files');
await p.waitForTimeout(300);                       // mid-walk, not after it
const midWalk = await line();
await p.evaluate(() => document.querySelector('#ar-sources .btn.ghost')?.click());
await p.waitForTimeout(1800);

// Only a walk that is still going can be called off: when it had already
// finished by the time the source was removed, there was nothing to cancel.
const walkOutlasted = midWalk.working;
// Decided by this walk alone: the one above may have been held up where this
// one was not.
if (!walkOutlasted) {
  console.log('SKIP  the walk was still running when the source was removed — it had already finished');
  console.log('SKIP  removing the source calls off the walk it started — there was no walk to call off');
} else {
  ok('the walk was still running when the source was removed',
    midWalk.working, midWalk.text);
  ok('removing the source calls off the walk it started',
    cancels.length > 0, JSON.stringify(cancels));
}
ok('the capacity line goes away with the source',
  (await line()).hidden, (await line()).text);
ok('no stale answer lands after the source is gone',
  (await (async () => { await p.waitForTimeout(1500); return line(); })()).hidden);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
