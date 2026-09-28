/**
 * Telling you a long run has finished, without you having to watch the tab.
 *
 * A consolidation of a family's drives runs for hours. Nobody sits in front of
 * it, so the console has to reach out rather than wait to be looked at: the
 * page title carries the progress so a background tab is readable at a glance,
 * and a browser notification arrives when the run ends.
 *
 * The progress is faked here through the status stream rather than by running
 * a real four-hour job — what is under test is the console's behaviour on a
 * given status, and the server's own behaviour is covered by the Python suite.
 *
 *   node tests/run_notify_ui.mjs      (wants a console on 3000,
 *                                      admin dad / correcthorse1)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

// Grant notifications up front: the permission dialog is native chrome and
// cannot be clicked from here, and what is under test is what the page does
// once it has permission.
const context = await b.newContext({
  viewport: { width: 1500, height: 1000 },
  permissions: ['notifications'],
  baseURL: ADMIN,
});
const p = await context.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

// Force the polling path and record every notification the page raises. No
// test hook is added to the app: the payloads go in through the real status
// endpoint, so this exercises the same code an actual run does.
await p.addInitScript(() => {
  delete window.EventSource;
  window.__notes = [];
  class FakeNotification {
    constructor(title, options) {
      window.__notes.push({ title, body: options?.body || '', tag: options?.tag });
    }
    static permission = 'granted';
    static requestPermission() { return Promise.resolve('granted'); }
    close() {}
    addEventListener() {}
  }
  window.Notification = FakeNotification;
});

// The status the panel will see next. Swapped between assertions.
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

const baseTitle = await p.title();
ok('the title is the plain one when nothing is running',
  !/%/.test(baseTitle), baseTitle);

/* ---------- drive the panel through a run ---------- */

// Hand the panel a status and wait for its next poll to pick it up.
const feed = async (data) => {
  status = data;
  await p.waitForTimeout(2600);          // the poll runs every 2s
};

const RUNNING = {
  is_scanning: true, is_paused: false, job_mode: 'copy', phase: 'copying',
  processed: 2500, total_files: 10000, verified: 2400, duplicates: 80,
  skipped: 20, errors: 0, pending: 7500, planned: 0, plan_duplicates: 0,
  plan_skipped: 0, total_scanned: 2500, bytes_copied: 1234567890,
  eta_seconds: 5400, resumed_from: 0, is_resume: false, job_message: '',
  recent: [], years: [],
};

await feed(RUNNING);
const runningTitle = await p.title();
ok('a running job puts its progress in the tab title',
  /25%/.test(runningTitle), runningTitle);
ok('the title still says which app this is',
  /Ninaivu/i.test(runningTitle), runningTitle);

await feed({ ...RUNNING, processed: 7000, verified: 6900 });
ok('the title tracks the run', /70%/.test(await p.title()), await p.title());

/* ---------- a paused run is not a running one ---------- */

await feed({ ...RUNNING, processed: 7000, is_paused: true });
ok('a paused run says so in the title rather than looking live',
  /paused/i.test(await p.title()), await p.title());

/* ---------- finishing ---------- */

await feed({ ...RUNNING, is_paused: false, processed: 7000 });
await feed({
  ...RUNNING, is_scanning: false, phase: 'done', processed: 10000,
  verified: 9800, duplicates: 150, skipped: 50, job_message: 'complete',
});

// Only the run's own notices. The archive also reports when its health
// changes, which is its own feature with its own tag and not this test's
// business — counting both made every assertion below read one place along.
const runNotes = () => p.evaluate(
  () => window.__notes.filter((n) => n.tag === 'ninaivu-archive-run'));

const notes = await runNotes();
ok('finishing raises a notification', notes.length === 1, JSON.stringify(notes));
ok('the notification names the app', /Ninaivu/i.test(notes[0]?.title || ''),
  notes[0]?.title);
ok('the notification says what happened',
  /9,800|9800/.test(notes[0]?.body || ''), notes[0]?.body);
ok('the title goes back to normal when the run ends',
  (await p.title()) === baseTitle, await p.title());

/* ---------- it does not nag ---------- */

await feed({
  ...RUNNING, is_scanning: false, phase: 'done', processed: 10000,
  verified: 9800, job_message: 'complete',
});
ok('a second poll of the same finished run does not notify again',
  (await runNotes()).length === 1,
  String((await runNotes()).length));

/* ---------- a failed run is worth saying out loud too ---------- */

await feed(RUNNING);
await feed({
  ...RUNNING, is_scanning: false, phase: 'error', errors: 12,
  job_message: 'stopped with errors',
});
const all = await runNotes();
ok('a run that ends badly notifies as well', all.length === 2, JSON.stringify(all));
ok('and says that it went wrong',
  /error|wrong|stopped/i.test(all[1]?.body + all[1]?.title), JSON.stringify(all[1]));

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
