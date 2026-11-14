/**
 * Easy view, in a browser: not there until the console switches it on; a
 * person turns it on from their profile, and the page grows calmer and
 * larger; the viewer says who, where and when under each photograph and
 * reads it aloud (with the device's own voice here, since the runner has no
 * voice downloaded); the console's People page can turn it on for anyone.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
// The device's voice, recorded rather than heard.
await context.addInitScript(() => {
  window.__said = [];
  const fake = {
    speak(u) { window.__said.push(u.text); setTimeout(() => u.onend?.(), 50); },
    cancel() {}, getVoices: () => [], addEventListener() {},
  };
  Object.defineProperty(window, 'speechSynthesis', { value: fake, configurable: true });
});
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

async function switchOn(value) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, on]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { easy_view: on } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, value]);
  await consolePage.close();
  return answer;
}

async function openProfile() {
  await page.click('#profile-btn');
  await page.click('#profile-open');
  await page.waitForSelector('#profile-sheet:not([hidden])', { timeout: 8000 });
}

await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
await openProfile();
ok('off: the profile offers no easy view',
  !(await page.locator('#profile-sheet').textContent()).includes('Easy view'));
await page.keyboard.press('Escape');

ok('the console switches it on', (await switchOn(true)) === 200);
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });
const zoomBefore = await page.locator('#zoom').inputValue();

await openProfile();
const toggle = page.locator('#profile-sheet label.toggle', { hasText: 'Bigger photographs and words' });
ok('on: the profile offers easy view', await toggle.count() === 1);
const saved = page.waitForResponse((r) => r.url().endsWith('/api/easy-view') && r.request().method() === 'PUT');
await toggle.locator('input').check();
ok('ticking it saves it', (await saved).status() === 200);
await page.waitForFunction(() => document.documentElement.classList.contains('easy-view'), null, { timeout: 5000 });
ok('the page is in easy view', true);
await page.keyboard.press('Escape');
ok('the photographs are at their largest', await page.locator('#zoom').inputValue() === '4');
ok('adding and making are out of the way', await page.locator('#add-btn').isHidden(),
  await page.evaluate(() => getComputedStyle(document.querySelector('#add-btn').closest('.bar-menu')).display
    + ' ' + [...document.styleSheets].map((s) => s.href).join(',')));
await shot('easy-view-timeline');

const first = await page.evaluate(() => Number(document.querySelector('#grid .cell[data-id]')?.dataset.id));
await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden]) .easy-told:not([hidden])', { timeout: 8000 });
await page.waitForFunction(() => document.querySelector('.easy-words')?.textContent.trim().length > 0, null, { timeout: 8000 });
const words = await page.locator('.easy-words').textContent();
ok('the viewer says what is known of the photograph', words.length > 0, words);
await page.click('.easy-tell');
await page.waitForFunction(() => window.__said.length > 0, null, { timeout: 8000 });
ok('Tell me reads it with the device voice when the home computer has none',
  (await page.evaluate(() => window.__said[0])) === words.trim());
await shot('easy-view-viewer');
await page.keyboard.press('Escape');
ok('closing the viewer hides the words', await page.locator('.easy-told').isHidden());

await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.documentElement.classList.contains('easy-view'), null, { timeout: 8000 });
ok('it is kept on the profile, not the page', true);
await openProfile();
const off = page.waitForResponse((r) => r.url().endsWith('/api/easy-view') && r.request().method() === 'PUT');
await page.locator('#profile-sheet label.toggle', { hasText: 'Bigger photographs and words' }).locator('input').uncheck();
await off;
await page.waitForFunction(() => !document.documentElement.classList.contains('easy-view'), null, { timeout: 5000 });
ok('unticking it brings the usual page back', await page.locator('#zoom').inputValue() === zoomBefore);
await page.keyboard.press('Escape');

const consolePage = await context.newPage();
// Signed in to the console already, by switchOn.
await consolePage.goto(ADMIN, { waitUntil: 'networkidle' });
await consolePage.click('[data-tab="people"]').catch(() => {});
await consolePage.waitForSelector('.admin-person .ap-easy:not([hidden])', { state: 'attached', timeout: 10000 })
  .then(() => true, () => false);
ok('the console People page offers it for each person',
  await consolePage.locator('.admin-person .ap-easy:not([hidden])').count() > 0);
await consolePage.close();

ok('the console switches it off again', (await switchOn(false)) === 200);
ok('no page errors', errors.length === 0, errors.join(' | '));
await done(browser);
