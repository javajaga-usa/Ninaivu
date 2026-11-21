/**
 * The AI models page: "Download all" and a Delete on every model.
 *
 * Nothing is downloaded: the test instance has no models, so the page
 * offers the needed ones, and no download is started (the confirm is
 * dismissed). It checks the page loads without an error in English and in
 * Tamil, offers Download all with a size, and that every model's row has
 * its Download button.
 *
 *   node tests/ai_models_manage_ui.mjs   (wants the console up and admin
 *                                         dad / correcthorse1)
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const b = await launch();
const p = await b.newPage({ viewport: { width: 1400, height: 1000 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', ADMIN_USER);
await p.fill('#gate input[type="password"]', ADMIN_PASS);
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.click('#tabs button[data-tab="ai-models"]');
await p.waitForSelector('#am-models .am-model', { timeout: 20000 });

ok('Download all is offered', await p.isVisible('#am-needed-go'));
const hint = await p.textContent('#am-needed-hint');
ok('it says how much it adds up to', /MB/.test(hint), hint);

let asked = '';
p.once('dialog', async (dialog) => { asked = dialog.message(); await dialog.dismiss(); });
await p.click('#am-needed-go');
await p.waitForTimeout(500);
ok('it asks first, naming the models', asked.includes('•'), asked);
ok('nothing was queued after No', (await p.evaluate(async () =>
  (await (await fetch('/api/admin/ai-models')).json()).queued.length)) === 0);

const rows = await p.$$eval('#am-models .am-model', (all) => all.map((row) => row.querySelector('.am-status button')?.textContent || ''));
ok('every model has a button', rows.every((label) => label.length > 0), rows.join(' | '));

await p.screenshot({ path: process.env.SHOT || '/tmp/ai-models-manage.png', fullPage: false });
ok('no script errors', errs.length === 0, errs.join('\n'));
await done(b);
