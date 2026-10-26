/**
 * The first day's question: one computer or two, and the steps that follow.
 *
 *   · Mugil → Two computers → Set up step by step opens the same steps
 *   · one computer: a single step, then done
 *   · the Secondary: originals folder, how the originals first arrive, and
 *     pairing with the Primary's address and code
 *   · copying by hand: the instructions, then "I have finished copying"
 *     checks the copy and lets the Primary in
 *   · a Primary that does not answer is said in words, and the page carries on
 *   · the Primary: it looks for the Secondary and offers a code
 *   · on a phone nothing is wider than the screen
 *
 * The instance is put back to one computer at the end, as it was found.
 *
 *   node tests/first_day_computers_ui.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { launch, ok, done, ADMIN, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];

const post = (page, url, body) => page.evaluate(async ([u, data]) => {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}) });
  return { status: r.status, body: await r.json() };
}, [url, body]);

const status = (page) => page.evaluate(async () => (await (await fetch('/api/admin/mugil')).json()));

const openGuide = async (page) => {
  await page.goto(ADMIN, { waitUntil: 'networkidle' });
  await page.waitForSelector('#tabs button[data-tab="cloud"]', { state: 'attached', timeout: 20000 });
  await page.evaluate(() => document.querySelector('#tabs button[data-tab="cloud"]').click());
  await page.waitForSelector('#tc-guide', { state: 'visible', timeout: 15000 });
  await page.click('#tc-guide');
  await page.waitForSelector('#first-day:not([hidden])', { timeout: 5000 });
};

const SHOTS = process.env.NINAIVU_SHOTS;
const shot = async (page, name) => {
  if (SHOTS) await page.locator('#first-day .fd-card').screenshot({ path: path.join(SHOTS, `first-day-${name}.png`) });
};

const steps = (page) => page.$$eval('#fd-steps li', (lis) => lis.map((li) => li.dataset.step));
const title = (page) => page.textContent('#fd-title');

const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);
const before = await status(page);
if (before.paired) await post(page, '/api/admin/mugil/unpair');
await post(page, '/api/admin/mugil/role', { role: 'single', originals_dir: '' });

await openGuide(page);
ok('the question comes first', /How many computers/.test(await title(page)), await title(page));
ok('one computer: one step', JSON.stringify(await steps(page)) === '["computers"]',
  JSON.stringify(await steps(page)));
ok('three choices', (await page.$$('#fd-body input[name="fd-part"]')).length === 3);
await shot(page, '1-computers');

// --- the Secondary ------------------------------------------------------------

await page.check('#fd-body input[value="secondary"]');
await page.waitForFunction(() => document.querySelectorAll('#fd-steps li').length === 4);
ok('Secondary: its own steps', JSON.stringify(await steps(page))
  === '["computers","originals","copying","primary"]', JSON.stringify(await steps(page)));
await page.click('#fd-next');
await page.waitForFunction(() => /originals be kept/.test(document.querySelector('#fd-title').textContent));
ok('Secondary: no folder yet, and it says pairing waits for one',
  /cannot pair until/.test(await page.textContent('#fd-note')));

const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'ninaivu-originals-'));
await post(page, '/api/admin/mugil/role', { role: 'secondary', originals_dir: folder });
await page.click('#fd-back');
await page.click('#fd-next');
await page.waitForFunction((f) => document.querySelector('#fd-body .fd-chosen')?.textContent === f, folder);
ok('Secondary: the folder is shown', true);

await page.click('#fd-next');
await page.waitForFunction(() => /first arrive/.test(document.querySelector('#fd-title').textContent));
ok('Secondary: automatic is the first answer',
  await page.isChecked('#fd-body input[value="automatic"]'));
await page.check('#fd-body input[value="manual"]');
await page.waitForSelector('#fd-body .fd-manual', { timeout: 5000 });
ok('Manual: it says where to copy', (await page.textContent('#fd-body .fd-manual')).includes(folder));
ok('Manual: the Primary waits', (await status(page)).manual.state === 'copying');
await shot(page, '2-copy-by-hand');
await page.click('text=I have finished copying');
await page.waitForFunction(() => /Checked:/.test(document.querySelector('#fd-body').textContent),
  null, { timeout: 15000 });
ok('Manual: checked, and the Primary is let in', (await status(page)).manual.state === 'ready');

await page.click('#fd-next');
await page.waitForFunction(() => /Pair with the Primary/.test(document.querySelector('#fd-title').textContent));
ok('Secondary: the address and code are asked',
  (await page.$$('#fd-body form input')).length === 2);
ok('Secondary: it says it is waiting to be found', /waiting now/.test(await page.textContent('#fd-body')));
await shot(page, '3-pair-with-primary');
await page.fill('#fd-body form input >> nth=0', '127.0.0.1:9');
await page.fill('#fd-body form input >> nth=1', 'AAAA-BBBB');
await page.click('#fd-body form button');
await page.waitForTimeout(2000);
ok('Secondary: a Primary that does not answer leaves it unpaired and asking',
  !(await status(page)).paired && (await page.$$('#fd-body form input')).length === 2);

// --- the Primary ----------------------------------------------------------------

await page.click('#fd-back');
await page.click('#fd-back');
await page.click('#fd-back');
await post(page, '/api/admin/mugil/role', { role: 'primary' });
await page.click('#fd-skip-all').catch(() => {});
await openGuide(page);
ok('Primary: the choice is kept', await page.isChecked('#fd-body input[value="primary"]'));
ok('Primary: pairing follows', JSON.stringify(await steps(page)) === '["computers","partner"]',
  JSON.stringify(await steps(page)));
await page.click('#fd-next');
await page.waitForFunction(() => /Pair with the Secondary/.test(document.querySelector('#fd-title').textContent));
await page.click('text=Show a code for the Secondary');
await page.waitForSelector('#fd-body .tc-code:not([hidden])', { timeout: 5000 });
await shot(page, '4-pair-with-secondary');
ok('Primary: a code for the Secondary',
  /^[2-9A-Z]{4}-[2-9A-Z]{4}/.test(await page.textContent('#fd-body .tc-code')));

// --- a phone ------------------------------------------------------------------

const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await post(phone, '/api/admin/mugil/role', { role: 'secondary', originals_dir: folder });
await openGuide(phone);
const card = await phone.evaluate(() => {
  const box = document.querySelector('#first-day .fd-card').getBoundingClientRect();
  return { right: box.right, width: window.innerWidth };
});
ok('Phone: the steps fit the screen', card.right <= card.width + 1, JSON.stringify(card));

await post(phone, '/api/admin/mugil/role', { role: 'secondary', copy_mode: 'automatic' });
await post(phone, '/api/admin/mugil/role', { role: 'single', originals_dir: '' });
fs.rmSync(folder, { recursive: true, force: true });
ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
