/**
 * The console's "Recently deleted" panel.
 *
 * The API for the bin was there before the screen was, which meant putting a
 * photograph back needed curl. A bin nobody can look into is only half a
 * promise, so this checks the other half: the panel lists what went, and the
 * Put back button really does return the file to the folder it came from.
 *
 *   node tests/recycle_ui.mjs      (wants an instance on 3000 and 5000,
 *                                   admin dad / correcthorse1)
 * @fixture several
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();

const signIn = async (page) => {
  await page.goto(ADMIN, { waitUntil: 'networkidle' });
  await page.fill('#gate input[type="text"]', 'dad');
  await page.fill('#gate input[type="password"]', 'correcthorse1');
  await page.click('#gate .btn.primary');
  await page.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
};

const con = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs = [];
con.on('pageerror', (e) => errs.push(e.message));
await signIn(con);

/* ---------- an empty bin is not an error ---------- */

await con.click('#tabs button[data-tab="folders"]');
await con.waitForSelector('[data-panel="folders"].active');
await con.waitForTimeout(1500);
ok('the Folders tab has a Recently deleted panel', await con.$('#bin-block') !== null);
const startedEmpty = (await con.$$('.bin-row')).length === 0;
if (startedEmpty) {
  ok('an empty bin says so rather than erroring',
    !(await con.$eval('#bin-empty', (n) => n.hidden)));
  ok('an empty bin offers no Put back button',
    await con.$eval('#bin-foot', (n) => n.hidden));
}

/* ---------- delete two photographs from the family app ---------- */

const fam = await b.newPage({ viewport: { width: 1440, height: 900 } });
await fam.goto(HOME, { waitUntil: 'networkidle' });
await fam.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await fam.reload({ waitUntil: 'networkidle' });
await fam.waitForFunction(() => document.querySelectorAll('.cell').length >= 4,
  null, { timeout: 25000 });
await fam.waitForTimeout(1000);

const before = await fam.evaluate(() => document.querySelectorAll('.cell').length);
for (const n of [0, 1]) {
  await fam.evaluate((i) => document.querySelectorAll('.cell')[i]
    ?.dispatchEvent(new MouseEvent('click', { bubbles: true, ctrlKey: true })), n);
  await fam.waitForTimeout(250);
}
await fam.waitForSelector('#sel-delete:not([hidden])', { timeout: 8000 });
await fam.click('#sel-delete');
await fam.waitForSelector('#delete-modal .modal-card', { state: 'visible', timeout: 8000 });
await fam.fill('#delete-password', 'correcthorse1');
await fam.click('#delete-confirm');
await fam.waitForTimeout(3500);
const after = await fam.evaluate(() => document.querySelectorAll('.cell').length);
ok('the two photographs left the gallery', after === before - 2, `${before} → ${after}`);

/* ---------- they turn up in the panel ---------- */

await con.click('#tabs button[data-tab="overview"]');
await con.waitForTimeout(400);
await con.click('#tabs button[data-tab="folders"]');
await con.waitForSelector('.bin-row', { timeout: 15000 });
const rows = await con.$$eval('.bin-row', (ns) => ns.map((n) => ({
  name: n.querySelector('strong')?.textContent,
  path: n.querySelector('.path')?.textContent,
  state: n.querySelector('.bin-state')?.textContent,
  size: n.querySelector('.bin-size')?.textContent,
})));
ok('both deletions are listed', rows.length >= 2, String(rows.length));
ok('each row names the file and where it came from',
  rows.every((r) => r.name && r.path && r.path.includes('/')),
  JSON.stringify(rows[0]));
ok('each row says the file is still in the bin',
  rows.slice(0, 2).every((r) => r.state === 'In the bin'),
  JSON.stringify(rows.map((r) => r.state)));
ok('each row gives a size', rows.slice(0, 2).every((r) => /\d/.test(r.size || '')));
ok('each row shows a thumbnail',
  await con.$$eval('.bin-row .bin-thumb', (images) => images.slice(0, 2)
    .every((img) => img.complete && img.naturalWidth > 0)));
ok('the heading carries a count',
  (await con.textContent('#bin-count')).trim() === String(rows.length),
  await con.textContent('#bin-count'));

/* ---------- the two buttons are off until something is chosen ---------- */

ok('Put back starts disabled', await con.isDisabled('#bin-restore'));
ok('Erase for good starts disabled', await con.isDisabled('#bin-delete'));
await con.click('#bin-all');
await con.waitForTimeout(300);
ok('Choose all enables Put back', !(await con.isDisabled('#bin-restore')));
ok('Choose all enables Erase for good', !(await con.isDisabled('#bin-delete')));
ok('the footer counts what is chosen',
  /\d+ files? chosen/.test(await con.textContent('#bin-selected')),
  await con.textContent('#bin-selected'));
await con.click('#bin-all');
await con.waitForTimeout(300);
ok('Choose all is a toggle, and clears', await con.isDisabled('#bin-restore'));

/* ---------- erasing asks for the password, and can be backed out of ------- */

// Deliberately not carried through: this suite ends by putting the fixture
// library back the way it found it, and an erased photograph cannot be put
// back. What matters here is that the button leads to the password and to
// nothing else.
await con.click('#bin-all');
await con.waitForTimeout(300);
await con.click('#bin-delete');
await con.waitForTimeout(600);
ok('Erase for good asks for the password',
  !(await con.evaluate(() => document.querySelector('#purge-modal').hidden)));
console.log(`      "${(await con.textContent('#purge-what')).trim()}"`);

await con.fill('#purge-password', 'wrongone');
await con.click('#purge-confirm');
await con.waitForTimeout(1500);
ok('a wrong password is refused and the box stays open',
  await con.evaluate(() => !document.querySelector('#purge-modal').hidden
    && !document.querySelector('#purge-error').hidden));
const survived = await con.evaluate(async () =>
  (await (await fetch('/api/recycle')).json()).summary.items);
ok('and nothing was erased', survived === rows.length, String(survived));

await con.click('#purge-cancel');
await con.waitForTimeout(400);
ok('Cancel closes it with the bin untouched',
  await con.evaluate(() => document.querySelector('#purge-modal').hidden));

/* ---------- putting them back ---------- */

// Choose all is a toggle — this file asserts as much further up — and
// cancelling the erase dialog leaves everything still chosen. Clicking it
// again here cleared the selection and left Put back disabled, so make sure
// something is chosen rather than assuming a click will choose it.
if (await con.isDisabled('#bin-restore')) {
  await con.click('#bin-all');
  await con.waitForTimeout(300);
}
await con.click('#bin-restore');
await con.waitForTimeout(4000);
const left = (await con.$$('.bin-row')).length;
ok('restored files leave the bin', left === 0, String(left));

// And the files are on the disk again, where they were: ask the server rather
// than the screen, because the index catches up on the next scan.
const back = await con.evaluate(async () => {
  const r = await (await fetch('/api/recycle')).json();
  return r.summary.items;
});
ok('the bin is empty on the server too', back === 0, String(back));

await fam.reload({ waitUntil: 'networkidle' });
await fam.waitForTimeout(6000);
const restored = await fam.evaluate(() => document.querySelectorAll('.cell').length);
ok('the gallery has them again after the rescan', restored === before,
  `${before} expected, ${restored} shown`);

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
