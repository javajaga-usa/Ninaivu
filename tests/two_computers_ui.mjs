/**
 * Mugil → Two computers, on the console.
 *
 *   · a single computer shows one choice and nothing more
 *   · as the Secondary it asks for the originals folder, then shows a code
 *   · as the Primary it asks for the Secondary's address and the code
 *   · a wrong folder is refused in words, and the page stays usable
 *   · on a phone nothing is wider than the screen
 *
 * The instance is put back to one computer at the end, as it was found.
 *
 *   node tests/two_computers_ui.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { launch, ok, done, ADMIN, signInAtConsole } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

await signInAtConsole(page);
const before = await page.evaluate(async () => (await (await fetch('/api/admin/mugil')).json()));
ok('the console answers for two computers', ['single', 'primary', 'secondary'].includes(before.role),
  JSON.stringify(before));

const post = (url, body) => page.evaluate(async ([u, data]) => {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}) });
  return { status: r.status, body: await r.json() };
}, [url, body]);

if (before.paired) await post('/api/admin/mugil/unpair');
await post('/api/admin/mugil/role', { role: 'single', originals_dir: '' });

const open = async (where) => {
  await where.goto(ADMIN, { waitUntil: 'networkidle' });
  await where.waitForSelector('#tabs button[data-tab="cloud"]', { state: 'attached', timeout: 20000 });
  // The tab may sit under a group that is folded away on a small screen.
  await where.evaluate(() => document.querySelector('#tabs button[data-tab="cloud"]').click());
  await where.waitForSelector('#tc-block', { state: 'visible', timeout: 15000 });
};
await open(page);

ok('one computer: the choice is there', await page.isVisible('#tc-role'));
ok('one computer: nothing else is asked',
  !(await page.isVisible('#tc-secondary')) && !(await page.isVisible('#tc-primary')));
ok('one computer: it says so', (await page.textContent('#tc-state')).trim() === 'One computer');

// --- the Secondary ------------------------------------------------------------

await page.selectOption('#tc-role', 'secondary');
await page.click('#tc-save-role');
await page.waitForSelector('#tc-secondary:not([hidden])', { timeout: 5000 });
ok('Secondary: it asks for the folder', await page.isVisible('#tc-folder'));
ok('Secondary: no code before there is a folder', !(await page.isVisible('#tc-code')));

await page.fill('#tc-folder', 'not/a/whole/path');
await page.click('#tc-save-folder');
await page.waitForTimeout(500);
const refused = await page.evaluate(async () => (await (await fetch('/api/admin/mugil')).json()));
ok('Secondary: a folder that is not a whole path is refused', !refused.originals_dir,
  JSON.stringify(refused));

const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'ninaivu-originals-'));
await page.fill('#tc-folder', folder);
await page.click('#tc-save-folder');
await page.waitForSelector('#tc-code-row:not([hidden])', { timeout: 5000 });
ok('Secondary: the folder is kept', (await page.inputValue('#tc-folder')) === folder);
ok('Secondary: it says what is kept there', /originals kept/.test(await page.textContent('#tc-keeping')));

await page.click('#tc-code');
await page.waitForSelector('#tc-code-out:not([hidden])', { timeout: 5000 });
const code = (await page.textContent('#tc-code-out')).slice(0, 9);
ok('Secondary: a code to read out', /^[2-9A-Z]{4}-[2-9A-Z]{4}$/.test(code), code);

// --- the Primary ----------------------------------------------------------------

await page.selectOption('#tc-role', 'primary');
await page.click('#tc-save-role');
await page.waitForSelector('#tc-primary:not([hidden])', { timeout: 5000 });
ok('Primary: it asks for the address and the code',
  (await page.isVisible('#tc-url')) && (await page.isVisible('#tc-pair-code')));
ok('Primary: nothing to send before it is paired', !(await page.isVisible('#tc-send')));

// Nobody answers at this address: refused in words, and the page carries on.
await page.fill('#tc-url', 'http://127.0.0.1:9');
await page.fill('#tc-pair-code', 'AAAA-BBBB');
await page.click('#tc-pair');
await page.waitForTimeout(1500);
ok('Primary: an address that does not answer leaves it unpaired',
  await page.isVisible('#tc-pair-row') && !(await page.isVisible('#tc-paired')));

// --- a phone ------------------------------------------------------------------

const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await open(phone);
const block = await phone.evaluate(() => {
  const box = document.querySelector('#tc-block').getBoundingClientRect();
  return { right: box.right, width: window.innerWidth };
});
ok('Phone: the block fits the screen', block.right <= block.width + 1, JSON.stringify(block));

await post('/api/admin/mugil/role', { role: 'single', originals_dir: '' });
fs.rmSync(folder, { recursive: true, force: true });
ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
