/**
 * The second copy's folder picker, and its files on the Folders screen.
 *
 *   · Browse opens the console's folder picker, and the folder chosen is
 *     described at once (free room, writable)
 *   · with the second copy on, Folders offers Library / Second copy
 *   · the copy lists its folders and files, and a folder leads back to the
 *     same folder in the library and on to the copy again
 *   · a file deleted there asks for the password first
 *   · on a phone nothing is wider than the screen
 *
 * The instance is put back as it was found.
 *
 *   node tests/second_copy_files_ui.mjs
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { launch, ok, done, ADMIN, ADMIN_PASS, signInAtConsole } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);

const get = (url) => page.evaluate(async (u) => (await (await fetch(u)).json()), url);
const post = (url, body) => page.evaluate(async ([u, data]) => {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}) });
  return { status: r.status, body: await r.json() };
}, [url, body]);
const before = await get('/api/mirror/status');

const tab = async (where, name, selector) => {
  await where.goto(ADMIN, { waitUntil: 'networkidle' });
  await where.waitForSelector(`#tabs button[data-tab="${name}"]`, { state: 'attached', timeout: 20000 });
  await where.evaluate((n) => document.querySelector(`#tabs button[data-tab="${n}"]`).click(), name);
  await where.waitForSelector(selector, { state: 'visible', timeout: 15000 });
};

/* -- Browse, and what the folder is ------------------------------------- */
await tab(page, 'cloud', '#sc-block');
const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'ninaivu-second-copy-files-'));
ok('there is a Browse button', await page.isVisible('#sc-browse'));
await page.click('#sc-browse');
await page.waitForSelector('#folder-modal:not([hidden])', { timeout: 5000 });
await page.fill('#fm-manual', folder);
await page.click('#fm-apply');
await page.waitForSelector('#sc-about:not([hidden])', { timeout: 10000 });
ok('the folder chosen fills the box', (await page.inputValue('#sc-folder')) === folder);
const about = await page.textContent('#sc-about');
ok('it says how much room there is and that Ninaivu can write there',
  /free of/.test(about) && /Ninaivu can write there/.test(about), about);

await page.check('#sc-enabled');
await page.selectOption('#sc-every', '0');
await page.click('#sc-save');
await page.waitForSelector('#sc-run-row:not([hidden])', { timeout: 5000 });
await page.click('#sc-start');
let status;
for (let i = 0; i < 60; i += 1) {
  status = await get('/api/mirror/status');
  if (!status.running && status.last_finished) break;
  await page.waitForTimeout(500);
}
ok('it copies the library there', status.copied === status.files && status.files > 0, JSON.stringify(status));

/* -- the Folders screen ---------------------------------------------------- */
await tab(page, 'folders', '#fold-block');
await page.waitForSelector('#fold-which:not([hidden])', { timeout: 10000 });
ok('Folders offers Library and Second copy', await page.isVisible('#fold-which-copy'));
await page.click('#fold-which-copy');
await page.waitForSelector('#scf-block:not([hidden]) .scf-dir, #scf-block:not([hidden]) .scf-item',
  { timeout: 10000 });
ok('the library view steps aside', await page.isHidden('#fold-block'));
ok('the copy shows where it is', (await page.textContent('#scf-where')).includes(folder));

// Down to a folder with files in it.
for (let i = 0; i < 6 && !(await page.$('.scf-item')); i += 1) {
  await page.click('.scf-dir');
  await page.waitForTimeout(400);
}
const files = await page.$$eval('.scf-item .name', (els) => els.map((e) => e.textContent));
ok('a folder of the copy lists its files', files.length > 0, JSON.stringify(files));
ok('each file says what it is',
  (await page.getAttribute('.scf-item .where', 'title')).includes('Copy of a library file'));
const copyPath = await page.evaluate(() => document.querySelector('#scf-trail .here')?.textContent);

await page.click('#scf-to-lib');
await page.waitForSelector('#fold-block:not([hidden])', { timeout: 5000 });
await page.waitForFunction((want) => document.querySelector('#fold-trail .here')?.textContent === want,
  copyPath, { timeout: 5000 }).catch(() => {});
const libHere = await page.evaluate(() => document.querySelector('#fold-trail .here')?.textContent);
ok('Show in library opens the same folder of the library', libHere === copyPath,
  `${libHere} / ${copyPath}`);
await page.click('#fold-to-copy');
await page.waitForSelector('#scf-block:not([hidden]) .scf-item', { timeout: 10000 });
await page.waitForFunction((want) => document.querySelector('#scf-trail .here')?.textContent === want,
  copyPath, { timeout: 5000 }).catch(() => {});
ok('and Show in second copy goes back', await page.evaluate(
  () => document.querySelector('#scf-trail .here')?.textContent) === copyPath);

const shot = process.env.NINAIVU_SHOTS;
if (shot) await page.screenshot({ path: path.join(shot, 'second-copy-files-desktop.png'), fullPage: true });

/* -- deleting on the copy ------------------------------------------------ */
await page.click('.scf-item input[type=checkbox]');
ok('choosing a file offers Delete', await page.isEnabled('#scf-delete'));
await page.click('#scf-delete');
await page.waitForSelector('#lf-delete-modal:not([hidden])', { timeout: 5000 });
ok('the dialog asks for the password', await page.isVisible('#lf-delete-password'));
ok('and says the library is not touched',
  (await page.textContent('#lf-delete-note-other')).includes('The library is not touched'));
await page.fill('#lf-delete-password', ADMIN_PASS);
await page.click('#lf-delete-confirm');
await page.waitForTimeout(1200);
const after = await get('/api/mirror/status');
ok('the file is gone from the copy and owed again', after.owed === 1, JSON.stringify(after.owed));

/* -- a phone ------------------------------------------------------------------ */
const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await tab(phone, 'folders', '#fold-block');
await phone.waitForSelector('#fold-which:not([hidden])', { timeout: 10000 });
await phone.click('#fold-which-copy');
await phone.waitForSelector('#scf-block:not([hidden]) .scf-dir', { timeout: 10000 });
await phone.click('.scf-dir');
await phone.waitForTimeout(600);
const wide = await phone.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
ok('Phone: nothing is wider than the screen', wide <= 1, String(wide));
if (shot) await phone.screenshot({ path: path.join(shot, 'second-copy-files-phone.png'), fullPage: false });

await post('/api/mirror/settings', { enabled: before.enabled, folder: before.folder || '',
  every_hours: before.every_hours });
fs.rmSync(folder, { recursive: true, force: true });
ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
