/**
 * The next person at the tablet is not shown what the viewer kept for the last.
 *
 * When a session ends under the page (signed out from the console, a password
 * changed elsewhere), the gallery goes back to the profile picker without a
 * reload, and whoever picks next starts on the same page. The viewer kept the
 * photographs it had opened as the last person was shown them, and showed
 * them so again: the camera, where it was taken and the download link, which
 * the server leaves out for a guest or for a profile without downloads.
 */
import { launch, ok, done, HOME, ADMIN_USER, ADMIN_PASS, signInAtConsole, signInAtHome } from './harness.mjs';

const b = await launch();
const ctx = await b.newContext({ viewport: { width: 1280, height: 860 }, serviceWorkers: 'block' });
// A library closed to guests, so a session that ends sends the page back to
// the picker (open to guests, it carries on as a guest).
const consolePage = await ctx.newPage();
await signInAtConsole(consolePage);
const setOpen = (open) => consolePage.evaluate(async (value) => {
  await fetch('/api/admin/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ open_browsing: value }),
  });
}, open);
const wasOpen = (await (await ctx.request.get(`${HOME}/api/auth/state`)).json()).open_browsing;
await setOpen(false);

const p = await ctx.newPage();
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(p);
await p.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, null, { timeout: 20000 });
await p.locator('#grid .cell').first().click();
await p.waitForSelector('.viewer-stage img', { timeout: 8000 });
await p.keyboard.press('Escape');
const kept = await p.evaluate(() => window.__mv.viewer.cache.size);
ok('the viewer keeps what it opened', kept > 0, `${kept}`);

// The session ends elsewhere; the next call finds it gone, and the picker
// comes up on the same page.
await p.evaluate(async () => {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.__mv.reload().catch(() => {});
});
const profiles = await (await p.request.get(`${HOME}/api/auth/profiles`)).json().catch(() => ({}));
const admin = (profiles.profiles || []).find((x) => x.role === 'admin');
const tile = p.locator('.picker-tile', { hasText: admin?.name || ADMIN_USER }).first();
await tile.waitFor({ timeout: 15000 });
ok('the page went back to the picker without a reload',
  await p.evaluate(() => window.__mv.viewer.cache.size) === kept);
await tile.click();
const input = p.locator('.gate-form input[name="secret"]');
await input.waitFor({ timeout: 5000 });
await input.fill(ADMIN_PASS);
await p.locator('.gate-submit').click();
await p.waitForFunction(() => document.querySelector('#profile-btn .avatar'), null, { timeout: 15000 });
await p.waitForTimeout(500);
const after = await p.evaluate(() => window.__mv.viewer.cache.size);
ok('whoever signs in next starts with none of it', after === 0, `${after} kept`);
if (wasOpen) await setOpen(true);
ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
