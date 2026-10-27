/**
 * Maintenance mode, as a family member sees it: a gallery that was already
 * open is covered by the downtime notice, a refresh shows the notice rather
 * than a broken page, and the gallery comes back by itself when it is over.
 */
import { launch, ok, done, HOME, ADMIN, signInAtHome } from './harness.mjs';

const b = await launch();
const consoleCtx = await b.newContext();
const consolePage = await consoleCtx.newPage();
await consolePage.goto(ADMIN, { waitUntil: 'networkidle' });
const asAdmin = (path, body) => consolePage.evaluate(async ([p, data]) => {
  const response = await fetch(p, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return { status: response.status, body: await response.json().catch(() => null) };
}, [path, body]);
await consolePage.evaluate(async () => {
  await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'dad', password: 'correcthorse1' }),
  });
});
await asAdmin('/api/people', {
  username: 'downtimekid', password: 'summerdays24', name: 'Downtime Kid',
  role: 'family', must_change: false,
});

const page = await b.newPage({ viewport: { width: 1280, height: 800 } });
const errs = []; page.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(page, 'downtimekid', 'summerdays24');
const me = await page.evaluate(() => fetch('/api/me').then((r) => r.json()));
ok('the family member is signed in', me.username === 'downtimekid' && me.role !== 'admin',
   JSON.stringify(me));

const started = await asAdmin('/api/admin/workload', { mode: 'maintenance' });
ok('maintenance starts', started.status === 200, JSON.stringify(started));

const notice = page.locator('dialog.downtime-screen');
// Someone looks at the tab again (the page also asks every minute).
await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
try { await notice.waitFor({ state: 'visible', timeout: 15000 }); } catch {}
ok('the open gallery is covered by the notice without anyone touching it',
   await notice.isVisible());

await page.reload({ waitUntil: 'networkidle' });
try { await notice.waitFor({ state: 'visible', timeout: 10000 }); } catch {}
ok('a refresh shows the notice', await notice.isVisible());
ok('saying it is down for maintenance',
   /maintenance/i.test(await notice.innerText().catch(() => '')));
if (process.env.SHOT) await page.screenshot({ path: `${process.env.SHOT}-down.png` });

const ended = await asAdmin('/api/admin/workload', { leave_maintenance: true });
ok('maintenance ends', ended.status === 200, JSON.stringify(ended));
try { await notice.waitFor({ state: 'detached', timeout: 30000 }); } catch {}
await page.waitForLoadState('networkidle');
ok('the gallery comes back by itself', await notice.count() === 0
   && !(await page.evaluate(() => document.body.classList.contains('screen-down'))));
ok('no console errors', errs.length === 0, errs.join('; '));

await done(b);
