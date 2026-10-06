/**
 * The administrator has a tile on the family app's "Who's watching?" screen,
 * as in Ninaivu Lite: their picture (or initials) and name, with a lock.
 * Tapping it asks for the password, and the right one signs them in with
 * their face on the profile button.
 */
import { launch, ok, done, HOME, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const b = await launch();
const page = await b.newPage({ viewport: { width: 1280, height: 800 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

const profiles = await (await page.request.get(`${HOME}/api/auth/profiles`)).json();
const admin = (profiles.profiles || []).find((p) => p.role === 'admin');
ok('the picker lists the administrator', !!admin, JSON.stringify(profiles));

await page.goto(HOME, { waitUntil: 'networkidle' });
const tile = page.locator('.picker-tile', { hasText: admin?.name || ADMIN_USER }).first();
await tile.waitFor({ timeout: 15000 });
ok('their tile shows a face', await tile.locator('.avatar').count() === 1);
ok('and is locked', await tile.locator('.picker-lock').count() === 1);
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-picker.png` });

await tile.click();
const input = page.locator('.gate-form input[name="secret"]');
await input.waitFor({ timeout: 5000 });
ok('it asks for the password, not a PIN',
   await input.getAttribute('autocomplete') === 'current-password');

await input.fill('not the password');
await page.locator('.gate-submit').click();
await page.locator('.gate-error:not([hidden])').waitFor({ timeout: 5000 });
ok('a wrong password is refused', await page.locator('.gate-card').isVisible());

await input.fill(ADMIN_PASS);
await page.locator('.gate-submit').click();
await page.waitForFunction(() => document.querySelector('#profile-btn .avatar'), null,
                           { timeout: 15000 });
const me = await page.evaluate(() => fetch('/api/me').then((r) => r.json()));
ok('the right one signs the administrator in', me.role === 'admin' && !me.anonymous,
   JSON.stringify(me));
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-signed-in.png` });
ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
