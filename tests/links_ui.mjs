/**
 * The two cross-app links, clicked for real in both directions.
 *
 * The console link on the home page used to sit at href="#" because
 * /api/status carried no admin_port, and the "Family app" link was only
 * given an href when the Library tab happened to render. Both now come from
 * the address the browser is already using, so they work from a phone.
 *
 *   node tests/links_ui.mjs        (wants an instance on 3000 and 5000)
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const b = await launch();

/* Reach the console the way a phone would — by IP, not localhost — and check
   the link it offers actually leads somewhere that loads. */
const admin = await b.newPage({ viewport: { width: 1280, height: 900 } });
await admin.goto(ADMIN, { waitUntil: 'networkidle' });
await admin.fill('#gate input[type="text"]', 'dad');
await admin.fill('#gate input[type="password"]', 'correcthorse1');
await admin.click('#gate .btn.primary');
await admin.waitForSelector('.admin-person', { state: 'attached', timeout: 15000 });
await admin.waitForTimeout(1200);

const homeHref = await admin.evaluate(() => document.querySelector('#open-home').href);
ok('the console link keeps the address the browser used',
   homeHref.replace(/\/$/, '') === HOME, homeHref);

// Follow it for real.
const [famPage] = await Promise.all([
  admin.context().waitForEvent('page'),
  admin.click('#open-home'),
]);
await famPage.waitForLoadState('networkidle');
ok('clicking "Family app" actually loads the family app',
   /Ninaivu|profile|gallery/i.test(await famPage.title() + await famPage.content().then(c=>c.slice(0,400))),
   famPage.url());
// The port the runner happened to pick, not 5000: that was the default when
// this was written, and a test that names it only passes where nobody chose.
ok('and it landed on the right port',
   famPage.url().startsWith(HOME), `${famPage.url()} is not under ${HOME}`);

/* Now the other direction. */
const home = await b.newPage({ viewport: { width: 1280, height: 900 } });
await home.goto(HOME, { waitUntil: 'networkidle' });
await home.evaluate(async () => {
  await fetch('/api/auth/login', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ username:'dad', password:'correcthorse1' }) });
});
await home.reload({ waitUntil: 'networkidle' });
await home.waitForTimeout(1500);

const adminHref = await home.evaluate(() => document.querySelector('#console-link').href);
ok('the family app links back to the console on the same host',
   adminHref.replace(/\/$/, '') === ADMIN, adminHref);

const [conPage] = await Promise.all([
  home.context().waitForEvent('page'),
  home.click('#console-link'),
]);
await conPage.waitForLoadState('networkidle');
ok('clicking it actually loads the console',
   conPage.url().startsWith(ADMIN), `${conPage.url()} is not under ${ADMIN}`);
ok('the console responds rather than erroring',
   (await conPage.content()).length > 500, `${(await conPage.content()).length} bytes`);

await b.close();
