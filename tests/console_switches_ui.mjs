/**
 * The switches a household could not reach, and the Gemini key it could not
 * give — both on the console now.
 *
 * Naming places, reading text and finding faces were all off by default with
 * no switch anywhere; Gemini was wired in with no way to give it a key. This
 * checks they are on the page, that a switch reaches the server, and that the
 * Gemini block says out loud what makes it different from everything else on
 * its page: a photograph used with it is sent to Google.
 *
 * It does not save a Gemini key. Saving checks the key with Google, and a
 * browser test has no business sending one there.
 *
 *   node tests/console_switches_ui.mjs   (wants the console up and admin
 *                                         dad / correcthorse1)
 */
import { launch, ok, done, ADMIN } from './harness.mjs';

const b = await launch();
const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
const errs = [];
p.on('pageerror', (e) => errs.push(e.message));

await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state: 'attached', timeout: 20000 });
await p.waitForTimeout(1500);

const stored = (key) => p.evaluate(async (k) =>
  (await (await fetch('/api/admin/overview')).json()).app[k], key);

/* ---------- the three scan passes, on the AI models page ---------- */

await p.click('#tabs button[data-tab="ai-models"]');
await p.waitForTimeout(1200);

for (const [key, label] of [
  ['place_names', 'Name the places photographs were taken'],
  ['ocr_enabled', 'Read the words in photographs'],
  ['faces_enabled', 'Find the people in photographs'],
]) {
  const row = p.locator('label.toggle', { hasText: label });
  ok(`the console offers "${label}"`, await row.count() === 1);
  const box = row.locator('input[type="checkbox"]');
  ok(`and it shows what the server has for ${key}`,
    await box.isChecked() === Boolean(await stored(key)));

  const was = await box.isChecked();
  await (was ? box.uncheck() : box.check());
  await p.waitForTimeout(900);
  ok(`switching ${key} reaches the server`, await stored(key) === !was,
    String(await stored(key)));
  await (was ? box.check() : box.uncheck());          // leave it as it was
  await p.waitForTimeout(900);
}

ok('the place-names switch says how long it takes',
  (await p.locator('p.hint', { hasText: 'take seconds' }).count()) === 1);
// The page also carries one general line about every pass; this is the faces switch's own.
ok('the faces switch says it can be stopped part-way',
  (await p.locator('p.hint', { hasText: 'Groups photographs by who is in them' })
    .filter({ hasText: 'carries on from there' }).count()) === 1);

/* ---------- the rules for everyone, on Visibility ---------- */

await p.click('#tabs button[data-tab="visibility"]');
await p.waitForTimeout(1200);
for (const label of ['Let visitors browse public media without signing in',
                     'Screen explicit content and hide it behind a toggle']) {
  ok(`Visibility carries "${label}"`,
    await p.locator('#access-settings label.toggle', { hasText: label }).count() === 1);
}
ok('and the date rule, before the folders',
  await p.locator('#access-settings h3', { hasText: 'date visibility' }).count() === 1);

/* ---------- Extensions, on System -> Settings ---------- */

await p.click('#tabs button[data-tab="settings"]');
await p.waitForTimeout(1500);

const extBlock = p.locator('#extensions-block');
ok('the Settings page has an Extensions block', await extBlock.isVisible());
ok('and it says a change takes effect at the next start',
  /next started/.test(await extBlock.innerText()));
const listing = await p.evaluate(async () =>
  (await fetch('/api/admin/extensions')).json());
ok('the page is told which extensions are installed and which are on',
  Array.isArray(listing.extensions) && Array.isArray(listing.active));
for (const ext of listing.extensions) {
  const row = p.locator('.ext-row', { hasText: ext.title });
  ok(`${ext.title} has a row with a switch`, await row.locator('input[type=checkbox]').count() === 1);
  if (ext.data_leaves_the_machine) {
    ok(`${ext.title} says something leaves this computer`,
      /leaves this computer/.test(await row.innerText()));
  }
}
const geminiOn = listing.active.includes('gemini');
await p.click('#tabs button[data-tab="ai-models"]');
await p.waitForTimeout(1200);
ok('the Gemini key form, on AI models, is shown only while that extension is on',
  (await p.locator('#gemini-block').isVisible()) === geminiOn);
if (geminiOn) {
  const block = p.locator('#gemini-block');
  ok('and it says a photograph is sent to Google',
    /sent to Google/.test(await block.innerText()));
  ok('the key field is a password field',
    await p.locator('#gemini-key').getAttribute('type') === 'password');
  const status = await p.evaluate(async () =>
    (await fetch('/api/admin/gemini')).json());
  ok('the page is told whether there is a key, and nothing more',
    Object.keys(status).sort().join() === 'hint,set,source,variable',
    JSON.stringify(Object.keys(status)));
  ok('Remove is offered only when there is a key to remove',
    await p.locator('#gemini-remove').isHidden() === !status.set
    || status.source === 'environment');
}

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
