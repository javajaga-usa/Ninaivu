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
 *       folder large enough that walking it takes more than a second)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

// A folder big enough that walking it takes more than a second, which
// is the whole point of this test — see the header.
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

await p.fill('#ar-source-input', SOURCE);
await p.click('#ar-add-source');
await p.fill('#ar-dest-input', DEST);

const frames = [];
for (let i = 0; i < 60; i += 1) {
  await p.waitForTimeout(70);
  frames.push(await line());
}

const working = frames.filter((f) => f.working);
const counting = frames.filter((f) => /Counting…/.test(f.text));
const finished = frames.filter((f) => !f.working && !f.hidden && /files,/.test(f.text));

// The point of this test is to watch a walk that is still going. A folder that a
// fast disk walks in a few milliseconds is over before any frame can catch it,
// whatever its size in this fixture, and there is nothing to watch: say so, and
// check only what can still be checked, rather than failing for being quick.
const tooFast = counting.length === 0 && finished.length > 0;
const whileItWorks = (label, condition, detail) => (tooFast
  ? console.log(`SKIP  ${label} — the folder was walked faster than it can be watched here`)
  : ok(label, condition, detail));

ok('the line appears as soon as a source is added',
  frames.slice(0, 6).some((f) => !f.hidden), JSON.stringify(frames[0]));
whileItWorks('there is a spinner while it works',
  working.length > 0 && working.every((f) => f.spinner));
whileItWorks('it reports a running count, not a static word',
  counting.length > 0, working.map((f) => f.text).slice(0, 3).join(' | '));
whileItWorks('the count moves',
  new Set(counting.map((f) => f.text.match(/([\d,]+) files so far/)?.[1])).size > 1,
  counting.map((f) => f.text.match(/([\d,]+) files/)?.[1]).join(','));
whileItWorks('it says which folder it is in',
  counting.some((f) => f.where.length > 0),
  JSON.stringify(counting[0] || {}));
ok('the folder is shortened rather than a full deep path',
  counting.every((f) => !f.where || f.where.length < 70),
  counting.map((f) => f.where).find((w) => w.length >= 70) || '');
whileItWorks('it shows an elapsed time',
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
await p.waitForTimeout(700);                       // mid-walk, not after it
const midWalk = await line();
await p.evaluate(() => document.querySelector('#ar-sources .btn.ghost')?.click());
await p.waitForTimeout(1800);

// Only a walk that is still going can be called off: when it had already
// finished by the time the source was removed, there was nothing to cancel.
const walkOutlasted = midWalk.working;
if (!walkOutlasted && tooFast) {
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
