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

/* ---------- the three scan passes ---------- */

await p.click('#tabs button[data-tab="library"]');
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
ok('the faces switch says it can be stopped part-way',
  (await p.locator('p.hint', { hasText: 'carries on from there' }).count()) === 1);

/* ---------- Gemini ---------- */

await p.click('#tabs button[data-tab="ai-models"]');
await p.waitForTimeout(1500);

const block = p.locator('#gemini-block');
ok('the AI models page has a Gemini block', await block.isVisible());
ok('and it says a photograph is sent to Google',
  /sent to Google/.test(await block.innerText()));
ok('which it keeps apart from the models that never leave the machine',
  (await p.locator('#gemini-block').evaluate(
    (el) => !el.previousElementSibling?.querySelector('#gemini-form'))));

const state = (await p.locator('#gemini-state').textContent()).trim();
ok('its state is shown', state.length > 0, state);
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

ok('no page errors', errs.length === 0, errs.join(' | '));
await done(b);
