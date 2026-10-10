/**
 * The Tuning page: it shows the machine, the profile, the expected use and
 * every number, and a profile chosen there is saved and can be undone.
 *
 *   node tests/tuning_ui.mjs   (wants the console up and admin
 *                               dad / correcthorse1)
 *
 * NINAIVU_SHOTS (or TUNING_SHOTS)=<folder> also saves a picture of the page,
 * wide and narrow. The phone check looks at the page itself: the console bar
 * around it is checked by the layout tests.
 */
import { launch, ok, done, ADMIN } from './harness.mjs';

const shots = process.env.TUNING_SHOTS || process.env.NINAIVU_SHOTS;
const b = await launch();
const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.waitForTimeout(1500);

await p.click('#tabs button[data-tab="tuning"]');
await p.locator('#tn-knobs .tn-knob').first().waitFor({ state: 'visible', timeout: 15000 });

ok('the Tuning page is the page shown',
  await p.locator('.panel.active').getAttribute('data-panel') === 'tuning');
ok('every setting has a row with a number',
  await p.locator('#tn-knobs .tn-knob input[type="number"]').count() === 7);
ok('the machine is described', (await p.locator('#tn-summary').textContent()).includes('This computer measures as'));
ok('the four profiles and automatic are offered',
  await p.locator('#tn-profiles input[name="tn-profile"]').count() === 5);
ok('the expected use is shown', await p.locator('#tn-usage .card').count() === 4);
if (shots) await p.screenshot({ path: `${shots}/tuning-wide.png`, fullPage: true });

await p.check('#tn-profiles input[value="peak"]');
await p.click('#tn-use-profile');
await p.waitForFunction(() => document.querySelector('#tn-summary').textContent.includes('Tuned as Peak performance'));
ok('peak is saved and named', (await p.locator('#tn-summary').textContent()).includes('Peak performance'));
ok('and allows every core and 95% of the memory',
  /100%.*95%/.test(await p.locator('#tn-ceiling').textContent()));

const workers = p.locator('#tn-knob-workers');
await workers.fill('2');
await p.click('#tn-save');
await p.waitForFunction(() => [...document.querySelectorAll('#tn-knobs .pf-chip')].some((c) => c.textContent === 'Set by you'));
ok('a number set by hand says so', true);

await p.click('#tn-reset');
await p.waitForFunction(() => !document.querySelector('#tn-summary').textContent.includes('Tuned as Peak'));
ok('back to automatic clears the profile and the number',
  await p.locator('#tn-profiles input[value="auto"]').isChecked()
  && !(await p.locator('#tn-knobs').textContent()).includes('Set by you'));

await p.setViewportSize({ width: 390, height: 900 });
await p.waitForTimeout(400);
const overflow = await p.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
const wide = await p.evaluate(() => [...document.querySelectorAll('[data-panel="tuning"] *')]
  .filter((n) => n.getBoundingClientRect().right > window.innerWidth + 1)
  .slice(0, 5).map((n) => `${n.tagName}.${n.className}#${n.id}`).join(', '));
ok('nothing on the page runs off a phone screen', overflow <= 1 || !wide, `${overflow}px wider: ${wide}`);
if (shots) await p.screenshot({ path: `${shots}/tuning-narrow.png`, fullPage: true });

ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
