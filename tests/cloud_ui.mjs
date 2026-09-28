/**
 * The Cloud tab in a real browser.
 *
 * The Python tests prove the endpoints behave. What they cannot show is
 * whether the screen an administrator actually meets works — and this panel
 * has more ways to be quietly broken than most, because almost all of it is
 * conditional: the setup instructions hide once an account is connected, the
 * buttons enable in an order, the poll only runs on the visible tab.
 *
 * So this drives it the way a person would, and checks the things that would
 * make it useless rather than the things that would make it ugly:
 *
 *   · the tab exists and opens without a script error
 *   · the redirect URI to paste into Google is shown, and is the real one
 *   · Connect is refused until a client ID and secret have been saved
 *   · switching cloud backup on persists across a reload
 *   · "check for new files" queues the library once and says so, and says
 *     something different the second time
 *   · the counts on the cards agree with what the API reports
 *   · nothing that looks like a token is anywhere in the page
 *   · the poll stops when the tab is left
 *
 *   node tests/cloud_ui.mjs
 *      (wants a console on 3000, admin dad / correcthorse1)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';

const b = await launch();
// Some of this can only be seen on a console that has not been set up yet —
// "Connect is refused before a client is saved" is not a thing you can observe
// twice. Rather than fail on the second run against the same instance, say
// which checks the starting state did not allow.
const skip = (l, why) => console.log(`SKIP  ${l} — ${why}`);

const page = await b.newPage({ viewport: { width: 1440, height: 950 } });
const errs = [];
page.on('pageerror', (e) => errs.push(e.message));

const statusCalls = [];
page.on('request', (r) => {
  if (r.url().includes('/api/cloud/status')) statusCalls.push(Date.now());
});

await page.goto(ADMIN, { waitUntil: 'networkidle' });
await page.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await page.reload({ waitUntil: 'networkidle' });
await page.waitForSelector('#tabs button[data-tab="cloud"]', { timeout: 20000 });

// --- the tab opens ---------------------------------------------------------

await page.click('#tabs button[data-tab="cloud"]');
await page.waitForTimeout(900);

ok('the Cloud panel is on screen',
  await page.evaluate(() => !!document.querySelector(
    '.panel.active[data-panel="cloud"]')));
ok('no script errors opening it', errs.length === 0, errs.join(' | '));

// --- setup -----------------------------------------------------------------

const redirect = await page.textContent('#cl-redirect');
ok('the redirect URI to paste into Google is shown',
  /^https?:\/\/[^/]+\/api\/cloud\/callback$/.test(redirect.trim()), redirect);

// What this instance looked like before the test touched it.
const before = await page.evaluate(
  async () => (await (await fetch('/api/cloud/status')).json()));

if (before.account.configured) {
  skip('Connect is not offered before the client is saved',
    'this console already has a Google client saved');
} else {
  ok('Connect is not offered before the client is saved',
    await page.evaluate(() => document.querySelector('#cl-connect').disabled));
}

await page.fill('#cl-client-id', '1234-test.apps.googleusercontent.com');
await page.fill('#cl-client-secret', 'test-secret-value');
await page.click('#cl-save-client');
await page.waitForTimeout(700);

ok('Connect becomes available once it is',
  await page.evaluate(() => !document.querySelector('#cl-connect').disabled));
ok('the secret is not left sitting in the field',
  await page.inputValue('#cl-client-secret') === '');

const leaked = await page.evaluate(
  () => document.body.innerHTML.includes('test-secret-value'));
ok('the secret is nowhere in the page after saving', !leaked);

// --- switching it on -------------------------------------------------------

await page.check('#cl-enabled');
await page.waitForTimeout(700);
await page.reload({ waitUntil: 'networkidle' });
await page.click('#tabs button[data-tab="cloud"]');
await page.waitForTimeout(900);

ok('cloud backup stays on across a reload',
  await page.isChecked('#cl-enabled'));

// --- queueing --------------------------------------------------------------

await page.click('#cl-queue');
await page.waitForTimeout(1200);
const firstToast = await page.evaluate(
  () => document.querySelector('.toasts')?.textContent || '');
if (before.queue.total) {
  skip('queueing the library says how many it found',
    'this console had already queued its library');
} else {
  ok('queueing the library says how many it found',
    /\d+ new files? queued/.test(firstToast), firstToast);
}

const waiting = Number(await page.textContent('#cl-pending'));
ok('the waiting count is what was queued', waiting > 0, String(waiting));

await page.click('#cl-queue');
await page.waitForTimeout(1200);
const secondToast = await page.evaluate(
  () => document.querySelector('.toasts')?.textContent || '');
ok('a second check does not claim to have found the library again',
  /Nothing new/.test(secondToast), secondToast);

// --- the cards agree with the API -----------------------------------------

const api = await page.evaluate(async () => (await (await fetch('/api/cloud/status')).json()));
ok('the Uploaded card matches the record',
  Number(await page.textContent('#cl-done')) === api.queue.done);
ok('the Waiting card matches the record',
  Number(await page.textContent('#cl-pending'))
    === api.queue.pending + api.queue.uploading);
ok('status carries no refresh token',
  !JSON.stringify(api).includes('refresh_token'));

// --- start is refused sensibly without an account -------------------------

ok('Start is not offered with no Google account connected',
  await page.evaluate(() => document.querySelector('#cl-start').disabled));

// --- the poll is only for the tab you are looking at ----------------------

statusCalls.length = 0;
await page.click('#tabs button[data-tab="overview"]');
await page.waitForTimeout(5000);
ok('leaving the tab stops the polling', statusCalls.length === 0,
  `${statusCalls.length} calls after leaving`);

ok('still no script errors', errs.length === 0, errs.join(' | '));

await done(b);
