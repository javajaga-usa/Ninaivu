/**
 * The console's People page, "What each person sees": on the administrator's
 * word it counts, for every profile, what the family app lets them open, and
 * says the administrator sees everything. A family member cannot ask.
 */
import { launch, ok, done, HOME, ADMIN, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];

const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);
await page.goto(ADMIN, { waitUntil: 'networkidle' });
await page.click('#tabs button[data-tab="people"]');
await page.waitForSelector('[data-panel="people"].active');
const block = page.locator('#privacy-view-block');
ok('the People page has the block', await block.isVisible());
await page.locator('#privacy-view-btn').click();
await page.locator('#privacy-view-list .pv-person').first().waitFor({ timeout: 20000 });
const rows = await page.locator('#privacy-view-list .pv-person').count();
ok('a row for each profile and one for nobody signed in', rows >= 2, String(rows));
const first = await page.locator('#privacy-view-list .pv-person').first().textContent();
ok('the administrator sees everything', /Sees everything/.test(first || ''), first);
const last = await page.locator('#privacy-view-list .pv-person').last().textContent();
if (process.env.SHOT) await block.screenshot({ path: `${process.env.SHOT}-console.png` });
ok('nobody signed in is shown last', /Nobody signed in/.test(last || ''), last);

const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await phone.goto(ADMIN, { waitUntil: 'networkidle' });
await phone.evaluate(() => document.querySelector('#tabs button[data-tab="people"]')?.click());
await phone.waitForSelector('[data-panel="people"].active');
await phone.locator('#privacy-view-btn').click();
await phone.locator('#privacy-view-list .pv-person').first().waitFor({ timeout: 20000 });
const wide = await phone.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
ok('Phone: nothing wider than the screen', !wide);

const stranger = await (await b.newContext()).newPage();
const refused = await stranger.request.get(`${HOME}/api/privacy/people`);
ok('somebody not signed in as an administrator is refused', [401, 403].includes(refused.status()), String(refused.status()));

ok('no script errors', errs.length === 0, errs.join(' | '));
await done(b);
