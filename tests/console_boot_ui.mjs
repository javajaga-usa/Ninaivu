/**
 * Opening the console asks nothing it is not allowed to ask.
 *
 * Found by looking at the Migration page — three pages that had been built,
 * tested and shipped without a person ever seeing them rendered. Over the
 * sign-in form sat a red toast: "Your session has ended. Please sign in
 * again." Nothing had ended. `ArchivePanel.wire()` called `checkDevices()`
 * while `init()` was still assembling the page, so `/api/devices` went out
 * before the gate was answered, and the 401 that came back was reported as an
 * expiry — to somebody who had not yet signed in for the first time.
 *
 * Two things are pinned here. A console that has just been opened sends
 * nothing that comes back 401, and it never claims a session ended before one
 * has started. The second matters on its own: a 401 can still arrive for other
 * reasons, and telling somebody their session expired when they never had one
 * sends them looking for a problem that is not there.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const browser = await launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });

const refused = [];
page.on('response', (r) => {
  if (r.status() === 401) refused.push(new URL(r.url()).pathname);
});

await page.goto(ADMIN, { waitUntil: 'networkidle' });
await page.waitForTimeout(2000);           // let anything eager fire

ok('a freshly opened console is refused nothing',
  refused.length === 0, refused.join(', '));

const shown = await page.evaluate(() => [...document.querySelectorAll(
  '.toast, #toast, [class*="toast"]')].map((n) => (n.innerText || '').trim())
  .filter(Boolean).join(' | '));
ok('and does not say a session ended before one started',
  !shown.includes('session has ended'), shown);

ok('the sign-in form is up', await page.isVisible('#gate input[type="password"]'));

await page.fill('#gate input[type="text"]', ADMIN_USER);
await page.fill('#gate input[type="password"]', ADMIN_PASS);
await page.click('#gate .btn.primary');
await page.waitForSelector('#tabs', { state: 'visible', timeout: 20000 });
ok('signing in still works', true);

// The device list is the thing that used to be asked too early. It must still
// be asked — just when the page that shows it is opened.
const archiveTab = await page.$('#tabs button[data-tab="archive"]');
if (archiveTab) {
  const asked = new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), 8000);
    page.on('request', (r) => {
      if (r.url().includes('/api/devices')) { clearTimeout(timer); resolve(true); }
    });
  });
  await archiveTab.click();
  ok('opening the Archive page is when the devices are looked up', await asked);
}

ok('and nothing was refused along the way',
  refused.length === 0, refused.join(', '));

await done(browser);
