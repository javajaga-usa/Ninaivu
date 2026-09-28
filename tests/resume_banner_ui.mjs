/**
 * The console saying a run is picking up where the last one stopped.
 *
 * "Start is also Resume" has always been true and has never been visible. This
 * checks the banner that makes it visible, and — just as important — that it
 * does not linger: the engine remembers the last job's resume count long after
 * that job ended, so a banner keyed only on that number would go on announcing
 * a resume over a run that finished yesterday.
 *
 *   node tests/resume_banner_ui.mjs   (wants a console on 3000,
 *                                      admin dad / correcthorse1)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));
await p.addInitScript(() => { delete window.EventSource; });

let status = null;
await p.route('**/api/archive/status', async (route) => {
  if (!status) return route.continue();
  await route.fulfill({ contentType: 'application/json', body: JSON.stringify(status) });
});

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.click('#tabs button[data-tab="archive"]');
await p.waitForTimeout(1500);

const feed = async (data) => { status = data; await p.waitForTimeout(2600); };
const banner = () => p.evaluate(() => ({
  hidden: document.querySelector('#ar-resume-banner').hidden,
  text: document.querySelector('#ar-resume-banner').innerText.replace(/\s+/g, ' ').trim(),
}));

const BASE = {
  is_scanning: true, is_paused: false, job_mode: 'copy', phase: 'copying',
  processed: 340122, total_files: 512890, verified: 1000, duplicates: 0,
  skipped: 0, errors: 0, pending: 0, planned: 0, plan_duplicates: 0,
  plan_skipped: 0, total_scanned: 340122, bytes_copied: 0, eta_seconds: null,
  job_message: '', recent: [], years: [],
};

/* ---------- a first run says nothing ---------- */

await feed({ ...BASE, resumed_from: 0, is_resume: false });
ok('a first run shows no resume banner', (await banner()).hidden, (await banner()).text);

/* ---------- a resumed run says so, with both numbers ---------- */

await feed({ ...BASE, resumed_from: 340122, is_resume: true });
const shown = await banner();
ok('a resumed run shows the banner', !shown.hidden, JSON.stringify(shown));
ok('it says how much was already done', /340,122/.test(shown.text), shown.text);
ok('it says how much there is in total', /512,890/.test(shown.text), shown.text);
ok('it explains the fast counter',
  /quickly|stepped over/i.test(shown.text), shown.text);

/* ---------- the counter tells new work from stepped-over work ---------- */

const count = () => p.evaluate(() => document.querySelector('#ar-count').textContent);
await feed({ ...BASE, resumed_from: 340122, is_resume: true, stepped_over: 300000 });
const split = await count();
ok('the count says how many are new this run', /40,122 new this run/.test(split), split);
ok('and how many were stepped over', /300,000 already done/.test(split), split);

await feed({ ...BASE, resumed_from: 0, is_resume: false, stepped_over: 0 });
ok('a first run keeps the plain count', (await count()) === '340,122 of 512,890 files',
  await count());

/* ---------- a dry run is a prediction, not a resume ---------- */

await feed({ ...BASE, job_mode: 'dry-run', resumed_from: 340122, is_resume: true });
ok('a dry run never claims to be resuming', (await banner()).hidden,
  (await banner()).text);

/* ---------- and it does not linger ---------- */

await feed({
  ...BASE, is_scanning: false, phase: 'done', job_message: 'complete',
  resumed_from: 340122, is_resume: true,
});
ok('the banner goes when the run ends, even though the engine still remembers',
  (await banner()).hidden, (await banner()).text);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
