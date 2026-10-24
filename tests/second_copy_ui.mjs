/**
 * Mugil → Second copy on another disk, on the console.
 *
 *   · the block is there, off, with a folder to choose
 *   · Will it fit? says what the disk there could keep
 *   · saved and switched on, it offers Copy now and Check the copy, and copies
 *   · a folder that is not a whole path is refused in words
 *   · on a phone nothing is wider than the screen
 *
 * The instance is put back as it was found.
 *
 *   node tests/second_copy_ui.mjs
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

const post = (url, body) => page.evaluate(async ([u, data]) => {
  const r = await fetch(u, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data || {}) });
  return { status: r.status, body: await r.json() };
}, [url, body]);
const before = await page.evaluate(async () => (await (await fetch('/api/mirror/status')).json()));

const open = async (where) => {
  await where.goto(ADMIN, { waitUntil: 'networkidle' });
  await where.waitForSelector('#tabs button[data-tab="cloud"]', { state: 'attached', timeout: 20000 });
  await where.evaluate(() => document.querySelector('#tabs button[data-tab="cloud"]').click());
  await where.waitForSelector('#sc-block', { state: 'visible', timeout: 15000 });
};
await open(page);
ok('the second copy is on the page', await page.isVisible('#sc-folder'));

const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'ninaivu-second-copy-'));
await page.fill('#sc-folder', folder);
await page.click('#sc-check');
await page.waitForSelector('#sc-fit:not([hidden])', { timeout: 15000 });
const says = await page.textContent('#sc-fit-says');
ok('Will it fit? answers for that disk', /That disk \(.*\) (can keep all|could not keep)/.test(says), says);

await page.check('#sc-enabled');
await page.selectOption('#sc-every', '0');
await page.click('#sc-save');
await page.waitForSelector('#sc-run-row:not([hidden])', { timeout: 5000 });
ok('switched on, it offers to copy', await page.isVisible('#sc-start') && await page.isVisible('#sc-verify'));
await page.click('#sc-start');
let status;
for (let i = 0; i < 60; i += 1) {
  status = await page.evaluate(async () => (await (await fetch('/api/mirror/status')).json()));
  if (!status.running && status.last_finished) break;
  await page.waitForTimeout(500);
}
ok('it copies the library there', status.copied === status.files && status.files > 0, JSON.stringify(status));
await page.waitForTimeout(4500);
ok('it says how far it has got', /of .* files are on the copy/.test(await page.textContent('#sc-progress')),
  await page.textContent('#sc-progress'));

await page.fill('#sc-folder', 'not/a/whole/path');
await page.click('#sc-save');
await page.waitForTimeout(600);
const kept = await page.evaluate(async () => (await (await fetch('/api/mirror/status')).json()));
ok('a folder that is not a whole path is refused', kept.folder === folder, JSON.stringify(kept.folder));

const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await open(phone);
const box = await phone.evaluate(() => {
  const r = document.querySelector('#sc-block').getBoundingClientRect();
  return { right: r.right, width: window.innerWidth };
});
ok('Phone: the block fits the screen', box.right <= box.width + 1, JSON.stringify(box));

await post('/api/mirror/settings', { enabled: before.enabled, folder: before.folder || '',
  every_hours: before.every_hours });
fs.rmSync(folder, { recursive: true, force: true });
ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
