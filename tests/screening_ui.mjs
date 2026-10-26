/**
 * Sent for screening: a family member flags photographs, an admin decides.
 *
 * The family side: Send for screening on the selection bar sends several at
 * once, and the viewer's button says it was sent and takes it back. The items
 * stay in the gallery. The console side: the Screening tab lists them with
 * who sent them, Choose all and one decision settles many, and the viewer's
 * own buttons settle one.
 *
 *   node tests/screening_ui.mjs   (wants the family app and the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtHome, signInAtConsole } from './harness.mjs';

const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();
const errs = [];

/* ---------- an admin makes a family member ---------- */
const adminContext = await b.newContext({ viewport: { width: 1400, height: 950 }, serviceWorkers: 'block' });
const admin = await adminContext.newPage();
admin.on('pageerror', (e) => errs.push(`console: ${e.message}`));
await signInAtConsole(admin);
const made = await admin.evaluate(async () => (await fetch('/api/people', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'maya', password: 'riverstones42', name: 'Maya',
    role: 'family', must_change: false }),
})).status);
ok('a family member is made', made === 200, String(made));

/* ---------- the family member sends three ---------- */
const familyContext = await b.newContext({ viewport: { width: 1300, height: 900 }, serviceWorkers: 'block' });
const family = await familyContext.newPage();
family.on('pageerror', (e) => errs.push(`family: ${e.message}`));
await signInAtHome(family, 'maya', 'riverstones42');
await family.waitForSelector('#grid .cell', { timeout: 60000 });
for (let i = 0; i < 120; i++) {
  const running = await family.evaluate(async () =>
    Boolean((await (await fetch('/api/status')).json()).scan?.running));
  if (!running) break;
  await family.waitForTimeout(500);
}

const before = await family.evaluate(async () =>
  (await (await fetch('/api/assets?limit=200')).json()).items.map((i) => i.id));

// Chosen the way a person does: the section's "Select all".
await family.evaluate(() => document.querySelector('[data-select-section]')?.click());
await family.waitForSelector('#selection-bar:not([hidden])', { timeout: 10000 });
const chosen = await family.evaluate(() =>
  Number((document.querySelector('#sel-count')?.textContent || '0').split(' ')[0]));
ok('several are chosen', chosen >= 2, String(chosen));
ok('the selection bar offers Send for screening',
  await family.evaluate(() => !document.querySelector('#sel-screen').hidden));
await family.click('#sel-screen');
await family.waitForFunction(() => !document.querySelector('#screen-modal')?.hidden);
await family.fill('#screen-note', 'Bank page');
await family.screenshot({ path: `${SHOTS}/screening-send.png` });
const sent = family.waitForResponse((r) => r.url().endsWith('/api/screening')
  && r.request().method() === 'POST');
await family.click('#screen-form button[type="submit"]');
const body = await (await sent).json();
ok('all of them are sent at once', body.sent === chosen, JSON.stringify(body));
const after = await family.evaluate(async () =>
  (await (await fetch('/api/assets?limit=200')).json()).items.map((i) => i.id));
ok('they stay in the gallery', after.length === before.length, `${before.length} → ${after.length}`);

// The viewer says it was sent, and takes one back.
const first = await family.evaluate(() => document.querySelector('[data-select-section]')
  ?.closest('section, .section, [data-section]')?.querySelector('.cell')?.dataset.id
  || document.querySelector('#grid .cell').dataset.id);
await family.click(`#grid .cell[data-id="${first}"]`);
await family.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await family.waitForFunction(() => document.querySelector('#v-screen')?.classList.contains('on'),
  null, { timeout: 10000 }).catch(() => {});
const wasOn = await family.evaluate(() => document.querySelector('#v-screen').classList.contains('on'));
ok('the viewer shows it was sent', wasOn);
await family.evaluate(() => document.querySelector('#v-screen').click());
await family.waitForFunction(() => !document.querySelector('#v-screen')?.classList.contains('on'),
  null, { timeout: 10000 }).catch(() => {});
ok('pressed again, the request is taken back', await family.evaluate(() =>
  !document.querySelector('#v-screen').classList.contains('on')));
const waiting = chosen - (wasOn ? 1 : 0);

/* ---------- the admin's queue ---------- */
await admin.reload({ waitUntil: 'networkidle' });
await admin.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await admin.evaluate(() => document.querySelector('#tabs button[data-tab="screening"]').click());
await admin.waitForSelector('[data-panel="screening"].active');
await admin.waitForFunction((n) => document.querySelectorAll('#scr-items .scr-item').length === n,
  waiting, { timeout: 10000 }).catch(() => {});
const tiles = await admin.$$eval('#scr-items .scr-item', (nodes) => nodes.map((n) => n.textContent));
ok('the queue lists those still sent', tiles.length === waiting, `${tiles.length} of ${waiting}`);
ok('…with who sent them and why', tiles.every((t) => /Maya/.test(t) && /Bank page/.test(t)),
  JSON.stringify(tiles));
await admin.screenshot({ path: `${SHOTS}/screening-queue.png` });
ok('the tab counts them', (await admin.textContent('#screening-count')).trim() === String(waiting));

// One from its viewer: Hide.
await admin.click('#scr-items .scr-item .fold-open');
await admin.waitForFunction(() => !document.querySelector('#scr-viewer')?.hidden);
await admin.screenshot({ path: `${SHOTS}/screening-viewer.png` });
await admin.click('#sv-hide');
await admin.waitForFunction((n) => document.querySelectorAll('#scr-items .scr-item').length === n,
  waiting - 1, { timeout: 10000 }).catch(() => {});
ok('hidden from the viewer, it leaves the queue',
  (await admin.$$('#scr-items .scr-item')).length === waiting - 1);

// The rest at once: Choose all, then Ignore.
await admin.evaluate(() => document.querySelector('#sv-close')?.click());
await admin.click('#scr-all');
await admin.click('#scr-ignore');
await admin.waitForFunction(() => !document.querySelector('#scr-empty')?.hidden, null, { timeout: 10000 })
  .catch(() => {});
ok('ignoring the rest empties the queue', await admin.evaluate(() =>
  !document.querySelector('#scr-empty').hidden && !document.querySelector('#scr-items').children.length));

// The family member no longer sees the hidden one, and still sees the ignored ones.
const visible = await family.evaluate(async () =>
  (await (await fetch('/api/assets?limit=200')).json()).items.map((i) => i.id));
ok('the family no longer sees the hidden one, and keeps the rest',
  visible.length === before.length - 1, `${before.length} → ${visible.length}`);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
