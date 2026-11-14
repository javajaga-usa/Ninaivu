/**
 * The week's health (static/js/health-week.js): the console's Overview shows
 * the four parts of the last seven days under "Is everything safe?", and the
 * family app shows an administrator a card that can be put away until the
 * next week. A family member is shown nothing.
 */
import { launch, ok, done, HOME, ADMIN, signInAtHome, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];

// -- the console ---------------------------------------------------------------
const consolePage = await b.newPage({ viewport: { width: 1280, height: 900 } });
consolePage.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(consolePage);
await consolePage.goto(`${ADMIN}/#overview`, { waitUntil: 'networkidle' });
await consolePage.locator('#hw-block:not([hidden]) #hw-parts li').first().waitFor({ timeout: 20000 });
const parts = await consolePage.locator('#hw-parts .sf-title').allTextContents();
ok('the Overview shows the four parts of the week',
   JSON.stringify(parts) === JSON.stringify(['Damaged files', 'Free space', 'Failed jobs', 'Drives']),
   JSON.stringify(parts));
const head = await consolePage.locator('#hw-headline').textContent();
ok('with a headline and the dates it covers', /week/i.test(head) && head.includes('–'), head);
if (process.env.SHOT) await consolePage.locator('#hw-block').screenshot({ path: `${process.env.SHOT}-console.png` });

// -- the family app, as an administrator --------------------------------------------
const home = await b.newPage({ viewport: { width: 1280, height: 900 } });
home.on('pageerror', (e) => errs.push(e.message));
await signInAtHome(home);
await home.goto(HOME, { waitUntil: 'networkidle' });
const card = home.locator('.week-card:not([hidden])');
await card.waitFor({ timeout: 20000 });
ok('the family app shows the administrator the week', await card.locator('.week-card-part').count() === 4);
const link = await card.locator('a.btn').getAttribute('href');
ok('with a way to the console', /^https?:\/\/[^/]+:\d+$/.test(link || ''), link);
if (process.env.SHOT) await card.screenshot({ path: `${process.env.SHOT}-app.png` });
await card.getByRole('button', { name: 'Hide until next week' }).click();
ok('it can be put away', await home.locator('.week-card:not([hidden])').count() === 0);
await home.reload({ waitUntil: 'networkidle' });
await home.waitForTimeout(1500);
ok('and stays away for the rest of the week', await home.locator('.week-card:not([hidden])').count() === 0);

// -- a family member -------------------------------------------------------------
const family = await b.newContext();
const theirs = await family.newPage();
const answer = await theirs.request.get(`${HOME}/api/health/week`);
ok('somebody not signed in as an administrator is refused', [401, 403].includes(answer.status()),
   String(answer.status()));

ok('no console errors', errs.length === 0, errs.join('; '));
await done(b);
