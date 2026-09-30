/**
 * What the Visibility page offers at the top of the tree, and what a folder
 * costs to hide.
 *
 * This once tested a whole-library password gate: Nobody on the whole library
 * asked for the password, and so did putting it back. The page no longer has a
 * control on the whole library at all — visibility is set on a folder, or on
 * the photographs themselves — so there is no such gate to test. What is worth
 * keeping is the other half: the top row says where to set it, and hiding a
 * named folder is one click, with no password.
 *
 *   node tests/visibility_password_ui.mjs      (wants an instance on 3000)
 * @fixture visibility
 */
import { launch, ok, done, HOME, ADMIN } from './harness.mjs';
const SHOTS = process.env.NINAIVU_SHOTS || '.';
const b = await launch();

const p = await b.newPage({ viewport: { width: 1500, height: 1000 } });
const errs=[]; p.on('pageerror', e=>errs.push(e.message));
await p.goto(ADMIN, { waitUntil: 'networkidle' });
await p.fill('#gate input[type="text"]', 'dad');
await p.fill('#gate input[type="password"]', 'correcthorse1');
await p.click('#gate .btn.primary');
await p.waitForSelector('.admin-person', { state:'attached', timeout:15000 });
await p.click('#tabs button[data-tab="visibility"]');
await p.waitForTimeout(1200);



/* the whole-library row is a label, not a control */
ok('the whole-library row offers no buttons', await p.evaluate(() => {
  const row = document.querySelector('#folder-tree').firstElementChild;
  return row.classList.contains('is-root') && row.querySelectorAll('button').length === 0;
}));
ok('and says where to set it', await p.evaluate(() =>
  /set on a folder/i.test(document.querySelector('#folder-tree .folder-note')?.textContent || '')));

/* a named folder is one click: no password. It is left hidden, which is the
   starting point visibility_ui (run next, on the same library) expects. */
const hiddenBefore = await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total);
ok('nothing is hidden to begin with', hiddenBefore === 0, String(hiddenBefore));
await p.evaluate(() => {
  const rows = [...document.querySelectorAll('#folder-tree > *')];
  const personal = rows.find((r) => /personal/.test(r.textContent));
  [...personal.querySelectorAll('button')].find((x) => /nobody/i.test(x.textContent)).click();
});
await p.waitForTimeout(1200);
ok('hiding a named folder asks for no password',
   await p.evaluate(() => !document.querySelector('#vispw-modal') || document.querySelector('#vispw-modal').hidden));
ok('and it is done: the three personal photographs are hidden', await p.evaluate(async () =>
  (await (await fetch('/api/assets?limit=99&visibility=hidden')).json()).total) === 3);

ok('no page errors', errs.length === 0, errs.join('; '));
await p.screenshot({ path: `${SHOTS}/pw-modal.png` });
await b.close();
