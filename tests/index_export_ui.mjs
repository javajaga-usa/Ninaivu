/**
 * Exporting and checking the whole index on the console's "Move to another
 * computer" page says at once what is happening: "Preparing…" the moment
 * Export is pressed, before the server has answered; a folder with anything
 * in it is refused with a request for an empty one; and Check reads the
 * export through, with Import offered only once that has passed. After
 * Import, a server that does not come back, or a restart that never
 * happened, is said instead of counted for ever.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const work = fs.mkdtempSync(path.join(os.tmpdir(), 'ninaivu-export-ui-'));
const full = path.join(work, 'full');
const empty = path.join(work, 'empty');
fs.mkdirSync(full);
fs.mkdirSync(empty);
fs.writeFileSync(path.join(full, 'notes.txt'), 'mine');

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);
await page.click('#tabs button[data-tab="migration"]');
await page.locator('#ix-export').waitFor({ timeout: 15000 });

// A folder with something in it: refused, on the page, in words.
await page.fill('#ix-folder', full);
await page.click('#ix-export');
const refused = page.locator('#ix-export-report .mg-alert.bad');
await refused.waitFor({ timeout: 10000 });
const said = await refused.textContent();
ok('a folder that is not empty is refused', /not empty/.test(said) && /notes\.txt/.test(said), said);
ok('and Export can be pressed again', !(await page.locator('#ix-export').isDisabled()));

// An empty one: "Preparing" before the server has answered. The answer is
// held back to make sure the words come from the press, not the reply.
let release;
const held = new Promise((resolve) => { release = resolve; });
await page.route('**/api/admin/migration/export', async (route) => {
  if (route.request().method() === 'POST') await held;
  await route.continue();
});
await page.fill('#ix-folder', empty);
await page.click('#ix-export');
const report = page.locator('#ix-export-report');
await page.waitForFunction(() => /Preparing/.test(document.querySelector('#ix-export-report')?.textContent || ''),
  null, { timeout: 2000 });
ok('Export says Preparing at once', /Preparing the export/.test(await report.textContent()));
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-preparing.png` });
release();
await page.waitForFunction(() => /Exported/.test(document.querySelector('#ix-export-report')?.textContent || ''),
  null, { timeout: 60000 });
const written = fs.readdirSync(empty).filter((n) => n.startsWith('ninaivu_export_'));
ok('the export is written into the empty folder', written.length === 1, written.join(', '));

// Check reads it through; Import is offered only once that has passed.
await page.fill('#ix-path', empty);
await page.click('#ix-look');
await page.waitForFunction(() => /are there and as they were written/.test(document.querySelector('#ix-import-report')?.textContent || ''),
  null, { timeout: 60000 });
ok('the check reads every file through', true);
ok('Import is offered after it passed', !(await page.locator('#ix-import').isDisabled()));
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-checked.png` });

// Import and restart, with the server's side played here: the page waits,
// then, with no answer for longer than the import should take, says Ninaivu
// has not come back and where its logs are, instead of counting for ever.
const bundle = path.join(empty, written[0]);
const pending = { bundle, asked_at: Date.now() / 1000 };
await page.route('**/api/admin/migration/import', (route) => route.fulfill({
  status: 202, contentType: 'application/json',
  body: JSON.stringify({ export: {}, check: null, pending, last_import: null, restart_problem: null,
    restarting: true, looked: { bundle }, patience_seconds: 1,
    restart_log: '/ninaivu/.ninaivu-control/restart.log', server_log: '/ninaivu/.ninaivu-control/server.log' }),
}));
let answer = 'gone';
await page.unroute('**/api/admin/migration/export');
await page.route('**/api/admin/migration/export', (route) => (answer === 'gone'
  ? route.abort('connectionrefused')
  : route.fulfill({ status: 200, contentType: 'application/json',
    body: JSON.stringify({ export: {}, check: null, pending, last_import: null, restart_problem: null,
      restarting: false, restart_log: '/ninaivu/.ninaivu-control/restart.log' }) })));
page.once('dialog', (dialog) => dialog.accept());
await page.click('#ix-import');
await page.waitForFunction(() => /has not come back/.test(document.querySelector('#ix-import-report')?.textContent || ''),
  null, { timeout: 20000 });
const overdue = await page.locator('#ix-import-report').textContent();
ok('a server gone too long is said to have not come back', /It may have stopped/.test(overdue), overdue);
ok('and where to look on that computer', /restart\.log/.test(overdue) && /server\.log/.test(overdue), overdue);
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-not-back.png` });

// The same server answering, the import still waiting and no restart under
// way: the restart did not happen, and the page says so.
answer = 'still here';
await page.waitForFunction(() => /could not restart itself/.test(document.querySelector('#ix-import-report')?.textContent || ''),
  null, { timeout: 20000 });
ok('a restart that never happened is said', true);

ok('no console errors', errs.length === 0, errs.join('; '));
fs.rmSync(work, { recursive: true, force: true });
await done(b);
