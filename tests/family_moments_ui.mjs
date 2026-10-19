/**
 * The living photo frame and time capsules, in a browser: hidden until the
 * console switches them on; the frame opens full screen at /#frame and shows
 * a photograph with what it says about it; a capsule is sealed from a
 * selection and shows as a closed envelope that will not open early.
 */
import { launch, ok, done, HOME, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('dialog', (dialog) => dialog.accept());
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

async function switches(on) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, value]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { photo_frame: value, time_capsules: value } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, on]);
  await consolePage.close();
  return answer;
}

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
ok('off: no frame button', await page.locator('#frame-btn').isHidden());
ok('off: no capsules button', await page.locator('#capsules-btn').isHidden());

ok('the console switches both on', (await switches(true)) === 200);
await page.goto(`${HOME}/#frame`, { waitUntil: 'networkidle' });
await page.waitForSelector('#photo-frame:not([hidden])', { timeout: 10000 });
ok('/#frame opens the frame', true);
await page.waitForSelector('#photo-frame .frame-layer.shown img', { timeout: 15000 });
const caption = await page.locator('#photo-frame .frame-when').textContent();
ok('a photograph with when it was taken', Boolean(caption?.trim()), caption);
ok('a clock in the corner', Boolean((await page.locator('#photo-frame .frame-time').textContent())?.trim()));
await page.mouse.move(640, 300);
await page.waitForTimeout(400);
await shot('frame');
await page.keyboard.press('ArrowRight');
await page.waitForTimeout(2600);
await page.keyboard.press('Escape');
ok('Escape closes it and the address forgets #frame',
  await page.locator('#photo-frame').isHidden() && !page.url().includes('#frame'));

// The page offers what its last look at the server said: a reload brings
// the switches the console just turned on.
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
ok('on: the frame button shows', await page.locator('#frame-btn').isVisible());
ok('on: the capsules button shows', await page.locator('#capsules-btn').isVisible());

// Seal photographs from a selection: the section's "Select all", as a person does.
await page.evaluate(() => document.querySelector('[data-select-section]')?.click());
await page.waitForSelector('#selection-bar:not([hidden])', { timeout: 5000 });
await page.click('#sel-capsule');
await page.waitForSelector('.capsule-form', { timeout: 5000 });
await page.fill('.capsule-form input[type="text"]', 'For Arun, on his eighteenth birthday');
await page.fill('.capsule-form textarea', 'Dear Arun, these are from your first year.');
await shot('capsule-seal');
await page.click('.capsule-form button[type="submit"]');
await page.waitForSelector('.capsule-countdown', { timeout: 5000 });
ok('sealed: a countdown, and no letter', !(await page.locator('.capsule-modal').textContent()).includes('Dear Arun'));
await shot('capsule-sealed');
await page.click('.capsule-modal .btn.ghost:not(.danger-text)');
await page.waitForSelector('.capsule-row', { timeout: 5000 });
ok('it is listed', (await page.locator('.capsule-row').count()) === 1);
await shot('capsule-list');

ok('no script errors', errors.length === 0, errors.join(' | '));
await switches(false);
await done(browser);
