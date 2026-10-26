/**
 * Sent for screening, and "Only to me": a change to who sees an item shows
 * on every open screen without a reload.
 *
 * The family side: Send for screening on the selection bar sends several at
 * once, and from the viewer one; either way they leave the sender's gallery
 * at once, and another family member's open gallery within an activity tick,
 * with nobody reloading. Keeping one "Only to me" does the same. The console
 * side: the Screening tab lists them with who sent them, the viewer's Hide
 * settles one, and Choose all with Ignore brings the rest back, again on the
 * open family pages by themselves.
 *
 *   node tests/screening_ui.mjs   (wants the family app and the console up)
 * @fixture several
 */
import { launch, ok, done, signInAtHome, signInAtConsole } from './harness.mjs';

const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();
const errs = [];

/* ---------- an admin makes two family members ---------- */
const adminContext = await b.newContext({ viewport: { width: 1400, height: 950 }, serviceWorkers: 'block' });
const admin = await adminContext.newPage();
admin.on('pageerror', (e) => errs.push(`console: ${e.message}`));
await signInAtConsole(admin);
for (const [username, name] of [['maya', 'Maya'], ['arjun', 'Arjun']]) {
  const made = await admin.evaluate(async ([user, display]) => (await fetch('/api/people', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: 'riverstones42', name: display,
      role: 'family', must_change: false }),
  })).status, [username, name]);
  ok(`${name} is made`, made === 200, String(made));
}

async function familyPage(username) {
  const context = await b.newContext({ viewport: { width: 1300, height: 900 }, serviceWorkers: 'block' });
  const page = await context.newPage();
  page.on('pageerror', (e) => errs.push(`${username}: ${e.message}`));
  await signInAtHome(page, username, 'riverstones42');
  await page.waitForSelector('#grid .cell', { timeout: 60000 });
  return page;
}

const family = await familyPage('maya');
for (let i = 0; i < 120; i++) {
  const running = await family.evaluate(async () =>
    Boolean((await (await fetch('/api/status')).json()).scan?.running));
  if (!running) break;
  await family.waitForTimeout(500);
}
const other = await familyPage('arjun');

const cellIds = (page) => page.evaluate(() =>
  [...document.querySelectorAll('#grid .cell')].map((cell) => Number(cell.dataset.id)));
/** Until none of `ids` is drawn on `page` (or all are, with `present`). */
async function waitCells(page, ids, present = false, ms = 25000) {
  for (let waited = 0; waited < ms; waited += 250) {
    const drawn = new Set(await cellIds(page));
    if (ids.every((id) => drawn.has(id) === present)) return true;
    await page.waitForTimeout(250);
  }
  return false;
}

/* ---------- the viewer sends one ---------- */
const [single] = await cellIds(family);
await family.click(`#grid .cell[data-id="${single}"]`);
await family.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await family.waitForFunction(() => !document.querySelector('#v-screen')?.hidden, null, { timeout: 10000 });
await family.evaluate(() => document.querySelector('#v-screen').click());
await family.waitForFunction(() => !document.querySelector('#screen-modal')?.hidden);
await family.click('#screen-form button[type="submit"]');
await family.waitForFunction(() => document.querySelector('#viewer')?.hidden, null, { timeout: 10000 })
  .catch(() => {});
ok('sent from the viewer, the viewer closes', await family.evaluate(() =>
  document.querySelector('#viewer').hidden));
ok('…and it is gone from the sender’s gallery at once', await waitCells(family, [single], false, 5000));
ok('…and from another member’s open gallery, without a reload',
  await waitCells(other, [single]));

/* ---------- "Only to me" shows at once too ---------- */
const [mine] = await cellIds(family);
await family.click(`#grid .cell[data-id="${mine}"]`);
await family.waitForFunction(() => !document.querySelector('#viewer')?.hidden, null, { timeout: 15000 });
await family.waitForFunction(() => !document.querySelector('#v-private')?.hidden, null, { timeout: 10000 });
await family.evaluate(() => document.querySelector('#v-private').click());
await family.keyboard.press('Escape');
ok('kept “Only to me”, it leaves the gallery behind the viewer at once',
  await waitCells(family, [mine], false, 5000));
ok('…and another member’s open gallery, without a reload', await waitCells(other, [mine]));

/* ---------- the selection bar sends several ---------- */
await family.evaluate(() => document.querySelector('[data-select-section]')?.click());
await family.waitForSelector('#selection-bar:not([hidden])', { timeout: 10000 });
const chosen = await family.evaluate(() =>
  Number((document.querySelector('#sel-count')?.textContent || '0').split(' ')[0]));
ok('several are chosen', chosen >= 2, String(chosen));
const shownBefore = await cellIds(family);
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
await family.waitForTimeout(1500);
const shownAfter = await cellIds(family);
const batch = shownBefore.filter((id) => !shownAfter.includes(id));
ok('they leave the sender’s gallery at once', batch.length === chosen,
  `${batch.length} of ${chosen}`);
ok('…and another member’s open gallery', await waitCells(other, batch));

/* ---------- the admin's queue ---------- */
const waiting = batch.length + 1;
await admin.reload({ waitUntil: 'networkidle' });
await admin.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await admin.evaluate(() => document.querySelector('#tabs button[data-tab="screening"]').click());
await admin.waitForSelector('[data-panel="screening"].active');
await admin.waitForFunction((n) => document.querySelectorAll('#scr-items .scr-item').length === n,
  waiting, { timeout: 10000 }).catch(() => {});
const tiles = await admin.$$eval('#scr-items .scr-item', (nodes) => nodes.map((n) => n.textContent));
ok('the queue lists everything sent', tiles.length === waiting, `${tiles.length} of ${waiting}`);
ok('…with who sent them', tiles.every((t) => /Maya/.test(t)), JSON.stringify(tiles));
ok('…and why', tiles.filter((t) => /Bank page/.test(t)).length === batch.length);
ok('held items are not shown as hidden by an admin',
  !(await admin.$('#scr-items .scr-item.is-hidden')));
await admin.screenshot({ path: `${SHOTS}/screening-queue.png` });
ok('the tab counts them', (await admin.textContent('#screening-count')).trim() === String(waiting));

// One from its viewer: Hide.
await admin.click('#scr-items .scr-item .fold-open');
await admin.waitForFunction(() => !document.querySelector('#scr-viewer')?.hidden);
const hiddenId = await admin.evaluate(() => Number(document.querySelector('#scr-items .scr-item').dataset.id));
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

const back = [single, ...batch].filter((id) => id !== hiddenId);
ok('ignored, they come back on the open family pages by themselves',
  await waitCells(other, back, true));
ok('the hidden one stays away', !(await cellIds(other)).includes(hiddenId));

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
