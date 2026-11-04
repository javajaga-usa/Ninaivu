/** Index relocation and recovery cleanup, from the live administrator console.
 * Invalid destinations use the real API. Restart/outage/result scenarios use
 * HTTP responses so this suite cannot move or restart somebody's real library.
 */
import { launch, ok, done, signInAtConsole, ADMIN } from './harness.mjs';

const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const statusUrl = '**/api/admin/migration/export';
const moveUrl = '**/api/admin/migration/move-index';
let posts = 0;
page.on('request', request => {
  if (request.url().endsWith('/migration/move-index') && request.method() === 'POST') posts++;
});
try {
  await signInAtConsole(page);
  const response = await page.request.get(`${ADMIN}/api/admin/migration/export`);
  const initial = await response.json();
  const source = initial.index_folder;
  await page.click('#tabs button[data-tab="migration"]');
  await page.locator('#index-move-current').filter({ hasText: source }).waitFor();
  ok('current index folder comes from the server', Boolean(source));
  const move = page.locator('#index-move-start');
  const report = page.locator('#index-move-report');
  await move.click();
  ok('empty destination sends no request', posts === 0);

  await page.fill('#index-move-folder', `${source}-new`);
  page.once('dialog', dialog => dialog.dismiss());
  await move.click();
  ok('cancelling confirmation sends no request', posts === 0);
  ok('cancelling keeps the move button available', await move.isEnabled());

  // The real API refuses the current directory without scheduling a restart.
  await page.fill('#index-move-folder', source);
  page.once('dialog', dialog => dialog.accept());
  await move.click();
  await report.filter({ hasText: 'outside the current index folder' }).waitFor();
  ok('API validation is visible and permits retry', await move.isEnabled());

  // Hold the accepted response: the button must disable before the network answers.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(moveUrl, async route => {
    await held;
    await route.fulfill({ status: 202, json: { restarting: false, restart_problem: 'Restart the service manually.' } });
  });
  await page.fill('#index-move-folder', `${source}-new`);
  page.once('dialog', dialog => dialog.accept());
  await move.click();
  await report.filter({ hasText: 'Scheduling' }).waitFor();
  ok('scheduling disables duplicate submissions', await move.isDisabled());
  release();
  await report.filter({ hasText: 'Restart the service manually.' }).waitFor();
  ok('manual restart explains that the move is scheduled', /scheduled/.test(await report.textContent()));

  await page.unroute(moveUrl);
  await page.route(moveUrl, route => route.fulfill({ status: 202, json: { restarting: true } }));
  const state = { ...initial, index_move_pending: null, index_move: null, recovery_cleanup: {} };
  await page.route(statusUrl, route => route.fulfill({ json: state }));
  async function reloadMigration() {
    await page.reload();
    await page.click('#tabs button[data-tab="migration"]');
    await page.locator('#index-move-current').filter({ hasText: state.index_folder }).waitFor();
  }
  await reloadMigration();
  await page.fill('#index-move-folder', `${source}-new`);
  page.once('dialog', dialog => dialog.accept());
  await move.click();
  await report.filter({ hasText: 'restarting to move the index' }).waitFor();
  ok('accepted automatic restart explains the interruption', /Refresh this page/.test(await report.textContent()));

  state.index_folder = `${source}-new`;
  state.index_move = { ok: true, source, destination: state.index_folder, recovery_files: 3, recovery_bytes: 512 };
  await reloadMigration();
  await report.filter({ hasText: 'moved and verified' }).waitFor();
  ok('returning server displays the new location and verified result', /recovery copy/.test(await report.textContent()));

  const cleanup = page.locator('#index-recovery-cleanup');
  const cleanupReport = page.locator('#index-recovery-report');
  let cleanups = 0;
  await page.route('**/api/admin/migration/recovery-cleanup', route => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { source, files: 3, bytes: 512 } });
    cleanups++;
    ok('cleanup requires explicit confirmation in the request', route.request().postDataJSON().confirm === true);
    state.recovery_cleanup = { removed: 2, bytes: 400, preserved: 1, running: false };
    return route.fulfill({ status: 202, json: state });
  });
  const cancelledCleanup = page.waitForEvent('dialog').then(dialog => dialog.dismiss());
  await cleanup.click();
  await cancelledCleanup;
  await page.waitForFunction(() => document.querySelector('#index-recovery-report').textContent === '');
  ok('cancelling cleanup deletes nothing', cleanups === 0);
  page.once('dialog', async dialog => {
    ok('cleanup confirmation names the source and preserves forwarding', dialog.message().includes(source) && /forwarding file/.test(dialog.message()));
    await dialog.accept();
  });
  await cleanup.click();
  await cleanupReport.filter({ hasText: 'cleanup finished' }).waitFor();
  ok('cleanup reports preserved files and forwarding note', /preserved 1/.test(await cleanupReport.textContent()));

  state.index_folder = source;
  state.index_move = { ok: false, error: 'The copied file could not be verified: index.db' };
  await reloadMigration();
  await report.filter({ hasText: 'original index is still in use' }).waitFor();
  ok('failed move displays the verification error and original location', /could not be verified/.test(await report.textContent()));
  ok('cleanup is unavailable after a failed move', await cleanup.isHidden());
  ok('no browser errors', errors.length === 0, errors.join('; '));
} finally {
  await done(browser);
}
