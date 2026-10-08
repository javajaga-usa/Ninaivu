/**
 * The console's Large files list: one set of buttons for the ticked rows.
 *
 * It had Compress, Replace, Keep and Delete on every row, which on a list of
 * forty large videos is a hundred and sixty buttons and no way to do the
 * same thing to several at once. Now each row has a tick box and the four
 * buttons sit once, above the list. The server's answers are stubbed here so
 * the test needs no multi-gigabyte files; what it checks is that the buttons
 * send what the per-row ones sent, once per chosen file or once for all.
 *
 *   node tests/large_files_ui.mjs      (wants an instance on 3000 and 5000,
 *                                       admin dad / correcthorse1)
 */
import { launch, ok, done, signInAtConsole } from './harness.mjs';

const GB = 1024 ** 3;
let items = [
  { id: 901, filename: '20240907_205049.mp4', kind: 'video', size: 6.3 * GB, duration: 878, folder: 'Ninaivu Archive/2024/09/07' },
  { id: 902, filename: 'IMG_3165.MOV', kind: 'video', size: 5.7 * GB, duration: 891, folder: 'Ninaivu Archive/2023/06/16' },
  { id: 903, filename: 'VID_20220618_232007_HDR10.mp4', kind: 'video', size: 5.5 * GB, duration: 358, folder: 'Ninaivu Archive/2022/06/19' },
  { id: 904, filename: 'panorama.tif', kind: 'image', size: 1.2 * GB, folder: 'Scans' },
];
const jobs = new Map();       // asset id -> job
const asked = { compress: [], keep: [], del: [] };

const b = await launch();
const p = await b.newPage({ viewport: { width: 1400, height: 900 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

const reply = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

await p.route('**/api/admin/large-files?*', (route) =>
  reply(route, { items, floor_mb: 500 }));
await p.route('**/api/admin/large-files/keep', (route) => {
  const { ids } = route.request().postDataJSON();
  asked.keep.push(ids);
  items = items.filter((item) => !ids.includes(item.id));
  return reply(route, { kept: ids.length });
});
await p.route('**/api/admin/large-files/compress', (route) => {
  if (route.request().method() === 'GET') {
    return reply(route, { jobs: [...jobs.values()], unavailable: null, preset: 'MP4 (H.264), up to 1080p' });
  }
  const body = route.request().postDataJSON();
  asked.compress.push(body);
  if (body.mode === 'replace' && body.password !== 'correcthorse1') {
    return reply(route, { needs_password: true, error: 'That password is not right.' }, 401);
  }
  const job = { id: `j${body.id}`, asset_id: body.id, state: 'queued', ahead: jobs.size, progress: 0 };
  jobs.set(body.id, job);
  return reply(route, { job }, 202);
});
await p.route('**/api/delete', (route) => {
  const body = route.request().postDataJSON();
  if (body.password !== 'correcthorse1') {
    return reply(route, { needs_password: true, error: 'That password is not right.' }, 401);
  }
  asked.del.push(body.ids);
  items = items.filter((item) => !body.ids.includes(item.id));
  return reply(route, { deleted: body.ids.length, failed: [] });
});

await signInAtConsole(p);
await p.evaluate(() => document.querySelector('#tabs button[data-tab="large-files"]').click());
await p.waitForSelector('.lf-row', { timeout: 15000 });

/* ---------- one bar, no buttons on the rows ---------- */

ok('every file has a row', (await p.$$('.lf-row')).length === 4);
ok('every row has a tick box', (await p.$$('.lf-row .lf-check')).length === 4);
ok('no row carries its own Compress/Replace/Keep/Delete',
  (await p.$$('.lf-row button')).length === 0);
ok('the bar is shown above the list', await p.evaluate(() => {
  const bar = document.querySelector('#lf-bar');
  const list = document.querySelector('#lf-list');
  return !bar.hidden && (bar.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING);
}));
const disabled = () => p.evaluate(() => Object.fromEntries(
  ['compress', 'replace', 'keep', 'delete'].map((k) => [k, document.querySelector(`#lf-${k}`).disabled])));
let state = await disabled();
ok('with nothing ticked every button waits',
  state.compress && state.replace && state.keep && state.delete, JSON.stringify(state));

/* ---------- ticking: the box, the row, and Choose all ---------- */

await p.click('.lf-row[data-id="902"] .lf-check');
await p.click('.lf-row[data-id="903"] .lf-name');      // the row itself ticks too
ok('ticking two rows says so', (await p.textContent('#lf-selected')).includes('2'),
  await p.textContent('#lf-selected'));
ok('a ticked row is marked', await p.$eval('.lf-row[data-id="903"]', (n) => n.classList.contains('chosen')));
ok('the Choose all box is half-ticked', await p.$eval('#lf-all', (n) => n.indeterminate));
state = await disabled();
ok('with videos ticked every button is ready',
  !state.compress && !state.replace && !state.keep && !state.delete, JSON.stringify(state));

await p.click('#lf-all');
ok('Choose all ticks every row', (await p.$$('.lf-row.chosen')).length === 4);
await p.click('#lf-all');
ok('and again clears them', (await p.$$('.lf-row.chosen')).length === 0);

/* ---------- a photo alone cannot be compressed ---------- */

await p.click('.lf-row[data-id="904"] .lf-check');
state = await disabled();
ok('a photo alone leaves Compress and Replace off but Keep and Delete on',
  state.compress && state.replace && !state.keep && !state.delete, JSON.stringify(state));
await p.click('.lf-row[data-id="904"] .lf-check');

/* ---------- Compress: one request per ticked video, rows show progress ---------- */

await p.click('.lf-row[data-id="901"] .lf-check');
await p.click('.lf-row[data-id="902"] .lf-check');
await p.click('.lf-row[data-id="904"] .lf-check');      // a photo is left out
await p.click('#lf-compress');
await p.waitForFunction(() => document.querySelectorAll('.lf-job').length === 2, null, { timeout: 8000 });
ok('Compress asked once for each ticked video, in order',
  JSON.stringify(asked.compress.map((c) => [c.id, c.mode])) === JSON.stringify([[901, 'copy'], [902, 'copy']]),
  JSON.stringify(asked.compress));
ok('each compressing row has its own line and Stop',
  (await p.$$('.lf-row .lf-job button')).length === 2);
ok('started videos are no longer ticked; the photo still is',
  JSON.stringify(await p.$$eval('.lf-row.chosen', (ns) => ns.map((n) => n.dataset.id))) === '["904"]');

/* ---------- Replace: one password for every ticked video ---------- */

await p.click('.lf-row[data-id="904"] .lf-check');
await p.click('.lf-row[data-id="901"] .lf-check');     // already compressing
await p.click('.lf-row[data-id="903"] .lf-check');
asked.compress.length = 0;
await p.click('#lf-replace');
await p.waitForSelector('#lf-replace-modal:not([hidden])');
ok('Replace names only the video it can still act on',
  (await p.textContent('#lf-replace-what')).includes('VID_20220618_232007_HDR10.mp4'),
  await p.textContent('#lf-replace-what'));
await p.fill('#lf-replace-password', 'wrong');
await p.click('#lf-replace-confirm');
await p.waitForSelector('#lf-replace-error:not([hidden])');
ok('a wrong password is said in the dialog', (await p.textContent('#lf-replace-error')).length > 0);
await p.fill('#lf-replace-password', 'correcthorse1');
await p.click('#lf-replace-confirm');
await p.waitForSelector('#lf-replace-modal', { state: 'hidden' });
ok('Replace asked with the password, for the free video only',
  JSON.stringify(asked.compress.map((c) => [c.id, c.mode, c.password]))
    === JSON.stringify([[903, 'replace', 'wrong'], [903, 'replace', 'correcthorse1']]),
  JSON.stringify(asked.compress));
await p.click('.lf-row[data-id="901"] .lf-check');

/* ---------- Keep: one request for all ticked ---------- */

await p.click('.lf-row[data-id="904"] .lf-check');
await p.click('.lf-row[data-id="902"] .lf-check');
await p.click('#lf-keep');
await p.waitForFunction(() => document.querySelectorAll('.lf-row').length === 2, null, { timeout: 8000 });
ok('Keep sent both ticked files in one request',
  JSON.stringify(asked.keep) === '[[902,904]]', JSON.stringify(asked.keep));

/* ---------- Delete: one password, one request ---------- */

await p.click('#lf-all');
await p.click('#lf-delete');
await p.waitForSelector('#lf-delete-modal:not([hidden])');
ok('Delete asks about both files at once', (await p.textContent('#lf-delete-title')).length > 0
  && (await p.textContent('#lf-delete-what')).includes('2'), await p.textContent('#lf-delete-what'));
await p.fill('#lf-delete-password', 'correcthorse1');
await p.click('#lf-delete-confirm');
await p.waitForSelector('#lf-delete-modal', { state: 'hidden' });
await p.waitForFunction(() => document.querySelectorAll('.lf-row').length === 0, null, { timeout: 8000 });
ok('Delete sent both in one request', JSON.stringify(asked.del) === '[[901,903]]', JSON.stringify(asked.del));
ok('an empty list hides the bar', await p.$eval('#lf-bar', (n) => n.hidden));

/* ---------- a phone: the bar fits ---------- */

items = [{ id: 905, filename: 'clip.mp4', kind: 'video', size: 2 * GB, duration: 60, folder: 'x' }];
await p.setViewportSize({ width: 390, height: 844 });
await p.click('#lf-refresh');
await p.waitForSelector('.lf-row');
ok('on a phone the bar is no wider than the screen',
  await p.$eval('#lf-bar', (n) => n.getBoundingClientRect().right <= window.innerWidth + 1));

ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
