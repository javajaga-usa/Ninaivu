/**
 * Browser checks for the two front-end defects the audit found: the folder
 * picker's inert text box (AUDIT-12) and the unreachable Switch profile
 * button (AUDIT-14). Wants a running instance on 3000 and 5000.
 *
 *   node tests/ui_fixes.mjs
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const b = await launch();

/* ---- AUDIT-12: the folder picker's text box ---- */
const admin = await b.newPage({ viewport: { width: 1280, height: 900 } });
const errs = []; admin.on('pageerror', e => errs.push(e.message));
await admin.goto(ADMIN, { waitUntil: 'networkidle' });
await admin.fill('#gate input[type="text"]', 'dad');
await admin.fill('#gate input[type="password"]', 'correcthorse1');
await admin.click('#gate .btn.primary');
await admin.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });
await admin.click('#tabs button[data-tab="library"]');
await admin.waitForTimeout(400);
await admin.click('#change-root');
await admin.waitForSelector('#folder-modal:not([hidden])', { timeout: 5000 });
await admin.waitForTimeout(700);

// Park the tree on a folder that cannot be chosen as a library root.
await admin.evaluate(() => {
  const row = [...document.querySelectorAll('#fm-dirs button, #fm-dirs .fm-row')]
    .find(n => /^\/(etc|usr|bin)?$/.test(n.textContent.trim()) || n.textContent.trim() === '/');
  if (row) row.click();
});
await admin.waitForTimeout(700);
const disabledBefore = await admin.evaluate(() => document.querySelector('#fm-apply').disabled);

await admin.fill('#fm-manual', '/tmp/ninaivu-ui/lib');
await admin.waitForTimeout(300);
const enabledAfter = await admin.evaluate(() => !document.querySelector('#fm-apply').disabled);
ok('typing a path enables "Use this folder"', enabledAfter,
   `disabled before typing=${disabledBefore}, after=${!enabledAfter}`);

// A stale typed path must not beat a folder picked afterwards from the tree.
await admin.evaluate(() => {
  const row = [...document.querySelectorAll('#fm-dirs button, #fm-dirs .fm-row')][1];
  if (row) row.click();
});
await admin.waitForTimeout(700);
const cleared = await admin.evaluate(() => document.querySelector('#fm-manual').value);
ok('picking from the tree clears the stale typed path', cleared === '', `still "${cleared}"`);
ok('no console errors in the console app', errs.length === 0, errs.join('; '));

/* ---- AUDIT-14: Switch profile ---- */
const home = await b.newPage({ viewport: { width: 1280, height: 900 } });
const herrs = []; home.on('pageerror', e => herrs.push(e.message));
await home.goto(HOME, { waitUntil: 'networkidle' });
await home.waitForTimeout(1200);
const anonHidden = await home.evaluate(() => document.querySelector('#switch-btn')?.hidden);
ok('Switch profile stays hidden for an anonymous visitor', anonHidden === true, String(anonHidden));

await home.evaluate(async () => {
  await fetch('/api/auth/login', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }) });
});
await home.reload({ waitUntil: 'networkidle' });
await home.waitForTimeout(1200);
const shown = await home.evaluate(() => {
  const el = document.querySelector('#switch-btn');
  return el && !el.hidden && el.getBoundingClientRect().width > 0;
});
ok('Switch profile is visible and clickable once signed in', shown === true, String(shown));
ok('no console errors in the family app', herrs.length === 0, herrs.join('; '));

await b.close();
