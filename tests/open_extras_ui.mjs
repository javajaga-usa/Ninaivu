/**
 * "Open Extras" on the Performance page opens Extras.
 *
 * Extras moved from a page of its own into Settings, and the server's advice
 * "ffmpeg is not installed" went on naming the old page, so its button did
 * nothing. The advice only appears on a computer without ffmpeg, so the page
 * is handed it here rather than depending on what the runner has installed.
 *
 *   node tests/open_extras_ui.mjs   (wants the console up and admin
 *                                    dad / correcthorse1)
 */
import { launch, ok, done, ADMIN } from './harness.mjs';

const b = await launch();
const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.route('**/api/admin/performance', async (route) => {
  const response = await route.fetch();
  const report = await response.json();
  report.recommendations = [{
    id: 'ffmpeg', level: 'act', title: 'ffmpeg is not installed',
    detail: 'Without it videos cannot be converted for the browser.',
    action: { kind: 'tab', tab: 'extras', label: 'Open Extras' },
  }, ...(report.recommendations || []).filter((item) => item.id !== 'ffmpeg')];
  await route.fulfill({ response, json: report });
});

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.waitForTimeout(1500);

await p.click('#tabs button[data-tab="performance"]');
const button = p.locator('.panel[data-panel="performance"] .pf-action', { hasText: 'Open Extras' });
await button.waitFor({ state: 'visible', timeout: 15000 });
await button.click();
await p.waitForTimeout(600);

ok('pressing "Open Extras" opens Settings',
  await p.locator('.panel.active').getAttribute('data-panel') === 'settings');
ok('and the sidebar marks Settings as the page shown',
  await p.locator('#tabs button.active').getAttribute('data-tab') === 'settings');
ok('with the Extras section on screen', await p.locator('#extras-block').isVisible()
  && await p.evaluate(() => {
    const box = document.querySelector('#extras-block').getBoundingClientRect();
    return box.top < window.innerHeight && box.bottom > 0;
  }));
ok('without a script error', errs.length === 0);

await b.close();
done();
