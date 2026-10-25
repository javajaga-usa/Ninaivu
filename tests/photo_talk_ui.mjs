/**
 * Talk about this photo, in a browser: hidden until the console switches it
 * on; the viewer's menu offers it on a photograph the AI may read and not on
 * one it may not; the panel shows suggestions, a question tapped or typed is
 * answered in a bubble beneath it, the earlier turns go with the next
 * question, and Escape closes the panel and leaves the viewer open.
 *
 * The model is not on the runner, so the two answers that need it (whether
 * it is ready, and what it says) are answered here.
 */
import { launch, ok, done, ADMIN, ADMIN_USER, ADMIN_PASS, signInAtHome } from './harness.mjs';

const browser = await launch();
// The page's own service worker would answer GETs before page.route sees them.
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, serviceWorkers: 'block' });
const page = await context.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
const shots = process.env.NINAIVU_SHOTS;
const shot = (name) => (shots ? page.screenshot({ path: `${shots}/${name}.png` }) : null);

async function moreMenu() {
  if (await page.locator('#v-more').isVisible()) await page.click('#v-more');
}

async function switches(on) {
  const consolePage = await context.newPage();
  await consolePage.goto(ADMIN, { waitUntil: 'domcontentloaded' });
  const answer = await consolePage.evaluate(async ([username, password, value]) => {
    const headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    await fetch('/api/auth/login', { method: 'POST', headers, body: JSON.stringify({ username, password }) });
    const r = await fetch('/api/admin/settings/all', {
      method: 'POST', headers, body: JSON.stringify({ settings: { photo_talk: value } }),
    });
    return r.status;
  }, [ADMIN_USER, ADMIN_PASS, on]);
  await consolePage.close();
  return answer;
}

// On by default since 3.1: turned off first, to see what off looks like.
ok('the console switches it off', (await switches(false)) === 200);
await signInAtHome(page);
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });

const [first, second] = await page.evaluate(async () => {
  const { items } = await (await fetch('/api/assets?limit=100')).json();
  const shown = new Set([...document.querySelectorAll('#grid .cell[data-id]')].map((c) => Number(c.dataset.id)));
  return items.filter((i) => i.kind === 'picture' && shown.has(i.id)).slice(0, 2).map((i) => i.id);
});
ok('two photographs to talk about', Number.isFinite(first) && Number.isFinite(second));

await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await page.waitForTimeout(400);
ok('off: not offered', await page.locator('#v-talk').isHidden());
await page.keyboard.press('Escape');

ok('the console switches it on', (await switches(true)) === 200);
await page.reload({ waitUntil: 'networkidle' });
await page.waitForFunction(() => document.querySelectorAll('#grid .cell').length > 0, { timeout: 15000 });

// The first photograph may be talked about; the second is kept from the AI.
const asked = [];
await page.route(new RegExp(`/api/asset/${first}/talk$`), async (route) => {
  if (route.request().method() === 'POST') {
    const body = route.request().postDataJSON();
    asked.push(body);
    await new Promise((r) => setTimeout(r, 400));
    const answer = /[஀-௿]/.test(body.question)
      ? 'பலகையில் "மீனாட்சி அம்மன் கோவில்" என்று எழுதியிருக்கிறது.'
      : 'This looks like a temple gateway in the evening, with a crowd gathered for a festival.';
    await route.fulfill({ json: { answer, language: /[஀-௿]/.test(body.question) ? 'ta' : 'en', model: 'text-model', took: 2.1 } });
  } else {
    await route.fulfill({ json: { on: true, allowed: true, kept: '', ready: true, why: '', max_question: 300 } });
  }
});
await page.route(new RegExp(`/api/asset/${second}/talk$`), (route) => route.fulfill({
  json: { on: true, allowed: false, kept: 'This photograph is hidden, so it is never shown to the AI.',
    ready: true, why: '', max_question: 300 },
}));

await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await moreMenu();
await page.waitForSelector('#v-talk:not([hidden])', { timeout: 8000 });
ok('on: offered in the menu', (await page.locator('#v-talk').textContent()).includes('Talk about this photo'));
await page.click('#v-talk');
await page.waitForSelector('#viewer-talk:not([hidden])', { timeout: 5000 });
ok('four suggestions to start with', await page.locator('#talk-chips .talk-chip').count() === 4);
ok('the box takes the typing', await page.evaluate(() => document.activeElement?.id === 'talk-input'));
await page.waitForTimeout(400);
await shot('talk-open');

await page.click('#talk-chips .talk-chip >> nth=2');
await page.waitForSelector('.talk-thinking', { timeout: 3000 });
ok('it says it is looking', (await page.locator('.talk-thinking').textContent()).includes('Looking at the photo'));
await page.waitForSelector('.talk-a:not(.talk-thinking)', { timeout: 5000 });
ok('the suggestion was asked', asked[0]?.question === 'What is happening here?' && asked[0].turns.length === 0);
ok('the answer is beneath it', (await page.locator('.talk-a').textContent()).includes('temple gateway'));
ok('the suggestions step aside', await page.locator('#talk-chips').isHidden());

await page.fill('#talk-input', 'பலகையில் என்ன எழுதியிருக்கிறது?');
// Typing in the box is for the box: the arrow keys and letters are not the viewer's.
await page.keyboard.press('ArrowRight');
await page.keyboard.press('Enter');
await page.waitForFunction(() => document.querySelectorAll('.talk-a:not(.talk-thinking)').length === 2, null, { timeout: 5000 });
ok('the panel stayed open on the photograph', await page.locator('#viewer-talk').isVisible());
ok('the talk so far went with the question',
  asked[1]?.turns.length === 1 && asked[1].turns[0].q === 'What is happening here?');
ok('a Tamil question, a Tamil answer', (await page.locator('.talk-a >> nth=1').textContent()).includes('மீனாட்சி'));
await page.waitForTimeout(250);
await shot('talk-answered');

// Another photograph, one the AI may not read: not offered.
await page.keyboard.press('Escape');
ok('Escape closes the panel and leaves the viewer open',
  await page.locator('#viewer-talk').isHidden() && await page.locator('#viewer').isVisible());
await page.waitForTimeout(300);
await page.keyboard.press('Escape');
await page.waitForSelector('#viewer', { state: 'hidden', timeout: 5000 });
await page.click(`#grid .cell[data-id="${second}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await page.waitForTimeout(500);
ok('a photograph kept from the AI is not offered', await page.locator('#v-talk').isHidden());
await page.keyboard.press('Escape');
await page.waitForSelector('#viewer', { state: 'hidden', timeout: 5000 });

// Back to the first: closing the viewer let its talk go.
await page.click(`#grid .cell[data-id="${first}"]`);
await page.waitForSelector('#viewer:not([hidden])', { timeout: 8000 });
await moreMenu();
await page.click('#v-talk');
await page.waitForSelector('#viewer-talk:not([hidden])', { timeout: 5000 });
ok('a reopened viewer starts a fresh talk',
  await page.locator('#talk-list .talk-q').count() === 0 && await page.locator('#talk-chips').isVisible());

// Dark, for the screenshots.
if (shots) {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.click('#talk-chips .talk-chip >> nth=0');
  await page.waitForSelector('.talk-a:not(.talk-thinking)', { timeout: 5000 });
  await page.waitForTimeout(250);
  await shot('talk-dark');
}

ok('no script errors', errors.length === 0, errors.join(' | '));
await switches(false);
await done(browser);
