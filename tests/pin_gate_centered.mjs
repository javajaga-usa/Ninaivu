/**
 * Bug: on a narrow phone, the PIN sign-in card was pushed off-center to the
 * right and clipped by the screen edge. Root cause: `.pin-input` had no
 * explicit width, so its own intrinsic size (inflated by its large
 * letter-spacing) forced the whole `.gate-card` wider than the viewport
 * could center — the card grew to fit the input instead of the other way
 * around. Fixed by giving `.pin-input` `width: 100%` and `.gate-card`
 * `min-width: 0` (removing the default content-based grid-item minimum that
 * let the blowout happen in the first place).
 */
import { launch, ok, done, HOME, ADMIN, ADMIN_USER, ADMIN_PASS } from './harness.mjs';

const b = await launch();

// Create a PIN-locked family member via the admin API so the gate has a
// locked profile to click through to the PIN screen.
const admin = await b.newPage();
await admin.request.post(`${ADMIN}/api/auth/login`, {
  data: { username: ADMIN_USER, password: ADMIN_PASS },
});
const created = await admin.request.post(`${ADMIN}/api/people`, {
  data: { name: 'PinKid', username: `pinkid${Date.now()}`, role: 'family', pin: '5827' },
});
ok('test fixture: PIN-locked profile created', created.ok(), await created.text());
await admin.close();

const page = await b.newPage({ viewport: { width: 390, height: 844 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));

await page.goto(HOME, { waitUntil: 'networkidle' });
// The administrator's tile is locked too (it asks for the password), so pick
// this profile by name rather than the first lock on the screen.
const tile = '.picker-tile:has(.picker-lock):has-text("PinKid")';
await page.waitForSelector(tile, { timeout: 15000 });
await page.click(tile);
await page.waitForSelector('.pin-input', { timeout: 5000 });
await page.waitForTimeout(150);

const box = await page.evaluate(() => {
  const r = document.querySelector('.gate-card').getBoundingClientRect();
  return { left: r.left, right: r.right, width: r.width, winWidth: window.innerWidth };
});

const leftGap = box.left;
const rightGap = box.winWidth - box.right;
ok('the PIN card stays within the viewport', box.right <= box.winWidth + 0.5,
   `card right edge ${box.right} vs viewport width ${box.winWidth}`);
ok('the PIN card is horizontally centered', Math.abs(leftGap - rightGap) < 1,
   `left gap ${leftGap} vs right gap ${rightGap}`);
ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
