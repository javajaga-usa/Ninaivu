/**
 * The first day's "new library, or bring one in?" step, in a real browser.
 *
 *   · it comes after "how many computers", before the library folder
 *   · bringing one in shows the three steps, and Next becomes Go to Import
 *   · Go to Import opens the Move to another computer page, the first day
 *     left open for next time
 *   · starting a new library goes on to the library folder as before
 *
 *   node tests/first_day_start_ui.mjs
 */
import { launch, ok, done, ADMIN, signInAtConsole } from './harness.mjs';

const b = await launch();
const errs = [];
const page = await b.newPage({ viewport: { width: 1280, height: 900 } });
page.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(page);
await page.goto(ADMIN, { waitUntil: 'networkidle' });
await page.waitForSelector('#first-day', { state: 'attached', timeout: 20000 });

await page.evaluate(async () => {
  const { FirstDay } = await import('/static/js/first-day.js');
  const answers = {
    '/api/admin/first-day': { done: false, library: { chosen: false, root: '' }, people: 0,
      ai: {}, faces_model: false, image_model: {}, backup: { enabled: false } },
    '/api/admin/mugil': { role: 'single' },
  };
  window.__posted = [];
  window.__opened = null;
  const fd = new FirstDay({
    json: async (url, options) => {
      if (options?.method === 'POST') { window.__posted.push(url); return {}; }
      return answers[url] || {};
    },
    toast: () => {}, pickFolder: () => {}, refresh: () => {},
    openPage: (name) => { window.__opened = name; },
  });
  window.__fd = fd;
  await fd.maybeOpen();
});
await page.waitForSelector('#first-day:not([hidden])', { timeout: 5000 });
const steps = await page.$$eval('#fd-steps li', (lis) => lis.map((li) => li.dataset.step));
ok('the choice comes after the computers and before the library',
  steps.indexOf('start') === steps.indexOf('computers') + 1
  && steps.indexOf('library') === steps.indexOf('start') + 1, JSON.stringify(steps));

await page.click('#fd-next');
await page.waitForFunction(() => /bring one from another computer/.test(document.querySelector('#fd-title').textContent));
ok('two choices, new first', (await page.$$('#fd-body input[name="fd-beginning"]')).length === 2
  && await page.isChecked('#fd-body input[value="new"]'));
ok('Next goes on to the library for a new one', (await page.textContent('#fd-next')).trim() === 'Next');

await page.check('#fd-body input[value="import"]');
await page.waitForSelector('#fd-body .fd-how', { timeout: 5000 });
ok('bringing one in shows three steps', (await page.$$('#fd-body .fd-how > li')).length === 3);
ok('with the folder to choose here', /Choose the photographs folder/.test(await page.textContent('#fd-body .fd-how')));
ok('Next says Go to Import', (await page.textContent('#fd-next')).trim() === 'Go to Import');

await page.click('#fd-next');
await page.waitForFunction(() => window.__opened === 'migration', null, { timeout: 5000 });
ok('it opens the Move to another computer page', await page.evaluate(() => window.__opened) === 'migration');
ok('and closes the first day without finishing it',
  await page.evaluate(() => document.querySelector('#first-day').hidden
    && !window.__posted.includes('/api/admin/first-day')));

await page.evaluate(() => { window.__fd.beginning = 'new'; window.__fd.step = 1; window.__fd.render(); document.querySelector('#first-day').hidden = false; });
await page.click('#fd-next');
await page.waitForFunction(() => /Where are the photographs/.test(document.querySelector('#fd-title').textContent));
ok('a new library goes on to the folder', true);

const phone = await b.newPage({ viewport: { width: 390, height: 844 } });
phone.on('pageerror', (e) => errs.push(e.message));
await signInAtConsole(phone);
await phone.goto(ADMIN, { waitUntil: 'networkidle' });
await phone.evaluate(async () => {
  const { FirstDay } = await import('/static/js/first-day.js');
  const fd = new FirstDay({ json: async (url) => (url === '/api/admin/mugil' ? { role: 'single' }
    : { done: false, library: { chosen: false, root: '' }, people: 0, ai: {}, backup: {} }),
  toast: () => {}, pickFolder: () => {}, refresh: () => {}, openPage: () => {} });
  await fd.maybeOpen();
  fd.beginning = 'import'; fd.step = 1; fd.render();
});
const card = await phone.evaluate(() => {
  const r = document.querySelector('#first-day .fd-card').getBoundingClientRect();
  return { right: r.right, width: window.innerWidth };
});
ok('Phone: the steps fit the screen', card.right <= card.width + 1, JSON.stringify(card));

ok('no script errors', errs.length === 0, errs.join('\n'));
await b.close();
done();
